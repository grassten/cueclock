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
    # cadence: still-ball noise floor 1.2-1.5 (max 1.66), spinning cue
    # 2.6-6.5 decaying to the floor exactly at visible spin stop. 2.0 splits
    # the gap; recalibrate if the detect stride ever changes.
    spin_churn_max: float = 2.0

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
    cue_id_debounce_frames: int = 2  # ignore 1-frame cue-flag excursions in strike logic
    # Carom guard: an impulse into a dwelled cue only counts as a strike if the
    # table was settled shortly before it. A strike's cause (the final stroke)
    # precedes contact by tens of ms — and routinely trips the motion state
    # itself — while a carom's cause (the original shot) precedes the kiss by
    # the balls' travel time, well beyond this window.
    strike_settled_guard_s: float = 0.4
    # Impulse shape (§2.3 lesson 6): a strike steps from rest to full speed in
    # 1-2 frames; a shaft/hand push ramps up over many. The cue must have
    # been reported still this recently for the impulse to count.
    strike_step_max_s: float = 0.12

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
    positions: deque[tuple[float, float, float]] = field(default_factory=deque)  # (t, x, y)


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
        self._last_settled_t = 0.0
        self._cue_id_last: int | None = None
        self._cue_id_streak = 0
        self._candidate: _StrikeCandidate | None = None
        self._cue_last_seen: tuple[float, float, float, float] | None = None  # (t, x, y, speed)
        self._cue_lost_while_moving = False
        self._scratch_fired = False

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
            history.positions.append((t, ball.x_mm, ball.y_mm))
            while history.positions and history.positions[0][0] < t - cfg.settle_creep_window_s:
                history.positions.popleft()
        self._history = {b.ball_id: self._history[b.ball_id] for b in balls}

        moved = (
            b
            for b in balls
            if self._judgeable(b)
            and self._displacement_over(b, cfg.moving_window_s) > cfg.moving_enter_mm
        )
        if next(moved, None) is not None:
            self._table_moving = True
            self._all_still_since = None
        elif balls and self._is_still(balls):
            if self._all_still_since is None:
                self._all_still_since = t
            if self._table_moving and t - self._all_still_since >= cfg.settle_dwell_s:
                self._table_moving = False
                events.append(BallsSettled(t))
        else:
            self._all_still_since = None
        if not self._table_moving:
            self._last_settled_t = t

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
        if t - self._last_settled_t > cfg.strike_settled_guard_s:
            return  # carom guard: the table was already in motion before this
        if t - self._cue_last_still_t > cfg.strike_step_max_s:
            return  # ramp, not a step: shaft/hand push accelerating the ball
        rest_t0, rest_x, rest_y = self._cue_rest
        if self._cue_last_still_t - rest_t0 < cfg.pre_strike_dwell_s:
            return
        dx, dy = cue.x_mm - rest_x, cue.y_mm - rest_y
        norm = hypot(dx, dy)
        if norm == 0.0 or norm > cfg.strike_handoff_radius_mm:
            return
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
        """Old enough to judge motion/stillness from its position history.

        Real footage produces short-lived junk tracks (hand sweeping over the
        cloth, a potted ball rattling in a pocket jaw); requiring most of a
        creep window of history keeps them out of the table-motion state
        entirely. Real ball motion always outlives this within a shot.
        """
        positions = self._history[ball.ball_id].positions
        return positions[-1][0] - positions[0][0] >= 0.8 * self._cfg.settle_creep_window_s

    def _is_still(self, balls: list[BallState]) -> bool:
        """All balls position-stable over the creep window, none spinning.

        Position-based on purpose (see EventConfig): catches pocket-edge
        creep, immune to the velocity-estimate artifacts that blob merges
        cause on real footage. Spin-in-place is the one motion position
        cannot see, and the rules require waiting for it (WPA def. 8.2) —
        the appearance-churn gate covers it, scoped to the ball's own
        pixels so hands/shadows elsewhere stay invisible to settle.

        Tracks younger than the creep window are not judged: they cannot
        show a meaningful window yet, and the short-lived jittery phantoms
        real footage produces (e.g. a potted ball rattling in a pocket jaw)
        would otherwise block settle forever. A genuinely moving young ball
        is caught by the moving-entry displacement branch instead.
        """
        cfg = self._cfg
        for ball in balls:
            if not self._judgeable(ball):
                continue
            if self._displacement_over(ball, cfg.settle_creep_window_s) > cfg.settle_creep_mm:
                return False
            if ball.churn is not None and ball.churn > cfg.spin_churn_max:
                return False  # stationary but still spinning
        return True

    # ---------- scratch helpers ----------

    def _near_pocket(self, x: float, y: float) -> bool:
        return any(
            hypot(x - px, y - py) <= self._cfg.pocket_zone_radius_mm
            for px, py in self._cfg.pocket_centers()
        )
