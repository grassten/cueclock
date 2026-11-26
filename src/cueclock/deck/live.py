"""The LIVE key's stream (#80, #81): go live on YouTube, stop, and say which.

The stream itself belongs to OBS. Its service is YouTube - RTMPS with a stream
key, saved in OBS's profile (`service.json`) and never in this repo
(`docs/hardware.md` §B3). This side asks OBS to start or stop the stream over
obs-websocket, and turns OBS's own answer (`GetStreamStatus`) into the key's
face.

**The video belongs to the server** (#81, `server/stream.py`). Before OBS is
told to start, the server creates the YouTube video with its title, visibility
and thumbnail, so a hold goes in two steps:

1. `POST /api/stream/prepare`, then wait on the poll for the server's
   `stream.phase` to read `ready`;
2. `StartStream`, then wait for OBS to say it is on air.

If the server reads `failed`, or never answers, **OBS is not started**: OBS
alone would make YouTube create a video with the last stream's settings,
which might be Public. The key then says `YOUTUBE FAILED`, and the next hold
tries again. In return this side tells the server what only OBS knows, that
the stream went on air or came off it.

The face comes from OBS's answer, not from what the deck last pressed, so it
is right after a daemon restart and when a stream ends on its own. No answer
means the key cannot do anything, so it goes dark (layout rule 1). That covers
OBS still starting and OBS gone. Off air it says what a hold will do, `GO
LIVE` over `UNLISTED` or `PUBLIC`, and with nobody signed in to YouTube it
says `SIGN IN` / `ON PHONE` and a hold does nothing.

Everything here runs on the daemon's poll through an injected `request` and
`post`, so the whole machine is unit-tested without OBS or a server.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from cueclock.deck import obsws
from cueclock.deck.faces import BG_ERR, BG_LIVE, BG_WARN, Face

HOLD_SUB = "HOLD 1s"
# How long the server may take to make the video before the hold is called
# failed. Measured against the real YouTube on the rig's WiFi, 2026-11-25:
# 5.2 to 6.2 s for the whole of it, over four dry runs. Creating the video
# (1.8 s) and uploading the thumbnail (2.4 s) are most of that, and they are
# YouTube's own time: sharing one connection changed nothing. Each request
# may wait 10 to 20 s for an answer before it gives up, and an unreachable
# YouTube is refused by the server within 5.
PREPARE_WAIT_S = 45.0
# How long a start may take to put the stream on air before it is called
# failed. Chosen, not measured against a failure: the two starts on 2026-11-25
# connected in 0.6 s and about 1 s, and 20 s leaves a slow WiFi handshake
# plenty of room without leaving STARTING… on the key for long.
START_WAIT_S = 20.0
# A stop has no failure to report: whatever OBS says after this is the truth.
STOP_WAIT_S = 10.0
# The poll asks OBS every 2 s, on the loop that also reads presses, so a slow
# answer must not hold the deck. `obsws.request` waits at most twice this.
STATUS_TIMEOUT_S = 1.0

Request = Callable[..., dict[str, Any] | None]
Post = Callable[[str], bool]
Intent = Literal["start", "stop"]
Step = Literal["prepare", "start", "stop"]
Failure = Literal["youtube", "stream"]


def _obs_request(request_type: str, *, timeout: float = 3.0) -> dict[str, Any] | None:
    return obsws.request(request_type, timeout=timeout)


def _no_server(_path: str) -> bool:
    """The default `post`: no server to ask, so nothing can go live."""
    return False


def off_air_face(visibility: str = "unlisted") -> Face:
    """What a hold will do: go live, and to whom."""
    return Face(text=f"GO LIVE\n{visibility.upper()}", sub=HOLD_SUB)


def sign_in_face() -> Face:
    """Nobody is signed in to YouTube. The phone's Stream section has the code."""
    return Face(text="SIGN IN", sub="ON PHONE")


def no_youtube_face() -> Face:
    """No YouTube account file on this machine; the phone says what is missing."""
    return Face(text="NO\nYOUTUBE", sub="SEE PHONE")


def on_air_face(duration_ms: int) -> Face:
    """Red, with the minutes on air: the room is on the internet."""
    return Face(text=f"LIVE\n{duration_ms // 60_000} MIN", sub=HOLD_SUB, bg=BG_LIVE)


def reconnecting_face() -> Face:
    return Face(text="LIVE\nRETRYING", sub=HOLD_SUB, bg=BG_WARN)


def failed_face() -> Face:
    """A start that never went on air. It says STREAM, not LIVE, so a red key
    read from across the room is never mistaken for being on air."""
    return Face(text="STREAM\nFAILED", sub=HOLD_SUB, bg=BG_ERR)


def youtube_failed_face() -> Face:
    """YouTube refused or did not answer, so OBS was never started. The
    phone's Stream section has the reason."""
    return Face(text="YOUTUBE\nFAILED", sub=HOLD_SUB, bg=BG_ERR)


