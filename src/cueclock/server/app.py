"""FastAPI app: the HTTP API (Stream Deck contract), WS broadcast, HTML pages.

The `/api/*` URLs are load-bearing — existing Stream Deck buttons point at
them (docs/design.md §2.1). Keep them stable.
"""

import asyncio
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from cueclock.clock import ALIASES, RULESETS
from cueclock.server.hub import Hub

STATIC_DIR = Path(__file__).parent / "static"


def create_app(score_path: Path | None = None) -> FastAPI:
    hub = Hub(score_path=score_path)  # None = score is in-memory only

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Stashed for `cueclock run`: vision events born on the camera thread
        # are dispatched into this loop via run_coroutine_threadsafe.
        app.state.loop = asyncio.get_running_loop()
        tick_task = asyncio.create_task(hub.tick_loop())
        try:
            yield
        finally:
            tick_task.cancel()

    app = FastAPI(title="cueclock", lifespan=lifespan)
    app.state.hub = hub  # reachable for future vision/audio wiring

    @app.get("/")
    async def index() -> JSONResponse:
        return JSONResponse({"endpoints": ["/shotclock", "/control", "/ws", "/api/*"]})

    @app.get("/shotclock")
    async def shotclock_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "shotclock.html")

    @app.get("/control")
    async def control_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "control.html")

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await hub.connect(ws)
        try:
            while True:
                await ws.receive_text()  # clients never send; this just detects disconnect
        except WebSocketDisconnect:
            hub.disconnect(ws)

    @app.post("/api/start")
    async def api_start() -> dict[str, bool]:
        await hub.start()
        return {"ok": True}

    @app.post("/api/pause")
    async def api_pause() -> dict[str, bool]:
        await hub.pause()
        return {"ok": True}

    @app.post("/api/reset")
    async def api_reset() -> dict[str, bool]:
        await hub.reset()
        return {"ok": True}

    @app.post("/api/rack")
    async def api_rack() -> dict[str, bool]:
        """Per-rack reset: fresh shot count + extension pool, IDLE (between games)."""
        await hub.new_rack()
        return {"ok": True}

    @app.post("/api/restart")
    async def api_restart() -> dict[str, bool]:
        """Force a fresh countdown running immediately (missed auto-reset recovery)."""
        await hub.restart()
        return {"ok": True}

    @app.post("/api/push")
    async def api_push() -> dict[str, bool]:
        """Push out called: re-enter the WNT §5 60 s first-shot window."""
        await hub.push()
        return {"ok": True}

    @app.post("/api/extension")
    async def api_extension() -> dict[str, bool]:
        return {"ok": True, "accepted": await hub.extension()}

    @app.post("/api/ruleset/{name}")
    async def api_set_ruleset(name: str) -> JSONResponse:
        if not await hub.set_ruleset(name):
            return JSONResponse({"ok": False, "error": f"unknown ruleset: {name}"}, status_code=404)
        return JSONResponse({"ok": True})

    @app.get("/api/rulesets")
    async def api_rulesets() -> dict[str, object]:
        return {
            "active": hub.clock.ruleset.name,
            "available": list(RULESETS),
            "aliases": ALIASES,
        }

    @app.get("/api/state")
    async def api_state() -> dict[str, object]:
        return hub.payload()  # same composed payload the WS broadcasts

    @app.get("/api/score")
    async def api_score() -> dict[str, object]:
        return hub.score.as_dict()

    @app.post("/api/score/reset")
    async def api_score_reset() -> dict[str, object]:
        """Scores back to zero; names and race-to are kept."""
        hub.score.reset_scores()
        await hub.score_changed()
        return {"ok": True, "score": hub.score.as_dict()}

    @app.post("/api/score/config")
    async def api_score_config(body: dict[str, object]) -> dict[str, object]:
        """Partial update: `p1_name` / `p2_name` / `race_to` (null clears)."""
        hub.score.configure(body)
        await hub.score_changed()
        return {"ok": True, "score": hub.score.as_dict()}

    @app.post("/api/score/{player}/{op}")
    async def api_score_adjust(player: str, op: str) -> JSONResponse:
        if player not in ("p1", "p2") or op not in ("increment", "decrement"):
            return JSONResponse(
                {"ok": False, "error": f"unknown score action: {player}/{op}"},
                status_code=404,
            )
        hub.score.adjust(player, 1 if op == "increment" else -1)  # floors at 0
        await hub.score_changed()
        return JSONResponse({"ok": True, "score": hub.score.as_dict()})

    @app.post("/api/vision/on")
    async def api_vision_on() -> dict[str, object]:
        await hub.set_vision(True)
        return {"ok": True, "vision": True}

    @app.post("/api/vision/off")
    async def api_vision_off() -> dict[str, object]:
        """Match-day kill switch: vision events stop moving the clock."""
        await hub.set_vision(False)
        return {"ok": True, "vision": False}

    @app.post("/api/dump")
    async def api_dump(label: str = "flagged") -> JSONResponse:
        """Save the last ~12 s of raw frames as a labeled clip.

        Only live under `cueclock run` (which wires `app.state.dump` to its
        ring buffer). This is the "that was wrong / that was interesting"
        button: every disagreement becomes replayable corpus evidence
        (BACKLOG.md idea bank §8).
        """
        dump: Callable[[str], str] | None = getattr(app.state, "dump", None)
        if dump is None:
            return JSONResponse(
                {"error": "no ring buffer — /api/dump only works under `cueclock run`"},
                status_code=503,
            )
        safe = re.sub(r"[^A-Za-z0-9_-]+", "", label)[:40] or "flagged"
        try:
            path = await asyncio.to_thread(dump, safe)
        except (RuntimeError, OSError) as exc:
            return JSONResponse(
                {"error": f"dump failed: {exc} (is the T7 mounted?)"}, status_code=503
            )
        return JSONResponse({"saved": path})

    return app
