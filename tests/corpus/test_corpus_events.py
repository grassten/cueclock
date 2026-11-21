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
import warnings
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pytest

from cueclock.server import hub as hub_module
from cueclock.vision.events import BallsSettled, Event, Scratch, ShotTaken

from . import goldens
from .clock_replay import (
    BREAK_CLIPS,
    FREEZE_EARLY_S,
    FREEZE_LATE_S,
    ClockTimeline,
    golden_shot_times,
    ground_truth,
    replay_clock,
)

if TYPE_CHECKING:
    from cueclock.capture.clip import ClipReader
    from cueclock.vision.ball_list import BallList
    from cueclock.vision.calibration import TableCalibration
    from cueclock.vision.detect import Detection, Intrusion
    from cueclock.vision.pipeline import ReferenceModel, VisionPipeline


class StrikeMiss(UserWarning):
    """A real stroke the strike layer never fired on (#34).

    Not a failure: the motion latch stopped the clock anyway, which is the
    same outcome ~0.25 s later. Counted rather than asserted so the
    strike-fire rate — the leading indicator for the tracker substrate —
    stays visible on a green run.
    """


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
    # all_balls_still_1115: all 16 balls standing still on the current mount
    # (#51's done-when). Nothing moves, so nothing may fire and nothing may
    # settle. Every ball is its own detection on every frame since #56
    # (`test_every_ball_is_its_own_detection`); checkpoint 111 read 11.
    "all_balls_still_1115": (0, 0, None, 0),
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
    # walk_around_settle: `docs/design.md`'s corpus checklist scripted this one
    # as "shoot, then walk around the table leaning over it while balls are
    # still settling" — so one real shot, by intent recorded before any code
    # saw the clip. It carried no shot count until 2026-08-06 (an oversight,
    # not a judgement: the checklist was never transcribed into this table),
    # which left the golden as its only pin. Steven confirmed from the clip
    # that day: a real shot, timing accurate, and the obstruction — leaning
    # over and swinging the cue through the settling balls — was deliberate.
    "walk_around_settle": (1, 0, 1),
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
    # rack_gather: racking after New Rack (the live "clock started while I
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
    # soft_shot_0731: the object ball is the blue 2, and it rolls to the end
    # of the clip (traced frame by frame, 6.5 s -> 12.0 s). The settle at
    # 8.63 s that this pinned until checkpoint 110 fired while the 2 was
    # invisible to the shadow gate (#51); there is no settle in-clip.
    "soft_shot_0731": (1, 0, None),
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
    # rack_place: the tail of a racking sequence after New Rack. The live
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
    # 2026-08-23 live flag, promoted in checkpoint 104. Steven's ground truth:
    # one real shot at ~15.1 s, and **he never touched a ball by hand** all
    # clip. It pots the object ball, so that ball vanishes and never arrives
    # and the cue is the settle's ONLY arrival — and he is still leaning over
    # the table where the cue stopped, so that one arrival reads
    # hand-delivered. Live, #41 refused it on `arrivals 1 / hand 1`: the
    # countdown ran on from 16.14 s into GRACE and EXPIRED and he restarted by
    # hand.
    #
    # This is the negative control the corpus was missing, and its absence is
    # why a ball-in-hand refusal shipped twice. `CARRY_CLIPS` grades the
    # carries; nothing graded *a pot whose only arrival is hand-covered*, which
    # is the population both #38 and #41 selected wrongly. Any third attempt
    # has to keep this green as well as the carries — and measurement says it
    # cannot, which is the point.
    "pot_single_arrival_0823": (1, 0, 1, 1),
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
    # *observed*, and the IDLE break gate ate the rack's opening shot. For a
    # week this was pinned as an honest gap with shots unasserted, because a
    # cold replay never confirmed the break at all. Checkpoint 67 closed it:
    # the streak did not "die", it outran the handoff radius while invisible
    # (born 305 mm off the rest at 6977 mm/s) and was then killed a second
    # time by the observation floor as it buried itself in the rack. It now
    # fires at 1.075 s reading 9469 mm/s — 3.8x the IDLE break floor. Asserted
    # hard now: this is the shot the live gate ate.
    "break_underread_0801": (1, 0, 1),
    # lone_roller: a soft strike out of a white+brown touching pair; the
    # white rolls to the left cushion, rebounds and rests — the ONLY mover,
    # and its track is a newborn (not roster), so nothing could latch table
    # motion and the settle never came: the live clock froze in MOTION at
    # 10.43 s remaining for 33 s, absorbing the next real shot. Pins
    # fire-latches-motion: exactly one shot AND its settle.
    "lone_roller_0801": (1, 0, 1, 1),
    # break_0801b: NOT a break, and filed as one for three months. The
    # nine-ball rack stands untouched through all 20.5 s; at 14.6 s Steven
    # picks the cue ball up from the head of the table, carries it round the
    # left rail and sets it down again at 18.5 s. The 7104 mm/s "strike" is
    # his hand taking the ball. He flagged it because the clock started
    # before he broke; the triage read the journal line (a fire out of IDLE
    # at break speed) and never looked at the frames. Frames read
    # 2026-11-18, after the same false start in session 20261119-020755.
    # The event stream is unasserted except through its golden: the fire
    # itself is the tracker's and still happens (what it pinned, exactly one
    # fire, the golden still pins). What the clock must do with it is in
    # FALSE_STARTS below.
    "break_0801b": (None, 0, None),
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
    # pickup_snatch: a bare-hand snatch of the resting white — Steven walks
    # the top rail carrying it, frame-verified. It birthed a track that beat
    # all three carried-ball gates and fired a phantom newborn shot, plus the
    # phantom settle closing that phantom's episode. It was pinned KNOWN-WRONG
    # at (1, 0, 2, 2) and written off as the acoustic-corroborator class.
    #
    # FIXED 2026-08-02 (#8). The gates were never wrong — the confirm was too
    # fast to use them. `newborn_raw_step_ratio_max` catches this carry
    # outright (ratio 2.24 at age 0.096 s) but the candidate confirmed at
    # 0.070 s, one frame earlier. A minimum confirm time for birth-path
    # candidates is the entire fix. Now 0 shots, and the phantom settle goes
    # with it: only the genuine post-placement quiet at 29.65 remains.
    "pickup_snatch_0801": (0, 0, 1, 1),
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
    # 2026-08-05 morning flag (checkpoint 64) — a `flag_missed` dump, match
    # mode. Ground truth from the table: Steven took a shot, and vision never
    # saw it. Live the clock ran down through the stroke from 30 to 10.95 and
    # he took an extension to avoid a false violation; the detect loop was
    # healthy at 76-78 Hz the whole time, so this is not starvation.
    #
    # The cue's own track froze at its rest spot and KEPT the whiteness vote
    # while the ball was struck and gone: the tracker holds a still track
    # through merged blobs and coasts it for still_coast_s (3 s), the struck
    # ball re-emerged 171 mm away — past the 100 mm association gate — as a
    # new track, and the frozen phantom's stale whiteness outranked the live
    # one for 2.8 s. So the flag-borne handoff never evaluated the real ball,
    # and cold replay produced **zero events over the whole 20 s**.
    #
    # This is the first cold-reproducible member of the identity-drift class
    # stroke_gear_margin_0802 could only show warm. Pins the cue's remembered
    # rest as the scan's third identity anchor: one shot, one settle.
    "phantom_flag_strike_0805": (1, 0, 1, 1),
    # 2026-08-05 afternoon flags (checkpoint 66) — three `flag_missed` dumps in
    # one session, three real shots vision never saw. Ground truth from the
    # table: a hard/long shot and two normal-pace pots. The detect loop was
    # healthy at 77-78 Hz right up to each press, so none of these is
    # starvation. All three journal the same signature — a BallsSettled with no
    # ShotTaken before it, and rack_shots incremented by clock/state.py's
    # missed-strike compensation, which is the engine's own marker for exactly
    # this. Live the clock never froze: it counted down through the balls
    # rolling and then reset on the settle.
    #
    # The strike gate opens correctly at the impulse and then the tracker loses
    # the ball: the struck cue outruns the 100 mm association gate and founds a
    # NEW track, while the candidate advances on extrapolated positions that
    # can never confirm. Holding that single slot is what blinds the engine —
    # the identity scans need it while the struck ball is still inside
    # strike_handoff_radius_mm of the rest, and by the time the candidate dies
    # the ball is 469 mm out (here) and 292 mm out (late_emergence_strike).
    # Pins the shelve: a candidate riding a coasting track releases the slot to
    # the handoff. Hard shot, so the cue crosses fast — one shot, one settle.
    "phantom_candidate_block_0805": (1, 0, 1, 1),
    # Same session, a normal-pace pot — and a different blindness. The vote was
    # parked on an *observed, still* ball (the object ball the cue was about to
    # pot) while the struck cue emerged from the address occlusion as a track
    # born already at 1752 mm/s. An observed flag-holder used to disable the
    # birth path outright, and _maybe_open_rested cannot cover it because a
    # newborn is not in the roster. Pins that the birth path runs with the
    # holder observed, and that it confirms on travel when it does.
    "parked_vote_newborn_0805": (1, 0, 1, 1),
    # Same session, the pot straight after — the block again, but this one also
    # pins the step gate. The abandoned cue track drifted instead of being held
    # still, so _cue_last_still_t froze and the handoff's ramp test started
    # billing the occlusion: 0.128 s against the frozen stamp, refused by 8 ms,
    # where the cue had actually been seen 0.109 s earlier. Measured from the
    # last observation it passes, and the strike lands 0.31 s after the impulse.
    "late_emergence_strike_0805": (1, 0, 1, 1),
    # break_outrun: Steven's first break of the next session, flagged at the
    # table. A normal-power break (his words) that the clock sat straight
    # through — IDLE at 60.0 for the whole rack, no ShotTaken in the journal
    # at all, so the break gate never even got a vote to reject; he had to
    # press Restart to start the match. Detection was healthy throughout
    # (77-78 Hz, uniform 12.8 ms frame gaps in the ring), so nothing here is
    # contention.
    #
    # Two gates refused it in series, and fixing either alone changes nothing.
    # The cue left its rest at 4.724 s, was never observed again, and its
    # continuation was born three frames later 320 mm out at 7221 mm/s — past
    # a 250 mm radius anchored on a 2281 mm/s emergence. Widen that and the
    # candidate opens, travels 1483 mm, and is killed at 0.20 s by the
    # observation floor: by then the cue is inside the rack it just scattered
    # and reads 0.56 observed, worse than the hand-grab the floor exists to
    # reject. Pins both — one shot, at break speed, from a rack explosion.
    "break_outrun_0805": (1, 0, 1, 1),
    # 2026-08-06 live session, first outing of the annulus identity. Three
    # flagged misses, three distinct mechanisms — checkpoint 74.
    #
    # A ring dump: the stored frames are exactly the ~72 Hz the live loop
    # processed, so replay is frame-for-frame what the session saw.
    #
    # The break the clock sat through (manual Restart at the table). The cue
    # crossed 1390 mm to the rack entirely unobserved — its "departure" was
    # one stick-smeared 2441 mm/s step — and the reborn cue was crowned at
    # the rack within 2 frames, so the flag-switch kill rule ate the
    # rest-anchored candidate and every re-qualification refused: gain test
    # (2.48x, above pickup_snatch's 2.37x carry — the denominator is blur
    # garbage), newborn (table-motion latch, correctly), handoff (poisoned
    # obs_t: the stick blob impersonated the resting cue after the ball
    # left). Until checkpoint 75 the golden pinned the DESIRED in-flight
    # fire (10.139 s at 6503 mm/s — the removed eruption restore's numbers)
    # and stood deliberately red. Retired with Steven's sign-off,
    # 2026-08-06 (§13.5(3)): the product no longer needs the engine to
    # catch this break in flight — the honest late fire the engine does
    # produce (14.107 s at 443 mm/s, floor-eaten live) plus the involved-12
    # settle is exactly what the hub's E8 adjudication converts into the
    # counted break, and test_server.py pins that conversion with this
    # clip's own numbers. The golden now pins today's actual events, so
    # drift stays visible; catching the flight in-engine remains E7 work
    # and would surface here as the fire moving earlier/faster.
    "break_dark_flight_0806": (1, 0, 1, 1),
    # The fully dark break (flag_missed_4, 2026-08-06 18:08Z; Steven's ground
    # truth: a full-power break off a fresh 15-ball rack, whole flight
    # in-ring at ~70 Hz). Cold, the racked pack never confirms a single track
    # — one confirmed ball on the whole pre-break table — so when the scatter
    # erupts into 12-18 fast newborns, none has roster tenure and the engine
    # never latches motion: zero events, warm and cold. Ground truth is ONE
    # shot; `shots` stays unasserted (not 0 — that would canonize the miss)
    # until the strike class is solved at the tracker layer (accuracy plan
    # §13.6). What IS pinned: exactly one settle — the pipeline's
    # quiet-rearrangement backstop, the identity-free layer that certifies
    # "the quiet layout is not the layout that was quiet before" when every
    # track-level channel slept. The named backstop test pins its mechanism.
    "break_dark_total_0806": (None, 0, 1, 1),
    # A soft-medium positional shot (~450 mm/s roll, 560 mm) missed live.
    # The cue emerged from a long address occlusion as a stick-merge split
    # whose centroid reads slow — first honest raw at age 0.179, debounced
    # crown at 0.31 — while the tight newborn age gate (calibrated on clean
    # corpus emergences at 0.012 s) refused it and a parked stick-retraction
    # blob squatted the slot. Pins the dead-rest age budget
    # (newborn_dead_rest_max_age_s): the crown was born into a rest story
    # already 7.6 s dead, so the fallback gets the time the emergence needs.
    "occluded_roll_0806": (1, 0, 1, 1),
    # A shot out of a frozen cue-object cluster, missed live twice over: the
    # struck cue walked away with the track id of the cluster ball beside it
    # (an id that had been present-not-observed for 176 s — the merged blob
    # is handed around its members, so no member can ever show the roster's
    # observed fraction), and the ghost of the address blob kept the crown
    # and the still-stamp at the vacated rest. Pins cluster-resident roster
    # admission and the presence-witnessed dwell of _maybe_open_rested (the
    # departure run must be pixel-witnessed; the dwell may be presence-
    # witnessed).
    "cluster_rest_shot_0806": (1, 0, 1, 1),
    # 2026-08-11 live flags, Steven's own ground truth (#39). Both scratches
    # went into the SAME corner and both died on the zone margin: the cue's
    # last observed position was 123 mm and 124 mm from the pocket centre
    # against a 120 mm zone, so neither reached the #26 adjudicator and
    # neither held the clock. On the pot the cue was tracked marching in —
    # 164 -> 148 -> 134 -> 123 mm over six frames at ~850 mm/s — i.e. the
    # track ended one frame short of the zone. Pins the one-frame flight
    # allowance in `_near_pocket`; before it, both clips emitted zero scratch
    # events of any kind.
    #
    # pot_scratch_nohold: an ordinary pot that scratched. Steven: "the scratch
    # delay didn't apply, clock started immediately."
    "pot_scratch_nohold_0811": (1, 1, 1, 1),
    # kick_scratch: kicked at a ball, hit nothing, cue scratched. Until #56
    # the verdict never landed inside the clip: the census confirms only when
    # every roster ball is observed, and a touching pair near the cue's path
    # was one merged still blob, which the tracker reports unobserved. Split,
    # both are observed and the Scratch lands at the candidate (16.00 s).
    "kick_scratch_0811": (1, 1, 2, 2),
    # #40, same session: a real shot at 2047.8 mm/s on which the engine
    # emitted **nothing at all** — no fire, no latch, no settle, 0 events in
    # 20.74 s — because the struck cue was reborn as a new track at the
    # impact and only *roster* tracks could latch motion. Live the countdown
    # ran straight through the stroke. Shots stay unasserted deliberately:
    # the strike layer is still blind here (the crown sat on the cue's dead
    # identity throughout), and #34's criterion is the clock, not the fire —
    # `test_latch_blind_shot_stops_the_clock` below is the real pin. What
    # this entry adds is the settle that was missing: exactly one, so a
    # future latch that fires twice on the same stroke shows up.
    "latch_blind_shot_0811": (None, 0, 1, 1),
    # phantom_still_down: THE PHANTOM, live and mid-rack (#37). 2026-08-11
    # 18:30:20Z, `flag_phantom`, session t=386.36. Steven's ground truth:
    # nothing happened — he was still down on the ball. The engine latched a
    # motion episode at session 384.71, the clock froze, and the settle 1.65 s
    # later counted a shot and reset the countdown 19.7 -> 30 s on
    # `involved=0 moved=0 appeared=0 vanished=0`, one surviving mover at
    # 26 mm. His real stroke follows 1.6 s afterwards and is detected normally
    # (3064 mm/s).
    #
    # A cold replay reproduces the live journal event for event — the same
    # settle at clip 10.503 with the same zero rearrangement, the same fire
    # speed to the millimetre, the same second settle at 18.920 reading
    # involved=3 — so this is the live failure itself, not a replay artifact.
    # What the journal could NOT say, and the replay does, is that the zero
    # was real: the diff was taken against a genuine 4-ball quiet layout, not
    # the `_settled_baseline or present` fallback. That fact is what #37's
    # rejection stands on, and it is what retires the claim the fail-open
    # branch used to carry — "involved == 0, a clip-start artifact shape,
    # never a measured placement".
    #
    # Two settles, one shot. The phantom settle still FIRES: vision emits
    # evidence and the hub applies policy (§13.1), so the event counts are
    # unchanged and what moved is only what the clock did with them. That is
    # pinned by `test_phantom_still_down_is_rejected_at_the_settle`, because
    # the EXPECTED grade structurally cannot see it — the phantom precedes the
    # clip's only real stroke, and `_assert_clock_outcome` ignores counts
    # before the first ground-truth stroke by design.
    "phantom_still_down_0811": (1, 0, 2, 2),
    # The three 2026-08-11 ball-in-hand carries (#41), Steven's own ground
    # truth: he moved the cue ball by hand mid-rack and the clock reset and
    # advanced the rack every time. Each clip holds a real shot AND at least
    # one carry, recorded minutes apart in one session — the controlled pair
    # the corpus never had.
    #
    # `shots` is deliberately unasserted on all three, and that is a statement
    # about the strike layer, not about the fix. A carry fires an honest-
    # looking ShotTaken (16 274 mm/s on carry, 1058 on carry_b — track-jump
    # artifacts, the dead-end #38 recorded), so the fire count here is 2, 2
    # and 1 against one real stroke each. Pinning it would either canonize the
    # phantom fires or demand a strike-layer fix this issue is not. What
    # matters is what the CLOCK did, and since #33 those two are deliberately
    # different things — so the outcome is pinned by
    # `test_0811_carries_do_not_reset_the_clock` below, which is the actual
    # done-when. Settles are pinned exactly (a carry still settles; it must
    # just not count), and the golden keeps the event stream visible.
    "ball_in_hand_carry_0811": (None, 0, 2, 2),
    "ball_in_hand_carry_b_0811": (None, 0, 2, 2),
    # triple: the middle settle (14.74 s) fires with the cue still in Steven's
    # hand (frames 14.4-14.9 s, checked at #56): the hand's blob drags the
    # coasting track of the orange ball it reaches past (446 -> 487 mm; the
    # orange never moves) and that episode settles mid-carry. Pinned as the
    # current behaviour, not endorsed.
    "ball_in_hand_triple_0811": (None, 0, 3, 3),
}

