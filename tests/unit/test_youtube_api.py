"""The YouTube client (#81) against a fake wire: no request leaves the machine.

`httpx.MockTransport` stands in for Google, so what is checked is exactly
what would be sent: the endpoints, the parameters, and what each answer is
turned into.
"""

from __future__ import annotations

import json
import stat
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from cueclock.youtube import api
from cueclock.youtube.api import Account, DeviceCode, SignedOutError, YouTube, YouTubeError

Handler = Callable[[httpx.Request], httpx.Response]


def _config(tmp_path: Path, **extra: str) -> Path:
    path = tmp_path / "youtube.json"
    path.write_text(json.dumps({"client_id": "cid", "client_secret": "shh"} | extra))
    return path


def _signed_in(tmp_path: Path, handler: Handler) -> Account:
    return Account(_config(tmp_path, refresh_token="rt"), transport=httpx.MockTransport(handler))


def _token(request: httpx.Request) -> httpx.Response | None:
    """Answer a token refresh; None for any other request."""
    if str(request.url) != api.TOKEN_URL:
        return None
    return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})


class Wire:
    """Records every API request and answers each with ``answer``."""

    def __init__(self, answer: Callable[[httpx.Request], httpx.Response] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.answer = answer or (lambda _r: httpx.Response(200, json={}))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        return _token(request) or self._api(request)

    def _api(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.answer(request)


# ---------- the account ----------


def test_the_state_is_read_off_the_file(tmp_path: Path) -> None:
    assert Account(tmp_path / "missing.json").state == "unconfigured"
    assert Account(_config(tmp_path)).state == "signed_out"
    assert Account(_config(tmp_path, refresh_token="rt")).state == "ok"
    (tmp_path / "youtube.json").write_text("{not json")
    assert Account(tmp_path / "youtube.json").state == "unconfigured"


def test_a_token_is_refreshed_once_and_reused_until_it_is_about_to_end(tmp_path: Path) -> None:
    clock = [0.0]
    sent: list[dict[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(parse_qs(request.content.decode()))
        return httpx.Response(200, json={"access_token": f"at{len(sent)}", "expires_in": 3600})

    account = _signed_in(tmp_path, handler)
    account.now = lambda: clock[0]
    assert account.token() == "at1" == account.token()
    assert sent == [
        {
            "client_id": ["cid"],
            "client_secret": ["shh"],
            "grant_type": ["refresh_token"],
            "refresh_token": ["rt"],
        }
    ]
    clock[0] = 3600 - api.TOKEN_MARGIN_S  # a minute before Google says it ends
    assert account.token() == "at2"


def test_a_sign_in_google_no_longer_honours_comes_off_the_file(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": "invalid_grant", "error_description": "Token has been revoked."}
        )

    account = _signed_in(tmp_path, handler)
    with pytest.raises(SignedOutError):
        account.token()
    assert account.state == "signed_out"
    saved = json.loads(account.path.read_text())
    assert "refresh_token" not in saved and saved["client_id"] == "cid"


def test_no_answer_is_an_error_worth_trying_again_not_a_sign_out(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    account = _signed_in(tmp_path, handler)
    with pytest.raises(YouTubeError) as caught:
        account.token()
    assert not isinstance(caught.value, SignedOutError)
    assert caught.value.unreachable and "did not answer" in str(caught.value)
    assert account.state == "ok"


def test_with_nobody_signed_in_no_request_is_made(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("a request was made")

    account = Account(_config(tmp_path), transport=httpx.MockTransport(handler))
    with pytest.raises(SignedOutError):
        account.token()


def test_a_call_carries_the_token_and_reads_youtubes_refusal(tmp_path: Path) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer at"
        return httpx.Response(
            403,
            json={
                "error": {
                    "code": 403,
                    "message": "The authenticated user doesn't have permissions.",
                    "errors": [{"reason": "forbidden"}],
                }
            },
        )

    account = _signed_in(tmp_path, Wire(answer))
    with pytest.raises(YouTubeError) as caught:
        account.call("GET", f"{api.API}/videos", "Reading the video")
    error = caught.value
    assert (error.status, error.reason, error.unreachable) == (403, "forbidden", False)
    assert str(error) == "Reading the video: The authenticated user doesn't have permissions."


def test_a_token_that_ended_early_is_refreshed_and_the_call_made_again(tmp_path: Path) -> None:
    answers = [httpx.Response(401, json={}), httpx.Response(200, json={"ok": 1})]
    wire = Wire(lambda _r: answers.pop(0))
    assert _signed_in(tmp_path, wire).call("GET", f"{api.API}/videos", "x") == {"ok": 1}
    assert len(wire.requests) == 2


def test_youtubes_own_trouble_is_worth_another_try(tmp_path: Path) -> None:
    account = _signed_in(tmp_path, Wire(lambda _r: httpx.Response(503, text="backend error")))
    with pytest.raises(YouTubeError) as caught:
        account.call("GET", f"{api.API}/videos", "Reading the video")
    assert caught.value.unreachable


# ---------- the sign-in from a phone ----------


def test_the_code_flow_stores_the_sign_in_for_this_user_only(tmp_path: Path) -> None:
    answers = [
        httpx.Response(
            200,
            json={
                "device_code": "dc",
                "user_code": "ABCD-EFGH",
                "verification_url": "https://www.google.com/device",
                "expires_in": 1800,
                "interval": 5,
            },
        ),
        httpx.Response(428, json={"error": "authorization_pending"}),
        httpx.Response(403, json={"error": "slow_down"}),
        httpx.Response(
            200,
            json={
                "access_token": "at",
                "refresh_token": "new-rt",
                "expires_in": 3599,
                "scope": api.SCOPE,
            },
        ),
    ]
    sent: list[tuple[str, dict[str, list[str]]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append((str(request.url), parse_qs(request.content.decode())))
        return answers.pop(0)

    account = Account(_config(tmp_path), transport=httpx.MockTransport(handler))
    code = account.request_code()
    assert code == DeviceCode("ABCD-EFGH", "https://www.google.com/device", "dc", 5.0, 1800.0)
    assert sent[0] == (api.DEVICE_URL, {"client_id": ["cid"], "scope": [api.SCOPE]})
    assert account.poll_code(code) is False  # not approved yet
    assert account.poll_code(code) is False  # asked too fast: still not yet
    assert account.poll_code(code) is True
    assert sent[-1][1]["grant_type"] == [api.DEVICE_GRANT]
    saved = json.loads(account.path.read_text())
    assert saved["refresh_token"] == "new-rt" and saved["client_secret"] == "shh"
    assert stat.S_IMODE(account.path.stat().st_mode) == 0o600
    assert account.state == "ok"
    assert account.token() == "at"  # the sign-in's own token, no second request
    assert not answers


def test_a_code_refused_at_google_ends_the_attempt(tmp_path: Path) -> None:
    code = DeviceCode("ABCD-EFGH", "https://www.google.com/device", "dc", 5.0, 1800.0)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "access_denied"})

    account = Account(_config(tmp_path), transport=httpx.MockTransport(handler))
    with pytest.raises(YouTubeError) as caught:
        account.poll_code(code)
    assert caught.value.reason == "access_denied"
    assert account.state == "signed_out"


# ---------- the calls a stream makes ----------


def _body(request: httpx.Request) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(request.content)
    return body


def test_the_stream_keys_are_listed_with_their_ids(tmp_path: Path) -> None:
    wire = Wire(
        lambda _r: httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "s1",
                        "snippet": {"title": "First"},
                        "cdn": {"ingestionInfo": {"streamName": "key-1"}},
                    },
                    {"id": "s2", "snippet": {"title": "No key"}, "cdn": {}},
                ]
            },
        )
    )
    assert YouTube(_signed_in(tmp_path, wire)).streams() == [
        {"id": "s1", "title": "First", "key": "key-1"}
    ]
    assert wire.requests[0].url.params["mine"] == "true"


def test_a_broadcast_is_made_to_start_and_stop_with_obs(tmp_path: Path) -> None:
    wire = Wire(
        lambda _r: httpx.Response(200, json={"id": "vid", "status": {"privacyStatus": "unlisted"}})
    )
    made = YouTube(_signed_in(tmp_path, wire)).create_broadcast(
        "Sam vs Jo | 8-Ball", "unlisted", datetime(2026, 11, 26, 1, 0, tzinfo=UTC)
    )
    assert made["id"] == "vid"
    request = wire.requests[0]
    assert (request.method, request.url.path) == ("POST", "/youtube/v3/liveBroadcasts")
    assert request.url.params["part"] == "snippet,status,contentDetails"
    assert _body(request) == {
        "snippet": {
            "title": "Sam vs Jo | 8-Ball",
            "scheduledStartTime": "2026-11-26T01:00:00+00:00",
        },
        "status": {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": False},
        "contentDetails": {
            "enableAutoStart": True,
            "enableAutoStop": True,
            "monitorStream": {"enableMonitorStream": False},
        },
    }


def test_bind_delete_and_the_two_reads_name_their_video(tmp_path: Path) -> None:
    wire = Wire(lambda _r: httpx.Response(200, json={"items": [{"id": "vid"}]}))
    yt = YouTube(_signed_in(tmp_path, wire))
    yt.bind("vid", "s1")
    yt.delete_broadcast("vid")
    assert yt.broadcast("vid") == {"id": "vid"}
    assert yt.video("vid") == {"id": "vid"}
    assert yt.upcoming() == [{"id": "vid"}]
    seen = [(r.method, r.url.path, dict(r.url.params)) for r in wire.requests]
    assert seen[0] == (
        "POST",
        "/youtube/v3/liveBroadcasts/bind",
        {"id": "vid", "part": "id,contentDetails", "streamId": "s1"},
    )
    assert seen[1] == ("DELETE", "/youtube/v3/liveBroadcasts", {"id": "vid"})
    assert seen[2][2]["id"] == "vid" and seen[3][1] == "/youtube/v3/videos"
    assert seen[4][2]["broadcastStatus"] == "upcoming" and "mine" not in seen[4][2]


def test_the_live_list_asks_for_what_youtube_has_not_ended(tmp_path: Path) -> None:
    wire = Wire(lambda _r: httpx.Response(200, json={"items": [{"id": "vid"}]}))
    assert YouTube(_signed_in(tmp_path, wire)).live() == [{"id": "vid"}]
    request = wire.requests[0]
    assert (request.method, request.url.path) == ("GET", "/youtube/v3/liveBroadcasts")
    # `broadcastStatus` is a filter of its own: YouTube refuses it with `mine`.
    assert request.url.params["broadcastStatus"] == "active" and "mine" not in request.url.params
    assert request.url.params["broadcastType"] == "all"


def test_ending_a_video_is_a_transition_to_complete_with_no_body(tmp_path: Path) -> None:
    wire = Wire()
    YouTube(_signed_in(tmp_path, wire)).end_broadcast("vid")
    request = wire.requests[0]
    assert (request.method, request.url.path) == ("POST", "/youtube/v3/liveBroadcasts/transition")
    assert dict(request.url.params) == {
        "broadcastStatus": "complete",
        "id": "vid",
        "part": "id,status",
    }
    assert request.content == b""


def _refusal(reason: str) -> Wire:
    return Wire(
        lambda _r: httpx.Response(
            403, json={"error": {"message": reason, "errors": [{"reason": reason}]}}
        )
    )


def test_a_video_youtube_already_ended_is_not_an_error(tmp_path: Path) -> None:
    """Auto stop can land between the listing and the request."""
    YouTube(_signed_in(tmp_path, _refusal("redundantTransition"))).end_broadcast("vid")


def test_any_other_refusal_to_end_a_video_is_raised(tmp_path: Path) -> None:
    with pytest.raises(YouTubeError) as caught:
        YouTube(_signed_in(tmp_path, _refusal("invalidTransition"))).end_broadcast("vid")
    assert caught.value.reason == "invalidTransition"


def test_a_video_that_is_gone_reads_as_none(tmp_path: Path) -> None:
    yt = YouTube(_signed_in(tmp_path, Wire(lambda _r: httpx.Response(200, json={"items": []}))))
    assert yt.broadcast("vid") is None and yt.video("vid") is None


def test_the_snippet_is_always_sent_whole_with_the_sports_category(tmp_path: Path) -> None:
    wire = Wire()
    YouTube(_signed_in(tmp_path, wire)).describe("vid", "Title", "Line one\n0:00 Game 1")
    request = wire.requests[0]
    assert (request.method, request.url.params["part"]) == ("PUT", "snippet")
    assert _body(request) == {
        "id": "vid",
        "snippet": {"title": "Title", "description": "Line one\n0:00 Game 1", "categoryId": "17"},
    }


def test_the_visibility_change_returns_what_youtube_set(tmp_path: Path) -> None:
    wire = Wire(lambda _r: httpx.Response(200, json={"status": {"privacyStatus": "private"}}))
    assert YouTube(_signed_in(tmp_path, wire)).set_privacy("vid", "public") == "private"
    assert _body(wire.requests[0])["status"] == {
        "privacyStatus": "public",
        "selfDeclaredMadeForKids": False,
    }


def test_the_thumbnail_is_uploaded_as_a_jpeg(tmp_path: Path) -> None:
    wire = Wire()
    YouTube(_signed_in(tmp_path, wire)).set_thumbnail("vid", b"\xff\xd8jpeg")
    request = wire.requests[0]
    assert str(request.url).startswith(f"{api.UPLOAD}/thumbnails/set?videoId=vid")
    assert request.headers["content-type"] == "image/jpeg"
    assert request.content == b"\xff\xd8jpeg"
