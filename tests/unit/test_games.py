"""Minigames platform (#46) — the registry, the games, and the seams.

Unit-only by design: nothing in `vision/` changes for this feature, so the
corpus battery has nothing to say about it. What *is* checked here is every
place a number or a name is duplicated across a boundary the type system
cannot see — the table geometry the server re-states rather than importing,
and the key number the TV cards print.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cueclock.deck.daemon import _games_fields, build_state
from cueclock.deck.layout import COLS, GAMES_FIRST_KEY, GAMES_MODE, GAMES_ROW, games_keys
from cueclock.deck.session import SessionState
from cueclock.display.marks import Bracket, Circle, Grid, Line, Rect
from cueclock.server import create_app
from cueclock.server.games import (
    BALL_RADIUS_MM,
    CELL_EDGE_MARGIN_MM,
    CELL_LENGTH_MM,
    CELL_WIDTH_MM,
    GAMES,
    GAMES_ROW_FIRST_KEY,
    LAG_ATTEMPT,
    TABLE_LENGTH_MM,
    TABLE_WIDTH_MM,
    Ball,
    GameCtx,
    GameSession,
    GameStats,
    LagGame,
    ShotsGame,
    Zone,
    cell_of,
    cells_out,
    size_face,
)
from cueclock.server.report import game_stats, shot_records

# ---------------------------------------------------------------------------
# The duplicated constants. `server/games.py` re-states these rather than
# importing them, because importing `vision/` would drag cv2 and numpy into the
# bare server — so this is the thing that keeps them honest.
# ---------------------------------------------------------------------------


def test_table_geometry_matches_the_calibration() -> None:
    from cueclock.vision.calibration import TABLE_LENGTH_MM as CAL_LENGTH
    from cueclock.vision.calibration import TABLE_WIDTH_MM as CAL_WIDTH

    assert (TABLE_LENGTH_MM, TABLE_WIDTH_MM) == (CAL_LENGTH, CAL_WIDTH)


def test_ball_radius_matches_the_vision_pipeline() -> None:
    from cueclock.vision.pipeline import BALL_RADIUS_MM as VISION_RADIUS

    assert BALL_RADIUS_MM == VISION_RADIUS


def test_first_picker_key_matches_the_deck_row() -> None:
    """The TV card prints "Key 11"; the deck had better put it there."""
    assert GAMES_ROW_FIRST_KEY == GAMES_FIRST_KEY == (GAMES_ROW - 1) * COLS + 1


# ---------------------------------------------------------------------------
# Lag
# ---------------------------------------------------------------------------


def _lag() -> tuple[LagGame, list[tuple[str, dict[str, object]]]]:
    lines: list[tuple[str, dict[str, object]]] = []
    game = LagGame(GameCtx(log=lambda kind, **detail: lines.append((kind, detail))))
    return game, lines


def test_lag_measures_the_gap_not_the_centre() -> None:
    """A ball frozen on the head rail reads ~0, not one ball radius."""
    game, lines = _lag()
    game.on_settle(1.0, [Ball(x_mm=BALL_RADIUS_MM, y_mm=480.0, is_cue=True)])
    assert game.last_mm == 0.0
    game.on_settle(2.0, [Ball(x_mm=BALL_RADIUS_MM + 43.0, y_mm=480.0, is_cue=True)])
    assert game.last_mm == 43.0
    assert [kind for kind, _ in lines] == [LAG_ATTEMPT, LAG_ATTEMPT]
    assert lines[-1][1]["distance_mm"] == 43


def test_lag_keeps_the_best_and_every_settle_is_an_attempt() -> None:
    game, _ = _lag()
    for x in (200.0, 60.0, 900.0):
        game.on_settle(1.0, [Ball(x_mm=x, y_mm=480.0, is_cue=True)])
    assert game.attempts == 3
    assert game.best_mm == 60.0 - BALL_RADIUS_MM
    assert game.last_mm == 900.0 - BALL_RADIUS_MM
    answer = game.hud().answer
    assert answer is not None and answer.eyebrow == "BEST"
    assert answer.word == str(round(60.0 - BALL_RADIUS_MM))


def test_lag_identity_falls_back_by_elimination_then_refuses() -> None:
    """Crown first; a one-ball table is unambiguous; anything else scores nothing."""
    game, _ = _lag()
    game.on_settle(1.0, [Ball(x_mm=300.0, y_mm=480.0)])  # no crown, one ball
    assert game.attempts == 1
    game.on_settle(2.0, [Ball(x_mm=300.0, y_mm=100.0), Ball(x_mm=800.0, y_mm=400.0)])
    assert game.attempts == 1  # two balls, no crown: not a lag
    game.on_settle(3.0, [Ball(x_mm=300.0, y_mm=100.0), Ball(x_mm=800.0, y_mm=400.0, is_cue=True)])
    assert game.attempts == 2  # the crown resolves it


def test_lag_draws_only_its_bracket() -> None:
    """No grid on the cloth: lag has no zones (mockups v1 §4)."""
    game, _ = _lag()
    assert game.marks() == ()
    game.on_settle(1.0, [Ball(x_mm=220.0, y_mm=480.0, is_cue=True)])
    (mark,) = game.marks()
    assert isinstance(mark, Bracket)
    assert mark.x0_mm == 0.0
    assert mark.x1_mm == 220.0 - BALL_RADIUS_MM  # the bracket ends at the ball's edge


def test_lag_card_reads_never_played_without_history() -> None:
    game, _ = _lag()
    card = game.card(GameStats())
    assert card["last_played"] is None
    assert [s["value"] for s in card["stats"]] == ["—", "0"]  # type: ignore[index,union-attr]


# ---------------------------------------------------------------------------
# The session
# ---------------------------------------------------------------------------


def test_session_picks_and_ends_a_game() -> None:
    lines: list[str] = []
    session = GameSession(log=lambda kind, **detail: lines.append(kind))
    assert session.payload()["active"] is None
    assert session.select("nope", players=[]) is False
    assert session.select("lag", players=["Steven"]) is True
    assert session.payload()["active"] == "lag"
    assert session.verbs()[0].name == "back"  # BACK is the platform's, always first
    assert session.press("back") is True
    assert session.payload()["active"] is None
    assert lines == ["game_started", "game_ended"]


def test_session_picker_names_a_key_per_game() -> None:
    session = GameSession(log=lambda kind, **detail: None)
    cards = session.picker()
    assert [c["name"] for c in cards] == list(GAMES)
    assert [c["key"] for c in cards] == [GAMES_ROW_FIRST_KEY + i for i in range(len(GAMES))]


def test_session_settles_against_the_newest_layout() -> None:
    session = GameSession(log=lambda kind, **detail: None)
    session.select("lag", players=[])
    session.note_balls((Ball(x_mm=128.575, y_mm=480.0, is_cue=True),), moving=False)
    session.on_settle(1.0)
    game = session.active
    assert isinstance(game, LagGame)
    assert game.last_mm == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# The hub seam and the HTTP contract
# ---------------------------------------------------------------------------


def test_games_payload_is_null_outside_games_mode() -> None:
    with TestClient(create_app()) as client:
        assert client.get("/api/state").json()["games"] is None
        # The picker POSTs refuse rather than switching the mode for you: the
        # Mode key is the one thing that says what you are doing at the table.
        assert client.post("/api/games/select/lag").status_code == 409
        assert client.post("/api/games/back").status_code == 409


def test_games_mode_lights_the_picker_then_the_verbs() -> None:
    with TestClient(create_app()) as client:
        assert client.post("/api/mode/games").json() == {"ok": True}
        games = client.get("/api/state").json()["games"]
        assert games["active"] is None
        assert [card["name"] for card in games["picker"]] == list(GAMES)
        assert client.post("/api/games/select/lag").json() == {"ok": True, "active": "lag"}
        games = client.get("/api/state").json()["games"]
        assert games["active"] == "lag"
        assert games["picker"] == []
        assert [v["name"] for v in games["verbs"]] == ["back"]
        assert games["hud"]["name"] == "Lag"
        assert client.post("/api/games/press/nope").status_code == 409
        assert client.post("/api/games/back").json() == {"ok": True}
        assert client.get("/api/state").json()["games"]["active"] is None


def test_leaving_games_mode_ends_the_game() -> None:
    with TestClient(create_app()) as client:
        client.post("/api/mode/games")
        client.post("/api/games/select/lag")
        client.post("/api/mode/match")
        assert client.get("/api/state").json()["games"] is None
        assert client.get("/api/games").json()["active"] is None


def test_games_listing_answers_in_any_mode() -> None:
    """The phone's catalog has to be readable from the couch, mid-match."""
    with TestClient(create_app()) as client:
        listing = client.get("/api/games").json()
        assert listing["in_games"] is False
        assert listing["available"] == list(GAMES)
        assert [card["name"] for card in listing["picker"]] == list(GAMES)


