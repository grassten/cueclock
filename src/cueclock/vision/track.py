"""Ball tracking: per-frame observations in table mm → stable per-ball states.

Implements §4.1's tracking layer, deliberately minimal: greedy nearest-
neighbor association against constant-velocity predictions (at 78-156 Hz the
assignment is nearly unambiguous — the researched reason not to pull in a MOT
stack), an alpha-beta filter per ball for smoothed velocity, and coasting
through brief occlusions (§2.3 lesson 4: hands and cues cross over balls
constantly; a track must survive ~200 ms without detections). Coasted states
are reported with ``observed=False`` so the event layer can tell velocity
extrapolation from evidence.

Two speeds per ball, because one cannot serve both §4.2 consumers:

- ``speed_mms`` — alpha-beta-smoothed, for settle detection. Raw one-step
  velocity noise at 78 Hz (~40 mm/s with 0.3 px centroid jitter) sits *at*
  the settle floor; smoothing over ~20 frames brings it to ~10 mm/s so the
  40 mm/s floor is >=3 sigma clean again (§4.3 arithmetic).
- ``raw_speed_mms`` — one-step displacement over dt, for strike detection.
  A strike is a step from rest to >=250 mm/s within 1-2 frames; the smoothed
  estimate lags far too much to show that step, the raw one shows it
  immediately and its noise is far below the impulse threshold.

Cue-ball identity is resolved globally (§4.1, POOL-AID lesson): the single
whitest confirmed track wins, with an absolute whiteness floor (so a pocketed
cue does not hand the flag to the least-yellow object ball) and switching
hysteresis (so two near-white readings cannot flip-flop the flag).

Pure module: no I/O, no clocks, no threads.
"""

from dataclasses import dataclass, field
from math import hypot

from cueclock.vision.events import BallState


@dataclass(frozen=True)
class Observation:
    """One detection converted to table coordinates."""

    x_mm: float
    y_mm: float
    whiteness: float


@dataclass(frozen=True)
class TrackConfig:
    gate_mm: float = 100.0  # association gate vs. prediction (break impulse fits)
    birth_frames: int = 3  # consecutive hits before a track is reported
    coast_s: float = 0.25  # occlusion survival while moving (§2.3 lesson 4: ~200 ms)
    still_coast_s: float = 3.0  # while stationary: a ball being addressed is
    # occluded/merged with the cue stick for seconds and cannot move unseen
    still_speed_mms: float = 40.0  # below this a coasting track counts as stationary
    alpha: float = 0.3  # alpha-beta position gain
    beta: float = 0.05  # alpha-beta velocity gain (~20-frame smoothing at 78 Hz)
    two_point_init_mms: float = 120.0  # birth step above this seeds velocity directly
    whiteness_ema: float = 0.05  # slow: one merged-blob sample must not tank the cue's score
    whiteness_freeze_mms: float = 500.0  # motion blur corrupts color samples: don't
    # update whiteness while the ball moves fast (a flying cue must keep its flag)
    cue_min_whiteness: float = 0.3
    cue_switch_margin: float = 0.05
    cue_regrab_s: float = 1.0  # after the flagged track dies, report no cue this long
    # instead of crowning the next-whitest ball — scratch detection keys on the
    # cue being *absent*, and the palest yellow reads deceptively white


@dataclass
class _Track:
    track_id: int
    x: float
    y: float
    t: float
    vx: float = 0.0
    vy: float = 0.0
    hits: int = 1
    whiteness: float = 0.0
    raw_speed: float = 0.0
    last_obs: tuple[float, float, float] = field(default=(0.0, 0.0, 0.0))  # t, x, y

    def predict(self, t: float) -> tuple[float, float]:
        dt = t - self.t
        return self.x + self.vx * dt, self.y + self.vy * dt


