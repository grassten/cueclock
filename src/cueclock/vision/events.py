"""Event logic: per-ball track states in → shot/settle/scratch events out.

Implements docs/design.md §4.2. All thresholds are physical units
(table millimeters), sized against the measured noise floor (§4.3: velocity
noise ~ 7-22 mm/s raw, so the 40 mm/s settle floor is >= 3 sigma clean).

Pure module: no I/O, no clocks, no threads. The caller (tracker) feeds
``EventEngine.update()`` one batch of :class:`BallState` per detection frame
with its timestamp; cadence is whatever the caller runs at (78 or 156 Hz)
— everything here works from timestamps, not frame counts, except the few
constants :data:`TUNED_HZ` names, which :meth:`EventConfig.at_cadence` and
``TrackConfig.at_cadence`` convert to the same span of time.

Input contract (firmed up with the tracker, `vision/track.py`): the tracker
reports every live track each frame (coasting through occlusions per §2.3
lesson 4, so a brief hand-over-ball does not look like a lost ball). A ball
absent from the batch is a *terminated* track — that is what scratch
detection keys on (§2.3 lesson 3: latched during motion, never evaluated at
settle time). Coasted reports carry ``observed=False``: their positions are
velocity extrapolations, and scratch reads only the last *observed* position
(a phantom coasting into a pocket zone is not a sink — the spin_in_place
corpus lesson). Each ball carries two speeds: ``speed_mms`` (smoothed —
settle-grade, low noise) and ``raw_speed_mms`` (one-step — impulse-grade,
shows the rest→moving step the smoothed estimate lags on). Strike impulse
reads the raw speed; everything else reads the smoothed one. Synthetic tests
may omit ``raw_speed_mms``; it falls back to ``speed_mms``.
"""

from collections import deque
from dataclasses import dataclass, field, replace
from math import hypot
from typing import Literal

# The detect cadence every frame-denominated constant in this module and in
# `track.py` was measured at: stride 2 of the camera's 156.25 fps, which is
# where all of them were tuned (checkpoints 1-105). A pipeline at any other
# cadence gets a copy converted to the same span of *time* — never a retune.
TUNED_HZ = 156.25 / 2


@dataclass(frozen=True)
class BallState:
    """One tracked ball in one detection frame, in table coordinates."""

    ball_id: int
    x_mm: float
    y_mm: float
    speed_mms: float  # smoothed speed, mm/s (settle-grade)
    is_cue: bool = False
    raw_speed_mms: float | None = None  # one-step speed (impulse-grade); None → speed_mms
    observed: bool = True  # False while the tracker is coasting this ball
    # through an occlusion — the position is then an extrapolation, not evidence
    present: bool = True  # an observation matched this ball this frame, even
    # one whose contents were discarded (a still ball merged with the stick is
    # observed=False, present=True: something is measurably there, covering
    # it). observed implies present; present=False means the tracker matched
    # nothing at all and the ball's very existence is an extrapolation —
    # a claim that must not found a cue rest (§5.1(b))
    merged: bool = False  # this frame's observation was an oversized (merged)
    # blob: the ball is inseparable from a hand/stick or streaking. Meaningful
    # only when observed; the newborn-strike confirm reads it (a hand-carried
    # ball is merged every frame, a struck ball on open cloth almost never)
    churn: float | None = None  # smoothed appearance churn inside the ball's
    # own disk (spin shows here; a still ball shows only sensor noise);
    # None when unobserved or no sample yet
    whiteness: float | None = None  # the tracker's slow whiteness EMA for this
    # track (annulus metric, freeze-guarded during fast motion). The scratch
    # census reads it (#26): at rest it converges to the per-frame ring read
    # within ~0.3 s. None = unknown (synthetic tests) — never witness-grade.
    color_age_s: float | None = None  # seconds since that EMA last folded in a
    # sample (motion freeze + occlusion both stall it). Candidate forensics:
    # says how stale the dying crown's color evidence was. None = no sample.


@dataclass(frozen=True)
class ShotTaken:
    t: float
    # Forensics + clock-side context (checkpoint 28): which track fired, at
    # what impulse speed, and via which path. The IDLE break gate reads
    # speed_mms (a break is fast; rack-gather placements are not) and the
    # session journal records all three, so a live misfire is attributable
    # from the dump alone. speed_mms is the larger of the fastest observed
    # ball in the candidate window and the confirm's own kinematics
    # (displacement / confirm time) — the observed sample alone underreads
    # when the struck ball streaks unobservably fast (break_0801b).
    ball_id: int | None = None
    speed_mms: float | None = None
    newborn: bool = False


@dataclass(frozen=True)
class BallsSettled:
    t: float


@dataclass(frozen=True)
class SettleMover:
    """One episode mover's evidence at the settle (#33): how far the ball
    actually traveled (episode origin -> last observed position) and whether
    its track was still present when the table settled. Movers are roster
    balls by construction — they were standing on the table before the
    episode latched. Rides beside the event stream like the rearrangement
    summary (``EventEngine.last_settle_movers``), so the corpus goldens stay
    byte-identical; the hub reads it as evidence for the settle-time shot
    adjudication and nothing in the vision layer acts on it (§13.1)."""

    travel_mm: float
    alive: bool


@dataclass(frozen=True)
class Scratch:
    t: float


@dataclass(frozen=True)
class ScratchCandidate:
    """Stage one of the two-stage scratch verdict (#26): the reappear watch
    expired on a cue track lost near a pocket. Evidence, never a verdict —
    exactly one Scratch or ScratchRefuted follows once the whiteness census
    resolves it (see ``_adjudicate_scratch``). Journaled so every live
    verdict carries its trigger evidence."""

    t: float
    x_mm: float  # death spot: the cue's last *observed* position
    y_mm: float
    pocket_x_mm: float  # center of the pocket zone the death spot fell in
    pocket_y_mm: float
    whiteness: float | None = None  # dead track's whiteness EMA when last observed
    color_age_s: float | None = None  # how stale that EMA was (motion freeze)


@dataclass(frozen=True)
class ScratchRefuted:
    """A ScratchCandidate killed by positive evidence: a roster ball born
    before the cue died, still and observed, reading cue-grade whiteness.
    The cue never left the table — a potted object ball wore the crown."""

    t: float
    witness_id: int
    x_mm: float  # where the witness sat when it refuted, for spot-audits
    y_mm: float
    whiteness: float


Event = ShotTaken | BallsSettled | Scratch | ScratchCandidate | ScratchRefuted