def test_unknown_game_is_a_404() -> None:
    with TestClient(create_app()) as client:
        client.post("/api/mode/games")
        assert client.post("/api/games/select/snooker").status_code == 404


# ---------------------------------------------------------------------------
# The deck row
# ---------------------------------------------------------------------------


def _state(**fields: object) -> object:
    return build_state({"mode": GAMES_MODE, **fields}, SessionState.ON)


def test_games_row_is_empty_outside_games_mode() -> None:
    state = build_state({"mode": "match"}, SessionState.ON)
    assert games_keys(state) == ()


def test_picker_becomes_the_games_row() -> None:
    state = build_state(
        {
            "mode": GAMES_MODE,
            "games": {
                "active": None,
                "picker": [{"name": "shots", "title": "SHOTS"}, {"name": "lag", "title": "LAG"}],
                "verbs": [],
            },
        },
        SessionState.ON,
    )
    keys = games_keys(state)
    assert [k.index for k in keys] == [GAMES_FIRST_KEY - 1, GAMES_FIRST_KEY]
    assert [k.text for k in keys] == ["SHOTS", "LAG"]
    assert keys[0].action.path == "/api/games/select/shots"  # type: ignore[union-attr]


def test_disabled_verbs_leave_dark_cells() -> None:
    """Layout rule A: a key that can't be pressed goes dark, it doesn't dim."""
    state = build_state(
        {
            "mode": GAMES_MODE,
            "games": {
                "active": "shots",
                "picker": [],
                "verbs": [
                    {"name": "back", "text": "BACK", "sub": "TO GAMES"},
                    {"name": "retry", "text": "RETRY", "enabled": False},
                    {"name": "save", "text": "SAVE", "sub": "SHOT 15", "armed": True},
                ],
            },
        },
        SessionState.ON,
    )
    keys = games_keys(state)
    assert [k.name for k in keys] == ["game_back", "game_save"]
    # The armed key keeps its own column — nothing reflows when a key leaves.
    assert keys[1].col == 3
    assert keys[0].face(state).sub == "TO GAMES"
    assert keys[1].face(state).bg is not None


