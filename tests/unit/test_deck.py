"""Deck layout / faces / client / dispatch — the pure parts.

Hardware (HID) paths are exercised live like the camera; nothing here needs a
physical Stream Deck or a running server.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from cueclock.clock import MODES, RULESETS
from cueclock.deck import client, faces
from cueclock.deck.daemon import _dispatch, _score_post, build_state
from cueclock.deck.layout import (
    CLOCKED,
    COLS,
    LAYOUT,
    MODE_FACES,
    REASON_KEYS,
    ROWS,
    ApiPost,
    Cycle,
    DeckState,
    FlagReason,
    FlagStart,
    Group,
    Key,
    ReplayShow,
    ScoreAdjust,
    SessionToggle,
    cycle_label,
    cycle_next,
    cycle_path,
)
from cueclock.deck.session import SessionState

MATCH = DeckState(up=True, mode="match", ruleset="wnt_9ball", base=60, extensions_allowed=2)
RUN_OUT = replace(MATCH, mode="run_out", ruleset="ultimate_pool_8ball", base=30)
NO_CLOCK = replace(MATCH, mode="no_clock")
DOWN = DeckState(up=False)

BY_NAME = {key.name: key for key in LAYOUT}


def visible_names(state: DeckState) -> set[str]:
    return {key.name for key in LAYOUT if key.visible(state)}


# --- grid ---


def test_layout_positions_unique_and_in_bounds() -> None:
    positions = [(k.row, k.col) for k in (*LAYOUT, *REASON_KEYS)]
    assert len(set(positions)) == len(positions)
    for key in (*LAYOUT, *REASON_KEYS):
        assert 1 <= key.row <= ROWS
        assert 1 <= key.col <= COLS


def test_key_index_is_row_major_from_top_left() -> None:
    probe = ApiPost("/x", "", "")
    assert Key(1, 1, "a", Group.SYSTEM, probe).index == 0
    assert Key(1, 5, "b", Group.SYSTEM, probe).index == 4
    assert Key(2, 1, "c", Group.SYSTEM, probe).index == 5
    assert Key(3, 5, "d", Group.SYSTEM, probe).index == 14


# --- the spine: top-left 2x2, present in every mode, never moves ---

SPINE = {"session", "mode", "replay", "flag"}
# With "I shot" retired (#19, 2026-08-05) the spine is the whole of what has no
# mode condition at all — nothing else survives every mode.
ALWAYS_ON = set(SPINE)


def test_spine_holds_the_top_left_block() -> None:
    assert {BY_NAME[name].index for name in SPINE} == {0, 1, 5, 6}


def test_spine_survives_every_mode() -> None:
    for mode in MODES:
        state = replace(MATCH, mode=mode)
        assert visible_names(state) >= SPINE, mode


def test_only_session_survives_the_server_going_down() -> None:
    # It is the key that *starts* the server, so hiding it would hide the way
    # back up. Everything else goes dark rather than dim (layout v2).
    assert visible_names(DOWN) == {"session"}


# --- removal, not dimming: the deck shrinks to what the mode affords ---


def test_match_lights_every_key() -> None:
    assert visible_names(MATCH) == set(BY_NAME)


def test_run_out_drops_the_rack_only_keys() -> None:
    # No rack: no push out to call and no opponent to score against.
    assert visible_names(MATCH) - visible_names(RUN_OUT) == {"push", "p1", "p2"}


def test_no_clock_collapses_to_the_spine() -> None:
    # Everything that exists to shape a countdown leaves with the countdown.
    assert visible_names(NO_CLOCK) == ALWAYS_ON


def test_the_retired_ground_truth_cell_stays_empty() -> None:
    """(3,5) held "I shot" (#19) until 2026-08-05; nothing took its place.

    Pinned because the accuracy denominator now comes from the journal with
    nothing asked of the table, and a key reappearing there would quietly
    re-introduce the thing that was removed.
    """
    assert all((key.row, key.col) != (3, 5) for key in (*LAYOUT, *REASON_KEYS))


def test_push_is_absent_under_the_eight_ball_rulebooks() -> None:
    # Push outs are a 9-ball rule; removal follows the ruleset axis too.
    for name, ruleset in RULESETS.items():
        state = replace(MATCH, ruleset=ruleset.name)
        assert ("push" in visible_names(state)) is (name == "wnt_9ball"), name


def test_an_unknown_mode_hides_nothing() -> None:
    # A server that predates modes (or a hiccup) must not blank the deck:
    # visibility only removes a key when we positively know it doesn't apply.
    assert visible_names(replace(MATCH, mode=None)) == set(BY_NAME)


def test_keys_never_move_between_modes() -> None:
    # A removed key leaves a hole; nothing reflows, so position is learnable
    # even though the set changes.
    for state in (MATCH, RUN_OUT, NO_CLOCK):
        for key in LAYOUT:
            assert BY_NAME[key.name].index == key.index, state.mode


# --- colour groups ---


def test_group_stripes_are_distinct_and_never_the_outcome_colours() -> None:
    # Green/red are the press-outcome language; a stripe that reused one would
    # read as a flash that never ends.
    stripes = [group.stripe for group in Group]
    assert len(set(stripes)) == len(stripes)
    assert faces.BG_OK not in stripes
    assert faces.BG_ERR not in stripes


def test_every_key_carries_its_group_stripe() -> None:
    for key in (*LAYOUT, *REASON_KEYS):
        assert key.face(MATCH).stripe == key.group.stripe, key.name


def test_the_clock_group_is_exactly_the_keys_that_leave_with_the_clock() -> None:
    clock_keys = {k.name for k in LAYOUT if k.group is Group.CLOCK}
    assert clock_keys == set(BY_NAME) - visible_names(NO_CLOCK) - {"ruleset", "setup", "p1", "p2"}
    for key in LAYOUT:
        if key.group is Group.CLOCK:
            assert key.modes is not None and key.modes <= CLOCKED, key.name


# --- face grammar: commands say the verb, cycles say now over next ---


def test_cycle_faces_put_current_over_next() -> None:
    for key in (BY_NAME["mode"], BY_NAME["ruleset"]):
        action = key.action
        assert isinstance(action, Cycle)
        active = MATCH.mode if action.kind == "mode" else MATCH.ruleset
        face = key.face(MATCH)
        assert face.text == cycle_label(action.kind, active)
        assert face.sub == cycle_label(action.kind, cycle_next(action.kind, active))
        assert face.sub != face.text  # a cycle that showed itself as next is stuck


def test_mode_cycle_runs_match_to_run_out_to_no_clock_and_wraps() -> None:
    seen = ["match"]
    for _ in MODES:
        seen.append(cycle_next("mode", seen[-1]))
    assert seen == ["match", "run_out", "no_clock", "match"]


def test_cycle_next_falls_back_to_the_first_value() -> None:
    assert cycle_next("mode", "garbage") == next(iter(MODES))
    assert cycle_next("ruleset", None) == next(iter(RULESETS))


def test_every_mode_and_ruleset_has_a_deliberate_face() -> None:
    # Guards against adding one and forgetting the deck. The fallback path
    # (truncate + upper) still renders rather than hiding a key, but a raw
    # `ULTIMATE_POO` on the tile is not a face anyone chose.
    for name in MODES:
        assert name in MODE_FACES, name
    for name in RULESETS:
        assert cycle_label("ruleset", name) != name[:12].upper(), name


def test_an_unknown_cycle_value_still_renders_something() -> None:
    assert cycle_label("mode", "invented_mode") == "INVENTED_MOD"
    assert cycle_label("mode", None) == "—"


def test_only_keys_with_a_second_gesture_carry_a_sub_line() -> None:
    # The sub line means "one gesture away" — a tap on a cycle key, a hold on
    # Session and Replay. On a key that does exactly one thing it would be a
    # lie, and the whole face grammar rests on the sub line being trustworthy.
    for key in LAYOUT:
        if not isinstance(key.action, Cycle | SessionToggle | ReplayShow):
            assert key.face(MATCH).sub is None, key.name


def test_session_face_states_where_it_is_and_names_the_way_out() -> None:
    assert SessionState.OFF.face == "START"  # the action, not the state
    assert SessionState.OFF.sub is None
    assert SessionState.ON.face == "ON"
    assert SessionState.ON.sub == "HOLD 1s"  # no tap action, so name the gesture


# --- Start: the key says the countdown it is about to hand you ---

START = BY_NAME["start"]


def test_start_face_shows_the_countdown_a_press_gives() -> None:
    # A plain "Reset" can't say which — the WNT first-shot window makes it 60.
    assert START.face(MATCH).big == "60"
    assert START.face(RUN_OUT).big == "30"
    assert START.face(replace(MATCH, base=15)).big == "15"
    assert START.face(MATCH).text == "START"


def test_start_posts_the_restart_endpoint() -> None:
    assert isinstance(START.action, ApiPost)
    assert START.action.path == "/api/restart"


# --- Extension: pips say the pool before the press, not after ---

EXT = BY_NAME["extension"]


def test_extension_sits_in_the_top_right_corner() -> None:
    # The one key a player under the clock hunts for (Steven, 2026-08-01).
    assert (EXT.row, EXT.col) == (1, 5)


def test_extension_pips_count_what_is_left() -> None:
    assert EXT.face(replace(MATCH, extensions_allowed=2, extensions_used=0)).pips == (2, 2)
    assert EXT.face(replace(MATCH, extensions_allowed=2, extensions_used=1)).pips == (1, 2)
    # Spent: the key stays lit with empty pips rather than going dark, because
    # the pips are the only readout of how many are left.
    assert EXT.face(replace(MATCH, extensions_allowed=2, extensions_used=2)).pips == (0, 2)
    assert "extension" in visible_names(replace(MATCH, extensions_used=2))


def test_extension_pips_never_go_negative_or_render_an_empty_row() -> None:
    assert EXT.face(replace(MATCH, extensions_allowed=1, extensions_used=5)).pips == (0, 1)
    assert EXT.face(replace(MATCH, extensions_allowed=0)).pips is None


# --- score: tap adds, hold takes back ---


def test_score_keys_are_adjacent_and_own_both_directions() -> None:
    p1, p2 = BY_NAME["p1"], BY_NAME["p2"]
    assert p2.index - p1.index == 1
    for key, player in ((p1, "p1"), (p2, "p2")):
        action = key.action
        assert isinstance(action, ScoreAdjust)
        assert action.path(decrement=False) == f"/api/score/{player}/increment"
        assert action.path(decrement=True) == f"/api/score/{player}/decrement"


def test_score_faces_show_the_live_score() -> None:
    face = BY_NAME["p1"].face(replace(MATCH, p1=3))
    assert face.text == "P1"
    assert face.big == "3"


def test_score_hold_is_shorter_than_the_session_hold() -> None:
    # Both are "the rare inverse", but only one kills the camera — taking a
    # point back should not need the deliberate-stop dwell.
    session = BY_NAME["session"].action
    p1 = BY_NAME["p1"].action
    assert isinstance(session, SessionToggle) and isinstance(p1, ScoreAdjust)
    assert p1.hold_s < session.hold_s


# --- flag: one press by feel, reason afterwards ---


def test_flag_page_unfolds_into_empty_cells_beside_the_flag_key() -> None:
    flag = BY_NAME["flag"]
    assert [k.index for k in REASON_KEYS] == [flag.index + 1, flag.index + 2, flag.index + 3]
    # Those cells are dark in every mode, so the page never covers a live key.
    occupied = {k.index for k in LAYOUT}
    assert not occupied & {k.index for k in REASON_KEYS}


def test_flag_reasons_send_distinct_labels_through_the_dump_endpoint() -> None:
    # The label is the whole point: it names the clip dir and rides the
    # journal's flag_dump line, so triage starts from *why* it was pressed.
    labels = {k.name: k.action.label for k in REASON_KEYS if isinstance(k.action, FlagReason)}
    assert labels == {
        "flag_phantom": "flag_phantom",
        "flag_missed": "flag_missed",
        "flag_other": "flag_other",
    }


def test_flag_reason_timeouts_cover_the_dump_write() -> None:
    # A dump replies only after writing ~3.6 GB; a short timeout makes
    # successful saves flash red (2026-07-31 triage: three "failed" dumps, fine).
    for key in REASON_KEYS:
        assert isinstance(key.action, FlagReason)
        assert key.action.timeout >= 30.0


def test_an_unanswered_flag_page_files_itself_as_a_reason_that_exists() -> None:
    action = BY_NAME["flag"].action
    assert isinstance(action, FlagStart)
    assert action.auto_reason in {k.name for k in REASON_KEYS}
    assert 0 < action.auto_s <= 10  # off a 20 s ring, so it has to stay small


# --- faces ---


def test_every_key_actually_renders_in_every_mode_and_rulebook() -> None:
    """Draw the whole deck, not just inspect the Face data.

    The data-level tests all passed while the daemon crash-looped on launch
    (2026-08-02): Pillow's `textlength` raises on a string containing a
    newline, and `FINAL\\n10` reaches the ruleset cycle key's `sub` as the next
    value. Nothing rendered a multi-line sub-line, so nothing caught it.
    """
    for mode in MODES:
        for ruleset in RULESETS:
            for replaying in (False, True):
                state = replace(MATCH, mode=mode, ruleset=ruleset, replaying=replaying)
                for key in (*LAYOUT, *REASON_KEYS):
                    image = faces.render(key.face(state))
                    assert image.size == (faces.SIZE, faces.SIZE), (key.name, mode, ruleset)


def test_render_survives_a_newline_in_every_field() -> None:
    for face in (
        faces.Face("A\nB"),
        faces.Face("A", sub="FINAL\n10"),
        faces.Face("A", big="1\n2"),
        faces.Face("A\nB", sub="C\nD", big="E\nF", stripe=faces.STRIPE_MATCH, pips=(1, 2)),
    ):
        assert faces.render(face).size == (faces.SIZE, faces.SIZE)


def test_render_is_key_sized_and_draws_something() -> None:
    img = faces.render(faces.Face("EXT", pips=(1, 2), stripe=faces.STRIPE_CLOCK))
    assert img.size == (faces.SIZE, faces.SIZE)
    assert len(img.getcolors(maxcolors=4096) or []) > 1  # not a solid tile


def test_blank_is_a_dark_tile() -> None:
    assert faces.blank().getcolors() == [(faces.SIZE * faces.SIZE, faces.BG)]


def test_flash_owns_the_whole_tile_and_carries_no_stripe() -> None:
    # Colour the tile for a group and the outcome flash stops reading.
    assert faces.flash_ok("X").getpixel((0, 0)) == faces.BG_OK
    assert faces.flash_err("X").getpixel((0, 0)) == faces.BG_ERR
    assert faces.flash_ok("X").getpixel((faces.SIZE // 2, faces.SIZE - 2)) == faces.BG_OK


def test_stripe_paints_the_bottom_edge_only() -> None:
    img = faces.render(faces.Face("X", stripe=faces.STRIPE_MATCH))
    assert img.getpixel((faces.SIZE // 2, faces.SIZE - 2)) == faces.STRIPE_MATCH
    assert img.getpixel((faces.SIZE // 2, 2)) == faces.BG


def test_sub_line_renders_dimmer_than_the_main_line() -> None:
    with_sub = faces.render(faces.Face("MATCH", sub="RUN OUT"))
    assert with_sub.tobytes() != faces.render(faces.Face("MATCH")).tobytes()
    assert faces.FG_DIM in {colour for _, colour in (with_sub.getcolors(maxcolors=4096) or [])}


# --- state from the poll ---


def test_build_state_reads_one_payload_into_every_face() -> None:
    state = build_state(
        {
            "mode": "run_out",
            "ruleset": "wnt_9ball",
            "base": 60,
            "extensions_used": 1,
            "extensions_allowed": 2,
            "score": {"p1": {"score": 3}, "p2": {"score": 2}},
        },
        SessionState.ON,
    )
    assert state.up
    assert (state.mode, state.ruleset, state.base) == ("run_out", "wnt_9ball", 60)
    assert (state.extensions_used, state.extensions_allowed) == (1, 2)
    assert (state.p1, state.p2) == (3, 2)
    assert (state.session_face, state.session_sub) == ("ON", "HOLD 1s")


def test_build_state_survives_a_junk_payload() -> None:
    # A control surface never dies because the server answered oddly.
    state = build_state({"mode": 7, "base": "soon", "score": "nope"}, SessionState.ON)
    assert state.up and state.mode is None and state.base == 30
    assert (state.p1, state.p2) == (0, 0)


def test_build_state_with_no_answer_is_the_server_down_deck() -> None:
    state = build_state(None, SessionState.OFF)
    assert not state.up
    assert state.session_face == "START"


# --- client (server down: refused connection, not a hang) ---


def test_post_when_server_down() -> None:
    result = client.post("http://127.0.0.1:1", "/api/reset", timeout=0.3)
    assert not result.ok
    assert result.status == 0


def test_get_json_when_server_down() -> None:
    assert client.get_json("http://127.0.0.1:1", "/api/state", timeout=0.3) is None


# --- dispatch (HTTP faked at the client module boundary) ---


def test_dispatch_api_ok_flashes_green(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "post", lambda *a, **k: client.PostResult(True, 200, "{}"))
    image, moved = _dispatch(START, "http://x", MATCH)
    assert image.getpixel((0, 0)) == faces.BG_OK
    assert moved is None


def test_dispatch_api_error_flashes_red(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "post", lambda *a, **k: client.PostResult(False, 503, "nope"))
    image, moved = _dispatch(START, "http://x", MATCH)
    assert image.getpixel((0, 0)) == faces.BG_ERR
    assert moved is None


def test_dispatch_flag_reason_posts_its_own_label(monkeypatch: pytest.MonkeyPatch) -> None:
    posted: list[str] = []

    def fake_post(base: str, path: str, timeout: float = 3.0) -> client.PostResult:
        posted.append(path)
        return client.PostResult(True, 200, "{}")

    monkeypatch.setattr(client, "post", fake_post)
    missed = next(k for k in REASON_KEYS if k.name == "flag_missed")
    image, moved = _dispatch(missed, "http://x", MATCH)
    assert posted == ["/api/dump?label=flag_missed"]
    assert image.getpixel((0, 0)) == faces.BG_OK
    assert moved is None


def test_dispatch_cycle_posts_next_and_moves_the_state(monkeypatch: pytest.MonkeyPatch) -> None:
    posted: list[str] = []

    def fake_post(base: str, path: str, timeout: float = 3.0) -> client.PostResult:
        posted.append(path)
        return client.PostResult(True, 200, "{}")

    monkeypatch.setattr(client, "get_json", lambda base, path, **k: {"available": list(MODES)})
    monkeypatch.setattr(client, "post", fake_post)
    image, moved = _dispatch(BY_NAME["mode"], "http://x", MATCH)
    assert posted == [cycle_path("mode", "run_out")]
    assert moved is not None and moved.mode == "run_out"
    assert image.getpixel((0, 0)) == faces.BG_OK


def test_dispatch_cycle_moving_mode_takes_keys_off_the_deck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The eagerly-moved state is what the redraw uses, so a cycle into
    # no_clock has to clear the clock keys before the next poll confirms it.
    monkeypatch.setattr(client, "get_json", lambda base, path, **k: {"available": list(MODES)})
    monkeypatch.setattr(client, "post", lambda *a, **k: client.PostResult(True, 200, "{}"))
    _, moved = _dispatch(BY_NAME["mode"], "http://x", replace(MATCH, mode="run_out"))
    assert moved is not None
    assert visible_names(moved) == ALWAYS_ON


def test_dispatch_cycle_server_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "get_json", lambda base, path, **k: None)
    monkeypatch.setattr(client, "post", lambda *a, **k: client.PostResult(False, 0, "down"))
    image, moved = _dispatch(BY_NAME["mode"], "http://x", MATCH)
    assert image.getpixel((0, 0)) == faces.BG_ERR
    assert moved is None


def test_score_post_picks_the_direction(monkeypatch: pytest.MonkeyPatch) -> None:
    posted: list[str] = []

    def fake_post(base: str, path: str, timeout: float = 3.0) -> client.PostResult:
        posted.append(path)
        return client.PostResult(True, 200, "{}")

    monkeypatch.setattr(client, "post", fake_post)
    action = BY_NAME["p1"].action
    assert isinstance(action, ScoreAdjust)
    _score_post(action, "p1", "http://x", decrement=False)
    _score_post(action, "p1", "http://x", decrement=True)
    assert posted == ["/api/score/p1/increment", "/api/score/p1/decrement"]


# --- the Replay key: one key, both directions ---


def test_replay_key_offers_the_verb_that_applies() -> None:
    live = BY_NAME["replay"].face(MATCH)
    mid_replay = BY_NAME["replay"].face(replace(MATCH, replaying=True))
    assert live.text == "REPLAY"
    # The hold is on the face or it does not exist — nothing else on the deck
    # would tell you the key has a second span.
    assert live.sub == "HOLD RACK"
    # "LIVE" alone would be a state on a key read as a command (rule 3), so the
    # face names the destination on its second line instead.
    assert mid_replay.text == "BACK"
    assert mid_replay.sub == "TO LIVE"


def test_replay_hold_matches_the_other_deck_holds() -> None:
    # One dwell to learn: the rare span sits behind the same hold as the rare
    # score direction, and both are shorter than the session's deliberate stop.
    replay_key, p1, session = (BY_NAME[n].action for n in ("replay", "p1", "session"))
    assert isinstance(replay_key, ReplayShow) and isinstance(p1, ScoreAdjust)
    assert isinstance(session, SessionToggle)
    assert replay_key.hold_s == p1.hold_s < session.hold_s


def test_replay_key_stays_on_the_spine_mid_replay() -> None:
    for mode in MODES:
        state = replace(MATCH, mode=mode, replaying=True)
        assert BY_NAME["replay"].visible(state), mode


# --- MODE_FACES is the deck's copy of the modes table ---


def test_mode_faces_cover_every_mode() -> None:
    assert set(MODE_FACES) == set(MODES)
