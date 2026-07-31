"""WebSocket hub: owns the clock, its 10 Hz tick loop, and connected clients.

This is where wall-clock time enters the system — ``ShotClock`` is pure, and
only this module calls ``time.monotonic()``.
"""

import asyncio
import json
import time

from fastapi import WebSocket

from cueclock.clock import DEFAULT_RULESET, ShotClock, Snapshot, get_ruleset

BROADCAST_INTERVAL_SECONDS = 0.1  # 10 Hz, per docs/design.md §2.1


class Hub:
    def __init__(self) -> None:
        self.clock = ShotClock(get_ruleset(DEFAULT_RULESET), now=time.monotonic())
        self.clients: set[WebSocket] = set()

    # ---------- WebSocket clients ----------

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.clients.add(ws)
        # Snapshot immediately so a new overlay renders without waiting a tick.
        await ws.send_text(json.dumps(self.clock.snapshot().as_dict()))

    def disconnect(self, ws: WebSocket) -> None:
        self.clients.discard(ws)

    async def broadcast(self, snapshot: Snapshot) -> None:
        message = json.dumps(snapshot.as_dict())
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

    # ---------- vision events (dispatched from the `cueclock run` camera thread) ----------

    async def shot_taken(self) -> None:
        self.clock.shot_taken()
        await self.broadcast(self.clock.snapshot())

    async def balls_settled(self) -> None:
        self.clock.balls_settled(time.monotonic())
        await self.broadcast(self.clock.snapshot())