def test_games_fields_survive_a_null_payload() -> None:
    """Every other mode leaves row 3 exactly the row it was."""
    assert _games_fields(None) == {}
    state = build_state({"mode": "match"}, SessionState.ON)
    assert state.games_active is None and state.games_picker == () and state.games_verbs == ()


# ---------------------------------------------------------------------------
# Journal-derived stats
# ---------------------------------------------------------------------------


def _journal(tmp_path: Path, name: str, records: list[dict[str, object]]) -> None:
    (tmp_path / name).write_text("".join(json.dumps(r) + "\n" for r in records))


def test_game_stats_derive_from_the_journal(tmp_path: Path) -> None:
    _journal(
        tmp_path,
        "session-20260820-100000.jsonl",
        [
            {"kind": "game_started", "game": "lag", "wall": "2026-08-20T10:00:00+00:00"},
            {
                "kind": "lag_attempt",
                "game": "lag",
                "distance_mm": 43,
                "wall": "2026-08-20T10:01:00+00:00",
            },
            {
                "kind": "lag_attempt",
                "game": "lag",
                "distance_mm": 12,
                "wall": "2026-08-20T10:02:00+00:00",
            },
            {"not": "a record"},
            "{ torn",  # type: ignore[list-item]
        ],
    )
    stats = game_stats(tmp_path)
    assert stats["lag"].numbers == {"attempts": 2.0, "best_mm": 12.0}
    assert stats["lag"].last_played == "Aug 20"
    assert "shots" not in stats  # never played is not the same as played badly


def test_shot_records_are_empty_without_drill_attempts(tmp_path: Path) -> None:
    _journal(tmp_path, "session-20260820-100000.jsonl", [{"kind": "lag_attempt", "game": "lag"}])
    assert shot_records(tmp_path) == {}


# ---------------------------------------------------------------------------
# The diamond grid
# ---------------------------------------------------------------------------


def test_a_cell_is_the_table_divided_by_the_grid() -> None:
    assert (CELL_LENGTH_MM, CELL_WIDTH_MM) == (TABLE_LENGTH_MM / 8, TABLE_WIDTH_MM / 4)
    assert cell_of(0.0, 0.0) == (0, 0)
    assert cell_of(TABLE_LENGTH_MM - 1, TABLE_WIDTH_MM - 1) == (7, 3)
    # Clamped, not refused: a ball against a cushion can read a hair outside
    # the corpus-grade calibration rectangle and is still visibly on the table.
    assert cell_of(-40.0, TABLE_WIDTH_MM + 40.0) == (0, 3)