# #41: the settle each clip's ONE real stroke produced, and the settles that
# are hand carries. Steven's ground truth, matched to
# `logs/session-20260811-182341.jsonl` through the dumps' state.json
# (t_clip = t_session - snapshot_t + (snapshot_ns - first_ns)/1e9):
#
#   ball_in_hand_carry_0811    shot 7.905 (s=647.38)   carry 15.425 (s=654.90)
#   ball_in_hand_carry_b_0811  shot 10.06 (s=1487.50)  carry 13.345 (s=1490.79)
#   ball_in_hand_triple_0811   shot 12.455 (s=1134.74) carries 14.740, 16.040
#
# The triple is the rack 9 -> 12 episode: three settles inside 3.6 s counted
# three shots for one real stroke plus two carries.
CARRY_CLIPS: dict[str, list[float]] = {
    "ball_in_hand_carry_0811": [7.905],
    "ball_in_hand_carry_b_0811": [10.062],
    "ball_in_hand_triple_0811": [12.455],
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
    "pot_single_arrival_0823": "empty_table_0801",
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
    "phantom_flag_strike_0805": "empty_table_0801",
    "phantom_candidate_block_0805": "empty_table_0801",
    "parked_vote_newborn_0805": "empty_table_0801",
    "late_emergence_strike_0805": "empty_table_0801",
    "break_outrun_0805": "empty_table_0801",
    "break_dark_flight_0806": "empty_table_0801",
    "occluded_roll_0806": "empty_table_0801",
    "cluster_rest_shot_0806": "empty_table_0801",
    "break_dark_total_0806": "empty_table_0801",
    # 2026-08-11 ring dumps: the live session's own reference (deck/session.py
    # DEFAULT_REFERENCE), same mount, so they replay against what shot them.
    "pot_scratch_nohold_0811": "empty_table_0801",
    "kick_scratch_0811": "empty_table_0801",
    "latch_blind_shot_0811": "empty_table_0801",
    "phantom_still_down_0811": "empty_table_0801",
    "ball_in_hand_carry_0811": "empty_table_0801",
    "ball_in_hand_carry_b_0811": "empty_table_0801",
    "ball_in_hand_triple_0811": "empty_table_0801",
    # The reference RECAL recorded on the same mount 38 minutes later (#56;
    # checkpoint 111 graded it against the stale empty_table_0801, whose rail
    # blobs swallowed the 14 and cut the 11 under the area floor).
    "all_balls_still_1115": "empty_table_1115",
}


