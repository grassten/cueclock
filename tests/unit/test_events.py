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
        self.occluded: set[int] = set()
        self.flickering: set[int] = set()
        self.churn: dict[int, float] = {}
        self._frame = 0

    def place(self, ball_id: int, x: float, y: float) -> None:
        self.balls[ball_id] = [x, y, 0.0, 0.0]

    def remove(self, ball_id: int) -> None:
        del self.balls[ball_id]
        self.occluded.discard(ball_id)
        self.flickering.discard(ball_id)
        self.churn.pop(ball_id, None)

    def occlude(self, ball_id: int) -> None:
        """Ball still reported but no longer observed: the tracker coasts it
        along its last velocity (position keeps integrating), observed=False."""
        self.occluded.add(ball_id)

    def flicker(self, ball_id: int) -> None:
        """Junk-track signature: detected only every third frame (~33%
        observed — hand/stick fragments measure 7-43% on real footage), the
        rest coasted. Real balls measure 90-100%."""
        self.flickering.add(ball_id)

    def set_churn(self, ball_id: int, value: float) -> None:
        """Appearance churn inside the ball's own disk (spin shows here)."""
        self.churn[ball_id] = value

    def velocity(self, ball_id: int, vx: float, vy: float) -> None:
        self.balls[ball_id][2:] = [vx, vy]

    def run(self, seconds: float) -> list[Event]:
        events: list[Event] = []
        for _ in range(round(seconds / DT)):
            self.t += DT
            self._frame += 1
            states = []
            for ball_id, ball in self.balls.items():
                ball[0] += ball[2] * DT
                ball[1] += ball[3] * DT
                observed = ball_id not in self.occluded and (
                    ball_id not in self.flickering or self._frame % 3 == 0
                )
                states.append(
                    BallState(
                        ball_id,
                        ball[0],
                        ball[1],
                        hypot(ball[2], ball[3]),
                        is_cue=ball_id == CUE,
                        observed=observed,
                        churn=self.churn.get(ball_id),
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
    # §4.2 conjunction clause 3: impulse-fast frames whose displacements never
    # cohere into real travel (a detection-jitter storm around the resting
    # ball) must not fire. Note the handoff-tolerant engine deliberately
    # treats *sustained coherent* motion after a glitch frame as a shot —
    # the ball demonstrably left its dwelled rest — so incoherence here means
    # no net travel, not merely a direction change.
    sim = rack()
    sim.run(1.0)
    for _ in range(40):  # ±300 mm/s alternating: violent jitter, no net travel
        sim.velocity(CUE, 0.0, 300.0)
        sim.run(DT)
        sim.velocity(CUE, 0.0, -300.0)
        sim.run(DT)
    sim.velocity(CUE, 0.0, 0.0)
    events = sim.run(0.5)
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


def test_settle_waits_for_spin_stop() -> None:
    # WPA def. 8.2 (rules caveat in §4.2): a shot ends only when all balls
    # have stopped moving AND spinning. A position-stationary ball whose own
    # pixels keep churning is still spinning and must hold settle.
    sim = rack()
    sim.run(1.0)
    sim.velocity(CUE, 500.0, 0.0)
    sim.run(0.5)
    sim.velocity(CUE, 0.0, 0.0)
    sim.set_churn(CUE, 5.0)  # stopped dead, spinning in place
    assert sim.run(1.5) == []  # would have settled without the churn gate
    sim.set_churn(CUE, 1.3)  # spin stops: churn back at the sensor-noise floor
    assert types(sim.run(1.0)) == [BallsSettled]


def test_glossy_still_ball_churn_does_not_block_settle() -> None:
    # 2026-07-29 (wall-TV day): the TV's moving image reflects off glossy
    # balls, holding a *still* ball's churn above spin_churn_max forever —
    # the 8-ball read a flat 2.2+ and settle never fired, so the clock never
    # started. Churn is a spin signal only near the ball's last translation
    # (spin_watch_s); a long-still ball's churn is lighting noise.
    sim = rack()
    sim.place(3, 900.0, 700.0)
    sim.set_churn(3, 3.0)  # above the gate, and it will never decay
    sim.run(10.5)  # outlives ball 3's spin watch window (first-sight start)
    sim.velocity(CUE, 500.0, 0.0)
    assert types(sim.run(0.5)) == [ShotTaken]
    sim.velocity(CUE, 0.0, 0.0)
    assert types(sim.run(1.2)) == [BallsSettled]


def test_hand_flicker_near_stroke_line_does_not_starve_strike() -> None:
    # missed_strike_0731 (checkpoint 20/22, ~50% of live shots): hand/stick
    # fragments sweep in as a flickering track, then creep 5-6 mm per window
    # on the stroke line while the player addresses the ball. The old
    # table-settledness carom guard starved on this — table_moving latched by
    # the sweep, settle blocked by the creep — and rejected the real strike.
    sim = rack()
    sim.run(1.0)
    sim.place(9, 900.0, 300.0)
    sim.flicker(9)
    sim.velocity(9, 250.0, 0.0)  # hand sweeps in (would latch table_moving)
    sim.run(0.4)
    sim.velocity(9, 12.0, 0.0)  # address creep: ~6 mm per 0.5 s window
    assert sim.run(2.0) == []  # no spurious settle cycle from the hand
    sim.velocity(CUE, 500.0, 0.0)  # the real strike
    assert ShotTaken in map(type, sim.run(0.5))


def test_stroke_gear_beyond_kiss_range_does_not_reject_strike() -> None:
    # medium_shot/power_shot regression (carom guard v2): the grip hand /
    # stick fragments read as well-observed genuine movers during the
    # backswing and final stroke — but from behind the bridge, 271-340 mm
    # from the cue's rest spot (measured). Only movers within kiss range
    # (carom_reach_mm) of the rest spot may reject the impulse.
    sim = rack()
    sim.run(1.0)
    sim.place(9, 200.0, 480.0)  # stroke gear: 300 mm behind the cue ball
    sim.run(0.6)  # judgeable, fully observed — the flicker filter won't bar it
    sim.velocity(9, 250.0, 0.0)  # forward stroke, moving right through contact
    sim.run(0.25)
    sim.velocity(CUE, 500.0, 0.0)  # the strike
    events = sim.run(0.5)
    sim.remove(9)
    assert ShotTaken in map(type, events)


def test_carom_kiss_into_resting_cue_is_not_a_shot() -> None:
    # The carom guard's reason to exist, pinned under its v2 (other-ball
    # motion) form: an object ball rolls across the table and kisses the
    # dwelled, resting cue ball. The cue's step impulse and coherent travel
    # pass every other strike gate — only the bringer's own genuine motion
    # in the preceding window says "kiss, not stroke".
    sim = rack()
    sim.run(1.0)
    sim.velocity(OBJ, -600.0, 0.0)  # bringer: 1200 -> ~540, arrives at the cue
    sim.run(1.1)
    sim.velocity(OBJ, 0.0, 0.0)  # momentum transfer at the kiss
    sim.velocity(CUE, -600.0, 0.0)
    sim.run(0.4)
    sim.velocity(CUE, 0.0, 0.0)
    events = sim.run(1.2)
    assert ShotTaken not in map(type, events)
    assert BallsSettled in map(type, events)


def test_hand_sweep_over_settled_table_is_not_a_settle_cycle() -> None:
    # BallsSettled resets the shot clock to a fresh 30 s (clock/state.py) —
    # a hand waved over the settled table must not mint one. Flicker junk
    # is barred from latching table_moving in the first place.
    sim = rack()
    sim.run(1.0)
    sim.velocity(CUE, 500.0, 0.0)
    sim.run(0.5)
    sim.velocity(CUE, 0.0, 0.0)
    assert BallsSettled in map(type, sim.run(1.2))
    sim.place(9, 800.0, 200.0)
    sim.flicker(9)
    sim.velocity(9, 400.0, 0.0)  # arm sweeps across the settled table
    sim.run(0.6)
    sim.remove(9)
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


def test_cue_coasting_into_pocket_zone_is_not_scratch() -> None:
    # spin_in_place corpus lesson: a cue occluded mid-flight keeps coasting on
    # its last velocity, and the phantom can slide into a pocket zone and die
    # there. Scratch must key on where the cue was last *observed* (the jaw,
    # for a real sink), never on where the extrapolation ended.
    sim = Sim()
    sim.place(CUE, 965.0, 600.0)
    sim.place(OBJ, 200.0, 500.0)
    sim.run(1.0)
    sim.velocity(CUE, 0.0, -1300.0)  # toward the top-middle pocket at (965, 0)
    shot = sim.run(0.2)  # last observed at y=340: far outside the 120 mm zone
    assert types(shot) == [ShotTaken]
    sim.occlude(CUE)
    sim.run(0.25)  # coasting phantom crosses into the pocket zone (y≈15)
    sim.remove(CUE)  # coast budget exhausted: track dies inside the zone
    events = sim.run(1.5)
    assert Scratch not in map(type, events)