def test_the_edge_margin_fails_toward_the_player() -> None:
    zone = Zone(col=2, row=1)
    x0, y0, _x1, _y1 = zone.bounds_mm
    inside = zone.contains(x0 + 10.0, y0 + 10.0)
    on_the_line = zone.contains(x0 - CELL_EDGE_MARGIN_MM + 0.5, y0 + 10.0)
    outside = zone.contains(x0 - CELL_EDGE_MARGIN_MM - 20.0, y0 + 10.0)
    assert (inside, on_the_line, outside) == (True, True, False)
    assert zone.miss_mm(x0 + 10.0, y0 + 10.0) == 0.0
    assert zone.miss_mm(x0 - 105.0, y0 + 10.0) == pytest.approx(105.0)


def test_a_zone_grows_toward_the_middle_of_the_table() -> None:
    """Growing outward from a rail target would push half the block off the cloth."""
    near_head = Zone(col=0, row=0).resized(2, 2, anchor=(0, 0))
    assert (near_head.col, near_head.row, near_head.w, near_head.h) == (0, 0, 2, 2)
    near_foot = Zone(col=7, row=3).resized(2, 2, anchor=(7, 3))
    assert (near_foot.col, near_foot.row) == (6, 2)  # grew back toward the centre
    assert near_foot.contains(*_centre(7, 3))  # and still covers the placed cell


def _centre(col: int, row: int) -> tuple[float, float]:
    return ((col + 0.5) * CELL_LENGTH_MM, (row + 0.5) * CELL_WIDTH_MM)


def test_zone_round_trips_through_its_typed_envelope() -> None:
    zone = Zone(col=3, row=2, w=2, h=1)
    assert zone.as_dict() == {"type": "cells", "col": 3, "row": 2, "w": 2, "h": 1}
    assert Zone.from_dict(zone.as_dict()) == zone
    # An unknown type raises rather than degrading: judging a future circle
    # zone as a cell block would score the shot against a target nobody set.
    with pytest.raises(ValueError, match="unknown zone type"):
        Zone.from_dict({"type": "circle", "x": 1, "y": 2})


def test_a_miss_is_captioned_in_cells_not_in_directions() -> None:
    assert cells_out(60.0) == "INSIDE ONE CELL"
    assert cells_out(214.0) == "ONE CELL OUT"
    assert cells_out(520.0) == "2 CELLS OUT"


# ---------------------------------------------------------------------------
# The shot catalog: capture by placing
# ---------------------------------------------------------------------------


def _shots() -> tuple[ShotsGame, list[tuple[str, dict[str, object]]]]:
    lines: list[tuple[str, dict[str, object]]] = []
    game = ShotsGame(GameCtx(log=lambda kind, **detail: lines.append((kind, detail))))
    return game, lines


CUE_SPOT = (600.0, 600.0)
OBJ_SPOT = (1300.0, 260.0)


def _layout(cue: tuple[float, float], obj: tuple[float, float]) -> list[Ball]:
    return [
        Ball(x_mm=cue[0], y_mm=cue[1], is_cue=True, ball_id=1),
        Ball(x_mm=obj[0], y_mm=obj[1], ball_id=2),
    ]


def _capture_a_cue_shot(game: ShotsGame, target: tuple[float, float]) -> None:
    """CAPTURE, pick the cue up and put it at `target`, SAVE."""
    game.press("capture", _layout(CUE_SPOT, OBJ_SPOT))
    game.tick(_layout(target, OBJ_SPOT), moving=False)


def test_capture_attributes_a_placement_by_the_ghost_it_vacated() -> None:
    game, _ = _shots()
    assert game.press("capture", _layout(CUE_SPOT, OBJ_SPOT)) is True
    assert game.transient is True  # the capture page owns the whole deck row
    assert [v.name for v in game.verbs()] == ["cancel", "zone", "_gap1", "_gap2", "save"]
    assert game.verbs()[-1].enabled is False  # SAVE dark until the cue's target exists

    target = _centre(1, 2)
    game.tick(_layout(target, OBJ_SPOT), moving=False)
    assert game._targets["cue"] == Zone(col=1, row=2)
    assert game.verbs()[-1].armed is True
    assert game.verbs()[-1].sub == "SHOT 1"  # the face names what a press writes


def test_a_hand_still_moving_places_nothing() -> None:
    game, _ = _shots()
    game.press("capture", _layout(CUE_SPOT, OBJ_SPOT))
    game.tick(_layout(_centre(1, 2), OBJ_SPOT), moving=True)
    assert game._targets == {}


