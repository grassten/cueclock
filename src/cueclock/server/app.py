"""FastAPI app: the HTTP API (Stream Deck contract), WS broadcast, HTML pages.

The `/api/*` URLs are load-bearing — existing Stream Deck buttons point at
them (docs/cv-app-rewrite.md §2.1). Keep them stable.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from cueclock.clock import ALIASES, RULESETS
from cueclock.server.hub import Hub

STATIC_DIR = Path(__file__).parent / "static"


def create_app() -> FastAPI:
    hub = Hub()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
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
        return hub.clock.snapshot().as_dict()

    return app
