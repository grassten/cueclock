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

import os
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pytest

from cueclock.vision.events import BallsSettled, Event, Scratch, ShotTaken

from . import goldens

if TYPE_CHECKING:
    from cueclock.capture.clip import ClipReader
    from cueclock.vision.calibration import TableCalibration
    from cueclock.vision.detect import Detection
    from cueclock.vision.pipeline import ReferenceModel, VisionPipeline

CLIPS = Path("tests/clips")
CALIBRATION = Path("config/table_calibration.json")

pytestmark = pytest.mark.skipif(
    not (CLIPS / "empty_table").exists() or not CALIBRATION.exists(),
    reason="corpus clips / local calibration not present",
)

# scenario -> (shots, scratches, min_settles) — None = don't assert.
# A fourth element, when present, is max_settles: the address/junk provokers
# must produce NO settle at all (a settle there is a phantom clock reset).
EXPECTED: dict[
    str,
    tuple[int | None, int | None, int | None]
    | tuple[int | None, int | None, int | None, int | None],
] = {
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
    # THE FOUL REVERSAL (2026-08-02, design.md §8). shaft_adjust and tip_bump
    # were pinned at 0 shots under the 2026-07-27 "v1 detects shots, not
    # fouls" rule. Both are fouls; a foul ends the visit; the incoming player
    # gets a fresh clock — so both must fire, exactly like the soft shot they
    # are indistinguishable from. That reversal is what unblocked #5: the two
    # constants making sub-impulse touches invisible were anchored on keeping
    # these two silent, not on any measurement of real shots.
    #
    # ball_in_hand_nudge is §9's "place by hand, then fine-tune a few inches
    # *with the shaft* (the legal way)" — the same physical act as
    # shaft_adjust, but legal, so its correct clock outcome is the opposite.
    # Vision cannot separate them (no ball-in-hand state), and Steven's call
    # was to fire on both and eat the rare wrong reset rather than add a key.
    # So **shots are deliberately unasserted here**: this clip happens not to
    # fire today, but only because the whiteness vote is parked on another
    # ball (the parked_vote_strike_0801 identity bug) — not because anything
    # separates it from shaft_adjust. Pinning 0 would claim a discriminator
    # that does not exist; pinning 1 would demand fixing an unrelated bug to
    # satisfy an outcome Steven already said he does not need either way.
    # The golden timeline still pins it exactly, so drift here stays visible.
    "ball_in_hand_nudge": (None, 0, None),
    "shaft_adjust": (1, 0, None),
    "tip_bump": (1, 0, 1),
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
    # Flag-key captures from the 2026-07-31 evening match-mode session — the
    # occluded-address / phantom-reset evidence (CHANGELOG checkpoint 26).
    # occluded_address: the player's address hides the cue ball long enough
    # that its track dies; the strike surfaces as a track born at 1572 mm/s.
    # Pins the newborn-cue strike path (was a 0-shot miss + a 60 s replay).
    "occluded_address_0731": (1, 0, 1, 1),
    # rack_gather: racking after Set up (the live "clock started while I
    # racked" trigger; the clock-side IDLE gate is the real fix — no shot may
    # be minted from racking motion).
    "rack_gather_0731": (0, 0, None),
    # soft_touch: a deliberate touch shot whose cue peaks at 225 mm/s raw.
    # This was the corpus's standing proof that the sub-impulse class was
    # unreachable — "waits on the acoustic corroborator", per the pin it
    # carried until 2026-08-02. It was never an acoustic problem: the impulse
    # floor sat at 250 solely to stay above this clip, because shaft_adjust
    # was pinned silent and the two are the same order of magnitude. With the
    # foul reversal (above) that anchor is gone, and the shot AND its settle
    # both land. This pin closes #5.
    "soft_touch_0731": (1, 0, 1),
    "soft_shot_0731": (1, 0, 1),
    # glove_address: bridge glove + watch on the cloth through an address; a
    # gear track reached 87% observed (past the flicker filter). The roster
    # rule keeps it from latching table motion: zero events, ever.
    "glove_address_0731": (0, 0, None, 0),
    # merge_yank: the addressed cue's blob merges with the stick tip and the
    # centroid drifts 31 mm. The detector merge flag (checkpoint 28) freezes
    # still balls through the merge: exactly one shot, one settle.
    "merge_yank_0731": (1, 0, 1, 1),
    # 2026-08-01 00:07-00:21 live captures (new-engine session) — the no-cue
    # strike evidence. The tracker can lose the cue outright (address
    # occlusion kills its track; regrab grace + whiteness floor + a dead
    # cue's ~0.15 s coasting phantom keep the flag empty through the strike):
    # the no-cue newborn scan carries the identity by birth conjunction.
    "no_cue_strike_0801": (1, 0, 1),
    # scratch_shot: same no-cue miss mechanism, plus a genuine scratch
    # (frame-verified: the cue leaves the table) — pins strike AND scratch
    # detection on one shot.
    "scratch_shot_0801": (1, 1, 1),
    # ball_in_hand_pickup: Steven picking the cue ball up (his own report).
    # The pickup discard (checkpoint 28) recognizes the episode — every ball
    # that latched motion died far from a pocket after a hand-scale drag —
    # and unlatches without a settle: zero events, ever.
    "ball_in_hand_pickup_0801": (0, 0, None, 0),
    # triage_0801b/c: address merge-yank phantom settle (b) and a hand pickup
    # near a pocket (c), each followed by a real, correctly detected shot.
    # The merge flag killed both phantoms: exactly one settle each.
    "triage_0801b": (1, 0, 1, 1),
    "triage_0801c": (1, 0, 1, 1),
    # 2026-08-01 morning flags (new-engine session, journal-correlated).
    # rack_place: the tail of a racking sequence after Set up. The live
    # phantom ShotTaken fired 12.7 s before the Flag press — outside the
    # then-12 s ring (which is why the ring is 20 s now) — so this pins only
    # that racking/placement motion stays event-silent. The phantom's clock
    # damage is covered by the Hub's IDLE break gate (sub-break impulses in
    # IDLE are ignored), unit-tested in test_server.
    "rack_place_0801": (0, 0, None, 0),
    # address_yank_strike: the two-sided merge-yank failure in one clip. The
    # stick merge yanks the addressed cue 47 mm (phantom settle live at
    # t~4.7 which reset the clock mid-address) AND wrecks the rest/still
    # state so the real strike 2 s later was missed live outright. With the
    # merge flag both sides invert: no settle from the yank, and the real
    # shot fires. Balls still roll at ring end, so settles stay min-None.
    "address_yank_strike_0801": (1, 0, None, 1),
    # break_0801: a real WNT 9-ball break the live IDLE gate wrongly ate
    # (the handoff underread — see test_break_0801_speed_clears_the_idle_gate
    # below). First break clip from the TV-era camera position.
    "break_0801": (1, 0, 1, 1),
    # 2026-08-01 midday drill flags (checkpoint 29).
    # stroke_tug: the final stroke tugs the addressed cue 12.04 mm — just
    # past the 12 mm axis-seed floor — and opens a candidate with a garbage
    # axis 0.1 s before impact. The axis check kills it as designed, but the
    # still-stamp was 0.01 s too stale for the documented re-open, and the
    # live shot was missed outright (clock ran through the shot, then reset
    # at settle). Pins the axis-kill re-open credit: exactly one shot, and
    # both settles are real (the prior shot's tail, then this one's).
    "stroke_tug_strike_0801": (1, 0, 2, 2),
    # hanger_pot: a real full-table pot of the 4 ball hanging at the
    # bottom-center pocket, shot from the top-left corner; the cue follows
    # and stops hanging in the jaw (Steven's ground truth — this clip was
    # first misread as a hand roll, corrected same day). The strike was
    # always detected; the clip pins the two real failures. (1) Premature
    # settle, FIXED (checkpoint 30): terminal track churn reborn the
    # still-rolling cue as a fresh id at the jaw and the age gate let
    # settle fire 1.3 s after the strike, mid-roll — young solidly-observed
    # movers now block stillness, and the settle lands at 14.56 s, after
    # the ball truly stops. (2) The Scratch was PHANTOM — the cue never
    # left the table, its track died inside the pocket zone because the
    # ball genuinely stopped there. FIXED 2026-08-02 (#6): the counter-
    # evidence was never missing, only late, so the verdict is deferred a
    # second and cancelled when a ball turns up at the death spot. Measured
    # here: a reborn track 0.24 s later, 33 mm away, converging to 12 mm.
    # Now 0 scratches, with the shot and settle unmoved to the millisecond.
    "hanger_pot_0801": (1, 0, 1, 1),
    # pickup_newborn: picking the resting cue up births a new track from
    # the hand+ball blob (the old track's merge-hold keeps eating the blob
    # until it slides out of the association gate). The carry was smooth
    # and fully observed — past both original carried-ball gates — but
    # every confirm observation was merged (8/8; real strikes measure 0%).
    # Pins the newborn merged-fraction gate: only the real shot fires.
    "pickup_newborn_0801": (1, 0, 1, 1),
    # jaw_strike: the cue hangs in the top-left jaws after a phantom-scratch
    # shot, untracked over the pocket mouth (the cue vote parks on a still
    # ball mid-table); the strike out of the jaws surfaces as a newborn at
    # 1294 mm/s. Live this was missed — Scratch had disarmed the newborn
    # path and no cue track existed to re-arm it (checkpoint 31 removed the
    # scratch disarm; test_shot_from_pocket_jaw_after_scratch_fires guards
    # the regression, since this cold replay never sees the scratch). The
    # pin guards the jaw-emergence detection itself.
    "jaw_strike_0801": (1, 0, 1, 1),
    # parked_vote_strike: two real shots from the 2026-08-01 match-day
    # session (the flag press followed the second). Shot 1 rolls the cue
    # into a cluster where it comes to rest against the blue; the merge
    # corrupts its color sample and the whiteness vote parks on the yellow
    # mid-table. Shot 2 strikes that rested cue out of the cluster (it
    # emerges from the stick occlusion at 619 mm/s) — both existing strike
    # paths were blind: the rest memory tracked the parked flag-holder
    # 578 mm away and the struck track was 11 s old, with the vote
    # returning only 0.6 s post-impact. Pins the own-rest (wrong-flag)
    # strike path. Live additionally missed shot 1 outright and delayed
    # its settle 9 s (state divergence a cold replay cannot reproduce; the
    # same wrong-flag configuration explains it and the same path covers
    # it). Cold truth: two clean shot -> settle pairs.
    "parked_vote_strike_0801": (2, 0, 2, 2),
    # 2026-08-01 evening drill flags (checkpoint 33) — eight ring dumps
    # from two sessions on the checkpoint-32 build, frame-verified against
    # the session journal. All reference empty_table_0801.
    # cue_grab: one real shot -> settle, then a gloved hand reaches in
    # (merged blob at 618-1921 mm/s), grabs the resting cue and lifts it
    # off the table. Live the grab fired a phantom rack shot through the
    # rest-anchored path (dwell/step/axis/travel all pass on a snatch) and
    # the 209.6 mm on-table drag beat the old 200 mm pickup ceiling,
    # minting a phantom settle too. Pins the hand-stamp carom guard and
    # the raised pickup travel ceiling: exactly one shot, one settle.
    "cue_grab_0801": (1, 0, 1, 1),
    # break_underread: a real break (frame-verified rack explosion) whose
    # live ShotTaken read 705 mm/s — the white streaked too fast to be
    # *observed*, and the IDLE break gate ate the rack's opening shot. A
    # cold replay never confirms this break at all (the streak dies before
    # the candidate can follow it): pinned as the honest gap, shots
    # unasserted. The kinematic speed bound (unit-tested) fixes the
    # live-shaped underread when the candidate does confirm.
    "break_underread_0801": (None, 0, 1),
    # lone_roller: a soft strike out of a white+brown touching pair; the
    # white rolls to the left cushion, rebounds and rests — the ONLY mover,
    # and its track is a newborn (not roster), so nothing could latch table
    # motion and the settle never came: the live clock froze in MOTION at
    # 10.43 s remaining for 33 s, absorbing the next real shot. Pins
    # fire-latches-motion: exactly one shot AND its settle.
    "lone_roller_0801": (1, 0, 1, 1),
    # break_0801b: a clean 7104 mm/s break. Live, a scatter ball reborn at
    # 906 mm/s fired a newborn ShotTaken 2.2 s into the scatter — the
    # roster movers had died in streak churn (mover clock stale, zero-ball
    # frames vacuously satisfied the pickup guards and unlatched the
    # episode). Pins the latched-table guard on the birth paths: exactly
    # one shot. Settles unpinned: the in-window settle lands in a
    # mid-scatter detection desert (balls still rolling, cold-only,
    # harmless post-break) — deferring it further would drop it out of the
    # window entirely.
    "break_0801b": (1, 0, None),
    # pair_strikes: two real strikes 9 s apart, each launched from a rest
    # against/near another ball. Shot 1's original track died mid-streak
    # while its continuation was untracked for a beat — the pickup discard
    # judged "every latcher died off-pocket under drag-scale travel" and
    # silently unlatched the live shot, eating its settle (clock frozen
    # 14 s, the second real strike absorbed mid-MOTION). Pins the
    # fired-episode discard suppression: two shot -> settle pairs.
    "pair_strikes_0801": (2, 0, 2, 2),
    # cluster_shot: a firm shot into a 3-ball cluster, scattered and
    # settled cleanly — cold, live and the frames all agree. Steven
    # flagged it live; nothing wrong is visible in journal or frames
    # (ground-truth question pending), so it pins the healthy behavior.
    "cluster_shot_0801": (1, 0, 1, 1),
    # pickup_snatch: PINNED KNOWN-WRONG (hand_roll precedent). A bare-hand
    # snatch of the resting white — Steven walks the top rail carrying it,
    # frame-verified — births a track that beats all three carried-ball
    # gates (observed, smooth, unmerged: bare fingers from above never
    # oversize the blob) and fires a phantom newborn shot. Silent hand
    # motion is the acoustic-corroborator class (BACKLOG); the settles are
    # real closures (post-snatch and post-placement quiet).
    "pickup_snatch_0801": (1, 0, 2, 2),
    # drill_shot: one real drill shot, fired late in a 41 s ring window —
    # the ring's frame rate halved while the previous flag dump was
    # writing (dump I/O starves the detect loop; see checkpoint 33). Live
    # this shot double-fired through two paths 0.05 s apart (rest-anchored
    # then newborn); the fire-latch closes the second path structurally.
    "drill_shot_0801": (1, 0, None),
    # settle_stall: the SETTLE STALL evidence (21:50 UTC flag, late in the
    # same evening session). One real shot at 6.90 s, every ball at rest
    # from ~9.7 s — and before the dwell chatter guard, NO settle for the
    # remaining 10 s of the clip: the stick lying across a parked orange
    # ball leaves a marginal half-ball diff blob whose centroid flips
    # between two modes 7-10 mm apart, and each 13-64 ms supra-creep-gate
    # excursion wiped the whole table's settle-dwell credit (live: settle
    # 13.7 s after the fire; 10 of that session's last shots clustered at
    # 12.0-16.3 s, and the same lottery is every session's latency tail).
    # Cold truth with the guard: the fire's settle lands at 10.23 s
    # (~0.5 s dwell after the creep-gate rest at 9.7), plus one warmup
    # settle at 1.29 s closing a cold-start episode (a founding-window
    # stick fragment genuinely travels ~26 mm and latches; live state was
    # long settled — ring-start artifact, same class as stroke_tug's
    # prior-shot tail). The timing itself is pinned by
    # test_settle_stall_settle_is_prompt below.
    "settle_stall_0801": (1, 0, 2, 2),
    # A real 1396 mm/s stroke on the rested replacement cue while the
    # whiteness vote parked on another white-ish ball (post-scratch
    # ball-in-hand rack). Address chatter used to refresh the ball's
    # translation stamp so the own-rest path never saw a dwell — no shot
    # fired live or cold (the settle-counts-the-shot fallback patched the
    # rack count 11 s later). First settle is the cold-start warmup at
    # 3.66 s; the second (15.16 s) is the shot's own. Timing pinned by
    # test_address_chatter_shot_fires_at_impulse below.
    "address_chatter_miss_0801": (1, 0, 2, 2),
    # 2026-08-02 evening flags (checkpoint 48) — two `flag_missed` dumps,
    # 2.5 min apart in one session. Ground truth from the table: one real
    # struck shot each, no scratch, balls settle in-clip. Live, NEITHER
    # fired a ShotTaken: the clock never froze, ran down through the shot
    # (5.3 s left on the first) and only jumped back to 30 when the settle
    # landed via the counts-the-shot fallback. Shoot at 1 s remaining and
    # the buzzer fires on a legal shot — a false violation, not cosmetic.
    #
    # stroke_gear_veto: pins the gear-behind exemption. Before it, the
    # carom guard vetoed the strike. The vetoing stamp is not a ball: a
    # transient non-roster blob appears 0.3 s BEFORE the impulse on empty
    # cloth 138 mm behind the cue's rest spot, flickering merged and
    # impulse-fast, so the hand-stamp rule (merged + non-roster + fast)
    # marks it and the rest-anchored cue path refuses the strike. It is
    # the player's own stroke gear. The cue then departs *away* from it
    # (rest 1443.7,675.5 -> 1348,666 while the stamp sits at 1528,783) —
    # the discriminator distance no longer provides: 157.7 deg here
    # against cue_grab_0801's 61.5 deg. §2.3's anchor for this guard was
    # gear at 271 mm (medium_shot) / 340 mm (power_shot) from the rest
    # spot; these live strokes put it at 138 mm, and cue_grab_0801 — the
    # phantom the stamps exist to reject — sits at 116 mm. 22 mm between
    # accept and reject is not a threshold, so the guard now reads
    # direction for hand stamps. Shot fires at 13.04 s.
    "stroke_gear_veto_0802": (1, 0, 1, 1),
    # stroke_gear_margin: the SAME mechanism, which before the exemption
    # survived cold by ONE FRAME — gear stamps vetoed at 12.097 s and
    # 12.110 s and the candidate opened at 12.123 s, 13 ms (one frame at
    # 78 Hz) after the last stamp aged out of the 0.4 s window. That it
    # ever passed was luck of timing, not a working guard; the exemption
    # (gear at 151.7 mm, 161.9 deg behind the departure) is what holds it
    # now. Second independent measurement of the stroke-gear geometry.
    # Its LIVE failure is a different bug and this cold pin does not
    # cover it: warm replay (state.pkl is in the clip) shows the cue's
    # identity had drifted — the vote's rest sat at (1325.4,719.1) while
    # the real struck cue ran as non-cue track 100, which closed to 26 mm
    # (inside a ball diameter, so not a second ball) and vetoed the
    # strike as a kiss. The cue self-vetoed. Cold replay rebuilds
    # identity from scratch and cannot reproduce it.
    "stroke_gear_margin_0802": (1, 0, 1, 1),
}