def test_the_zone_key_sizes_the_most_recently_placed_target() -> None:
    game, _ = _shots()
    _capture_a_cue_shot(game, _centre(1, 2))
    assert game.verbs()[1].text == size_face(1, 1)
    assert game.verbs()[1].sub == size_face(2, 1)  # a cycle key: current over next
    game.press("zone", [])
    assert (game._targets["cue"].w, game._targets["cue"].h) == (2, 1)
    for _ in range(3):
        game.press("zone", [])
    assert (game._targets["cue"].w, game._targets["cue"].h) == (1, 1)  # wrapped


def test_a_resize_survives_the_hand_hovering_over_the_same_cell() -> None:
    """Re-placing in the SAME cell must not re-anchor and undo the size."""
    game, _ = _shots()
    target = _centre(1, 2)
    _capture_a_cue_shot(game, target)
    game.press("zone", [])
    game.tick(_layout((target[0] + 8.0, target[1] - 6.0), OBJ_SPOT), moving=False)
    assert (game._targets["cue"].w, game._targets["cue"].h) == (2, 1)
    # A different cell does re-anchor, back to one cell.
    game.tick(_layout(_centre(5, 1), OBJ_SPOT), moving=False)
    assert game._targets["cue"] == Zone(col=5, row=1)


def test_saving_loads_the_shot_and_hands_the_row_back() -> None:
    game, lines = _shots()
    _capture_a_cue_shot(game, _centre(1, 2))
    assert game.press("save", []) is True
    assert game.transient is False
    assert [s.name for s in game.shots] == ["Shot 1"]
    assert game.state == ShotsGame.SERVING  # saving loads it: no extra press
    assert [v.name for v in game.verbs()] == ["capture", "play", "retry"]
    assert game.verbs()[1].text == "NEXT"  # something is served, so it is NEXT
    assert lines == []  # nothing journaled yet — an attempt is what gets recorded


def test_cancel_discards_and_puts_back_what_was_on_the_wall() -> None:
    game, _ = _shots()
    _capture_a_cue_shot(game, _centre(1, 2))
    game.press("save", [])
    game.press("capture", _layout(CUE_SPOT, OBJ_SPOT))
    game.press("cancel", [])
    assert game.state == ShotsGame.SERVING  # not IDLE: the served shot is still up
    assert len(game.shots) == 1


# ---------------------------------------------------------------------------
# Serving, arming, judging
# ---------------------------------------------------------------------------


def _served(target_cell: tuple[int, int] = (1, 2)) -> ShotsGame:
    game, _ = _shots()
    _capture_a_cue_shot(game, _centre(*target_cell))
    game.press("save", [])
    return game


def test_the_shot_arms_itself_when_the_balls_are_on_the_ghosts_and_still() -> None:
    game = _served()
    game.tick(_layout((CUE_SPOT[0] + 90.0, CUE_SPOT[1]), OBJ_SPOT), moving=False)
    assert game.state == ShotsGame.SERVING
    assert round(game.offsets["cue"]) == 90
    assert game.hud().stats[0].value == "90"

    on_the_ghost = (CUE_SPOT[0] + 6.0, CUE_SPOT[1] - 4.0)
    game.tick(_layout(on_the_ghost, OBJ_SPOT), moving=True)
    assert game.state == ShotsGame.SERVING  # placed, but a hand is still moving
    game.tick(_layout(on_the_ghost, OBJ_SPOT), moving=False)
    assert game.state == ShotsGame.ARMED
    assert game.hud().stats[0].value == "CUE IN ZONE"


def test_the_ghosts_drop_at_arm_and_the_zone_stays_lit() -> None:
    game = _served()
    serving = game.marks()
    assert sum(isinstance(m, Circle) for m in serving) == 2  # both ghosts
    assert sum(isinstance(m, Rect) for m in serving) == 1  # the cue's zone
    game.tick(_layout(CUE_SPOT, OBJ_SPOT), moving=False)
    armed = game.marks()
    assert not any(isinstance(m, Circle) for m in armed)
    assert sum(isinstance(m, Rect) for m in armed) == 1
    assert any(isinstance(m, Grid) for m in armed)


def test_a_pass_is_every_requirement_in_its_zone() -> None:
    game, lines = _shots()
    _capture_a_cue_shot(game, _centre(1, 2))
    game.press("save", [])
    game.tick(_layout(CUE_SPOT, OBJ_SPOT), moving=False)  # arms
    game.on_settle(9.0, _layout(_centre(1, 2), (900.0, 700.0)))
    assert game.passed is True
    assert game.state == ShotsGame.VERDICT
    answer = game.hud().answer
    assert answer is not None and (answer.word, answer.fill) == ("PASS", "pass")
    kinds = [kind for kind, _ in lines]
    assert kinds == ["drill_armed", "drill_attempt"]
    assert lines[-1][1]["misses"] == [{"role": "cue", "miss_mm": 0}]
    assert lines[-1][1]["pass"] is True