@dataclass(frozen=True)
class EventConfig:
    """§4.2 thresholds. Defaults for a 7 ft UK table (1930 x 965 mm playing area)."""

    # Settle / table-moving — position-based, not speed-based (learned on the
    # real corpus 2026-07-27): the addressed cue ball's blob merges with the
    # cue stick and gets centroid-yanked, so *velocity estimates* spike with
    # artifacts while the ball never actually moves. Displacement over a time
    # window measures the same physics (v = d/t: 30 mm / 0.2 s = the old
    # 150 mm/s enter threshold; 3 mm / 0.5 s creep = 6 mm/s sustained) but is
    # immune to velocity-estimation noise. Hysteresis is preserved: entering
    # "moving" takes 30 mm of net travel, staying settled tolerates 3 mm.
    settle_floor_mms: float = 40.0  # still used by strike rest + scratch latch
    moving_enter_mm: float = 30.0  # net displacement that flips the table to moving
    moving_window_s: float = 0.2
    settle_dwell_s: float = 0.5
    settle_creep_mm: float = 5.0  # max any-ball displacement over the creep window
    # (5 mm, not the design's 3: measured centroid jitter of bright balls on
    # the real corpus reaches ~4 mm over 0.5 s; 5 mm still catches sustained
    # 10 mm/s pocket creep)
    settle_creep_window_s: float = 0.5
    # Settle-at-spin-stop (§4.2 rules caveat, WPA def. 8.2: a shot ends only
    # when balls stop moving AND spinning): a position-stationary ball whose
    # own pixels keep churning is still spinning — not settled. Threshold in
    # detector gray levels, measured on spin_in_place at the 78 Hz detect
    # cadence: spinning cue 2.6-6.5 decaying to the noise floor exactly at
    # visible spin stop. The still-ball floor is lighting-dependent: 1.2-1.5
    # on the TV-less corpus, but up to 2.34 with the wall TV on (its moving
    # image reflects off glossy ball surfaces — the black 8-ball with a white
    # decal read a flat 2.2, measured 2026-07-29). 2.5 clears the TV floor
    # while spin still crosses it late enough (settle ~7.19 s vs. the 7.0 s
    # corpus pin). Recalibrate if the detect stride ever changes.
    spin_churn_max: float = 2.5
    # Spin can only persist a few seconds after a ball stops translating
    # (spin_in_place: 2.5 s), so churn may block settle only within this
    # window of the ball's last real movement. Without it, one glossy ball
    # reading above spin_churn_max under reflections blocks settle *forever*
    # (the 2026-07-29 "clock never starts" failure).
    spin_watch_s: float = 10.0

    # Strike = conjunction: stationary dwell + velocity impulse + coherent trajectory
    pre_strike_dwell_s: float = 0.4
    # 110, not the original 250 (foul reversal, 2026-08-02 — see design.md §8).
    # A shaft/tip bump is a foul, a foul ends the visit, and the incoming
    # player gets a fresh clock — so a bump must fire exactly like a soft shot.
    # That deleted the reason the floor sat where it did: 250 was placed just
    # above soft_touch_0731's 225 mm/s peak specifically to keep the
    # sub-impulse class out.
    #
    # Measured across the class (peak observed raw speed of the ball that
    # actually moved): tip_bump 124, soft_touch_0731 225, shaft_adjust 881.
    # 110 clears the weakest by 11% and still sits 5x over the §4.3 velocity
    # noise ceiling (22 mm/s).
    #
    # **Speed is not what separates a shot from a provoker** — this is the
    # measurement that matters here and it inverts the original design's
    # assumption. practice_strokes' stick fragment peaks at 1576 mm/s, higher
    # than every true event in the class; chalk_near and ball_in_hand_place
    # carry still balls whose noise blips reach 600 mm/s. The floor only
    # decides which candidates *open*. The confirm displacement below is what
    # rejects them, and it is where the real separation lives.
    strike_impulse_mms: float = 110.0
    # Confirm = real travel, in two stages. A shot is confirmed the moment it
    # covers `strike_confirm_displacement_mm`; failing that, it gets one late
    # look at `strike_slow_confirm_mm` once the window has nearly run out.
    #
    # 60 mm stays exactly where it was. It was originally placed to reject
    # shaft_adjust's ~59 mm — which is now a shot — so the foul reversal does
    # invalidate that anchor, but 60 turned out to be load-bearing for a second
    # reason nobody had written down: **it forces the speed measurement to span
    # enough real travel.** `ShotTaken.speed_mms` is
    # max(fastest observed, displacement / candidate lifetime), and both terms
    # are truncated by an early confirm. Dropping the single threshold to 22
    # made break_0801 confirm 0.18 s sooner and report **2068 mm/s instead of
    # 7371** — under `MATCH_BREAK_FLOOR_MMS`, so the IDLE gate would eat the
    # break. That is the exact live failure checkpoint 45 fixed, reintroduced.
    # (Cause: at impact the cue's blob merges with the stick, so the centroid
    # lags and early displacement underreads. A break needs distance, not time,
    # before its speed is honest.)
    #
    # So the fast path is untouched and every real shot fires on the same frame
    # with the same speed as before. Only balls that *never* reach 60 mm — the
    # entire sub-impulse class — reach the slow gate, and by then the full
    # window has been observed, so their speed is as measured as it will get.
    #
    # Measured displacement over the window, well-observed unmerged tracks:
    #
    #   must fire     tip_bump 29.7 · soft_touch_0731 29.0 · shaft_adjust 36.8
    #   must not      practice_strokes 14.5 · ball_in_hand_place 6.8
    #                 chalk_near 6.6 · rack_balls 2.3
    #
    # 14.5 (a stick fragment at 80% observed — the worst false provoker in the
    # corpus) against 29.0 is a clean 2x. 22 is the geometric middle: 1.5x over
    # the provoker, 24% under the weakest true event.
    strike_confirm_window_s: float = 0.30  # was 0.25; the extra 50 ms is where
    # the slow gate is judged, so the fast path keeps its original 0.25 s of
    # travel budget rather than giving frames up to the new gate.
    strike_confirm_displacement_mm: float = 60.0
    strike_slow_confirm_mm: float = 22.0
    strike_slow_confirm_after_s: float = 0.25  # must stay < the window above
    # The candidate must watch the ball move — travel measured from where the
    # ball was when the candidate *opened*, not from the rest/birth anchor it
    # may already have left. Without this, confirm displacement can be
    # satisfied entirely by pre-open history: triage_0801b opened 180.6 mm
    # from the rest spot and fired 19 ms later having travelled **0.5 mm**,
    # reporting 9434 mm/s (displacement-from-rest over the candidate's short
    # life). Measured travel-during-candidate: 12.1 mm (tip_bump, the weakest
    # real event) up to ~45 mm on ordinary shots, against that 0.5 mm — a 24x
    # gap, so 10 sits an order of magnitude clear of the artifact and 20%
    # under the weakest true one.
    #
    # Only reachable because the impulse floor came down: at 250 mm/s that
    # candidate never opened. It is a pre-existing hole, not a new one.
    strike_min_travel_mm: float = 10.0
    # The birth path does NOT get the lowered impulse floor. The whole
    # justification for admitting a 124 mm/s event is chain of custody — the
    # tracker watched that ball sit still and then move. A newborn track has
    # none by definition, and a slow ball appearing from nowhere is far more
    # likely carried than struck: cue_grab_0801's gloved hand lifts the
    # resting cue at 130 mm/s and confirmed as a newborn strike once the floor
    # dropped. Every real birth-path strike in the corpus is fast — 1294 mm/s
    # (jaw_strike_0801) and 1572 (occluded_address_0731) — so this floor keeps
    # its original value with 5x of headroom under the slowest real one.
    newborn_impulse_mms: float = 250.0
    strike_axis_dot_min: float = 0.7
    # The impact itself disrupts the cue's own track (the stick merges with the
    # ball's blob), so the impulse often arrives on a *reborn* track shortly
    # after and some distance from where the cue rested. The rest-memory
    # tolerates that handoff:
    strike_handoff_gap_s: float = 0.35  # max unstill gap between rest and impulse
    strike_handoff_radius_mm: float = 250.0  # max impulse distance from the rest spot
    # ...but a radius is a distance budget, and a ball struck hard enough
    # outruns it while it is invisible (checkpoint 67). The 2026-08-05 live
    # break: the cue left its rest at 4.724 s, went dark for three frames, and
    # founded a new track 320 mm out at 7221 mm/s — refused by a budget
    # anchored on a 2281 mm/s handoff (flag_missed_2, 193 mm). Distance is the
    # wrong currency; the invariant is *time* since the cue was last seen, and
    # inside that the honest bound is physics — the ball cannot be further
    # than its own speed carried it while unseen. This margin covers what that
    # product under-counts: the cue was already moving when last observed, and
    # a newborn's raw speed comes off a centroid smeared across ~43 mm of
    # exposure. Measured d_rest/(raw*dark) on the rescue frame of every real
    # break in the corpus — 1.14 (break_underread_0801), 1.16 (the live
    # break), 1.18 (break_0801) — so 1.5 keeps ~27% headroom over the worst.
    handoff_reach_margin: float = 1.5
    # Flag-switch re-qualification vs the table-motion latch (checkpoint 74).
    # A hard stroke's departing cue can itself latch _table_moving before its
    # track dies (power_shot: 44 mm observed yank), and the latch then blocks
    # the very continuation it certifies — circular. The physics that breaks
    # the circle: after the ~1 ms tip contact only friction acts, so a struck
    # ball CANNOT re-appear faster than its departure was last measured; a
    # carried ball can, because the hand keeps accelerating it. Measured:
    # power_shot re-appears at 1.03x its old track's max observed raw
    # (5854 vs 5698), the pickup_snatch_0801 carry at 2.37x (5506 vs 2327).
    # 1.5 is the geometric middle. Applied ONLY when the latch is up — the
    # latch doubles as the certificate that the departure's speed was
    # measured from real observed displacement (break_0801's 21 mm remnant
    # yank never latches, and its under-read 2068 never faces this test).
    handoff_speed_gain_max: float = 1.5
    # The candidate's trajectory axis is seeded from the cue's displacement
    # off its rest spot at open time. On the impulse's first frame that vector
    # is not motion evidence: filter lag and the stick merging into the blob
    # leave it at noise scale, pointing anywhere (missed_strike_0731: 2 mm,
    # backwards — the axis check then killed the true strike as incoherent,
    # and the step gate expired 8 ms before a re-open). Wait until the
    # displacement is an honest motion vector: above the 8.5 mm largest
    # single-window yank excursion measured on the live captures, about two
    # impulse-floor steps at the 78 Hz detect cadence.
    strike_dir_seed_min_mm: float = 12.0
    cue_id_debounce_frames: int = 2  # ignore 1-frame cue-flag excursions in strike logic
    # (frames at TUNED_HZ — see at_cadence)
    # Carom guard: an impulse into a dwelled cue is only a strike if no *other*
    # ball was both genuinely moving this recently AND within kiss range of
    # the cue's rest spot. A carom's cause is another ball arriving to kiss
    # the cue: its approach trips the same net-displacement criterion the
    # table-motion state uses (moving_enter_mm over moving_window_s, observed
    # positions only), and by physics its last such report lands next to the
    # rest spot — centers touch at ~51 mm, plus at most a frame or two of
    # travel and a short occlusion coast (~120 mm even at break speed). Both
    # conditions are needed: the player's stroke gear (grip hand / stick
    # fragments) also reads as a well-observed mover during the backswing and
    # final stroke, but it does so from behind the bridge — measured 271 mm
    # (medium_shot) and 340 mm (power_shot) from the rest spot. The v1 guard
    # read table-wide settledness instead ("settled <= 0.4 s before the
    # impulse") and player presence starved it: hand/stick junk latched
    # table_moving with a sweep, its 5-6 mm creep blocked settle from
    # re-stamping for seconds, and every real strike in that shadow was
    # rejected (missed_strike_0731 — ~50% of live shots, checkpoint 22).
    # Per-ball motion evidence is windowed and self-clearing: nothing
    # persistent exists to starve.
    strike_other_motion_guard_s: float = 0.4
    carom_reach_mm: float = 200.0
    # Impulse shape (§2.3 lesson 6): a strike steps from rest to full speed in
    # 1-2 frames; a shaft/hand push ramps up over many. The cue must have
    # been reported still this recently for the impulse to count.
    strike_step_max_s: float = 0.12
    # Occluded-address strike (§4.2, occluded_address_0731): when the player's
    # body/stick hides the addressed cue ball long enough for its track to die,
    # the whiteness vote parks on some other bright ball and the strike shows
    # up as a brand-new track born already at impulse speed (measured: born at
    # 1572 mm/s, 964 mm from the wrong flag-holder's rest spot — every
    # rest-anchored gate correctly rejects it). A ball cannot teleport into
    # existence at speed: on a table with no mover for a pre-strike dwell and
    # none within kiss range of the birth spot, such a track is the struck cue
    # emerging from the occlusion. The candidate anchors on the birth spot.
    newborn_strike_max_age_s: float = 0.1  # cue-vote debounce (2 frames) plus
    # a few association frames at the 78 Hz detect cadence; the real case
    # opened 0.012 s after birth
    # The dead-rest fallback branches (`_maybe_open_candidate`: no rest, or a
    # still-stamp gone stale) get a longer age budget. Those branches run
    # when the flag's own story is already broken — the crown had nowhere to
    # be for seconds — and a live address emergence needs the time: the
    # track is born from a messy stick-merge whose polluted centroid reads
    # slow (first raw >= newborn_impulse_mms at age 0.179 on flag_missed_2),
    # and the debounced crown that routes this fallback arrived at age 0.31,
    # 3x over the tight gate — while the junk stick blob next to it, born
    # clean, passed at age 0.0. The tight gate anti-selects for junk there.
    # The birth-radius gate still bounds the claim (the candidate must be
    # within strike_handoff_radius_mm of its OWN birth spot); 0.6 covers the
    # measured 0.18-0.31 with the 2x headroom the other occlusion windows
    # carry. The too_far fallback keeps the tight gate: its real dependent
    # (occluded_address_0731) opened at 0.012 s, and the widened gate there
    # admitted a crowned body fragment born 768 mm from the rest 2.5 s
    # before the real shot (stroke_tug_strike_0801, 2026-08-06).
    newborn_dead_rest_max_age_s: float = 0.6
    # A hand-carried cue ball (scratch_replace, ball-in-hand) can also be born
    # at speed with the table quiet. Two measured discriminators, both far
    # from both sides: a struck cue on open cloth is observed every confirm
    # frame with raw speed smooth within ±5%; a carried ball's track is
    # 27-40% observed (fingers wrap it, long coast runs) with raw speed
    # erratic (575 → 4140 mm/s between adjacent frames).
    newborn_confirm_obs_min: float = 0.9
    newborn_raw_step_ratio_max: float = 2.0
    # Third carried-ball discriminator (pickup_newborn_0801): a steadier
    # carry beats both gates above (its 8 confirm observations were fully
    # observed with smooth speed) — but every one of them was a MERGED blob,
    # because a ball in a hand is inseparable from the hand. A struck ball
    # on open cloth detects clean: measured 0% merged confirm observations
    # on every real strike in the corpus (occluded_address_0731,
    # scratch_shot_0801, break_0801 at 7361 mm/s, five more) with one 11%
    # outlier (no_cue_strike_0801, non-newborn). 0.5 sits 4x above the
    # worst real strike and 2x below the measured carry (100%).
    newborn_merged_frac_max: float = 0.5
    # Parked-vote birth strike (flag_missed_2, checkpoint 66). When the flag
    # sits on an *observed* ball, the birth path loses the vote's corroboration
    # — the one thing that told it this newborn is the cue — so it must earn
    # the identity from the ball's own behaviour instead. A struck cue crosses
    # the table; a stick fragment, a rebirth blob or a hand-moved ball does not.
    #
    # Measured over 1 s from the birth spot on every candidate this
    # configuration admits corpus-wide: the real strike (flag_missed_2, the
    # potted cue) travels **962 mm**, against 375 (address_chatter_miss_0801),
    # 328/382 and 328 x4 (scratch_replace, including its 3179 mm/s throw),
    # 267/277 (stroke_tug_strike_0801) and 256/202 (parked_vote_strike_0801).
    # 600 sits 1.6x above the worst false and 1.6x below the true one — the
    # geometric middle of a 2.5x gap. The real strike crosses it 0.435 s after
    # the open, which sets the window below.
    #
    # Note the cost this does NOT pay: rest-anchored and cue-quiet birth
    # candidates are untouched, so every ordinary strike keeps its 60 mm/0.30 s
    # confirm. Only the weakest identity claim in the engine waits.
    parked_confirm_travel_mm: float = 600.0
    parked_confirm_window_s: float = 0.60  # 0.435 s measured + margin
    # Fast tier of the parked confirm (flag_missed_3, 2026-08-06). The 600 mm
    # travel confirm demands ~1000 mm/s AVERAGE over the whole window, and a
    # live in-flight cue track rarely survives that long — the real struck
    # cue fragmented into three successive tracks on one shot, and its
    # 1865-1990 mm/s fragment died parked at 0.6 s having never been allowed
    # the 60 mm confirm. Every false this parking was anchored on is slow:
    # the practice-stroke blob at 791 mm/s, ball 11's spike suppressed to
    # noise by two-step corroboration, stick retractions at 340-460. A
    # parked candidate whose target shows TWO CONSECUTIVE observed raw
    # steps at this floor (min(prev, raw) — the same one-corrupt-centroid
    # suppression impulse_raw uses, so a single merged yank cannot certify)
    # is moving like a struck ball and confirms on the normal displacement
    # thresholds. 1200 is 1.5x the measured false ceiling (791) and 0.64x
    # the slowest real that needs the tier (1865).
    parked_fast_confirm_mms: float = 1200.0
    # Minimum confirm time for birth-path candidates (#8, pickup_snatch_0801,
    # 2026-08-02). The three gates above are all *fractions over the confirm
    # window*, and a fast confirm can close that window in three observations
    # — at which point they are a lottery rather than a measurement. The
    # bare-hand snatch fired with merged_frac exactly **0.00** over 0.070 s
    # while reading 71% merged across the whole carry.
    #
    # Nothing was wrong with the gates. `newborn_raw_step_ratio_max` is the
    # documented carried-ball discriminator (a carry's raw speed is erratic;
    # a struck ball's is smooth within +/-5%) and it *does* catch this snatch:
    # traced frame by frame, the ratio runs 1.01, 1.00, 1.07 and then **2.24**
    # at age 0.096 s. The candidate simply confirmed at age 0.070 — one frame
    # before the evidence arrived. All this constant does is make a birth-path
    # candidate wait for it.
    #
    # The cost lands only on birth-path strikes — the occluded-address class,
    # which live sessions were missing outright — where a tenth of a second is
    # irrelevant to a 30 s clock. Rest-anchored strikes, which is nearly all
    # of them, are untouched. Measured: 0.06 s is too short (the snatch still
    # fires), 0.08 s works; 0.10 keeps a frame of margin over that cliff and
    # still leaves two thirds of the confirm window.
    #
    # MEASURED AND REJECTED — do not re-derive. The plan for #8 proposed an
    # apparent-size height cue: the camera looks straight down (§4.3), so a
    # carried ball images larger. Both forms were built and measured against
    # the corpus, and neither can fire on this clip:
    #
    #   * Ratio against balls resting on the cloth. Real newborn strikes read
    #     0.82-1.22, the carry reads 2.45 — a clean 2x split. But **at the
    #     instant the phantom confirms the ball reads 1.14**, mid-band: it has
    #     only just been touched and has not risen yet. It passes 2.0 about
    #     25 ms later. A gate cannot use evidence that does not exist yet.
    #     (It also misjudges no_cue_strike_0801, a real strike at 2.62, where
    #     the cue is merged with the stick through the impact.)
    #   * Growth during the confirm window. Strictly worse: a ball
    #     accelerating out of a strike blurs and streaks, so break_0801b grows
    #     1.58x against the snatch's 1.27x. It also resets every time the
    #     carried ball is reborn as a fresh track, which it does repeatedly.
    #
    # The measurements are kept here because they are real and were expensive;
    # the gate is not, because no clip exercises it and untested gates rot.
    newborn_confirm_min_s: float = 0.10
    # Same idea for the handoff path (phantom_flag_strike_0805), and measured
    # the same way. A handoff candidate is rest-anchored, so it does NOT run
    # the merged gate — a ball struck out of an address is merged with the
    # stick that struck it (measured 100% merged confirm observations on the
    # real strike), which is exactly why the birth path cannot be the fix. The
    # observation fraction carries the whole carried-ball judgement instead, so
    # it has to be a measurement rather than three lucky frames.
    #
    # Observation fraction vs. confirm age, over the clips that must fire and
    # the hand class that must not:
    #
    #   age    strike 0805  scratch_shot   cue_grab  pickup_newborn  shaft_adjust
    #   0.10       1.00        1.00          1.00        1.00           0.25
    #   0.15       1.00        1.00          0.82        0.92           0.21
    #   0.20       1.00        1.00          0.64        0.69           0.22
    #
    # 0.20 s is the first age at which the existing `newborn_confirm_obs_min`
    # (0.9) separates them — at 0.10 s two of the three carries read a perfect
    # 1.00 and at 0.15 s pickup_newborn's 0.92 still clears the floor. So the
    # fraction needs no new constant; only the wait does. It leaves a third of
    # `strike_confirm_window_s` for the confirm to land, and 0.2 s is nothing
    # against a 30 s clock (same trade as newborn_confirm_min_s).
    handoff_confirm_min_s: float = 0.20
    # ...except that a break's cue is not on open cloth by the time the floor
    # is read. It has already reached the rack, and the tracker loses it inside
    # the scatter it caused (checkpoint 67). The whole population that reaches
    # this floor, corpus-wide, by observed fraction at the gate frame:
    #
    #   0.31 shaft_adjust    0.56 the live break   0.64 cue_grab_0801
    #   0.69 break_underread_0801 / pickup_newborn_0801    0.81 break_0801
    #   1.00 x5 (scratch_shot_0801, phantom_flag_strike_0805, and the
    #            three 0805 scenarios)
    #
    # So the fraction cannot simply be loosened: the live break reads 0.56,
    # *below* both the grab and the pickup it exists to reject. Nor can the
    # unbroken observed run off the open frame — 7 frames for the break
    # against 9 for the grab and 11 for the pickup. Speed is what separates,
    # by an order the corpus leaves no doubt about: everything that must stay
    # rejected tops out at 2968 mm/s (shaft_adjust) and every break starts at
    # 7371 (break_0801, then 9469 and 9923). A hand cannot move a ball this
    # fast — the quickest hand-driven ball measured anywhere in the corpus is
    # scratch_replace's throw at 3179 mm/s (checkpoint 66) — and at this speed
    # occlusion is expected rather than suspicious, because the ball crosses
    # the table and buries itself in other balls inside the window. 5000 sits
    # 1.6x above that throw and 1.5x below the slowest real break.
    handoff_obs_floor_exempt_mms: float = 5000.0
    # Flicker filter: hand/stick fragments pass the detector's geometry gates
    # only intermittently, so their tracks are mostly coasted — measured 7-43%
    # observed on the flagged live captures (missed_strike_0731,
    # live_divergence_0731a) while every real ball reads 90-100%. A track
    # below this observed fraction over the creep window is not judged for
    # table motion or stillness: junk can neither latch table_moving (a hand
    # sweep is not a shot settling later — that would reset the clock) nor
    # hold settle hostage with centroid creep (the 11.8 s "clock stays paused
    # while my hands are in frame" stall, checkpoint 22).
    judge_obs_fraction_min: float = 0.7
    # The roster this floor feeds is *negative* evidence — "this track was
    # not standing on the last quiet table, so distrust it" — and #40 is the
    # class that can never satisfy it: a cue reborn at the impact is born
    # mid-motion by construction. `_struck_mover` is the positive-evidence
    # path that lets such a track latch anyway; it adds no constant of its
    # own, reusing `parked_fast_confirm_mms` and `newborn_merged_frac_max`
    # below, and the measurement admitting it is in that method's docstring.

    # Pickup discard (ball_in_hand_pickup_0801, checkpoint 28): a hand pickup
    # latches table motion (a roster ball genuinely drags) and then the
    # ball leaves the table — the subsequent quiet is not a shot settling,
    # and the settle it minted reset the clock mid-visit. Discriminators,
    # measured: a picked-up ball's track dies FAR from any pocket after a
    # hand-scale drag (41-78 mm across the pickup captures), while a real
    # shot always ends with a live mover still on the table (shot_debug's
    # object ball) or a death inside a pocket zone after real travel (its
    # potted cue: 1699 mm). When every ball that latched the episode has
    # died off-pocket under this travel, the episode is a pickup: unlatch
    # without a settle. 400 mm = 1.9x the measured drag max (41-78 mm on the
    # original pickup captures, then 209.6 mm on cue_grab_0801's
    # snatch-and-lift, which blew past the old 200 ceiling and minted a
    # phantom settle), still 4.2x under the shortest measured real-shot
    # travel (1699 mm).
    pickup_travel_max_mm: float = 400.0

    # Scratch
    cue_lost_latch_s: float = 0.3
    # Scratch is a *deferred* verdict (#6, 2026-08-02). A cue that stops
    # hanging in the pocket jaw is indistinguishable from a potted one at the
    # moment its track dies — both end with the last observation inside the
    # pocket zone — so hanger_pot_0801 fired a Scratch 0.75 s after a legal
    # shot Steven confirmed. The counter-evidence is unambiguous but it
    # arrives late: a ball reappears at the death spot. Measured there, a
    # reborn track shows up 0.24 s after the loss, 33 mm away, converging to
    # 12 mm. On all three genuine scratches (scratch_shot_0801, shot_debug,
    # scratch_replace) **nothing at all** is observed within 200 mm for the
    # next 3 s. That is the whole discriminator; it just has to be waited for.
    #
    # 1.0 s is 4x the one measured reappearance, and it costs the real
    # scratches nothing: each has 3.5-6.6 s of clip left when it fires, and
    # Scratch drives no clock state in v1 (§8), so latency here is free.
    # Bias is deliberate — waiting longer can only suppress phantoms.
    #
    # NOTE: the emitted `t` is therefore the *confirmation* time, roughly
    # scratch_confirm_s after the ball actually vanished. Subtract it when
    # correlating a Scratch against clip footage.
    scratch_confirm_s: float = 1.0
    # 60 mm: above the 33 mm first sighting with margin, and well under the
    # 94 mm at which a genuinely different, stationary ball sat during that
    # same episode — which must not cancel anything. Balls are ~51 mm.
    scratch_reappear_mm: float = 60.0
    # 120, not 80: a sinking ball's last observations come from the pocket-jaw
    # blob, whose centroid drifts past the table edge as the ball goes down
    # (cue seen at 57+ mm outside the corner in the corpus scratch clip)
    pocket_zone_radius_mm: float = 120.0
    # Two-stage verdict (#26, 2026-08-07). The reappear watch above acquits
    # hangers, but a potted *object ball* mis-crowned as the cue passes every
    # gate honestly — it died near a pocket while moving, and it genuinely
    # never reappears. Live journals confessed a ~6x over-count from exactly
    # this shape. The one fact the watch never checks is the one that
    # matters: after a real scratch there is NO cue on the table until the
    # player replaces it; after a false one the real cue is sitting right
    # there. So the watch's output is a ScratchCandidate, and the verdict
    # waits (scratch is log-only — latency is free) for a whiteness census:
    #
    #   - REFUTE on positive evidence only: one roster ball, judgeable,
    #     still + observed, whiteness EMA >= the floor, born before the dead
    #     track's last observation. "No cue-grade ball visible" proves
    #     nothing — the corpus shows the real cue absent from the census for
    #     a 41 s occluded address (drill_shot_0801).
    #   - CONFIRM on complete census: table quiet and every live roster ball
    #     observed, none qualifying. All three corpus scratches reach this
    #     within ~1-3.5 s of the candidate, so they still confirm in-clip.
    #   - The birth gate is mandatory: a hand-placed replacement cue reads
    #     0.880 within ~1 s of landing (scratch_replace) — a witness born
    #     after the cue died must never refute, or every replaced real
    #     scratch gets acquitted.
    #
    # Floor 0.72: corpus-wide sweep of the rest-EMA (2026-08-07, every
    # confirmed track at observed stillness across all 62 scenarios). The
    # sustained never-crowned ceiling is 0.613 (a stripe at rest, white face
    # up, 595 samples; pool p99 0.542) and the real cue's converged rest
    # band bottoms at ~0.83-0.87 — 0.72 is the corridor midpoint, 0.11 clear
    # both ways. One single-frame flash track read 1.000; the judgeable +
    # roster witness gates exclude that class structurally. Known leak,
    # priced: a real cue at rest in deep top-rail shade reads 0.53
    # (cluster_rest_shot_0806) and cannot refute — that false scratch
    # stands, which is exactly today's behavior, never a new failure mode.
    scratch_witness_whiteness: float = 0.72
    # Fallback horizon from the candidate latch if the census never
    # completes (perma-occlusion): confirm, preserving the pre-#26 upper-
    # bound behavior. 30 s covers the measured live tail with headroom —
    # across all 15 sessions, 68/68 scratch candidates had a settle within
    # 19.4 s of the trigger (p90 5.5 s).
    scratch_verdict_horizon_s: float = 30.0
    table_length_mm: float = 1930.0
    table_width_mm: float = 965.0

    def at_cadence(self, hz: float) -> "EventConfig":
        """This config at another detect cadence: frame counts re-expressed
        as the same span of time they cover at :data:`TUNED_HZ`."""
        if hz == TUNED_HZ:
            return self
        frames = max(1, round(self.cue_id_debounce_frames * hz / TUNED_HZ))
        return replace(self, cue_id_debounce_frames=frames)

    def pocket_centers(self) -> tuple[tuple[float, float], ...]:
        length, width = self.table_length_mm, self.table_width_mm
        return (
            (0.0, 0.0),
            (length / 2, 0.0),
            (length, 0.0),
            (0.0, width),
            (length / 2, width),
            (length, width),
        )


