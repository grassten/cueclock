"""Event logic: per-ball track states in → shot/settle/scratch events out.

Implements docs/cv-app-rewrite.md §4.2. All thresholds are physical units
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
    churn: float | None = None  # smoothed appearance churn inside the ball's
    # own disk (spin shows here; a still ball shows only sensor noise);
    # None when unobserved or no sample yet


@dataclass(frozen=True)
class ShotTaken:
    t: float


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


@dataclass
class _BallHistory:
    # (t, x, y, observed) — observed=False rows are coasted extrapolations
    positions: deque[tuple[float, float, float, bool]] = field(default_factory=deque)


class EventEngine:
    """Feed per-frame track states; collect events. See module docstring."""

    def __init__(self, config: EventConfig | None = None) -> None:
        self._cfg = config or EventConfig()
        self._history: dict[int, _BallHistory] = {}
        self._table_moving = False
        self._all_still_since: float | None = None
        # Cue-ball rest memory: (t_still_start, x, y) of the current rest spot,
        # plus when the cue was last seen still. Survives the strike-time track
        # handoff (see EventConfig).
        self._cue_rest: tuple[float, float, float] | None = None
        self._cue_last_still_t = 0.0
        # Carom guard memory: (t, x, y) of every recent genuine observed
        # non-cue mover, pruned to the guard window. Survives the mover's
        # track dying at the kiss (blob merge).
        self._recent_movers: deque[tuple[float, float, float]] = deque()
        self._cue_id_last: int | None = None
        self._cue_id_streak = 0
        self._candidate: _StrikeCandidate | None = None
        self._cue_last_seen: tuple[float, float, float, float] | None = None  # (t, x, y, speed)
        self._cue_lost_while_moving = False
        self._scratch_fired = False
        # Per-ball last time it visibly translated (first sighting counts):
        # bounds how long the spin-churn gate may hold settle (spin_watch_s).
        self._last_translate: dict[int, float] = {}

    def update(self, t: float, balls: list[BallState]) -> list[Event]:
        events: list[Event] = []
        cfg = self._cfg
        cue = next((b for b in balls if b.is_cue), None)
        was_moving = self._table_moving

        # ---- strike detection ----
        # Debounce the cue flag: the tracker's whiteness vote can visit the
        # wrong ball for a frame around the impact (merged blobs corrupt the
        # sample); a one-frame excursion must not rewrite the rest memory.
        if cue is not None:
            if cue.ball_id == self._cue_id_last:
                self._cue_id_streak += 1
            else:
                self._cue_id_last, self._cue_id_streak = cue.ball_id, 1
        if cue is not None and self._cue_id_streak >= cfg.cue_id_debounce_frames:
            if self._candidate is not None:
                events.extend(self._advance_candidate(t, cue))
            else:
                self._maybe_open_candidate(t, cue)
            self._update_cue_rest(t, cue)

        # ---- table moving / settled (per-ball displacement over windows) ----
        for ball in balls:
            history = self._history.setdefault(ball.ball_id, _BallHistory())
            history.positions.append((t, ball.x_mm, ball.y_mm, ball.observed))
            while history.positions and history.positions[0][0] < t - cfg.settle_creep_window_s:
                history.positions.popleft()
            self._last_translate.setdefault(ball.ball_id, t)
            if self._displacement_over(ball, cfg.settle_creep_window_s) > cfg.settle_creep_mm:
                self._last_translate[ball.ball_id] = t
        self._history = {b.ball_id: self._history[b.ball_id] for b in balls}
        self._last_translate = {b.ball_id: self._last_translate[b.ball_id] for b in balls}

        movers = [
            b
            for b in balls
            if self._judgeable(b)
            and self._displacement_over(b, cfg.moving_window_s) > cfg.moving_enter_mm
        ]
        # Carom-guard evidence: non-cue balls in genuine observed motion. The
        # stamps are read by the *next* cycles' impulses (strike logic runs
        # first), which is exactly right — a bringer's approach precedes the
        # kiss. The reborn-cue track of a strike handoff cannot stamp: it is
        # too young to be judgeable before the candidate opens.
        cue_id = None if cue is None else cue.ball_id
        self._recent_movers.extend(
            (t, b.x_mm, b.y_mm) for b in movers if b.observed and b.ball_id != cue_id
        )
        stale = t - cfg.strike_other_motion_guard_s
        while self._recent_movers and self._recent_movers[0][0] < stale:
            self._recent_movers.popleft()
        if movers:
            self._table_moving = True
            self._all_still_since = None
        elif balls and self._is_still(t, balls):
            if self._all_still_since is None:
                self._all_still_since = t
            if self._table_moving and t - self._all_still_since >= cfg.settle_dwell_s:
                self._table_moving = False
                events.append(BallsSettled(t))
        else:
            self._all_still_since = None

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
            if self._cue_rest is None:
                self._cue_rest = (t, cue.x_mm, cue.y_mm)
            else:
                self._cue_rest = (self._cue_rest[0], cue.x_mm, cue.y_mm)
            self._cue_last_still_t = t
        elif self._cue_rest is not None and t - self._cue_last_still_t > cfg.strike_handoff_gap_s:
            self._cue_rest = None

    def _maybe_open_candidate(self, t: float, cue: BallState) -> None:
        cfg = self._cfg
        raw = cue.speed_mms if cue.raw_speed_mms is None else cue.raw_speed_mms
        if raw < cfg.strike_impulse_mms or self._cue_rest is None:
            return
        if t - self._cue_last_still_t > cfg.strike_step_max_s:
            return  # ramp, not a step: shaft/hand push accelerating the ball
        rest_t0, rest_x, rest_y = self._cue_rest
        if self._cue_last_still_t - rest_t0 < cfg.pre_strike_dwell_s:
            return
        if any(
            hypot(mx - rest_x, my - rest_y) <= cfg.carom_reach_mm
            for _mt, mx, my in self._recent_movers
        ):
            return  # carom guard: a moving ball reached the rest spot — a kiss
        dx, dy = cue.x_mm - rest_x, cue.y_mm - rest_y
        norm = hypot(dx, dy)
        if norm < cfg.strike_dir_seed_min_mm or norm > cfg.strike_handoff_radius_mm:
            return  # too little displacement to trust as a direction, or too far
        # Rest memory is kept until a candidate *fires*: a bogus candidate
        # (e.g. the impact yanking the old track's centroid) then fails the
        # axis check and the real impulse can still re-open from the same rest.
        self._candidate = _StrikeCandidate(
            t0=t, x0=rest_x, y0=rest_y, dir_x=dx / norm, dir_y=dy / norm
        )

    def _advance_candidate(self, t: float, cue: BallState) -> list[Event]:
        cfg = self._cfg
        candidate = self._candidate
        if candidate is None:
            return []
        if t - candidate.t0 > cfg.strike_confirm_window_s:
            self._candidate = None  # impulse without follow-through: not a shot
            return []
        dx, dy = cue.x_mm - candidate.x0, cue.y_mm - candidate.y0
        displacement = hypot(dx, dy)
        if displacement < cfg.strike_confirm_displacement_mm:
            return []
        axis_dot = (dx * candidate.dir_x + dy * candidate.dir_y) / displacement
        if axis_dot < cfg.strike_axis_dot_min:
            self._candidate = None  # incoherent trajectory (e.g. detection jitter)
            return []
        self._candidate = None
        self._cue_rest = None
        return [ShotTaken(t)]

    # ---------- settle helpers ----------

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

        Tracks that fail :meth:`_judgeable` (too young, or mostly-coasted
        hand/stick flicker) are not judged: junk creep at 5-6 mm per window
        would otherwise hold settle hostage for as long as the player leans
        over the table — the live "clock stays paused with my hands in
        frame" stall. A genuinely moving young ball is caught by the
        moving-entry displacement branch as soon as it is judgeable.
        """
        cfg = self._cfg
        for ball in balls:
            if not self._judgeable(ball):
                continue
            if self._displacement_over(ball, cfg.settle_creep_window_s) > cfg.settle_creep_mm:
                return False
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
