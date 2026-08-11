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
diagnostics, not the grade. Shot and settle counts, and the golden timeline,
are now **reported rather than asserted** on any clip this criterion can
grade; a golden diff there is drift to read, not a defect. Two things stay
hard assertions, because the clock grade cannot see them: **scratch counts**
(a Scratch verdict deliberately never moves the clock — #25/#26), and
everything on a clip with **no clock ground truth**, where the golden is the
only gate left.

Goldens are never regenerated to absorb this. On `stroke_tug_strike_0801` the
golden is the only surviving record of when that stroke was, and it is what
makes the clip gradeable at all.

**Late is fine, extra is not.** A missed strike is recoverable by design
since #33; an invented one is not, and no downstream policy can undo it.

The grading itself lives in `test_corpus_events.py::test_corpus_clip`, which
replays each clip ONCE and grades it twice — the replay is the whole cost of
the battery, and a second parametrized pass doubled it from 5 to 9 minutes
when this first landed. This module keeps the criterion's documentation and
guards its constants.

Known state (2026-08-11), so a reader is not surprised by red:

* ``stroke_tug_strike_0801`` — **green.** Its fire is gone and its golden
  still holds the 13.198 s stroke, yet the latch stops the clock 0.128 s
  *earlier* than that fire ever did and the settle counts it. Work item 2
  predicted exactly this: the clip was behaving correctly and the event
  grade was what was wrong about it. Its golden diff now reports as drift.
* ``parked_vote_strike_0801`` — **red, and correctly so.** Three counts
  against a recorded truth of two, with a phantom freeze at 10.36 s
  mid-clip. Extra is not fine, and no downstream policy can undo it. This is
  the one genuine defect the criterion leaves standing.
* ``pickup_snatch_0801`` — surfaced here, then **resolved as a clip artifact
  rather than a live defect**, on measurement. It counts a phantom shot at
  its settle, but its rearrangement is diffed against a **one-ball baseline**
  (the clip opens mid-action with no quiet layout), so it reads
  ``moved=1, appeared=6`` and misses #33 Part A's placement shape
  (``moved == 0 and involved == 1``). Two things settle it: every live
  ball-in-hand settle on record, with an anchored baseline, *does* match that
  shape and is correctly refused; and across the 111 live settles carrying
  ``appeared``, **every RUNNING settle at ap>=5 is a caught break** (all at
  ``rack_shots=1``) — the shape this clip produces has never occurred live.
  Bounded by three sessions, and stated as such. So the count assertion is
  gated on ``ClockTimeline.rearrangement_is_anchored`` rather than excusing
  this clip by name: ``parked_vote_strike_0801`` measures 7/7/7, stays
  graded, and stays red.
"""

from __future__ import annotations

from .clock_replay import FREEZE_EARLY_S, FREEZE_LATE_S


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
