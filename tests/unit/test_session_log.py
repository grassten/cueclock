"""The session log and its replay (#57) — synthetic, no camera, no clips."""

import asyncio
import json
import threading
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from cueclock.bridge import hub_calls
from cueclock.capture.session_log import (
    LoggedFrame,
    SessionLog,
    SessionLogWriter,
    StillFrame,
)
from cueclock.capture.session_replay import (
    VirtualClock,
    compare,
    read_journal,
    to_mono,
    verify,
)
from cueclock.server.hub import BROADCAST_INTERVAL_SECONDS, Hub
from cueclock.vision.calibration import TableCalibration, UndistortParams
from cueclock.vision.detect import REJECT_LARGE, DetectConfig, Detection, Reject
from cueclock.vision.pipeline import VisionPipeline

STARTED_MONO = 5000.0
STARTED_WALL = datetime(2026, 11, 16, 22, 52, 26, tzinfo=UTC)
CADENCE = 156.25


def _calibration() -> TableCalibration:
    """Pixels are table millimetres: no distortion, identity homography."""
    return TableCalibration(
        homography=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
        length_mm=2540.0,
        width_mm=1270.0,
        line_residual_px=0.0,
        undistort=UndistortParams(
            fx=1000.0,
            fy=1000.0,
            cx=960.0,
            cy=600.0,
            dist=(0.0, 0.0, 0.0, 0.0, 0.0),
            width=1920,
            height=1200,
            fit_residual_px=0.0,
        ),
    )


def _header(tmp_path: Path) -> dict[str, object]:
    cal = tmp_path / "cal.json"
    _calibration().save(cal)
    return {
        "stamp": "20261116-225226",
        "started_wall": STARTED_WALL.isoformat(timespec="milliseconds"),
        "started_mono": STARTED_MONO,
        "git_sha": None,
        "git_dirty": False,
        "cadence_hz": CADENCE,
        "stride": 1,
        "churn_lag": 2,
        "detect_config": {**DetectConfig().__dict__, "export_rejects": True},
        "reference": {"path": "x", "digest": "y"},
        "calibration": json.loads(cal.read_text()),
        "hub": {
            "ruleset": "ultimate_pool_8ball",
            "mode": "match",
            "vision": True,
            "score": None,
            "bracket": None,
        },
    }


def _ball(x: float, y: float, white: float) -> Detection:
    return Detection(
        cx=x, cy=y, area=1450.0, bbox=(int(x) - 21, int(y) - 21, 43, 43), whiteness=white
    )


def test_round_trip(tmp_path: Path) -> None:
    root = tmp_path / "s"
    writer = SessionLogWriter(root, _header(tmp_path), chunk_seconds=0.05)
    frames = [
        LoggedFrame(
            frame_i=100 + i + (i > 5),  # a camera drop after frame 5
            t=i / CADENCE,
            mono=STARTED_MONO + i / CADENCE + 0.001,
            detections=[_ball(400.0 + i * 0.123456789, 300.0, 0.9)],
            churn=[None if i < 2 else 1.25],
            intrusions=[],
            rejects=[Reject(bbox=(10, 20, 300, 200), area=9000.0, reason=REJECT_LARGE)] * (i % 2),
        )
        for i in range(20)
    ]
    for f in frames:
        writer.frame(f)
    bayer = np.random.default_rng(1).integers(0, 256, (12, 16), dtype=np.uint8)
    writer.still(
        "shot",
        mono=1.0,
        wall="w",
        t=0.1,
        frame_i=7,
        frames=[StillFrame(5, 0.0, bayer), StillFrame(7, 0.1, bayer[::-1].copy())],
    )
    writer.input(
        {"mono": 1.5, "wall": "w", "method": "report_result", "kwargs": {"scores": (3, 2)}}
    )
    stats = writer.close()
    assert stats.frames == 20 and stats.chunks > 1 and stats.pngs == 2
    assert stats.dropped_frames == stats.dropped_stills == 0

    log = SessionLog(root)
    assert list(log.frames()) == frames  # float64: centroids round-trip exactly
    assert log.inputs()[0]["kwargs"] == {"scores": [3, 2]}
    (still,) = log.stills()
    assert still["reason"] == "shot" and still["files"] == ["000000005.png", "000000007.png"]
    assert np.array_equal(log.still_frame(still["files"][1]), bayer[::-1])


