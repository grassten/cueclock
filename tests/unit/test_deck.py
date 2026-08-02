"""Deck layout / faces / client / dispatch — the pure parts.

Hardware (HID) paths are exercised live like the camera; nothing here needs a
physical Stream Deck or a running server.
"""

from __future__ import annotations

from cueclock.clock.rulesets import RULESETS
from cueclock.deck import client, faces
from cueclock.deck.daemon import _dispatch, _face_for
from cueclock.deck.layout import (
    COLS,
    LAYOUT,
    ROWS,
    ApiPost,
    Key,
    RulesetCycle,
    SessionToggle,
    next_ruleset,
    ruleset_face,
)
from cueclock.deck.session import SessionState

OFF = SessionState.OFF  # face selection for non-Session keys ignores this

# --- layout ---


def test_layout_positions_unique_and_in_bounds() -> None:
    positions = [(k.row, k.col) for k in LAYOUT]
    assert len(set(positions)) == len(positions)
    for key in LAYOUT:
        assert 1 <= key.row <= ROWS
        assert 1 <= key.col <= COLS


def test_key_index_is_row_major_from_top_left() -> None:
    probe = ApiPost("/x", "", "")
    assert Key(1, 1, "a", "", probe).index == 0
    assert Key(1, 5, "b", "", probe).index == 4
    assert Key(2, 1, "c", "", probe).index == 5
    assert Key(3, 5, "d", "", probe).index == 14


def test_every_ruleset_has_a_short_face() -> None:
    # Guards against adding a ruleset and forgetting the deck; raw names don't fit.
    for name in RULESETS:
        assert ruleset_face(name) != name[:12].upper()


def test_next_ruleset_cycles_in_server_order_and_wraps() -> None:
    names = list(RULESETS)
    seen = [names[0]]
    for _ in names:
        seen.append(next_ruleset(seen[-1], names))
    assert seen == [*names, names[0]]


def test_next_ruleset_unknown_active_falls_back_to_first() -> None:
    assert next_ruleset("garbage", ["a", "b"]) == "a"
    assert next_ruleset("anything", []) == "anything"


# --- match-mode keys (contract 2026-07-31; positions are the plan of record) ---


def test_match_keys_sit_where_the_contract_put_them() -> None:
    spots = {k.name: (k.row, k.col) for k in LAYOUT}
    assert spots["push"] == (1, 4)
    assert spots["rack"] == (3, 3)  # score row: the whole between-racks ritual (2026-08-01)
    assert spots["restart"] == (2, 2)  # beside Reset: the clock-recovery pair (2026-08-01)
    assert spots["p1_plus"] == (3, 1)
    assert spots["p1_minus"] == (3, 2)
    assert spots["p2_minus"] == (3, 4)
    assert spots["p2_plus"] == (3, 5)


def test_match_keys_post_the_match_endpoints() -> None:
    paths = {k.name: k.action.path for k in LAYOUT if isinstance(k.action, ApiPost)}
    assert paths["push"] == "/api/push"
    assert paths["rack"] == "/api/rack"
    assert paths["restart"] == "/api/restart"
    assert paths["p1_plus"] == "/api/score/p1/increment"
    assert paths["p1_minus"] == "/api/score/p1/decrement"
    assert paths["p2_minus"] == "/api/score/p2/decrement"
    assert paths["p2_plus"] == "/api/score/p2/increment"


def test_push_stays_static_across_rulesets() -> None:
    # Push outs exist only in 9-ball — no per-ruleset face/flash treatment.
    push = next(k for k in LAYOUT if k.name == "push")
    assert push.face_for("wnt_9ball") == "PUSH"
    assert isinstance(push.action, ApiPost)
    assert push.action.ok_face_for("wnt_9ball") == "60 s"


# --- ruleset-aware extension key ---


EXT = next(k for k in LAYOUT if k.name == "extension")


def test_extension_sits_top_right() -> None:
    # The one key a player under the clock hunts for (Steven, 2026-08-01).
    assert (EXT.row, EXT.col) == (1, 5)


def test_extension_face_follows_the_active_ruleset() -> None:
    # 15 s for the Ultimate Pool rulesets; 30 s for WNT (WNT Event Rules §5).
    assert EXT.face_for("ultimate_pool_8ball") == "EXT\n+15"
    assert EXT.face_for("ultimate_pool_8ball_final10") == "EXT\n+15"
    assert EXT.face_for("wnt_9ball") == "EXT\n+30"
    assert EXT.face_for(None) == EXT.face  # server down / unpolled -> static default
    assert EXT.face_for("garbage") == EXT.face


def test_extension_flash_matches_the_face() -> None:
    assert isinstance(EXT.action, ApiPost)
    assert EXT.action.ok_face_for("ultimate_pool_8ball") == "+15 s"
    assert EXT.action.ok_face_for("wnt_9ball") == "+30 s"
    assert EXT.action.ok_face_for(None) == EXT.action.ok_face


# --- faces ---


def test_render_is_key_sized_and_draws_something() -> None:
    img = faces.render("EXT\n+15")
    assert img.size == (faces.SIZE, faces.SIZE)
    assert len(img.getcolors(maxcolors=4096) or []) > 1  # not a solid tile


def test_render_empty_is_blank() -> None:
    assert faces.render("").getcolors() == [(faces.SIZE * faces.SIZE, faces.BG)]


def test_flash_and_dim_variants_differ() -> None:
    assert faces.flash_ok("X").getpixel((0, 0)) == faces.BG_OK
    assert faces.flash_err("X").getpixel((0, 0)) == faces.BG_ERR
    assert faces.dimmed("X").tobytes() != faces.render("X").tobytes()


