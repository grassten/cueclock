"""WebSocket hub: owns the clock, its 10 Hz tick loop, and connected clients.

This is where wall-clock time enters the system — ``ShotClock`` is pure, and
only this module calls ``time.monotonic()``. It also owns the match score and
the vision kill switch, and composes them with the clock snapshot into the
one payload every WS client and ``GET /api/state`` see.
"""

import asyncio
import json
import time
from pathlib import Path

from fastapi import WebSocket

from cueclock.clock import DEFAULT_RULESET, ShotClock, Snapshot, get_ruleset
from cueclock.server.journal import SessionJournal
from cueclock.server.score import MatchScore

BROADCAST_INTERVAL_SECONDS = 0.1  # 10 Hz, per docs/design.md §2.1


class Hub:
    def __init__(self, score_path: Path | None = None, journal_path: Path | None = None) -> None:
        self.clock = ShotClock(get_ruleset(DEFAULT_RULESET), now=time.monotonic())
        self.clients: set[WebSocket] = set()
        self.score = MatchScore(path=score_path)  # None = in-memory only
        # Match-day kill switch: when False, vision events are ignored and
        # the clock runs manually (Stream Deck / voice / control panel).
        self.vision_enabled = True
        # Every command / vision event / clock state transition, timestamped —
        # ring dumps copy the tail so a flagged clip records what the live
        # engine actually did (journal.py module docstring).
        self.journal = SessionJournal(path=journal_path)
        self._journal_state = self.clock.state.value

    def _log(self, kind: str, **detail: object) -> None:
        state = self.clock.state.value
        self.journal.log(
            kind,
            state=state,
            remaining=round(self.clock.remaining, 2),
            rack_shots=self.clock.rack_shots,
            **detail,
        )
        self._journal_state = state

    def payload(self, snapshot: Snapshot | None = None) -> dict[str, object]:
        """The composed broadcast/state payload: clock + `vision` + `score`."""
        snap = snapshot if snapshot is not None else self.clock.snapshot()
        return {
            **snap.as_dict(),
            "vision": self.vision_enabled,
            "score": self.score.as_dict(),
        }

    # ---------- WebSocket clients ----------

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.clients.add(ws)
        # Snapshot immediately so a new overlay renders without waiting a tick.
        await ws.send_text(json.dumps(self.payload()))

    def disconnect(self, ws: WebSocket) -> None:
        self.clients.discard(ws)

    async def broadcast(self, snapshot: Snapshot) -> None:
        message = json.dumps(self.payload(snapshot))
        for ws in list(self.clients):
            try:
                await ws.send_text(message)
            except Exception:
                self.clients.discard(ws)

    async def tick_loop(self) -> None:
        while True:
            snapshot = self.clock.tick(time.monotonic())
            if snapshot.state != self._journal_state:
                # GRACE/EXPIRED/auto-restart transitions happen inside tick.
                self._log("tick_transition")
            await self.broadcast(snapshot)
            await asyncio.sleep(BROADCAST_INTERVAL_SECONDS)

    # ---------- commands (each also broadcasts, per the "on every event" rule) ----------

    async def start(self) -> None:
        self.clock.start(time.monotonic())
        self._log("cmd_start")
        await self.broadcast(self.clock.snapshot())

    async def pause(self) -> None:
        self.clock.pause()
        self._log("cmd_pause")
        await self.broadcast(self.clock.snapshot())

    async def reset(self) -> None:
        self.clock.reset()
        self._log("cmd_reset")
        await self.broadcast(self.clock.snapshot())

    async def new_rack(self) -> None:
        self.clock.new_rack(time.monotonic())
        self._log("cmd_new_rack")
        await self.broadcast(self.clock.snapshot())

    async def restart(self) -> None:
        self.clock.restart(time.monotonic())
        self._log("cmd_restart")
        await self.broadcast(self.clock.snapshot())

    async def push(self) -> None:
        self.clock.push(time.monotonic())
        self._log("cmd_push")
        await self.broadcast(self.clock.snapshot())

    async def extension(self) -> bool:
        accepted = self.clock.extension()
        self._log("cmd_extension", accepted=accepted)
        await self.broadcast(self.clock.snapshot())
        return accepted

    async def set_ruleset(self, name: str) -> bool:
        """Switch ruleset by canonical name or alias; False if unknown."""
        try:
            ruleset = get_ruleset(name)
        except KeyError:
            return False
        self.clock.set_ruleset(ruleset)
        self._log("cmd_ruleset", ruleset=self.clock.ruleset.name)
        await self.broadcast(self.clock.snapshot())
        return True

    async def set_vision(self, enabled: bool) -> None:
        self.vision_enabled = enabled
        self._log("cmd_vision", enabled=enabled)
        await self.broadcast(self.clock.snapshot())

    async def score_changed(self) -> None:
        """Broadcast after any MatchScore mutation (score rides every payload)."""
        await self.broadcast(self.clock.snapshot())

    # ---------- vision events (dispatched from the `cueclock run` camera thread) ----------

    async def shot_taken(self, t: float | None = None) -> None:
        if not self.vision_enabled:
            self._log("vision_shot_taken_killed", t=t)
            return  # kill switch: misbehaving vision must not move the clock
        self.clock.shot_taken()
        self._log("vision_shot_taken", t=t)
        await self.broadcast(self.clock.snapshot())

    async def balls_settled(self, t: float | None = None) -> None:
        if not self.vision_enabled:
            self._log("vision_balls_settled_killed", t=t)
            return  # kill switch, as above
        self.clock.balls_settled(time.monotonic())
        self._log("vision_balls_settled", t=t)
        await self.broadcast(self.clock.snapshot())

    async def scratch_seen(self, t: float | None = None) -> None:
        # No clock action in v1 (CHANGELOG decision) — journal only.
        self._log("vision_scratch", t=t)
