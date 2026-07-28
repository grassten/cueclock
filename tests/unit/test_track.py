"""Tracker tests on synthetic observation streams — no camera, no clips."""

import numpy as np

from cueclock.vision.track import BallTracker, Observation, TrackConfig

DT = 1.0 / 78.0  # nominal detection cadence (every 2nd camera frame)


def _obs(x: float, y: float, whiteness: float = 0.1) -> Observation:
    return Observation(x_mm=x, y_mm=y, whiteness=whiteness)


def test_birth_requires_consecutive_frames() -> None:
    tracker = BallTracker(TrackConfig(birth_frames=3))
    assert tracker.update(0 * DT, [_obs(500, 500)]) == []
    assert tracker.update(1 * DT, [_obs(500, 500)]) == []
    states = tracker.update(2 * DT, [_obs(500, 500)])
    assert len(states) == 1


def test_single_frame_flicker_never_reported() -> None:
    tracker = BallTracker()
    assert tracker.update(0.0, [_obs(300, 300)]) == []
    for i in range(1, 10):
        assert tracker.update(i * DT, []) == []


def test_stable_id_and_speed_convergence() -> None:
    tracker = BallTracker()
    speed = 1000.0  # mm/s along x
    ids = set()
    last = None
    for i in range(60):
        t = i * DT
        states = tracker.update(t, [_obs(100 + speed * t, 500)])
        if states:
            ids.add(states[0].ball_id)
            last = states[0]
    assert len(ids) == 1
    assert last is not None
    assert last.speed_mms == np.float64(last.speed_mms)
    assert abs(last.speed_mms - speed) < 0.15 * speed
    assert last.raw_speed_mms is not None
    assert abs(last.raw_speed_mms - speed) < 0.05 * speed


def test_raw_speed_shows_impulse_before_smoothed() -> None:
    """The §4.2 strike step: rest → 2 m/s must appear in raw speed at once."""
    tracker = BallTracker()
    for i in range(20):
        tracker.update(i * DT, [_obs(500, 500)])
    x = 500.0
    t = 20 * DT
    x += 2000.0 * DT  # one frame at 2 m/s
    states = tracker.update(t, [_obs(x, 500)])
    assert states[0].raw_speed_mms is not None
    assert states[0].raw_speed_mms > 1500.0  # immediately visible
    assert states[0].speed_mms < 500.0  # smoothed estimate lags — by design


def test_two_parallel_balls_keep_identities() -> None:
    tracker = BallTracker()
    id_by_y: dict[float, set[int]] = {300: set(), 500: set()}
    for i in range(40):
        t = i * DT
        x = 200 + 800 * t
        states = tracker.update(t, [_obs(x, 300), _obs(x, 500)])
        for s in states:
            id_by_y[round(s.y_mm / 100) * 100].add(s.ball_id)
    assert len(id_by_y[300]) == 1 and len(id_by_y[500]) == 1
    assert id_by_y[300] != id_by_y[500]


def test_coasts_through_occlusion_and_reattaches() -> None:
    tracker = BallTracker(TrackConfig(coast_s=0.25))
    for i in range(10):
        states = tracker.update(i * DT, [_obs(500, 500)])
    original_id = states[0].ball_id
    # 150 ms occlusion: track must stay reported (coasting), same id
    t = 10 * DT
    gap_frames = int(0.15 / DT)
    for i in range(gap_frames):
        states = tracker.update(t + i * DT, [])
        assert len(states) == 1
        assert states[0].ball_id == original_id
    # reappears near where it was: same track continues
    t2 = t + gap_frames * DT
    states = tracker.update(t2, [_obs(502, 500)])
    assert [s.ball_id for s in states] == [original_id]


def test_moving_track_dies_after_coast_budget() -> None:
    tracker = BallTracker(TrackConfig(coast_s=0.25))
    for i in range(10):
        t = i * DT
        tracker.update(t, [_obs(500 + 1000 * t, 500)])
    t0 = 10 * DT
    dead_after = int(0.3 / DT) + 1
    states = []
    for i in range(dead_after):
        states = tracker.update(t0 + i * DT, [])
    assert states == []  # terminated: absent from output (scratch keys on this)


def test_still_track_survives_address_occlusion() -> None:
    """A stationary ball merged with the cue stick for ~1 s must not die —
    that is exactly the pre-strike address in every real shot clip."""
    tracker = BallTracker()
    for i in range(10):
        states = tracker.update(i * DT, [_obs(500, 500)])
    original_id = states[0].ball_id
    t = 10 * DT
    for i in range(int(1.0 / DT)):
        states = tracker.update(t + i * DT, [])
        assert [s.ball_id for s in states] == [original_id]


def test_two_point_init_makes_born_moving_track_fast() -> None:
    """A track born on an already-moving ball reads near-true speed at once."""
    tracker = BallTracker(TrackConfig(birth_frames=3))
    speed = 1000.0
    states = []
    for i in range(3):
        t = i * DT
        states = tracker.update(t, [_obs(200 + speed * t, 500)])
    assert len(states) == 1
    assert states[0].speed_mms > 0.8 * speed


def test_cue_is_whitest_confirmed_track_with_floor() -> None:
    tracker = BallTracker()
    for i in range(5):
        states = tracker.update(
            i * DT, [_obs(300, 300, whiteness=0.6), _obs(700, 700, whiteness=0.1)]
        )
    cues = [s for s in states if s.is_cue]
    assert len(cues) == 1
    assert cues[0].x_mm < 400

    # all-dark scene: nobody gets the flag
    tracker2 = BallTracker()
    for i in range(5):
        states = tracker2.update(
            i * DT, [_obs(300, 300, whiteness=0.1), _obs(700, 700, whiteness=0.15)]
        )
    assert not any(s.is_cue for s in states)


def test_cue_flag_does_not_flipflop() -> None:
    tracker = BallTracker()
    flags: list[int] = []
    rng = np.random.default_rng(7)
    for i in range(40):
        w1 = 0.5 + float(rng.normal(0, 0.01))
        w2 = 0.5 + float(rng.normal(0, 0.01))
        states = tracker.update(i * DT, [_obs(300, 300, w1), _obs(700, 700, w2)])
        flags.extend(s.ball_id for s in states if s.is_cue)
    assert len(set(flags)) == 1  # hysteresis: one winner, no oscillation