class BallTracker:
    """Feed per-frame observations; get per-ball :class:`BallState` lists."""

    def __init__(self, config: TrackConfig | None = None) -> None:
        self._cfg = config or TrackConfig()
        self._tracks: list[_Track] = []
        self._next_id = 0
        self._cue_id: int | None = None
        self._cue_lost_t: float | None = None

    def update(self, t: float, observations: list[Observation]) -> list[BallState]:
        cfg = self._cfg

        # ---- associate: globally sort candidate pairs by distance, greedily
        # take each track/observation once (resolves adjacent balls correctly)
        pairs: list[tuple[float, int, int]] = []
        for ti, track in enumerate(self._tracks):
            px, py = track.predict(t)
            for oi, obs in enumerate(observations):
                d = hypot(obs.x_mm - px, obs.y_mm - py)
                if d <= cfg.gate_mm:
                    pairs.append((d, ti, oi))
        pairs.sort()
        matched_tracks: set[int] = set()
        matched_obs: set[int] = set()
        for _d, ti, oi in pairs:
            if ti in matched_tracks or oi in matched_obs:
                continue
            matched_tracks.add(ti)
            matched_obs.add(oi)
            self._update_track(self._tracks[ti], t, observations[oi])

        # ---- unmatched tracks: coast, or die after the coast budget ----
        # (stationary tracks get a much longer budget — see TrackConfig)
        survivors: list[_Track] = []
        for ti, track in enumerate(self._tracks):
            still = hypot(track.vx, track.vy) < cfg.still_speed_mms
            budget = cfg.still_coast_s if still else cfg.coast_s
            if ti in matched_tracks or t - track.last_obs[0] <= budget:
                if ti not in matched_tracks:
                    if still:
                        # a still ball does not move unseen — freeze it, so a
                        # residual few-mm/s velocity can't drift the phantom
                        # around and stall settle detection
                        track.vx = track.vy = 0.0
                        track.raw_speed = 0.0
                    else:
                        track.x, track.y = track.predict(t)
                        track.raw_speed = hypot(track.vx, track.vy)
                    track.t = t
                survivors.append(track)
        self._tracks = survivors

        # ---- unmatched observations: tentative new tracks ----
        for oi, obs in enumerate(observations):
            if oi not in matched_obs:
                self._tracks.append(
                    _Track(
                        track_id=self._next_id,
                        x=obs.x_mm,
                        y=obs.y_mm,
                        t=t,
                        whiteness=obs.whiteness,
                        last_obs=(t, obs.x_mm, obs.y_mm),
                    )
                )
                self._next_id += 1

        confirmed = [tr for tr in self._tracks if tr.hits >= cfg.birth_frames]
        self._resolve_cue(t, confirmed)
        return [
            BallState(
                ball_id=tr.track_id,
                x_mm=tr.x,
                y_mm=tr.y,
                speed_mms=hypot(tr.vx, tr.vy),
                raw_speed_mms=tr.raw_speed,
                is_cue=tr.track_id == self._cue_id,
                observed=tr.last_obs[0] == t,
            )
            for tr in confirmed
        ]

    def _update_track(self, track: _Track, t: float, obs: Observation) -> None:
        cfg = self._cfg
        last_t, last_x, last_y = track.last_obs
        obs_dt = t - last_t
        if obs_dt > 0:
            track.raw_speed = hypot(obs.x_mm - last_x, obs.y_mm - last_y) / obs_dt
        if track.hits == 1 and obs_dt > 0 and track.raw_speed > cfg.two_point_init_mms:
            # born moving: seed velocity from the first step — alpha-beta from
            # v=0 would take many frames to catch up to a struck ball
            track.vx = (obs.x_mm - last_x) / obs_dt
            track.vy = (obs.y_mm - last_y) / obs_dt
            track.x, track.y = obs.x_mm, obs.y_mm
        else:
            px, py = track.predict(t)
            rx, ry = obs.x_mm - px, obs.y_mm - py
            track.x = px + cfg.alpha * rx
            track.y = py + cfg.alpha * ry
            dt = t - track.t
            if dt > 0:
                track.vx += cfg.beta * rx / dt
                track.vy += cfg.beta * ry / dt
        track.t = t
        track.hits += 1
        if track.raw_speed < cfg.whiteness_freeze_mms:
            track.whiteness += cfg.whiteness_ema * (obs.whiteness - track.whiteness)
        track.last_obs = (t, obs.x_mm, obs.y_mm)

    def _resolve_cue(self, t: float, confirmed: list[_Track]) -> None:
        cfg = self._cfg
        if not confirmed:
            return  # keep the flag: the cue may reappear (ball-in-hand)
        current = next((tr for tr in confirmed if tr.track_id == self._cue_id), None)
        if current is None and self._cue_id is not None:
            # flag holder just vanished (pocketed / picked up): report no cue
            # for a grace period rather than crowning the next-whitest ball
            if self._cue_lost_t is None:
                self._cue_lost_t = t
            if t - self._cue_lost_t < cfg.cue_regrab_s:
                return
        if current is not None:
            self._cue_lost_t = None
        challenger = max(confirmed, key=lambda tr: tr.whiteness)
        if challenger.whiteness < cfg.cue_min_whiteness:
            if current is None:
                self._cue_id = None
            return
        if current is None or challenger.whiteness > current.whiteness + cfg.cue_switch_margin:
            self._cue_id = challenger.track_id
            self._cue_lost_t = None
