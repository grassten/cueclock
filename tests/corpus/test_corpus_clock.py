"""Grade the corpus on clock outcome, not event counts — issue #34, item 1.

`test_corpus_events.py` asserts counts of `ShotTaken` / `Scratch` /
`BallsSettled`. That was a fair proxy for "did the clock behave" until #33
(checkpoint 83) deliberately introduced a *second* path to the same
user-visible outcome: when the strike machinery misses, the motion-episode
latch freezes the clock ~0.25 s after the stroke and the settle counts the
shot. The felt result is indistinguishable from a detected strike — it is
faster than the ~0.5 s a confirmed `ShotTaken` takes.

So since #33 the event stream and the product diverge on purpose, and a clip
where vision misses the strike but the clock stops on time is scored as a
failure while behaving correctly. Steven's framing (2026-08-11):

    "If the clock stops 0.25 s after the shot actually occurs, for all
    intents and purposes, that IS shot detection."

This module grades that. What it asserts, per real shot:

1. the countdown **stopped** within the measured budget of the stroke;
2. the shot was **counted exactly once** — no shot invented, none dropped.

*How* it happened — strike fire, latch freeze, or settle compensation — is
diagnostics, not the grade. Event counts stay asserted in
`test_corpus_events.py` for now: they are the leading indicator if the strike
substrate rots, and this module has to prove itself before anything is
switched off.

**Late is fine, extra is not.** A missed strike is recoverable by design
since #33; an invented one is not, and no downstream policy can undo it.

Known state at landing (2026-08-11), so a reader is not surprised by red:

* ``stroke_tug_strike_0801`` — **passes here and fails the event battery**,
  which is the whole point. Its fire is gone, and the latch still stops the
  clock 0.128 s *before* the golden's stroke. #34 work item 2 predicted
  exactly this; the clip is behaving correctly and the event grade is what
  is wrong about it.
* ``parked_vote_strike_0801`` — **fails both.** It counts three shots against
  a recorded truth of two, with a phantom freeze at 10.36 s in the middle of
  the clip. A genuine defect under either criterion.
* ``pickup_snatch_0801`` — **new finding**, surfaced by this module. A
  bare-hand snatch of the resting white freezes the countdown at 22.41 s and
  the settle at 29.65 s counts it as a shot. Whether this is a live defect
  is NOT yet established: the clip's rearrangement is measured against a
  degenerate one-ball baseline (it opens mid-action with no quiet layout),
  so it reads ``moved=1, appeared=6`` and misses #33 Part A's placement
  shape (``moved == 0 and involved == 1``). Every live ball-in-hand settle
  on record, with an anchored baseline, DOES match that shape and is
  correctly refused. One live hand-snatch with an anchored baseline settles
  it either way; until then the red stands rather than being tuned away.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .clock_replay import (
    BREAK_CLIPS,
    FREEZE_EARLY_S,
    FREEZE_LATE_S,
    ground_truth,
    replay_clock,
)
from .test_corpus_events import (
    CLIPS,
    EXPECTED,
    REFERENCES,
    _calibration,
    _detections,
    _reference_model,
)

pytestmark = pytest.mark.skipif(
    not (CLIPS / "empty_table").exists(),
    reason="corpus clips not present",
)

GOLDENS = Path("tests/goldens")


def _golden_shot_times(name: str) -> list[float] | None:
    path = GOLDENS / f"{name}.json"
    if not path.exists():
        return None
    doc = json.loads(path.read_text())
    return [float(e["t"]) for e in doc["events"] if e["type"] == "ShotTaken"]


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_clock_outcome(name: str) -> None:
    from cueclock.capture.clip import ClipReader
    from cueclock.vision.pipeline import VisionPipeline

    if not (CLIPS / name).exists():
        pytest.skip(f"clip {name} not recorded")
    truth, provenance = ground_truth(name, EXPECTED.get(name), _golden_shot_times(name))
    if truth is None:
        # Deliberately a skip, not a silent pass: the coverage gap is part of
        # the result, and #34 asks for it to be visible per clip.
        pytest.skip(f"no ground truth — {provenance}")

    reference = _reference_model(REFERENCES.get(name, "empty_table"))
    pipeline = VisionPipeline(_calibration(), reference)
    reader = ClipReader(CLIPS / name)
    indices = reader.detect_indices()
    frames = _detections(reader, CLIPS / name, pipeline, indices)
    timeline = replay_clock(
        name,
        pipeline,
        [float(t) for t in reader.timestamps],
        frames,
        indices,
        mid_rack=name not in BREAK_CLIPS,
    )

    settles_asserted = EXPECTED[name][2] is not None

    if name in BREAK_CLIPS:
        # No countdown to freeze — WNT §5 starts the window at the rest. The
        # only question is whether the break got counted, once.
        assert len(timeline.counts) == len(truth), (
            f"{name}: break counted {len(timeline.counts)}x, expected {len(truth)} "
            f"(counts at {[round(c, 2) for c in timeline.counts]})"
        )
        return

    if not truth:
        assert not timeline.counts, (
            f"{name}: nothing was struck, but the clock counted "
            f"{len(timeline.counts)} shot(s) at {[round(c, 2) for c in timeline.counts]}"
        )
        return

    # 1. Every real shot stopped the countdown, within budget.
    for stroke in truth:
        window = [
            f for f in timeline.freezes if stroke - FREEZE_EARLY_S <= f <= stroke + FREEZE_LATE_S
        ]
        assert window, (
            f"{name}: the stroke at {stroke:.3f}s never stopped the clock within "
            f"[-{FREEZE_EARLY_S}, +{FREEZE_LATE_S}]s — freezes at "
            f"{[round(f, 3) for f in timeline.freezes]}. The countdown ran through a "
            f"real shot; that is the failure this criterion exists to catch."
        )

    # 2. Counted exactly once each — the "extra is not" half.
    #
    # Gated on the clip containing its settles, because a shot is counted at
    # its settle and five corpus clips deliberately end while the balls are
    # still rolling (`medium_shot`, `power_shot`, `shaft_adjust`,
    # `drill_shot_0801`, `address_yank_strike_0801` — all `min_settles=None`
    # in EXPECTED, "settle count is unknowable"). That gate is read off the
    # existing table rather than decided here.
    if settles_asserted:
        lo = truth[0] - FREEZE_EARLY_S
        counts = [c for c in timeline.counts if c >= lo]
        assert len(counts) == len(truth), (
            f"{name}: clock counted {len(counts)} shots at "
            f"{[round(c, 2) for c in counts]}, ground truth has {len(truth)} at "
            f"{[round(s, 3) for s in truth]}. Extra counts are phantom shots — a "
            f"freeze on nothing, which no downstream policy can undo."
        )


def test_freeze_budget_is_anchored_on_the_measured_distribution() -> None:
    """Guard the constants against drifting back into taste.

    The budget came from `scripts/clock_budget_probe.py` over n=49 strokes:
    max lateness +0.019 s, max earliness -0.256 s. These bounds must keep
    clearing the measured extremes with margin; if someone tightens them to
    make a clip pass, this says so.
    """
    assert FREEZE_LATE_S >= 0.019 * 2, "late bound no longer clears the measured worst case"
    assert FREEZE_EARLY_S >= 0.256, "early bound now cuts inside the measured latch lead"
    assert FREEZE_LATE_S <= 0.5, "budget past the imperceptibility line #34 names"