def test_a_full_queue_drops_and_counts_instead_of_waiting(tmp_path: Path) -> None:
    """The detect loop must never block on disk: a stalled writer costs items."""
    release = threading.Event()
    dropped: list[tuple[str, int]] = []
    writer = SessionLogWriter(
        tmp_path / "s",
        _header(tmp_path),
        chunk_seconds=0.0,  # every frame closes a chunk
        queue_items=1,
        on_drop=lambda what, n: dropped.append((what, n)),
    )
    real = writer._write_chunk

    def stalled(index: int, frames: list[LoggedFrame]) -> None:
        release.wait(5.0)
        real(index, frames)

    writer._write_chunk = stalled  # type: ignore[method-assign]
    for i in range(6):
        writer.frame(LoggedFrame(i, i * 0.01, float(i), [], [], [], []))
    frame = StillFrame(0, 0.0, np.zeros((4, 4), np.uint8))
    writer.still("settle", mono=0.0, wall="w", t=0.0, frame_i=0, frames=[frame])
    assert writer.stats.dropped_frames >= 4 and writer.stats.dropped_stills == 1
    assert ("still", 1) in dropped and ("chunk", 1) in dropped
    release.set()
    stats = writer.close()
    assert stats.frames + stats.dropped_frames == 6


def test_hub_runs_on_an_injected_clock_and_logs_inputs_once() -> None:
    clock = VirtualClock(now=100.0, started_mono=100.0, started_wall=STARTED_WALL)
    hub = Hub(now=clock.mono, wall=clock.wall)
    inputs: list[dict[str, object]] = []
    hub.on_input = inputs.append
    clock.now = 103.25
    asyncio.run(hub.start())
    # clear_match_day calls clear_bracket, itself an input: logged once, outermost.
    asyncio.run(hub.clear_match_day())
    asyncio.run(hub.score_adjust("p1", 1))
    assert [r["method"] for r in inputs] == ["start", "clear_match_day", "score_adjust"]
    assert inputs[0]["mono"] == 103.25 and inputs[0]["wall"] == "2026-11-16T22:52:29.250+00:00"
    assert inputs[2]["kwargs"] == {"player": "p1", "delta": 1}
    assert hub.score.p1_score == 1
    clock.now += 31.0  # past the 30 s countdown: the tick sees it on the virtual clock
    hub.tick()
    record = hub.journal.tail(1.0)[-1]
    assert record["kind"] == "tick_transition" and record["mono"] == 134.25


def test_to_mono_reads_seconds_and_wall_times() -> None:
    header = {"started_mono": 10.0, "started_wall": STARTED_WALL.isoformat()}
    assert to_mono(header, "12.5") == 22.5
    assert to_mono(header, "2026-11-16T22:53:26") == 70.0


def _scene(t: float) -> list[Detection]:
    """A cue at rest, struck at 3 s along +x, rolling to rest; an object ball still."""
    still = _ball(1700.0, 640.0, 0.1)
    if t < 3.0:
        x = 600.0
    else:
        dt = min(t - 3.0, 0.8)
        x = 600.0 + 1500.0 * dt - 0.5 * 1875.0 * dt * dt  # 1500 mm/s to rest in 0.8 s
    return [_ball(x, 640.0, 0.9), still]


