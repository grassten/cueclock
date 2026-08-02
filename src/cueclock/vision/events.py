"""Event logic: per-ball track states in → shot/settle/scratch events out.

Implements docs/design.md §4.2. All thresholds are physical units
(table millimeters), sized against the measured noise floor (§4.3: velocity
noise ~ 7-22 mm/s raw, so the 40 mm/s settle floor is >= 3 sigma clean).

Pure module: no I/O, no clocks, no threads. The caller (tracker) feeds
``EventEngine.update()`` one batch of :class:`BallState` per detection frame
with its timestamp; cadence is whatever the caller runs at (nominally 80 Hz)
— everything here works from timestamps, not frame counts.

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
from dataclasses import dataclass, field
from math import hypot


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
    merged: bool = False  # this frame's observation was an oversized (merged)
    # blob: the ball is inseparable from a hand/stick or streaking. Meaningful
    # only when observed; the newborn-strike confirm reads it (a hand-carried
    # ball is merged every frame, a struck ball on open cloth almost never)
    churn: float | None = None  # smoothed appearance churn inside the ball's
    # own disk (spin shows here; a still ball shows only sensor noise);
    # None when unobserved or no sample yet


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
class Scratch:
    t: float


Event = ShotTaken | BallsSettled | Scratch


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
    strike_impulse_mms: float = 250.0
    # Confirm = real travel: a shaft adjustment can leave the ball at shot-like
    # speed but it stops within ~4 cm (measured on the corpus); any deliberate
    # shot covers 60 mm quickly (even the softest, 450 mm/s, in 0.13 s).
    strike_confirm_window_s: float = 0.25
    strike_confirm_displacement_mm: float = 60.0
    strike_axis_dot_min: float = 0.7
    # The impact itself disrupts the cue's own track (the stick merges with the
    # ball's blob), so the impulse often arrives on a *reborn* track shortly
    # after and some distance from where the cue rested. The rest-memory
    # tolerates that handoff:
    strike_handoff_gap_s: float = 0.35  # max unstill gap between rest and impulse
    strike_handoff_radius_mm: float = 250.0  # max impulse distance from the rest spot
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
    # 120, not 80: a sinking ball's last observations come from the pocket-jaw
    # blob, whose centroid drifts past the table edge as the ball goes down
    # (cue seen at 57+ mm outside the corner in the corpus scratch clip)
    pocket_zone_radius_mm: float = 120.0
    table_length_mm: float = 1930.0
    table_width_mm: float = 965.0

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
    last_raw: float | None = None
    ball_id: int | None = None
    impulse_raw: float = 0.0  # max raw speed seen while the candidate lives.
    # Not just the opening sample: on a handoff strike the open-frame raw is
    # averaged across the address-occlusion gap and reads far low (the
    # 2026-08-01 gated break opened at 2072 mm/s while the cue measurably
    # flew 5300-5500 — the confirm frames carry the honest speed).


@dataclass
class _BallHistory:
    # (t, x, y, observed, merged) — observed=False rows are coasted
    # extrapolations; merged marks oversized-blob observations
    positions: deque[tuple[float, float, float, bool, bool]] = field(default_factory=deque)


class EventEngine:
    """Feed per-frame track states; collect events. See module docstring."""

    def __init__(self, config: EventConfig | None = None) -> None:
        self._cfg = config or EventConfig()
        self._history: dict[int, _BallHistory] = {}
        self._table_moving = False
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
        self._cue_lost_while_moving = False
        self._scratch_fired = False
        # Per-ball last time it visibly translated (first sighting counts):
        # bounds how long the spin-churn gate may hold settle (spin_watch_s).
        self._last_translate: dict[int, float] = {}
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
        # Occluded-address strike arming: one shot per rest cycle. Disarmed
        # when a newborn candidate fires (no machine-gun re-opens off the
        # same strike); re-armed by a cue-flagged ball at rest that has
        # *translated since the disarm* (the struck cue coming to rest, or a
        # fresh placement) — the whiteness vote's fallback to some still ball
        # that never moved must not re-arm. Scratch no longer disarms
        # (checkpoint 31, jaw_strike_0801): a phantom scratch — the cue
        # stopping *in* a jaw, dying untracked over the pocket mouth — left
        # the disarm stuck with no cue track to ever re-arm it, and the real
        # strike out of the jaws was categorically dropped. The fetched-carry
        # false positive the scratch disarm guarded (scratch_replace, born
        # 2220 mm/s) dies on the three measured confirm gates instead:
        # observation ≥ 0.9, raw-step ratio ≤ 2, merged fraction ≤ 0.5.
        self._newborn_disarmed_at: float | None = None

    def update(self, t: float, balls: list[BallState]) -> list[Event]:
        events: list[Event] = []
        cfg = self._cfg
        cue = next((b for b in balls if b.is_cue), None)
        was_moving = self._table_moving

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
        if self._candidate is not None and t - self._candidate.t0 > cfg.strike_confirm_window_s:
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
            fast = [
                b.speed_mms if b.raw_speed_mms is None else b.raw_speed_mms
                for b in balls
                if b.observed
            ]
            if fast:
                self._candidate.impulse_raw = max(self._candidate.impulse_raw, max(fast))
        if self._candidate is not None and self._candidate.ball_id is not None:
            # A no-cue candidate follows its own track, not the cue flag.
            target = next((b for b in balls if b.ball_id == self._candidate.ball_id), None)
            if target is not None:
                events.extend(self._advance_candidate(t, target))
        elif cue is not None and self._cue_id_streak >= cfg.cue_id_debounce_frames:
            if self._candidate is not None:
                events.extend(self._advance_candidate(t, cue))
            else:
                self._maybe_open_candidate(t, cue)
            self._update_cue_rest(t, cue)
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
            # conjunction, an old roster track by its own rest.
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
                    if cue_quiet:
                        self._maybe_open_newborn(t, ball, raw, follow_ball=True)
                    if self._candidate is None:
                        self._maybe_open_rested(t, ball, raw)
                    if self._candidate is not None:
                        break

        # ---- table moving / settled (per-ball displacement over windows) ----
        for ball in balls:
            history = self._history.setdefault(ball.ball_id, _BallHistory())
            history.positions.append((t, ball.x_mm, ball.y_mm, ball.observed, ball.merged))
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
        self._first_seen = {b.ball_id: self._first_seen[b.ball_id] for b in balls}
        self._birth_pos = {b.ball_id: self._birth_pos[b.ball_id] for b in balls}

        movers = [
            b
            for b in balls
            if b.ball_id in self._roster
            and self._judgeable(b)
            and self._displacement_over(b, cfg.moving_window_s) > cfg.moving_enter_mm
        ]
        if movers:
            self._last_mover_t = t
            for b in movers:
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
        if movers:
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
                self._roster.update(b.ball_id for b in balls if self._judgeable(b))
            if self._table_moving and t - self._all_still_since >= cfg.settle_dwell_s:
                self._table_moving = False
                self._roster = {b.ball_id for b in balls if self._judgeable(b)}
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
            self._cue_lost_while_moving = False
            self._scratch_fired = False  # cue back on the table re-arms the latch
        elif cue is None and self._cue_last_seen is not None:
            last_t, last_x, last_y, last_speed = self._cue_last_seen
            if not self._cue_lost_while_moving:
                self._cue_lost_while_moving = was_moving and last_speed > cfg.settle_floor_mms
            if (
                self._cue_lost_while_moving
                and not self._scratch_fired
                and t - last_t >= cfg.cue_lost_latch_s
                and self._near_pocket(last_x, last_y)
            ):
                self._scratch_fired = True
                events.append(Scratch(t))

        return events

    # ---------- strike helpers ----------

    def _update_cue_rest(self, t: float, cue: BallState) -> None:
        cfg = self._cfg
        if cue.speed_mms < cfg.settle_floor_mms:
            if (
                self._newborn_disarmed_at is not None
                and self._last_translate.get(cue.ball_id, float("-inf")) > self._newborn_disarmed_at
            ):
                # A cue at rest that moved since the disarm: rest cycle
                # complete, the next fast newborn may be a strike again.
                self._newborn_disarmed_at = None
            if self._cue_rest is None:
                self._cue_rest = (t, cue.x_mm, cue.y_mm)
            elif (
                hypot(cue.x_mm - self._cue_rest[1], cue.y_mm - self._cue_rest[2])
                > cfg.strike_handoff_radius_mm
            ):
                # The flag jumped to a different still ball (the whiteness
                # vote wanders when the real cue is occluded): that is a new
                # rest, not the old one relocated — dwell credit restarts.
                # A genuinely creeping cue moves ~0.5 mm between stamps and
                # never trips this.
                self._cue_rest = (t, cue.x_mm, cue.y_mm)
            else:
                self._cue_rest = (self._cue_rest[0], cue.x_mm, cue.y_mm)
            self._cue_last_still_t = t
        elif self._cue_rest is not None and t - self._cue_last_still_t > cfg.strike_handoff_gap_s:
            self._cue_rest = None
            self._axis_killed_t = None  # no rest to re-open from

    def _maybe_open_candidate(self, t: float, cue: BallState) -> None:
        cfg = self._cfg
        raw = cue.speed_mms if cue.raw_speed_mms is None else cue.raw_speed_mms
        if raw < cfg.strike_impulse_mms:
            return
        if self._cue_rest is None:
            self._maybe_open_newborn(t, cue, raw)
            return
        if t - self._cue_last_still_t > cfg.strike_step_max_s and not (
            self._axis_killed_t is not None and t - self._axis_killed_t <= cfg.strike_step_max_s
        ):
            # Ramp, not a step: shaft/hand push accelerating the ball. A
            # just-axis-killed candidate is the exception — the tug that
            # opened it already burned the still-stamp window (see
            # _axis_killed_t) and the honest impulse arrives right behind.
            self._maybe_open_newborn(t, cue, raw)
            return
        rest_t0, rest_x, rest_y = self._cue_rest
        if self._cue_last_still_t - rest_t0 < cfg.pre_strike_dwell_s:
            return
        if any(
            hypot(mx - rest_x, my - rest_y) <= cfg.carom_reach_mm
            for _mt, mx, my, mid, _hand in self._recent_movers
            if mid != cue.ball_id
        ):
            return  # carom guard: a mover (ball or hand) reached the rest spot
        dx, dy = cue.x_mm - rest_x, cue.y_mm - rest_y
        norm = hypot(dx, dy)
        if norm < cfg.strike_dir_seed_min_mm or norm > cfg.strike_handoff_radius_mm:
            if norm > cfg.strike_handoff_radius_mm:
                # Impulse far from the remembered rest: the rest may be
                # anchored to the wrong ball (flag parked elsewhere while the
                # real cue was occluded) — the birth evidence gets its say.
                self._maybe_open_newborn(t, cue, raw)
            return  # too little displacement to trust as a direction, or too far
        # Rest memory is kept until a candidate *fires*: a bogus candidate
        # (e.g. the impact yanking the old track's centroid) then fails the
        # axis check and the real impulse can still re-open from the same rest.
        self._candidate = _StrikeCandidate(
            t0=t, x0=rest_x, y0=rest_y, dir_x=dx / norm, dir_y=dy / norm, impulse_raw=raw
        )

    def _maybe_open_newborn(
        self, t: float, cue: BallState, raw: float, follow_ball: bool = False
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
        if self._newborn_disarmed_at is not None:
            return
        if self._table_moving:
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
        if born is None or t - born > cfg.newborn_strike_max_age_s:
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
            impulse_raw=raw,
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
        if self._newborn_disarmed_at is not None:
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
            impulse_raw=raw,
        )

    def _advance_candidate(self, t: float, cue: BallState) -> list[Event]:
        cfg = self._cfg
        candidate = self._candidate
        if candidate is None:
            return []
        if t - candidate.t0 > cfg.strike_confirm_window_s:
            self._candidate = None  # impulse without follow-through: not a shot
            return []
        raw = cue.speed_mms if cue.raw_speed_mms is None else cue.raw_speed_mms
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
        if displacement < cfg.strike_confirm_displacement_mm:
            # Feasibility: a candidate that can no longer reach the confirm
            # displacement in the window it has left is dead now, not at
            # expiry. Physics, no new constants — a tip bump travels ~47 mm
            # and rests short of the 60 mm floor, and before this check its
            # candidate lingered at rest until a stick-merge centroid jump
            # could fake the remaining distance (tip_bump's phantom fire).
            # A real strike's ball is still at speed here and passes
            # trivially.
            time_left = candidate.t0 + cfg.strike_confirm_window_s - t
            if displacement + raw * time_left < cfg.strike_confirm_displacement_mm:
                self._candidate = None
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
        if candidate.newborn:
            self._newborn_disarmed_at = t  # one shot per rest cycle
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

    def _near_pocket(self, x: float, y: float) -> bool:
        return any(
            hypot(x - px, y - py) <= self._cfg.pocket_zone_radius_mm
            for px, py in self._cfg.pocket_centers()
        )
