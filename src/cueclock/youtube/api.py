"""YouTube over plain HTTPS: the sign-in, and the few calls a stream needs (#81).

No Google client library. A stream is seven kinds of request against two
hosts, and `httpx` is already here for the Fargo search.

**The account** is one gitignored file, `config/youtube.json`:

    {"client_id": "...", "client_secret": "...",      from the Google Cloud console
     "refresh_token": "...", "scope": "...", "signed_in_at": "..."}   written by the sign-in

The client is a *TVs and Limited Input devices* OAuth client, so the sign-in is
Google's device flow: this side shows a short code, and it is typed at
google.com/device on any phone (`docs/hardware.md` §B3). The refresh token is
the sign-in; access tokens last an hour, are fetched when needed and are never
written down. Three processes read the file (`run`, for the control page and
the go-live; the archive pass, for chapters; a terminal), and each refreshes
its own.

Every call raises `YouTubeError` when YouTube refuses or does not answer, with
a message fit to show on the phone. Nothing here retries: the callers decide
what a failure means (the LIVE key refuses to go live; the archive pass tries
again in fifteen minutes).

The scope is plain `https://www.googleapis.com/auth/youtube`: the device flow
refuses `youtube.force-ssl`, and every method used here accepts the plain one
(read off each method's reference page, 2026-11-25).
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "youtube.json"
SCOPE = "https://www.googleapis.com/auth/youtube"
DEVICE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # nosec B105: an endpoint, not a secret
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
API = "https://www.googleapis.com/youtube/v3"
UPLOAD = "https://www.googleapis.com/upload/youtube/v3"
# YouTube's video category "Sports" (`videoCategories.list`, checked 2026-11-25).
SPORTS = "17"
# A call is a few hundred milliseconds on the rig's WiFi. These only bound how
# long an unreachable YouTube can hold up a go-live before it is refused.
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
UPLOAD_TIMEOUT = httpx.Timeout(20.0, connect=5.0)
# An access token is refreshed this long before Google says it ends.
TOKEN_MARGIN_S = 60.0


class YouTubeError(Exception):
    """YouTube refused a call or did not answer; `str()` is fit for the phone.

    ``status`` is the HTTP status (None when there was no answer at all) and
    ``reason`` YouTube's own short code (`forbidden`, `videoNotFound`).
    """

    def __init__(self, message: str, *, status: int | None = None, reason: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason

    @property
    def unreachable(self) -> bool:
        """No answer, or YouTube's own fault: worth trying again later."""
        return self.status is None or self.status >= 500 or self.status == 429


class SignedOutError(YouTubeError):
    """Nobody is signed in: no token on file, or Google no longer honours it."""


@dataclass(frozen=True)
class DeviceCode:
    """One sign-in attempt: the code to type, where, and how to ask about it."""

    user_code: str
    url: str
    device_code: str
    interval_s: float
    expires_s: float


def _error(resp: httpx.Response, what: str) -> YouTubeError:
    """Google's error body as an exception; both of its shapes are read."""
    message, reason = "", ""
    with contextlib.suppress(ValueError):
        body = resp.json()
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):  # the Data API's shape
            message = str(error.get("message") or "")
            errors = error.get("errors")
            if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                reason = str(errors[0].get("reason") or "")
        elif isinstance(error, str):  # the OAuth endpoints' shape
            reason = error
            message = str(body.get("error_description") or error)
    detail = message or reason or f"HTTP {resp.status_code}"
    return YouTubeError(f"{what}: {detail}", status=resp.status_code, reason=reason)


