"""Deck layout / faces / client / dispatch — the pure parts.

Hardware (HID) paths are exercised live like the camera; nothing here needs a
physical Stream Deck or a running server.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from cueclock.clock import MODES, OPTIONS, Settings
from cueclock.deck import client, faces, live
from cueclock.deck.daemon import _dispatch, _score_post, build_state
from cueclock.deck.layout import (
    CLOCKED,
    COLS,
    LAYOUT,
    MENU,
    MODE_FACES,
    ROWS,
    ApiPost,
    Cycle,
    DeckState,
    Group,
    Key,
    MenuToggle,
    ScoreAdjust,
    SessionToggle,
    SettingCycle,
    cycle_label,
    cycle_next,
    cycle_path,
    page,
    setting_next,
)
from cueclock.deck.session import SessionState

WNT = Settings(after_break="60", extension="30", push="on").as_dict()

# A session's deck, so OBS is answering and the LIVE key (#80) is lit off air.
MATCH = DeckState(
    up=True,
    mode="match",
    settings=WNT,
    base=60,
    extensions_allowed=2,
    live_face=live.off_air_face(),
)
RUN_OUT = replace(MATCH, mode="run_out", settings=Settings().as_dict(), base=30)
NO_CLOCK = replace(MATCH, mode="no_clock")
DOWN = DeckState(up=False)
# The same deck with a bracket loaded (#43). Kept as a separate state rather
# than folded into MATCH, because casual play is the common case and every
# invariant below should keep describing it.
TOURNEY = replace(
    MATCH, bracket_loaded=True, bracket_match_id=4, bracket_match="MATCH 4", bracket_side="A SIDE"
)
ARMED = replace(TOURNEY, bracket_armed=("Priya Shah", 5, 2))

BY_NAME = {key.name: key for key in LAYOUT}
# Keys lit only while the server is down (#49's RECAL). No state with the
# server up ever shows them, so the "every key" sets below leave them out.
OFFLINE_ONLY = {key.name for key in LAYOUT if key.offline_only}
ONLINE = set(BY_NAME) - OFFLINE_ONLY


def visible_names(state: DeckState) -> set[str]:
    return {key.name for key in LAYOUT if key.visible(state)}


# --- grid ---


def test_layout_positions_unique_and_in_bounds() -> None:
    positions = [(k.row, k.col) for k in LAYOUT]
    assert len(set(positions)) == len(positions)
    for key in LAYOUT:
        assert 1 <= key.row <= ROWS
        assert 1 <= key.col <= COLS


def test_key_index_is_row_major_from_top_left() -> None:
    probe = ApiPost("/x", "", "")
    assert Key(1, 1, "a", Group.SYSTEM, probe).index == 0
    assert Key(1, 5, "b", Group.SYSTEM, probe).index == 4
    assert Key(2, 1, "c", Group.SYSTEM, probe).index == 5
    assert Key(3, 5, "d", Group.SYSTEM, probe).index == 14


# --- the spine: top-left 2x2, present in every mode, never moves ---

SPINE = {"session", "mode"}
# With "I shot" retired (#19, 2026-08-05) the spine is the whole of what has no
# mode condition at all, plus LIVE (#80): a stream does not care what the clock
# is doing, so it survives every mode too. It is not spine, because it leaves
# whenever OBS has no stream to offer.
ALWAYS_ON = SPINE | {"live"}


def test_spine_holds_the_top_left_block() -> None:
    # Under it, (2,1) is LIVE (checkpoint 153; Replay until checkpoint 144) and
    # (2,2) has been empty since Flag was removed (checkpoint 114).
    assert {BY_NAME[name].index for name in SPINE} == {0, 1}
    assert BY_NAME["live"].index == 5
    assert 6 not in {key.index for key in LAYOUT}


def test_spine_survives_every_mode() -> None:
    for mode in MODES:
        state = replace(MATCH, mode=mode)
        assert visible_names(state) >= SPINE, mode


def test_only_session_survives_the_server_going_down() -> None:
    # It is the key that *starts* the server, so hiding it would hide the way
    # back up. Everything else goes dark rather than dim (layout v2).
    assert visible_names(DOWN) == {"session"}


def test_recal_is_lit_only_while_the_session_is_at_rest() -> None:
    # #49: the recalibration needs the camera, so the key exists only with the
    # server down *and* no start or stop in progress — never beside a session.
    assert {"recal"} == OFFLINE_ONLY
    assert visible_names(replace(DOWN, session_idle=True)) == {"session", "recal"}
    assert "recal" not in visible_names(replace(MATCH, session_idle=True))


# --- removal, not dimming: the deck shrinks to what the mode affords ---


def test_match_lights_every_key_a_casual_night_has() -> None:
    assert visible_names(MATCH) == ONLINE - {"scene", "confirm"}


def test_a_bracket_lights_the_two_keys_it_adds_and_nothing_else() -> None:
    # The governing constraint of #43, on the deck: with no bracket loaded the
    # surface is exactly the surface it was, and loading one only ever adds.
    assert visible_names(TOURNEY) - visible_names(MATCH) == {"scene", "confirm"}
    assert visible_names(TOURNEY) == ONLINE


def test_run_out_drops_the_rack_only_keys() -> None:
    # No rack: no push out to call and no opponent to score against.
    assert visible_names(MATCH) - visible_names(RUN_OUT) == {"push", "p1", "p2"}


def test_no_clock_collapses_to_the_spine() -> None:
    # Everything that exists to shape a countdown leaves with the countdown.
    assert visible_names(NO_CLOCK) == ALWAYS_ON


def test_clock_off_drops_the_countdown_keys_but_keeps_the_match() -> None:
    # CLOCK off (the freestyle ruleset until #74): no countdown, so Start and
    # Extension go dark with it — but it is still a match, so the score keys
    # and New Rack stay exactly where they are. PUSH follows its own setting.
    untimed = replace(MATCH, settings={**WNT, "clock": "off", "push": "off"})
    assert visible_names(MATCH) - visible_names(untimed) == {"start", "push", "extension"}
    assert {"p1", "p2", "new_rack", "setup"} <= visible_names(untimed)


def test_the_retired_ground_truth_cell_holds_the_bracket_confirm() -> None:
    """(3,5) held "I shot" (#19) until 2026-08-05, then stood empty until #43.

    What the removal established is what this pins: the deck never asks the
    player to *classify* anything. Confirm is the opposite kind of key — it has
    no deadline, it only exists while a bracket does, and what it commits is
    already on the wall in front of everyone before the press.
    """
    at_35 = [k for k in LAYOUT if (k.row, k.col) == (3, 5)]
    assert [k.name for k in at_35] == ["confirm"]
    assert at_35[0].needs_bracket
    assert not at_35[0].visible(MATCH)


def test_push_shows_only_with_its_setting_on() -> None:
    for value in OPTIONS["push"]:
        state = replace(MATCH, settings={**WNT, "push": value})
        assert ("push" in visible_names(state)) is (value == "on"), value


def test_an_unknown_mode_hides_nothing() -> None:
    # A server that predates modes (or a hiccup) must not blank the deck:
    # visibility only removes a key when we positively know it doesn't apply.
    assert visible_names(replace(TOURNEY, mode=None)) == ONLINE


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
    for key in LAYOUT:
        assert key.face(MATCH).stripe == key.group.stripe, key.name


def test_the_clock_group_is_exactly_the_keys_that_leave_with_the_clock() -> None:
    clock_keys = {k.name for k in LAYOUT if k.group is Group.CLOCK}
    # These leave `no_clock` for a different reason — they are rack-scoped, not
    # clock-scoped. Confirm joins them: a drill has no match to report.
    non_clock = {"setup", "new_rack", "p1", "p2", "confirm"}
    quiet = visible_names(replace(TOURNEY, mode="no_clock"))
    assert clock_keys == ONLINE - quiet - non_clock
    for key in LAYOUT:
        if key.group is Group.CLOCK:
            assert key.modes is not None and key.modes <= CLOCKED, key.name


# --- face grammar: commands say the verb, cycles say now over next ---


def test_cycle_faces_put_current_over_next() -> None:
    for key in (BY_NAME["mode"],):
        action = key.action
        assert isinstance(action, Cycle)
        active = MATCH.mode
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


def test_every_mode_has_a_deliberate_face() -> None:
    # Guards against adding one and forgetting the deck. The fallback path
    # (truncate + upper) still renders rather than hiding a key, but a raw
    # name on the tile is not a face anyone chose.
    for name in MODES:
        assert name in MODE_FACES, name


def test_an_unknown_cycle_value_still_renders_something() -> None:
    assert cycle_label("mode", "invented_mode") == "INVENTED_MOD"
    assert cycle_label("mode", None) == "—"


# What each key's second line is for. A sub line is either the value one tap
# away, the gesture that reaches the other thing, or where the press goes when
# the key's own word cannot say — and never context, which is the one thing it
# must not be, because a face with no press behind its second line makes every
# other second line untrustworthy.
SUB_LINE_KEYS = {
    "mode": "value",
    "session": "gesture",
    "live": "gesture",
    "confirm": "gesture",  # only once armed — see below
    "scene": "destination",
}


def test_only_keys_with_a_second_gesture_carry_a_sub_line() -> None:
    for key in LAYOUT:
        if key.name not in SUB_LINE_KEYS:
            assert key.face(ARMED).sub is None, key.name


def test_the_confirm_key_promises_nothing_until_it_is_armed() -> None:
    confirm = BY_NAME["confirm"]
    idle = confirm.face(TOURNEY)
    assert idle.sub is None  # a label, not an offer
    assert idle.text == "MATCH 4\nA SIDE"
    live = confirm.face(ARMED)
    assert live.text == "PRIYA\nWINS 5-2"  # the first name is what fits 72 px
    assert live.sub == "CONFIRM"  # the gesture, named
    assert live.bg == faces.BG_ARMED


def test_the_scene_key_says_where_the_press_goes() -> None:
    scene = BY_NAME["scene"]
    assert (scene.face(TOURNEY).text, scene.face(TOURNEY).sub) == ("BRACKET", "TO TV")
    on = replace(TOURNEY, on_bracket=True)
    assert (scene.face(on).text, scene.face(on).sub) == ("BACK", "TO PLAY")


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


# --- faces ---


def test_every_key_actually_renders_in_every_mode_and_rulebook() -> None:
    """Draw the whole deck, not just inspect the Face data.

    The data-level tests all passed while the daemon crash-looped on launch
    (2026-08-02): Pillow's `textlength` raises on a string containing a
    newline, and `FINAL\\n10` reached the ruleset cycle key's `sub` as the next
    value. Nothing rendered a multi-line sub-line, so nothing caught it.
    """
    for mode in MODES:
        for name, options in OPTIONS.items():
            for value in options:
                for menu in (False, True):
                    state = replace(MATCH, mode=mode, settings={**WNT, name: value}, menu=menu)
                    for key in page(state):
                        image = faces.render(key.face(state))
                        assert image.size == (faces.SIZE, faces.SIZE), (key.name, value)


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
            "settings": WNT,
            "base": 60,
            "extensions_used": 1,
            "extensions_allowed": 2,
            "score": {"p1": {"score": 3}, "p2": {"score": 2}},
        },
        SessionState.ON,
    )
    assert state.up
    assert (state.mode, state.settings, state.base) == ("run_out", WNT, 60)
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


def test_build_state_reads_the_bracket_summary() -> None:
    state = build_state(
        {
            "mode": "match",
            "bracket": {
                "revision": 4,
                "match": {"id": 4, "side": "A", "header": "Match 4 \u00b7 A Side"},
                "armed": {"slot": 0, "name": "Priya Shah", "score": 5, "other": 2},
            },
        },
        SessionState.ON,
    )
    assert state.bracket_loaded
    assert (state.bracket_match_id, state.bracket_match) == (4, "MATCH 4")
    assert state.bracket_side == "A SIDE"
    assert state.bracket_armed == ("Priya Shah", 5, 2)


def test_build_state_with_no_bracket_leaves_both_keys_dark() -> None:
    # Casual play, and every session that never builds a draw.
    state = build_state({"mode": "match", "bracket": None}, SessionState.ON)
    assert not state.bracket_loaded
    assert state.bracket_armed is None
    assert not BY_NAME["confirm"].visible(state)
    assert not BY_NAME["scene"].visible(state)


def test_build_state_survives_a_bracket_with_nothing_loaded() -> None:
    # Between matches: a bracket exists but no match is on the wall, so the
    # confirm key is lit and simply has nothing to name.
    state = build_state(
        {"mode": "match", "bracket": {"revision": 1, "match": None, "armed": None}},
        SessionState.ON,
    )
    assert state.bracket_loaded and state.bracket_match is None
    assert BY_NAME["confirm"].face(state).text == "—"


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


# --- MODE_FACES is the deck's copy of the modes table ---


def test_mode_faces_cover_every_mode() -> None:
    assert set(MODE_FACES) == set(MODES)


def test_no_face_on_the_deck_overflows_its_tile() -> None:
    """Every face this layout can produce actually fits 72x72.

    `faces._fit` falls through to its smallest size rather than failing, so a
    line that is too long is silently *clipped* — which is how "PRESS TO
    CONFIRM" and an eleven-letter first name both got onto a key and came back
    unreadable (#43, caught by rendering the tiles). Nothing else checks this,
    because nothing else renders.
    """
    from PIL import ImageDraw

    states = [
        MATCH,
        RUN_OUT,
        NO_CLOCK,
        DOWN,
        *(replace(MATCH, live_face=face) for face in LIVE_FACES),
        TOURNEY,
        ARMED,
        replace(TOURNEY, on_bracket=True),
        replace(TOURNEY, bracket_side="FINAL", bracket_match="MATCH 15"),
        replace(ARMED, bracket_armed=("Christopher", 11, 10)),
        replace(ARMED, bracket_armed=("Jo", 5, 4)),
    ]
    probe = ImageDraw.Draw(faces.render(faces.Face()))
    for state in states:
        for key in LAYOUT:
            face = key.face(state)
            image = faces.render(face)  # must not raise
            assert image.size == (faces.SIZE, faces.SIZE)
            for text, sizes in (
                (face.text, faces._MAIN_SIZES),
                (face.big, faces._BIG_SIZES),
                (face.sub, faces._SUB_SIZES),
            ):
                if not text:
                    continue
                font = faces._font(sizes[-1])  # what _fit falls through to
                widest = max(probe.textlength(line, font=font) for line in text.split("\n"))
                assert widest <= faces.SIZE - 8, (key.name, state.mode, text)


# --- the Session key's health dot (#52) ---


def test_session_dot_turns_amber_when_the_health_line_warns() -> None:
    healthy = {"health": {"drift": 1.9, "hz": 150.0, "balls": 9, "warn": False}}
    stale = {"health": {"drift": 28.2, "hz": 133.0, "balls": 9, "warn": True}}
    on = SessionState.ON
    assert build_state(healthy, on).session_dot == faces.DOT_OK
    assert build_state(stale, on).session_dot == faces.DOT_WARN
    # A server that predates the field (or a bare one) reads as healthy.
    assert build_state({}, on).session_dot == faces.DOT_OK
    assert build_state({"health": None}, on).session_dot == faces.DOT_OK
    # And the dot reaches the face.
    session = BY_NAME["session"]
    assert session.face(build_state(stale, on)).dot == faces.DOT_WARN


def test_recal_face_comes_from_the_recalibration_and_wears_the_system_stripe() -> None:
    recal = BY_NAME["recal"]
    state = build_state(None, SessionState.OFF, recal_face=faces.Face(text="RECAL", sub="HOLD 2s"))
    assert state.session_idle and recal.visible(state)
    face = recal.face(state)
    assert (face.text, face.sub, face.stripe) == ("RECAL", "HOLD 2s", Group.SYSTEM.stripe)
    # Mid-start the camera is spoken for: the key goes dark.
    assert not recal.visible(build_state(None, SessionState.STARTING))
    assert recal.visible(build_state(None, SessionState.NO_CAMERA))


# --- the LIVE key (#80): OBS's answer on the face, dark without one ---

LIVE_FACES = (
    live.off_air_face(),
    live.off_air_face("public"),
    live.sign_in_face(),
    live.no_youtube_face(),
    live.youtube_failed_face(),
    live.on_air_face(28 * 60_000),
    live.on_air_face(125 * 60_000),
    live.reconnecting_face(),
    live.failed_face(),
    faces.Face("STARTING…"),
    faces.Face("STOPPING…"),
)


def test_live_is_dark_without_an_answer_from_obs() -> None:
    key = BY_NAME["live"]
    assert key.visible(MATCH)
    assert not key.visible(replace(MATCH, live_face=None))
    assert not key.visible(replace(DOWN, live_face=live.off_air_face()))


def test_live_shows_obs_answer_with_the_system_stripe() -> None:
    key = BY_NAME["live"]
    for face in LIVE_FACES:
        shown = key.face(replace(MATCH, live_face=face))
        assert (shown.text, shown.sub, shown.bg) == (face.text, face.sub, face.bg)
        assert shown.stripe == Group.SYSTEM.stripe


def test_on_air_and_a_failed_start_never_read_as_each_other() -> None:
    on_air, failed = live.on_air_face(0), live.failed_face()
    assert on_air.text.startswith("LIVE") and not failed.text.startswith("LIVE")
    assert on_air.bg != failed.bg
    assert live.on_air_face(28 * 60_000 + 59_000).text == "LIVE\n28 MIN"
    # Nor does YouTube refusing (#81): OBS was never started for it.
    refused = live.youtube_failed_face()
    assert not refused.text.startswith("LIVE") and refused.bg != on_air.bg


def test_off_air_the_key_names_the_visibility_the_hold_will_use() -> None:
    assert live.off_air_face().text == "GO LIVE\nUNLISTED"
    assert live.off_air_face("public").text == "GO LIVE\nPUBLIC"
    assert live.off_air_face().sub == "HOLD 1s"  # the gesture is still named
    # Signed out there is no hold to name: the next move is on the phone.
    assert (live.sign_in_face().text, live.sign_in_face().sub) == ("SIGN IN", "ON PHONE")


# --- the SETUP menu (#74): the clock's settings, one key each ---

MENU_BY_NAME = {key.name: key for key in MENU}
OPEN = replace(MATCH, menu=True)


def test_setup_opens_a_page_and_the_same_cell_closes_it() -> None:
    setup = BY_NAME["setup"]
    assert isinstance(setup.action, MenuToggle)
    assert (setup.row, setup.col) == (3, 1)  # the ruleset key's old cell
    assert MENU_BY_NAME["setup"] is setup
    assert setup.face(MATCH).text == "SETUP"
    assert setup.face(OPEN).text == "DONE"  # not BACK: BACK means the TV
    assert page(MATCH) is LAYOUT and page(OPEN) is MENU


def test_the_menu_keeps_session_and_ext_where_they_are() -> None:
    # EXT is the key a player under the clock hunts for; an open menu must
    # never cost it, and the night's start/stop key never moves.
    for name in ("session", "extension"):
        assert MENU_BY_NAME[name] is BY_NAME[name], name
    assert "extension" in {k.name for k in MENU if k.visible(OPEN)}


def test_every_setting_has_one_menu_key() -> None:
    settings = [k.action.setting for k in MENU if isinstance(k.action, SettingCycle)]
    assert sorted(settings) == sorted(OPTIONS)
    positions = [(k.row, k.col) for k in MENU]
    assert len(set(positions)) == len(positions)
    assert all(1 <= k.row <= ROWS and 1 <= k.col <= COLS for k in MENU)


def test_a_setting_key_names_its_setting_over_its_value() -> None:
    shot = MENU_BY_NAME["setting_shot"]
    face = shot.face(replace(OPEN, settings={**WNT, "shot": "15"}))
    assert (face.text, face.big) == ("SHOT", "15")
    assert MENU_BY_NAME["setting_game"].face(OPEN).big == "8-BALL"
    assert MENU_BY_NAME["setting_after_break"].face(OPEN).big == "60"
    assert MENU_BY_NAME["setting_shot"].face(replace(OPEN, settings={})).big == "—"


def test_setting_keys_cycle_in_the_settings_own_order() -> None:
    assert setting_next("game", "8ball") == "9ball"
    assert setting_next("game", "other") == "8ball"  # wraps
    assert setting_next("shot", None) == "30"  # unknown: the first


def test_dispatching_a_setting_posts_it_and_moves_every_face_it_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posted: list[str] = []

    def fake_post(base: str, path: str, timeout: float = 3.0) -> client.PostResult:
        posted.append(path)
        return client.PostResult(True, 200, "{}")

    monkeypatch.setattr(client, "post", fake_post)
    image, moved = _dispatch(MENU_BY_NAME["setting_push"], "http://x", OPEN)
    assert posted == ["/api/setting/push/off"]
    assert moved is not None and moved.settings["push"] == "off"
    assert "push" not in visible_names(replace(moved, menu=False))
    assert image.getpixel((0, 0)) == faces.BG_OK
