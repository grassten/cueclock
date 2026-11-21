"""#67: how long the cue ball was seen rolling in the motion a settle ends
(`vision/roll.py`, docs/design.md §21). A ball that moved by itself rolls in
the open; a ball in a hand is one blob with the hand."""

import dataclasses

import numpy as np
import pytest

from cueclock.vision.events import BallsSettled
from cueclock.vision.pipeline import VisionPipeline
from cueclock.vision.roll import CueRollWitness
from tests.unit.test_session_log import _calibration, _scene

REST = (600.0, 640.0)


def _at(*spots: tuple[float, float]) -> np.ndarray:
    return np.array(spots, dtype=np.float64).reshape(-1, 2)


def _run(
    hz: float,
    seconds: float,
    whites,  # t -> np.ndarray of clean cue-white positions
    moving,  # t -> bool: the episode latch
    shot_at: float | None = None,
) -> float | None:
    """Feed ``seconds`` of frames, settle on the last one."""
    w = CueRollWitness(hz)
    n = int(seconds * hz)
    shot_k = None if shot_at is None else int(shot_at * hz)
    for k in range(n):
        t = k / hz
        w.frame(whites(t), moving(t), k == shot_k)
    return w.settle()


@pytest.mark.parametrize("hz", [78.125, 156.25])
def test_a_struck_ball_is_seen_rolling_for_as_long_as_it_rolls(hz: float) -> None:
    def whites(t: float) -> np.ndarray:
        if t < 3.0:
            return _at(REST)
        return _at((REST[0] + 1000.0 * min(t - 3.0, 0.8), REST[1]))  # 0.8 s of roll

    roll = _run(hz, 6.0, whites, lambda t: 3.0 <= t < 5.5, shot_at=3.0)
    assert roll is not None and abs(roll - 0.8) <= 2.5 / hz


@pytest.mark.parametrize("hz", [78.125, 156.25])
def test_a_carried_ball_that_shows_for_a_frame_is_not(hz: float) -> None:
    """Hand and ball are one blob: nothing clean on the way but one stray
    frame, then the ball clean at its new spot."""
    stray = int(4.0 * hz) / hz

    def whites(t: float) -> np.ndarray:
        if t < 3.0:
            return _at(REST)
        if abs(t - stray) < 1e-9:
            return _at((900.0, 640.0))
        if t >= 5.0:
            return _at((1300.0, 700.0))  # put down
        return _at()

    roll = _run(hz, 6.0, whites, lambda t: 3.0 <= t < 5.5)
    assert roll is not None and roll <= 1.0 / hz + 1e-9


def test_a_ball_hidden_at_the_window_s_start_still_gets_its_spot() -> None:
    """Nothing clean and white in the window's first 50 ms (a hand over it),
    then the ball seen resting before the motion: that is where it stood,
    not the cue ball rolling (26 of 152 live shots open like this)."""
    hz = 156.25

    def whites(t: float) -> np.ndarray:
        if t < 2.3:
            return _at()
        if t < 3.0:
            return _at(REST)
        return _at()  # picked up and carried off

    assert _run(hz, 4.0, whites, lambda t: 3.0 <= t < 3.9) == 0.0


def test_a_slow_bump_is_seen_rolling() -> None:
    """`tip_bump`'s shape: 48 mm at a few hundred mm/s, read 0.19 s."""
    hz = 78.125

    def whites(t: float) -> np.ndarray:
        if t < 3.0:
            return _at(REST)
        return _at((REST[0] + 160.0 * min(t - 3.0, 0.3), REST[1]))

    roll = _run(hz, 5.0, whites, lambda t: 3.0 <= t < 4.5, shot_at=3.0)
    assert roll is not None and roll >= 0.19


def test_a_resting_white_elsewhere_does_not_count() -> None:
    """A stripe that reads white, standing still through the motion."""
    hz = 156.25

    def whites(t: float) -> np.ndarray:
        return _at(REST, (1500.0, 300.0)) if t < 3.0 else _at((1500.0, 300.0))

    assert _run(hz, 5.0, whites, lambda t: 3.0 <= t < 4.0) == 0.0


def test_no_motion_since_the_settle_before_is_no_evidence() -> None:
    """The pipeline's backstop settle: no latch, no strike."""
    assert _run(156.25, 3.0, lambda t: _at(REST), lambda t: False) is None


def test_the_window_opens_no_earlier_than_the_settle_before() -> None:
    """A roll that ended at the settle before is that settle's, not this one's."""
    hz = 156.25
    w = CueRollWitness(hz)
    for k in range(int(4 * hz)):
        t = k / hz
        x = REST[0] + 1000.0 * min(max(t - 1.0, 0.0), 0.8)
        w.frame(_at((x, REST[1])), 1.0 <= t < 3.5, False)
    first = w.settle()
    assert first is not None and first > 0.7
    for k in range(int(4 * hz), int(5 * hz)):
        w.frame(_at((REST[0] + 800.0, REST[1])), k / hz < 4.2, False)  # where it stopped
    assert w.settle() == 0.0


def test_the_history_stays_bounded_while_the_table_is_quiet() -> None:
    hz = 156.25
    w = CueRollWitness(hz)
    for _ in range(int(120 * hz)):
        w.frame(_at(REST), False, False)
    assert len(w._frames) <= hz * 1.1  # about a second, not two minutes


def test_the_pipeline_reports_it_at_every_settle() -> None:
    """End to end on logged-style detections (no pixels): a cue struck at 3 s
    rolls for 0.8 s; the settle carries that, a merged blob never counts."""
    hz = 156.25
    vision = VisionPipeline(_calibration(), None, cadence_hz=hz)
    settles = []
    for i in range(int(8 * hz)):
        t = i / hz
        dets = _scene(t)
        if 3.2 < t < 3.3:
            dets[0] = dataclasses.replace(dets[0], merged=True)
        events = vision.advance(t, dets, [1.0] * len(dets), [])
        if any(isinstance(e, BallsSettled) for e in events):
            settles.append(vision.last_settle_cue_roll_s)
    assert len(settles) == 1 and settles[0] is not None
    assert 0.6 <= settles[0] <= 0.8