# Clips pair with the empty-table reference from the same camera position:
# post-TV-install clips use empty_table_0729, everything earlier the original;
# clips from 2026-08-01 midday onward use empty_table_0801 (the §E2 refresh
# after the drift warning — same mount, new lighting state).
REFERENCES: dict[str, str] = {
    "empty_table_0729": "empty_table_0729",
    "shot_debug": "empty_table_0729",
    "missed_strike_0731": "empty_table_0729",
    "live_divergence_0731a": "empty_table_0729",
    "live_divergence_0731b": "empty_table_0729",
    "occluded_address_0731": "empty_table_0729",
    "rack_gather_0731": "empty_table_0729",
    "soft_touch_0731": "empty_table_0729",
    "soft_shot_0731": "empty_table_0729",
    "glove_address_0731": "empty_table_0729",
    "merge_yank_0731": "empty_table_0729",
    "no_cue_strike_0801": "empty_table_0729",
    "scratch_shot_0801": "empty_table_0729",
    "ball_in_hand_pickup_0801": "empty_table_0729",
    "triage_0801b": "empty_table_0729",
    "triage_0801c": "empty_table_0729",
    "rack_place_0801": "empty_table_0729",
    "address_yank_strike_0801": "empty_table_0729",
    "break_0801": "empty_table_0729",
    "stroke_tug_strike_0801": "empty_table_0729",
    "hanger_pot_0801": "empty_table_0729",
    "pickup_newborn_0801": "empty_table_0729",
    "jaw_strike_0801": "empty_table_0729",
    "parked_vote_strike_0801": "empty_table_0801",
    "cue_grab_0801": "empty_table_0801",
    "break_underread_0801": "empty_table_0801",
    "lone_roller_0801": "empty_table_0801",
    "break_0801b": "empty_table_0801",
    "pair_strikes_0801": "empty_table_0801",
    "cluster_shot_0801": "empty_table_0801",
    "pickup_snatch_0801": "empty_table_0801",
    "drill_shot_0801": "empty_table_0801",
    "settle_stall_0801": "empty_table_0801",
    "address_chatter_miss_0801": "empty_table_0801",
    "stroke_gear_veto_0802": "empty_table_0801",
    "stroke_gear_margin_0802": "empty_table_0801",
}