@cache
def _reference_model(name: str) -> "ReferenceModel":
    """Built once per reference clip per process — it costs ~0.3 s each."""
    from cueclock.capture.clip import ClipReader
    from cueclock.vision.pipeline import ReferenceModel

    reader = ClipReader(CLIPS / name)
    indices = np.linspace(0, len(reader) - 1, num=15, dtype=int)
    return ReferenceModel.build([reader.frame(int(i)) for i in indices], _calibration(name))


@cache
def _calibration(reference: str) -> "TableCalibration":
    """The calibration a reference clip was graded under.

    A reference clip directory may carry its own `table_calibration.json`
    (`cueclock recalibrate` writes one into every reference it records);
    otherwise the rig's live one. Without this a recalibration silently
    re-grades the whole corpus under the new mount's homography: on
    2026-11-15 RECAL replaced the 2026-08-01 fit every older clip had been
    graded under.
    """
    from cueclock.vision.calibration import TableCalibration

    own = CLIPS / reference / CALIBRATION.name
    return TableCalibration.load(own if own.exists() else CALIBRATION)


def _pipeline(reference: str, reader: "ClipReader") -> "VisionPipeline":
    """A fresh pipeline on `reference`, at the clip's own cadence."""
    from cueclock.vision.pipeline import VisionPipeline

    return VisionPipeline(
        _calibration(reference), _reference_model(reference), cadence_hz=reader.cadence_hz
    )


def _run_clip(name: str, reference: str) -> list[Event]:
    """Replay one clip at the live detect cadence and collect its events.

    The two pipeline halves are run as two passes rather than interleaved:
    `detect` is stateful only through the previous frame's gray and `advance`
    only through the tracker/engine, so the split is behaviour-preserving —
    and it is what lets the detector pass be served from disk when
    `--detect-cache` is on (see `_detections`).
    """
    from cueclock.capture.clip import ClipReader

    reader = ClipReader(CLIPS / name)
    pipeline = _pipeline(reference, reader)
    indices = reader.detect_indices()
    frames = _detections(reader, CLIPS / name, pipeline, indices)
    events: list[Event] = []
    for i, (detections, churn, intrusions) in zip(indices, frames, strict=True):
        events += pipeline.advance(float(reader.timestamps[i]), detections, churn, intrusions)
    return events