# --- client (server down: refused connection, not a hang) ---


def test_post_when_server_down() -> None:
    result = client.post("http://127.0.0.1:1", "/api/reset", timeout=0.3)
    assert not result.ok
    assert result.status == 0


def test_get_json_when_server_down() -> None:
    assert client.get_json("http://127.0.0.1:1", "/api/state", timeout=0.3) is None


# --- flag family (one Flag became three reason-coded keys, 2026-08-01) ---


FLAGS = tuple(k for k in LAYOUT if k.name.startswith("flag_"))


def test_flag_family_is_a_contiguous_reason_coded_run() -> None:
    # Right half of row 2, in reason order; (2,5) is the old single-Flag cell,
    # kept as a flag key so a pre-split reflex press still saves the ring.
    spots = {k.name: (k.row, k.col) for k in FLAGS}
    assert spots == {
        "flag_phantom": (2, 3),
        "flag_missed": (2, 4),
        "flag_other": (2, 5),
    }


def test_flag_family_sends_distinct_labels_through_the_dump_endpoint() -> None:
    # The label is the whole point: it names the clip dir and rides the
    # journal's flag_dump line, so triage starts from *why* it was pressed.
    paths = {k.name: k.action.path for k in FLAGS if isinstance(k.action, ApiPost)}
    assert paths == {
        "flag_phantom": "/api/dump?label=flag_phantom",
        "flag_missed": "/api/dump?label=flag_missed",
        "flag_other": "/api/dump?label=flag_other",
    }


def test_flag_family_faces_are_instantly_distinguishable() -> None:
    reasons = [k.face.split("\n") for k in FLAGS]
    assert all(first == "FLAG" for first, _ in reasons)  # reads as one family
    assert len({second for _, second in reasons}) == len(FLAGS)


def test_flag_dump_timeouts_cover_the_t7_write() -> None:
    # Dump replies only after writing ~3.6 GB to the T7; a short timeout makes
    # successful saves flash red (2026-07-31 triage: three "failed" dumps, all fine).
    for key in FLAGS:
        assert isinstance(key.action, ApiPost)
        assert key.action.timeout >= 30.0
        assert key.action.pending_face


# --- dispatch (HTTP faked at the client module boundary) ---


FLAG = next(k for k in LAYOUT if k.name == "flag_phantom")
RULESET_KEY = next(k for k in LAYOUT if isinstance(k.action, RulesetCycle))


def test_dispatch_api_ok_flashes_green(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(client, "post", lambda *a, **k: client.PostResult(True, 200, "{}"))
    image, changed = _dispatch(FLAG, "http://x", None)
    assert image.getpixel((0, 0)) == faces.BG_OK
    assert changed is None


def test_dispatch_api_error_flashes_red(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(client, "post", lambda *a, **k: client.PostResult(False, 503, "no T7"))
    image, changed = _dispatch(FLAG, "http://x", None)
    assert image.getpixel((0, 0)) == faces.BG_ERR
    assert changed is None


def test_dispatch_ruleset_cycle_posts_next_and_reports_change(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    names = list(RULESETS)
    posted: list[str] = []

    def fake_post(base: str, path: str, timeout: float = 3.0) -> client.PostResult:
        posted.append(path)
        return client.PostResult(True, 200, "{}")

    monkeypatch.setattr(client, "active_rulesets", lambda base: (names[0], names))
    monkeypatch.setattr(client, "post", fake_post)
    _, changed = _dispatch(RULESET_KEY, "http://x", names[0])
    assert posted == [f"/api/ruleset/{names[1]}"]
    assert changed == names[1]


def test_dispatch_ruleset_cycle_server_down(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(client, "active_rulesets", lambda base: None)
    image, changed = _dispatch(RULESET_KEY, "http://x", None)
    assert image.getpixel((0, 0)) == faces.BG_ERR
    assert changed is None


# --- face selection ---


def test_faces_dim_when_server_down() -> None:
    assert _face_for(FLAG, False, None, OFF).tobytes() == faces.dimmed(FLAG.face).tobytes()
    assert _face_for(FLAG, True, None, OFF).tobytes() == faces.render(FLAG.face).tobytes()


def test_session_key_stays_lit_with_the_server_down_and_shows_its_state() -> None:
    session = next(k for k in LAYOUT if isinstance(k.action, SessionToggle))
    # It is the key that *starts* the server, so dimming it would hide the way
    # back up — it renders its own state, lit, either way.
    for up in (False, True):
        for state in SessionState:
            face = _face_for(session, up, None, state)
            assert face.tobytes() == faces.render(state.face).tobytes()


def test_ruleset_face_tracks_active_when_up() -> None:
    name = next(iter(RULESETS))
    rendered = _face_for(RULESET_KEY, True, name, OFF)
    assert rendered.tobytes() == faces.render(ruleset_face(name)).tobytes()
    dim = _face_for(RULESET_KEY, False, None, OFF)
    assert dim.tobytes() == faces.dimmed(RULESET_KEY.face).tobytes()


def test_extension_face_and_flash_render_the_wnt_values(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    assert _face_for(EXT, True, "wnt_9ball", OFF).tobytes() == faces.render("EXT\n+30").tobytes()
    assert _face_for(EXT, False, None, OFF).tobytes() == faces.dimmed("EXT\n+15").tobytes()
    monkeypatch.setattr(client, "post", lambda *a, **k: client.PostResult(True, 200, "{}"))
    image, changed = _dispatch(EXT, "http://x", "wnt_9ball")
    assert image.tobytes() == faces.flash_ok("+30 s").tobytes()
    assert changed is None
