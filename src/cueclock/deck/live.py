"""The LIVE key's stream (#80): go live on YouTube, stop, and say which.

The stream itself belongs to OBS. Its service is YouTube - RTMPS with a stream
key, saved in OBS's profile (`service.json`) and never in this repo
(`docs/hardware.md` §B3). Visibility is YouTube's business: Unlisted, set in
YouTube Studio, and never something the deck changes. This side only asks OBS
to start or stop the stream over obs-websocket, and turns OBS's own answer
(`GetStreamStatus`) into the key's face.

The face comes from that answer, not from what the deck last pressed, so it is
right after a daemon restart and when a stream ends on its own. No answer
means the key cannot do anything, so it goes dark (layout rule 1). That covers
OBS still starting and OBS gone.

Everything here runs on the daemon's 2 s poll through an injected `request`,
so the whole machine is unit-tested without OBS.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from cueclock.deck import obsws
from cueclock.deck.faces import BG_ERR, BG_LIVE, BG_WARN, Face

HOLD_SUB = "HOLD 1s"
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
Intent = Literal["start", "stop"]


def _obs_request(request_type: str, *, timeout: float = 3.0) -> dict[str, Any] | None:
    return obsws.request(request_type, timeout=timeout)


def off_air_face() -> Face:
    return Face(text="GO LIVE", sub=HOLD_SUB)


def on_air_face(duration_ms: int) -> Face:
    """Red, with the minutes on air: the room is on the internet."""
    return Face(text=f"LIVE\n{duration_ms // 60_000} MIN", sub=HOLD_SUB, bg=BG_LIVE)


def reconnecting_face() -> Face:
    return Face(text="LIVE\nRETRYING", sub=HOLD_SUB, bg=BG_WARN)


def failed_face() -> Face:
    """A start that never went on air. It says STREAM, not LIVE, so a red key
    read from across the room is never mistaken for being on air."""
    return Face(text="STREAM\nFAILED", sub=HOLD_SUB, bg=BG_ERR)


@dataclass
class Stream:
    """OBS's stream as the LIVE key sees it.

    `poll` asks OBS and gives the face, or None while the key must be dark.
    `intent` is what a hold on that face would do, read when the finger comes
    down. `act` does it, but only if OBS still agrees when the hold matures:
    a hold begun to stop a stream that has since died must not start a new one.
    """

    request: Request = _obs_request
    now: Callable[[], float] = time.monotonic
    failed: bool = False
    _status: dict[str, Any] | None = field(default=None, repr=False)
    # A start or stop that OBS has accepted but not yet carried out.
    _pending: tuple[Intent, float] | None = field(default=None, repr=False)

    def poll(self, session_on: bool) -> Face | None:
        """Ask OBS for the stream's state; the key's face, or None for dark.

        Only a deck-owned session has a stream to show: OBS is launched with
        it and quit with it. Off a session the machine resets, so a failure
        never outlives the night it happened on.
        """
        if not session_on:
            self.failed, self._status, self._pending = False, None, None
            return None
        self._status = self.request("GetStreamStatus", timeout=STATUS_TIMEOUT_S)
        if self._status is None:
            return None
        self._settle()
        return self.face()

    def _settle(self) -> None:
        """Resolve a pending start or stop against OBS's latest answer."""
        if self._pending is None:
            return
        intent, since = self._pending
        if intent == "start" and self._active:
            print("live: on air", flush=True)
            self._pending = None
        elif intent == "stop" and not self._active:
            print("live: off air", flush=True)
            self._pending = None
        elif intent == "start" and self.now() - since >= START_WAIT_S:
            print(f"live: start failed — not on air after {START_WAIT_S:.0f} s", flush=True)
            self._pending, self.failed = None, True
        elif intent == "stop" and self.now() - since >= STOP_WAIT_S:
            self._pending = None

    @property
    def _active(self) -> bool:
        return bool(self._status and self._status.get("outputActive"))

    def face(self) -> Face | None:
        """What the key says, from the last answer; None if there was none."""
        if self._status is None:
            return None
        if self._pending is not None:
            return Face(text="STARTING…" if self._pending[0] == "start" else "STOPPING…")
        if self._active:
            if self._status.get("outputReconnecting"):
                return reconnecting_face()
            duration = self._status.get("outputDuration")
            return on_air_face(duration if isinstance(duration, int) else 0)
        return failed_face() if self.failed else off_air_face()

    def intent(self) -> Intent | None:
        """What a hold on the current face would do; None mid-start or stop."""
        if self._status is None or self._pending is not None:
            return None
        return "stop" if self._active else "start"

    def act(self, intent: Intent) -> None:
        """Carry out a matured hold, if OBS still agrees it makes sense."""
        status = self.request("GetStreamStatus", timeout=STATUS_TIMEOUT_S)
        if status is None:
            return  # OBS is gone; the next poll darkens the key
        self._status = status
        if (intent == "stop") != self._active:
            print(f"live: hold to {intent} ignored — the stream had changed", flush=True)
            return
        self.failed = False
        request_type = "StartStream" if intent == "start" else "StopStream"
        if self.request(request_type) is None:
            # obsws has already logged OBS's own reason.
            print(f"live: {request_type} refused", flush=True)
            self.failed = intent == "start"
            return
        print(f"live: {request_type} sent", flush=True)
        self._pending = (intent, self.now())