def test_a_fail_measures_the_miss_and_draws_the_line() -> None:
    game, lines = _shots()
    _capture_a_cue_shot(game, _centre(1, 2))
    game.press("save", [])
    game.tick(_layout(CUE_SPOT, OBJ_SPOT), moving=False)
    # One whole cell to the left of the target block's left edge.
    x0, y0, _x1, _y1 = Zone(col=1, row=2).bounds_mm
    game.on_settle(9.0, _layout((x0 - 214.0, y0 + 60.0), (900.0, 700.0)))
    assert game.passed is False
    answer = game.hud().answer
    assert answer is not None and (answer.word, answer.fill) == ("FAIL", "fail")
    stat = game.hud().stats[0]
    assert (stat.label, stat.value, stat.unit, stat.sub) == ("CUE", "214", "MM", "ONE CELL OUT")
    (line,) = [m for m in game.marks() if isinstance(m, Line)]
    assert line.role == "miss"
    assert (line.x2_mm, line.y2_mm) == (x0, y0 + 60.0)  # ends on the edge it missed
    # ...and starts at the ball's edge, not its centre, or the first ball
    # radius of it would be drawn underneath the ball.
    assert line.x1_mm == pytest.approx(x0 - 214.0 + BALL_RADIUS_MM)
    assert lines[-1][1]["misses"] == [{"role": "cue", "miss_mm": 214}]


def test_a_scratch_fails_the_cues_requirement() -> None:
    """The cue is not on the table, so it is not in its zone. Nothing special."""
    game, lines = _shots()
    _capture_a_cue_shot(game, _centre(1, 2))
    game.press("save", [])
    game.tick(_layout(CUE_SPOT, OBJ_SPOT), moving=False)
    game.on_settle(9.0, [Ball(x_mm=900.0, y_mm=700.0, ball_id=2)])  # no crown left
    assert game.passed is False
    assert lines[-1][1]["misses"] == [{"role": "cue", "miss_mm": None}]
    assert game.hud().stats[0].sub == "OFF THE TABLE"


def test_the_cue_is_judged_by_the_crown_not_by_lineage() -> None:
    """A mid-shot label swap must not be able to change the cue's verdict."""
    game = _served()
    game.tick(_layout(CUE_SPOT, OBJ_SPOT), moving=False)
    target = _centre(1, 2)
    # The ball wearing the crown at rest carries a track id that was NOT the
    # cue's when the shot armed. The crown wins anyway.
    game.on_settle(
        9.0,
        [
            Ball(x_mm=target[0], y_mm=target[1], is_cue=True, ball_id=77),
            Ball(x_mm=900.0, y_mm=700.0, ball_id=1),
        ],
    )
    assert game.passed is True


def test_retry_reserves_the_same_shot_and_next_serves_again() -> None:
    game = _served()
    game.tick(_layout(CUE_SPOT, OBJ_SPOT), moving=False)
    game.on_settle(9.0, _layout(_centre(1, 2), (900.0, 700.0)))
    shot = game.served
    assert game.press("retry", []) is True
    assert game.served is shot and game.state == ShotsGame.SERVING
    assert game.served.attempts == 1  # the attempt is recorded, not re-run


def test_play_is_dark_with_nothing_saved() -> None:
    game, _ = _shots()
    verbs = {v.name: v for v in game.verbs()}
    assert verbs["play"].enabled is False and verbs["play"].text == "PLAY"
    assert verbs["retry"].enabled is False  # dark until a shot has been served
    assert game.press("play", []) is False


def test_a_miss_smaller_than_a_ball_draws_no_line() -> None:
    """There is no room for one, and the millimetres in the bar say it instead."""
    game = _served()
    game.tick(_layout(CUE_SPOT, OBJ_SPOT), moving=False)
    x0, y0, _x1, _y1 = Zone(col=1, row=2).bounds_mm
    game.on_settle(9.0, _layout((x0 - 20.0, y0 + 60.0), (900.0, 700.0)))
    assert game.passed is False
    assert not [m for m in game.marks() if isinstance(m, Line)]
    assert game.hud().stats[0].value == "20"


# ---------------------------------------------------------------------------
# The catalog: persistence, safeties, and the phone's endpoints
# ---------------------------------------------------------------------------


