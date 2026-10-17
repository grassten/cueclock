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
    assert states[0].observed
    # 150 ms occlusion: track must stay reported (coasting), same id — but
    # flagged unobserved, so the event layer knows it's extrapolation
    t = 10 * DT
    gap_frames = int(0.15 / DT)
    for i in range(gap_frames):
        states = tracker.update(t + i * DT, [])
        assert len(states) == 1
        assert states[0].ball_id == original_id
        assert not states[0].observed
    # reappears near where it was: same track continues
    t2 = t + gap_frames * DT
    states = tracker.update(t2, [_obs(502, 500)])
    assert [s.ball_id for s in states] == [original_id]
    assert states[0].observed


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


def test_merged_blob_on_still_ball_holds_position() -> None:
    # Checkpoint 28: the stick merging into an addressed ball's blob yanks
    # the centroid (31-47 mm measured). A still ball cannot streak, so the
    # merged observation is presence evidence only — position held.
    tracker = BallTracker()
    states = []
    for i in range(10):
        states = tracker.update(i * DT, [_obs(500, 500)])
    for i in range(10, 40):
        states = tracker.update(
            i * DT, [Observation(x_mm=540, y_mm=500, whiteness=0.1, merged=True)]
        )
    assert len(states) == 1
    assert abs(states[0].x_mm - 500) < 1.0  # yank ignored
    assert not states[0].observed  # contaminated centroid is not evidence
    assert states[0].speed_mms < 1.0
    for i in range(40, 50):  # stick leaves: clean observation, tracking resumes
        states = tracker.update(i * DT, [_obs(500, 500)])
    assert states[0].observed
    assert abs(states[0].x_mm - 500) < 2.0


def test_merged_blob_on_moving_ball_still_tracks() -> None:
    # §2.3 lesson 2 stands: fast balls streak into oversized blobs, and a
    # moving track must keep following its merged observations.
    tracker = BallTracker()
    speed = 1500.0
    last = None
    for i in range(40):
        t = i * DT
        states = tracker.update(
            t, [Observation(x_mm=100 + speed * t, y_mm=500, whiteness=0.1, merged=True)]
        )
        if states:
            last = states[0]
    assert last is not None
    assert last.observed
    assert last.speed_mms > 1000.0


# ---- cadence (checkpoint 106): 156 Hz must read like the tuned 78 Hz ----

FULL_DT = 1.0 / 156.25  # every camera frame


def test_at_tuned_cadence_is_identity() -> None:
    cfg = TrackConfig()
    assert cfg.at_cadence(156.25 / 2) is cfg


def test_at_full_rate_spans_the_same_time() -> None:
    tuned = TrackConfig()
    full = tuned.at_cadence(156.25)
    assert full.birth_frames == 2 * tuned.birth_frames
    # two full-rate EMA steps decay exactly as far as one tuned step
    assert np.isclose((1 - full.alpha) ** 2, 1 - tuned.alpha)
    assert np.isclose((1 - full.churn_ema) ** 2, 1 - tuned.churn_ema)
    assert np.isclose(full.beta, tuned.beta / 2)


def test_raw_speed_spans_a_tuned_interval_at_full_rate() -> None:
    """Jitter must not double the impulse-grade speed: at 156 Hz raw speed is
    measured across two frames (12.8 ms), exactly the tuned single step."""
    tracker = BallTracker(TrackConfig().at_cadence(156.25))
    jitter = [0.0, 0.5, 0.0, 0.5, 0.0, 0.5, 0.0, 0.5, 0.0, 0.5, 0.0, 0.5]
    states = []
    for i, dx in enumerate(jitter):
        states = tracker.update(i * FULL_DT, [_obs(500 + dx, 500)])
    assert states
    # one-frame step would read 0.5 mm / 6.4 ms = 78 mm/s; the two-frame
    # baseline spans a back-and-forth and reads ~0
    assert states[0].raw_speed_mms is not None and states[0].raw_speed_mms < 1.0


def test_raw_speed_at_tuned_cadence_is_the_one_step_speed() -> None:
    tracker = BallTracker()
    states = []
    for i in range(6):
        states = tracker.update(i * DT, [_obs(500 + 0.5 * (i % 2), 500)])
    assert states and states[0].raw_speed_mms is not None
    assert np.isclose(states[0].raw_speed_mms, 0.5 / DT)
