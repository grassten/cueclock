"""Warm-replay determinism: snapshot mid-run, resume, identical events.

The point of state snapshots in ring dumps (`analyze --warm`, docs/cli.md)
is that a restored core computes exactly what the uninterrupted live core
did — accumulated state (rest memory, roster, episode latches, whiteness
vote) included. These tests are that acceptance criterion on synthetic
input, so they run everywhere; tests/corpus/test_warm_replay.py is the
same criterion over real footage through the full VisionPipeline.

Snapshots are whole-object pickles (VisionPipeline.dump_state), deliberately
schema-free so state added to the tracker/engine later rides along — which
is also why these tests pickle the live objects rather than enumerate
fields.
"""

import pickle
from math import hypot

from cueclock.vision import BallsSettled, BallState, Event, EventEngine, ShotTaken
from cueclock.vision.track import BallTracker, Observation

DT = 1 / 80  # nominal detect cadence; both engine and tracker are cadence-agnostic

CUE = 0
OBJ = 1
SPARE = 2


class _Script:
    """Scripted ball motion recorded as (t, [BallState]) frames.

    Same physics as test_events.Sim, but the frames are kept so several
    engines can replay byte-identical input.
    """

    def __init__(self) -> None:
        self.t = 0.0
        self.balls: dict[int, list[float]] = {}  # id -> [x, y, vx, vy]
        self.occluded: set[int] = set()
        self.frames: list[tuple[float, list[BallState]]] = []

    def place(self, ball_id: int, x: float, y: float) -> None:
        self.balls[ball_id] = [x, y, 0.0, 0.0]

    def velocity(self, ball_id: int, vx: float, vy: float) -> None:
        self.balls[ball_id][2:] = [vx, vy]

    def run(self, seconds: float) -> None:
        for _ in range(round(seconds / DT)):
            self.t += DT
            states = []
            for ball_id, ball in self.balls.items():
                ball[0] += ball[2] * DT
                ball[1] += ball[3] * DT
                states.append(
                    BallState(
                        ball_id,
                        ball[0],
                        ball[1],
                        hypot(ball[2], ball[3]),
                        is_cue=ball_id == CUE,
                        observed=ball_id not in self.occluded,
                    )
                )
            self.frames.append((self.t, states))


def _scenario() -> list[tuple[float, list[BallState]]]:
    """Two shot→settle cycles with an address occlusion between them.

    Rich enough that a cut can land mid-dwell, mid-candidate, mid-episode,
    or mid-settle-window — the states a naive resume would corrupt.
    """
    s = _Script()
    s.place(CUE, 500.0, 500.0)
    s.place(OBJ, 1200.0, 500.0)
    s.place(SPARE, 300.0, 800.0)
    s.run(1.0)  # founding dwell: roster + rest memory build up
    s.velocity(CUE, 500.0, 0.0)  # shot 1
    s.run(0.5)
    s.velocity(CUE, 0.0, 0.0)
    s.run(1.2)  # settle 1
    s.occluded.add(CUE)  # address: hand/stick over the cue for a beat
    s.run(0.15)
    s.occluded.discard(CUE)
    s.run(0.5)
    s.velocity(CUE, 700.0, 0.0)  # shot 2, kissing motion into OBJ afterwards
    s.run(0.4)
    s.velocity(CUE, 0.0, 0.0)
    s.velocity(OBJ, 300.0, 0.0)
    s.run(0.3)
    s.velocity(OBJ, 0.0, 0.0)
    s.run(1.2)  # settle 2
    return s.frames


def _replay(engine: EventEngine, frames: list[tuple[float, list[BallState]]]) -> list[Event]:
    events: list[Event] = []
    for t, states in frames:
        events += engine.update(t, states)
    return events


def test_engine_snapshot_resume_reproduces_uninterrupted_run() -> None:
    frames = _scenario()
    baseline = _replay(EventEngine(), frames)
    # The scenario must actually exercise event state, or the comparison is vacuous.
    assert [type(e) for e in baseline] == [ShotTaken, BallsSettled, ShotTaken, BallsSettled]
    for cut in range(10, len(frames), 10):
        engine = EventEngine()
        prefix = _replay(engine, frames[:cut])
        resumed: EventEngine = pickle.loads(pickle.dumps(engine))
        suffix = _replay(resumed, frames[cut:])
        assert prefix + suffix == baseline, f"cut at frame {cut} diverged"


def _obs_frames() -> list[tuple[float, list[Observation]]]:
    """Rest → impulse (with a mid-flight occlusion gap) → rest, as raw
    Observations, so the tracker's own state (alpha-beta filters, coast
    budgets, whiteness EMA / cue vote) is part of the snapshot under test."""
    frames: list[tuple[float, list[Observation]]] = []
    t = 0.0
    cue_x = 500.0
    for _ in range(round(3.5 / DT)):
        t += DT
        vx = 500.0 if 1.0 <= t < 1.5 else 0.0
        cue_x += vx * DT
        obs = [Observation(x_mm=1200.0, y_mm=500.0, whiteness=0.4)]
        if not 1.2 <= t < 1.35:  # occlusion gap: the tracker must coast the cue
            obs.append(Observation(x_mm=cue_x, y_mm=500.0, whiteness=0.9))
        frames.append((t, obs))
    return frames


def _replay_pair(
    tracker: BallTracker, engine: EventEngine, frames: list[tuple[float, list[Observation]]]
) -> list[Event]:
    events: list[Event] = []
    for t, obs in frames:
        events += engine.update(t, tracker.update(t, obs))
    return events


def test_tracker_and_engine_snapshot_resume_reproduces_uninterrupted_run() -> None:
    frames = _obs_frames()
    baseline = _replay_pair(BallTracker(), EventEngine(), frames)
    assert ShotTaken in map(type, baseline) and BallsSettled in map(type, baseline)
    for cut in range(20, len(frames), 20):
        tracker, engine = BallTracker(), EventEngine()
        prefix = _replay_pair(tracker, engine, frames[:cut])
        # Snapshot both stages together, exactly as VisionPipeline.dump_state does.
        restored_tracker, restored_engine = pickle.loads(pickle.dumps((tracker, engine)))
        suffix = _replay_pair(restored_tracker, restored_engine, frames[cut:])
        assert prefix + suffix == baseline, f"cut at frame {cut} diverged"
