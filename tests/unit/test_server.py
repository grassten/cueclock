"""HTTP/WS surface — the Stream Deck contract from docs/design.md §2.1."""

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from cueclock.clock import State
from cueclock.server import create_app
from cueclock.server.hub import Hub
from cueclock.server.score import MatchScore


def make_client() -> TestClient:
    return TestClient(create_app())


def test_state_snapshot_shape() -> None:
    with make_client() as client:
        snap = client.get("/api/state").json()
        assert snap["state"] == "IDLE"
        assert snap["display"] == 30
        assert set(snap) == {
            "state",
            "time",
            "display",
            "ruleset",
            "extensions_used",
            "extensions_allowed",
            "cue",
            "vision",
            "score",
        }
        assert snap["vision"] is True
        assert snap["score"] == {
            "p1": {"name": "PLAYER 1", "score": 0},
            "p2": {"name": "PLAYER 2", "score": 0},
            "race_to": None,
        }


def test_start_pause_reset() -> None:
    with make_client() as client:
        assert client.post("/api/start").json() == {"ok": True}
        assert client.get("/api/state").json()["state"] == "RUNNING"
        client.post("/api/pause")
        assert client.get("/api/state").json()["state"] == "IDLE"
        client.post("/api/reset")
        assert client.get("/api/state").json()["display"] == 30


def test_extension_accepted_then_rejected() -> None:
    with make_client() as client:
        client.post("/api/start")
        assert client.post("/api/extension").json() == {"ok": True, "accepted": True}
        assert client.post("/api/extension").json()["accepted"] is False


def test_ruleset_switch_alias_and_unknown() -> None:
    with make_client() as client:
        listing = client.get("/api/rulesets").json()
        assert listing["active"] == "ultimate_pool_8ball"
        assert "wnt_9ball" in listing["available"]
        assert client.post("/api/ruleset/ultimate_9ball").json() == {"ok": True}  # alias
        assert client.get("/api/state").json()["ruleset"] == "wnt_9ball"
        assert client.post("/api/ruleset/no_such_ruleset").status_code == 404


def test_websocket_snapshot_on_connect() -> None:
    with make_client() as client, client.websocket_connect("/ws") as ws:
        snap = ws.receive_json()
        assert snap["state"] == "IDLE"
        assert snap["display"] == 30
        # The WS payload is the same composed shape as GET /api/state.
        assert snap["vision"] is True
        assert snap["score"]["race_to"] is None


def test_html_pages_served() -> None:
    with make_client() as client:
        page = client.get("/shotclock").text
        assert "clock-square" in page
        # ?view=full (broadcast-style bare number for the full-screen OBS
        # scene) and ?audio=off both live client-side in the one page.
        assert "body.full" in page
        assert "audioEnabled" in page
        assert "scoreboard-container" in client.get("/score").text
        assert "Shot Clock Control" in client.get("/control").text


def test_vision_events_drive_clock() -> None:
    """The `cueclock run` wiring: a shot freezes the clock, its settle restarts it.

    A settle in IDLE is a no-op — racking after New Rack produces genuine
    settles and must not start the countdown; the untimed break enters via
    ShotTaken (IDLE → MOTION) instead.
    """
    hub = Hub()
    asyncio.run(hub.balls_settled())  # racking settles while IDLE — ignored
    assert hub.clock.state is State.IDLE
    asyncio.run(hub.shot_taken())  # the break
    assert hub.clock.state is State.MOTION
    asyncio.run(hub.balls_settled())
    assert hub.clock.state is State.RUNNING
    asyncio.run(hub.shot_taken())
    asyncio.run(hub.balls_settled())
    assert hub.clock.state is State.RUNNING
    assert hub.clock.remaining == float(hub.clock.ruleset.base_seconds)


def test_dump_without_ring_is_503() -> None:
    with make_client() as client:
        assert client.post("/api/dump").status_code == 503


def test_dump_calls_the_wired_ring_and_sanitizes_label() -> None:
    app = create_app()
    seen: list[str] = []

    def fake_dump(label: str) -> str:
        seen.append(label)
        return f"tests/clips/{label}"

    app.state.dump = fake_dump
    with TestClient(app) as client:
        resp = client.post("/api/dump", params={"label": "weird label!!"})
    assert resp.status_code == 200
    assert resp.json() == {"saved": "tests/clips/weirdlabel"}
    assert seen == ["weirdlabel"]


def test_dump_failure_is_503_not_a_crash() -> None:
    app = create_app()

    def empty_ring(label: str) -> str:
        raise RuntimeError("ring buffer is empty")

    app.state.dump = empty_ring
    with TestClient(app) as client:
        assert client.post("/api/dump").status_code == 503


# ---------- match mode (rack / push / score / vision, added 2026-07-31) ----------


def test_rack_restart_and_push_endpoints() -> None:
    with make_client() as client:
        client.post("/api/ruleset/wnt_9ball")
        assert client.post("/api/rack").json() == {"ok": True}
        snap = client.get("/api/state").json()
        assert snap["state"] == "IDLE"
        assert snap["display"] == 60  # WNT §5 first-shot window
        assert client.post("/api/restart").json() == {"ok": True}
        snap = client.get("/api/state").json()
        assert snap["state"] == "RUNNING"
        assert snap["display"] == 60
        assert client.post("/api/push").json() == {"ok": True}
        assert client.get("/api/state").json()["display"] == 60