@cache
def _reference_model(name: str) -> "ReferenceModel":
    """Built once per reference clip per process — it costs ~0.3 s each."""
    from cueclock.capture.clip import ClipReader
    from cueclock.vision.pipeline import ReferenceModel

    reader = ClipReader(CLIPS / name)
    indices = np.linspace(0, len(reader) - 1, num=15, dtype=int)
    return ReferenceModel.build([reader.frame(int(i)) for i in indices])


@cache
def _calibration() -> "TableCalibration":
    from cueclock.vision.calibration import TableCalibration

    return TableCalibration.load(CALIBRATION)


def _run_clip(name: str, reference: "ReferenceModel") -> list[Event]:
    """Replay one clip at the live detect cadence and collect its events.

    The two pipeline halves are run as two passes rather than interleaved:
    `detect` is stateful only through the previous frame's gray and `advance`
    only through the tracker/engine, so the split is behaviour-preserving —
    and it is what lets the detector pass be served from disk when
    `--detect-cache` is on (see `_detections`).
    """
    from cueclock.capture.clip import ClipReader
    from cueclock.vision.pipeline import VisionPipeline

    pipeline = VisionPipeline(_calibration(), reference)
    reader = ClipReader(CLIPS / name)
    indices = reader.detect_indices()
    frames = _detections(reader, CLIPS / name, pipeline, indices)
    events: list[Event] = []
    for i, (detections, churn) in zip(indices, frames, strict=True):
        events += pipeline.advance(float(reader.timestamps[i]), detections, churn)
    return events


