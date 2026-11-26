"""The stream desk (#81): going live, the phone's edits, and the sign-in,
against a fake YouTube. Nothing here reaches the network or OBS."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cueclock.server import create_app
from cueclock.server.hub import Hub
from cueclock.server.stream import READY_TTL_S, StreamDesk, obs_stream_key
from cueclock.youtube.api import Account, DeviceCode, SignedOutError, YouTubeError

T0 = datetime(2026, 11, 26, 1, 0, tzinfo=UTC)


class FakeYouTube:
    """Records every call; fails the ones a test names."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.fail: dict[str, Exception] = {}
        self.keys = [{"id": "s1", "title": "First", "key": "obs-key"}]
        self.waiting: list[dict[str, Any]] = []
        self.privacy: str | None = None  # what YouTube sets, when not what was asked
        self.hold: threading.Event | None = None  # create_broadcast waits on it

    def _did(self, name: str, *args: object) -> None:
        self.calls.append((name, *args))
        if name in self.fail:
            raise self.fail[name]

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]

    def streams(self) -> list[dict[str, str]]:
        self._did("streams")
        return self.keys

    def upcoming(self) -> list[dict[str, Any]]:
        self._did("upcoming")
        return self.waiting

    def create_broadcast(self, title: str, privacy: str, start: datetime) -> dict[str, Any]:
        self._did("create", title, privacy)
        if self.hold is not None:
            self.hold.wait(5)
        return {"id": "vid1", "status": {"privacyStatus": self.privacy or privacy}}

    def bind(self, video_id: str, stream_id: str) -> None:
        self._did("bind", video_id, stream_id)

    def describe(self, video_id: str, title: str, description: str) -> None:
        self._did("describe", video_id, title, description)

    def set_thumbnail(self, video_id: str, jpeg: bytes) -> None:
        self._did("thumbnail", video_id, jpeg[:2])

    def set_privacy(self, video_id: str, privacy: str) -> str:
        self._did("privacy", video_id, privacy)
        return self.privacy or privacy

    def delete_broadcast(self, video_id: str) -> None:
        self._did("delete", video_id)


class Rig:
    """A desk on an in-memory hub, with clocks a test moves."""

    def __init__(self, tmp_path: Path, *, signed_in: bool = True, client: bool = True) -> None:
        config: dict[str, str] = {"client_id": "cid", "client_secret": "shh"} if client else {}
        if signed_in:
            config["refresh_token"] = "rt"
        self.path = tmp_path / "youtube.json"
        self.path.write_text(json.dumps(config))
        self.hub = Hub()
        self.hub.score.configure(
            {"p1_name": "Sam Okafor", "p2_name": "Jordan Lee", "p1_fargo": 612, "race_to": 7}
        )
        self.mono = 100.0
        self.wall = T0
        self.yt = FakeYouTube()
        self.desk = StreamDesk(
            self.hub,
            Account(self.path),
            obs_key=lambda: "obs-key",
            today=lambda: date(2026, 11, 25),
            now=lambda: self.mono,
            wall=lambda: self.wall,
        )
        self.desk.youtube = self.yt  # type: ignore[assignment]

    def journal(self, kind: str) -> list[dict[str, object]]:
        return [r for r in self.hub.journal._records if r["kind"] == kind]

    async def go(self) -> None:
        """A LIVE hold's first half, to its end."""
        assert self.desk.prepare() is None
        await self.settle()

    async def settle(self) -> None:
        for _ in range(500):
            if self.desk.phase != "preparing" and not self.desk._tasks:
                return
            await asyncio.sleep(0.005)
        raise AssertionError("the desk never settled")


def _run(scenario: Callable[[], Awaitable[None]]) -> None:
    asyncio.run(scenario())  # type: ignore[arg-type]


MATCH_TITLE = "Sam Okafor (612) vs Jordan Lee | 8-Ball Race to 7"

# ---------- going live ----------