def test_score_increment_decrement_floor_and_404s() -> None:
    with make_client() as client:
        assert client.get("/api/score").json()["p1"]["score"] == 0
        resp = client.post("/api/score/p1/increment").json()
        assert resp["ok"] is True
        assert resp["score"]["p1"]["score"] == 1
        assert client.post("/api/score/p2/decrement").json()["score"]["p2"]["score"] == 0
        assert client.post("/api/score/p2/decrement").json()["score"]["p2"]["score"] == 0  # floors
        assert client.post("/api/score/p3/increment").status_code == 404
        assert client.post("/api/score/p1/double").status_code == 404


def test_score_config_and_reset() -> None:
    with make_client() as client:
        resp = client.post("/api/score/config", json={"p1_name": "STEVEN", "race_to": 5}).json()
        assert resp["score"]["p1"]["name"] == "STEVEN"
        assert resp["score"]["p2"]["name"] == "PLAYER 2"  # absent keys untouched
        assert resp["score"]["race_to"] == 5
        client.post("/api/score/p1/increment")
        resp = client.post("/api/score/reset").json()
        assert resp["score"]["p1"]["score"] == 0
        assert resp["score"]["p1"]["name"] == "STEVEN"  # names + race survive reset
        assert resp["score"]["race_to"] == 5
        # Explicit null clears the race (hides the overlay center segment).
        resp = client.post("/api/score/config", json={"race_to": None}).json()
        assert resp["score"]["race_to"] is None


def test_vision_toggle_endpoints_and_payload_flag() -> None:
    with make_client() as client:
        assert client.post("/api/vision/off").json() == {"ok": True, "vision": False}
        assert client.get("/api/state").json()["vision"] is False
        assert client.post("/api/vision/on").json() == {"ok": True, "vision": True}
        assert client.get("/api/state").json()["vision"] is True


def test_vision_off_blocks_events_but_not_operator() -> None:
    """The match-day kill switch: vision events can't move the clock while off."""
    hub = Hub()
    asyncio.run(hub.set_vision(False))
    asyncio.run(hub.balls_settled())
    assert hub.clock.state is State.IDLE
    asyncio.run(hub.shot_taken())
    assert hub.clock.state is State.IDLE
    asyncio.run(hub.start())  # operator commands still work
    assert hub.clock.state is State.RUNNING
    asyncio.run(hub.set_vision(True))
    asyncio.run(hub.shot_taken())  # switch back on restores the wiring
    assert hub.clock.state is State.MOTION


def test_score_persistence_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "score.json"
    with TestClient(create_app(score_path=path)) as client:
        client.post("/api/score/p1/increment")
        client.post("/api/score/config", json={"p2_name": "VISITOR", "race_to": 7})
    # A fresh server (mid-match crash restart) reloads the same score.
    with TestClient(create_app(score_path=path)) as client:
        assert client.get("/api/score").json() == {
            "p1": {"name": "PLAYER 1", "score": 1},
            "p2": {"name": "VISITOR", "score": 0},
            "race_to": 7,
        }


def test_score_corrupt_file_falls_back_to_defaults(tmp_path: Path) -> None:
    path = tmp_path / "score.json"
    path.write_text("{not json")
    score = MatchScore(path=path)
    assert score.as_dict()["p1"] == {"name": "PLAYER 1", "score": 0}
    score.adjust("p1", 1)  # next mutation rewrites a clean file
    assert MatchScore(path=path).p1_score == 1


def test_idle_break_gate() -> None:
    """Out of New Rack, only a break-speed shot starts the rack (checkpoint 28).

    The 2026-08-01 rack-gather phantom started the clock 19 s before the real
    break; sub-break impulses in IDLE are racking/placement motion. The gate
    is IDLE-only — mid-rack soft shots pass — and speedless events (manual /
    synthetic) always pass.
    """
    hub = Hub()
    asyncio.run(hub.shot_taken(speed_mms=800.0))  # racking lookalike
    assert hub.clock.state is State.IDLE
    asyncio.run(hub.shot_taken(speed_mms=5000.0))  # the break
    assert hub.clock.state is State.MOTION
    asyncio.run(hub.balls_settled())
    assert hub.clock.state is State.RUNNING
    asyncio.run(hub.shot_taken(speed_mms=800.0))  # ordinary soft shot mid-rack
    assert hub.clock.state is State.MOTION


def test_idle_break_gate_rejects_newborn_fires() -> None:
    """A newborn fire never starts a rack (checkpoint 33).

    Hard bowled returns during rack setup measured 3489 and 3926 mm/s —
    above the speed floor — and each started the rack while Steven was
    still placing balls. Every real break in the corpus fires
    rest-anchored (newborn=False); a bowled ball is always a newborn
    track. Mid-rack (RUNNING) newborn fires still count: the occluded-
    address and own-rest strike paths are newborn by design.
    """
    hub = Hub()
    asyncio.run(hub.shot_taken(speed_mms=3926.0, newborn=True))  # hard bowl
    assert hub.clock.state is State.IDLE
    asyncio.run(hub.shot_taken(speed_mms=5000.0, newborn=False))  # the break
    assert hub.clock.state is State.MOTION
    asyncio.run(hub.balls_settled())
    assert hub.clock.state is State.RUNNING
    asyncio.run(hub.shot_taken(speed_mms=1500.0, newborn=True))  # occluded strike
    assert hub.clock.state is State.MOTION
