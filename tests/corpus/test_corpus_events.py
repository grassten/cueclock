"""Corpus scoreboard: the detection pipeline vs. every recorded clip.

Each §2.3 scenario has expected event counts; this is the accuracy
scoreboard build-order step 4 calls for. Local-only by construction: skips
when the clips or the fitted calibration are absent (CI has neither).

Expectations are deliberately asymmetric in strictness: scenarios that
define the §2.3 failure modes (nudges, bumps, adjusts — must NOT read as
shots) are asserted hard; scenarios whose incidental events are unknowable
without frame-accurate ground truth (e.g. how many settles a racking
session produces) only pin what matters.
"""

from functools import cache
from pathlib import Path

import numpy as np
import pytest

from cueclock.vision.events import BallsSettled, Event, Scratch, ShotTaken

CLIPS = Path("tests/clips")
CALIBRATION = Path("config/table_calibration.json")

pytestmark = pytest.mark.skipif(
    not (CLIPS / "empty_table").exists() or not CALIBRATION.exists(),
    reason="corpus clips / local calibration not present",
)

# scenario -> (shots, scratches, min_settles) — None = don't assert
EXPECTED: dict[str, tuple[int | None, int | None, int | None]] = {
    "empty_table": (0, 0, None),
    "static_spread": (0, 0, None),
    "soft_shot": (1, 0, 1),
    # medium/power: struck ~4 s in, balls legitimately still rolling when the
    # 8/10 s clip ends — settle count is unknowable, only the shot is pinned
    "medium_shot": (1, 0, None),
    "power_shot": (1, 0, None),
    "break_shot": (1, 0, None),
    "practice_strokes": (0, 0, None),
    "chalk_near": (0, 0, None),
    "ball_in_hand_place": (0, 0, None),
    "ball_in_hand_nudge": (0, 0, None),
    "shaft_adjust": (0, 0, None),
    "tip_bump": (0, 0, None),
    "spin_in_place": (1, 0, 1),
    "slow_creep": (1, 0, 1),
    "walk_around_settle": (None, 0, None),
    "rack_balls": (0, 0, None),
    "scratch_replace": (1, 1, None),
    # TV-era scenarios (recorded 2026-07-29: post-TV-install camera position,
    # wall TV on). shot_debug is the live-deafness repro: one firm shot, a
    # real cue scratch into the top-right pocket, settle once the slow-rolling
    # 6-ball truly stops (~11.9 s). It guards the settle path against
    # TV-reflection churn — the glossy 8-ball reads ~2.2 the whole clip, above
    # the old 2.0 gate that deadlocked settle live.
    "empty_table_0729": (0, 0, None),
    "shot_debug": (1, 1, 1),
    # Flag-key live captures (2026-07-31): the carom-guard-redesign evidence.
    # Ground truth from the table: one real strike each, no scratch, balls
    # settle in-clip. The live session missed the first two strikes outright
    # (v1 guard starved by hand/stick junk latching table_moving) and the
    # third session also minted a phantom second shot. These pin the v2
    # guard (other-ball motion), the flicker filter, and the axis-seed floor.
    "missed_strike_0731": (1, 0, 1),
    "live_divergence_0731a": (1, 0, 1),
    "live_divergence_0731b": (1, 0, 1),
}

# Clips pair with the empty-table reference from the same camera position:
# post-TV-install clips use empty_table_0729, everything earlier the original.
REFERENCES: dict[str, str] = {
    "empty_table_0729": "empty_table_0729",
    "shot_debug": "empty_table_0729",
    "missed_strike_0731": "empty_table_0729",
    "live_divergence_0731a": "empty_table_0729",
    "live_divergence_0731b": "empty_table_0729",
}


@cache
def _reference_frames(name: str) -> list[np.ndarray]:
    from cueclock.capture.clip import ClipReader

    reader = ClipReader(CLIPS / name)
    indices = np.linspace(0, len(reader) - 1, num=15, dtype=int)
    return [reader.frame(int(i)) for i in indices]


def _run_clip(name: str, reference_frames: list[np.ndarray]) -> list[Event]:
    from cueclock.capture.clip import ClipReader
    from cueclock.vision.calibration import TableCalibration
    from cueclock.vision.pipeline import VisionPipeline

    pipeline = VisionPipeline(TableCalibration.load(CALIBRATION), reference_frames)
    reader = ClipReader(CLIPS / name)
    # Replay at the live detect cadence (~78 Hz): record-made clips store the
    # full 156 fps camera stream, while ring-buffer dumps store only the
    # detect-stride frames (78 fps) — one fixed stride would replay dumps at
    # half the cadence the live session actually ran.
    timestamps = reader.timestamps
    fps = (len(reader) - 1) / float(timestamps[-1] - timestamps[0])
    stride = max(1, round(fps / 78.0))
    events: list[Event] = []
    for i in range(0, len(reader), stride):
        events += pipeline.process(float(timestamps[i]), reader.frame(i))
    return events


# Known defects, kept visible instead of loosening the expectation.
# (spin_in_place's false Scratch lived here until the observed-position fix:
# scratch now reads where the cue was last *observed*, not where the coasting
# extrapolation died — see vision/events.py.)
KNOWN_FAILURES: dict[str, pytest.MarkDecorator] = {}


@pytest.mark.parametrize(
    "name",
    [pytest.param(n, marks=KNOWN_FAILURES.get(n, ())) for n in sorted(EXPECTED)],
)
def test_corpus_clip(name: str) -> None:
    if not (CLIPS / name).exists():
        pytest.skip(f"clip {name} not recorded")
    ref_name = REFERENCES.get(name, "empty_table")
    if not (CLIPS / ref_name).exists():
        pytest.skip(f"reference clip {ref_name} not recorded")
    events = _run_clip(name, _reference_frames(ref_name))
    shots = sum(isinstance(e, ShotTaken) for e in events)
    scratches = sum(isinstance(e, Scratch) for e in events)
    settles = sum(isinstance(e, BallsSettled) for e in events)
    summary = f"{name}: {[f'{type(e).__name__}@{e.t:.2f}' for e in events]}"
    print(summary)

    exp_shots, exp_scratches, min_settles = EXPECTED[name]
    if exp_shots is not None:
        assert shots == exp_shots, summary
    if exp_scratches is not None:
        assert scratches == exp_scratches, summary
    if min_settles is not None:
        assert settles >= min_settles, summary


def test_spin_in_place_settle_waits_for_spin_stop() -> None:
    """WPA def. 8.2: the shot ends when the ball stops moving AND spinning.

    Ground truth measured from the clip's own pixels (2026-07-28): the cue's
    appearance churn reaches the still-ball noise floor — visible spin stop —
    at t≈7.0 s. Without the churn gate, settle fired at 6.48 s, mid-spin.
    """
    if not (CLIPS / "spin_in_place").exists():
        pytest.skip("clip spin_in_place not recorded")
    events = _run_clip("spin_in_place", _reference_frames("empty_table"))
    settles = [e.t for e in events if isinstance(e, BallsSettled)]
    assert settles, "spin_in_place must settle before the clip ends"
    assert settles[-1] >= 7.0, f"settled at {settles[-1]:.2f}s — before spin stopped"