@dataclass
class _StrikeCandidate:
    t0: float
    x0: float
    y0: float
    dir_x: float
    dir_y: float
    # Newborn-origin candidates (occluded-address strike) carry extra confirm
    # gates: full observation and smooth raw speed, the measured separators
    # from a hand-carried ball. last_raw remembers the previous advance
    # frame's raw speed for the step-ratio check. ball_id is set when no ball
    # holds the cue flag at open time: the candidate then follows that track
    # directly instead of the (empty) cue flag.
    newborn: bool = False
    # Handoff-origin candidates (phantom-flag strike) are rest-anchored, so
    # they skip the merged gate a struck-out-of-address ball cannot pass — and
    # pay for it with an observation floor held open long enough to measure.
    handoff: bool = False
    # Parked-vote birth candidates opened while an *observed* ball still held
    # the cue flag, so the whiteness vote corroborates nothing. They confirm on
    # travel instead — see `parked_confirm_travel_mm` — unless the target
    # certifies struck-ball speed (two consecutive observed raws at
    # `parked_fast_confirm_mms`; latched here because the fragment's track can
    # die between certifying and confirming).
    parked: bool = False
    fast_certified: bool = False
    last_obs_raw: float | None = None  # observed-frames-only chain for the fast tier
    last_raw: float | None = None
    ball_id: int | None = None
    opened_on: int | None = None  # the track id the candidate's target had at
    # open time. A flag-following candidate (ball_id=None) advances on
    # whatever holds the cue flag, and the whiteness vote can move the flag
    # to a *different track* mid-confirm — the candidate's displacement is
    # then measured across an identity switch, not along one ball's travel.
    # The dispatch in `update` kills the candidate when that happens and
    # makes the new holder re-qualify from scratch (see the comment there).
    impulse_raw: float = 0.0  # max raw speed seen while the candidate lives.
    # Not just the opening sample: on a handoff strike the open-frame raw is
    # averaged across the address-occlusion gap and reads far low (the
    # 2026-08-01 gated break opened at 2072 mm/s while the cue measurably
    # flew 5300-5500 — the confirm frames carry the honest speed).
    # Where the ball actually was when the candidate opened. (x0, y0) is the
    # rest/birth *anchor*, which the ball may already have left — so confirm
    # displacement measured from it can be satisfied by travel that happened
    # before this candidate existed. See strike_min_travel_mm.
    x_open: float = 0.0
    y_open: float = 0.0


@dataclass
class _BallHistory:
    # (t, x, y, observed, merged, raw_mms) — observed=False rows are coasted
    # extrapolations; merged marks oversized-blob observations; raw_mms is the
    # one-step raw speed, kept because filtered positions absorb only ~30% of
    # a departure step and cannot reconstruct how fast a dying track was last
    # *measured* going (the flag-switch re-qualification needs exactly that)
    positions: deque[tuple[float, float, float, bool, bool, float]] = field(default_factory=deque)


