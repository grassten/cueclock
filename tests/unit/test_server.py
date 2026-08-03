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
            "mode",
            # What a fresh countdown gives you right now. The deck's START key
            # renders it, so the face can't drift from the behaviour.
            "base",
            "extensions_used",
            "extensions_allowed",
            "cue",
            "vision",
            "score",
        }
        assert snap["mode"] == "match"
        assert snap["base"] == 30
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

    A settle in IDLE is a no-op — racking after Set up produces genuine
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


def test_dump_without_label_stays_flagged() -> None:
    # Backward compat: the deck's reason-coded keys always send a label
    # (2026-08-01), but a bare POST — curl, old clients — still means "flagged".
    app = create_app()
    seen: list[str] = []

    def fake_dump(label: str) -> str:
        seen.append(label)
        return f"tests/clips/{label}"

    app.state.dump = fake_dump
    with TestClient(app) as client:
        assert client.post("/api/dump").status_code == 200
    assert seen == ["flagged"]


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
    """Out of a fresh rack, only a break-speed shot starts it (checkpoint 28).

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


def test_idle_break_gate_reads_speed_not_birth() -> None:
    """A break-speed newborn fire starts the rack (checkpoint 57, closing #10).

    Until 2026-08-02 a second IDLE gate ignored every newborn fire regardless
    of speed. Its anchor was two clips read as hard hand-rolled returns at
    3489/3926 mm/s; Steven watched both and **they are 9-ball breaks**, fired
    newborn. The premise "newborn implies not a real shot" is contradicted by
    seven more real newborn strikes in the corpus — the occluded-address and
    own-rest paths are newborn by design — and supported by no rack-setup
    motion anywhere in it.

    A break out of a fully occluded address is exactly that case, and it must
    start the rack. Speed is the discriminator that survived measurement:
    racking and placement lookalikes sit below 1000 mm/s against breaks at
    5845-7361.
    """
    hub = Hub()
    asyncio.run(hub.shot_taken(speed_mms=3926.0, newborn=True))  # occluded break
    assert hub.clock.state is State.MOTION


def test_idle_break_gate_still_eats_slow_newborn_motion() -> None:
    """Removing the newborn half must not weaken the speed floor.

    Rack setup that happens to surface as a newborn track is still rejected —
    on speed, which is the part of the gate that was measured and stays.
    """
    hub = Hub()
    asyncio.run(hub.shot_taken(speed_mms=900.0, newborn=True))  # placement motion
    assert hub.clock.state is State.IDLE
    asyncio.run(hub.shot_taken(speed_mms=5000.0, newborn=False))  # the break
    assert hub.clock.state is State.MOTION
    asyncio.run(hub.balls_settled())
    assert hub.clock.state is State.RUNNING
    asyncio.run(hub.shot_taken(speed_mms=1500.0, newborn=True))  # occluded strike
    assert hub.clock.state is State.MOTION


# ---------- ground truth (#19) ----------


def test_ground_truth_press_never_touches_the_clock() -> None:
    """A press is an observation, not a command.

    The whole design rests on the player pressing one key with no judgement
    attached; if the press moved the clock it would become a decision again.
    """
    hub = Hub()
    hub.ground_truth_press()
    assert hub.clock.state is State.IDLE
    asyncio.run(hub.shot_taken(speed_mms=5000.0))
    assert hub.clock.state is State.MOTION
    hub.ground_truth_press()
    assert hub.clock.state is State.MOTION


def test_ground_truth_press_is_journaled_on_the_capture_clock() -> None:
    """Stamped like a vision event so alignment never crosses clock domains.

    The press has no frame of its own, so the stamp extrapolates from the
    newest processed frame — which is exactly where detect-loop starvation
    would otherwise hide, since emit time and capture time diverge there.
    """
    hub = Hub()
    hub.note_frame(41.5)
    hub.ground_truth_press()
    record = hub.journal.tail(60.0)[-1]
    assert record["kind"] == "ground_truth_press"
    assert isinstance(record["t"], float)
    assert record["t"] >= 41.5
    assert record["frame_t"] == record["t"]


def test_ground_truth_press_without_vision_is_unanchored_not_guessed() -> None:
    """No frames means no capture clock. Record null rather than invent one."""
    hub = Hub()
    hub.ground_truth_press()
    record = hub.journal.tail(60.0)[-1]
    assert record["kind"] == "ground_truth_press"
    assert record["t"] is None


def test_ground_truth_endpoint_records_a_press() -> None:
    hub = Hub()
    app = create_app()
    app.state.hub = hub
    with TestClient(app) as client:
        assert client.post("/api/groundtruth").json() == {"ok": True}


# ---------- session journal upgrades (frame_t + detect heartbeat, 2026-08-01) ----------


def test_vision_events_journal_frame_t() -> None:
    """Vision journal lines carry the confirming frame's capture timestamp.

    `frame_t` names the frame-capture time unambiguously against the
    record's emit-time `mono` stamp; `t` keeps the same value for old
    readers (journal.py contract: additive fields only).
    """
    hub = Hub()
    asyncio.run(hub.shot_taken(3.25, ball_id=7, speed_mms=5000.0))
    asyncio.run(hub.balls_settled(4.5))
    asyncio.run(hub.scratch_seen(5.75))
    by_kind = {r["kind"]: r for r in hub.journal.tail(60.0)}
    assert by_kind["vision_shot_taken"]["frame_t"] == 3.25
    assert by_kind["vision_shot_taken"]["t"] == 3.25  # old readers keep working
    assert by_kind["vision_balls_settled"]["frame_t"] == 4.5
    assert by_kind["vision_scratch"]["frame_t"] == 5.75


def test_detect_heartbeat_reports_hz_and_frame_age() -> None:
    """`detect_heartbeat` journals effective detect Hz + newest-frame age.

    This is what makes detect-loop starvation measurable (BACKLOG: a
    flag-dump write halved the detect rate for ~10 s with no journal
    trace). The first sighting of frames only establishes the baseline;
    every interval after that emits a line.
    """
    import time

    hub = Hub()
    now = time.monotonic()
    assert hub.journal.tail(60.0) == []
    hub._maybe_heartbeat(now)  # no frames yet: silent (bare server stays quiet)
    assert hub.journal.tail(60.0) == []
    hub.note_frame(1.0)
    hub._maybe_heartbeat(now)  # baseline only, no line yet
    assert hub.journal.tail(60.0) == []
    hub.note_frame(2.0)
    hub.note_frame(3.0)
    hub._maybe_heartbeat(now + 1.0)  # under the 5 s cadence: still silent
    assert hub.journal.tail(60.0) == []
    hub._maybe_heartbeat(now + 5.0)
    (beat,) = hub.journal.tail(60.0)
    assert beat["kind"] == "detect_heartbeat"
    assert beat["hz"] == 0.4  # 2 frames over the 5 s interval
    assert beat["frame_t"] == 3.0  # newest processed frame's capture time
    age = beat["frame_age_s"]
    assert isinstance(age, float) and age >= 0.0


# --- session modes (2026-08-02) ---


def test_mode_listing_and_switch_and_unknown() -> None:
    with make_client() as client:
        listing = client.get("/api/modes").json()
        assert listing["active"] == "match"
        assert listing["available"] == ["match", "run_out", "no_clock"]
        assert client.post("/api/mode/run_out").json() == {"ok": True}
        assert client.get("/api/state").json()["mode"] == "run_out"
        assert client.post("/api/mode/no_such_mode").status_code == 404
        assert client.get("/api/state").json()["mode"] == "run_out"  # unchanged


def test_run_out_drops_the_first_shot_allowance_over_the_api() -> None:
    with make_client() as client:
        client.post("/api/ruleset/wnt_9ball")
        assert client.get("/api/state").json()["base"] == 60
        client.post("/api/mode/run_out")
        assert client.get("/api/state").json()["base"] == 30


def test_no_clock_ignores_vision_but_still_journals_it(tmp_path: Path) -> None:
    # A drill still produces evidence: the events are recorded, they just
    # never move the clock. This is what makes a Flag press during one useful.
    journal = tmp_path / "session.jsonl"
    app = create_app(journal_path=journal)
    hub: Hub = app.state.hub
    with TestClient(app) as client:
        client.post("/api/mode/no_clock")
        asyncio.run(hub.shot_taken(t=1.0, speed_mms=4000.0))
        asyncio.run(hub.balls_settled(t=2.0))
        snap = client.get("/api/state").json()
        assert snap["state"] == "IDLE"
        assert snap["display"] == 30
    kinds = [line.split('"kind": "')[1].split('"')[0] for line in journal.read_text().splitlines()]
    assert "vision_shot_taken_no_clock" in kinds
    assert "vision_balls_settled_no_clock" in kinds


def test_last_shot_is_null_until_one_is_seen() -> None:
    with make_client() as client:
        assert client.get("/api/last_shot").json() == {"age_s": None}


def test_last_shot_reports_how_long_ago_the_shot_was() -> None:
    app = create_app()
    hub: Hub = app.state.hub
    with TestClient(app) as client:
        asyncio.run(hub.shot_taken(t=1.0, speed_mms=5000.0))
        age = client.get("/api/last_shot").json()["age_s"]
    assert age is not None
    assert 0.0 <= age < 1.0


def test_the_replay_anchor_is_stamped_before_every_gate() -> None:
    """Replay asks what is on the video, not what moved the clock.

    So the anchor survives the two gates that discard a shot — `no_clock`
    (where drills live, and where a replay is most wanted) and the vision kill
    switch — and the IDLE break floor, which drops racking lookalikes.
    """
    for setup in (
        lambda c: c.post("/api/mode/no_clock"),
        lambda c: c.post("/api/vision/off"),
        lambda c: c.post("/api/reset"),  # IDLE: a sub-floor shot is gated out
    ):
        app = create_app()
        hub: Hub = app.state.hub
        with TestClient(app) as client:
            setup(client)
            assert client.get("/api/last_shot").json()["age_s"] is None
            asyncio.run(hub.shot_taken(t=1.0, speed_mms=800.0))
            assert client.get("/api/last_shot").json()["age_s"] is not None


def test_run_out_lets_a_slow_opener_start_the_clock() -> None:
    # The gate that exists for breaks ate honest ~1000 mm/s drill openers
    # twice (BACKLOG, checkpoint 33) — run_out is the fix, not a lower floor.
    app = create_app()
    hub: Hub = app.state.hub
    with TestClient(app) as client:
        client.post("/api/mode/run_out")
        asyncio.run(hub.shot_taken(t=1.0, speed_mms=1000.0))
        assert client.get("/api/state").json()["state"] == "MOTION"


def test_match_still_eats_a_slow_opener_out_of_idle() -> None:
    app = create_app()
    hub: Hub = app.state.hub
    with TestClient(app) as client:
        asyncio.run(hub.shot_taken(t=1.0, speed_mms=1000.0))
        assert client.get("/api/state").json()["state"] == "IDLE"


def test_run_out_still_ignores_vision_under_the_kill_switch() -> None:
    # Mode and the match-day kill switch are independent gates; neither one
    # being open may reopen the other.
    app = create_app()
    hub: Hub = app.state.hub
    with TestClient(app) as client:
        client.post("/api/mode/run_out")
        client.post("/api/vision/off")
        asyncio.run(hub.shot_taken(t=1.0, speed_mms=4000.0))
        assert client.get("/api/state").json()["state"] == "IDLE"