def _stored(tmp_path: Path, history: dict[str, tuple[int, int, str | None]] | None = None):
    lines: list[tuple[str, dict[str, object]]] = []
    game = ShotsGame(
        GameCtx(
            log=lambda kind, **detail: lines.append((kind, detail)),
            store_dir=tmp_path,
            history=lambda: history or {},
        )
    )
    game.start()
    return game, lines


def test_a_saved_shot_is_one_json_of_definitions(tmp_path: Path) -> None:
    game, _ = _stored(tmp_path)
    _capture_a_cue_shot(game, _centre(1, 2))
    game.press("save", [])
    (path,) = sorted((tmp_path / "shots").glob("*.json"))
    assert path.name == "shot-1.json"
    stored = json.loads(path.read_text())
    assert stored["requirements"] == [
        {"role": "cue", "zone": {"type": "cells", "col": 1, "row": 2, "w": 1, "h": 1}}
    ]
    assert [e["role"] for e in stored["layout"]] == ["cue", "obj1"]
    # Definitions ONLY: attempts live in the journal, so the file cannot drift
    # from what `cueclock report` says about the same shot.
    assert "attempts" not in stored and "made" not in stored


def test_the_catalog_survives_a_restart_and_reads_its_record_back(tmp_path: Path) -> None:
    game, _ = _stored(tmp_path)
    _capture_a_cue_shot(game, _centre(1, 2))
    game.press("save", [])
    reopened, _ = _stored(tmp_path, history={"shot-1": (8, 2, "Aug 18")})
    (shot,) = reopened.shots
    assert (shot.id, shot.name) == ("shot-1", "Shot 1")
    assert (shot.attempts, shot.made, shot.make_pct) == (8, 2, 25)
    assert shot.last_played == "Aug 18"


def test_a_deleted_number_is_reused_by_the_next_capture(tmp_path: Path) -> None:
    """The SAVE key promises a name; the lowest free number is what it can keep."""
    game, _ = _stored(tmp_path)
    for _ in range(2):
        _capture_a_cue_shot(game, _centre(1, 2))
        game.press("save", [])
    assert [s.id for s in game.shots] == ["shot-1", "shot-2"]
    assert game.remove("shot-1") is True
    assert [s.id for s in game.shots] == ["shot-2"]
    game.press("capture", _layout(CUE_SPOT, OBJ_SPOT))
    assert game.verbs()[-1].sub == "SHOT 1"


def test_a_torn_file_does_not_hide_the_rest_of_the_catalog(tmp_path: Path) -> None:
    game, _ = _stored(tmp_path)
    _capture_a_cue_shot(game, _centre(1, 2))
    game.press("save", [])
    (tmp_path / "shots" / "shot-9.json").write_text("{ not json")
    game.reload()
    assert [s.id for s in game.shots] == ["shot-1"]


def test_play_serves_uniformly_and_never_twice_in_a_row(tmp_path: Path) -> None:
    game, _ = _stored(tmp_path)
    for cell in ((1, 2), (5, 1), (3, 0)):
        _capture_a_cue_shot(game, _centre(*cell))
        game.press("save", [])
    served = [game.served.id]
    for _ in range(12):
        game.press("play", [])
        assert game.served is not None
        assert game.served.id != served[-1]  # PLAY twice must not read as a stuck key
        served.append(game.served.id)
    assert len(set(served)) == 3  # and over a dozen presses it reaches all of them


def test_a_single_shot_catalog_serves_that_shot(tmp_path: Path) -> None:
    game, _ = _stored(tmp_path)
    _capture_a_cue_shot(game, _centre(1, 2))
    game.press("save", [])
    assert game.press("play", []) is True
    assert game.served is not None and game.served.id == "shot-1"


def test_a_safety_carries_two_requirements_and_judges_both(tmp_path: Path) -> None:
    """Pick up the cue, place it; pick up the object ball, place it. Two zones."""
    game, lines = _stored(tmp_path)
    game.press("capture", _layout(CUE_SPOT, OBJ_SPOT))
    cue_target, obj_target = _centre(1, 2), _centre(7, 0)
    game.tick(_layout(cue_target, OBJ_SPOT), moving=False)
    # The cue is accounted for against the target it just set, so the object
    # ball coming off its ghost is still unambiguous.
    game.tick(_layout(cue_target, obj_target), moving=False)
    assert set(game._targets) == {"cue", "obj1"}
    game.press("save", [])
    shot = game.served
    assert shot is not None and shot.safety is True
    assert [role for role, _ in shot.requirements] == ["cue", "obj1"]

    game.tick(_layout(CUE_SPOT, OBJ_SPOT), moving=False)  # arms
    game.on_settle(9.0, _layout(cue_target, obj_target))
    assert game.passed is True
    assert lines[-1][1]["misses"] == [
        {"role": "cue", "miss_mm": 0},
        {"role": "obj1", "miss_mm": 0},
    ]
    labels = [s.label for s in game.hud().stats]
    assert labels == ["CUE", "OBJECT"]  # never a colour: no 16-ball identity
    assert game.hud().name.endswith("· Safety")