def _detections(
    reader: "ClipReader", clip_dir: Path, pipeline: "VisionPipeline", indices: list[int]
) -> list[tuple[list["Detection"], list[float | None], list["Intrusion"]]]:
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
    key = cache_key(clip_dir, pipeline.reference, pipeline.detect_config, pipeline.churn_lag)
    hit = store.load(clip_dir, key, indices)
    if hit is not None:
        return hit
    computed = [pipeline.detect(reader.frame(i)) for i in indices]
    store.store(clip_dir, key, indices, computed)
    return computed


class _BallLists:
    """The ball list of a clip's frame ``i`` (#63), through the same JSON a
    session replay reads `balls.jsonl` with, so cold and cached runs hand the
    hub identical lists. With `--detect-cache` the lists are cached too
    (`BallListCache`): a settle that moves only reads the frames it lands on.
    """

    def __init__(self, reader: "ClipReader", clip_dir: Path, pipeline: "VisionPipeline") -> None:
        self._reader, self._clip_dir, self._reference = reader, clip_dir, pipeline.reference
        self._known: dict[int, dict[str, object]] = {}
        self._added = False
        self._store = None
        cache_dir = os.environ.get("CUECLOCK_DETECT_CACHE")
        if cache_dir:
            from cueclock.capture.detect_cache import BallListCache, ball_list_key

            self._store = BallListCache(Path(cache_dir))
            self._key = ball_list_key(clip_dir, self._reference)
            self._known = self._store.load(clip_dir, self._key)

    def __call__(self, i: int) -> "BallList":
        from cueclock.vision.ball_list import BallList, read_ball_list

        if i not in self._known:
            self._known[i] = read_ball_list(self._reader.frame(i), self._reference).as_dict()
            self._added = True
        return BallList.from_dict(self._known[i])

    def save(self) -> None:
        if self._store is not None and self._added:
            self._store.store(self._clip_dir, self._key, self._known)


# #63's gates: the ball list's verdict at named settles (golden settle times).
# Every other settle in the corpus must call no scratch. Measured in the
# issue's settle table (`settle_table.py`, `settle_movers.py`,
# `tracker_witness.py` in its research folder).
LIST_VERDICTS: dict[str, list[tuple[float, set[str]]]] = {
    # The scratch that touched nothing: travel 1711 mm. Candidate at 16.00 s.
    "kick_scratch_0811": [(15.37, {"scratch"})],
    "pot_scratch_nohold_0811": [(8.35, {"scratch"})],  # one ball moved 8.6 px
    "scratch_shot_0801": [(5.73, {"scratch"})],
    "scratch_replace": [(8.45, {"scratch"})],  # candidate already open (5.02 s)
    "shot_debug": [(11.90, {"scratch"})],  # candidate already open (9.52 s)
    # The cue lifted by hand (159 mm, nothing else moved): picked up (#64).
    "ball_in_hand_carry_b_0811": [(13.35, {"lifted"})],
    "break_0801b": [(17.16, {"none"})],  # a hand on the cue: the list cannot say
    # The list holds the cue (0.93) where the tracker holds none.
    "cluster_shot_0801": [(18.43, {"none", "cleared"})],
}
# Each of the five scratches gets exactly one hold (mid-rack, a countdown live).
SCRATCH_HOLD_CLIPS = frozenset(n for n, v in LIST_VERDICTS.items() if v[0][1] == {"scratch"})


# #64's gates: what each named settle was, and whether it counted. After a
# scratch, ball in hand lasts until an object ball moves, and a settle that
# moves only the cue in between is placing and counts nothing. Outside ball
# in hand nothing changed: a cue lifted or moved by hand counts as before
# (Steven, 2026-11-17: open-play carries are behavioural). Every other judged
# settle in the corpus must not be placing.
HAND_ROWS: dict[str, list[tuple[float, str, bool]]] = {
    # The cue back after the scratch at 8.45 s: no count, and its hold runs on.
    "scratch_replace": [(8.45, "scratch", True), (10.00, "ball_in_hand", False)],
    "kick_scratch_0811": [(15.37, "scratch", True)],  # 1711 mm: still a scratch (#63)
    # Open play, no scratch before: as before #64.
    "ball_in_hand_carry_b_0811": [(13.35, "lifted", True)],  # 159 mm, no candidate
    "ball_in_hand_carry_0811": [(15.43, "cue_moved", True)],
    "ball_in_hand_triple_0811": [
        (12.46, "cue_moved", True),  # a stick shot that touched nothing
        (14.74, "cue_moved", True),
        (16.04, "cue_moved", True),
    ],
    "pickup_newborn_0801": [(8.60, "cue_moved", True)],  # a stick, touching nothing
    "tip_bump": [(3.87, "cue_moved", True)],  # a stick bump, 34.5 px
}


# §17's gate: a strike out of an armed rack that left the rack standing is
# not the break. The clock takes the count back at the named settle and ends
# the clip where it began: IDLE, nothing counted, waiting for the break.
FALSE_STARTS: dict[str, float] = {
    "break_0801b": 17.16,  # the cue ball carried by hand, the rack untouched
}


def _assert_false_starts(name: str, timeline: "ClockTimeline") -> None:
    settle_t = FALSE_STARTS.get(name)
    if settle_t is None:
        # Everywhere else nothing is ever taken back: a real break stays counted.
        taken_back = [round(m.t, 2) for m in timeline.marks if m.kind == "uncount"]
        assert not taken_back, f"{name}: a counted break was taken back at {taken_back}"
        return
    back = [m.t for m in timeline.marks if m.kind == "uncount"]
    assert len(back) == 1 and abs(back[0] - settle_t) <= 0.4, (
        f"{name}: the false start was taken back at {[round(t, 2) for t in back]}, "
        f"expected once at the {settle_t:.2f} s settle"
    )
    assert timeline.final == ("IDLE", 0), f"{name}: the clip left the clock at {timeline.final}"


def _assert_hand_rows(name: str, timeline: "ClockTimeline") -> None:
    """#64: placing during ball in hand counts nothing; nothing else moved."""
    expected = HAND_ROWS.get(name, [])
    for settle_t, row, counted in expected:
        near = [r for t, r in timeline.rows if abs(t - settle_t) <= 0.1]
        assert near == [row], f"{name}: row at {settle_t:.2f} s was {near}, expected {row}"
        count = any(abs(c - settle_t) <= 0.1 for c in timeline.counts)
        assert count == counted, (
            f"{name}: the {settle_t:.2f} s settle {'did not count' if counted else 'counted'} "
            f"(counts {[round(c, 2) for c in timeline.counts]})"
        )
        if row == "ball_in_hand":
            assert not timeline.releases, f"{name}: the hold was released at {timeline.releases}"
    named = [t for t, _, _ in expected]
    stray = [
        round(t, 2)
        for t, r in timeline.rows
        if r == "ball_in_hand" and not any(abs(t - n) <= 0.1 for n in named)
    ]
    assert not stray, f"{name}: placing the corpus does not hold, at {stray}"


