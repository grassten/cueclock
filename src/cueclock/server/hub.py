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
from cueclock.server.score import MatchScore

BROADCAST_INTERVAL_SECONDS = 0.1  # 10 Hz, per docs/design.md §2.1


class Hub:
    def __init__(self, score_path: Path | None = None) -> None:
        self.clock = ShotClock(get_ruleset(DEFAULT_RULESET), now=time.monotonic())
        self.clients: set[WebSocket] = set()
        self.score = MatchScore(path=score_path)  # None = in-memory only
        # Match-day kill switch: when False, vision events are ignored and
        # the clock runs manually (Stream Deck / voice / control panel).
        self.vision_enabled = True

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
            await self.broadcast(self.clock.tick(time.monotonic()))
            await asyncio.sleep(BROADCAST_INTERVAL_SECONDS)

    # ---------- commands (each also broadcasts, per the "on every event" rule) ----------

    async def start(self) -> None:
        self.clock.start(time.monotonic())
        await self.broadcast(self.clock.snapshot())

    async def pause(self) -> None:
        self.clock.pause()
        await self.broadcast(self.clock.snapshot())

    async def reset(self) -> None:
        self.clock.reset()
        await self.broadcast(self.clock.snapshot())

    async def new_rack(self) -> None:
        self.clock.new_rack(time.monotonic())
        await self.broadcast(self.clock.snapshot())

    async def restart(self) -> None:
        self.clock.restart(time.monotonic())
        await self.broadcast(self.clock.snapshot())

    async def push(self) -> None:
        self.clock.push(time.monotonic())
        await self.broadcast(self.clock.snapshot())

    async def extension(self) -> bool:
        accepted = self.clock.extension()
        await self.broadcast(self.clock.snapshot())
        return accepted

    async def set_ruleset(self, name: str) -> bool:
        """Switch ruleset by canonical name or alias; False if unknown."""
        try:
            ruleset = get_ruleset(name)
        except KeyError:
            return False
        self.clock.set_ruleset(ruleset)
        await self.broadcast(self.clock.snapshot())
        return True

    async def set_vision(self, enabled: bool) -> None:
        self.vision_enabled = enabled
        await self.broadcast(self.clock.snapshot())

    async def score_changed(self) -> None:
        """Broadcast after any MatchScore mutation (score rides every payload)."""
        await self.broadcast(self.clock.snapshot())

    # ---------- vision events (dispatched from the `cueclock run` camera thread) ----------

    async def shot_taken(self) -> None:
        if not self.vision_enabled:
            return  # kill switch: misbehaving vision must not move the clock
        self.clock.shot_taken()
        await self.broadcast(self.clock.snapshot())

    async def balls_settled(self) -> None:
        if not self.vision_enabled:
            return  # kill switch, as above
        self.clock.balls_settled(time.monotonic())
        await self.broadcast(self.clock.snapshot())