def test_an_object_ball_potted_during_a_safety_fails_its_own_zone(tmp_path: Path) -> None:
    game, _ = _stored(tmp_path)
    game.press("capture", _layout(CUE_SPOT, OBJ_SPOT))
    cue_target, obj_target = _centre(1, 2), _centre(7, 0)
    game.tick(_layout(cue_target, OBJ_SPOT), moving=False)
    game.tick(_layout(cue_target, obj_target), moving=False)
    game.press("save", [])
    game.tick(_layout(CUE_SPOT, OBJ_SPOT), moving=False)
    game.on_settle(9.0, [Ball(x_mm=cue_target[0], y_mm=cue_target[1], is_cue=True, ball_id=1)])
    assert game.passed is False
    assert game.verdict == [("cue", 0.0), ("obj1", None)]


def test_the_editor_renames_and_rezones_but_never_moves_a_ball(tmp_path: Path) -> None:
    game, _ = _stored(tmp_path)
    _capture_a_cue_shot(game, _centre(1, 2))
    game.press("save", [])
    layout_before = list(game.shots[0].layout)
    moved = Zone(col=5, row=1, w=2, h=2)
    shot = game.edit("shot-1", name="  Thin cut, top-right  ", zones={"cue": moved.as_dict()})
    assert shot is not None
    assert shot.name == "Thin cut, top-right"
    assert shot.requirements == [("cue", Zone(col=5, row=1, w=2, h=2))]
    assert shot.layout == layout_before
    # ...and it is on disk, so it survives the restart the phone never sees.
    reopened, _ = _stored(tmp_path)
    assert reopened.shots[0].name == "Thin cut, top-right"


def test_editing_the_served_shot_keeps_it_served(tmp_path: Path) -> None:
    """Moving a zone is exactly the thing you would want to try immediately."""
    game, _ = _stored(tmp_path)
    _capture_a_cue_shot(game, _centre(1, 2))
    game.press("save", [])
    game.edit("shot-1", name=None, zones={"cue": Zone(col=6, row=0).as_dict()})
    game.reload()
    assert game.served is not None and game.served.id == "shot-1"
    assert game.state == ShotsGame.SERVING
    (zone,) = [m for m in game.marks() if isinstance(m, Rect)]
    assert zone.x_mm == pytest.approx(6 * CELL_LENGTH_MM)


def test_deleting_the_served_shot_leaves_nothing_served(tmp_path: Path) -> None:
    game, _ = _stored(tmp_path)
    _capture_a_cue_shot(game, _centre(1, 2))
    game.press("save", [])
    assert game.remove("shot-1") is True
    assert game.served is None and game.state == ShotsGame.IDLE
    assert {v.name: v.enabled for v in game.verbs()}["play"] is False


def test_the_phone_reads_the_catalog_in_any_mode(tmp_path: Path) -> None:
    with TestClient(create_app(score_path=tmp_path / "score.json")) as client:
        assert client.get("/api/games/shots").json() == {"ok": True, "shots": []}
        assert client.post("/api/games/shots/shot-1/serve").status_code == 409  # not in games
        assert client.patch("/api/games/shots/nope", json={"name": "x"}).status_code == 404
        assert client.delete("/api/games/shots/nope").status_code == 404


def test_an_unknown_zone_type_is_refused_not_degraded(tmp_path: Path) -> None:
    store_dir = tmp_path / "games" / "shots"
    store_dir.mkdir(parents=True)
    (store_dir / "shot-1.json").write_text(
        json.dumps(
            {
                "id": "shot-1",
                "name": "Shot 1",
                "layout": [{"role": "cue", "x_mm": 600.0, "y_mm": 600.0}],
                "requirements": [{"role": "cue", "zone": {"type": "cells", "col": 1, "row": 2}}],
            }
        )
    )
    with TestClient(create_app(score_path=tmp_path / "score.json")) as client:
        assert len(client.get("/api/games/shots").json()["shots"]) == 1
        bad = client.patch(
            "/api/games/shots/shot-1", json={"zones": {"cue": {"type": "circle", "r": 5}}}
        )
        assert bad.status_code == 400
        assert "unknown zone type" in bad.json()["error"]