def test_a_hold_makes_the_video_in_order_and_then_reads_ready(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    async def scenario() -> None:
        await rig.go()
        assert rig.yt.names() == ["streams", "upcoming", "create", "bind", "describe", "thumbnail"]
        assert rig.yt.calls[2] == ("create", MATCH_TITLE, "unlisted")
        assert rig.yt.calls[3] == ("bind", "vid1", "s1")
        assert rig.yt.calls[4][1:3] == ("vid1", MATCH_TITLE)
        assert rig.yt.calls[4][3].startswith("Sam Okafor (612) vs Jordan Lee\n8-Ball, race to 7.")
        assert rig.yt.calls[5] == ("thumbnail", "vid1", b"\xff\xd8")  # a JPEG
        payload = rig.desk.payload()
        assert (payload["phase"], payload["error"], payload["note"]) == ("ready", None, None)
        # Not on air yet, so no link yet: OBS has not been started.
        assert (payload["on_air"], payload["url"]) == (False, None)
        assert rig.journal("stream_prepared")[0]["video_id"] == "vid1"

    _run(scenario)


def test_the_stream_key_is_looked_up_once_a_run(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    async def scenario() -> None:
        await rig.go()
        rig.desk.cancel()
        await rig.settle()
        await rig.go()
        assert rig.yt.names().count("streams") == 1

    _run(scenario)


@pytest.mark.parametrize("step", ["streams", "upcoming", "create"])
def test_a_refusal_before_the_video_exists_fails_and_leaves_nothing(
    tmp_path: Path, step: str
) -> None:
    rig = Rig(tmp_path)
    rig.yt.fail[step] = YouTubeError("Creating the stream: YouTube did not answer (ConnectError)")

    async def scenario() -> None:
        await rig.go()
        payload = rig.desk.payload()
        assert payload["phase"] == "failed"
        assert payload["error"] == "Creating the stream: YouTube did not answer (ConnectError)"
        assert "bind" not in rig.yt.names() and "delete" not in rig.yt.names()
        assert rig.journal("stream_failed")

    _run(scenario)


def test_a_video_that_cannot_be_given_the_stream_key_is_removed(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.yt.fail["bind"] = YouTubeError("Attaching the stream key: forbidden", status=403)

    async def scenario() -> None:
        await rig.go()
        assert rig.desk.phase == "failed"
        assert rig.yt.names()[-2:] == ["bind", "delete"]

    _run(scenario)


def test_a_stream_youtube_would_only_make_private_is_refused(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.yt.privacy = "private"

    async def scenario() -> None:
        await rig.go()
        assert rig.desk.phase == "failed"
        assert rig.desk.error == "YouTube would only make this stream Private"
        assert rig.yt.names()[-1] == "delete" and "bind" not in rig.yt.names()

    _run(scenario)


def test_public_that_youtube_makes_unlisted_goes_ahead_and_says_so(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.yt.privacy = "unlisted"

    async def scenario() -> None:
        await rig.desk.set_visibility("public")
        await rig.go()
        payload = rig.desk.payload()
        assert (payload["phase"], payload["visibility"]) == ("ready", "unlisted")
        assert payload["note"] == "YouTube made this stream Unlisted, not Public."

    _run(scenario)


def test_a_thumbnail_youtube_refuses_does_not_stop_the_stream(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.yt.fail["thumbnail"] = YouTubeError("Setting the thumbnail: forbidden", status=403)
    rig.yt.fail["describe"] = YouTubeError("Updating the video: backend error", status=503)

    async def scenario() -> None:
        await rig.go()
        payload = rig.desk.payload()
        assert payload["phase"] == "ready"
        assert payload["note"] == (
            "Description not set (Updating the video: backend error). "
            "Thumbnail not set (Setting the thumbnail: forbidden)."
        )

    _run(scenario)


def test_an_unstarted_video_left_on_obs_key_is_removed_first(tmp_path: Path) -> None:
    def waiting(vid: str, stream: str, auto: bool, life: str) -> dict[str, Any]:
        return {
            "id": vid,
            "status": {"lifeCycleStatus": life},
            "contentDetails": {"boundStreamId": stream, "enableAutoStart": auto},
        }

    rig = Rig(tmp_path)
    rig.yt.waiting = [
        waiting("leftover", "s1", True, "ready"),  # would go on air with ours
        waiting("other-key", "s2", True, "ready"),
        waiting("scheduled-by-hand", "s1", False, "ready"),  # needs its own Go Live
        waiting("already-live", "s1", True, "live"),
    ]

    async def scenario() -> None:
        await rig.go()
        assert [c for c in rig.yt.calls if c[0] == "delete"] == [("delete", "leftover")]
        assert rig.yt.names().index("delete") < rig.yt.names().index("create")

    _run(scenario)


def test_a_key_obs_holds_that_the_channel_does_not_is_named(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.yt.keys = [{"id": "s9", "title": "Other", "key": "another-key"}]

    async def scenario() -> None:
        await rig.go()
        assert rig.desk.error == "OBS's stream key is not one of this channel's keys"
        assert "create" not in rig.yt.names()

    _run(scenario)


def test_with_nobody_signed_in_the_hold_is_refused_in_words(tmp_path: Path) -> None:
    async def scenario() -> None:
        out = Rig(tmp_path / "a", signed_in=False)
        assert out.desk.prepare() == "Not signed in to YouTube"
        assert out.desk.payload()["account"] == "signed_out"
        bare = Rig(tmp_path / "b", signed_in=False, client=False)
        assert bare.desk.prepare() == "YouTube is not set up on this machine"
        assert bare.desk.payload()["account"] == "unconfigured"
        assert out.yt.calls == [] and bare.yt.calls == []

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    _run(scenario)


def test_a_sign_in_found_dead_at_the_hold_reads_signed_out(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    def revoked() -> list[dict[str, str]]:
        rig.path.write_text(json.dumps({"client_id": "cid", "client_secret": "shh"}))
        raise SignedOutError("The YouTube sign-in has ended")

    rig.yt.streams = revoked  # type: ignore[method-assign]

    async def scenario() -> None:
        await rig.go()
        payload = rig.desk.payload()
        assert (payload["phase"], payload["account"]) == ("failed", "signed_out")

    _run(scenario)


def test_obs_not_starting_takes_the_video_back(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    async def scenario() -> None:
        await rig.go()
        rig.desk.cancel()
        await rig.settle()
        assert rig.yt.calls[-1] == ("delete", "vid1")
        payload = rig.desk.payload()
        assert (payload["phase"], payload["error"]) == ("failed", "OBS did not start the stream")

    _run(scenario)


def test_a_video_that_lands_after_the_deck_gave_up_is_removed(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.yt.hold = threading.Event()

    async def scenario() -> None:
        assert rig.desk.prepare() is None
        await asyncio.sleep(0.05)  # YouTube is slow to answer
        rig.desk.cancel()  # the deck's wait ran out
        assert rig.desk.phase == "preparing"
        rig.yt.hold.set()  # type: ignore[union-attr]
        await rig.settle()
        assert rig.yt.calls[-1] == ("delete", "vid1")
        assert rig.desk.payload()["phase"] == "failed"

    _run(scenario)


def test_a_ready_video_nobody_starts_is_taken_back_on_its_own(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    async def scenario() -> None:
        await rig.go()
        rig.mono += READY_TTL_S - 1
        rig.desk.tick()
        assert rig.desk.phase == "ready"
        rig.mono += 2
        rig.desk.tick()
        await rig.settle()
        assert rig.desk.phase == "failed" and rig.yt.calls[-1] == ("delete", "vid1")

    _run(scenario)


def test_a_second_hold_while_one_is_preparing_changes_nothing(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    async def scenario() -> None:
        assert rig.desk.prepare() is None
        assert rig.desk.prepare() is None
        await rig.settle()
        assert rig.yt.names().count("create") == 1

    _run(scenario)


# ---------- on air ----------


def test_on_air_has_the_link_the_minutes_and_a_journal_line(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    async def scenario() -> None:
        await rig.go()
        rig.desk.went_on_air(duration_ms=1500)
        rig.wall += timedelta(minutes=12, seconds=30)
        payload = rig.desk.payload()
        assert (payload["on_air"], payload["phase"], payload["minutes"]) == (True, "live", 12)
        assert payload["url"] == "https://youtu.be/vid1"
        assert (payload["title"], payload["source"]) == (MATCH_TITLE, "live")
        assert rig.desk.prepare() == "Already on air"
        line = rig.journal("stream_on_air")[0]
        assert line["video_id"] == "vid1"
        assert (
            line["start_wall"] == "2026-11-26T00:59:58.500+00:00"
        )  # back-dated by OBS's own count
        rig.desk.went_on_air()  # told twice: one stream
        assert len(rig.journal("stream_on_air")) == 1

    _run(scenario)


def test_the_title_on_air_stays_what_it_was_at_go_live(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    async def scenario() -> None:
        await rig.go()
        rig.desk.went_on_air()
        rig.hub.score.configure({"p1_name": "Ana Ruiz", "p2_name": "Bo Chen"})  # the next match
        payload = rig.desk.payload()
        assert payload["title"] == MATCH_TITLE
        assert payload["auto_title"] == "Ana Ruiz vs Bo Chen | 8-Ball Race to 7"

    _run(scenario)


def test_the_match_is_marked_when_it_changes_and_only_then(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    async def scenario() -> None:
        await rig.go()
        rig.desk.tick()
        assert rig.journal("stream_mark") == []  # not on air: nothing to mark
        rig.desk.went_on_air()
        rig.desk.tick()
        rig.desk.tick()
        rig.hub.score.adjust("p1", 1)
        rig.desk.tick()
        rig.desk.tick()
        for _ in range(6):
            rig.hub.score.adjust("p1", 1)  # to the race: 7-0
        rig.desk.tick()
        marks = rig.journal("stream_mark")
        assert [(m["s1"], m["s2"], m["over"]) for m in marks] == [
            (0, 0, False),
            (1, 0, False),
            (7, 0, True),
        ]
        assert marks[0]["video_id"] == "vid1"
        assert (marks[0]["p1"], marks[0]["tournament"], marks[0]["match"]) == (
            "Sam Okafor",
            None,
            None,
        )

    _run(scenario)


def test_a_stream_ending_puts_everything_back_where_a_stream_starts(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    async def scenario() -> None:
        await rig.desk.set_visibility("public")
        await rig.desk.set_title("Friday night money game")
        await rig.go()
        rig.desk.went_on_air()
        assert rig.desk.payload()["visibility"] == "public"
        rig.desk.went_off_air()
        payload = rig.desk.payload()
        assert (payload["on_air"], payload["phase"], payload["url"]) == (False, "idle", None)
        # Last week's Public and the typed title never carry over.
        assert (payload["visibility"], payload["source"]) == ("unlisted", "match")
        assert payload["title"] == MATCH_TITLE
        assert rig.journal("stream_off_air")[0]["video_id"] == "vid1"
        rig.desk.went_off_air()  # told twice: one line
        assert len(rig.journal("stream_off_air")) == 1

    _run(scenario)


def test_a_stream_started_in_obs_itself_has_no_link_and_no_marks(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    async def scenario() -> None:
        rig.desk.went_on_air()
        rig.desk.tick()
        payload = rig.desk.payload()
        assert (payload["on_air"], payload["url"]) == (True, None)
        assert rig.journal("stream_on_air")[0]["video_id"] is None
        assert rig.journal("stream_mark") == []
        await rig.desk.set_title("ignored")  # nothing to edit: kept for the next stream
        assert rig.yt.calls == []

    _run(scenario)


# ---------- the phone's edits ----------


def test_a_typed_title_is_kept_until_it_is_given_up(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    async def scenario() -> None:
        await rig.desk.set_title("  Friday night <money> game  ")
        payload = rig.desk.payload()
        assert (payload["title"], payload["source"]) == ("Friday night money game", "typed")
        assert payload["auto_title"] == MATCH_TITLE
        await rig.go()
        assert rig.yt.calls[2] == ("create", "Friday night money game", "unlisted")
        rig.desk.cancel()
        await rig.desk.set_title(None)  # Use the match title
        assert rig.desk.payload()["source"] == "match"
        await rig.desk.set_title(MATCH_TITLE)  # typing the match's own title is not typing one
        assert rig.desk.payload()["source"] == "match"

    _run(scenario)


def test_an_edit_on_air_goes_to_the_live_video(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    async def scenario() -> None:
        await rig.go()
        rig.desk.went_on_air()
        description = rig.yt.calls[4][3]
        await rig.desk.set_title("Hill-hill decider")
        assert rig.yt.calls[-1] == ("describe", "vid1", "Hill-hill decider", description)
        assert rig.desk.payload()["title"] == "Hill-hill decider"
        await rig.desk.set_title("Hill-hill decider")  # unchanged: no request
        assert rig.yt.names().count("describe") == 2
        await rig.desk.set_title(None)  # back to the match's own
        assert rig.yt.calls[-1] == ("describe", "vid1", MATCH_TITLE, description)
        rig.yt.fail["describe"] = YouTubeError("Updating the video: forbidden", status=403)
        with pytest.raises(YouTubeError):
            await rig.desk.set_title("Refused")
        assert rig.desk.payload()["title"] == MATCH_TITLE  # still what YouTube has

    _run(scenario)


def test_visibility_is_a_setting_off_air_and_a_request_on_air(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    async def scenario() -> None:
        await rig.desk.set_visibility("public")
        assert rig.desk.payload()["visibility"] == "public" and rig.yt.calls == []
        await rig.desk.set_visibility("unlisted")
        with pytest.raises(ValueError, match="private"):
            await rig.desk.set_visibility("private")  # never offered
        await rig.go()
        rig.desk.went_on_air()
        await rig.desk.set_visibility("public")
        assert rig.yt.calls[-1] == ("privacy", "vid1", "public")
        assert rig.desk.payload()["visibility"] == "public"
        rig.yt.privacy = "unlisted"  # YouTube will not make it public
        await rig.desk.set_visibility("public")
        payload = rig.desk.payload()
        assert payload["visibility"] == "unlisted"
        assert payload["note"] == "YouTube kept this stream Unlisted. Change it in YouTube Studio."

    _run(scenario)


# ---------- the sign-in ----------


def test_the_phone_is_given_a_code_and_the_desk_notices_the_approval(tmp_path: Path) -> None:
    rig = Rig(tmp_path, signed_in=False)
    polls: list[str] = []

    def poll(code: DeviceCode) -> bool:
        polls.append(code.device_code)
        if len(polls) < 3:
            return False
        rig.path.write_text(
            json.dumps({"client_id": "cid", "client_secret": "shh", "refresh_token": "rt"})
        )
        return True

    rig.desk.account.request_code = lambda: DeviceCode(  # type: ignore[method-assign]
        "ABCD-EFGH", "https://www.google.com/device", "dc", 0.01, 1800.0
    )
    rig.desk.account.poll_code = poll  # type: ignore[method-assign]

    async def scenario() -> None:
        rig.desk.sign_in()
        rig.desk.sign_in()  # asked again by a second phone: still one code
        await asyncio.sleep(0.02)
        assert rig.desk.payload()["signin"] == {
            "code": "ABCD-EFGH",
            "url": "https://www.google.com/device",
        }
        await rig.settle()
        payload = rig.desk.payload()
        assert (payload["account"], payload["signin"]) == ("ok", None)
        assert polls == ["dc", "dc", "dc"]
        assert rig.journal("stream_signed_in")

    _run(scenario)


def test_a_code_that_runs_out_is_withdrawn(tmp_path: Path) -> None:
    rig = Rig(tmp_path, signed_in=False)
    rig.desk.account.request_code = lambda: DeviceCode(  # type: ignore[method-assign]
        "ABCD-EFGH", "https://www.google.com/device", "dc", 0.01, 30.0
    )

    def poll(_code: DeviceCode) -> bool:
        rig.mono += 20.0
        return False

    rig.desk.account.poll_code = poll  # type: ignore[method-assign]

    async def scenario() -> None:
        rig.desk.sign_in()
        await rig.settle()
        payload = rig.desk.payload()
        assert (payload["account"], payload["signin"]) == ("signed_out", None)

    _run(scenario)


def test_signed_in_nobody_is_given_a_code(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    async def scenario() -> None:
        rig.desk.sign_in()
        assert not rig.desk._tasks and rig.desk.payload()["signin"] is None

    _run(scenario)


# ---------- OBS's key, and the routes ----------


def test_the_stream_key_is_read_from_obs_current_profile(tmp_path: Path) -> None:
    for name, key in (("Untitled", "key-current"), ("Old", "key-old")):
        profile = tmp_path / "basic" / "profiles" / name
        profile.mkdir(parents=True)
        (profile / "service.json").write_text(json.dumps({"settings": {"key": key}}))
    (tmp_path / "user.ini").write_text("[Basic]\nProfile=Untitled\nProfileDir=Untitled\n")
    assert obs_stream_key(tmp_path) == "key-current"
    (tmp_path / "user.ini").write_text("[Basic]\nProfileDir=Old\n")
    assert obs_stream_key(tmp_path) == "key-old"
    assert obs_stream_key(tmp_path / "nowhere") is None


def test_a_server_with_no_account_file_has_no_stream(tmp_path: Path) -> None:
    with TestClient(create_app()) as client:
        assert client.get("/api/state").json()["stream"] is None
        for path in ("prepare", "cancel", "on_air", "off_air", "signin", "visibility/public"):
            assert client.post(f"/api/stream/{path}").status_code == 404
        assert client.post("/api/stream/title", json={"title": "x"}).status_code == 404


def test_the_routes_drive_the_desk(tmp_path: Path) -> None:
    path = tmp_path / "youtube.json"
    path.write_text(json.dumps({"client_id": "cid", "client_secret": "shh"}))  # nobody signed in
    with TestClient(create_app(youtube_path=path)) as client:
        stream = client.get("/api/state").json()["stream"]
        assert set(stream) == {
            "account",
            "signin",
            "phase",
            "on_air",
            "minutes",
            "error",
            "note",
            "title",
            "source",
            "auto_title",
            "visibility",
            "url",
        }
        assert (stream["account"], stream["phase"], stream["visibility"]) == (
            "signed_out",
            "idle",
            "unlisted",
        )
        assert stream["source"] == "practice" and stream["title"].startswith("Practice | ")
        refused = client.post("/api/stream/prepare")
        assert refused.status_code == 409
        assert refused.json() == {"ok": False, "error": "Not signed in to YouTube"}
        assert client.post("/api/stream/visibility/public").json() == {"ok": True}
        assert client.post("/api/stream/visibility/friends").status_code == 404
        assert client.post("/api/stream/title", json={"title": "Drills"}).json() == {"ok": True}
        stream = client.get("/api/state").json()["stream"]
        assert (stream["visibility"], stream["title"], stream["source"]) == (
            "public",
            "Drills",
            "typed",
        )
        assert client.post("/api/stream/on_air", params={"duration_ms": 90_000}).json()["ok"]
        stream = client.get("/api/state").json()["stream"]
        assert (stream["on_air"], stream["minutes"], stream["url"]) == (True, 1, None)
        assert client.post("/api/stream/off_air").json()["ok"]
        stream = client.get("/api/state").json()["stream"]
        assert (stream["on_air"], stream["visibility"], stream["source"]) == (
            False,
            "unlisted",
            "practice",
        )
        # The stream rides the WebSocket too: the control page reads it there.
        with client.websocket_connect("/ws") as ws:
            assert json.loads(ws.receive_text())["stream"]["account"] == "signed_out"