class EventEngine:
    """Feed per-frame track states; collect events. See module docstring."""

    @property
    def table_moving(self) -> bool:
        """Is a motion episode latched? Read by the pipeline's settle backstop
        so it never speaks over an episode the engine is already tracking."""
        return self._table_moving

    def __init__(self, config: EventConfig | None = None) -> None:
        self._cfg = config or EventConfig()
        self._history: dict[int, _BallHistory] = {}
        self._table_moving = False
        # #33 settle evidence: the most recent engine settle's episode movers
        # (see SettleMover). Set in the settle branch just before the episode
        # state clears; read by the pipeline the same way the rearrangement
        # summary is.
        self.last_settle_movers: list[SettleMover] = []
        self._all_still_since: float | None = None
        # Settle-dwell chatter guard (settle_stall_0801, the 2026-08-01
        # evening 12-16 s late-settle cluster): a corrupt observation stream
        # on ONE parked ball — the stick lying across it left a marginal
        # half-ball diff blob whose centroid flipped between two modes
        # 7-10 mm apart — trips the creep gate in brief bursts, and every
        # single supra-gate frame used to wipe the whole table's accumulated
        # dwell credit. With bursts every ~0.2-0.3 s a clean 0.5 s dwell
        # window almost never occurs: the clip stalls with no settle for
        # 10 s, and live settles became a lottery (12.0-16.3 s on 10 of that
        # session's last shots; the same mechanism is the long tail in every
        # session's latencies — 17.9/18.4 s, 17.3/50.0/47.6 s, 17.0/16.9 s).
        # Measured discriminator: corrupt excursions persist <= 64 ms (40+
        # runs on the clip's parked phase), while genuine motion holds the
        # un-still verdict continuously — >= 371 ms across the corpus
        # (hanger_pot_0801 pre-shot 371 ms, strike 794 ms, reborn roller
        # 1434 ms; slow_creep's creep one unbroken 3469 ms run whose end
        # + dwell IS the settle time). An un-still verdict therefore only
        # erases dwell credit once it has persisted for a full
        # moving_window_s (the existing displacement-evidence window,
        # 3.1x the worst corrupt run, 1.9x under the shortest genuine one);
        # this stamp tracks the current un-still run's start. Settle still
        # fires only on a still-verdict frame, so a genuine mover blocks it
        # from its very first frame exactly as before — the persistence
        # window only decides whether dwell credit survives a transient.
        self._unstill_since: float | None = None
        # Cue-ball rest memory: (t_still_start, x, y) of the current rest spot,
        # plus when the cue was last seen still. Survives the strike-time track
        # handoff (see EventConfig).
        self._cue_rest: tuple[float, float, float] | None = None
        self._cue_last_still_t = 0.0
        # Carom guard memory: (t, x, y, ball_id, hand) of every recent
        # genuine observed non-cue mover, pruned to the guard window.
        # Survives the mover's track dying at the kiss (blob merge). Two
        # sources: roster movers (hand=False, the classic bringer — read by
        # all three strike paths), and merged, impulse-fast, non-roster
        # tracks (hand=True): a hand/arm reaching in reads as exactly that
        # (cue_grab_0801: the gloved hand crossed at 618-1921 mm/s, merged
        # on every observation, 116 mm from the cue's rest, two frames
        # before the "impulse"). Hand stamps are read by the rest-anchored
        # cue path ONLY: the birth-evidence paths fire on balls the player
        # is by definition reaching over (the stroke's own merged stick/arm
        # blob stamped 66-72 mm from lone_roller_0801's birth spot and
        # vetoed the real strike when hand stamps guarded them too). The
        # ball_id lets an open skip stamps from its own candidate track (a
        # struck ball must not veto itself during a handoff).
        self._recent_movers: deque[tuple[float, float, float, int, bool]] = deque()
        self._cue_id_last: int | None = None
        self._cue_id_streak = 0
        self._candidate: _StrikeCandidate | None = None
        # Axis-kill re-open credit (stroke_tug_strike_0801): the final stroke
        # can tug the addressed cue's centroid just past the 12 mm seed floor
        # (measured 12.04 mm) ~0.1 s before impact, opening a candidate with
        # a garbage axis. The axis check rightly kills it — the design intent
        # is that "the real impulse can still re-open from the same rest" —
        # but by then the step gate's still-stamp is stale (expired 0.01 s
        # short on the live miss) and the re-open never happens. An axis kill
        # is itself evidence that an impulse is in progress: it re-arms the
        # step gate for one strike_step_max_s window.
        self._axis_killed_t: float | None = None
        self._cue_last_seen: tuple[float, float, float, float] | None = None  # (t, x, y, speed)
        # Color forensics for the same observation: (whiteness EMA, its age).
        # Snapshotted into the ScratchCandidate so every live verdict records
        # how convincing — and how stale — the dying crown's color evidence was.
        self._cue_last_color: tuple[float | None, float | None] = (None, None)
        self._cue_lost_while_moving = False
        self._scratch_fired = False
        # A latched but unpublished scratch: (trigger_t, x, y, last_obs_t) of
        # the cue's last observed position. Resolved by _resolve_pending_scratch.
        self._pending_scratch: tuple[float, float, float, float] | None = None
        # A published ScratchCandidate awaiting its verdict: (latch_t,
        # cutoff_t). cutoff_t is the dead track's last observation — the
        # witness birth gate (#26): any track born at or after that moment
        # could be the hand-placed replacement and must never refute.
        self._scratch_open: tuple[float, float] | None = None
        # Per-ball last time it visibly translated (first sighting counts):
        # bounds how long the spin-churn gate may hold settle (spin_watch_s).
        self._last_translate: dict[int, float] = {}
        # Per-ball current translation run: (first stamp of the run, the
        # stamp that stood before it). Lets the own-rest strike path test
        # dwell against the stamp as of BEFORE the impulse's own onset — a
        # cluster ball's dwell is presence-witnessed, never pixel-witnessed,
        # so the impulse frames must not be billed against it
        # (flag_missed_3, 2026-08-06). A new run starts when a stamp lands
        # more than moving_window_s after the previous one.
        self._translate_run: dict[int, tuple[float, float]] = {}
        # Per-ball open creep-gate excursion: (start t, pre-excursion x/y,
        # pre-excursion translate stamp). Translate stamps land immediately —
        # a hand ramp or tip bump must break the strike dwell on its very
        # first supra-gate frame, exactly as before — but an excursion that
        # CLOSES within the motion window back at its starting spot was
        # observation chatter, not travel, and the stamps it wrote are
        # revised away (checkpoint 35's measured discriminator: corrupt
        # excursions persist <= 64 ms and return to base — centroid modes
        # 7-10 mm apart, zero net travel — while genuine motion holds
        # >= 371 ms or ends displaced). Without the revision, address
        # chatter on a rested ball kept `_last_translate` current and the
        # own-rest strike path could never see a dwell
        # (`address_chatter_miss_0801`: a real 1396 mm/s stroke unseen).
        self._translate_excursion: dict[int, tuple[float, float, float, float]] = {}
        # Birth registry: when and where each live track first appeared.
        # Feeds the occluded-address strike path (a track born at impulse
        # speed) and the roster rule below.
        self._first_seen: dict[int, float] = {}
        self._birth_pos: dict[int, tuple[float, float]] = {}
        # Ball roster: only tracks that were part of a quiet table state may
        # latch table motion. Address gear (glove knuckles, watch, stick
        # fragments) reaches 79-90% observed — past the flicker filter — but
        # in every measured clip real latches come from tracks alive since
        # the last quiet period, while gear tracks are born mid-play and
        # already moving. Membership: tracks seen during the engine's
        # founding window, then wholesale refresh with the judgeable tracks
        # present whenever the table has been continuously still for a settle
        # dwell (not only at BallsSettled: racking and hand-placement produce
        # quiet tables without a latch, and the newly placed balls must be
        # able to latch the next shot). A gear fragment sweeping in cannot
        # get admitted mid-sweep — its own motion breaks the quiet streak.
        self._roster: set[int] = set()
        self._engine_t0: float | None = None
        # Struck-ball certification for the latch admission above (#40): track
        # ids that have shown two consecutive *observed* raw reports at
        # `parked_fast_confirm_mms`, and the previous observed raw each track
        # was measured at. Sticky, like `_StrikeCandidate.fast_certified` and
        # for the same reason — the evidence must outlive the frame it was
        # measured on, because a struck ball's track blurs, merges and coasts
        # through the frames right after the impact.
        self._fast_certified: set[int] = set()
        self._last_obs_raw: dict[int, float] = {}
        self._last_mover_t = float("-inf")  # last genuine observed mover, any position
        # Motion-episode memory for the pickup discard: for every roster ball
        # that latched this episode, its pre-motion position (oldest history
        # row at first latch) and its last observed position.
        self._episode_origin: dict[int, tuple[float, float]] = {}
        self._episode_last: dict[int, tuple[float, float]] = {}
        # Sticky per-episode marker: a judgeable ball outside the mover set
        # translated while the table was latched — real motion is running
        # free (a struck ball's newborn continuation is not roster, so it
        # never joins the mover set), and the episode can never be a pickup.
        self._episode_free_mover = False
        # A ShotTaken fired in this episode: the pickup discard is then
        # structurally wrong — the engine has already certified the motion
        # as a shot, so the quiet that follows IS the shot settling, and the
        # clock needs the BallsSettled. Without this, the struck ball's
        # original track dying mid-streak (its continuation untracked for a
        # beat) read as "every latcher died off-pocket" and the discard
        # silently unlatched a live shot (pair_strikes_0801 lost its first
        # settle that way; live, the same shape froze the clock in MOTION).
        self._fired_this_episode = False
        # Last time the whiteness-vote holder was actually *observed*, not
        # coasted. The handoff path (_maybe_open_handoff) reads it to tell the
        # cue's own continuation from a bystander: only a track born after the
        # cue went dark can be the ball that left the rest spot.
        self._cue_last_obs_t: float = float("-inf")
        # Measured detect cadence — see the note in `update`. None until two
        # frames have been seen; `_near_pocket` then gives a still-unmeasured
        # engine no flight allowance at all, which is the safe direction.
        self._prev_update_t: float | None = None
        self._frame_dt_s: float | None = None

    def update(self, t: float, balls: list[BallState]) -> list[Event]:
        events: list[Event] = []
        cfg = self._cfg
        cue = next((b for b in balls if b.is_cue), None)
        was_moving = self._table_moving
        if cue is not None and cue.observed:
            self._cue_last_obs_t = t
        # The engine's own detect cadence, measured rather than configured, so
        # it tracks the live rate (78 Hz healthy, 11-20 Hz starved) instead of
        # assuming it. `_near_pocket` is the only reader: an observation of a
        # moving ball is one of these stale by construction.
        if self._prev_update_t is not None:
            dt = t - self._prev_update_t
            if 0.0 < dt < 1.0:  # ignore replay seeks and the first frame
                prev = self._frame_dt_s
                self._frame_dt_s = dt if prev is None else 0.9 * prev + 0.1 * dt
        self._prev_update_t = t

        # Birth registry + roster founding. Tracks present within the first
        # creep window of engine life are founding roster members (the engine
        # starts on a settled table); later admission happens only at settle.
        if self._engine_t0 is None:
            self._engine_t0 = t
        for ball in balls:
            if ball.ball_id not in self._first_seen:
                self._first_seen[ball.ball_id] = t
                self._birth_pos[ball.ball_id] = (ball.x_mm, ball.y_mm)
                if t - self._engine_t0 <= cfg.settle_creep_window_s:
                    self._roster.add(ball.ball_id)

        # ---- strike detection ----
        # Debounce the cue flag: the tracker's whiteness vote can visit the
        # wrong ball for a frame around the impact (merged blobs corrupt the
        # sample); a one-frame excursion must not rewrite the rest memory.
        if cue is not None:
            if cue.ball_id == self._cue_id_last:
                self._cue_id_streak += 1
            else:
                self._cue_id_last, self._cue_id_streak = cue.ball_id, 1
        if self._candidate is not None and t - self._candidate.t0 > self._confirm_window(
            self._candidate
        ):
            # Centralized expiry: a candidate whose target cannot advance
            # (cue-follow candidate while the flag is empty, or a no-cue
            # candidate whose track died) must not linger and block the scan.
            self._candidate = None
        if self._candidate is not None:
            # The shot's honest speed is the fastest observed ball anywhere
            # while the candidate confirms — the table was quiet by the dwell
            # gate, so fast motion in this window IS the shot. The candidate's
            # own target can read far low on a handoff strike: the 2026-08-01
            # gated break advanced on the old track's remnant (2072 mm/s peak)
            # while the reborn cue measurably flew 5300-5500.
            # A one-step raw speed is the difference of two centroids, so a
            # single corrupt centroid (stick/shadow merged into the blob,
            # association jitter) fakes exactly one spike — real fast travel
            # sustains across steps. Each ball contributes min(current raw,
            # its previous observed raw): the gated break's reborn cue reads
            # 5300-5500 on consecutive steps and passes untouched, while the
            # merged yanks that were being reported as the shot's speed
            # (late_emergence_strike_0805: a 3687 spike between 1195 and 551,
            # published as golden 3888.8; address_chatter_miss_0801: 8152 on
            # a 1409 mm/s shot) never repeat and are suppressed.
            fast = []
            for b in balls:
                if not b.observed:
                    continue
                cur = b.speed_mms if b.raw_speed_mms is None else b.raw_speed_mms
                hist = self._history.get(b.ball_id)
                prev = (
                    None
                    if hist is None
                    else next((p[5] for p in reversed(hist.positions) if p[3]), None)
                )
                if prev is not None:
                    fast.append(min(cur, prev))
            if fast:
                self._candidate.impulse_raw = max(self._candidate.impulse_raw, max(fast))
        # A parked-vote candidate is the weakest claim the engine can hold and
        # it occupies the slot for twice as long as anything else, so it must
        # never shut out the flag-borne path: the cue's own flag stepping off
        # its own remembered rest outranks a newborn the vote does not
        # corroborate at all. Without this a merged stroke blob could park the
        # slot and swallow the real shot behind it for the whole 0.60 s window
        # (test_stroke_gear_behind_the_departing_cue_does_not_reject_strike:
        # the gear sweeps in at 900 mm/s, the cue then departs at 1200 and
        # never got evaluated). Same shelve-and-restore shape as the scan
        # below, and the same principle — strongest anchor first.
        if (
            self._candidate is not None
            and self._candidate.parked
            and cue is not None
            and cue.observed
            and self._cue_id_streak >= cfg.cue_id_debounce_frames
        ):
            parked = self._candidate
            self._candidate = None
            self._maybe_open_candidate(t, cue)
            if self._candidate is None:
                self._candidate = parked
        if self._candidate is not None and self._candidate.ball_id is not None:
            # A no-cue candidate follows its own track, not the cue flag.
            target = next((b for b in balls if b.ball_id == self._candidate.ball_id), None)
            if target is not None:
                events.extend(self._advance_candidate(t, target))
            # The flag's rest/still bookkeeping is orthogonal to who holds
            # the candidate slot and must not starve while a follow-ball
            # candidate occupies it: a parked scan open on a stroke fragment
            # froze the still-stamp for 0.6 s and the real cue's flag-borne
            # open then read its own quiet address as a stale ramp
            # (stroke_gear_veto_0802, 2026-08-06 — the fire the gear-behind
            # exemption exists to admit).
            if cue is not None and self._cue_id_streak >= cfg.cue_id_debounce_frames:
                self._update_cue_rest(t, cue)
        elif cue is not None and self._cue_id_streak >= cfg.cue_id_debounce_frames:
            if self._candidate is not None and self._candidate.opened_on not in (
                None,
                cue.ball_id,
            ):
                # The debounced flag moved to a DIFFERENT track while a
                # flag-following candidate was open. Advancing would measure
                # displacement from the old holder's rest to the new holder's
                # position — track-switch inheritance, not ball travel. On a
                # struck cue reborn past the association gate that number is
                # honest (the same ball, continuing); on a snatched cue
                # re-detected in a moving hand it is a phantom (the annulus
                # correctly follows the physical ball into the hand, and
                # pickup_snatch_0801 fired a 6108 mm/s "shot" — the identity
                # got *better* and exposed that this path never re-judges a
                # switch). Kinematics cannot separate those two in-window
                # (yank 2068→5470 vs 2327→5473, both collinear), so the
                # candidate dies and the new holder re-qualifies through the
                # handoff path — the one built for "a young track claims the
                # old rest". Its gates judge exactly this pair: the
                # quiet-table latch (a grab's drag latches motion before the
                # birth; a stroke's one-frame yank does not — measured on
                # this pair), the born-after-dark ordering, the dark-scaled
                # reach, and a confirm with no merged gate — a genuinely
                # struck rebirth streaks, and streaks are merge-flagged
                # (break_underread_0801 died 4/9 merged in the newborn
                # confirm when routed there). The ordering must run on the
                # OLD holder's timeline: _cue_last_obs_t already tracks the
                # new holder, which is observed by construction.
                old_id = self._candidate.opened_on
                self._candidate = None
                raw = cue.speed_mms if cue.raw_speed_mms is None else cue.raw_speed_mms
                old_hist = self._history.get(old_id) if old_id is not None else None
                last_obs_old = (
                    None
                    if old_hist is None
                    else next((p[0] for p in reversed(old_hist.positions) if p[3]), None)
                )
                # The old holder's own departure can latch _table_moving,
                # which would block its continuation inside the handoff —
                # circular, the departure IS the strike evidence. When the
                # latch is up, the physics test stands in for it: a struck
                # ball cannot re-appear faster than its departure was last
                # measured (see handoff_speed_gain_max).
                quiet_enough = not self._table_moving
                if not quiet_enough and old_hist is not None:
                    old_raws = [p[5] for p in old_hist.positions if p[3]]
                    quiet_enough = bool(old_raws) and raw <= (
                        max(old_raws[-4:]) * cfg.handoff_speed_gain_max
                    )
                if raw >= cfg.strike_impulse_mms and last_obs_old is not None and quiet_enough:
                    self._maybe_open_handoff(t, cue, raw, last_obs_t=last_obs_old)
                # MEASURED AND REMOVED (2026-08-06): an "eruption restore"
                # lived here — stash the killed candidate and restore it when
                # the aftermath births a fleet of fast young tracks, to fire
                # the live break this kill eats (its cue crosses the table
                # unobserved; the gain test's denominator is one stick-
                # smeared 2441 mm/s step while the ball flew 8000+). Twelve
                # candidate discriminators against pickup_snatch_0801 were
                # measured and ALL failed: young-track count (4 vs 6),
                # speed (538-2709 vs 700-2700), spread (400+ mm both),
                # persistence (arm fragments outlive 3 observed frames),
                # fleet birth distance from the rest (1067-1337 vs 988+),
                # non-hand mover stamps (0 both), roster deaths (rack reads
                # as 1-2 merged tracks), old-holder afterlife (coast->gone
                # both), departure certification (single fast step both:
                # 2327 vs 2441). A snatch's aftermath is observationally
                # identical to a break's at this layer, and a pickup must
                # never fire. The break class needs tracker/detect-level
                # evidence (see the accuracy plan) — do not rebuild this
                # from event-layer aftermath statistics.
            elif self._candidate is not None:
                events.extend(self._advance_candidate(t, cue))
            else:
                self._maybe_open_candidate(t, cue)
            self._update_cue_rest(t, cue)
        # A candidate riding a coasting track must not hold the slot shut
        # (flag_missed / flag_missed_3, checkpoint 66). The flag-borne open is
        # correct at the impulse frame and then the tracker loses the ball: the
        # struck cue outruns the 100 mm association gate and founds a *new*
        # track, while the candidate advances on extrapolated positions that
        # can never confirm. The single candidate slot is exactly what the
        # identity scans below need, and they need it *now* — the struck ball
        # is only inside `strike_handoff_radius_mm` of the rest for a moment.
        # Measured: on flag_missed the candidate opened at 11.239 and died at
        # 11.355 (the feasibility check, on a phantom re-stamped still), by
        # which time the real cue was 469 mm out — 219 mm past the radius it
        # had cleared at 224 mm just 0.07 s earlier; on flag_missed_3 the
        # candidate rode a coasting phantom for its full 0.30 s window while
        # the struck ball sat 175 mm from the rest, and the scan first ran at
        # 15.726 with everything 292 mm or further away. Shelved rather than
        # dropped: if no scan opens, the candidate is put straight back, so a
        # real strike whose cue merely blinks through an occlusion keeps its
        # window. No new constant — the scans' own gates do all the judging.
        shelved: _StrikeCandidate | None = None
        if self._candidate is not None and not self._candidate_target_observed(balls, cue):
            shelved, self._candidate = self._candidate, None
        if self._candidate is None:
            # Identity-carrying scans, for strikes the cue flag cannot vouch
            # for. Two configurations: no *observed* ball holds the flag —
            # the tracker lost the cue (address occlusion killed its track;
            # the regrab grace and the whiteness floor keep the flag empty,
            # and a fast newborn's blur-corrupted color sample cannot win it
            # back within the newborn window), or the flag-holder is a
            # coasting phantom (a dead cue coasts ~0.15 s after impact, long
            # enough to age the real newborn out — the 2026-08-01
            # scratch-shot miss) — or the flag is parked on some *other*
            # still ball entirely (the real cue lost the vote resting merged
            # against a cluster and the vote returned 0.6 s post-impact:
            # parked_vote_strike_0801). On a quiet table the only thing that
            # can materialize at impulse speed is the ball that was just
            # struck: a fresh track carries the identity by its birth
            # conjunction, an old roster track by its own rest, and a track
            # that stepped off the cue's own remembered rest inside the handoff
            # radius by that rest (the phantom-flag case, _maybe_open_handoff).
            cue_quiet = cue is None or not cue.observed
            cue_raw = (
                0.0
                if cue is None
                else (cue.speed_mms if cue.raw_speed_mms is None else cue.raw_speed_mms)
            )
            if cue_quiet or cue_raw < cfg.strike_impulse_mms:
                for ball in balls:
                    if cue is not None and ball.ball_id == cue.ball_id:
                        continue
                    raw = ball.speed_mms if ball.raw_speed_mms is None else ball.raw_speed_mms
                    if raw < cfg.strike_impulse_mms or not ball.observed:
                        continue
                    # Strongest anchor first: the cue's own remembered rest
                    # carries chain of custody, so it admits the merged
                    # emergence the birth path must reject as a carry.
                    #
                    # A shelved candidate releases the slot to *this path only*
                    # (checkpoint 66). The handoff is the one claimant that
                    # carries chain of custody from the cue's own rest, which is
                    # what makes it safe to pre-empt a candidate that is still
                    # nominally alive. Letting the birth path in as well cost
                    # break_0801 its flag-borne fire: a scatter track born at
                    # 5502 mm/s took the slot 51 ms early and the break changed
                    # hands (ball 0 at 7371 mm/s -> ball 4 at 6060, newborn).
                    # Dark-run length cannot separate those — measured 4 frames
                    # on break_0801 against 3 on flag_missed at its rescue — but
                    # the anchor can.
                    self._maybe_open_handoff(t, ball, raw)
                    if shelved is None:
                        # Neither path below is gated on the flag-holder being
                        # unobserved any more (flag_missed_2, checkpoint 66). A
                        # vote parked on an *observed, still* ball is the same
                        # blindness as a vote parked on a dark one, and it
                        # disabled the birth path outright: the struck cue was
                        # born at 11.310 already at 1752 mm/s out of an address
                        # occlusion while the flag sat on the object ball it was
                        # about to pot — observed on every frame, so `cue_quiet`
                        # was False and only `_maybe_open_rested` ran, which a
                        # newborn can never satisfy (it demands roster
                        # membership, and the roster admits at settle). What the
                        # birth path loses with the flag-holder observed is the
                        # vote's corroboration, so a candidate opened here is
                        # marked `parked` and must clear the travel confirm —
                        # see `parked_confirm_travel_mm`.
                        #
                        # An EMPTY flag parks the candidate too (2026-08-06):
                        # "parked" is the epistemic state "the vote does not
                        # corroborate this ball", and no-crowned-cue is that
                        # state exactly as much as crowned-elsewhere is. The
                        # old `not cue_quiet` condition dated from a metric
                        # whose generous floor always crowned *something*; the
                        # annulus floor honestly leaves the flag empty, and
                        # three false fires walked straight through the fast
                        # confirm the moment it did (a practice-stroke blob at
                        # 791 mm/s on parked_vote_strike_0801, ball 11 on
                        # address_chatter_miss_0801 — both under 400 mm of
                        # travel, the measured false ceiling). A candidate
                        # that WINS the debounced vote mid-confirm is
                        # un-parked in _advance_candidate: corroboration
                        # arriving is corroboration.
                        opening = self._candidate is None
                        self._maybe_open_newborn(t, ball, raw, follow_ball=True)
                        if opening and self._candidate is not None:
                            self._candidate.parked = True
                        if self._candidate is None:
                            self._maybe_open_rested(t, ball, raw)
                    if self._candidate is not None:
                        break
        if shelved is not None and self._candidate is None:
            self._candidate = shelved  # no scan took the slot — hand it back

        # ---- table moving / settled (per-ball displacement over windows) ----
        for ball in balls:
            raw_mms = ball.speed_mms if ball.raw_speed_mms is None else ball.raw_speed_mms
            if ball.observed:
                prev_raw = self._last_obs_raw.get(ball.ball_id)
                if prev_raw is not None and min(prev_raw, raw_mms) >= cfg.parked_fast_confirm_mms:
                    self._fast_certified.add(ball.ball_id)
                self._last_obs_raw[ball.ball_id] = raw_mms
            history = self._history.setdefault(ball.ball_id, _BallHistory())
            history.positions.append(
                (
                    t,
                    ball.x_mm,
                    ball.y_mm,
                    ball.observed,
                    ball.merged,
                    raw_mms,
                )
            )
            while history.positions and history.positions[0][0] < t - cfg.settle_creep_window_s:
                history.positions.popleft()
            self._last_translate.setdefault(ball.ball_id, t)
            if self._displacement_over(ball, cfg.settle_creep_window_s) > cfg.settle_creep_mm:
                if ball.ball_id not in self._translate_excursion:
                    positions = self._history[ball.ball_id].positions
                    base = positions[-2] if len(positions) >= 2 else positions[-1]
                    self._translate_excursion[ball.ball_id] = (
                        t,
                        base[1],
                        base[2],
                        self._last_translate[ball.ball_id],
                    )
                prev_stamp_run = self._last_translate[ball.ball_id]
                if t - prev_stamp_run > cfg.moving_window_s:
                    self._translate_run[ball.ball_id] = (t, prev_stamp_run)
                self._last_translate[ball.ball_id] = t
            else:
                excursion = self._translate_excursion.pop(ball.ball_id, None)
                if excursion is not None:
                    start_t, base_x, base_y, prev_stamp = excursion
                    if (
                        t - start_t < cfg.moving_window_s
                        and hypot(ball.x_mm - base_x, ball.y_mm - base_y) < cfg.moving_enter_mm
                    ):
                        # The excursion failed the table's own motion-evidence
                        # bar (moving_enter_mm over moving_window_s): chatter,
                        # not travel — its stamps never happened. Centroid
                        # mode flips close up to ~10.6 mm from base (p95,
                        # settle_stall_0801), so the creep gate is too tight
                        # a "returned" test; a tip bump travels 47 mm and can
                        # never qualify.
                        self._last_translate[ball.ball_id] = prev_stamp
        self._history = {b.ball_id: self._history[b.ball_id] for b in balls}
        self._last_translate = {b.ball_id: self._last_translate[b.ball_id] for b in balls}
        self._translate_excursion = {
            b.ball_id: exc
            for b in balls
            if (exc := self._translate_excursion.get(b.ball_id)) is not None
        }
        self._translate_run = {
            b.ball_id: run for b in balls if (run := self._translate_run.get(b.ball_id)) is not None
        }
        self._first_seen = {b.ball_id: self._first_seen[b.ball_id] for b in balls}
        self._birth_pos = {b.ball_id: self._birth_pos[b.ball_id] for b in balls}
        live_ids = {b.ball_id for b in balls}
        self._fast_certified &= live_ids
        self._last_obs_raw = {i: r for i, r in self._last_obs_raw.items() if i in live_ids}

        movers = [
            b
            for b in balls
            if b.ball_id in self._roster
            and self._judgeable(b)
            and self._displacement_over(b, cfg.moving_window_s) > cfg.moving_enter_mm
        ]
        # #40: the struck-ball latch admission. The episode reads `latchers`;
        # `movers` stays the roster-only set that `_last_mover_t` and the
        # carom guard's stamps below are calibrated on — a reborn cue must
        # stay invisible to both, or admitting it here would refuse the very
        # strikes it exists to catch.
        mover_ids = {b.ball_id for b in movers}
        latchers = movers + [
            b for b in balls if b.ball_id not in mover_ids and self._struck_mover(b)
        ]
        if movers:
            self._last_mover_t = t
        if latchers:
            for b in latchers:
                if b.ball_id not in self._episode_origin:
                    positions = self._history[b.ball_id].positions
                    oldest = positions[0]
                    self._episode_origin[b.ball_id] = (oldest[1], oldest[2])
                    # Seed the last-observed position too: a picked-up ball
                    # may never be observed again after the latch (fingers
                    # already wrap it — every later report is a coast).
                    last_obs = next((p for p in reversed(positions) if p[3]), oldest)
                    self._episode_last[b.ball_id] = (last_obs[1], last_obs[2])
        for b in balls:
            if b.ball_id in self._episode_origin and b.observed:
                self._episode_last[b.ball_id] = (b.x_mm, b.y_mm)
        if self._table_moving and not self._episode_free_mover:
            self._episode_free_mover = any(
                b.ball_id not in self._episode_origin
                and self._last_translate.get(b.ball_id) == t
                and self._judgeable(b)
                for b in balls
            )
        # Carom-guard evidence: non-cue balls in genuine observed motion. The
        # stamps are read by the *next* cycles' impulses (strike logic runs
        # first), which is exactly right — a bringer's approach precedes the
        # kiss. The reborn-cue track of a strike handoff cannot stamp: it is
        # too young to be judgeable before the candidate opens.
        cue_id = None if cue is None else cue.ball_id
        self._recent_movers.extend(
            (t, b.x_mm, b.y_mm, b.ball_id, False)
            for b in movers
            if b.observed and b.ball_id != cue_id
        )
        # Hand/arm evidence (cue_grab_0801): a merged, impulse-fast,
        # non-roster track is a hand-scale blob sweeping in — stamp it so
        # the cue path's carom guard can refuse the grab it is about to
        # commit. Roster movers above already cover every ball-shaped
        # bringer.
        self._recent_movers.extend(
            (t, b.x_mm, b.y_mm, b.ball_id, True)
            for b in balls
            if b.observed
            and b.merged
            and b.ball_id not in self._roster
            and b.ball_id != cue_id
            and (b.speed_mms if b.raw_speed_mms is None else b.raw_speed_mms)
            >= cfg.strike_impulse_mms
        )
        stale = t - cfg.strike_other_motion_guard_s
        while self._recent_movers and self._recent_movers[0][0] < stale:
            self._recent_movers.popleft()
        if latchers:
            self._table_moving = True
            self._all_still_since = None
        elif (
            balls
            and self._table_moving
            and not self._fired_this_episode
            and self._episode_is_pickup(t, balls)
        ):
            # `balls and`: mid-scatter detection gaps report zero tracks for a
            # frame or two (every ball streaking at once), which makes every
            # pickup guard vacuously true — the discard then unlatched a live
            # break and a scatter rebirth fired as a newborn strike into the
            # "quiet" table (break_0801b). A real pickup always leaves the
            # other balls standing and detected.
            # Every ball that latched this episode has left the table far
            # from any pocket after a hand-scale drag: someone picked it up
            # (ball in hand). The quiet that follows is not a shot settling —
            # unlatch without an event so the clock keeps running.
            self._table_moving = False
            self._all_still_since = None
            self._episode_origin.clear()
            self._episode_last.clear()
            self._episode_free_mover = False
        elif balls and self._is_still(t, balls):
            self._unstill_since = None
            if self._all_still_since is None:
                self._all_still_since = t
            if t - self._all_still_since >= cfg.settle_dwell_s:
                # Roster admission on any sustained-quiet table: tracks
                # standing judgeable through the stillness are balls at rest
                # (racked balls, a placed cue). Union, not replacement — a
                # roster ball occluded by the addressing player must not fall
                # off mid-aim. Dead ids are inert (ids are never reused), and
                # the settle below prunes to the standing set while the
                # player is away from the table.
                self._roster.update(
                    b.ball_id for b in balls if self._judgeable(b) or self._cluster_resident(b)
                )
            if self._table_moving and t - self._all_still_since >= cfg.settle_dwell_s:
                # #33 settle evidence, exported before the episode state is
                # cleared: per-mover episode travel (origin -> last observed)
                # and whether the track survived to the settle. A retrieval's
                # lifted ball shows travel but alive=False; a real shot's
                # surviving mover shows both — the hub's stun-pot guard.
                present_ids = {b.ball_id for b in balls if b.present}
                self.last_settle_movers = []
                for mover_id, (ox, oy) in self._episode_origin.items():
                    lx, ly = self._episode_last.get(mover_id, (ox, oy))
                    self.last_settle_movers.append(
                        SettleMover(
                            travel_mm=hypot(lx - ox, ly - oy), alive=mover_id in present_ids
                        )
                    )
                self._table_moving = False
                self._roster = {
                    b.ball_id for b in balls if self._judgeable(b) or self._cluster_resident(b)
                }
                self._episode_origin.clear()
                self._episode_last.clear()
                self._episode_free_mover = False
                self._fired_this_episode = False
                events.append(BallsSettled(t))
        elif balls:
            # Un-still verdict on a populated table: dwell credit survives
            # transients shorter than the motion-evidence window (see
            # _unstill_since — corrupt-observation chatter measures <= 64 ms
            # per excursion, genuine motion >= 371 ms). No settle can fire
            # during the grace: the fire above sits in the still branch.
            if self._unstill_since is None:
                self._unstill_since = t
            if t - self._unstill_since >= cfg.moving_window_s:
                self._all_still_since = None
        else:
            # Zero-ball frame: not stillness evidence (mid-scatter detection
            # desert) — reset outright, exactly the pre-guard behavior.
            self._all_still_since = None
            self._unstill_since = None

        # ---- scratch: cue track terminated near a pocket while balls were moving ----
        # Only *observed* positions count as "last seen". A sinking cue's final
        # observations come from the pocket jaw itself, so they land inside the
        # pocket zone; an occluded passing cue keeps coasting on its last
        # velocity, and that phantom can slide into the zone and die there
        # (measured on the spin_in_place corpus clip: last real observation
        # 359 mm from the pocket, coasted death 89 mm from it).
        if cue is not None and cue.observed:
            self._cue_last_seen = (t, cue.x_mm, cue.y_mm, cue.speed_mms)
            self._cue_last_color = (cue.whiteness, cue.color_age_s)
            self._cue_lost_while_moving = False
            if self._pending_scratch is None:
                self._scratch_fired = False  # cue back on the table re-arms the latch
            # A pending verdict is NOT cancelled by the cue flag coming back.
            # After the cue drops, the whiteness vote parks on some other
            # white-ish ball within a few frames — that is the vote finding a
            # replacement, not the cue reappearing, and letting it cancel here
            # suppressed all three genuine scratches in the corpus. Only
            # position decides: see _resolve_pending_scratch.
        elif cue is None and self._cue_last_seen is not None:
            last_t, last_x, last_y, last_speed = self._cue_last_seen
            if not self._cue_lost_while_moving:
                self._cue_lost_while_moving = was_moving and last_speed > cfg.settle_floor_mms
            if (
                self._cue_lost_while_moving
                and not self._scratch_fired
                and self._scratch_open is None  # one candidate at a time (#26):
                # overlap would need a candidate still unresolved when the
                # *next* shot's cue dies — live, 68/68 resolved a settle
                # within 19.4 s, well inside any shot cycle
                and t - last_t >= cfg.cue_lost_latch_s
                and self._near_pocket(last_x, last_y, last_speed)
            ):
                # Latch the verdict, do not publish it: a ball may yet turn up
                # at the death spot, which means the cue stopped in the jaw
                # rather than dropping. See scratch_confirm_s.
                self._scratch_fired = True
                self._pending_scratch = (t, last_x, last_y, last_t)

        events.extend(self._resolve_pending_scratch(t, balls))
        events.extend(self._adjudicate_scratch(t, balls))
        return events

    # ---------- strike helpers ----------

    def _update_cue_rest(self, t: float, cue: BallState) -> None:
        cfg = self._cfg
        if cue.speed_mms < cfg.settle_floor_mms:
            if self._cue_rest is None or (
                hypot(cue.x_mm - self._cue_rest[1], cue.y_mm - self._cue_rest[2])
                > cfg.strike_handoff_radius_mm
            ):
                # Founding a rest — from nothing, or because the flag jumped
                # to a different still ball (a jump is a new rest, not the old
                # one relocated: dwell credit restarts; a genuinely creeping
                # cue moves ~0.5 mm between stamps and never trips this).
                # Either way it is a claim about where a ball IS, so it needs
                # presence evidence. Without the guard, a flag-holder coasting
                # on nothing after a fire cleared the rest would found a fresh
                # one at its phantom position and accrue dwell on zero
                # evidence — the re-arm that mints a second ShotTaken when the
                # flag finally transfers to the real ball (§4.14).
                if cue.present:
                    self._cue_rest = (t, cue.x_mm, cue.y_mm)
            else:
                self._cue_rest = (self._cue_rest[0], cue.x_mm, cue.y_mm)
            # The still-stamp refreshes even without presence — load-bearing:
            # the real strike on phantom_flag_strike_0805 arrives 0.128 s
            # after the phantom went dark, against strike_step_max_s = 0.12,
            # and only survives because _maybe_open_handoff reads a fresh
            # still-stamp off the coasting holder (§4.14, trap 2).
            self._cue_last_still_t = t
        elif self._cue_rest is not None and t - self._cue_last_still_t > cfg.strike_handoff_gap_s:
            self._cue_rest = None
            self._axis_killed_t = None  # no rest to re-open from

    def _maybe_open_candidate(self, t: float, cue: BallState) -> None:
        cfg = self._cfg
        raw = cue.speed_mms if cue.raw_speed_mms is None else cue.raw_speed_mms
        if raw < cfg.strike_impulse_mms:
            return
        born = self._first_seen.get(cue.ball_id)
        # The dead-rest age budget applies only when the rest story was
        # already broken when this track was born: a crown with nowhere
        # credible to be for the ball's whole life is the occluded-address
        # signature (flag_missed_2: still-stamp dead 7.6 s before the
        # emergence). A story that broke AFTER the birth broke because of
        # the birth — the fragment's own crowning stopped the refresh
        # (stroke_tug_strike_0801: still fresh until 0.13 s before the
        # fallback, fragment already 0.45 s old) — and gets no extra time.
        wide = (
            cfg.newborn_dead_rest_max_age_s
            if born is not None and self._cue_last_still_t <= born
            else None
        )
        if self._cue_rest is None:
            self._maybe_open_newborn(t, cue, raw, max_age_s=wide)
            return
        if t - self._cue_last_still_t > cfg.strike_step_max_s and not (
            self._axis_killed_t is not None and t - self._axis_killed_t <= cfg.strike_step_max_s
        ):
            # Ramp, not a step: shaft/hand push accelerating the ball. A
            # just-axis-killed candidate is the exception — the tug that
            # opened it already burned the still-stamp window (see
            # _axis_killed_t) and the honest impulse arrives right behind.
            #
            # MEASURED AND REVERTED (2026-08-06): parking these fallback
            # opens when the still-story is stale — to send the crowned
            # ball-in-hand roll of parked_vote_strike_0801 to the travel
            # confirm — costs two real strikes outright (lone_roller_0801,
            # occluded_address_0731: rolls under 600 mm) and delays two more
            # by 0.33-0.45 s. A stale still-story is the occluded-address
            # class's defining feature, not a placement signature; do not
            # re-propose staleness as the separator.
            self._maybe_open_newborn(t, cue, raw, max_age_s=wide)
            return
        verdict = self._open_from_rest(t, cue, raw, self._cue_rest, follow_ball=False)
        if verdict == "too_far":
            # Impulse far from the remembered rest: the rest may be anchored to
            # the wrong ball (flag parked elsewhere while the real cue was
            # occluded) — the birth evidence gets its say.
            self._maybe_open_newborn(t, cue, raw)

    def _open_from_rest(
        self,
        t: float,
        ball: BallState,
        raw: float,
        rest: tuple[float, float, float],
        follow_ball: bool,
        reach_mm: float | None = None,
    ) -> Literal["opened", "rejected", "too_far"]:
        """Open a rest-anchored candidate: dwell + carom guard + axis seed.

        Shared by the flag-borne path (`_maybe_open_candidate`, where `ball` is
        the flag holder itself) and the handoff path (`_maybe_open_handoff`,
        where the flag is stuck on a coasting phantom and `ball` is the reborn
        track carrying the cue's identity). The evidence is the same either
        way: the impulse steps off a spot the cue held still for a full dwell,
        no ball-mover reached that spot, and the departure is long enough to
        seed a trajectory axis.

        `too_far` is reported instead of a plain rejection so the caller can
        fall through to the birth evidence — an impulse beyond the handoff
        radius means the rest may be anchored to the wrong ball entirely.

        `reach_mm` overrides that radius for a caller that can account for the
        distance some other way (`_maybe_open_handoff`, which knows how long
        the ball was invisible). The direction seed is always taken from the
        rest anchor regardless — widening the reach changes what is admitted,
        never where the trajectory is measured from.
        """
        cfg = self._cfg
        rest_t0, rest_x, rest_y = rest
        if self._cue_last_still_t - rest_t0 < cfg.pre_strike_dwell_s:
            return "rejected"
        dx, dy = ball.x_mm - rest_x, ball.y_mm - rest_y
        # Gear-behind exemption (stroke_gear_veto_0802, checkpoint 50): a hand
        # stamp inside carom_reach_mm is not, by itself, evidence of a grab.
        # The player's own stick/bridge blob stamps from *behind* the cue on a
        # normal stroke — measured 138 mm (stroke_gear_veto_0802) and 152 mm
        # (stroke_gear_margin_0802) from the rest spot, well inside the 200 mm
        # reach and nothing like the 271 mm (medium_shot) / 340 mm
        # (power_shot) this guard was anchored on. It killed both live shots:
        # no ShotTaken, the clock ran down *through* the stroke.
        # Distance cannot fix it — cue_grab_0801, the phantom the hand stamps
        # exist to reject, grabs from 116 mm, so accept-at-138 and
        # reject-at-116 leaves 22 mm and one measurement each side.
        # Direction can, and by physics: a stroke drives the cue *away* from
        # the gear behind it (measured 148-162 deg between the cue's departure
        # and the direction to the stamp), while a grab drags it across toward
        # the hand (61 deg). The test is the sign of the dot product — a 90 deg
        # boundary sitting 29 deg off the grab and 58 deg off the nearest
        # stroke, with no constant to tune. It needs a departure long enough to
        # have a trustworthy direction, so it reuses the seed floor the
        # candidate must clear anyway.
        # Ball movers keep the pure-distance rule: a genuine carom also sends
        # the cue away from its bringer, so direction cannot judge them.
        departing = hypot(dx, dy) >= cfg.strike_dir_seed_min_mm
        for _mt, mx, my, mid, hand in self._recent_movers:
            if mid == ball.ball_id:
                continue
            if hypot(mx - rest_x, my - rest_y) > cfg.carom_reach_mm:
                continue
            if hand and departing and (mx - rest_x) * dx + (my - rest_y) * dy < 0.0:
                continue  # the stroke's own gear, behind the departing cue
            return "rejected"  # carom guard: a mover reached the rest spot
        norm = hypot(dx, dy)
        if norm > (cfg.strike_handoff_radius_mm if reach_mm is None else reach_mm):
            return "too_far"
        if norm < cfg.strike_dir_seed_min_mm:
            return "rejected"  # too little displacement to trust as a direction
        # Rest memory is kept until a candidate *fires*: a bogus candidate
        # (e.g. the impact yanking the old track's centroid) then fails the
        # axis check and the real impulse can still re-open from the same rest.
        self._candidate = _StrikeCandidate(
            t0=t,
            x0=rest_x,
            y0=rest_y,
            dir_x=dx / norm,
            dir_y=dy / norm,
            impulse_raw=raw,
            x_open=ball.x_mm,
            y_open=ball.y_mm,
            ball_id=ball.ball_id if follow_ball else None,
            opened_on=ball.ball_id,
        )
        return "opened"

    def _maybe_open_handoff(
        self, t: float, ball: BallState, raw: float, last_obs_t: float | None = None
    ) -> None:
        """Phantom-flag handoff strike (flag_missed_2, checkpoint 64).

        `last_obs_t` overrides `_cue_last_obs_t` for the flag-switch
        re-qualification (see the dispatch in `update`): there the flag has
        *already* moved to the reborn track, so `_cue_last_obs_t` tracks the
        new holder and the ordering/dark tests below would measure the wrong
        timeline. The caller passes the OLD holder's last observed time —
        the moment the departing cue actually went dark.

        The addressed cue's own track can be frozen at its rest spot and keep
        the whiteness vote while the ball is struck and gone. Three mechanisms
        compose: the tracker holds a still track in place through merged blobs
        and coasts it for `still_coast_s` (3 s) on the premise that an
        addressed ball "cannot move unseen"; the struck ball re-emerges beyond
        the 100 mm association gate and so founds a *new* track; and the frozen
        phantom's whiteness is never resampled, so its stale score outranks the
        live one and the flag never transfers.

        Measured on flag_missed_2: the cue was last detected at its rest spot
        (1529, 872) at 10.030 s, went undetected entirely for 0.173 s while it
        travelled 171 mm, and re-emerged at 10.228 s as a merged blob 193 mm
        away at 2281 mm/s — with the flag still on the phantom for 2.8 s more.
        At that impulse frame every gate of the flag-borne handoff already
        passed (dwell 9.72 s, still-stamp gap 0.013 s, quiet table, no mover
        within `carom_reach_mm` of the rest, 193 mm inside the 250 mm radius).
        The strike was missed for one reason only: the flag was on the wrong
        track, so `_maybe_open_candidate` never evaluated this ball.

        So the cue's remembered rest becomes a third identity anchor for the
        scan, beside birth conjunction (`_maybe_open_newborn`) and a roster
        ball's own rest (`_maybe_open_rested`). It introduces no constant: the
        dwell, step, radius and carom guard are the flag-borne path's, reused
        verbatim through `_open_from_rest`.

        The rest-anchored confirm is the point of routing it here rather than
        through the birth path. A ball struck out of an address emerges merged
        with the stick that struck it — this one reads merged on nearly every
        frame — and `newborn_merged_frac_max` correctly reads that as a carried
        ball. Chain of custody from the cue's own rest is what makes the merge
        admissible, exactly as it is for an ordinary streaking shot.
        """
        cfg = self._cfg
        obs_t = self._cue_last_obs_t if last_obs_t is None else last_obs_t
        if self._cue_rest is None:
            return
        if raw < cfg.newborn_impulse_mms:
            # The birth path's floor, not the flag-borne one. The rest spot
            # vouches for the *place* the impulse came from, but nothing
            # vouches for this track — so a slow thing appearing near the
            # cue's rest is still far more likely carried than struck, exactly
            # as it is for a birth. At 110 the scan handed the slot to
            # hand/stick fragments near the rest and cost soft_shot its real
            # shot outright.
            return
        if self._table_moving and last_obs_t is None:
            # Same latch guard as the other two scan paths: mid-scatter, a
            # reborn ball's impulse near the cue's stale rest is a carom or
            # streak churn, not a strike. In re-qualify mode the caller has
            # already applied the departure-speed physics test that stands in
            # for this latch (handoff_speed_gain_max) — the latch there is
            # usually the old holder's own departure, and the carom guard
            # below still refuses any OTHER mover that reached the rest.
            return
        # Ramp, not a step — but measured from the last moment the cue was
        # *visible*, not from a still-stamp that stopped advancing precisely
        # because the ball disappeared (flag_missed_3, checkpoint 66).
        # `_cue_last_still_t` only advances while the flag-holder is reported
        # still (`_update_cue_rest`), so once the abandoned track starts
        # drifting the stamp freezes and this gate silently bills the occlusion
        # to the ramp budget. Measured on flag_missed_3: cue last still 15.410,
        # last observed 15.425, struck ball surfaced at 15.534 — 0.128 s against
        # the frozen stamp (refused by 8 ms) but 0.109 s since the cue was last
        # seen. Checkpoint 64's own phantom was *held* still through merged
        # blobs, so its stamp stayed fresh and `max` leaves it untouched.
        #
        # This cannot re-admit a ramp: the ordering test below requires the
        # track to be born after `_cue_last_obs_t`, so what the window now
        # bounds is how long after the cue vanished its continuation may
        # surface — the occlusion coast. A shaft/hand push keeps the cue in
        # view and founds no new track, so it never reaches this path at all.
        born = self._first_seen.get(ball.ball_id)
        # In re-qualify mode the impulse "arrived" when the continuation
        # track SURFACED, not when the vote's debounce let the kill run —
        # the debounce adds 1-2 frames of bookkeeping latency, and billing
        # it to the occlusion budget refused late_emergence_strike_0805 by
        # 2 ms (dark 0.109 s at birth, 0.122 s at the kill frame).
        arrival = t if last_obs_t is None else (t if born is None else born)
        if arrival - max(self._cue_last_still_t, obs_t) > cfg.strike_step_max_s:
            return
        if born is None or born <= obs_t:
            # The identity claim, and the whole reason this path may trust a
            # merged emergence: the cue went dark and *then* this track
            # appeared, so it is the cue's continuation rather than a
            # bystander. A stick fragment, a reaching hand or an established
            # ball all pre-date the cue going dark and are refused here —
            # which is what keeps the pickup/grab class (cue_grab_0801,
            # pickup_newborn_0801, pickup_snatch_0801,
            # ball_in_hand_pickup_0801) out of a path that skips the
            # carried-ball gates. No constant: it is an ordering test.
            return
        # How far the ball is allowed to be from the rest. The plain radius is
        # a distance budget and a break outruns it during its own occlusion:
        # the cue leaves at 7000+ mm/s, is dark for three frames, and founds
        # its new track 320 mm out — the strike was never near the rest by the
        # time anything could see it (checkpoint 67, the 2026-08-05 live break;
        # the same refusal is on record for break_underread_0801 at 305 mm and
        # break_0801 at 330 mm, both of which survived only on the flag-borne
        # fire). Inside the occlusion the ball's own speed accounts for the
        # distance, so that becomes the budget.
        #
        # The window is `strike_step_max_s` — no new constant, and the corpus
        # says it is the right one. Across every handoff impulse the radius
        # currently refuses, cue-dark time splits the population in two with
        # nothing in between: the three real breaks sit at 0.038-0.102 s, and
        # the nearest thing that must stay refused is 0.742 s
        # (parked_vote_strike_0801), then 2.9 s (ball_in_hand_pickup_0801) and
        # 6.5 s (parked_vote_newborn_0805) — a 7x gap with the step window in
        # the middle of it. Measured from `_cue_last_obs_t` alone, deliberately
        # *not* the `max(...)` the step gate above uses: a flag parked on some
        # other still ball keeps that stamp fresh indefinitely, which is
        # exactly how those three long-dark rows get this far.
        dark = arrival - obs_t
        reach = cfg.strike_handoff_radius_mm
        if dark <= cfg.strike_step_max_s:
            # Eligibility is the TRUE occlusion length (birth-anchored in
            # re-qualify mode), but the travel budget must cover the ball's
            # motion through the frame actually being evaluated — in
            # re-qualify mode that is 1-2 debounce frames after the birth,
            # and a 5854 mm/s power shot covers another 74 mm in them
            # (power_shot: 411 mm from the rest at the kill frame against a
            # birth-anchored budget of 334).
            reach = max(reach, raw * (t - obs_t) * cfg.handoff_reach_margin)
        opened = self._open_from_rest(
            t, ball, raw, self._cue_rest, follow_ball=True, reach_mm=reach
        )
        if opened == "opened" and self._candidate is not None:
            self._candidate.handoff = True

    def _maybe_open_newborn(
        self,
        t: float,
        cue: BallState,
        raw: float,
        follow_ball: bool = False,
        max_age_s: float | None = None,
    ) -> None:
        """Occluded-address strike: the cue-vote winner was just born at speed.

        A ball cannot teleport into existence at impulse speed. When the
        table has had no genuine mover for a pre-strike dwell and none within
        kiss range of the birth spot, a fresh track born already fast that
        wins the whiteness vote is the struck cue emerging from the player's
        address occlusion (its resting track died under the stick/bridge, so
        the rest-memory path had nothing — or the wrong ball — to anchor on).
        The candidate anchors on the birth spot; the confirm adds the
        carried-ball gates (full observation, smooth raw speed).
        """
        cfg = self._cfg
        if raw < cfg.newborn_impulse_mms:
            # The birth path keeps the original impulse floor — see
            # newborn_impulse_mms. No chain of custody, so no relaxation.
            return
        if self._table_moving:
            # REMOVED 2026-08-11 (#37 Part B): the one-shot-per-rest-cycle
            # newborn disarm (`_newborn_disarmed_at`) stood on the line above
            # this one. Recorded here because deleting a guard silently is how
            # it comes back.
            #
            # It existed to stop machine-gun re-opens off a single strike: a
            # newborn fire disarmed all three scan paths until a cue-flagged
            # ball at rest had translated past the disarm stamp.
            #
            # The invariant that retires it: the ONLY setter was the newborn
            # fire, and that same statement block sets `_table_moving = True`
            # and `_fired_this_episode = True`. The latch can then only fall
            # at the settle — the pickup discard refuses a fired episode — and
            # the settle cleared the disarm in the same block. So the stamp
            # was never non-None while the table was unlatched. On this path
            # and on the own-rest path the refusal directly below it is
            # therefore an exact superset: every input the disarm could have
            # refused, the latch check already returns on.
            #
            # The re-qualify path (`_maybe_open_rested`'s scan, whose latch
            # check is conditional on `last_obs_t is None`) is the one case
            # where the disarm could still bite, and it is retired on a
            # different argument: since #33 a second fire inside one latched
            # episode has NO clock consequence at all. `ShotClock.shot_taken`
            # returns early in MOTION and the settle counts exactly one shot
            # per episode, so the machine-gun this guarded can no longer
            # produce a machine-gun of counted shots — only of journal lines.
            #
            # Corpus: flat. Seven real strikes fire newborn (checkpoint 57's
            # list) and none re-fires without it — no clip changes outcome, no
            # golden moves. Its *own* history is the other half of the case:
            # scoping it to one track's translate stamp shut all three paths
            # for a whole session (checkpoint 51, stroke_gear_margin_0802),
            # and it needed the checkpoint-31 scratch fix before that. Two
            # live failures caused, none prevented.
            #
            # A latched, unsettled episode is not a quiet table, even when
            # the mover clock has gone stale: a break/fast shot's roster
            # movers die in streak churn and their reborn continuations are
            # too young to stamp, so mid-scatter the table *looks* quiet to
            # the dwell gate — and a scatter ball reborn at 906 mm/s fired
            # as a newborn strike 2.2 s into a live break (break_0801b).
            # Every real birth-path strike in the corpus fires from a
            # settled (unlatched) table.
            return
        born = self._first_seen.get(cue.ball_id)
        age_budget = cfg.newborn_strike_max_age_s if max_age_s is None else max_age_s
        if born is None or t - born > age_budget:
            return
        if self._engine_t0 is None or born - self._engine_t0 <= cfg.settle_creep_window_s:
            return  # founding track: first sight at engine start is not a birth
        if born - self._last_mover_t < cfg.pre_strike_dwell_s:
            return  # something was genuinely moving right before this birth
        birth_x, birth_y = self._birth_pos[cue.ball_id]
        if any(
            hypot(mx - birth_x, my - birth_y) <= cfg.carom_reach_mm
            for _mt, mx, my, mid, hand in self._recent_movers
            if mid != cue.ball_id and not hand
        ):
            return  # a ball-mover reached the birth spot: kiss, not a strike
        dx, dy = cue.x_mm - birth_x, cue.y_mm - birth_y
        norm = hypot(dx, dy)
        if norm < cfg.strike_dir_seed_min_mm or norm > cfg.strike_handoff_radius_mm:
            return
        self._candidate = _StrikeCandidate(
            t0=t,
            x0=birth_x,
            y0=birth_y,
            dir_x=dx / norm,
            dir_y=dy / norm,
            newborn=True,
            last_raw=raw,
            ball_id=cue.ball_id if follow_ball else None,
            opened_on=cue.ball_id,
            impulse_raw=raw,
            x_open=cue.x_mm,
            y_open=cue.y_mm,
        )

    def _maybe_open_rested(self, t: float, ball: BallState, raw: float) -> None:
        """Wrong-flag strike (parked_vote_strike_0801): a roster ball with a
        long rest of its own steps to impulse speed while the whiteness vote
        is parked elsewhere.

        The real cue can lose the vote resting merged against a cluster (its
        color sample corrupts) and win it back only ~0.6 s after being
        struck — far outside every rest/newborn window. Both existing paths
        are then structurally blind: the rest memory tracks the parked
        flag-holder (the impulse lands hundreds of mm from it), and the
        struck ball's track is seconds old (not a birth). The ball's own
        history carries the anchor instead: it was admitted to the roster at
        a settle (a real ball at rest, not junk), its last translation is a
        full pre-strike dwell ago (a hand push ramps and trips the creep
        window early — it can never show a long dwell at impulse speed), and
        the impulse steps away from its own rest spot. The scan sees
        `_last_translate` before this frame's update, so the impulse frame
        itself cannot break its own dwell. Confirm runs the carried-ball
        gates (observation fraction, raw-speed smoothness, merged fraction) —
        the same measured hand discriminators the newborn path uses. No new
        thresholds.
        """
        cfg = self._cfg
        if ball.ball_id not in self._roster:
            return
        if self._table_moving:
            # Same latch guard as the newborn path: a mid-episode kiss from
            # an untracked (reborn) mover must not fire off a bystander's
            # rest. Every real own-rest strike fires from a settled table
            # (parked_vote_strike_0801 both live cases).
            return
        history = self._history.get(ball.ball_id)
        if history is None:
            return
        rest = next(((p[1], p[2]) for p in reversed(history.positions) if p[3]), None)
        if rest is None:
            return
        rest_x, rest_y = rest
        last_translate = self._last_translate.get(ball.ball_id)
        run = self._translate_run.get(ball.ball_id)
        if (
            last_translate is not None
            and run is not None
            and t - run[0] <= cfg.moving_window_s
            and self._run_witnessed(history, run[0], t)
        ):
            # The fresh stamps are the impulse's own onset run, just started
            # and pixel-witnessed end to end. The dwell question is about
            # what came BEFORE it, so test the stamp that stood before the
            # run. A cluster ball's dwell can never be observation-witnessed
            # (the merged blob is handed around its member tracks, so every
            # member reads present-not-observed — flag_missed_3 measured
            # ~0% observed over 176 s of genuine rest), and the checks below
            # would refuse it on exactly that blindness. The departure is
            # where pixel evidence is non-negotiable: a tip-bump push rides
            # the ball out of sight for 666 ms, while a struck ball's onset
            # gap measures <= 77 ms — `_run_witnessed` holds that line with
            # the strike occlusion budget (strike_step_max_s).
            last_translate = run[1]
        if last_translate is None or t - last_translate < cfg.pre_strike_dwell_s:
            # The stamp says "recently moved" — but sustained address churn
            # fakes that: the stick and bridge hand over the addressed ball
            # drive raw chatter of 300-3000 mm/s for a second-plus while the
            # ball never leaves a ~10 mm wobble (address_chatter_miss_0801,
            # the struck cue pre-impulse; settle_stall_0801 measured the
            # centroid modes at 7-10 mm). The ball's own observed positions
            # are the truth: if everything through the pre-strike window
            # stayed inside the table's motion-evidence bar around the rest
            # spot, it never actually left its rest. A hand push cannot
            # pass — reaching impulse speed through the ramp puts it
            # >= moving_enter_mm out (120 mm/s for the 0.3 s ramp control is
            # 36 mm of real travel); chatter's net stays <= 10.6 mm (p95).
            recent = [p for p in history.positions if p[0] >= t - cfg.pre_strike_dwell_s and p[3]]
            if not recent or any(
                hypot(px - rest_x, py - rest_y) >= cfg.moving_enter_mm
                for _pt, px, py, *_rest_fields in recent
            ):
                return
            # And the rest must be continuously WITNESSED, not inferred
            # through an occlusion: a push rides the ball out of sight for
            # its whole travel (666 ms unobserved, tip_bump) while a
            # strike's stick passes through contact (worst observed gap
            # 77 ms, chatter flicker included, address_chatter_miss_0801).
            # Reported positions after a long coast carry smoothing and
            # association artifacts (tip_bump's emergence frame is stored AT
            # the rest spot), so this gate reads observation timing only:
            # the dwell window must contain no unobserved run longer than
            # the motion-evidence window.
            window_start = t - cfg.pre_strike_dwell_s
            prev_seen = window_start
            max_gap = 0.0
            for p in history.positions:
                if p[0] < window_start:
                    continue
                if p[3]:
                    max_gap = max(max_gap, p[0] - prev_seen)
                    prev_seen = p[0]
            max_gap = max(max_gap, t - prev_seen)
            if max_gap > cfg.moving_window_s:
                return
        if any(
            hypot(mx - rest_x, my - rest_y) <= cfg.carom_reach_mm
            for _mt, mx, my, mid, hand in self._recent_movers
            if mid != ball.ball_id and not hand
        ):
            return  # a ball-mover reached the rest spot: kiss, not a strike
        dx, dy = ball.x_mm - rest_x, ball.y_mm - rest_y
        norm = hypot(dx, dy)
        if norm < cfg.strike_dir_seed_min_mm or norm > cfg.strike_handoff_radius_mm:
            return
        self._candidate = _StrikeCandidate(
            t0=t,
            x0=rest_x,
            y0=rest_y,
            dir_x=dx / norm,
            dir_y=dy / norm,
            newborn=True,
            last_raw=raw,
            ball_id=ball.ball_id,
            opened_on=ball.ball_id,
            impulse_raw=raw,
            x_open=ball.x_mm,
            y_open=ball.y_mm,
        )

    def _confirm_window(self, candidate: _StrikeCandidate) -> float:
        """How long this candidate has to confirm.

        Parked-vote birth candidates get the longer window their travel
        confirm needs; everything else keeps `strike_confirm_window_s`.
        """
        cfg = self._cfg
        return cfg.parked_confirm_window_s if candidate.parked else cfg.strike_confirm_window_s

    def _candidate_target_observed(self, balls: list[BallState], cue: BallState | None) -> bool:
        """Is the open candidate's target a real detection this frame?

        A candidate advances either on its own track (`ball_id`) or on whatever
        holds the cue flag. Either way, an unobserved target is an extrapolation
        the tracker is coasting — it cannot confirm a strike, and it must not
        keep the identity scans from claiming the slot. See the shelve in
        `update`.
        """
        candidate = self._candidate
        if candidate is None:
            return False
        target = (
            cue
            if candidate.ball_id is None
            else next((b for b in balls if b.ball_id == candidate.ball_id), None)
        )
        return target is not None and target.observed

    def _advance_candidate(self, t: float, cue: BallState) -> list[Event]:
        cfg = self._cfg
        candidate = self._candidate
        if candidate is None:
            return []
        if t - candidate.t0 > self._confirm_window(candidate):
            self._candidate = None  # impulse without follow-through: not a shot
            return []
        raw = cue.speed_mms if cue.raw_speed_mms is None else cue.raw_speed_mms
        # NOTE (2026-08-06): an un-park-on-crowning rule was built here —
        # "the candidate's ball won the debounced vote mid-confirm, so the
        # parked state's premise is gone" — and measured to have ZERO
        # dependents: the battery is byte-identical with and without it,
        # because every crowned emergence in the corpus is crowned BEFORE its
        # candidate opens (the annulus votes in 1-2 frames) and so never
        # opens parked. Worse, identity is not strike evidence: the one thing
        # the rule would have un-parked live is a crowned ball-in-hand
        # placement roll (parked_vote_strike_0801, 256 mm of travel), whose
        # refusal is the travel confirm the un-park would bypass. Removed.
        # Parked fast tier: two consecutive raws at the floor, only from
        # frames the target was actually measured — a coasted frame repeats
        # the EMA and would fake the corroboration (see
        # `parked_fast_confirm_mms`).
        if cue.observed:
            prev_obs = candidate.last_obs_raw
            candidate.last_obs_raw = raw
            if prev_obs is not None and min(prev_obs, raw) >= cfg.parked_fast_confirm_mms:
                candidate.fast_certified = True
        if candidate.newborn:
            # Carried-ball gates (measured on scratch_replace): a hand-carried
            # cue's raw speed is erratic between adjacent frames while a
            # struck cue's is smooth; kill on the first wild step.
            prev = candidate.last_raw
            candidate.last_raw = raw
            if (
                prev is not None
                and min(prev, raw) > 0
                and max(prev, raw) / min(prev, raw) > cfg.newborn_raw_step_ratio_max
            ):
                self._candidate = None
                return []
        dx, dy = cue.x_mm - candidate.x0, cue.y_mm - candidate.y0
        displacement = hypot(dx, dy)
        # Two-stage confirm (foul reversal, 2026-08-02). Fast: 60 mm, any time
        # in the window — unchanged, and this is the path every ordinary shot
        # takes. Slow: 22 mm, but only once the window has nearly expired, so
        # a sub-impulse touch has to have spent the whole window travelling
        # rather than jumped 22 mm on one merged-centroid frame.
        late = t - candidate.t0 >= cfg.strike_slow_confirm_after_s
        needed = cfg.strike_slow_confirm_mm if late else cfg.strike_confirm_displacement_mm
        if candidate.parked and not candidate.fast_certified:
            # The vote corroborates nothing here, so the ball's own travel is
            # the identity evidence — see `parked_confirm_travel_mm`. A
            # fast-certified target has shown corroborated struck-ball speed
            # instead and keeps the normal thresholds
            # (`parked_fast_confirm_mms`).
            needed = cfg.parked_confirm_travel_mm
        if displacement < needed:
            # Feasibility: a candidate that can no longer reach the confirm
            # displacement in the window it has left is dead now, not at
            # expiry. Physics, no new constants — before this check a tip
            # bump's candidate lingered at rest until a stick-merge centroid
            # jump could fake the remaining distance (tip_bump's phantom
            # fire). A real strike's ball is still at speed here and passes
            # trivially. The reachable target is the *slow* threshold: that is
            # the least a candidate could still fire on.
            time_left = candidate.t0 + self._confirm_window(candidate) - t
            if displacement + raw * time_left < cfg.strike_slow_confirm_mm:
                self._candidate = None
            return []
        if hypot(cue.x_mm - candidate.x_open, cue.y_mm - candidate.y_open) < (
            cfg.strike_min_travel_mm
        ):
            # Confirmed on inherited displacement: the ball had already left
            # the anchor before this candidate opened and has barely moved
            # since. Not a strike — see strike_min_travel_mm. Kept as a
            # non-fatal wait rather than a kill: the honest impulse may still
            # arrive inside the window.
            return []
        axis_dot = (dx * candidate.dir_x + dy * candidate.dir_y) / displacement
        if axis_dot < cfg.strike_axis_dot_min:
            # Incoherent trajectory (detection jitter, or a pre-impact stroke
            # tug that seeded a garbage axis). For rest-anchored candidates,
            # leave a one-window re-open credit: the honest impulse may be
            # arriving right now on a stale still-stamp (see _axis_killed_t).
            if not candidate.newborn:
                self._axis_killed_t = t
            self._candidate = None
            return []
        if candidate.newborn and t - candidate.t0 < cfg.newborn_confirm_min_s:
            # Confirmed on too little evidence to judge a carry. Hold — the
            # gates below need frames, not luck. See newborn_confirm_min_s.
            return []
        if candidate.newborn and not self._observed_since(cue, candidate.t0):
            # A carried ball coasts through finger occlusions; a struck cue on
            # open cloth is observed every frame of the confirm window.
            self._candidate = None
            return []
        if candidate.newborn and self._merged_frac_since(cue, candidate.t0) > (
            cfg.newborn_merged_frac_max
        ):
            # Every confirm observation is an oversized blob: the "ball" is
            # still inside a hand (pickup_newborn_0801 — a smooth, fully
            # observed carry that beat both gates above). Struck balls
            # measure 0% merged here; see newborn_merged_frac_max. Newborn
            # candidates only: a rest-anchored candidate legitimately streaks
            # (streaks are merge-flagged too), while its dwell/step/axis
            # gates already carry the rest-cycle evidence.
            self._candidate = None
            return []
        if candidate.handoff:
            if t - candidate.t0 < cfg.handoff_confirm_min_s:
                return []  # too early to judge a carry — see handoff_confirm_min_s
            open_gap = hypot(candidate.x_open - candidate.x0, candidate.y_open - candidate.y0)
            if displacement < open_gap + cfg.strike_min_travel_mm:
                # It must be *leaving* the rest spot, not arriving at it. The
                # axis check tests direction only, and a hand sweeping in along
                # the stroke line holds a near-perfect axis dot while its
                # distance from the anchor shrinks — so the player's own
                # approaching stick/hand blob, which is born right when the cue
                # goes dark and ends inside the handoff radius, satisfied every
                # other gate and fired. It cannot be caught by the carom guard
                # like other movers can, because here it *is* the candidate and
                # the guard skips the candidate's own stamps.
                #
                # A struck ball's distance from the rest grows monotonically off
                # the anchor (193 -> 221 mm over this window on the real
                # strike); a bringer's shrinks. No new constant: the margin is
                # the travel floor the candidate must clear anyway. Held rather
                # than killed, so a real strike that is briefly flat still has
                # the rest of its window.
                return []
            if (
                not self._observed_since(cue, candidate.t0)
                and candidate.impulse_raw < cfg.handoff_obs_floor_exempt_mms
            ):
                # A hand keeps losing the ball it is carrying (fingers wrap it,
                # long coast runs): measured 0.64 / 0.69 / 0.22 observed over
                # this window against 1.00 on both real handoff strikes. This
                # is the only carried-ball gate the path has, since the merged
                # one cannot apply.
                #
                # A ball at break speed is exempt: it is losing observations
                # because it has arrived in the rack, not because something is
                # holding it. See `handoff_obs_floor_exempt_mms` for the
                # measurement — the floor reads *worse* on the live break than
                # on the grab it exists to reject, so speed has to carry it.
                self._candidate = None
                return []
        self._candidate = None
        self._cue_rest = None
        self._axis_killed_t = None
        # The fire certifies both the track and the motion. Admit the ball to
        # the roster AND latch table motion directly: a strike whose only
        # mover is the fired track (nothing else disturbed) otherwise never
        # latches — the struck ball streaks, so it only becomes judgeable
        # after it stops, when its displacement no longer trips the mover
        # criterion — and the settle never comes (lone_roller_0801 froze the
        # live clock in MOTION for 33 s; pair_strikes_0801 lost a settle the
        # same way). If the fire was itself a phantom (nothing truly moving),
        # stillness returns within a dwell and the settle resolves it — a
        # reset, never a stuck clock.
        self._roster.add(cue.ball_id)
        self._table_moving = True
        self._all_still_since = None
        self._fired_this_episode = True
        # The confirm is itself a speed measurement: the displacement the
        # gates just verified, over the candidate's life. A break's white can
        # streak too fast to be *observed* at all, so the fastest-observed
        # sample alone underreads catastrophically — break_0801b read
        # 705 mm/s on a full rack explosion and the Hub's IDLE break gate ate
        # the real break. The kinematic bound cannot miss travel the confirm
        # already proved.
        dt = t - candidate.t0
        speed = candidate.impulse_raw
        if dt > 0:
            speed = max(speed, displacement / dt)
        return [
            ShotTaken(
                t,
                ball_id=cue.ball_id,
                speed_mms=speed,
                newborn=candidate.newborn,
            )
        ]

    def _observed_since(self, ball: BallState, t0: float) -> bool:
        rows = [p for p in self._history[ball.ball_id].positions if p[0] >= t0]
        if not rows:
            return False
        observed = sum(1 for p in rows if p[3])
        return observed >= self._cfg.newborn_confirm_obs_min * len(rows)

    def _merged_frac_since(self, ball: BallState, t0: float) -> float:
        """Fraction of the ball's observed rows since t0 that were merged blobs."""
        rows = [p for p in self._history[ball.ball_id].positions if p[0] >= t0 and p[3]]
        if not rows:
            return 0.0
        return sum(1 for p in rows if p[4]) / len(rows)

    # ---------- settle helpers ----------

    def _episode_is_pickup(self, t: float, balls: list[BallState]) -> bool:
        """True when this motion episode was a hand pickup, not a shot.

        Every ball that latched the episode must be gone (track dead), each
        must have last been observed far from any pocket (a pocket-adjacent
        death is a potted ball — a real shot), and none may have travelled
        beyond hand-drag scale (see EventConfig.pickup_travel_max_mm). A real
        shot always fails at least one: its balls come to rest on the table
        (tracks alive), or its potted balls die inside pocket zones after
        real travel — and while anything is still visibly flying the episode
        cannot be judged at all (below).
        """
        if not self._episode_origin:
            return False
        if self._episode_free_mover:
            # A judgeable non-mover ball translated during this episode: the
            # shot's motion ran on a track outside the roster mover set (a
            # no-cue strike's newborn continuation) — its settle is real.
            return False
        if any(
            self._last_translate.get(b.ball_id, float("-inf"))
            > self._first_seen.get(b.ball_id, float("inf"))  # really moved, not birth-init
            and t - self._last_translate[b.ball_id] < self._cfg.settle_dwell_s
            for b in balls
        ):
            # Something on the table translated within the last settle dwell:
            # the episode's motion is still in progress, so its movers dying
            # proves nothing yet. This is not redundant with the liveness
            # check below — in a no-cue strike (no_cue_strike_0801) the
            # struck ball's *original* track dies in the address handoff
            # (mirroring a pickup death) while its newborn continuation is
            # not yet roster, so the episode's mover set is all-dead even
            # though the shot is visibly mid-flight. _last_translate covers
            # coasted frames too, so a brief occlusion cannot open a one-
            # frame discard window.
            return False
        live = {b.ball_id for b in balls}
        for ball_id, origin in self._episode_origin.items():
            if ball_id in live:
                return False
            last = self._episode_last.get(ball_id)
            if last is None:
                return False
            if self._near_pocket(last[0], last[1]):
                return False
            if hypot(last[0] - origin[0], last[1] - origin[1]) > self._cfg.pickup_travel_max_mm:
                return False
        return True

    def _displacement_over(self, ball: BallState, window_s: float) -> float:
        """Net displacement from the ball's position ~window_s ago."""
        positions = self._history[ball.ball_id].positions
        t_now = positions[-1][0]
        ref = next((p for p in positions if p[0] >= t_now - window_s), positions[0])
        return hypot(ball.x_mm - ref[1], ball.y_mm - ref[2])

    def _run_witnessed(self, history: "_BallHistory", start: float, t: float) -> bool:
        """Every frame of [start, t] observed within the strike occlusion
        budget — the departure-evidence half of the own-rest path's
        presence-witnessed dwell (see `_maybe_open_rested`)."""
        prev_seen = start
        for p in history.positions:
            if p[0] < start:
                continue
            if p[3]:
                if p[0] - prev_seen > self._cfg.strike_step_max_s:
                    return False
                prev_seen = p[0]
        return t - prev_seen <= self._cfg.strike_step_max_s

    def _cluster_resident(self, ball: BallState) -> bool:
        """Old track whose spot the tracker still accounts for, without clean
        observations — a ball resting inside a cluster.

        Roster admission used to demand `_judgeable`, whose observed-fraction
        half a cluster ball can never show: adjacent resting balls merge into
        one blob and the matcher hands the blob around its member tracks
        frame by frame, so each reads present-not-observed near 100% of the
        time. flag_missed_3 (2026-08-06) measured the cost: the blue ball's
        track sat 176 s at ~0% observed through six settles, locked out of
        the roster — and when the struck cue's blob was re-associated onto
        it at the impulse, `_maybe_open_rested` refused the real shot on
        roster membership alone, with every behavioural gate downstream of
        the roster check ready to pass.

        Presence through sustained table quiet is the admission evidence:
        the union below only runs after `settle_dwell_s` of stillness, so a
        track that is old enough to judge and still being matched (any
        branch, merged-still included) is a ball standing on a quiet table,
        not a hand fragment sweeping through. What this deliberately admits
        beyond `_judgeable` is exactly the merged-still population; junk
        that merely coasts (matched by nothing) stays out — coasting is
        `present=False`.
        """
        positions = self._history[ball.ball_id].positions
        if positions[-1][0] - positions[0][0] < 0.8 * self._cfg.settle_creep_window_s:
            return False
        return ball.present

    def _struck_mover(self, ball: BallState) -> bool:
        """A track the roster cannot vouch for that is visibly a struck ball.

        The positive-evidence half of the motion latch (#40,
        `latch_blind_shot_0811`): the struck cue crossed the table at
        2048 mm/s as a two-frame-old track and the engine latched nothing at
        all — no episode, no provisional freeze, no settle, **0 events in
        20.74 s** — because only roster members could latch and roster
        admission happens only during sustained quiet.

        Four conditions, in cost order. Two are the ordinary motion evidence
        (`_solidly_observed`, `moving_enter_mm` of displacement); the other
        two are the gates the engine already uses to tell a struck ball from
        the things that fool it, reused rather than re-derived:

        * **speed** — two consecutive *observed* raw reports at
          `parked_fast_confirm_mms`, min of the pair so one merged yank
          cannot certify. Struck-ball kinematics.
        * **merges** — observed history at most `newborn_merged_frac_max`
          merged. A ball in a hand is inseparable from the hand.

        Measured over every non-roster track corpus-wide that clears the
        ordinary motion evidence (2026-08-11, 70 clips). Best
        consecutive-observed raw pair, by class:

            real strokes that newly latch   1210-3803  (jaw_strike 1210,
                scratch_shot 1296, late_emergence 1315, occluded_address
                1419, drill 1598, parked_vote_newborn 1828, latch_blind
                1917, pot_scratch 2646, phantom_candidate_block 3803)
            hand junk under the floor       <= 1122  (scratch_replace's
                throw 1122, lone_roller 1025, pickup_snatch 1022,
                address_chatter 871)

        and the ones that DO reach struck-ball speed are exactly the carried
        balls, which the merge gate then removes: pickup_snatch 0.67-0.71
        merged, and pickup_newborn / cue_grab / cluster_rest /
        scratch_replace / parked_vote_newborn's stick blob all 1.00 — against
        0.00-0.17 for every real stroke in the admitted set (latch_blind
        0.09, drill 0.17). The two gates share no failure mode: nothing in
        the corpus clears both without being a ball that was just struck.

        Age is deliberately not required. `_judgeable`'s age gate is what a
        newborn cannot satisfy in the frames that matter (the struck cue here
        needed 0.40 s to become judgeable, by which time the countdown had
        run through the stroke), and `_is_still` already makes exactly this
        trade in the other direction — a solidly observed newborn is position
        evidence from its first frames.

        What this deliberately does NOT do is stamp `_recent_movers` or
        `_last_mover_t`: a reborn cue must stay invisible to the carom guard
        and to the birth path's pre-strike-dwell test, or admitting it here
        would refuse the very strikes it exists to catch.
        """
        cfg = self._cfg
        if ball.ball_id not in self._fast_certified:
            return False
        positions = self._history[ball.ball_id].positions
        observed = [p for p in positions if p[3]]
        if not observed:
            return False
        if sum(1 for p in observed if p[4]) > cfg.newborn_merged_frac_max * len(observed):
            return False
        return (
            self._solidly_observed(ball)
            and self._displacement_over(ball, cfg.moving_window_s) > cfg.moving_enter_mm
        )

    def _judgeable(self, ball: BallState) -> bool:
        """Old and solid enough to judge motion/stillness from its history.

        Two junk filters (real footage produces tracks that are not balls):

        - Age: short-lived tracks (a hand sweeping over the cloth, a potted
          ball rattling in a pocket jaw) cannot show a meaningful window yet;
          most of a creep window of history is required. Real ball motion
          always outlives this within a shot.
        - Observed fraction: hand/stick fragments pass the detector's gates
          only intermittently, so their tracks are mostly coasted (measured
          7-43% observed vs. 90-100% for every real ball — see EventConfig).
          A mostly-coasted track's positions are extrapolations, not
          evidence, and must not drive the table-motion state either way.
        """
        positions = self._history[ball.ball_id].positions
        if positions[-1][0] - positions[0][0] < 0.8 * self._cfg.settle_creep_window_s:
            return False
        return self._solidly_observed(ball)

    def _solidly_observed(self, ball: BallState) -> bool:
        """Enough real observations to trust this track's positions at all.

        The fraction half of :meth:`_judgeable`, without the age
        requirement: hand/stick flicker measures 7-43 % observed vs.
        90-100 % for every real ball. Stillness judgment (`_is_still`)
        reads this alone — a newly reborn track carrying a still-rolling
        ball is position evidence from its first frames (hanger_pot_0801)
        even though its window is too short for creep or churn judgment.
        """
        positions = self._history[ball.ball_id].positions
        observed = sum(1 for p in positions if p[3])
        return observed >= self._cfg.judge_obs_fraction_min * len(positions)

    def _is_still(self, t: float, balls: list[BallState]) -> bool:
        """All balls position-stable over the creep window, none spinning.

        Position-based on purpose (see EventConfig): catches pocket-edge
        creep, immune to the velocity-estimate artifacts that blob merges
        cause on real footage. Spin-in-place is the one motion position
        cannot see, and the rules require waiting for it (WPA def. 8.2) —
        the appearance-churn gate covers it, scoped to the ball's own
        pixels so hands/shadows elsewhere stay invisible to settle.

        The churn gate only applies within spin_watch_s of the ball's last
        real translation: spin decays within seconds of a ball stopping,
        while reflection/decal noise on a glossy still ball persists
        indefinitely and must not hold settle hostage (2026-07-29, wall TV).

        Mostly-coasted hand/stick flicker is never judged: junk creep at
        5-6 mm per window would otherwise hold settle hostage for as long
        as the player leans over the table — the live "clock stays paused
        with my hands in frame" stall. But a *solidly observed* track blocks
        settle on real displacement even before it is old enough to judge
        (hanger_pot_0801: a fast shot's terminal track churn reborn the
        still-rolling cue as a fresh id at the pocket jaw — 190 mm/s,
        cleanly observed every frame — and the age gate let settle fire
        1.3 s after the strike while the ball visibly rolled on for another
        37 mm; the clock reset mid-flight). Junk cannot ride this: flicker
        fragments are 7-43 % observed, far under the fraction floor. The
        churn gate still requires a full judgeable window — a two-frame
        churn sample is not spin evidence.
        """
        cfg = self._cfg
        for ball in balls:
            if not self._solidly_observed(ball):
                continue
            if self._displacement_over(ball, cfg.settle_creep_window_s) > cfg.settle_creep_mm:
                return False
            if not self._judgeable(ball):
                continue
            if (
                ball.churn is not None
                and ball.churn > cfg.spin_churn_max
                and t - self._last_translate.get(ball.ball_id, t) <= cfg.spin_watch_s
            ):
                return False  # stationary but still spinning
        return True

    # ---------- scratch helpers ----------

    def _resolve_pending_scratch(self, t: float, balls: list[BallState]) -> list[Event]:
        """Publish a ScratchCandidate, or cancel the latch, once the evidence is in.

        A ball observed at the spot where the cue vanished means it never
        left the table — it stopped hanging in the jaw, which is a legal shot
        (hanger_pot_0801). Anything else, after `scratch_confirm_s`, is a
        scratch *candidate* — the verdict itself waits for the whiteness
        census (#26, see `_adjudicate_scratch`).

        Cancelling restores the same state as seeing the cue directly: the
        ball sitting there *is* the cue, so `_cue_last_seen` follows it. That
        also keeps the latch from re-arming on the next frame and firing the
        same phantom again when the whiteness vote is slow to return.
        """
        pending = self._pending_scratch
        if pending is None:
            return []
        cfg = self._cfg
        trigger_t, x, y, cutoff_t = pending
        nearest = min(
            (b for b in balls if b.observed),
            key=lambda b: hypot(b.x_mm - x, b.y_mm - y),
            default=None,
        )
        if nearest is not None and hypot(nearest.x_mm - x, nearest.y_mm - y) <= (
            cfg.scratch_reappear_mm
        ):
            self._pending_scratch = None
            self._scratch_fired = False
            self._cue_lost_while_moving = False
            self._cue_last_seen = (t, nearest.x_mm, nearest.y_mm, nearest.speed_mms)
            self._cue_last_color = (nearest.whiteness, nearest.color_age_s)
            return []
        if t - trigger_t >= cfg.scratch_confirm_s:
            self._pending_scratch = None
            self._scratch_open = (trigger_t, cutoff_t)
            pocket_x, pocket_y = min(cfg.pocket_centers(), key=lambda p: hypot(x - p[0], y - p[1]))
            whiteness, color_age_s = self._cue_last_color
            return [
                ScratchCandidate(
                    t,
                    x_mm=x,
                    y_mm=y,
                    pocket_x_mm=pocket_x,
                    pocket_y_mm=pocket_y,
                    whiteness=whiteness,
                    color_age_s=color_age_s,
                )
            ]
        return []

    def _adjudicate_scratch(self, t: float, balls: list[BallState]) -> list[Event]:
        """Resolve an open ScratchCandidate: refute, confirm, or keep waiting.

        The question the reappear watch never asks (#26): is the cue still on
        the table? Refutation takes positive evidence only — one roster ball,
        judgeable, still and observed, reading cue-grade whiteness, born
        before the dead track's last observation (the birth gate that keeps a
        hand-placed replacement from acquitting a real scratch). Confirmation
        takes a complete census: a quiet table with every live roster ball
        observed and none qualifying. Neither arriving by the horizon
        confirms too — absence of evidence keeps today's upper-bound bias.
        Thresholds and their anchors: see the config block.
        """
        open_ = self._scratch_open
        if open_ is None:
            return []
        cfg = self._cfg
        latch_t, cutoff_t = open_
        for b in balls:
            if not b.observed or b.ball_id not in self._roster:
                continue
            if self._first_seen.get(b.ball_id, float("inf")) >= cutoff_t:
                continue  # born after the cue died: could be the replacement
            if b.speed_mms >= cfg.settle_floor_mms or not self._judgeable(b):
                continue
            if b.whiteness is not None and b.whiteness >= cfg.scratch_witness_whiteness:
                self._scratch_open = None
                return [
                    ScratchRefuted(
                        t,
                        witness_id=b.ball_id,
                        x_mm=b.x_mm,
                        y_mm=b.y_mm,
                        whiteness=b.whiteness,
                    )
                ]
        if not self._table_moving and self._is_still(t, balls):
            live_roster = [b for b in balls if b.ball_id in self._roster]
            if live_roster and all(b.observed for b in live_roster):
                self._scratch_open = None
                return [Scratch(t)]
        if t - latch_t >= cfg.scratch_verdict_horizon_s:
            self._scratch_open = None
            return [Scratch(t)]
        return []

    def _near_pocket(self, x: float, y: float, speed_mms: float = 0.0) -> bool:
        """Did the cue die inside a pocket zone — allowing for one frame of flight?

        The death spot is the last *observed* position, and an observation is
        up to one detect frame stale by construction. A ball sinking at speed
        therefore reads systematically short of where it actually was when the
        track ended, by exactly `speed x frame interval`. Both 2026-08-11
        scratches died on that margin: the pot at **123 mm** and the kick at
        **124 mm** from the same corner, against a 120 mm zone, with the pot
        tracked marching in at 164 -> 148 -> 134 -> 123 mm over six frames at
        ~850 mm/s (#39). Neither reached the adjudicator at all.

        The allowance is a measured bound, not a threshold: at 850 mm/s a
        frame is ~11 mm, which is why 123 lands inside and the corpus's
        coasted phantom does not — `spin_in_place`'s last real observation sat
        **359 mm** out, and no single frame bridges 240 mm. It is deliberately
        NOT a bigger `pocket_zone_radius_mm`: widening the zone would admit
        slow deaths anywhere near a pocket, which is the ~6x over-fire #26
        just closed. A still ball gets no allowance at all.
        """
        zone = self._cfg.pocket_zone_radius_mm
        # Capped at one more zone radius so a starved detect loop (11-20 Hz
        # measured) cannot turn the allowance into a second, much larger zone.
        # The cap is expressed in the existing constant on purpose — it is not
        # a new tunable — and even at its ceiling it stays well short of
        # `spin_in_place`'s 359 mm coasted death.
        flight = min(max(speed_mms, 0.0) * (self._frame_dt_s or 0.0), zone)
        return any(hypot(x - px, y - py) <= zone + flight for px, py in self._cfg.pocket_centers())
