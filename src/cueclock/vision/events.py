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
settle time). Each ball carries two speeds: ``speed_mms`` (smoothed —
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

    # Settle (Schmitt-trigger hysteresis: floor to settle, higher bar to re-enter moving)
    settle_floor_mms: float = 40.0
    moving_threshold_mms: float = 120.0
    moving_confirm_frames: int = 2  # consecutive frames above threshold to call the table moving
    settle_dwell_s: float = 0.5
    settle_creep_mm: float = 3.0  # max any-ball displacement over the creep window
    settle_creep_window_s: float = 0.5

    # Strike = conjunction: stationary dwell + velocity impulse + coherent trajectory
    pre_strike_dwell_s: float = 0.4
    strike_impulse_mms: float = 250.0
    strike_impulse_frames: int = 2  # rest → impulse within this many detection frames
    strike_confirm_window_s: float = 0.15
    strike_confirm_displacement_mm: float = 30.0
    strike_axis_dot_min: float = 0.7

    # Scratch
    cue_lost_latch_s: float = 0.3
    pocket_zone_radius_mm: float = 80.0
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
        self._moving_streak = 0
        self._all_still_since: float | None = None
        # Cue-ball bookkeeping
        self._cue_still_since: float | None = None
        self._cue_prev: deque[tuple[float, float, float, float]] = deque(
            maxlen=4
        )  # (t, x, y, speed)
        self._candidate: _StrikeCandidate | None = None
        self._cue_last_seen: tuple[float, float, float, float] | None = None  # (t, x, y, speed)
        self._cue_lost_while_moving = False
        self._scratch_fired = False

    def update(self, t: float, balls: list[BallState]) -> list[Event]:
        events: list[Event] = []
        cfg = self._cfg
        cue = next((b for b in balls if b.is_cue), None)
        was_moving = self._table_moving

        # ---- strike detection (uses table state from *before* this frame, so the
        # impulse frame itself does not disqualify the strike) ----
        if cue is not None:
            if self._candidate is not None:
                events.extend(self._advance_candidate(t, cue))
            elif not was_moving:
                self._maybe_open_candidate(t, cue)
            self._update_cue_stillness(t, cue)
            self._cue_prev.append((t, cue.x_mm, cue.y_mm, cue.speed_mms))

        # ---- table moving / settled (per-ball speeds with hysteresis) ----
        for ball in balls:
            history = self._history.setdefault(ball.ball_id, _BallHistory())
            history.positions.append((t, ball.x_mm, ball.y_mm))
            while history.positions and history.positions[0][0] < t - cfg.settle_creep_window_s:
                history.positions.popleft()
        self._history = {b.ball_id: self._history[b.ball_id] for b in balls}

        if any(b.speed_mms > cfg.moving_threshold_mms for b in balls):
            self._moving_streak += 1
            if self._moving_streak >= cfg.moving_confirm_frames:
                self._table_moving = True
            self._all_still_since = None
        else:
            self._moving_streak = 0
            if balls and self._is_still(balls):
                if self._all_still_since is None:
                    self._all_still_since = t
                if self._table_moving and t - self._all_still_since >= cfg.settle_dwell_s:
                    self._table_moving = False
                    events.append(BallsSettled(t))
            else:
                self._all_still_since = None

        # ---- scratch: cue track terminated near a pocket while balls were moving ----
        if cue is not None:
            self._cue_last_seen = (t, cue.x_mm, cue.y_mm, cue.speed_mms)
            self._cue_lost_while_moving = False
            self._scratch_fired = False  # cue back on the table re-arms the latch
        elif self._cue_last_seen is not None:
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

    def _update_cue_stillness(self, t: float, cue: BallState) -> None:
        if cue.speed_mms < self._cfg.settle_floor_mms:
            if self._cue_still_since is None:
                self._cue_still_since = t
        else:
            self._cue_still_since = None

    def _maybe_open_candidate(self, t: float, cue: BallState) -> None:
        cfg = self._cfg
        raw = cue.speed_mms if cue.raw_speed_mms is None else cue.raw_speed_mms
        if raw < cfg.strike_impulse_mms:
            return
        # Stationary dwell must be satisfied at the moment of impulse.
        if self._cue_still_since is None or not self._cue_prev:
            return
        recent = list(self._cue_prev)[-cfg.strike_impulse_frames :]
        rest_t = next(
            (pt for pt, _, _, ps in recent if ps < cfg.settle_floor_mms),
            None,
        )
        if rest_t is None or rest_t - self._cue_still_since < cfg.pre_strike_dwell_s:
            return
        _prev_t, prev_x, prev_y, _ = self._cue_prev[-1]
        dx, dy = cue.x_mm - prev_x, cue.y_mm - prev_y
        norm = hypot(dx, dy)
        if norm == 0.0:
            return
        self._candidate = _StrikeCandidate(
            t0=t, x0=prev_x, y0=prev_y, dir_x=dx / norm, dir_y=dy / norm
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
        return [ShotTaken(t)]

    # ---------- settle helpers ----------

    def _is_still(self, balls: list[BallState]) -> bool:
        cfg = self._cfg
        if any(b.speed_mms >= cfg.settle_floor_mms for b in balls):
            return False
        # Displacement gate catches pocket-edge creep that stays under the
        # speed floor (§4.2) — and is immune to a ball spinning in place.
        for ball in balls:
            positions = self._history[ball.ball_id].positions
            _t0, x0, y0 = positions[0]
            if hypot(ball.x_mm - x0, ball.y_mm - y0) > cfg.settle_creep_mm:
                return False
        return True

    # ---------- scratch helpers ----------

    def _near_pocket(self, x: float, y: float) -> bool:
        return any(
            hypot(x - px, y - py) <= self._cfg.pocket_zone_radius_mm
            for px, py in self._cfg.pocket_centers()
        )