def _assert_list_verdicts(name: str, timeline: "ClockTimeline") -> None:
    """#63: the list called exactly the scratches the corpus holds."""
    expected = LIST_VERDICTS.get(name, [])
    for settle_t, allowed in expected:
        near = [v for t, v in timeline.list_verdicts if abs(t - settle_t) <= 0.1]
        assert len(near) == 1 and near[0] in allowed, (
            f"{name}: list verdict at the {settle_t:.2f} s settle was {near}, expected "
            f"{sorted(allowed)} (all: {timeline.list_verdicts})"
        )
    named = [t for t, _ in expected]
    stray = [
        (round(t, 2), v)
        for t, v in timeline.list_verdicts
        if v == "scratch" and not any(abs(t - n) <= 0.1 for n in named)
    ]
    assert not stray, f"{name}: the list called a scratch the corpus does not hold: {stray}"
    if name in SCRATCH_HOLD_CLIPS:
        assert len(timeline.holds) == 1, f"{name}: holds {timeline.holds}, expected one"


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
    # ONE replay, graded twice. The replay is the whole cost (~5 min cold for
    # the battery), so the clock harness drives the same pass rather than
    # replaying the corpus a second time — that mistake doubled the battery to
    # 9 minutes when the clock grade first landed.
    from cueclock.capture.clip import ClipReader

    reader = ClipReader(CLIPS / name)
    pipeline = _pipeline(ref_name, reader)
    indices = reader.detect_indices()
    lists = _BallLists(reader, CLIPS / name, pipeline)
    timeline = replay_clock(
        name,
        pipeline,
        [float(t) for t in reader.timestamps],
        _detections(reader, CLIPS / name, pipeline, indices),
        indices,
        mid_rack=name not in BREAK_CLIPS,
        lists=lists,
    )
    lists.save()
    events = timeline.events
    shots = sum(isinstance(e, ShotTaken) for e in events)
    scratches = sum(isinstance(e, Scratch) for e in events)
    settles = sum(isinstance(e, BallsSettled) for e in events)
    summary = f"{name}: {[f'{type(e).__name__}@{e.t:.2f}' for e in events]}"
    print(summary)

    truth, provenance = ground_truth(name, EXPECTED[name], golden_shot_times(name))

    # ---- the gate: what the clock did (#34) ----
    #
    # Since #33 the event stream and the product diverge on purpose — a missed
    # strike whose latch still stops the countdown ~0.25 s after the stroke is
    # indistinguishable at the table from a detected one. So the clip is
    # graded on the clock, and the event stream below is diagnostics.
    if truth is not None:
        _assert_clock_outcome(name, truth, timeline)
    _assert_list_verdicts(name, timeline)
    _assert_hand_rows(name, timeline)
    _assert_false_starts(name, timeline)

    # ---- the golden: the record, and the gate only where nothing else is ----
    #
    # A golden diff is no longer a failure by itself: under the criterion
    # above, an event-stream change matters only when it changes what the
    # clock did. `stroke_tug_strike_0801` is the case that forced this — its
    # fire was lost at checkpoint 73 and the golden still holds the 13.198 s
    # stroke, yet the latch stops the clock 0.128 s *earlier* than that fire
    # ever did. The golden is kept exactly as it is, because it is now the
    # only surviving record of when that stroke was, and it is what makes the
    # clip gradeable at all. Regenerating it would destroy that.
    #
    # On clips with no clock ground truth the golden is the only gate left,
    # so there it still fails.
    goldens.check(name, ref_name, events, gate=truth is None)

    # ---- event counts: diagnostics, except where the clock cannot see ----
    expected = EXPECTED[name]
    exp_shots, exp_scratches, min_settles = expected[:3]
    max_settles = expected[3] if len(expected) == 4 else None
    if truth is None and exp_shots is not None:
        assert shots == exp_shots, f"{summary} (no clock ground truth: {provenance})"
    # Scratches are NOT covered by the clock grade — a Scratch verdict
    # deliberately never moves the clock (#25/#26; the candidate grants the
    # hold, the verdict is journal-only). So this stays a hard assertion, or
    # the whole scratch class would go ungraded.
    if exp_scratches is not None:
        assert scratches == exp_scratches, summary
    if truth is None:
        if min_settles is not None:
            assert settles >= min_settles, summary
        if max_settles is not None:
            assert settles <= max_settles, summary


def _assert_clock_outcome(name: str, truth: list[float], timeline: "ClockTimeline") -> None:
    """Did the countdown stop on time, and count each real shot exactly once?"""
    if name in BREAK_CLIPS:
        # No countdown to freeze — WNT §5 makes the break untimed and starts
        # the window at the rest. The only question is whether it got counted.
        assert len(timeline.counts) == len(truth), (
            f"{name}: break counted {len(timeline.counts)}x, expected {len(truth)} "
            f"(counts at {[round(c, 2) for c in timeline.counts]})"
        )
        return

    resumes = [m.t for m in timeline.marks if m.kind == "resume"]
    for stroke in truth:
        window = [
            f for f in timeline.freezes if stroke - FREEZE_EARLY_S <= f <= stroke + FREEZE_LATE_S
        ]
        # ...or a freeze older than the EARLY bound that the clock never came
        # out of before the stroke (#40, pot_scratch_nohold_0811). The EARLY
        # bound is a *proxy*: "a freeze earlier than the confirm path's own
        # latency cannot belong to this stroke's episode". Held-since-then is
        # the thing the proxy is standing in for, and it can be read directly
        # — if no resume lies between the freeze and the stroke, the countdown
        # was stopped continuously through it and by definition did not run
        # through the shot. What made the proxy wrong here: the golden's fire
        # on that clip is 1.11 s LATE (the stroke is at 6.509 — a track born
        # at 2646 mm/s — and the fire lands on the ball's third rebirth at
        # 7.623), so the ground truth this loop grades against is itself
        # displaced, and a freeze at the true stroke reads as too early.
        # Nothing is loosened for a real miss: a missed stroke meets a
        # RUNNING clock, which means a resume, which fails this too.
        held = [
            f
            for f in timeline.freezes
            if f < stroke - FREEZE_EARLY_S and not any(f < r <= stroke for r in resumes)
        ]
        assert window or held, (
            f"{name}: the stroke at {stroke:.3f}s never stopped the clock within "
            f"[-{FREEZE_EARLY_S}, +{FREEZE_LATE_S}]s — freezes at "
            f"{[round(f, 3) for f in timeline.freezes]}, resumes at "
            f"{[round(r, 3) for r in resumes]}. The countdown ran through a "
            f"real shot; that is the failure this criterion exists to catch."
        )
        # #34: the strike-fire rate stays worth knowing — it is the difference
        # between a 0.25 s freeze and a 0.5 s one, and the leading indicator
        # if the substrate rots. Now that the fire is no longer a gate, this
        # warning is how a miss stays visible: pytest counts it in the
        # end-of-run summary, so the rate is readable on a green run.
        if not any(abs(f - stroke) <= FREEZE_EARLY_S for f in timeline.shots_fired):
            warnings.warn(
                f"strike miss on {name}: the stroke at {stroke:.3f}s never fired a "
                f"ShotTaken — the motion latch stopped the clock instead. Correct "
                f"outcome, degraded substrate.",
                StrikeMiss,
                stacklevel=2,
            )

    # "Extra is not fine" — a shot counted that was never taken is a phantom,
    # and no downstream policy can undo it. Gated on the clip's rearrangement
    # being anchored at all: a clip that opens mid-action has a 1-2 ball
    # baseline, which makes every settled ball read as `appeared` and the
    # summary an artifact of the clip rather than a fact about the table
    # (`pickup_snatch_0801`). `parked_vote_strike_0801` measures 7/7/7 and
    # stays graded, which is what keeps this from excusing the real defect.
    if not timeline.rearrangement_is_anchored:
        return
    lo = (truth[0] - FREEZE_EARLY_S) if truth else float("-inf")
    counts = [c for c in timeline.counts if c >= lo]
    assert len(counts) == len(truth), (
        f"{name}: clock counted {len(counts)} shots at {[round(c, 2) for c in counts]}, "
        f"ground truth has {len(truth)} at {[round(s, 3) for s in truth]}. Extra counts "
        f"are phantom shots — a freeze on nothing."
    )


# Clips with every ball on the table standing still, and how many there are.
ALL_BALLS = {"all_balls_still_1115": 16, "static_spread": 16}


