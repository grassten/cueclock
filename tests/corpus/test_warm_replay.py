"""Warm-replay determinism over real footage (the corpus half of the
acceptance criterion — the synthetic half lives in tests/unit/test_warm_replay.py).

Snapshot the full VisionPipeline mid-clip via dump_state(), restore into a
freshly built pipeline (same calibration + reference frames, exactly the
`analyze --warm` contract), replay the remaining frames, and require the
identical events of the uninterrupted run. Skips like every corpus test
when clips or the local calibration are absent.
"""

from pathlib import Path

import numpy as np
import pytest

CLIPS = Path("tests/clips")
CALIBRATION = Path("config/table_calibration.json")
# Original-corpus scenario: stable name, one real shot mid-clip, so the cut
# lands with a strike candidate / motion episode plausibly in flight.
CLIP = "medium_shot"
REFERENCE = "empty_table"

pytestmark = pytest.mark.skipif(
    not (CLIPS / REFERENCE).exists() or not CALIBRATION.exists(),
    reason="corpus clips / local calibration not present",
)


def test_pipeline_warm_resume_matches_uninterrupted_replay() -> None:
    if not (CLIPS / CLIP).exists():
        pytest.skip(f"clip {CLIP} not recorded")
    from cueclock.capture.clip import ClipReader
    from cueclock.vision.calibration import TableCalibration
    from cueclock.vision.pipeline import VisionPipeline

    calibration = TableCalibration.load(CALIBRATION)
    ref_reader = ClipReader(CLIPS / REFERENCE)
    ref_idx = np.linspace(0, len(ref_reader) - 1, num=15, dtype=int)
    refs = [ref_reader.frame(int(i)) for i in ref_idx]

    reader = ClipReader(CLIPS / CLIP)
    timestamps = reader.timestamps
    indices = reader.detect_indices()  # every stored frame, as analyze/corpus do
    cut = len(indices) // 2

    live = VisionPipeline(calibration, refs, cadence_hz=reader.cadence_hz)
    blob: bytes | None = None
    per_frame = []
    for k, i in enumerate(indices):
        if k == cut:
            blob = live.dump_state()  # what `run` would have in its snapshot deque
        per_frame.append(live.process(float(timestamps[i]), reader.frame(i)))
    baseline = [e for events in per_frame for e in events]
    prefix = [e for events in per_frame[:cut] for e in events]
    assert baseline, "scenario must produce events for the comparison to mean anything"
    assert blob is not None

    warm = VisionPipeline(
        calibration, refs, cadence_hz=reader.cadence_hz
    )  # fresh build, as `analyze --warm` does
    warm.load_state(blob)
    resumed: list = []
    for i in indices[cut:]:
        resumed += warm.process(float(timestamps[i]), reader.frame(i))
    assert prefix + resumed == baseline
