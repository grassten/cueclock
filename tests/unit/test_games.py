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
from cueclock.display.marks import Bracket
from cueclock.server import create_app
from cueclock.server.games import (
    BALL_RADIUS_MM,
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
