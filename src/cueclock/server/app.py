"""FastAPI app: the HTTP API (Stream Deck contract), WS broadcast, HTML pages.

The `/api/*` URLs are load-bearing — existing Stream Deck buttons point at
them (docs/design.md §2.1). Keep them stable.
"""

import asyncio
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from cueclock.clock import ALIASES, MODES, RULESETS
from cueclock.server.hub import Hub

STATIC_DIR = Path(__file__).parent / "static"
# Spoken cues, synthesized by scripts/make_voice_assets.py and committed —
# Kokoro-82M is Apache-2.0, so unlike the macOS `say` output this first shipped
# with, the audio can live in a public repo (docs/hardware.md Part D).
VOICE_DIR = STATIC_DIR / "voice"


def create_app(score_path: Path | None = None, journal_path: Path | None = None) -> FastAPI:
    hub = Hub(score_path=score_path, journal_path=journal_path)  # None = in-memory only

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
        return JSONResponse({"endpoints": ["/shotclock", "/replay", "/control", "/ws", "/api/*"]})

    @app.get("/shotclock")
    async def shotclock_page() -> FileResponse:
        """Every clock overlay, selected by `?view=` (docs/hardware.md §B3).

        `?view=bar` is the live scene's bottom bar — clock *and* scoreboard
        from one page, because the drain spans both and no overlay can span
        two OBS sources. It replaced the standalone `/score` page on
        2026-08-03 (issue #23); `?view=full` and the bare square are unchanged.
        """
        return FileResponse(STATIC_DIR / "shotclock.html")

    @app.get("/control")
    async def control_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "control.html")

    @app.get("/replay")
    async def replay_page() -> FileResponse:
        """REPLAY badge for the OBS Replay scene (`?s=<seconds>` drains the bar)."""
        return FileResponse(STATIC_DIR / "replay.html")

    @app.get("/audio/extension_called.wav")
    async def extension_called_audio() -> FileResponse:
        """The ref's "Extension called" announcement for the shotclock overlay.

        One fixed path rather than a served directory: the only asset there is
        is this one, and a name parameter would be a traversal to validate for
        nothing.
        """
        path = VOICE_DIR / "extension_called.wav"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="run scripts/make_voice_assets.py")
        return FileResponse(path, media_type="audio/wav")

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

    @app.post("/api/mode/{name}")
    async def api_set_mode(name: str) -> JSONResponse:
        """Switch session mode: match / run_out / no_clock (docs/design.md §2.1)."""
        if not await hub.set_mode(name):
            return JSONResponse({"ok": False, "error": f"unknown mode: {name}"}, status_code=404)
        return JSONResponse({"ok": True})

    @app.get("/api/rulesets")
    async def api_rulesets() -> dict[str, object]:
        return {
            "active": hub.clock.ruleset.name,
            "available": list(RULESETS),
            "aliases": ALIASES,
        }

    @app.get("/api/modes")
    async def api_modes() -> dict[str, object]:
        return {"active": hub.clock.mode.name, "available": list(MODES)}

    @app.get("/api/state")
    async def api_state() -> dict[str, object]:
        return hub.payload()  # same composed payload the WS broadcasts

    @app.get("/api/replay_anchors")
    async def api_replay_anchors() -> dict[str, object]:
        """Both points the instant replay can seek to, as ages in seconds.

        `shot_age_s` is the last ShotTaken (a tapped Replay key), `rack_age_s`
        is where this rack began (a held one) — either may be null, which the
        deck reads as "play as far back as the buffer goes".

        Its own endpoint rather than fields of `/api/state` because the deck
        polls state every 2 s and these answers are only worth having at the
        instant of the press: two seconds of staleness is two seconds of the
        replay starting in the wrong place. Both in one response because a
        press only ever needs one round trip, and it wants them measured
        against the same instant — the save that ends the file.

        Replaced `GET /api/last_shot` (2026-08-05) rather than growing a second
        field onto it: an endpoint named for one anchor that returns two is a
        name that lies. The old URL 404s deliberately — a deck daemon left on
        the old build should fail visibly, not quietly cue every replay to the
        top of the buffer.
        """
        return {"shot_age_s": hub.last_shot_age(), "rack_age_s": hub.rack_age()}

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

    @app.post("/api/groundtruth")
    async def api_ground_truth() -> dict[str, bool]:
        """Record that a shot was actually taken (#19). Never moves the clock.

        Always live — not a mode, no toggle, no flag. Once the design
        collapsed to a single unconditional key the only remaining cost is the
        press itself, so there is no reason to hide it behind state the player
        has to remember. Arming is inferred offline from the press runs
        themselves; see `cueclock report`.
        """
        hub.ground_truth_press()
        return {"ok": True}

    @app.post("/api/dump")
    async def api_dump(label: str = "flagged") -> JSONResponse:
        """Save the ring buffer's raw frames as a labeled clip (~20 s default).

        Only live under `cueclock run` (which wires `app.state.dump` to its
        ring buffer). This is the "that was wrong / that was interesting"
        button: every disagreement becomes replayable corpus evidence.
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
            # Dumps stage on the internal SSD since 2026-08-01, so the old
            # "is the T7 mounted?" hint is wrong — the writer's own message
            # names the staging root and the disk-space remedy.
            return JSONResponse({"error": f"dump failed: {exc}"}, status_code=503)
        return JSONResponse({"saved": path})

    return app