@dataclass
class Stream:
    """OBS's stream as the LIVE key sees it.

    `poll` asks OBS and gives the face, or None while the key must be dark.
    `intent` is what a hold on that face would do, read when the finger comes
    down. `act` does it, but only if OBS still agrees when the hold matures:
    a hold begun to stop a stream that has since died must not start a new one.
    """

    request: Request = _obs_request
    post: Post = _no_server
    now: Callable[[], float] = time.monotonic
    failed: Failure | None = None
    _status: dict[str, Any] | None = field(default=None, repr=False)
    # The server's `stream` object from the same poll (`GET /api/state`).
    _server: dict[str, Any] = field(default_factory=dict, repr=False)
    # A step that has been asked for and not yet carried out.
    _pending: tuple[Step, float] | None = field(default=None, repr=False)

    def poll(self, session_on: bool, server: object = None) -> Face | None:
        """Ask OBS for the stream's state; the key's face, or None for dark.

        Only a deck-owned session has a stream to show: OBS is launched with
        it and quit with it. Off a session the machine resets, so a failure
        never outlives the night it happened on. ``server`` is the `stream`
        object of the state payload this poll fetched.
        """
        if not session_on:
            self.failed, self._status, self._pending, self._server = None, None, None, {}
            return None
        self._server = server if isinstance(server, dict) else {}
        self._status = self.request("GetStreamStatus", timeout=STATUS_TIMEOUT_S)
        if self._status is None:
            return None
        self._report()
        self._settle()
        return self.face()

    def _report(self) -> None:
        """Tell the server when it and OBS disagree about being on air.

        Compared on every poll, not sent on a change, so a daemon restarted
        mid-stream or a report the server missed corrects itself.
        """
        if not self._server or bool(self._server.get("on_air")) == self._active:
            return
        if self._active:
            duration = self._status.get("outputDuration") if self._status else 0
            ms = duration if isinstance(duration, int) else 0
            self.post(f"/api/stream/on_air?duration_ms={ms}")
        else:
            self.post("/api/stream/off_air")

    def _settle(self) -> None:
        """Resolve a pending step against OBS's and the server's latest answers."""
        if self._pending is None:
            return
        step, since = self._pending
        waited = self.now() - since
        if step == "prepare":
            phase = self._server.get("phase")
            if phase == "ready":
                self._start_obs()
            elif phase == "failed":
                print(f"live: YouTube refused — {self._server.get('error')}", flush=True)
                self._pending, self.failed = None, "youtube"
            elif waited >= PREPARE_WAIT_S:
                print(f"live: no video from YouTube after {PREPARE_WAIT_S:.0f} s", flush=True)
                self.post("/api/stream/cancel")
                self._pending, self.failed = None, "youtube"
        elif step == "start" and self._active:
            print("live: on air", flush=True)
            self._pending = None
        elif step == "stop" and not self._active:
            print("live: off air", flush=True)
            self._pending = None
        elif step == "start" and waited >= START_WAIT_S:
            print(f"live: start failed — not on air after {START_WAIT_S:.0f} s", flush=True)
            # OBS may still be dialling. Stopped first, so it cannot connect
            # after its video has been taken back and have YouTube make one.
            self.request("StopStream")
            self.post("/api/stream/cancel")
            self._pending, self.failed = None, "stream"
        elif step == "stop" and waited >= STOP_WAIT_S:
            self._pending = None

    def _start_obs(self) -> None:
        """The video exists: now OBS may send to it."""
        if self.request("StartStream") is None:
            # obsws has already logged OBS's own reason.
            print("live: StartStream refused", flush=True)
            self.post("/api/stream/cancel")
            self._pending, self.failed = None, "stream"
            return
        print("live: StartStream sent", flush=True)
        self._pending = ("start", self.now())

    @property
    def _active(self) -> bool:
        return bool(self._status and self._status.get("outputActive"))

    @property
    def _account(self) -> str:
        """The server's YouTube sign-in: `ok`, `signed_out`, or `unconfigured`
        (which is also what a server with no stream desk reads as)."""
        account = self._server.get("account")
        return account if isinstance(account, str) else "unconfigured"

    @property
    def busy(self) -> bool:
        """A hold is being carried out: the daemon polls faster until it lands."""
        return self._pending is not None

    def face(self) -> Face | None:
        """What the key says, from the last answers; None if OBS gave none."""
        if self._status is None:
            return None
        if self._pending is not None:
            return Face(text="STOPPING…" if self._pending[0] == "stop" else "STARTING…")
        if self._active:
            if self._status.get("outputReconnecting"):
                return reconnecting_face()
            duration = self._status.get("outputDuration")
            return on_air_face(duration if isinstance(duration, int) else 0)
        # The sign-in comes before a failure: a hold cannot retry without it.
        if self._account == "signed_out":
            return sign_in_face()
        if self._account != "ok":
            return no_youtube_face()
        if self.failed == "youtube":
            return youtube_failed_face()
        if self.failed == "stream":
            return failed_face()
        visibility = self._server.get("visibility")
        return off_air_face(visibility if isinstance(visibility, str) else "unlisted")

    def intent(self) -> Intent | None:
        """What a hold on the current face would do; None when it would do
        nothing: mid-start or stop, or off air with nobody signed in."""
        if self._status is None or self._pending is not None:
            return None
        if self._active:
            return "stop"
        return "start" if self._account == "ok" else None

    def act(self, intent: Intent) -> None:
        """Carry out a matured hold, if OBS still agrees it makes sense."""
        status = self.request("GetStreamStatus", timeout=STATUS_TIMEOUT_S)
        if status is None:
            return  # OBS is gone; the next poll darkens the key
        self._status = status
        if (intent == "stop") != self._active:
            print(f"live: hold to {intent} ignored — the stream had changed", flush=True)
            return
        self.failed = None
        if intent == "start":
            # The video first (#81). OBS is started from `_settle`, once the
            # server says the video is there.
            if not self.post("/api/stream/prepare"):
                print("live: the server refused to prepare the stream", flush=True)
                self.failed = "youtube"
                return
            print("live: preparing the YouTube video", flush=True)
            self._pending = ("prepare", self.now())
            return
        if self.request("StopStream") is None:
            print("live: StopStream refused", flush=True)
            return
        print("live: StopStream sent", flush=True)
        self._pending = ("stop", self.now())