def _detections(
    reader: "ClipReader", clip_dir: Path, pipeline: "VisionPipeline", indices: list[int]
) -> list[tuple[list["Detection"], list[float | None]]]:
    """Detector output for `indices` — from disk when `--detect-cache` is on.

    Cold (the default) is the fidelity gate: it exercises the detector against
    real pixels. Cached is the iteration path for event/tracker work, where
    the detector provably cannot have changed — the key would differ if it had.
    """
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
    events = _run_clip(name, _reference_model(ref_name))
    shots = sum(isinstance(e, ShotTaken) for e in events)
    scratches = sum(isinstance(e, Scratch) for e in events)
    settles = sum(isinstance(e, BallsSettled) for e in events)
    summary = f"{name}: {[f'{type(e).__name__}@{e.t:.2f}' for e in events]}"
    print(summary)

    # The golden runs in the same test as the counts on purpose: the replay is
    # the whole cost (~5 min cold for the battery), so a separate parametrized
    # golden test would double it to grade the same events twice.
    #
    # Before the count assertions, not after — so that `--update-goldens`
    # records the timeline even for a clip whose counts are currently red.
    # That is exactly the case during a deliberate expectation change, when
    # seeing what actually fired is the whole point.
    goldens.check(name, ref_name, events)

    expected = EXPECTED[name]
    exp_shots, exp_scratches, min_settles = expected[:3]
    max_settles = expected[3] if len(expected) == 4 else None
    if exp_shots is not None:
        assert shots == exp_shots, summary
    if exp_scratches is not None:
        assert scratches == exp_scratches, summary
    if min_settles is not None:
        assert settles >= min_settles, summary
    if max_settles is not None:
        assert settles <= max_settles, summary


