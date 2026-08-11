"""Measure the corpus freeze-latency distribution — issue #34's budget anchor.

The clock-outcome criterion needs a number: how long after the stroke may the
countdown stop and still be imperceptible? #34 says anchor it on measurement,
not taste, so this replays every corpus clip that has ground truth through a
real ``Hub`` (``tests/corpus/clock_replay``) and reports, per clip:

    freeze latency = (clock entered MOTION) - (ground-truth stroke time)

Ground truth is seeded mechanically and its provenance is printed with every
row, because inventing a stroke time is the failure mode this whole issue
exists to avoid:

    * how many real shots  -> the EXPECTED table in test_corpus_events.py
    * roughly when         -> the golden's ShotTaken.t, on clips that ever
                              fired one (including clips current code misses)

Where the two disagree, or where a clip has neither, the row is reported as
NO GROUND TRUTH and contributes nothing to the distribution.

Usage:
    uv run python scripts/clock_budget_probe.py            # all clips
    uv run python scripts/clock_budget_probe.py soft_shot  # a subset

Respects CUECLOCK_DETECT_CACHE exactly like the corpus battery.
"""

import json
import os
import statistics
import sys
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))  # `tests` is not a distributed package

from cueclock.capture.clip import ClipReader  # noqa: E402
from cueclock.vision.calibration import TableCalibration  # noqa: E402
from cueclock.vision.pipeline import ReferenceModel, VisionPipeline  # noqa: E402
from tests.corpus.clock_replay import replay_clock  # noqa: E402
from tests.corpus.test_corpus_events import EXPECTED, REFERENCES  # noqa: E402

CLIPS = Path("tests/clips")
CALIBRATION = Path("config/table_calibration.json")
GOLDENS = Path("tests/goldens")

# Clips whose recorded truth is a break out of an armed rack: the IDLE path,
# where E8 decides, rather than the mid-rack freeze path.
BREAK_CLIPS = {
    "break_0801",
    "break_0801b",
    "break_outrun_0805",
    "break_underread_0801",
    "break_dark_flight_0806",
    "break_dark_total_0806",
    "break_shot",
}


def _golden_shot_times(name: str) -> list[float] | None:
    path = GOLDENS / f"{name}.json"
    if not path.exists():
        return None
    doc = json.loads(path.read_text())
    return [float(e["t"]) for e in doc["events"] if e["type"] == "ShotTaken"]


def _ground_truth(name: str) -> tuple[list[float] | None, str]:
    """(stroke times, provenance) — mechanically seeded, never invented."""
    expected = EXPECTED.get(name)
    shots_expected = expected[0] if expected else None
    golden = _golden_shot_times(name)
    if shots_expected is None:
        return None, "shots unasserted in EXPECTED"
    if golden is None:
        return None, "no golden recorded"
    if len(golden) != shots_expected:
        return None, f"EXPECTED says {shots_expected} shots, golden holds {len(golden)}"
    if not golden:
        return [], "EXPECTED: no shots"
    return golden, "golden ShotTaken.t"


def _detections(reader, clip_dir, pipeline, indices):
    cache_dir = os.environ.get("CUECLOCK_DETECT_CACHE")
    if not cache_dir:
        return [pipeline.detect(reader.frame(i)) for i in indices]
    from cueclock.capture.detect_cache import DetectionCache, cache_key

    store = DetectionCache(Path(cache_dir))
    key = cache_key(clip_dir, pipeline.reference, pipeline.detect_config)
    hit = store.load(clip_dir, key, indices)
    if hit is not None:
        return hit
    computed = [pipeline.detect(reader.frame(i)) for i in indices]
    store.store(clip_dir, key, indices, computed)
    return computed


_REF_CACHE: dict[str, ReferenceModel] = {}


def _reference(name: str, calibration) -> ReferenceModel:
    if name not in _REF_CACHE:
        reader = ClipReader(CLIPS / name)
        idx = np.linspace(0, len(reader) - 1, num=15, dtype=int)
        _REF_CACHE[name] = ReferenceModel.build([reader.frame(int(i)) for i in idx], calibration)
    return _REF_CACHE[name]


