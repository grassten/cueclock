"""FastAPI app: the HTTP API (Stream Deck contract), WS broadcast, HTML pages.

The `/api/*` URLs are load-bearing — existing Stream Deck buttons point at
them (docs/design.md §2.1). Keep them stable.
"""

import asyncio
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from cueclock.clock import ALIASES, MODES, RULESETS
from cueclock.server import fargo, lan
from cueclock.server.bracket import BracketError
from cueclock.server.games import GAMES
from cueclock.server.hub import Hub

STATIC_DIR = Path(__file__).parent / "static"
# Spoken cues, synthesized by scripts/make_voice_assets.py and committed —
# Kokoro-82M is Apache-2.0, so unlike the macOS `say` output this first shipped
# with, the audio can live in a public repo (docs/hardware.md Part D).
VOICE_DIR = STATIC_DIR / "voice"


def create_app(
    score_path: Path | None = None,
    journal_path: Path | None = None,
    bracket_path: Path | None = None,
    archive_dir: Path | None = None,
) -> FastAPI:
    hub = Hub(  # every path None = in-memory only
        score_path=score_path,
        journal_path=journal_path,
        bracket_path=bracket_path,
        archive_dir=archive_dir,
    )

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
        return JSONResponse(
            {
                "endpoints": [
                    "/shotclock",
                    "/replay",
                    "/control",
                    "/bracket",
                    "/games",
                    "/ws",
                    "/api/*",
                ]
            }
        )

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

    @app.get("/games")
    async def games_page() -> FileResponse:
        """The minigames HUD, 1920x1080, filling its own Games scene (#46).

        One source with two postures: an opaque menu while the picker is up,
        and chrome-only — a bottom bar over a transparent canvas — once a game
        owns the table. Everything on the cloth is drawn server-side into the
        Syphon feed instead (`display/pipeline.py`), so this page never has to
        align itself with the table image.
        """
        return FileResponse(STATIC_DIR / "games.html")

    @app.get("/bracket")
    async def bracket_page() -> FileResponse:
        """The double-elimination bracket, 1920x1080, its own OBS scene (#43).

        It draws the bracket and nothing else — no current-match panel, no
        up-next list. The play scene's score bar already says who is up and the
        live match is the highlighted box, so repeating either only costs the
        boxes the space that makes them readable from the couch.
        """
        return FileResponse(STATIC_DIR / "bracket.html")

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
        """The listing plus per-ruleset facts, so the control page can caption
        the pick (shot time, first-shot allowance, extension length, untimed)
        without hardcoding the rulebook."""
        return {
            "active": hub.clock.ruleset.name,
            "available": list(RULESETS),
            "aliases": ALIASES,
            "details": {
                name: {
                    "base_seconds": r.base_seconds,
                    "first_shot_seconds": r.first_shot_seconds,
                    "extension_seconds": r.extension_seconds,
                    "timed": r.timed,
                }
                for name, r in RULESETS.items()
            },
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

    @app.post("/api/match/clear")
    async def api_match_clear() -> dict[str, object]:
        """End of day: bracket archived-and-cleared, score back to defaults.

        The deck's Session hold calls this on the way down so the next
        session comes up on PLAYER 1 / PLAYER 2 instead of yesterday's match.
        """
        archived = await hub.clear_match_day()
        return {"ok": True, "archived": archived.name if archived is not None else None}

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

    @app.get("/api/fargo/search")
    async def api_fargo_search(q: str = "") -> JSONResponse:
        """Proxy FargoRate's player search for the control page (issue #27).

        Lives here because dashboard.fargorate.com sends no CORS headers, so
        the browser can't ask it directly. Read-only against a third party —
        an upstream failure is a 502 the page renders as "Fargo unreachable",
        never an error that touches the clock or the score.
        """
        if not q.strip():
            return JSONResponse({"ok": False, "error": "empty query"}, status_code=400)
        try:
            players = await fargo.search(q.strip())
        except httpx.HTTPError as exc:
            return JSONResponse(
                {"ok": False, "error": f"fargorate unreachable: {exc}"}, status_code=502
            )
        return JSONResponse({"ok": True, "players": players})

    # ---------- bracket (#43) ----------
    #
    # Optional state layered on top of the score: with no bracket loaded every
    # one of these answers 404/409 and every other endpoint behaves exactly as
    # it did before, which is what makes casual night and tournament night the
    # same software. A refusal is a 4xx carrying the bracket's own words, so the
    # phone can print "Match 5 has already been played — clear its result first"
    # without knowing the rules that produced it.

    def _bracket_error(exc: BracketError) -> JSONResponse:
        missing = str(exc) == "no bracket loaded"
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=404 if missing else 409)

    @app.get("/api/bracket")
    async def api_bracket() -> JSONResponse:
        if hub.bracket is None:
            return JSONResponse({"ok": True, "bracket": None})
        return JSONResponse({"ok": True, "bracket": hub.bracket.as_dict(hub.live_scores())})

    @app.post("/api/bracket")
    async def api_bracket_create(body: dict[str, object]) -> JSONResponse:
        """Build the draw: `{name, players[], a_race, b_race, handicap, seeding}`."""
        try:
            bracket = await hub.create_bracket(body)
        except BracketError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "bracket": bracket.as_dict(hub.live_scores())})

    @app.delete("/api/bracket")
    async def api_bracket_clear() -> dict[str, object]:
        """Back to casual. Anything played is archived on the way out."""
        archived = await hub.clear_bracket()
        return {"ok": True, "archived": archived.name if archived is not None else None}

    @app.patch("/api/bracket")
    async def api_bracket_races(body: dict[str, object]) -> JSONResponse:
        """Change the base races on a bracket underway: `{a_race, b_race}`."""
        try:
            bracket = await hub.set_bracket_races(body)
        except BracketError as exc:
            return _bracket_error(exc)
        return JSONResponse({"ok": True, "a_race": bracket.a_race, "b_race": bracket.b_race})

    @app.post("/api/bracket/player/{index}/withdraw")
    async def api_bracket_withdraw(index: int) -> JSONResponse:
        """Concede every unplayed match of a player who has gone home."""
        try:
            player = await hub.withdraw_player(index, True)
        except BracketError as exc:
            return _bracket_error(exc)
        return JSONResponse({"ok": True, "player": player.name, "withdrawn": True})

    @app.delete("/api/bracket/player/{index}/withdraw")
    async def api_bracket_reinstate(index: int) -> JSONResponse:
        """They came back. The concessions were derived, so they simply undo."""
        try:
            player = await hub.withdraw_player(index, False)
        except BracketError as exc:
            return _bracket_error(exc)
        return JSONResponse({"ok": True, "player": player.name, "withdrawn": False})

    @app.post("/api/bracket/archive")
    async def api_bracket_archive() -> JSONResponse:
        try:
            path = hub.archive_bracket()
        except BracketError as exc:
            return _bracket_error(exc)
        return JSONResponse({"ok": True, "path": str(path)})

    @app.get("/api/bracket/archives")
    async def api_bracket_archives() -> JSONResponse:
        """Past brackets on disk, newest first — the Load past bracket list."""
        if hub.archive_dir is None or not hub.archive_dir.is_dir():
            return JSONResponse({"ok": True, "archives": []})
        files = sorted(
            hub.archive_dir.glob("bracket-*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return JSONResponse({"ok": True, "archives": [f.name for f in files]})

    @app.post("/api/bracket/archives/{name}")
    async def api_bracket_reopen(name: str) -> JSONResponse:
        """Reopen an archived bracket as the live one."""
        try:
            bracket = await hub.reopen_archive(name)
        except BracketError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
        return JSONResponse({"ok": True, "bracket": bracket.as_dict(hub.live_scores())})

    @app.patch("/api/bracket/match/{match_id}")
    async def api_bracket_edit(match_id: int, body: dict[str, object]) -> JSONResponse:
        try:
            await hub.edit_match(match_id, body)
        except BracketError as exc:
            return _bracket_error(exc)
        return JSONResponse({"ok": True})

    @app.post("/api/bracket/match/{match_id}/load")
    async def api_bracket_load(match_id: int) -> JSONResponse:
        try:
            match = await hub.load_match(match_id)
        except BracketError as exc:
            return _bracket_error(exc)
        return JSONResponse({"ok": True, "match": match.id, "targets": match.targets})

    @app.post("/api/bracket/match/{match_id}/result")
    async def api_bracket_result(
        match_id: int, body: dict[str, object] | None = None
    ) -> JSONResponse:
        """Confirm a result and advance. Body may carry explicit `scores`.

        Without one the loaded match's live scores are the result — that is the
        deck's confirm press, where the score on the wall *is* the thing being
        confirmed and re-typing it would only be a way to disagree with it.
        """
        scores: tuple[int, int] | None = None
        raw = (body or {}).get("scores")
        if isinstance(raw, list) and len(raw) == 2:
            scores = (int(raw[0]), int(raw[1]))
        try:
            result = await hub.report_result(match_id, scores)
        except BracketError as exc:
            return _bracket_error(exc)
        return JSONResponse({"ok": True, **result})

    @app.post("/api/bracket/match/{match_id}/concede")
    async def api_bracket_concede(match_id: int, body: dict[str, object]) -> JSONResponse:
        """`{slot: 0|1}` — one player gives up one match and plays the rest.

        Distinct from a withdrawal, which concedes every unplayed match they
        have. Undone by the DELETE below rather than by clearing a result: a
        conceded match was never played, so there is no result to clear.
        """
        try:
            match = await hub.concede_match(match_id, body)
        except BracketError as exc:
            return _bracket_error(exc)
        return JSONResponse({"ok": True, "match": match.id, "conceded": match.conceded})

    @app.delete("/api/bracket/match/{match_id}/concede")
    async def api_bracket_unconcede(match_id: int) -> JSONResponse:
        """They will play it after all."""
        try:
            match = await hub.concede_match(match_id, None)
        except BracketError as exc:
            return _bracket_error(exc)
        return JSONResponse({"ok": True, "match": match.id, "conceded": None})

    @app.delete("/api/bracket/match/{match_id}/result")
    async def api_bracket_clear_result(match_id: int) -> JSONResponse:
        try:
            await hub.clear_result(match_id)
        except BracketError as exc:
            return _bracket_error(exc)
        return JSONResponse({"ok": True})

    # ---------- minigames (#46) ----------
    #
    # Layered on the mode axis the way the bracket is layered on the score:
    # outside games mode every one of these 409s and `/api/state` carries
    # `games: null`, so nothing on any other page changes. The deck talks HTTP
    # like every other key (docs/design.md §2.1) — there is no second protocol.

    @app.get("/api/games")
    async def api_games() -> dict[str, object]:
        """The registry and the active game — the picker cards with their stats.

        Answers whatever the mode is, unlike the POSTs below: this is a
        *listing*, and the phone's Drills tab has to be able to read the
        catalog from the couch with a match on the wall.
        """
        return {
            "mode": hub.clock.mode.name,
            "in_games": hub.in_games,
            "available": list(GAMES),
            **hub.games.payload(),
        }

    @app.post("/api/games/select/{name}")
    async def api_games_select(name: str) -> JSONResponse:
        """Pick a game — the picker's one press."""
        if not hub.in_games:
            return JSONResponse({"ok": False, "error": "not in games mode"}, status_code=409)
        if not await hub.select_game(name):
            return JSONResponse({"ok": False, "error": f"unknown game: {name}"}, status_code=404)
        return JSONResponse({"ok": True, "active": name})

    @app.post("/api/games/back")
    async def api_games_back() -> JSONResponse:
        """End the active game and show the picker. Nothing is parked."""
        if not await hub.press_game("back"):
            return JSONResponse({"ok": False, "error": "not in games mode"}, status_code=409)
        return JSONResponse({"ok": True})

    @app.post("/api/games/press/{verb}")
    async def api_games_press(verb: str) -> JSONResponse:
        """One press on the active game's own verb row."""
        if not await hub.press_game(verb):
            return JSONResponse(
                {"ok": False, "error": f"no such verb right now: {verb}"}, status_code=409
            )
        return JSONResponse({"ok": True})

    @app.get("/api/lan")
    async def api_lan(request: Request) -> JSONResponse:
        """Where the scoring page lives on the LAN, and a QR code of it (#43).

        The port comes off the request because it is the same port whatever
        address you arrived on; only the host has to be discovered, since a page
        OBS loaded over loopback cannot tell a phone where to go. `url: null`
        means this machine has no LAN address — the page says so rather than
        printing a QR of the loopback nobody can reach.
        """
        port = request.url.port or 80
        url = lan.join_url(port)
        if url is None:
            return JSONResponse({"ok": True, "url": None, "qr": None})
        return JSONResponse({"ok": True, "url": url, "qr": lan.qr_data_uri(url)})

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