@pytest.mark.parametrize("name", sorted(ALL_BALLS))
def test_every_ball_is_its_own_detection(name: str) -> None:
    """#56: every ball reads as one detection, on every frame. Before it,
    all_balls_still_1115 read 11 (the red 3 below the gray threshold, two
    touching pairs as one blob each, two rail balls lost to a stale
    reference) and static_spread 14."""
    if not (CLIPS / name).exists():
        pytest.skip(f"clip {name} not recorded")
    from cueclock.capture.clip import ClipReader

    reader = ClipReader(CLIPS / name)
    pipeline = _pipeline(REFERENCES.get(name, "empty_table"), reader)
    indices = reader.detect_indices()
    counts = [len(balls) for balls, _, _ in _detections(reader, CLIPS / name, pipeline, indices)]
    wrong = [(i, c) for i, c in zip(indices, counts, strict=True) if c != ALL_BALLS[name]]
    assert not wrong, f"{len(wrong)} of {len(indices)} frames miscount, first: {wrong[:5]}"


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
    reference = REFERENCES.get(clip, "empty_table")

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
    events = _run_clip("break_0801", "empty_table_0729")
    shots = [e for e in events if isinstance(e, ShotTaken)]
    assert len(shots) == 1
    assert shots[0].speed_mms is not None
    assert shots[0].speed_mms >= MATCH_BREAK_FLOOR_MMS, f"break read {shots[0].speed_mms:.0f}"


def test_break_outrun_fires_at_the_stroke() -> None:
    """A break the clock sat straight through, and the shape it failed in.

    break_outrun_0805, 2026-08-05 21:52, flagged at the table on Steven's
    first break of the session: ground truth is a normal-power break, and the
    journal shows the clock IDLE at 60.0 s through the whole rack with no
    ShotTaken recorded at all. Detection was healthy (77-78 Hz), so this is
    not contention — and the only thing vision emitted was the settle at
    12.936 s, the missed-strike marker.

    The cue steps off a 4.67 s dwell at 4.724 s and is never observed again;
    the track that carries it onward is born at 4.762 s, 320 mm from the rest
    at 7221 mm/s. Counts alone would stay green if the shot arrived a second
    late, so the timing is what is pinned: the strike belongs to the stroke.
    """
    if not (CLIPS / "break_outrun_0805").exists():
        pytest.skip("clip break_outrun_0805 not recorded")
    events = _run_clip("break_outrun_0805", "empty_table_0801")
    shots = [e for e in events if isinstance(e, ShotTaken)]
    assert len(shots) == 1, f"expected one break, got {[round(s.t, 3) for s in shots]}"
    assert 4.7 <= shots[0].t <= 5.1, f"break fired at {shots[0].t:.3f}s, stroke was 4.724s"


def test_break_outrun_speed_clears_the_idle_gate() -> None:
    """The break must also start the match, not just be seen.

    Same rule as `test_break_0801_speed_clears_the_idle_gate`: out of a fresh
    rack the IDLE gate only opens on a break, so a ShotTaken that reads low is
    the same failure to Steven as no ShotTaken at all — the clock stays dead
    and he has to press at the table. Reads 9923 mm/s here, 4x the floor.
    """
    from cueclock.clock.modes import MATCH_BREAK_FLOOR_MMS

    if not (CLIPS / "break_outrun_0805").exists():
        pytest.skip("clip break_outrun_0805 not recorded")
    events = _run_clip("break_outrun_0805", "empty_table_0801")
    shots = [e for e in events if isinstance(e, ShotTaken)]
    assert len(shots) == 1
    assert shots[0].speed_mms is not None
    assert shots[0].speed_mms >= MATCH_BREAK_FLOOR_MMS, f"break read {shots[0].speed_mms:.0f}"


def test_break_underread_speed_clears_the_idle_gate() -> None:
    """The break this clip is named for — the one the live gate ate at 705 mm/s.

    Pinned as an honest gap for a week (cold replay confirmed no break at
    all), and closed by checkpoint 67: the streak did not die, it outran the
    handoff radius while invisible and was then killed by the observation
    floor inside the rack. Now fires reading 9469 mm/s. This asserts the part
    that actually mattered at the table — that the clock would have started.
    """
    from cueclock.clock.modes import MATCH_BREAK_FLOOR_MMS

    if not (CLIPS / "break_underread_0801").exists():
        pytest.skip("clip break_underread_0801 not recorded")
    events = _run_clip("break_underread_0801", "empty_table_0801")
    shots = [e for e in events if isinstance(e, ShotTaken)]
    assert len(shots) == 1, f"expected one break, got {[round(s.t, 3) for s in shots]}"
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
    events = _run_clip("spin_in_place", "empty_table")
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
    events = _run_clip("settle_stall_0801", "empty_table_0801")
    settles = [e.t for e in events if isinstance(e, BallsSettled)]
    post_shot = [t for t in settles if t > 6.9]
    assert post_shot, "the shot's settle never fired — the settle stall is back"
    assert 9.7 <= post_shot[0] <= 10.7, f"settle at {post_shot[0]:.2f}s — outside rest+2 dwells"


def test_phantom_flag_shot_fires_at_the_stroke() -> None:
    """The handoff must anchor on the stroke, not on the vote recovering.

    phantom_flag_strike_0805: the cue is last detected at its rest spot at
    10.030 s (clip-relative), undetected for 0.173 s while it travels 171 mm,
    and re-emerges at 10.228 s as a merged blob 193 mm out at 2281 mm/s. The
    fire confirms at 10.27 s off the cue's remembered rest.

    Counts alone would stay green through the bug that matters here. The
    tracker's own recovery path eventually reaches the same place — the frozen
    phantom dies at its 3 s coast budget (13.04 s), the regrab grace expires a
    second later, and the flag lands on the real ball around 14.06 s — so a
    shot minted from *that* would still count 1. It would also be ~3.8 s late
    on a 30 s clock, which is the whole failure. The window pinned here is the
    stroke plus a confirm, so riding the vote's recovery fails it.
    """
    if not (CLIPS / "phantom_flag_strike_0805").exists():
        pytest.skip("clip phantom_flag_strike_0805 not recorded")
    events = _run_clip("phantom_flag_strike_0805", "empty_table_0801")
    shots = [e.t for e in events if isinstance(e, ShotTaken)]
    assert shots, "the stroke never fired — the phantom-flag miss is back"
    assert 10.0 <= shots[0] <= 10.8, f"shot at {shots[0]:.2f}s — not anchored on the stroke"


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
    events = _run_clip("address_chatter_miss_0801", "empty_table_0801")
    shots = [e.t for e in events if isinstance(e, ShotTaken)]
    assert shots, "the stroke never fired — the address-chatter miss is back"
    assert 10.0 <= shots[0] <= 11.2, f"shot at {shots[0]:.2f}s — not anchored on the impulse"


def test_blocked_candidate_shot_fires_at_the_stroke() -> None:
    """The shelved slot must be claimed while the ball is still in radius.

    phantom_candidate_block_0805: the cue dwells at (1709, 803) for 11.1 s,
    steps off it at 11.239 s reading 2106 mm/s, and the flag-borne candidate
    opens correctly — then the tracker loses the ball. The struck cue surfaces
    as a new track at 11.284 s, 224 mm from the rest and inside the 250 mm
    handoff radius, but the doomed candidate holds the slot until the
    feasibility check kills it at 11.355 s (21.1 mm travelled against the
    22.0 mm it still needed — dead by 0.9 mm, on a phantom the tracker had
    re-stamped still). By then the real ball is 469 mm out and every anchor
    refuses it. Cold replay produced one BallsSettled and no ShotTaken.

    Counts would catch a total miss, but not a late one: the window pinned
    here is the impulse plus a confirm. A shot minted off some later recovery
    would still count 1 while the clock had already run through the stroke.
    """
    if not (CLIPS / "phantom_candidate_block_0805").exists():
        pytest.skip("clip phantom_candidate_block_0805 not recorded")
    events = _run_clip("phantom_candidate_block_0805", "empty_table_0801")
    shots = [e.t for e in events if isinstance(e, ShotTaken)]
    assert shots, "the stroke never fired — the blocked-candidate miss is back"
    assert 11.2 <= shots[0] <= 11.9, f"shot at {shots[0]:.2f}s — not anchored on the stroke"