def _record_live_session(tmp_path: Path) -> Path:
    """What `cueclock run` does, on a virtual clock: frames, inputs, ticks.

    The tick phase and the dispatch lag deliberately differ from replay's, as
    they do live."""
    root = tmp_path / "sessions" / "20261116-225226"
    header = _header(tmp_path)
    writer = SessionLogWriter(root, header)
    clock = VirtualClock(now=STARTED_MONO, started_mono=STARTED_MONO, started_wall=STARTED_WALL)
    hub = Hub(journal_path=tmp_path / "live.jsonl", now=clock.mono, wall=clock.wall)
    hub.on_input = writer.input
    vision = VisionPipeline(_calibration(), None, cadence_hz=CADENCE)
    commands = {1.0: hub.start, 20.0: hub.extension, 70.0: hub.new_rack}
    next_tick = STARTED_MONO + 0.037
    episode = False

    async def drive() -> None:
        nonlocal next_tick, episode
        for i in range(int(80 * CADENCE)):
            t = i / CADENCE
            mono = STARTED_MONO + t + 0.003
            while next_tick <= mono:
                clock.now = next_tick
                hub.tick()
                next_tick += BROADCAST_INTERVAL_SECONDS
            for at in [a for a in commands if a <= t]:
                clock.now = STARTED_MONO + at
                await commands.pop(at)()
            clock.now = mono
            detections = _scene(t)
            writer.frame(LoggedFrame(i, t, mono, detections, [1.0, 1.0], [], []))
            events = vision.advance(t, detections, [1.0, 1.0], [])
            calls, episode = hub_calls(hub, vision, t, events, episode)
            hub.note_frame(t, balls=len(vision.last_balls))
            hub.note_motion(vision.last_motion)
            clock.now = mono + 0.002  # the hop to the asyncio loop
            for call in calls:
                await call

    asyncio.run(drive())
    writer.close()
    return root


def test_replay_reproduces_the_live_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _record_live_session(tmp_path)
    live = read_journal(tmp_path / "live.jsonl")
    kinds = {r["kind"] for r in live}
    # The scenario exercises vision, commands and tick-driven transitions.
    assert {"vision_shot_taken", "vision_balls_settled", "cmd_start", "tick_transition"} <= kinds
    monkeypatch.setattr("cueclock.capture.session_replay.git_state", lambda: (None, False))
    verdict = verify(SessionLog(root), tmp_path / "live.jsonl", tmp_path / "replay.jsonl")
    assert verdict["ok"], verdict["diffs"]
    assert verdict["live_decisions"] == verdict["replay_decisions"] > 5
    assert json.loads((root / "replay.json").read_text())["ok"]


def test_a_missing_input_is_a_divergence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The self-check: a log that lost an input cannot reproduce the session."""
    root = _record_live_session(tmp_path)
    lines = (root / "inputs.jsonl").read_text().splitlines()
    (root / "inputs.jsonl").write_text("\n".join(lines[1:]) + "\n")  # lose the start
    monkeypatch.setattr("cueclock.capture.session_replay.git_state", lambda: (None, False))
    verdict = verify(SessionLog(root), tmp_path / "live.jsonl", tmp_path / "replay.jsonl")
    assert not verdict["ok"]
    first = verdict["diffs"][0]
    assert first["wall"] and first["summary"].startswith("live: cmd_start RUNNING")


def test_compare_tolerates_timing_but_not_verdicts() -> None:
    def rec(kind: str, remaining: float, **kw: object) -> dict[str, object]:
        return {"kind": kind, "state": "RUNNING", "rack_shots": 1, "remaining": remaining, **kw}

    live = [rec("vision_balls_settled", 30.0), rec("cmd_extension", 12.0, accepted=True)]
    assert compare(live, [rec("vision_balls_settled", 29.9), live[1]]) == []
    assert compare(live, [rec("vision_balls_settled", 29.0), live[1]])[0]["op"] == "remaining"
    changed = compare(live, [live[0], rec("cmd_extension", 12.0, accepted=False)])
    assert [d["op"] for d in changed] == ["changed"]
    assert compare([*live, {"kind": "detect_heartbeat"}], live) == []