@dataclass
class Account:
    """The sign-in on file, and authorised requests made with it."""

    path: Path = CONFIG_PATH
    transport: httpx.BaseTransport | None = None  # tests pass a fake wire
    now: Callable[[], float] = time.monotonic
    _access: str | None = field(default=None, repr=False)
    _access_until: float = field(default=0.0, repr=False)
    _client: httpx.Client | None = field(default=None, repr=False)

    # ---------- the file ----------

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, Any]) -> None:
        """Atomic, and readable by this user only: it holds the sign-in."""
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(data, indent=2) + "\n")
        tmp.replace(self.path)

    @property
    def state(self) -> str:
        """`ok`, `signed_out`, or `unconfigured` (no OAuth client on file).

        Read off the file alone, so it costs nothing to ask on every
        broadcast. A token Google has revoked reads `ok` until a call finds
        out, and that call takes it off the file (`token`).
        """
        cfg = self._read()
        if not (cfg.get("client_id") and cfg.get("client_secret")):
            return "unconfigured"
        return "ok" if cfg.get("refresh_token") else "signed_out"

    # ---------- the wire ----------

    def _send(self, what: str, request: Callable[[httpx.Client], httpx.Response]) -> httpx.Response:
        # One client for the account's life, so a go-live's seven requests
        # share a connection instead of each paying for its own handshake.
        if self._client is None:
            self._client = httpx.Client(transport=self.transport, timeout=TIMEOUT)
        try:
            return request(self._client)
        except httpx.HTTPError as exc:
            raise YouTubeError(f"{what}: YouTube did not answer ({type(exc).__name__})") from exc

    def token(self) -> str:
        """A live access token, refreshed when the last one is about to end."""
        if self._access is not None and self.now() < self._access_until:
            return self._access
        cfg = self._read()
        refresh = cfg.get("refresh_token")
        if not (cfg.get("client_id") and cfg.get("client_secret")):
            raise SignedOutError("YouTube is not set up on this machine (config/youtube.json)")
        if not refresh:
            raise SignedOutError("Not signed in to YouTube")
        resp = self._send(
            "Signing in",
            lambda client: client.post(
                TOKEN_URL,
                data={
                    "client_id": cfg["client_id"],
                    "client_secret": cfg["client_secret"],
                    "grant_type": "refresh_token",
                    "refresh_token": refresh,
                },
            ),
        )
        if resp.status_code != 200:
            error = _error(resp, "Signing in")
            if error.reason == "invalid_grant":
                # Google no longer honours this sign-in (revoked, or expired).
                # It comes off the file so every process reads `signed_out`
                # and the phone is offered the code, not a dead token.
                cfg.pop("refresh_token", None)
                cfg.pop("signed_in_at", None)
                self._write(cfg)
                raise SignedOutError("The YouTube sign-in has ended", status=resp.status_code)
            raise error
        body = resp.json()
        self._access = str(body["access_token"])
        self._access_until = self.now() + float(body.get("expires_in", 3600)) - TOKEN_MARGIN_S
        return self._access

    def call(
        self,
        method: str,
        url: str,
        what: str,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        content: bytes | None = None,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        """One authorised request; its JSON answer, or `{}` for an empty one."""
        for attempt in (0, 1):
            headers = {"Authorization": f"Bearer {self.token()}"}
            if content_type is not None:
                headers["Content-Type"] = content_type
            resp = self._send(
                what,
                lambda client, headers=headers: client.request(  # type: ignore[misc]
                    method,
                    url,
                    params=params,
                    json=body,
                    content=content,
                    headers=headers,
                    timeout=UPLOAD_TIMEOUT if content is not None else TIMEOUT,
                ),
            )
            if resp.status_code == 401 and attempt == 0:
                self._access = None  # ended early: one fresh token, one more try
                continue
            break
        if resp.status_code >= 300:
            raise _error(resp, what)
        if not resp.content:
            return {}
        data = resp.json()
        return data if isinstance(data, dict) else {}

    # ---------- the sign-in, from a phone ----------

    def request_code(self) -> DeviceCode:
        """Start a sign-in: the code to type at google.com/device."""
        cfg = self._read()
        if not (cfg.get("client_id") and cfg.get("client_secret")):
            raise SignedOutError("YouTube is not set up on this machine (config/youtube.json)")
        resp = self._send(
            "Asking for a sign-in code",
            lambda client: client.post(
                DEVICE_URL, data={"client_id": cfg["client_id"], "scope": SCOPE}
            ),
        )
        if resp.status_code != 200:
            raise _error(resp, "Asking for a sign-in code")
        body = resp.json()
        return DeviceCode(
            user_code=str(body["user_code"]),
            url=str(body.get("verification_url") or "https://www.google.com/device"),
            device_code=str(body["device_code"]),
            interval_s=float(body.get("interval", 5)),
            expires_s=float(body.get("expires_in", 1800)),
        )

    def poll_code(self, code: DeviceCode) -> bool:
        """Has the code been approved? True stores the sign-in; False = not yet.

        Raises `YouTubeError` once it can no longer be: refused at Google, or
        the code ran out.
        """
        cfg = self._read()
        resp = self._send(
            "Signing in",
            lambda client: client.post(
                TOKEN_URL,
                data={
                    "client_id": cfg.get("client_id", ""),
                    "client_secret": cfg.get("client_secret", ""),
                    "device_code": code.device_code,
                    "grant_type": DEVICE_GRANT,
                },
            ),
        )
        if resp.status_code == 200:
            body = resp.json()
            cfg["refresh_token"] = body["refresh_token"]
            cfg["scope"] = body.get("scope", SCOPE)
            cfg["signed_in_at"] = datetime.now(UTC).isoformat(timespec="seconds")
            self._write(cfg)
            self._access = str(body["access_token"])
            self._access_until = self.now() + float(body.get("expires_in", 3600)) - TOKEN_MARGIN_S
            return True
        error = _error(resp, "Signing in")
        if error.reason in ("authorization_pending", "slow_down"):
            return False
        raise error


@dataclass
class YouTube:
    """The calls a stream makes, each one request (quota: 1 unit unless noted)."""

    account: Account = field(default_factory=Account)

    def streams(self) -> list[dict[str, str]]:
        """This channel's reusable streams: `{id, title, key}`.

        The key is what OBS holds (`cdn.ingestionInfo.streamName`), so it is
        how the stream OBS will send to is found.
        """
        data = self.account.call(
            "GET",
            f"{API}/liveStreams",
            "Listing the stream keys",
            params={"part": "id,snippet,cdn", "mine": "true", "maxResults": "50"},
        )
        out = []
        for item in data.get("items", []):
            key = item.get("cdn", {}).get("ingestionInfo", {}).get("streamName")
            if isinstance(key, str) and key:
                out.append(
                    {
                        "id": str(item["id"]),
                        "title": str(item.get("snippet", {}).get("title", "")),
                        "key": key,
                    }
                )
        return out

    def create_broadcast(self, title: str, privacy: str, start: datetime) -> dict[str, Any]:
        """A new video that goes live when OBS connects and ends when it stops.

        Auto start and auto stop are what let OBS stay the only switch: nothing
        here has to tell YouTube the stream began or ended. The monitor stream
        is off because nothing watches it.
        """
        return self.account.call(
            "POST",
            f"{API}/liveBroadcasts",
            "Creating the stream",
            params={"part": "snippet,status,contentDetails"},
            body={
                "snippet": {
                    "title": title,
                    "scheduledStartTime": start.astimezone(UTC).isoformat(timespec="seconds"),
                },
                "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
                "contentDetails": {
                    "enableAutoStart": True,
                    "enableAutoStop": True,
                    "monitorStream": {"enableMonitorStream": False},
                },
            },
        )

    def bind(self, broadcast_id: str, stream_id: str) -> None:
        """Attach the video to the stream whose key OBS has."""
        self.account.call(
            "POST",
            f"{API}/liveBroadcasts/bind",
            "Attaching the stream key",
            params={"id": broadcast_id, "part": "id,contentDetails", "streamId": stream_id},
        )

    def upcoming(self) -> list[dict[str, Any]]:
        """Broadcasts created and not yet started, whoever created them."""
        data = self.account.call(
            "GET",
            f"{API}/liveBroadcasts",
            "Listing upcoming streams",
            params={
                "part": "id,snippet,status,contentDetails",
                "broadcastStatus": "upcoming",
                "broadcastType": "all",
                "maxResults": "50",
            },
        )
        items = data.get("items", [])
        return items if isinstance(items, list) else []

    def broadcast(self, broadcast_id: str) -> dict[str, Any] | None:
        """One broadcast, or None if YouTube has none by that id."""
        data = self.account.call(
            "GET",
            f"{API}/liveBroadcasts",
            "Reading the stream",
            params={"part": "id,snippet,status,contentDetails", "id": broadcast_id},
        )
        items = data.get("items", [])
        return items[0] if items else None

    def delete_broadcast(self, broadcast_id: str) -> None:
        self.account.call(
            "DELETE", f"{API}/liveBroadcasts", "Removing the stream", params={"id": broadcast_id}
        )

    def video(self, video_id: str) -> dict[str, Any] | None:
        """One video (a broadcast is also a video, under the same id)."""
        data = self.account.call(
            "GET",
            f"{API}/videos",
            "Reading the video",
            params={"part": "snippet,status,liveStreamingDetails", "id": video_id},
        )
        items = data.get("items", [])
        return items[0] if items else None

    def describe(self, video_id: str, title: str, description: str) -> None:
        """Set title, description and the Sports category (50 units).

        All three at once, always: `videos.update` deletes whatever part of
        the snippet a request leaves out.
        """
        self.account.call(
            "PUT",
            f"{API}/videos",
            "Updating the video",
            params={"part": "snippet"},
            body={
                "id": video_id,
                "snippet": {"title": title, "description": description, "categoryId": SPORTS},
            },
        )

    def set_privacy(self, video_id: str, privacy: str) -> str:
        """Change who can watch; returns what YouTube says it is now (50 units)."""
        data = self.account.call(
            "PUT",
            f"{API}/videos",
            "Changing the visibility",
            params={"part": "status"},
            body={
                "id": video_id,
                "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
            },
        )
        return str(data.get("status", {}).get("privacyStatus", privacy))

    def set_thumbnail(self, video_id: str, jpeg: bytes) -> None:
        """Upload the thumbnail (about 50 units; 2 MB at most)."""
        self.account.call(
            "POST",
            f"{UPLOAD}/thumbnails/set",
            "Setting the thumbnail",
            params={"videoId": video_id},
            content=jpeg,
            content_type="image/jpeg",
        )