def test_late_emergence_shot_fires_at_the_stroke() -> None:
    """The handoff's step gate must not bill the occlusion to the ramp budget.

    late_emergence_strike_0805: the cue is struck at 15.425 s (2479 mm/s off a
    15 s rest) and its track is abandoned *drifting* rather than held still, so
    `_cue_last_still_t` freezes at 15.410. The struck ball surfaces at 15.534 s,
    175 mm from the rest — well inside the handoff radius — and was refused by
    8 ms: 0.128 s against the frozen stamp, where the cue had actually been
    observed 0.109 s earlier. Measured from the last observation it passes and
    the strike confirms at 15.739 s.

    The margin here is 11 ms, so this is the scenario that will notice if the
    step gate ever goes back to reading a stamp that stops advancing precisely
    because the ball vanished.
    """
    if not (CLIPS / "late_emergence_strike_0805").exists():
        pytest.skip("clip late_emergence_strike_0805 not recorded")
    events = _run_clip("late_emergence_strike_0805", "empty_table_0801")
    shots = [e.t for e in events if isinstance(e, ShotTaken)]
    assert shots, "the stroke never fired — the late-emergence miss is back"
    assert 15.4 <= shots[0] <= 16.1, f"shot at {shots[0]:.2f}s — not anchored on the stroke"


def test_parked_vote_newborn_confirms_on_travel_not_speed() -> None:
    """The parked-vote birth path must judge the ball, not trust the vote.

    parked_vote_newborn_0805: the whiteness vote sits on an observed, still
    ball at (717, 91) — the object ball about to be potted — while the struck
    cue emerges from the address occlusion as a track born at 11.310 s already
    doing 1752 mm/s. With the holder observed there was no path at all: the
    birth path was gated on the holder being dark, and the own-rest path wants
    roster membership a newborn cannot have.

    A single speed reading cannot be the discriminator — scratch_replace's
    hand throw spikes to 3179 mm/s, *faster* than this strike's impulse — so
    the parked confirm has two tiers, both pinned here. Travel: measured over
    1 s from the birth spot, this real strike goes 962 mm while every false
    the configuration admitted corpus-wide managed 202-382 mm (600 mm gate).
    Corroborated speed (`parked_fast_confirm_mms`, flag_missed_3 2026-08-06):
    TWO CONSECUTIVE observed raw steps at 1200+ — this strike sustains
    1865-2019 mm/s and certifies, firing at 11.425 s on the normal 60 mm
    confirm, 0.33 s before the travel tier would have; a hand throw's spike
    never repeats on adjacent observed steps and still waits out the 600 mm
    it cannot cover (scratch_replace stays silent in its own scenario).
    Firing before the impulse at 11.310, or later than the travel tier's
    11.758, would mean one of the tiers is broken.
    """
    if not (CLIPS / "parked_vote_newborn_0805").exists():
        pytest.skip("clip parked_vote_newborn_0805 not recorded")
    events = _run_clip("parked_vote_newborn_0805", "empty_table_0801")
    shots = [e.t for e in events if isinstance(e, ShotTaken)]
    assert shots, "the stroke never fired — the parked-vote birth miss is back"
    assert 11.31 <= shots[0] <= 11.8, f"shot at {shots[0]:.2f}s — parked confirm tiers broken"


def test_break_dark_total_settles_at_the_rest() -> None:
    """The fully dark break must settle, at the rest, into the dark tier.

    break_dark_total_0806: a 15-ball break whose every channel slept — the
    racked pack never confirms a track cold (one confirmed ball on the whole
    pre-break table), the scatter erupts as rosterless newborns, and live the
    session emitted nothing between the New Rack press and Steven's flag.

    **Retired deliberately at checkpoint 88 (#40): this used to pin the
    settle as the *backstop's*.** The engine slept through the episode
    because the scatter's tracks are all newborns and only roster balls could
    latch motion; with the struck-ball latch admission the engine sees the
    scatter itself and settles it at **14.29 s**, against the backstop's
    18.51 s. That is not a lost pin, it is the tracker news the old docstring
    said to expect: the balls are measurably at rest by 13.2-13.8 s (last
    supra-creep displacement 13.6 s, last roller 310 mm/s at 13.5 s), so the
    engine's settle lands one dwell after the rest and the backstop's was
    **4.2 s late** — pixel chatter kept resetting its quiet run.

    What is pinned now is what the clip is actually for: exactly one settle,
    at the rest, with break-scale rearrangement that reaches the hub's dark
    tier. The backstop itself is untouched and still covers the case where
    the engine really does sleep; it simply is not needed here any more.
    """
    name = "break_dark_total_0806"
    if not (CLIPS / name).exists():
        pytest.skip(f"clip {name} not recorded")
    from cueclock.capture.clip import ClipReader

    reader = ClipReader(CLIPS / name)
    pipeline = _pipeline("empty_table_0801", reader)
    indices = reader.detect_indices()
    frames = _detections(reader, CLIPS / name, pipeline, indices)
    settles: list[tuple[float, bool, int, int]] = []
    for i, (detections, churn, intrusions) in zip(indices, frames, strict=True):
        for event in pipeline.advance(float(reader.timestamps[i]), detections, churn, intrusions):
            if isinstance(event, BallsSettled):
                summary = pipeline.last_settle_rearrange
                involved = summary.involved if summary is not None else 0
                appeared = summary.appeared if summary is not None else 0
                settles.append((event.t, pipeline.last_settle_backstop, involved, appeared))
    assert len(settles) == 1, f"expected exactly one settle, got {settles}"
    t, _backstop, involved, appeared = settles[0]
    # Rest (13.2-13.8 s, measured above) plus a settle dwell, with the same
    # margin the other timing pins carry. Later than this means the settle has
    # gone back to riding a chatter lottery.
    assert 13.2 <= t <= 15.0, f"dark break settled at {t:.2f}s — not one dwell after the rest"
    assert involved >= 8, f"settle fired at involved {involved} — not break-scale evidence"
    # #36 Part 3: the settle must not merely exist, it must reach the hub's
    # dark tier — which reads `appeared`, the pack un-merging. Nothing here
    # ever produces a fire, so this is the *only* channel by which a fully
    # dark break can start the clock. If it stops clearing the floor, that
    # class silently reverts to being missed.
    assert appeared >= hub_module.BREAK_DARK_MIN_APPEARED, (
        f"settle appeared={appeared} is under the dark floor "
        f"({hub_module.BREAK_DARK_MIN_APPEARED}) — the fully dark break no "
        "longer reaches the tier that counts it"
    )


def test_latch_blind_shot_stops_the_clock() -> None:
    """#40: a shot the whole engine slept through must still stop the clock.

    latch_blind_shot_0811, Steven's flag from session-20260811-182341. The
    cue is struck at **8.756 s** — its old track goes dark at the impact and
    the ball reappears as track 8, born already reading 3412 mm/s raw /
    2048 mm/s smoothed and crossing 358 mm per 0.2 s window, solidly observed
    the whole way. Cold replay emitted **zero events over all 20.74 s** and
    `episode_active` was False throughout: the motion latch only admitted
    roster members, and roster admission happens only during sustained quiet,
    so the fastest object on the table could not latch. Live, the countdown
    ran through the stroke and hit GRACE.

    No `ShotTaken` is asserted, and none fires: the crown stayed parked on
    the cue's dead identity, sitting still and unobserved at a position the
    ball had already left, so every strike path was watching a corpse. The
    latch is the criterion (#34) — it stops the countdown ~0.1 s after the
    stroke, which at the table is the same outcome as a fire.

    Pinned: the freeze belongs to the stroke, and the shot is counted exactly
    once. A latch that fires on the settle instead of the stroke, or twice,
    fails here rather than drifting a golden.
    """
    name = "latch_blind_shot_0811"
    if not (CLIPS / name).exists():
        pytest.skip(f"clip {name} not recorded")
    from cueclock.capture.clip import ClipReader

    stroke = 8.756
    reader = ClipReader(CLIPS / name)
    pipeline = _pipeline("empty_table_0801", reader)
    indices = reader.detect_indices()
    timeline = replay_clock(
        name,
        pipeline,
        [float(t) for t in reader.timestamps],
        _detections(reader, CLIPS / name, pipeline, indices),
        indices,
    )
    assert timeline.freezes, (
        "the countdown never stopped at all — the roster-scoped latch is back "
        f"(events: {[f'{type(e).__name__}@{e.t:.2f}' for e in timeline.events]})"
    )
    assert any(stroke - FREEZE_EARLY_S <= f <= stroke + FREEZE_LATE_S for f in timeline.freezes), (
        f"the stroke at {stroke:.3f}s did not stop the clock — freezes at "
        f"{[round(f, 3) for f in timeline.freezes]}"
    )
    assert len(timeline.counts) == 1, (
        f"the stroke was counted {len(timeline.counts)}x at "
        f"{[round(c, 2) for c in timeline.counts]} — expected exactly once"
    )