def test_detect_cache_replay_matches_cold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--detect-cache` must not move a single event, on real pixels.

    tests/unit/test_detect_cache.py proves the container round-trips exactly;
    only a real clip proves the *key* is complete and that splitting
    `process()` into detect/advance is behaviour-preserving. spin_in_place is
    the clip to do it with: its settle is gated on appearance churn, the one
    cached field that is derived across frames rather than within one, so a
    mis-packed churn column moves its settle instead of failing loudly.
    """
    clip = "spin_in_place"
    if not (CLIPS / clip).exists():
        pytest.skip(f"clip {clip} not recorded")
    reference = _reference_model(REFERENCES.get(clip, "empty_table"))

    # The cold leg must be genuinely cold even when the whole suite is run
    # with --detect-cache, or this degenerates to comparing the cache to
    # itself — which would pass while proving nothing.
    monkeypatch.delenv("CUECLOCK_DETECT_CACHE", raising=False)
    cold = _run_clip(clip, reference)
    monkeypatch.setenv("CUECLOCK_DETECT_CACHE", str(tmp_path / "cache"))
    built = _run_clip(clip, reference)  # miss: computes, then stores
    cached = _run_clip(clip, reference)  # hit: served entirely from disk

    assert list((tmp_path / "cache").glob("*.npz")), "nothing was cached"
    assert built == cold, "the miss path diverged from a cold replay"
    assert cached == cold, "the hit path diverged from a cold replay"


def test_break_0801_speed_clears_the_idle_gate() -> None:
    """The IDLE break gate must never eat a real break — live, it did once.

    2026-08-01 12:06: the gate read 2072 mm/s on Steven's break and held the
    clock in IDLE. The candidate had advanced on the dying original track's
    remnant while the reborn cue measurably flew 5300-5500 mm/s — on a
    handoff strike the target's own raw underreads badly. ShotTaken now
    carries the fastest observed ball in the candidate window; this replays
    that exact break and pins the reported speed above the floor.

    The floor moved to `clock/modes.py` in checkpoint 45 — it is a *match*
    rule, and `run_out` has none because it has no break to gate.
    """
    from cueclock.clock.modes import MATCH_BREAK_FLOOR_MMS

    if not (CLIPS / "break_0801").exists():
        pytest.skip("clip break_0801 not recorded")
    events = _run_clip("break_0801", _reference_model("empty_table_0729"))
    shots = [e for e in events if isinstance(e, ShotTaken)]
    assert len(shots) == 1
    assert shots[0].speed_mms is not None
    assert shots[0].speed_mms >= MATCH_BREAK_FLOOR_MMS, f"break read {shots[0].speed_mms:.0f}"


def test_spin_in_place_settle_waits_for_spin_stop() -> None:
    """WPA def. 8.2: the shot ends when the ball stops moving AND spinning.

    Ground truth measured from the clip's own pixels (2026-07-28): the cue's
    appearance churn reaches the still-ball noise floor — visible spin stop —
    at t≈7.0 s. Without the churn gate, settle fired at 6.48 s, mid-spin.
    """
    if not (CLIPS / "spin_in_place").exists():
        pytest.skip("clip spin_in_place not recorded")
    events = _run_clip("spin_in_place", _reference_model("empty_table"))
    settles = [e.t for e in events if isinstance(e, BallsSettled)]
    assert settles, "spin_in_place must settle before the clip ends"
    assert settles[-1] >= 7.0, f"settled at {settles[-1]:.2f}s — before spin stopped"


def test_settle_stall_settle_is_prompt() -> None:
    """The shot's settle must land a dwell after rest, not a chatter lottery.

    settle_stall_0801: the stroke fires at 6.90 s and the table is at rest by
    the creep-gate measure at 9.7 s (last genuine supra-gate motion; all
    velocities < 6 mm/s from ~10.1). A parked ball under the stick throws
    5-10 mm single-mode-flip excursions every ~0.2-0.3 s for the rest of the
    clip; before the dwell chatter guard each one wiped the accumulated
    settle dwell and NO settle fired in 10 s of quiet (live: 13.7 s late,
    session cluster 12.0-16.3 s). With the guard the settle lands at 10.23 s.
    The window pinned here is rest (9.7) to rest + two dwells (10.7): late
    means the chatter lottery is back, absent means the stall is back.
    """
    if not (CLIPS / "settle_stall_0801").exists():
        pytest.skip("clip settle_stall_0801 not recorded")
    events = _run_clip("settle_stall_0801", _reference_model("empty_table_0801"))
    settles = [e.t for e in events if isinstance(e, BallsSettled)]
    post_shot = [t for t in settles if t > 6.9]
    assert post_shot, "the shot's settle never fired — the settle stall is back"
    assert 9.7 <= post_shot[0] <= 10.7, f"settle at {post_shot[0]:.2f}s — outside rest+2 dwells"


def test_address_chatter_shot_fires_at_impulse() -> None:
    """The own-rest strike must open off the impulse, not off vote recovery.

    address_chatter_miss_0801: the cue steps to 1396 mm/s at 10.13 s
    (clip-relative) from a 13 s rest while the whiteness vote is parked on
    another white ball, and address chatter throws sub-window excursions
    through the rest. Before the translation-stamp persistence rule the
    own-rest dwell gate read ~0 rest and no shot ever fired (live or cold);
    with it the fire confirms at 10.25 s. The window pinned here is impulse
    to impulse + ~1 s of confirm: later means the path is riding the vote's
    recovery (which took 2 s live) instead of the ball's own rest anchor.
    """
    if not (CLIPS / "address_chatter_miss_0801").exists():
        pytest.skip("clip address_chatter_miss_0801 not recorded")
    events = _run_clip("address_chatter_miss_0801", _reference_model("empty_table_0801"))
    shots = [e.t for e in events if isinstance(e, ShotTaken)]
    assert shots, "the stroke never fired — the address-chatter miss is back"
    assert 10.0 <= shots[0] <= 11.2, f"shot at {shots[0]:.2f}s — not anchored on the impulse"