def main() -> int:
    wanted = sys.argv[1:] or sorted(EXPECTED)
    calibration = TableCalibration.load(CALIBRATION)
    latencies: list[tuple[str, float]] = []
    unasserted: list[tuple[str, str]] = []

    print(f"{'clip':30s} {'truth':>8s} {'freezes':>8s} {'counts':>7s} {'latency (s)':>26s}")
    print("-" * 90)
    for name in wanted:
        if not (CLIPS / name).exists():
            continue
        truth, provenance = _ground_truth(name)
        # Same default the battery uses. A wrong-era reference degrades
        # detection silently — no error, just missing events.
        reference = REFERENCES.get(name, "empty_table")
        if not (CLIPS / reference).exists():
            continue
        pipeline = VisionPipeline(calibration, _reference(reference, calibration))
        reader = ClipReader(CLIPS / name)
        indices = reader.detect_indices()
        frames = _detections(reader, CLIPS / name, pipeline, indices)
        timeline = replay_clock(
            name,
            pipeline,
            [float(x) for x in reader.timestamps],
            frames,
            indices,
            mid_rack=name not in BREAK_CLIPS,
        )
        if truth is None:
            unasserted.append((name, provenance))
            print(
                f"{name:30s} {'-':>8s} {len(timeline.freezes):8d} "
                f"{len(timeline.counts):7d}   {provenance}"
            )
            continue
        # Pair each true stroke with the first freeze at or after it.
        pairs = []
        for stroke in truth:
            after = [f for f in timeline.freezes if f >= stroke - 1.0]
            pairs.append(after[0] - stroke if after else float("nan"))
        for lat in pairs:
            if lat == lat:  # not NaN
                latencies.append((name, lat))
        shown = ", ".join("miss" if p != p else f"{p:+.3f}" for p in pairs)
        print(
            f"{name:30s} {len(truth):8d} {len(timeline.freezes):8d} "
            f"{len(timeline.counts):7d}   {shown}"
        )
        # Candidate grading rule, applied here so it can be checked against
        # every clip BEFORE it becomes an assertion.
        if name not in BREAK_CLIPS and truth:
            lo = truth[0] - 0.5
            unmatched = [
                f
                for f in timeline.freezes
                if f >= lo and not any(s - 0.5 <= f <= s + 0.25 for s in truth)
            ]
            counts_in = [c for c in timeline.counts if c >= lo]
            froze = all(any(s - 0.5 <= f <= s + 0.25 for f in timeline.freezes) for s in truth)
            verdict = []
            if not froze:
                verdict.append("NO-FREEZE")
            if len(counts_in) != len(truth):
                verdict.append(f"COUNTS {len(counts_in)}!={len(truth)}")
            if unmatched:
                verdict.append(f"extra-freeze {[round(x, 2) for x in unmatched]}")
            if verdict:
                print(f"      >>> {' | '.join(verdict)}")
        if os.environ.get("CLOCK_PROBE_DETAIL"):
            print(f"      strokes {[round(x, 3) for x in truth]}")
            print(f"      freezes {[round(x, 3) for x in timeline.freezes]}")
            print(f"      counts  {[round(x, 3) for x in timeline.counts]}")
            print(f"      settles {[round(x, 3) for x in timeline.settles]}")
            print(f"      fires   {[round(x, 3) for x in timeline.shots_fired]}")

    print("\n" + "=" * 90)
    vals = [v for _, v in latencies]
    if vals:
        vals_sorted = sorted(vals)
        print(f"freeze latency vs golden stroke, n={len(vals)}")
        print(
            f"  min {min(vals):+.3f}  median {statistics.median(vals):+.3f}  max {max(vals):+.3f}"
        )
        p90 = vals_sorted[int(0.9 * (len(vals_sorted) - 1))]
        print(f"  p90 {p90:+.3f}")
        print("  NOTE: a golden's ShotTaken.t is the CONFIRM, not the stroke — on a clip")
        print("  that still fires, the latch freezes BEFORE it, so negative is expected")
        print("  and is the #33 improvement. The positive tail is what the budget bounds.")
        positive = [v for v in vals if v > 0]
        if positive:
            print(f"  positive tail: n={len(positive)} max {max(positive):+.3f}")
    print(f"\nno ground truth ({len(unasserted)}):")
    for name, why in unasserted:
        print(f"  {name:30s} {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