def test_0811_scratches_reach_the_adjudicator() -> None:
    """#39: two real scratches that produced zero candidates, 2026-08-11.

    Both went into the same corner and both died on the zone margin — the
    cue's last *observed* position 123 mm (the pot) and 124 mm (the kick)
    from the pocket centre, against `pocket_zone_radius_mm` = 120. The
    whiteness vote held the cue throughout on both (0.989 and 1.0), so this
    was never the §2.3 identity failure it looked like from the journal; the
    latch simply never armed, and the two-stage verdict #26 built was never
    reached.

    A `ScratchCandidate` is the evidence the hub's hold and adjudication both
    hang off. Before the one-frame flight allowance in `_near_pocket`, both
    clips emitted **zero** scratch events of any kind. Both reach a full
    `Scratch`; the kick's only since #56 (see its EXPECTED note).
    """
    for name, wants_verdict in (("pot_scratch_nohold_0811", True), ("kick_scratch_0811", True)):
        if not (CLIPS / name).exists():
            pytest.skip(f"clip {name} not recorded")
        events = _run_clip(name, "empty_table_0801")
        candidates = [e for e in events if type(e).__name__ == "ScratchCandidate"]
        assert len(candidates) == 1, (
            f"{name}: expected exactly one scratch candidate, got {len(candidates)} — "
            "the cue died in the pocket zone and must reach the adjudicator"
        )
        if wants_verdict:
            assert any(isinstance(e, Scratch) for e in events), (
                f"{name}: the candidate never resolved to a Scratch"
            )


def test_phantom_still_down_is_rejected_at_the_settle() -> None:
    """#37: a settle that proves nothing happened must not count a shot.

    `phantom_still_down_0811` is the live case, mid-rack and 375 s into a
    session: the latch froze the clock while Steven was still down on the
    ball, and 1.65 s later the settle counted a shot and reset the countdown
    19.7 -> 30 s. His real stroke lands 1.6 s after that.

    This cannot be graded by `EXPECTED`: the phantom precedes the clip's only
    real stroke, and `_assert_clock_outcome` ignores counts before the first
    ground-truth stroke on purpose (clip-start artifacts). So the phantom is
    pinned here, on the three things that must all be true:

      * the settle's evidence is a **real** zero — an anchored quiet baseline,
        not the `_settled_baseline or present` fallback that makes every
        mid-action clip read zero for free;
      * the clock counts exactly one shot in the whole clip, and it is the
        real one;
      * the phantom's freeze is *released*, not merely uncounted — the
        countdown resumes, which is the whole point of doing this at the
        settle instead of refusing the freeze up front.

    Pre-#37 this failed on the second: two counts, at 10.50 and 18.92.
    """
    name = "phantom_still_down_0811"
    if not (CLIPS / name).exists():
        pytest.skip(f"clip {name} not recorded")
    from cueclock.capture.clip import ClipReader

    reader = ClipReader(CLIPS / name)
    pipeline = _pipeline("empty_table_0801", reader)
    indices = reader.detect_indices()
    timeline = replay_clock(
        name,
        pipeline,
        [float(t) for t in reader.timestamps],
        _detections(reader, CLIPS / name, pipeline, indices),
        indices,
    )

    assert timeline.baselines and timeline.baselines[0] is not None, (
        f"{name}: the phantom settle had no quiet baseline at all "
        f"(baselines {timeline.baselines}) — the zero would be arithmetic, not "
        "evidence, and the rejection must fail open on it"
    )
    assert timeline.baselines[0] >= hub_module.QUIET_BASELINE_MIN_BALLS, (
        f"{name}: the phantom settle's baseline resolved {timeline.baselines[0]} balls, "
        f"under the floor ({hub_module.QUIET_BASELINE_MIN_BALLS}) — this clip no longer "
        "demonstrates a rejectable phantom"
    )
    assert len(timeline.counts) == 1, (
        f"{name}: the clock counted {len(timeline.counts)} shots at "
        f"{[round(c, 2) for c in timeline.counts]}; ground truth is ONE, the stroke at "
        f"~12.10s. The first settle (~10.50s) is the phantom — Steven was still down on "
        "the ball, and the table is provably unchanged across it."
    )
    assert timeline.counts[0] > 12.0, (
        f"{name}: the one count landed at {timeline.counts[0]:.2f}s — that is the "
        "phantom's settle, not the real shot's"
    )
    resumes = [m.t for m in timeline.marks if m.kind == "resume"]
    assert any(10.0 < r < 12.0 for r in resumes), (
        f"{name}: the phantom's freeze was never released (resumes at "
        f"{[round(r, 2) for r in resumes]}) — a rejected phantom must hand the "
        "countdown back, not leave the clock frozen"
    )


@pytest.mark.parametrize("name", sorted(CARRY_CLIPS))
def test_0811_carries_still_count_the_real_stroke(name: str) -> None:
    """The labelled carry clips, pinned on what survives #41's removal.

    Each clip holds one real stroke and one or two ball-in-hand carries
    (`CARRY_CLIPS` maps the strokes, with the journal timestamps they came
    from). Until checkpoint 104 this test asserted the carries did *not*
    count — the #41 done-when, stated as the clock. That verdict is gone
    (`hub`'s removal note has the live measurement), so the carries reset the
    countdown again and asserting otherwise would be asserting a rule the
    product no longer has.

    What is still true, and is what this now defends: **the real stroke is
    counted, and counted at its own settle.** Every regression that ever
    threatened these clips took that away — the pre-#41 engine counted the
    carries *instead of* landing on the stroke — so the clips keep their
    grading value without pinning the retired verdict.

    The freeze assertion is kept for the same reason it was written: the
    countdown must stop before the stroke and resume at the remaining it
    froze with. A change that stopped a carry counting by making the table
    invisible would pass a count assertion and be a worse product.

    These clips are the labelled ground truth any future ball-in-hand attempt
    starts from. Keeping them graded is why they are still here.
    """
    if not (CLIPS / name).exists():
        pytest.skip(f"clip {name} not recorded")
    from cueclock.capture.clip import ClipReader

    reader = ClipReader(CLIPS / name)
    pipeline = _pipeline(REFERENCES[name], reader)
    indices = reader.detect_indices()
    timeline = replay_clock(
        name,
        pipeline,
        [float(t) for t in reader.timestamps],
        _detections(reader, CLIPS / name, pipeline, indices),
        indices,
    )
    strokes = CARRY_CLIPS[name]
    for stroke in strokes:
        assert any(abs(c - stroke) <= FREEZE_LATE_S for c in timeline.counts), (
            f"{name}: no count landed on the real stroke's settle at "
            f"{stroke:.3f}s — counts are {[round(c, 3) for c in timeline.counts]}. "
            "The carries counting too is expected since #41's removal; the "
            "stroke itself going uncounted is a miss, and the thing this "
            "clip is here to catch."
        )
    for stroke in strokes:
        assert any(f <= stroke for f in timeline.freezes), (
            f"{name}: the countdown never stopped before the stroke settling at "
            f"{stroke:.3f}s — freezes at {[round(f, 3) for f in timeline.freezes]}"
        )
