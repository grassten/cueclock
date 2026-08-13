"""HTTP/WS surface — the Stream Deck contract from docs/design.md §2.1."""

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import httpx
import pytest
from fastapi import WebSocket
from fastapi.testclient import TestClient

import cueclock.server.app as server_app
import cueclock.server.hub as hub_module
from cueclock.clock import State
from cueclock.server import create_app, fargo
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
            # The solo panel (#30): null in match, pace numbers in run_out,
            # rep counters in no_clock.
            "solo",
            # The bracket (#43): null in casual play, which is every session
            # that doesn't build one.
            "bracket",
        }
        assert snap["mode"] == "match"
        assert snap["base"] == 30
        assert snap["vision"] is True
        assert snap["solo"] is None
        assert snap["bracket"] is None
        assert snap["score"] == {
            "p1": {"name": "PLAYER 1", "score": 0, "fargo": None, "target": None},
            "p2": {"name": "PLAYER 2", "score": 0, "fargo": None, "target": None},
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
        assert client.post("/api/extension").json()["accepted"] is True  # pool of 2
        assert client.post("/api/extension").json()["accepted"] is False


class RecordingWS:
    """Minimal WebSocket stand-in: keeps every payload the hub broadcasts."""

    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    async def accept(self) -> None:
        pass

    async def send_text(self, text: str) -> None:
        self.messages.append(json.loads(text))


def test_extension_cues_the_overlay_to_announce_it() -> None:
    """Granted extensions cue the spoken call; refused ones cue their own tone.

    The cue rides the broadcast, not the caller, so the deck key and the voice
    announce identically — a ref calls the extension, not the microphone. A
    refusal has to be audible too: silence is indistinguishable from a mic that
    never heard the shout (docs/hardware.md Part D).
    """
    hub = Hub()
    ws = RecordingWS()
    asyncio.run(hub.connect(cast(WebSocket, ws)))
    asyncio.run(hub.start())
    assert asyncio.run(hub.extension()) is True
    assert ws.messages[-1]["cue"] == "extension"
    asyncio.run(hub.extension())  # second of the rack's two
    assert asyncio.run(hub.extension()) is False  # pool spent
    assert ws.messages[-1]["cue"] == "extension_denied"


def test_only_a_granted_extension_deafens_the_mic() -> None:
    """The mute hook follows the announcement, and a refusal announces no words.

    `extension_denied` is a pair of tones with no keyword in them, so muting
    there would deafen the listener for nothing.
    """
    hub = Hub()
    muted: list[int] = []
    hub.on_extension_granted = lambda: muted.append(1)
    asyncio.run(hub.start())
    asyncio.run(hub.extension())
    assert len(muted) == 1
    asyncio.run(hub.extension())  # second grant of the rack: announced, so muted again
    assert len(muted) == 2
    asyncio.run(hub.extension())  # refused — a denial speaks no words to hear
    assert len(muted) == 2


def test_voice_asset_is_served_when_generated_and_404s_when_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Gitignored and macOS-generated, so "absent" is a normal state: CI and a
    # fresh clone both hit the 404 and fall back to the overlay's chime.
    monkeypatch.setattr(server_app, "VOICE_DIR", tmp_path)
    with make_client() as client:
        assert client.get("/audio/extension_called.wav").status_code == 404
        (tmp_path / "extension_called.wav").write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
        response = client.get("/audio/extension_called.wav")
        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/wav"


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
        # scene), ?view=bar (the live scene's bottom bar) and ?audio=off all
        # live client-side in the one page.
        assert "body.full" in page
        assert "body.bar .plinth" in page
        assert "audioEnabled" in page
        # The scoreboard is part of that bar since 2026-08-03 (#23) — the
        # drain runs the whole width of it, crossing from the score area into
        # the clock cell, and no overlay can span two OBS sources. So `/score`
        # is gone rather than deprecated: a route still answering would let a
        # stale OBS source look like it still worked.
        assert "bar-p1-score" in page and "bar-race" in page
        assert client.get("/score").status_code == 404
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
        client.post("/api/score/config", json={"p2_fargo": 471})
    # A fresh server (mid-match crash restart) reloads the same score.
    with TestClient(create_app(score_path=path)) as client:
        assert client.get("/api/score").json() == {
            "p1": {"name": "PLAYER 1", "score": 1, "fargo": None, "target": None},
            "p2": {"name": "VISITOR", "score": 0, "fargo": 471, "target": None},
            "race_to": 7,
        }


def test_score_corrupt_file_falls_back_to_defaults(tmp_path: Path) -> None:
    path = tmp_path / "score.json"
    path.write_text("{not json")
    score = MatchScore(path=path)
    assert score.as_dict()["p1"] == {"name": "PLAYER 1", "score": 0, "fargo": None, "target": None}
    score.adjust("p1", 1)  # next mutation rewrites a clean file
    assert MatchScore(path=path).p1_score == 1


def test_score_fargo_set_and_cleared_by_rename() -> None:
    """A rating belongs to a name (issue #27): renaming clears it."""
    with make_client() as client:
        client.post("/api/score/config", json={"p1_name": "STEVEN"})
        resp = client.post("/api/score/config", json={"p1_fargo": 471}).json()
        assert resp["score"]["p1"]["fargo"] == 471
        # Same name again — the rating stays put.
        resp = client.post("/api/score/config", json={"p1_name": "STEVEN"}).json()
        assert resp["score"]["p1"]["fargo"] == 471
        # Scores reset keeps the rating (next match, same players).
        assert client.post("/api/score/reset").json()["score"]["p1"]["fargo"] == 471
        # A different name means a different person: stale rating dropped.
        resp = client.post("/api/score/config", json={"p1_name": "HANK"}).json()
        assert resp["score"]["p1"]["fargo"] is None
        # Name + rating in one update is the pick flow — both land.
        resp = client.post("/api/score/config", json={"p2_name": "SHANE", "p2_fargo": 847}).json()
        assert resp["score"]["p2"] == {"name": "SHANE", "score": 0, "fargo": 847, "target": None}
        # Explicit null clears (the "not established" case).
        resp = client.post("/api/score/config", json={"p2_fargo": None}).json()
        assert resp["score"]["p2"]["fargo"] is None


def test_fargo_search_normalizes_upstream_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """The proxy flattens FargoRate's rows to what the pick-list renders."""

    async def fake_fetch(q: str) -> dict[str, object]:
        assert q == "John Smith"
        return {
            "value": [
                {
                    "firstName": "John",
                    "lastName": "Smith",
                    "location": "Des Moines IA",
                    "effectiveRating": "404",
                    "robustness": "1318",
                },
                # Provisional: under 200 recorded games.
                {
                    "firstName": "JOHNATHON",
                    "lastName": "SMITH",
                    "location": "OK",
                    "effectiveRating": "365",
                    "robustness": "4",
                },
                # Unparseable rating: nothing to display, row dropped.
                {"firstName": "J", "lastName": "S", "effectiveRating": None},
            ]
        }

    monkeypatch.setattr(fargo, "_fetch", fake_fetch)
    with make_client() as client:
        resp = client.get("/api/fargo/search", params={"q": "John Smith"}).json()
    assert resp["ok"] is True
    assert resp["players"] == [
        {"name": "John Smith", "location": "Des Moines IA", "rating": 404, "established": True},
        {"name": "JOHNATHON SMITH", "location": "OK", "rating": 365, "established": False},
    ]


def test_fargo_search_upstream_failure_is_502(monkeypatch: pytest.MonkeyPatch) -> None:
    """FargoRate down must degrade to "no rating", never break the page."""

    async def fake_fetch(q: str) -> dict[str, object]:
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(fargo, "_fetch", fake_fetch)
    with make_client() as client:
        resp = client.get("/api/fargo/search", params={"q": "Steven Asten"})
    assert resp.status_code == 502
    assert resp.json()["ok"] is False


def test_fargo_search_empty_query_is_400() -> None:
    with make_client() as client:
        assert client.get("/api/fargo/search", params={"q": "  "}).status_code == 400
        assert client.get("/api/fargo/search").status_code == 400


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


def test_idle_break_gate_passes_the_2996_break() -> None:
    """The floor must not eat a normal break by 4 mm/s (checkpoint 64).

    flag_missed, 2026-08-03 17:37:23Z: Steven pressed New Rack, broke, and the
    gate ignored the shot at **2996 mm/s** against a 3000 floor. Ground truth
    is his: a normal-power break. The clock sat IDLE through it and he
    recovered by switching to run_out and pressing Restart — the table-side
    intervention the old floor's "the Start key recovers" trade-off waved off.

    2500 is anchored between the two ground-truth-labeled journal populations
    (see MATCH_BREAK_FLOOR_MMS): the confirmed break at 2996 and the worst
    labeled racking impulse at 2072. Both edges are pinned here, so neither
    can drift back over the other.
    """
    hub = Hub()
    asyncio.run(hub.shot_taken(speed_mms=2996.0))  # the break the gate ate
    assert hub.clock.state is State.MOTION

    hub = Hub()
    asyncio.run(hub.shot_taken(speed_mms=2072.0))  # journal-pinned racking phantom
    assert hub.clock.state is State.IDLE


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


# ---------- ground truth (#19, retired 2026-08-05) ----------


def test_ground_truth_endpoint_is_gone() -> None:
    """The "I shot" key is removed; its endpoint must fail visibly.

    Same rule as `/score` and `/api/last_shot` (checkpoints 61, 65): a deck
    daemon left on an old build must break loudly, never keep looking like it
    works. The denominator now comes from the clock's own missed-strike
    compensation, which asks nothing of the table (`server/report.py`).
    """
    app = create_app()
    app.state.hub = Hub()
    with TestClient(app) as client:
        assert client.post("/api/groundtruth").status_code == 404


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


def test_scratch_adjudication_journals_its_evidence() -> None:
    """The two-stage verdict's evidence records (#26) are journal-auditable.

    A candidate carries its death spot, pocket and the dying crown's color
    forensics; a refutation carries the witness's position and reading — so
    any live verdict can be spot-checked against footage without a dump.
    """
    hub = Hub()
    asyncio.run(
        hub.scratch_candidate(
            6.0,
            x_mm=57.2,
            y_mm=-12.9,
            pocket_x_mm=0.0,
            pocket_y_mm=0.0,
            whiteness=0.874,
            color_age_s=1.23,
        )
    )
    asyncio.run(hub.scratch_refuted(8.5, witness_id=4, x_mm=1204.6, y_mm=498.2, whiteness=0.912))
    by_kind = {r["kind"]: r for r in hub.journal.tail(60.0)}
    candidate = by_kind["vision_scratch_candidate"]
    assert candidate["frame_t"] == 6.0
    assert (candidate["x_mm"], candidate["y_mm"]) == (57, -13)
    assert (candidate["pocket_x_mm"], candidate["pocket_y_mm"]) == (0, 0)
    assert candidate["whiteness"] == 0.874
    assert candidate["color_age_s"] == 1.23
    refuted = by_kind["vision_scratch_refuted"]
    assert refuted["frame_t"] == 8.5
    assert refuted["witness_id"] == 4
    assert (refuted["x_mm"], refuted["y_mm"]) == (1205, 498)
    assert refuted["whiteness"] == 0.912


# ---------- post-scratch grace (#25, checkpoint 78) ----------


def _candidate(hub: Hub, t: float) -> None:
    asyncio.run(
        hub.scratch_candidate(
            t,
            x_mm=60.0,
            y_mm=80.0,
            pocket_x_mm=0.0,
            pocket_y_mm=0.0,
            whiteness=0.5,
            color_age_s=0.0,
        )
    )


def _shot_and_settle(hub: Hub) -> None:
    hub.clock.start(time.monotonic())
    asyncio.run(hub.shot_taken(3.0))
    asyncio.run(hub.balls_settled(4.0))
    assert hub.clock.state is State.RUNNING


def test_scratch_candidate_holds_the_fresh_countdown() -> None:
    """#25: an un-refuted candidate freezes the countdown the settle already
    started (scratch_shot_0801's order: settle first, candidate 0.6 s later);
    a refutation releases it. Both actions journal."""
    hub = Hub()
    _shot_and_settle(hub)
    _candidate(hub, 5.0)
    assert hub.clock.holding(time.monotonic())
    by_kind = {r["kind"]: r for r in hub.journal.tail(60.0)}
    assert by_kind["scratch_hold"]["hold_s"] == 5.0
    asyncio.run(hub.scratch_refuted(6.0, witness_id=1, x_mm=0.0, y_mm=0.0, whiteness=0.9))
    assert not hub.clock.holding(time.monotonic())
    assert "scratch_hold_released" in {r["kind"] for r in hub.journal.tail(60.0)}


def test_scratch_candidate_before_settle_holds_at_the_settle() -> None:
    """shot_debug's order: the candidate lands during MOTION (balls still
    rolling), so the settle that follows starts its countdown already held."""
    hub = Hub()
    hub.clock.start(time.monotonic())
    asyncio.run(hub.shot_taken(3.0))
    _candidate(hub, 4.0)
    assert hub.clock.state is State.MOTION
    assert not hub.clock.holding(time.monotonic())  # nothing running to hold
    asyncio.run(hub.balls_settled(6.0))
    assert hub.clock.holding(time.monotonic())


def test_scratch_hold_is_granted_once_per_candidate() -> None:
    """A candidate that outlives a whole shot cycle (blocked census riding
    toward the vision core's 30 s horizon) must not also hold the next
    shot's clock: one grant, at the first opportunity."""
    hub = Hub()
    _shot_and_settle(hub)
    _candidate(hub, 5.0)
    hub.clock.release_hold(time.monotonic())  # the 5 s elapse
    asyncio.run(hub.shot_taken(8.0))
    asyncio.run(hub.balls_settled(12.0))  # candidate still unresolved
    assert not hub.clock.holding(time.monotonic())
    kinds = [r["kind"] for r in hub.journal.tail(60.0)]
    assert kinds.count("scratch_hold") == 1


def test_confirmed_scratch_keeps_the_hold() -> None:
    """The verdict closes the candidate without touching the countdown — by
    census-completion time the retrieval the hold covers is underway."""
    hub = Hub()
    _shot_and_settle(hub)
    _candidate(hub, 5.0)
    asyncio.run(hub.scratch_seen(5.5))
    assert hub.clock.holding(time.monotonic())


def test_scratch_hold_respects_the_kill_switch() -> None:
    """The hold is gated exactly like the events that move the clock: with
    vision killed the candidate stays journaled evidence, nothing more."""
    hub = Hub()
    _shot_and_settle(hub)
    hub.vision_enabled = False
    _candidate(hub, 5.0)
    assert not hub.clock.holding(time.monotonic())
    assert "vision_scratch_candidate" in {r["kind"] for r in hub.journal.tail(60.0)}


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


def test_both_replay_anchors_are_null_until_something_happens() -> None:
    with make_client() as client:
        assert client.get("/api/replay_anchors").json() == {
            "shot_age_s": None,
            "rack_age_s": None,
        }


def test_last_shot_reports_how_long_ago_the_shot_was() -> None:
    app = create_app()
    hub: Hub = app.state.hub
    with TestClient(app) as client:
        asyncio.run(hub.shot_taken(t=1.0, speed_mms=5000.0))
        age = client.get("/api/replay_anchors").json()["shot_age_s"]
    assert age is not None
    assert 0.0 <= age < 1.0


def test_the_rack_anchor_is_the_break_and_stays_there_for_the_rack() -> None:
    """A rack replay opens on the break, not on a minute of racking."""
    app = create_app()
    hub: Hub = app.state.hub
    with TestClient(app) as client:
        client.post("/api/rack")
        assert client.get("/api/replay_anchors").json()["rack_age_s"] is None  # nothing yet

        asyncio.run(hub.shot_taken(t=1.0, speed_mms=5000.0))  # the break
        hub._rack_break_mono -= 60.0
        assert client.get("/api/replay_anchors").json()["rack_age_s"] > 59.0

        asyncio.run(hub.shot_taken(t=2.0, speed_mms=3000.0))  # the next shot
        assert client.get("/api/replay_anchors").json()["rack_age_s"] > 59.0  # still the break


def test_setting_up_keeps_the_rack_you_just_played_until_the_next_break() -> None:
    """The live failure of 2026-08-05, which is the common press.

    You run out, you press New Rack, you hold Replay to watch how you did it.
    Retiring the anchor on the press cued 595.1 s into a 598.5 s file and put
    3.4 s of the empty table you had just cleared on the wall.
    """
    app = create_app()
    hub: Hub = app.state.hub
    with TestClient(app) as client:
        asyncio.run(hub.shot_taken(t=1.0, speed_mms=5000.0))  # the break
        hub._rack_break_mono -= 300.0  # five minutes of rack
        assert client.get("/api/replay_anchors").json()["rack_age_s"] > 299.0

        client.post("/api/rack")  # ran out, racked up
        assert client.get("/api/replay_anchors").json()["rack_age_s"] > 299.0

        asyncio.run(hub.shot_taken(t=2.0, speed_mms=5000.0))  # the new break retires it
        assert client.get("/api/replay_anchors").json()["rack_age_s"] < 1.0


def test_the_rack_anchor_survives_the_gates_the_shot_anchor_does() -> None:
    # Same reasoning as the shot anchor: a drill in `no_clock` has no New Rack
    # key, so its first shot is what "this rack" can mean out there.
    app = create_app()
    hub: Hub = app.state.hub
    with TestClient(app) as client:
        client.post("/api/mode/no_clock")
        asyncio.run(hub.shot_taken(t=1.0, speed_mms=5000.0))
        assert client.get("/api/replay_anchors").json()["rack_age_s"] is not None


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
            assert client.get("/api/replay_anchors").json()["shot_age_s"] is None
            asyncio.run(hub.shot_taken(t=1.0, speed_mms=800.0))
            assert client.get("/api/replay_anchors").json()["shot_age_s"] is not None


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


# ---------- E8 break adjudication + E9 hub guards (2026-08-06, plan §13.2/§13.3) ----------


def _summary(
    moved: int = 0,
    appeared: int = 0,
    vanished: int = 0,
    moved_mm: float = 0.0,
    baseline_balls: int | None = None,
):
    # `baseline_balls` defaults to None — the unanchored reading — so every
    # test written before #37 keeps describing a settle whose zero could be
    # arithmetic, which is the shape the hub must still fail open on.
    from cueclock.vision.pipeline import RearrangeSummary

    return RearrangeSummary(
        moved=moved,
        appeared=appeared,
        vanished=vanished,
        moved_mm=moved_mm,
        baseline_balls=baseline_balls,
    )


def test_break_adjudicated_from_eaten_fire_and_break_scale_settle() -> None:
    """E8's fired tier: the exact shape of today's morning break (dark_flight).

    The fire read 443 mm/s — in-flight tracks under-read 2-4x — so the IDLE
    floor ate it and the clock sat through the break. The settle 3.8 s later
    rearranged 12 balls (probe-measured: moved 1 / appeared 11, 381 mm).
    Fire at any speed + break-scale settle = the break, counted at the rest,
    which is when WNT §5 starts the 60 s window anyway.
    """
    hub = Hub()
    asyncio.run(hub.new_rack())
    asyncio.run(hub.shot_taken(speed_mms=443.0))  # eaten by the floor
    assert hub.clock.state is State.IDLE
    asyncio.run(hub.balls_settled(rearrange=_summary(moved=1, appeared=11, moved_mm=381.0)))
    assert hub.clock.state is State.RUNNING
    assert hub.clock.rack_shots == 1  # the break is counted, not confessed
    kinds = [r["kind"] for r in hub.journal.tail(60.0)]
    assert "break_adjudicated" in kinds


def test_small_settle_after_eaten_fire_stays_idle() -> None:
    # A placement settle (1-2 balls) after racking noise: racking must never
    # start the clock (checkpoint 28's phantom, the named E8 hazard).
    hub = Hub()
    asyncio.run(hub.new_rack())
    asyncio.run(hub.shot_taken(speed_mms=1471.0))  # rack-gather roll, eaten
    asyncio.run(hub.balls_settled(rearrange=_summary(moved=2, moved_mm=190.0)))
    assert hub.clock.state is State.IDLE
    assert hub.clock.rack_shots == 0


def test_dark_break_scale_settle_counts_the_break() -> None:
    """The no-fire tier counts the break off the pack un-merging alone (#36).

    History, because the inversion matters: this test pinned the opposite
    until 2026-08-11. The tier shipped shadow-only on the belief that its
    hazard — a racking settle looking appear-heavy exactly like a break —
    was unmeasurable offline, and #33 Part C then "confirmed" the blocker by
    reading a settle's identity off press timing rather than off the physics
    in its dump. The settle was the missed break. With the record re-mined,
    every racking settle measures appeared=0 (racking MERGES the pack) and
    every break measures >= 5, so the tier acts.
    """
    hub = Hub()
    asyncio.run(hub.new_rack())
    asyncio.run(hub.balls_settled(rearrange=_summary(moved=1, appeared=11, moved_mm=381.0)))
    assert hub.clock.state is State.RUNNING
    assert hub.clock.rack_shots == 1  # counted exactly once
    # ...and the settle started the WNT §5 first-shot window over it.
    assert hub.clock.current_base() == hub.clock.ruleset.first_shot_seconds
    assert hub.clock.remaining == pytest.approx(hub.clock.current_base(), abs=0.2)
    tiers = [r.get("tier") for r in hub.journal.tail(60.0) if r["kind"] == "break_adjudicated"]
    assert tiers == ["dark"]


def test_dark_tier_ignores_a_racking_scale_settle() -> None:
    # The 2026-08-01 racking-countdown hazard, in its measured shape: racking
    # merges singles into the pack, so it lands in `vanished` and reads
    # appeared=0 no matter how far the balls travelled (live gathers measured
    # up to 2278 mm). Nothing may start the clock here.
    hub = Hub()
    asyncio.run(hub.new_rack())
    asyncio.run(hub.balls_settled(rearrange=_summary(moved=0, vanished=6, moved_mm=2278.0)))
    assert hub.clock.state is State.IDLE
    assert hub.clock.rack_shots == 0
    assert "break_adjudicated" not in [r["kind"] for r in hub.journal.tail(60.0)]


def test_dark_tier_floor_is_the_appeared_count_not_involved() -> None:
    # Two pins in one, both from the 2026-08-11 measurement. A big `involved`
    # with appeared under the floor is not a break (the old involved>=8 floor
    # would have fired here); and the floor sits at 5 because the lowest
    # measured break — a 9-ball break, involved 7 — appeared exactly 5, with
    # the 1-4 band empty across every journal on record.
    hub = Hub()
    asyncio.run(hub.new_rack())
    asyncio.run(hub.balls_settled(rearrange=_summary(moved=4, appeared=4, vanished=4)))
    assert hub.clock.state is State.IDLE
    asyncio.run(hub.balls_settled(rearrange=_summary(moved=2, appeared=5, moved_mm=741.0)))
    assert hub.clock.state is State.RUNNING
    assert hub.clock.rack_shots == 1


def test_stale_fire_falls_through_to_the_dark_tier() -> None:
    # A fire arms the FIRED tier only for the settle of its own episode (the
    # longest measured fire->settle roll-out is 8.62 s); an old racking blip
    # must not convert a later settle on its evidence. The settle is still
    # break-shaped on its own, so the dark tier takes it — on the un-merge,
    # not on the stale fire.
    hub = Hub()
    asyncio.run(hub.new_rack())
    asyncio.run(hub.shot_taken(speed_mms=1000.0))
    assert hub._idle_fire is not None
    stamp, speed = hub._idle_fire
    hub._idle_fire = (stamp - 20.0, speed)  # age it past the 15 s window
    asyncio.run(hub.balls_settled(rearrange=_summary(moved=1, appeared=11, moved_mm=381.0)))
    tiers = [r.get("tier") for r in hub.journal.tail(60.0) if r["kind"] == "break_adjudicated"]
    assert tiers == ["dark"]
    assert hub._idle_fire is not None  # the stale fire is not consumed by the dark tier


def test_dark_tier_counts_a_backstop_settle_with_no_mover_evidence() -> None:
    """Fail-open (#36 Part 3): the class this tier exists for is exactly the
    one where the engine slept through the whole episode, so the settle
    arrives from the pipeline backstop carrying no movers at all. Absence of
    corroborating evidence must never block the verdict — that would silently
    revert the dark-flight break to being missed.
    """
    hub = Hub()
    asyncio.run(hub.new_rack())
    asyncio.run(
        hub.balls_settled(
            rearrange=_summary(moved=1, appeared=10, moved_mm=0.0), backstop=True, movers=[]
        )
    )
    assert hub.clock.state is State.RUNNING
    assert hub.clock.rack_shots == 1


def test_both_live_missed_breaks_now_count() -> None:
    """The ground truth #36 exists for, at the numbers they actually produced.

    These are not invented shapes — each is the rearrangement its settle
    carried in the live journal, both of them breaks the clock slept through
    while Steven waited, flagged, and pressed a key:

    * 2026-08-11 03:14:17, 9-ball, `flag_missed_3` — warm replay reproduces
      the live settle exactly (t=26.02, episode latched 19.82, 8 tracks dead,
      7 newborns flying to 1543 mm, no fire anywhere in the episode).
    * 2026-08-10 21:07:56, 8-ball, `flag_missed_2` — the settle #33 Part C
      misread as racking off press timing. Its dump predates the current
      vision core, so warm replay is refused by the fingerprint and a cold
      one has no quiet layout to anchor a baseline; the live journal record
      is the ground truth here, and it is the load-bearing one.
    """
    for label, summary in (
        ("9-ball 03:14:17", _summary(moved=2, appeared=5, moved_mm=741.0)),
        ("8-ball 21:07:56", _summary(moved=0, appeared=12, moved_mm=0.0)),
    ):
        hub = Hub()
        asyncio.run(hub.new_rack())
        asyncio.run(hub.balls_settled(rearrange=summary))
        assert hub.clock.state is State.RUNNING, f"{label} still missed"
        assert hub.clock.rack_shots == 1, f"{label} counted {hub.clock.rack_shots} shots"


def test_a_caught_break_still_counts_exactly_once() -> None:
    """`flag_other`, 2026-08-11 03:15:20 — a 9-ball break vision DID catch.

    Fire at 7360 mm/s, settle 7.7 s later at moved 1 / appeared 6. The fire
    clears the break floor, so it starts the clock itself and the settle
    arrives in MOTION — the adjudicator never sees it. That is the point of
    the test: a break-scale `appeared` must not count a *second* shot on top
    of the fire that already counted it. Adjudication is IDLE-only, and this
    pins that it stays so now that the dark tier acts on `appeared` alone.
    """
    hub = Hub()
    asyncio.run(hub.new_rack())
    asyncio.run(hub.shot_taken(speed_mms=7360.0))
    assert hub.clock.state is State.MOTION  # the fire started it, no tier needed
    asyncio.run(hub.balls_settled(rearrange=_summary(moved=1, appeared=6, moved_mm=377.0)))
    assert hub.clock.rack_shots == 1  # once, not once per mechanism
    assert "break_adjudicated" not in [r["kind"] for r in hub.journal.tail(60.0)]


def test_the_9ball_racking_settles_still_do_nothing() -> None:
    # The three racking settles from the same session (03:14:49, 03:15:35,
    # 03:16:21), at their measured shape: six balls MERGING into the pack,
    # which is `vanished`, not `appeared`. All three arrived via the pipeline
    # backstop. Racking must remain the clock's no-op — checkpoint 28's
    # racking countdown is the hazard this whole policy is measured against.
    hub = Hub()
    asyncio.run(hub.new_rack())
    for _ in range(3):
        asyncio.run(
            hub.balls_settled(rearrange=_summary(moved=0, vanished=6, moved_mm=0.0), backstop=True)
        )
    assert hub.clock.state is State.IDLE
    assert hub.clock.rack_shots == 0
    assert "break_adjudicated" not in [r["kind"] for r in hub.journal.tail(60.0)]


def test_dark_tier_respects_the_kill_switch_and_no_clock() -> None:
    hub = Hub()
    asyncio.run(hub.new_rack())
    hub.vision_enabled = False
    asyncio.run(hub.balls_settled(rearrange=_summary(moved=1, appeared=11)))
    assert hub.clock.state is State.IDLE
    hub.vision_enabled = True
    asyncio.run(hub.set_mode("no_clock"))
    asyncio.run(hub.balls_settled(rearrange=_summary(moved=1, appeared=11)))
    assert hub.clock.state is State.IDLE
    assert "break_adjudicated" not in [r["kind"] for r in hub.journal.tail(60.0)]


def test_mid_rack_settles_never_adjudicate() -> None:
    # Adjudication is armed-rack-only: once the break is counted, IDLE can
    # recur (pause) but a big settle there is not a break.
    hub = Hub()
    asyncio.run(hub.new_rack())
    asyncio.run(hub.shot_taken(speed_mms=5000.0))  # real break, passes floor
    asyncio.run(hub.balls_settled(rearrange=_summary(moved=2, appeared=9, moved_mm=300.0)))
    assert hub.clock.rack_shots == 1
    asyncio.run(hub.pause())  # operator holds the clock
    asyncio.run(hub.shot_taken(speed_mms=443.0))
    asyncio.run(hub.balls_settled(rearrange=_summary(moved=1, appeared=11, moved_mm=381.0)))
    assert hub.clock.state is State.IDLE  # held means held
    assert hub.clock.rack_shots == 1


def test_operator_commands_clear_the_armed_fire() -> None:
    hub = Hub()
    asyncio.run(hub.new_rack())
    asyncio.run(hub.shot_taken(speed_mms=1000.0))
    assert hub._idle_fire is not None
    asyncio.run(hub.reset())
    assert hub._idle_fire is None


def test_frozen_clock_watchdog_synthesizes_the_lost_settle() -> None:
    """E9's watchdog: MOTION quiet past 12 s self-heals (plan §13.3).

    14 clocks over 30 sessions were left frozen in MOTION until an operator
    key rescued them. With the motion bit low past the watchdog, the hub
    synthesizes the settle the vision layer lost; the journal mark keeps the
    miss visible to triage and to `report`.
    """
    hub = Hub()
    asyncio.run(hub.start())
    asyncio.run(hub.shot_taken(speed_mms=5000.0))
    assert hub.clock.state is State.MOTION
    hub._watch_frozen_clock(1000.0)  # anchors MOTION entry
    hub._watch_frozen_clock(1011.9)  # inside the window
    assert hub.clock.state is State.MOTION
    hub._watch_frozen_clock(1012.1)  # past it
    assert hub.clock.state is State.RUNNING
    kinds = [r["kind"] for r in hub.journal.tail(60.0)]
    assert "settle_synthesized" in kinds


def test_watchdog_defers_while_motion_bit_high() -> None:
    hub = Hub()
    asyncio.run(hub.start())
    asyncio.run(hub.shot_taken(speed_mms=5000.0))
    hub._watch_frozen_clock(1000.0)
    hub._table_motion = True  # balls genuinely rolling
    hub._watch_frozen_clock(1020.0)
    assert hub.clock.state is State.MOTION  # no synthetic settle over real motion


# ---------- the solo panel (#30) ----------


def _freeze_hub_time(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Pin the hub module's clock; advance it by mutating the returned cell.

    Patches the hub's own `time` binding rather than `time.monotonic`
    globally, so asyncio's loop clock (and everything else) stays real.
    """
    now = [1000.0]
    monkeypatch.setattr(hub_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


def test_solo_panel_paces_a_run_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """LAST/AVG are wall time from each countdown's start to the shot that ends it.

    The opening shot comes out of IDLE with no countdown over it, so it counts
    in SHOTS but never in the pace — there is nothing honest to time it against.
    """
    now = _freeze_hub_time(monkeypatch)
    hub = Hub()
    asyncio.run(hub.set_mode("run_out"))
    assert hub.payload()["solo"] == {"shots": 0, "last_s": None, "avg_s": None}

    asyncio.run(hub.shot_taken(speed_mms=900.0))  # opening shot: untimed
    now[0] += 3.0
    asyncio.run(hub.balls_settled())  # countdown starts — the pace timer opens
    now[0] += 8.2
    asyncio.run(hub.shot_taken(speed_mms=1500.0))
    now[0] += 2.0
    asyncio.run(hub.balls_settled())
    now[0] += 5.0
    asyncio.run(hub.shot_taken(speed_mms=1500.0))
    now[0] += 2.0
    # #33 Part B: a RUNNING fire is provisional — SHOTS and its pace commit
    # at the settle that counts the episode, not at the strike.
    asyncio.run(hub.balls_settled())
    assert hub.payload()["solo"] == {"shots": 3, "last_s": 5.0, "avg_s": 6.6}

    # A New Rack is a fresh fifteen thrown up: the whole panel rescopes.
    asyncio.run(hub.new_rack())
    assert hub.payload()["solo"] == {"shots": 0, "last_s": None, "avg_s": None}


def test_solo_pace_times_from_restart_and_ignores_placement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = _freeze_hub_time(monkeypatch)
    hub = Hub()
    asyncio.run(hub.set_mode("run_out"))
    asyncio.run(hub.restart())  # the deck's Start key
    now[0] += 4.0
    asyncio.run(hub.shot_taken(speed_mms=1500.0))
    asyncio.run(hub.balls_settled())  # commits the provisional's pace (#33)
    solo = cast(dict[str, object], hub.payload()["solo"])
    assert solo["last_s"] == 4.0

    # Placement motion while IDLE settles for real but times nothing: the
    # settle is ignored by the clock, so it must not open a pace timer either.
    asyncio.run(hub.reset())
    asyncio.run(hub.balls_settled())
    now[0] += 9.9
    asyncio.run(hub.shot_taken(speed_mms=900.0))  # from IDLE: untimed
    solo = cast(dict[str, object], hub.payload()["solo"])
    assert solo["last_s"] == 4.0  # unchanged


def test_solo_panel_counts_a_drill(monkeypatch: pytest.MonkeyPatch) -> None:
    now = _freeze_hub_time(monkeypatch)
    hub = Hub()
    asyncio.run(hub.set_mode("no_clock"))
    now[0] += 130.0
    asyncio.run(hub.shot_taken(speed_mms=1200.0))
    asyncio.run(hub.shot_taken(speed_mms=1200.0))
    assert hub.payload()["solo"] == {"reps": 2, "elapsed_s": 130.0}

    # The kill switch outranks the counter, like every other vision consumer.
    asyncio.run(hub.set_vision(False))
    asyncio.run(hub.shot_taken(speed_mms=1200.0))
    asyncio.run(hub.set_vision(True))
    assert cast(dict[str, object], hub.payload()["solo"])["reps"] == 2

    # Leaving and returning rescopes: the panel is this drill, not the day.
    asyncio.run(hub.set_mode("match"))
    assert hub.payload()["solo"] is None
    asyncio.run(hub.set_mode("no_clock"))
    assert hub.payload()["solo"] == {"reps": 0, "elapsed_s": 0.0}


# ---------- #33 Part B: the provisional freeze, and what closes it ----------
#
# Part A (the settle refusal) was deleted at checkpoint 87 — see the #38 note
# in hub.py. What survives is the freeze at motion onset and its two honest
# endings: a settle counts the shot, and an unlatched episode with no settle
# (the engine's pickup discard) reverts it. The settle tests below therefore
# pin *counting*, which is the default verdict again.


def _mover(travel_mm: float, alive: bool):
    from cueclock.vision.events import SettleMover

    return SettleMover(travel_mm=travel_mm, alive=alive)


def _running_hub() -> Hub:
    """A hub whose clock is RUNNING mid-rack (break counted via Restart)."""
    hub = Hub()
    asyncio.run(hub.new_rack())
    asyncio.run(hub.restart())  # counts the break, starts a live countdown
    return hub


def test_missed_strike_settle_still_counts() -> None:
    # 2026-08-10 21:02:06: `moved=2, 668mm` with no ShotTaken — the
    # compensation this issue keeps, felt exactly as before.
    hub = _running_hub()
    shots = hub.clock.rack_shots
    asyncio.run(hub.balls_settled(rearrange=_summary(moved=2, moved_mm=668.0)))
    assert hub.clock.rack_shots == shots + 1
    assert hub.clock.state is State.RUNNING


def test_settle_with_no_evidence_counts() -> None:
    # Fail-open: no rearrangement summary (E9-synthesized settles, degraded
    # replay) must keep today's compensation — refusal needs positive evidence.
    hub = _running_hub()
    shots = hub.clock.rack_shots
    asyncio.run(hub.balls_settled())
    assert hub.clock.rack_shots == shots + 1


def test_multi_ball_pickup_shape_still_counts() -> None:
    # `vanished=2, 0mm` is NOT the measured refusal shape (a double-pot stun
    # reads the same) — fail-open keeps counting it until live ground truth
    # says otherwise.
    hub = _running_hub()
    shots = hub.clock.rack_shots
    asyncio.run(hub.balls_settled(rearrange=_summary(vanished=2)))
    assert hub.clock.rack_shots == shots + 1


# ---------- #37: rejecting the phantom at the settle ----------


def test_phantom_settle_reverts_the_freeze_and_counts_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live 2026-08-11 phantom, in hub terms.

    Steven still down on the ball; the latch froze a live countdown and the
    settle 1.65 s later counted a shot and reset 19.7 -> 30 s on an
    `involved == 0` reading — against a genuine 4-ball quiet baseline, which
    a replay of the dump confirmed. Rejected now: no count, no reset, the
    countdown resumes with exactly the remaining it froze at.
    """
    now = _freeze_hub_time(monkeypatch)
    hub = _running_hub()
    shots = hub.clock.rack_shots
    now[0] += 10.3  # 19.7 s left on a 30 s countdown, as in the journal
    hub.clock.tick(now[0])
    remaining = hub.clock.remaining
    asyncio.run(hub.episode_started(t=384.71))
    assert hub.clock.state is State.MOTION
    now[0] += 1.65
    asyncio.run(hub.balls_settled(t=386.36, rearrange=_summary(baseline_balls=4)))
    assert hub.clock.rack_shots == shots, "a phantom must not count a shot"
    assert hub.clock.state is State.RUNNING, "the freeze must be handed back"
    assert hub.clock.remaining == remaining, "the countdown must not reset"
    records = hub.journal.tail(60.0)
    revert = next(r for r in records if r["kind"] == "shot_provisional_reverted")
    assert revert["reason"] == "phantom_settle"
    assert revert["baseline"] == 4
    settle = next(r for r in records if r["kind"] == "vision_balls_settled")
    assert settle["rejected"] == "phantom", "the settle record must carry the verdict"
    assert settle["baseline"] == 4, "and what the diff was taken against"


def test_phantom_settle_drops_a_deferred_fire() -> None:
    # `parked_vote_strike_0801`'s shape: a ShotTaken really did fire into the
    # RUNNING countdown (a hoodie and a cue shaft crowned as the cue ball), so
    # the episode is *fired*, not merely latched. #33 defers a RUNNING fire's
    # count to the settle precisely so a verdict like this can drop it.
    hub = _running_hub()
    shots = hub.clock.rack_shots
    asyncio.run(hub.shot_taken(speed_mms=1085.0))
    assert hub.clock.state is State.MOTION
    asyncio.run(hub.balls_settled(rearrange=_summary(baseline_balls=7)))
    assert hub.clock.rack_shots == shots
    assert hub.clock.state is State.RUNNING


def test_unanchored_zero_settle_still_counts() -> None:
    # THE TRAP #33 failed open on, and it still has to. `involved == 0` with no
    # quiet baseline is arithmetic, not evidence: twelve corpus clips read it
    # purely because they open mid-action. Refusal takes positive evidence.
    hub = _running_hub()
    shots = hub.clock.rack_shots
    asyncio.run(hub.episode_started(t=10.0))
    asyncio.run(hub.balls_settled(rearrange=_summary(baseline_balls=None)))
    assert hub.clock.rack_shots == shots + 1


def test_thin_baseline_zero_settle_still_counts() -> None:
    # A baseline that exists but resolved almost nothing — an unresolved
    # racked pack — can read zero while balls scatter, because both sides of
    # the diff hold the same two tracks. Under the floor, so it counts.
    hub = _running_hub()
    shots = hub.clock.rack_shots
    asyncio.run(hub.episode_started(t=10.0))
    asyncio.run(
        hub.balls_settled(
            rearrange=_summary(baseline_balls=hub_module.QUIET_BASELINE_MIN_BALLS - 1)
        )
    )
    assert hub.clock.rack_shots == shots + 1


def test_any_rearrangement_at_all_still_counts() -> None:
    # The softest real touches in the corpus still move something
    # (`soft_touch_0731` and `tip_bump` both involve 2 balls). One is enough.
    hub = _running_hub()
    shots = hub.clock.rack_shots
    asyncio.run(hub.episode_started(t=10.0))
    asyncio.run(hub.balls_settled(rearrange=_summary(moved=1, moved_mm=63.0, baseline_balls=9)))
    assert hub.clock.rack_shots == shots + 1


def test_grace_settle_is_never_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """In GRACE the same refusal would buzz a false violation.

    The fail-safe direction: a wrongly-rejected MOTION settle costs the
    countdown it froze at, which is what #33 already accepts. A wrongly-
    rejected GRACE settle lets the clock EXPIRE over a live shot — the worst
    felt failure on record (2 in 30 sessions). So the verdict is MOTION-only.
    """
    now = _freeze_hub_time(monkeypatch)
    hub = _running_hub()
    shots = hub.clock.rack_shots
    now[0] += hub.clock.remaining + 0.1  # into GRACE
    hub.clock.tick(now[0])
    assert hub.clock.state is State.GRACE
    asyncio.run(hub.balls_settled(rearrange=_summary(baseline_balls=9)))
    assert hub.clock.rack_shots == shots + 1
    assert hub.clock.state is State.RUNNING


def test_backstop_settle_is_never_rejected() -> None:
    # The pipeline's identity-free backstop carries no episode at all, so
    # there is no mover evidence behind its summary. It cannot read zero today
    # (SETTLE_BACKSTOP_MIN_INVOLVED), and the verdict says so out loud rather
    # than relying on that staying true.
    hub = _running_hub()
    shots = hub.clock.rack_shots
    asyncio.run(hub.episode_started(t=10.0))
    asyncio.run(hub.balls_settled(rearrange=_summary(baseline_balls=9), backstop=True))
    assert hub.clock.rack_shots == shots + 1


def test_fire_merges_into_the_provisional_and_counts_once() -> None:
    # Latch freezes first, the ShotTaken lands late (10 frames or 300 —
    # unbounded within the episode), the shot-like settle counts exactly one.
    hub = _running_hub()
    shots = hub.clock.rack_shots
    asyncio.run(hub.episode_started(t=10.0))
    asyncio.run(hub.shot_taken(speed_mms=2400.0))  # merges into the freeze
    asyncio.run(hub.balls_settled(rearrange=_summary(moved=2, moved_mm=500.0)))
    assert hub.clock.rack_shots == shots + 1
    assert hub.clock.state is State.RUNNING


def test_missed_strike_recovers_at_the_latch_and_counts_at_the_settle() -> None:
    # No fire at all: freeze at the latch, count via compensation at the
    # settle — the ~5 s bleed becomes ~0.25 s, felt like a detected shot.
    hub = _running_hub()
    shots = hub.clock.rack_shots
    asyncio.run(hub.episode_started(t=10.0))
    assert hub.clock.state is State.MOTION
    asyncio.run(hub.balls_settled(rearrange=_summary(moved=1, moved_mm=195.0)))
    assert hub.clock.rack_shots == shots + 1
    assert hub.clock.state is State.RUNNING


def test_episode_discarded_reverts_the_provisional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The engine's pickup discard unlatches with no settle: revert, exact time.
    now = _freeze_hub_time(monkeypatch)
    hub = _running_hub()
    shots = hub.clock.rack_shots
    now[0] += 9.0
    hub.clock.tick(now[0])
    remaining = hub.clock.remaining
    asyncio.run(hub.episode_started(t=50.0))
    now[0] += 3.0
    asyncio.run(hub.episode_discarded(t=53.0))
    assert hub.clock.state is State.RUNNING
    assert hub.clock.remaining == remaining
    assert hub.clock.rack_shots == shots
    kinds = [r["kind"] for r in hub.journal.tail(60.0)]
    assert "shot_provisional_reverted" in kinds


def test_episode_start_respects_kill_switch_and_mode() -> None:
    hub = _running_hub()
    asyncio.run(hub.set_vision(False))
    asyncio.run(hub.episode_started(t=1.0))
    assert hub.clock.state is State.RUNNING  # kill switch: no freeze
    asyncio.run(hub.set_vision(True))
    asyncio.run(hub.set_mode("no_clock"))
    asyncio.run(hub.episode_started(t=2.0))
    assert hub.clock.state is State.IDLE  # no_clock parks the countdown


def test_operator_keys_abandon_an_open_provisional(monkeypatch: pytest.MonkeyPatch) -> None:
    # An operator key rescopes the clock, so the open episode no longer
    # describes it: the provisional's anchor and its held pace both die, and
    # the settle that eventually arrives is adjudicated on its own evidence.
    now = _freeze_hub_time(monkeypatch)
    hub = Hub()
    asyncio.run(hub.set_mode("run_out"))
    asyncio.run(hub.restart())
    now[0] += 5.0
    asyncio.run(hub.episode_started(t=10.0))  # provisional freeze
    assert hub.clock.state is State.MOTION
    asyncio.run(hub.start())  # operator takes over
    assert hub._provisional_open_mono is None
    assert hub._pace_pending is None
    now[0] += 3.0
    asyncio.run(hub.shot_taken(speed_mms=2000.0))
    asyncio.run(hub.balls_settled())
    solo = cast(dict[str, object], hub.payload()["solo"])
    assert solo["last_s"] == 3.0  # timed from the press, not the abandoned freeze


# ---------------------------------------------------------------- bracket (#43)


BRACKET_BODY = {
    "name": "Asten Family 8-Ball",
    "a_race": 5,
    "b_race": 4,
    "handicap": "off",
    "seeding": "adjacent",
    "players": [
        {"name": "Hank Harrison", "rating": 563},
        {"name": "Joel Braun", "rating": 495},
        {"name": "Steven Asten", "rating": 471},
        {"name": "Marcus Tran", "rating": 415, "established": False},
    ],
}


def test_no_bracket_means_every_page_behaves_as_before() -> None:
    with make_client() as client:
        assert client.get("/api/bracket").json() == {"ok": True, "bracket": None}
        assert client.get("/api/state").json()["bracket"] is None
        # Every bracket action is a 404 rather than a crash or a silent no-op.
        assert client.post("/api/bracket/match/1/load").status_code == 404
        assert client.post("/api/bracket/match/1/result").status_code == 404
        assert client.post("/api/bracket/archive").status_code == 404


def test_building_the_draw_puts_the_first_match_up() -> None:
    # Otherwise the bracket exists with nothing loaded, which the deck can only
    # show as "—" and which nobody wants anyway: you build the draw because you
    # are about to play the first match.
    with make_client() as client:
        client.post("/api/bracket", json=BRACKET_BODY)
        assert client.get("/api/bracket").json()["bracket"]["loaded"] == 1
        state = client.get("/api/state").json()
        assert state["bracket"]["match"]["id"] == 1
        assert state["score"]["p1"]["name"] == "Hank Harrison"


def test_create_build_and_load_a_match() -> None:
    with make_client() as client:
        created = client.post("/api/bracket", json=BRACKET_BODY).json()
        assert created["ok"]
        assert created["bracket"]["size"] == 4 and created["bracket"]["field"] == 4
        assert client.post("/api/bracket/match/1/load").json()["ok"]
        score = client.get("/api/score").json()
        assert score["p1"]["target"] == 5 and score["p2"]["target"] == 5
        assert score["race_to"] is None  # a handicapped match has no single race
        # An unestablished rating seeds the draw but never reaches the wall.
        names = {score["p1"]["name"], score["p2"]["name"]}
        assert names == {"Hank Harrison", "Joel Braun"}
        state = client.get("/api/state").json()["bracket"]
        assert state["match"]["header"] == "Match 1 · A Side · Race to 5"
        assert state["armed"] is None


def test_an_estimated_rating_seeds_but_is_not_shown_as_fargo() -> None:
    with make_client() as client:
        client.post("/api/bracket", json=BRACKET_BODY)
        client.post("/api/bracket/match/2/load")  # Steven 471 v Marcus 415 (prov)
        score = client.get("/api/score").json()
        assert score["p1"]["fargo"] == 471
        assert score["p2"]["fargo"] is None  # established=False


def test_reaching_the_target_arms_the_deck_key_without_advancing() -> None:
    with make_client() as client:
        client.post("/api/bracket", json=BRACKET_BODY)
        client.post("/api/bracket/match/1/load")
        for _ in range(5):
            client.post("/api/score/p1/increment")
        armed = client.get("/api/state").json()["bracket"]["armed"]
        assert armed["name"] == "Hank Harrison" and armed["score"] == 5
        # The bracket has not moved: Match 1 is still the live match.
        assert client.get("/api/bracket").json()["bracket"]["loaded"] == 1
        client.post("/api/score/p1/decrement")  # the hold that takes a point back
        assert client.get("/api/state").json()["bracket"]["armed"] is None


def test_confirming_routes_loads_the_next_match_and_racks() -> None:
    with make_client() as client:
        client.post("/api/bracket", json=BRACKET_BODY)
        client.post("/api/bracket/match/1/load")
        for _ in range(5):
            client.post("/api/score/p1/increment")
        client.post("/api/score/p2/increment")
        client.post("/api/start")  # a clock running under the match being played
        result = client.post("/api/bracket/match/1/result").json()
        assert result["winner"] == "Hank Harrison"
        assert result["loaded"] == 2  # the next ready match, loaded at 0-0
        score = client.get("/api/score").json()
        assert (score["p1"]["score"], score["p2"]["score"]) == (0, 0)
        assert score["p1"]["name"] == "Steven Asten"
        # A new match is a new rack: the clock parks and the shot count zeroes.
        assert client.get("/api/state").json()["state"] == "IDLE"
        bracket = client.get("/api/bracket").json()["bracket"]
        played = next(m for m in bracket["matches"] if m["id"] == 1)
        assert played["p1"]["score"] == 5 and played["p1"]["won"] is True
        # A four-player draw: Match 1's winner goes to the A final, its
        # loser opens the B side. The numbers are generated, never typed.
        assert played["routes"] == {"winner": 3, "loser": 4}


def test_a_correction_behind_a_played_match_is_refused_with_its_reason() -> None:
    with make_client() as client:
        client.post("/api/bracket", json=BRACKET_BODY)
        client.post("/api/bracket/match/1/result", json={"scores": [5, 2]})
        client.post("/api/bracket/match/2/result", json={"scores": [5, 1]})
        client.post("/api/bracket/match/3/result", json={"scores": [5, 0]})
        refused = client.post("/api/bracket/match/1/result", json={"scores": [2, 5]})
        assert refused.status_code == 409
        assert "Match 3" in refused.json()["error"]
        assert client.delete("/api/bracket/match/3/result").json()["ok"]
        assert client.post("/api/bracket/match/1/result", json={"scores": [2, 5]}).json()["ok"]


def test_a_correction_to_an_unloaded_match_never_touches_the_clock() -> None:
    with make_client() as client:
        client.post("/api/bracket", json=BRACKET_BODY)
        client.post("/api/bracket/match/1/load")
        client.post("/api/start")
        client.post("/api/bracket/match/2/result", json={"scores": [5, 0]})
        assert client.get("/api/state").json()["state"] == "RUNNING"
        assert client.get("/api/bracket").json()["bracket"]["loaded"] == 1


def test_editing_a_round_one_matchup_over_http() -> None:
    with make_client() as client:
        client.post("/api/bracket", json=BRACKET_BODY)
        assert client.patch("/api/bracket/match/1", json={"p1": 3, "p2": 0}).json()["ok"]
        bracket = client.get("/api/bracket").json()["bracket"]
        first = next(m for m in bracket["matches"] if m["id"] == 1)
        assert [first["p1"]["name"], first["p2"]["name"]] == ["Marcus Tran", "Hank Harrison"]
        # A derived slot is not editable — it is derived.
        assert client.patch("/api/bracket/match/3", json={"p1": 1}).status_code == 409


def test_target_override_reaches_the_live_scoreboard() -> None:
    with make_client() as client:
        client.post("/api/bracket", json=BRACKET_BODY)
        client.post("/api/bracket/match/1/load")
        client.patch("/api/bracket/match/1", json={"p1_target": 7, "p2_target": 3})
        score = client.get("/api/score").json()
        assert (score["p1"]["target"], score["p2"]["target"]) == (7, 3)


def test_bracket_survives_a_restart_and_clearing_removes_it(tmp_path: Path) -> None:
    path = tmp_path / "bracket.json"
    with TestClient(create_app(bracket_path=path)) as client:
        client.post("/api/bracket", json=BRACKET_BODY)
        client.post("/api/bracket/match/1/result", json={"scores": [5, 2]})
    with TestClient(create_app(bracket_path=path)) as client:
        bracket = client.get("/api/bracket").json()["bracket"]
        assert bracket["name"] == "Asten Family 8-Ball"
        first = next(m for m in bracket["matches"] if m["id"] == 1)
        assert first["status"] == "played"
        client.post("/api/bracket/match/2/load")
        assert client.get("/api/score").json()["p1"]["target"] == 5
        assert client.delete("/api/bracket").json() == {"ok": True}
        assert client.get("/api/bracket").json()["bracket"] is None
        # Casual again, all the way down: a handicap nothing can now explain
        # must not be left on the wall.
        casual = client.get("/api/score").json()
        assert casual["p1"]["target"] is None and casual["p2"]["target"] is None
    assert not path.exists()  # a cleared bracket must not come back on restart
    with TestClient(create_app(bracket_path=path)) as client:
        assert client.get("/api/bracket").json()["bracket"] is None


def test_archive_and_reopen(tmp_path: Path) -> None:
    with TestClient(
        create_app(bracket_path=tmp_path / "bracket.json", archive_dir=tmp_path / "logs")
    ) as client:
        client.post("/api/bracket", json=BRACKET_BODY)
        client.post("/api/bracket/match/1/result", json={"scores": [5, 2]})
        archived = client.post("/api/bracket/archive").json()
        assert archived["ok"] and archived["path"].endswith("bracket-asten-family-8-ball.json")
        listing = client.get("/api/bracket/archives").json()["archives"]
        assert listing == ["bracket-asten-family-8-ball.json"]
        client.delete("/api/bracket")
        reopened = client.post(f"/api/bracket/archives/{listing[0]}").json()
        assert reopened["ok"]
        first = next(m for m in reopened["bracket"]["matches"] if m["id"] == 1)
        assert first["p1"]["score"] == 5
        # A name that walks out of the archive directory is refused.
        assert client.post("/api/bracket/archives/..%2Fbracket.json").status_code == 404


def test_a_bad_field_is_a_400_not_a_500() -> None:
    with make_client() as client:
        assert client.post("/api/bracket", json={"players": []}).status_code == 400
        assert client.post("/api/bracket", json={"players": "nope"}).status_code == 400
        bad = {**BRACKET_BODY, "handicap": "spicy"}
        assert client.post("/api/bracket", json=bad).status_code == 400
