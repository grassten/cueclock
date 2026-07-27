"""Event engine vs. synthetic tracks.

Each test encodes a §4.2 clause or a §2.3 hard-won lesson from the old app —
these are the failure modes the rewrite must never re-learn. The same
scenarios get re-tested against real recorded clips once the corpus exists.
"""

from math import hypot

from cueclock.vision import BallsSettled, BallState, Event, EventEngine, Scratch, ShotTaken

DT = 1 / 80  # nominal detection cadence (frame timestamps; the engine is cadence-agnostic)

CUE = 0
OBJ = 1


class Sim:
    """Scripted ball motion → BallState frames → engine events."""

    def __init__(self) -> None:
        self.engine = EventEngine()
        self.t = 0.0
        self.balls: dict[int, list[float]] = {}  # id -> [x, y, vx, vy]

    def place(self, ball_id: int, x: float, y: float) -> None:
        self.balls[ball_id] = [x, y, 0.0, 0.0]

    def remove(self, ball_id: int) -> None:
        del self.balls[ball_id]

    def velocity(self, ball_id: int, vx: float, vy: float) -> None:
        self.balls[ball_id][2:] = [vx, vy]

    def run(self, seconds: float) -> list[Event]:
        events: list[Event] = []
        for _ in range(round(seconds / DT)):
            self.t += DT
            states = []
            for ball_id, ball in self.balls.items():
                ball[0] += ball[2] * DT
                ball[1] += ball[3] * DT
                states.append(
                    BallState(
                        ball_id, ball[0], ball[1], hypot(ball[2], ball[3]), is_cue=ball_id == CUE
                    )
                )
            events += self.engine.update(self.t, states)
        return events


def types(events: list[Event]) -> list[type]:
    return [type(e) for e in events]


def rack() -> Sim:
    sim = Sim()
    sim.place(CUE, 500.0, 500.0)
    sim.place(OBJ, 1200.0, 500.0)
    return sim


def test_legal_shot_then_settle() -> None:
    sim = rack()
    assert sim.run(1.0) == []  # dwell at rest: nothing fires
    sim.velocity(CUE, 500.0, 0.0)
    assert types(sim.run(0.5)) == [ShotTaken]
    sim.velocity(CUE, 0.0, 0.0)
    assert types(sim.run(1.2)) == [BallsSettled]


def test_soft_shot_at_threshold_speed() -> None:
    # Softest deliberate shot ≈ 450 mm/s (Dr. Dave); impulse floor is 250.
    sim = rack()
    sim.run(1.0)
    sim.velocity(CUE, 300.0, 0.0)
    assert ShotTaken in map(type, sim.run(0.5))


def test_ball_in_hand_drag_is_not_a_shot() -> None:
    # §2.3 lesson 6: placement is a continuous low-speed drag, not an impulse.
    sim = rack()
    sim.run(1.0)
    sim.velocity(CUE, 80.0, 0.0)
    sim.run(1.0)
    sim.velocity(CUE, 0.0, 0.0)
    assert sim.run(1.0) == []


def test_no_stillness_dwell_no_shot() -> None:
    # §2.3 lesson 1/6: motion that starts from an already-moving cue ball
    # (shaft adjustment, push after a nudge) is not a strike.
    sim = rack()
    sim.run(1.0)
    sim.velocity(CUE, 60.0, 0.0)  # above settle floor: stillness broken
    sim.run(0.5)
    sim.velocity(CUE, 400.0, 0.0)  # impulse — but not from rest
    sim.run(0.3)
    sim.velocity(CUE, 0.0, 0.0)
    assert types(sim.run(1.3)) == [BallsSettled]  # motion ended, but no ShotTaken


def test_single_frame_spike_is_not_a_shot() -> None:
    # Detection glitch: one frame of phantom speed with no follow-through.
    sim = rack()
    sim.run(1.0)
    sim.velocity(CUE, 300.0, 0.0)
    sim.run(DT)
    sim.velocity(CUE, 0.0, 0.0)
    assert sim.run(0.5) == []


def test_incoherent_trajectory_rejected() -> None:
    # §4.2 conjunction clause 3: displacement must agree with the strike axis.
    sim = rack()
    sim.run(1.0)
    sim.velocity(CUE, 300.0, 0.0)
    sim.run(DT)  # impulse frame opens the candidate
    sim.velocity(CUE, -300.0, 0.0)  # association glitch: ball "runs backwards"
    events = sim.run(1.5)
    assert ShotTaken not in map(type, events)


def test_pocket_creep_blocks_settle() -> None:
    # §4.2: speeds under the floor but still drifting ⇒ not settled.
    sim = rack()
    sim.run(1.0)
    sim.velocity(CUE, 500.0, 0.0)
    sim.run(0.5)
    sim.velocity(CUE, 0.0, 0.0)
    sim.velocity(OBJ, 30.0, 0.0)  # creep: under 40 mm/s floor, over 3 mm / 500 ms
    creep_events = sim.run(1.5)
    assert BallsSettled not in map(type, creep_events)
    sim.velocity(OBJ, 0.0, 0.0)
    assert types(sim.run(1.5)) == [BallsSettled]


def test_settle_hysteresis_no_refire() -> None:
    # Schmitt trigger: a settled table nudged below the moving threshold
    # must not produce a second balls_settled.
    sim = rack()
    sim.run(1.0)
    sim.velocity(CUE, 500.0, 0.0)
    sim.run(0.5)
    sim.velocity(CUE, 0.0, 0.0)
    sim.run(1.2)
    sim.velocity(OBJ, 100.0, 0.0)  # between floor (40) and moving threshold (120)
    sim.run(0.2)
    sim.velocity(OBJ, 0.0, 0.0)
    assert sim.run(1.5) == []


def test_scratch_latched_during_motion() -> None:
    # §2.3 lesson 3: the cue ball vanishes into a pocket mid-motion and the
    # player replaces it before settle — scratch must already be latched.
    sim = Sim()
    sim.place(CUE, 500.0, 40.0)
    sim.place(OBJ, 1200.0, 500.0)
    sim.run(1.0)
    sim.velocity(CUE, -700.0, 0.0)
    shot = sim.run(0.65)  # ends ~45 mm from the (0, 0) pocket
    assert types(shot) == [ShotTaken]
    sim.remove(CUE)
    events = sim.run(0.8)
    assert types(events) == [Scratch, BallsSettled]
    sim.place(CUE, 500.0, 500.0)  # ball in hand: replaced on the table
    assert sim.run(1.0) == []  # no re-scratch, no extra settle


def test_object_ball_pocketed_is_not_scratch() -> None:
    sim = Sim()
    sim.place(CUE, 500.0, 500.0)
    sim.place(OBJ, 500.0, 40.0)
    sim.run(1.0)
    sim.velocity(OBJ, -700.0, 0.0)  # rolled toward the (0, 0) pocket
    sim.run(0.65)
    sim.remove(OBJ)
    events = sim.run(1.2)
    assert Scratch not in map(type, events)
    assert BallsSettled in map(type, events)


def test_cue_lost_mid_table_is_not_scratch() -> None:
    # Track loss away from any pocket (severe occlusion) must not scratch.
    sim = rack()
    sim.run(1.0)
    sim.velocity(CUE, 500.0, 0.0)
    sim.run(0.3)  # cue is mid-table, moving
    sim.remove(CUE)
    events = sim.run(1.5)
    assert Scratch not in map(type, events)
