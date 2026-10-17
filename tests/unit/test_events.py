"""Event engine vs. synthetic tracks.

Each test encodes a §4.2 clause or a §2.3 hard-won lesson from the old app —
these are the failure modes the rewrite must never re-learn. The same
scenarios get re-tested against real recorded clips once the corpus exists.
"""

from math import hypot

from cueclock.vision import (
    BallsSettled,
    BallState,
    Event,
    EventEngine,
    Scratch,
    ScratchCandidate,
    ScratchRefuted,
    ShotTaken,
)

DT = 1 / 80  # nominal detection cadence (frame timestamps; the engine is cadence-agnostic)

CUE = 0
OBJ = 1
REBORN = 2  # a struck ball's continuation: a track born mid-episode, so it is
# neither a roster member nor the flag holder


class Sim:
    """Scripted ball motion → BallState frames → engine events."""

    def __init__(self) -> None:
        self.engine = EventEngine()
        self.t = 0.0
        self.balls: dict[int, list[float]] = {}  # id -> [x, y, vx, vy]
        self.cue_id = CUE  # whiteness-vote holder; tests park it elsewhere
        self.occluded: set[int] = set()
        self.flickering: set[int] = set()
        self.merged_ids: set[int] = set()
        self.churn: dict[int, float] = {}
        self.whiteness: dict[int, float] = {}
        self._frame = 0

    def place(self, ball_id: int, x: float, y: float) -> None:
        self.balls[ball_id] = [x, y, 0.0, 0.0]

    def remove(self, ball_id: int) -> None:
        del self.balls[ball_id]
        self.occluded.discard(ball_id)
        self.flickering.discard(ball_id)
        self.merged_ids.discard(ball_id)
        self.churn.pop(ball_id, None)

    def occlude(self, ball_id: int) -> None:
        """Ball still reported but no longer observed: the tracker coasts it
        along its last velocity (position keeps integrating), observed=False."""
        self.occluded.add(ball_id)

    def reveal(self, ball_id: int) -> None:
        """Occlusion over: the ball is observed again."""
        self.occluded.discard(ball_id)

    def flicker(self, ball_id: int) -> None:
        """Junk-track signature: detected only every third frame (~33%
        observed — hand/stick fragments measure 7-43% on real footage), the
        rest coasted. Real balls measure 90-100%."""
        self.flickering.add(ball_id)

    def merge(self, ball_id: int) -> None:
        """Every observation of this ball is an oversized (merged) blob —
        the detector's hand/stick-inseparable signature on real footage."""
        self.merged_ids.add(ball_id)

    def set_churn(self, ball_id: int, value: float) -> None:
        """Appearance churn inside the ball's own disk (spin shows here)."""
        self.churn[ball_id] = value

    def paint(self, ball_id: int, whiteness: float) -> None:
        """Give the ball a tracker whiteness EMA (the scratch census reads
        it). Unpainted balls report None — never witness-grade."""
        self.whiteness[ball_id] = whiteness

    def velocity(self, ball_id: int, vx: float, vy: float) -> None:
        self.balls[ball_id][2:] = [vx, vy]

    def displace(self, ball_id: int, dx: float, dy: float = 0.0) -> None:
        """Instant centroid excursion — a corrupt observation, not motion
        (the ball's velocity is untouched; the position simply jumps)."""
        self.balls[ball_id][0] += dx
        self.balls[ball_id][1] += dy

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
                        is_cue=ball_id == self.cue_id,
                        observed=observed,
                        merged=observed and ball_id in self.merged_ids,
                        churn=self.churn.get(ball_id),
                        whiteness=self.whiteness.get(ball_id),
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


def test_observation_chatter_does_not_stall_settle() -> None:
    # settle_stall_0801 (the 2026-08-01 12-16 s late-settle cluster): a
    # parked ball under the stick throws brief centroid excursions past the
    # creep gate — measured <= 64 ms per excursion, every ~0.2-0.3 s — and
    # each one used to wipe the whole accumulated settle dwell, so a clean
    # 0.5 s window never occurred and the clock stalled in MOTION. Dwell
    # credit must survive un-still transients shorter than the motion
    # window; genuine motion (>= 371 ms runs measured corpus-wide) still
    # resets it — test_pocket_creep_blocks_settle is the control.
    sim = rack()
    sim.place(3, 900.0, 700.0)  # the parked ball with the corrupt blob
    sim.run(1.0)
    sim.velocity(CUE, 500.0, 0.0)
    assert types(sim.run(0.5)) == [ShotTaken]
    sim.velocity(CUE, 0.0, 0.0)
    events: list[Event] = []
    for _ in range(12):  # 8 mm single-frame mode flips at sub-dwell intervals
        events += sim.run(0.2)
        sim.displace(3, 8.0)
        events += sim.run(DT)
        sim.displace(3, -8.0)
    assert BallsSettled in map(type, events)


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
    # The scratch is *latched* here but published a scratch_confirm_s later
    # (#6, 2026-08-02) — the delay is what lets a cue that merely stopped in
    # the jaw cancel its own phantom by reappearing. Nothing turns up at this
    # death spot, so the candidate stands — and with the table quiet, every
    # roster ball observed and none reading cue-grade white, the census
    # completes on the spot and confirms it (#26).
    assert types(sim.run(0.8)) == [BallsSettled]
    assert types(sim.run(0.8)) == [ScratchCandidate, Scratch]
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


def test_hand_pickup_of_a_ball_is_not_a_settle_cycle() -> None:
    # ball_in_hand_pickup_0801: the pickup drag latches motion (a roster ball
    # genuinely moves), then the fingers wrap it and the track dies mid-table.
    # The quiet that follows is not a shot settling — no clock reset.
    sim = rack()
    sim.run(1.0)
    sim.velocity(OBJ, 400.0, 0.0)  # hand drag: past the motion latch
    sim.run(0.2)  # ~80 mm, hand-drag scale (measured 41-78 mm live)
    sim.remove(OBJ)  # picked up: track dead, far from any pocket
    assert sim.run(1.5) == []


def test_potted_ball_track_death_still_settles() -> None:
    # The discard's contrast case: a potted ball also dies, but inside a
    # pocket zone after real travel — its settle is real and must survive.
    sim = rack()
    sim.run(1.0)
    sim.velocity(OBJ, -2000.0, -800.0)  # rolled at the top-left pocket
    sim.run(0.55)  # dies at ~(100, 60): inside the pocket zone
    sim.remove(OBJ)
    sim.velocity(CUE, 0.0, 0.0)
    assert BallsSettled in map(type, sim.run(1.3))


def test_track_rebirth_mid_roll_defers_settle() -> None:
    # hanger_pot_0801: a fast shot's terminal track churn can rebirth the
    # still-rolling ball as a brand-new id (coast-overshoot death at the
    # pocket jaw). The reborn track is cleanly observed and really
    # displacing from its first frames — settle must wait for IT to stop,
    # not fire the moment every *old* track has gone quiet (the live clip
    # reset the clock 1.3 s after the strike with the cue visibly rolling).
    sim = rack()
    sim.run(1.0)
    sim.velocity(CUE, 500.0, 0.0)
    sim.run(0.5)
    sim.remove(CUE)  # terminal churn: the fast track dies mid-table...
    sim.place(7, 760.0, 500.0)  # ...and is reborn as a young id, still rolling
    sim.velocity(7, 200.0, 0.0)
    assert BallsSettled not in map(type, sim.run(0.6))
    sim.velocity(7, 0.0, 0.0)
    assert BallsSettled in map(type, sim.run(1.2))


def test_stroke_tug_then_strike_still_fires() -> None:
    # stroke_tug_strike_0801: the final stroke tugs the addressed cue's
    # centroid just past the axis-seed floor for longer than the step
    # window, opening a candidate with a garbage axis. The real impulse
    # then fails that candidate's axis check — and the re-open credit must
    # let it found a fresh candidate from the same rest, or the shot is
    # silently missed (the live clip: clock never stopped, then reset).
    sim = rack()
    sim.run(1.0)
    sim.velocity(CUE, 0.0, 260.0)  # stroke tug: impulse-fast, wrong axis
    sim.run(0.15)  # ~39 mm — outlives the 0.12 s step window, under 60 mm
    sim.velocity(CUE, 2000.0, 0.0)  # the honest strike, orthogonal
    assert ShotTaken in map(type, sim.run(0.3))


def test_no_cue_newborn_strike_fires() -> None:
    # Control for the carried-ball gate below, and the no-cue newborn path
    # (no_cue_strike_0801) in synthetic form: the occluded cue's strike
    # surfaces as a brand-new track already at impulse speed on a quiet
    # table, cleanly detected every confirm frame.
    sim = rack()
    sim.run(1.0)
    sim.occlude(CUE)  # address occlusion: no observed ball holds the flag
    sim.run(0.2)
    sim.place(7, 800.0, 300.0)
    sim.cue_id = 7  # the annulus vote crowns a clean white emergence on its
    # first confirmed frame (measured born-whiteness 0.98-1.00, checkpoint
    # 73 traces) — that crowning is what un-parks the birth candidate; an
    # uncrowned emergence waits for the parked travel confirm instead
    sim.velocity(7, 900.0, 0.0)
    assert ShotTaken in map(type, sim.run(0.4))


def test_hand_carried_newborn_is_not_a_shot() -> None:
    # pickup_newborn_0801: picking the resting cue up births a new track
    # from the hand+ball blob, sliding smoothly and fully observed — it
    # beats the observation and speed-smoothness gates. But every one of
    # its observations is merged (a ball in a hand is inseparable from the
    # hand); real strikes measure 0% merged confirm observations.
    sim = rack()
    sim.run(1.0)
    sim.occlude(CUE)
    sim.run(0.2)
    sim.place(7, 800.0, 300.0)
    sim.merge(7)  # hand+ball blob every frame
    sim.velocity(7, 900.0, 0.0)
    assert ShotTaken not in map(type, sim.run(0.4))


def scratch_prelude() -> Sim:
    """Strike the cue into the (0, 0) pocket zone and let Scratch latch."""
    sim = Sim()
    sim.place(CUE, 500.0, 40.0)
    sim.place(OBJ, 1200.0, 500.0)
    sim.run(1.0)
    sim.velocity(CUE, -700.0, 0.0)
    shot = sim.run(0.65)  # dies ~60 mm from the (0, 0) pocket
    assert types(shot) == [ShotTaken]
    sim.remove(CUE)
    # Latched immediately, published a scratch_confirm_s later (#6) — so the
    # settle now lands first. Nothing reappears at the death spot, so the
    # candidate stands and the quiet-table census confirms it at once (#26).
    assert types(sim.run(0.8)) == [BallsSettled]
    assert types(sim.run(0.8)) == [ScratchCandidate, Scratch]
    sim.run(1.0)  # table quiet; no cue-flagged track exists
    return sim


def test_cue_stopping_in_the_jaw_is_not_a_scratch() -> None:
    # #6, hanger_pot_0801: a legal pot whose cue follows in and stops hanging
    # in the jaw. Its track dies inside the pocket zone — identical to a pot
    # at the moment of death — and it fired a Scratch 0.75 s after a shot
    # Steven confirmed legal. The counter-evidence was never missing, only
    # late: measured on that clip, a reborn track appears 0.24 s after the
    # loss, 33 mm from the death spot. Deferring the verdict is the fix.
    sim = Sim()
    sim.place(CUE, 500.0, 40.0)
    sim.place(OBJ, 1200.0, 500.0)
    sim.run(1.0)
    sim.velocity(CUE, -700.0, 0.0)
    assert types(sim.run(0.65)) == [ShotTaken]  # dies ~45 mm from (0, 0)
    sim.remove(CUE)  # track lost over the pocket mouth
    sim.run(0.5)  # latched, not yet published
    sim.place(7, 55.0, 45.0)  # the ball was there all along: reborn as a new id
    events = sim.run(2.0)
    assert Scratch not in map(type, events)
    assert ScratchCandidate not in map(type, events)  # cancelled pre-candidate


def test_a_different_ball_near_the_pocket_does_not_cancel_a_scratch() -> None:
    # The other side of the same gate. Cancellation is positional, so it has
    # to be tight enough that an object ball resting near the same pocket
    # cannot vouch for a cue that genuinely dropped. In the hanger_pot
    # episode the reappearing cue sat 33 mm away while an unrelated still
    # ball sat 94 mm away; the gate is 60 mm.
    sim = Sim()
    sim.place(CUE, 500.0, 40.0)
    sim.place(OBJ, 1200.0, 500.0)
    sim.run(1.0)
    sim.velocity(CUE, -700.0, 0.0)
    assert types(sim.run(0.65)) == [ShotTaken]
    sim.remove(CUE)
    sim.run(0.5)
    sim.place(7, 140.0, 40.0)  # ~95 mm away: a neighbour, not the cue
    assert Scratch in map(type, sim.run(2.0))


def test_the_vote_finding_another_ball_does_not_cancel_a_scratch() -> None:
    # After the cue drops, the whiteness vote parks on some other white-ish
    # ball within a few frames. That is the vote finding a replacement, not
    # the cue reappearing — and treating it as the cue returning suppressed
    # all three genuine scratches in the corpus while this fix was being
    # built. Only position may cancel.
    sim = Sim()
    sim.place(CUE, 500.0, 40.0)
    sim.place(OBJ, 1200.0, 500.0)
    sim.run(1.0)
    sim.velocity(CUE, -700.0, 0.0)
    assert types(sim.run(0.65)) == [ShotTaken]
    sim.remove(CUE)
    sim.run(0.5)
    sim.cue_id = OBJ  # the vote parks on a ball far from the death spot
    assert Scratch in map(type, sim.run(2.0))


def test_false_scratch_refuted_by_the_resting_cue() -> None:
    # #26's reason to exist: a potted object ball mis-crowned as the cue
    # passes every latch gate honestly — it died near a pocket while moving,
    # and it genuinely never reappears. The live journals confessed a ~6x
    # over-count from exactly this shape. The one fact the reappear watch
    # never checks: the real cue is still sitting on the table, reading
    # cue-grade white. One witness — roster, judgeable, still, observed,
    # born before the death — kills the candidate.
    sim = Sim()
    sim.place(CUE, 500.0, 40.0)  # the crown, wrongly, on a potted stripe
    sim.place(OBJ, 1200.0, 500.0)  # the real cue, at rest all along
    sim.paint(OBJ, 0.9)
    sim.run(1.0)
    sim.velocity(CUE, -700.0, 0.0)
    assert types(sim.run(0.65)) == [ShotTaken]
    sim.remove(CUE)
    events = sim.run(3.0)
    assert ScratchCandidate in map(type, events)
    assert ScratchRefuted in map(type, events)
    assert Scratch not in map(type, events)


def test_replacement_cue_does_not_refute_a_real_scratch() -> None:
    # The mandatory birth gate: scratch_replace measured the hand-placed
    # replacement reading 0.880 within ~1 s of landing — witness-grade
    # white, and meaningless, because it was born after the cue died. It
    # must not refute; the census completes around it and the Scratch
    # stands.
    sim = Sim()
    sim.place(CUE, 500.0, 40.0)
    sim.place(OBJ, 1200.0, 500.0)
    sim.run(1.0)
    sim.velocity(CUE, -700.0, 0.0)
    assert types(sim.run(0.65)) == [ShotTaken]
    sim.remove(CUE)
    sim.run(0.5)  # latched; still inside the reappear watch
    sim.place(7, 900.0, 700.0)  # ball in hand, far from the death spot
    sim.paint(7, 0.9)
    events = sim.run(3.0)
    assert Scratch in map(type, events)
    assert ScratchRefuted not in map(type, events)


def test_flicker_junk_cannot_refute_a_scratch() -> None:
    # The corpus sweep's one witness-grade outlier was a single-frame flash
    # track reading 1.000. A witness must be roster-admitted, judgeable,
    # still and observed — hand/stick flicker (7-43% observed) fails the
    # judgeable bar however white it reads, so a real scratch still
    # confirms past it.
    sim = Sim()
    sim.place(CUE, 500.0, 40.0)
    sim.place(OBJ, 1200.0, 500.0)
    sim.place(9, 800.0, 800.0)
    sim.paint(9, 1.0)  # reads pure white whenever it is seen at all...
    sim.flicker(9)  # ...but it is junk: ~33% observed
    sim.run(1.0)
    sim.velocity(CUE, -700.0, 0.0)
    assert types(sim.run(0.65)) == [ShotTaken]
    sim.remove(CUE)
    events = sim.run(3.0)
    assert Scratch in map(type, events)
    assert ScratchRefuted not in map(type, events)


def test_blocked_census_confirms_at_the_horizon() -> None:
    # Refute only on positive evidence: a census that never completes (the
    # corpus shows the cue absent from it for an entire 41 s occluded
    # address) cannot acquit anything. The verdict falls back to confirming
    # at the horizon — the pre-#26 upper-bound bias, deliberately kept.
    sim = Sim()
    sim.place(CUE, 500.0, 40.0)
    sim.place(OBJ, 1200.0, 500.0)
    sim.run(1.0)
    sim.velocity(CUE, -700.0, 0.0)
    assert types(sim.run(0.65)) == [ShotTaken]
    sim.remove(CUE)
    sim.occlude(OBJ)  # the census can never complete: OBJ is never observed
    early = sim.run(28.0)  # horizon is 30 s from the latch (~1.95 s here)
    assert ScratchCandidate in map(type, early)
    assert Scratch not in map(type, early)
    assert Scratch in map(type, sim.run(5.0))


def test_shot_from_pocket_jaw_after_scratch_fires() -> None:
    # jaw_strike_0801: a cue that stops hanging in a jaw dies untracked over
    # the pocket mouth and mints a phantom Scratch — then the player strikes
    # the invisible hanger out of the jaws, surfacing as a newborn at
    # impulse speed. Scratch must not disarm the newborn path: with no cue
    # track on the table there is nothing to ever re-arm it, and the strike
    # is categorically dropped (live, the settle-counts-the-shot fallback
    # silently absorbed the miss).
    sim = scratch_prelude()
    sim.place(7, 60.0, 60.0)  # the hanger emerges from the jaws...
    sim.cue_id = 7  # ...white and unopposed, so the vote crowns it at once
    sim.velocity(7, 900.0, 900.0)  # ...clean, fully observed, smooth
    assert ShotTaken in map(type, sim.run(0.4))


def test_post_scratch_carry_back_is_not_a_shot() -> None:
    # scratch_replace's false-positive twin, contained by gates instead of
    # the removed categorical disarm: the fetched cue ball comes back inside
    # a hand, born as a fast newborn — but every carry observation is a
    # merged blob (and the real carry also fails the observation and
    # raw-step gates; any one of the three kills it).
    sim = scratch_prelude()
    sim.place(7, 800.0, 300.0)  # the fetch carry: hand+ball blob at speed
    sim.merge(7)
    sim.velocity(7, 900.0, 0.0)
    assert ShotTaken not in map(type, sim.run(0.4))


def test_parked_vote_strike_fires_from_own_rest() -> None:
    # parked_vote_strike_0801: the real cue rests merged against a cluster,
    # loses the whiteness vote to another still ball, then gets struck. The
    # flag-holder shows no impulse and the struck track is seconds old, so
    # neither the rest-anchored nor the newborn path can open — the ball's
    # own roster rest carries the anchor. The strike moment mirrors the
    # measured clip: the stick hides the ball through contact (its still
    # track coasts in place) and it emerges already displaced, at speed.
    sim = rack()
    sim.run(3.0)  # sustained quiet: both balls admitted to the roster
    sim.cue_id = OBJ  # the vote parks on the wrong ball
    sim.run(0.5)
    sim.occlude(CUE)  # stick covers the ball through contact; coast holds
    sim.run(0.05)
    sim.place(CUE, 560.0, 500.0)  # emerges 60 mm out at impulse speed
    sim.velocity(CUE, 600.0, 0.0)
    sim.reveal(CUE)
    assert ShotTaken in map(type, sim.run(0.5))


def test_settle_rearms_the_birth_paths_when_the_vote_is_parked() -> None:
    # Two consecutive wrong-flag strikes must both fire, with the whiteness
    # vote parked on a ball that never moves.
    #
    # stroke_gear_margin_0802 (warm) is why this exists: a birth-path fire
    # used to disarm the newborn and own-rest paths for one rest cycle, and
    # the disarm cleared only once the *flagged* cue's own translate stamp
    # advanced past it. Park the vote on a ball that never moves again and
    # the stamp never advances: the latch was permanent and both birth paths
    # stayed dead for the rest of the session. The live snapshot proved it —
    # captured 13 s after the table had settled (_table_moving False), it
    # still carried _newborn_disarmed_at = 478.295, the exact timestamp of the
    # journal's newborn shot, and the real strike 25 s later had no path left
    # to fire through. Checkpoint 51 re-armed the paths at the settle; #37
    # Part B deleted the disarm outright (removal note in
    # `_maybe_open_newborn`). The scenario stays pinned here on its OUTCOME
    # rather than on the mechanism — which is exactly what lets it survive the
    # machinery being removed.
    sim = rack()
    sim.run(3.0)  # sustained quiet: both balls admitted to the roster
    sim.cue_id = OBJ  # the vote parks on the wrong ball, and it stays parked
    sim.run(0.5)
    sim.occlude(CUE)
    sim.run(0.05)
    sim.place(CUE, 560.0, 500.0)  # first wrong-flag strike: arms the disarm
    sim.velocity(CUE, 600.0, 0.0)
    sim.reveal(CUE)
    assert ShotTaken in map(type, sim.run(0.5))
    sim.velocity(CUE, 0.0, 0.0)
    assert BallsSettled in map(type, sim.run(1.5))  # the episode closes
    sim.run(1.0)
    sim.occlude(CUE)  # a second, equally real wrong-flag strike
    sim.run(0.05)
    sim.place(CUE, 620.0, 500.0)
    sim.velocity(CUE, 600.0, 0.0)
    sim.reveal(CUE)
    assert ShotTaken in map(type, sim.run(0.5))


def test_parked_vote_hand_carry_does_not_fire() -> None:
    # The same configuration, but a hand lifts the rested ball instead: a
    # ball in a hand is inseparable from the hand, so every confirm
    # observation is a merged blob and the carried-ball gate kills it.
    sim = rack()
    sim.run(3.0)
    sim.cue_id = OBJ
    sim.run(0.5)
    sim.occlude(CUE)
    sim.run(0.05)
    sim.place(CUE, 560.0, 500.0)
    sim.velocity(CUE, 600.0, 0.0)
    sim.merge(CUE)
    sim.reveal(CUE)
    assert ShotTaken not in map(type, sim.run(0.5))


def test_parked_vote_hand_ramp_does_not_fire() -> None:
    # A hand pushing the rested ball up to speed ramps through the creep
    # window first — the translation trips before impulse speed is reached,
    # so the own-rest dwell is already broken when the scan looks.
    sim = rack()
    sim.run(3.0)
    sim.cue_id = OBJ
    sim.run(0.5)
    sim.velocity(CUE, 120.0, 0.0)  # sub-impulse push...
    sim.run(0.3)
    sim.velocity(CUE, 400.0, 0.0)  # ...accelerating to shot-like speed
    assert ShotTaken not in map(type, sim.run(0.5))


def test_address_chatter_does_not_close_own_rest_window() -> None:
    # address_chatter_miss_0801 (2026-08-02 02:51 UTC): the replacement cue
    # rested 13 s while the whiteness vote parked on another white-ish ball,
    # but stick-address chatter kept refreshing its translation stamp — at a
    # real 1396 mm/s impulse the own-rest dwell gate read ~0 rest and the
    # stroke went unseen (settle-counts-the-shot patched the count 11 s
    # later). Translation must ignore excursions shorter than the motion
    # window (chatter <= 64 ms; genuine motion >= 371 ms —
    # test_parked_vote_hand_ramp_does_not_fire is the sustained-motion
    # control that must keep failing to fire).
    sim = rack()
    sim.run(3.0)
    sim.cue_id = OBJ  # the vote parks on the wrong ball
    sim.run(0.5)
    for i in range(8):  # single-frame 8 mm mode flips through the address —
        # alternating, so each excursion CLOSES at the far mode (8 mm from
        # its base) exactly like the measured clip, not back at zero
        sim.displace(CUE, 8.0 if i % 2 == 0 else -8.0)
        sim.run(DT)
        sim.run(0.2)
    sim.occlude(CUE)  # stick hides the ball through contact, as measured
    sim.run(0.05)
    sim.place(CUE, 560.0, 500.0)
    sim.velocity(CUE, 600.0, 0.0)
    sim.reveal(CUE)
    assert ShotTaken in map(type, sim.run(0.5))


def test_occluded_push_does_not_open_own_rest() -> None:
    # tip_bump's 3.5 s event: the stick tip rides the cue out of sight for
    # 666 ms and the ball emerges at ~600 mm/s, 40+ mm out — kinematically a
    # parked-vote strike's twin (which hides its ball only ~26 ms through
    # contact). The own-rest dwell must be continuously witnessed: a dwell
    # window holding an unobserved run longer than the motion window is a
    # coasting memory, not a rest.
    sim = rack()
    sim.run(3.0)
    sim.cue_id = OBJ  # vote parked elsewhere, own-rest is the only path
    sim.run(0.5)
    sim.displace(CUE, 8.0)  # chatter breaks the translate stamp first, so
    sim.run(DT)  # the open must go through the witnessed-dwell fallback
    sim.occlude(CUE)
    sim.run(0.5)  # the push: ball rides under the stick, unseen
    sim.place(CUE, 560.0, 500.0)
    sim.velocity(CUE, 600.0, 0.0)
    sim.reveal(CUE)
    assert ShotTaken not in map(type, sim.run(0.5))


def test_bump_candidate_dies_at_rest_not_at_expiry() -> None:
    # A candidate that can no longer reach the confirm displacement in its
    # remaining window (displacement + raw * time_left) must die at that
    # moment, not linger to expiry where a stick-merge centroid jump could
    # fake the remaining distance and fire.
    #
    # The reachable target is the *slow* floor (22 mm) since the foul
    # reversal — before it, this test used a ~47 mm bump against the 60 mm
    # floor, which is now a legal fire (see the tip_bump test below). A
    # ~9 mm nudge is the equivalent case now: too small to confirm on its
    # own, and the later 20 mm jump must land on no candidate at all.
    sim = rack()
    sim.run(3.0)
    sim.cue_id = OBJ  # vote parked elsewhere: the bump opens via own-rest
    sim.run(0.5)
    sim.velocity(CUE, 600.0, 0.0)
    sim.run(0.015)  # ~9 mm of travel, then the ball stops short of 22 mm
    sim.velocity(CUE, 0.0, 0.0)
    sim.run(0.05)
    sim.displace(CUE, 20.0)  # stick-merge centroid jump inside the window
    assert ShotTaken not in map(type, sim.run(0.4))


def test_sub_impulse_tip_bump_fires() -> None:
    # THE FOUL REVERSAL (2026-08-02, design.md §8). A tip bump is a foul, a
    # foul ends the visit, and the incoming player gets a fresh clock — so it
    # must fire exactly like the soft shot it is indistinguishable from.
    # design.md §9 calls tip_bump "the hardest case in the corpus".
    #
    # Corpus measurement: the bumped ball peaks at 124 mm/s and covers
    # ~30 mm. Both are under the pre-reversal floors (250 mm/s, 60 mm), and
    # both of those were anchored on keeping this silent.
    sim = rack()
    sim.run(1.0)
    sim.velocity(CUE, 150.0, 0.0)  # under the old 250 impulse floor
    assert ShotTaken in map(type, sim.run(0.35))  # ~52 mm, confirmed slow


def test_practice_stroke_scale_excursion_still_rejected() -> None:
    # The other side of the same threshold move. The worst false provoker in
    # the corpus is a stick fragment covering 14.5 mm over the confirm window
    # (practice_strokes) — the slow floor sits at 22 mm, 1.5x above it.
    # A shot that never travels that far is not a shot.
    sim = rack()
    sim.run(1.0)
    sim.velocity(CUE, 150.0, 0.0)
    sim.run(0.09)  # ~13.5 mm, practice-stroke scale
    sim.velocity(CUE, 0.0, 0.0)
    assert ShotTaken not in map(type, sim.run(0.5))


def test_candidate_must_watch_the_ball_move() -> None:
    # triage_0801b (2026-08-02): a candidate opened 180.6 mm from the rest
    # anchor and fired 19 ms later having travelled 0.5 mm — the confirm
    # displacement is measured from the anchor, so it was satisfied entirely
    # by travel that happened before the candidate existed. It reported
    # 9434 mm/s (anchor displacement over the candidate's short life).
    #
    # Here the ball drifts well past the confirm floor while the vote is
    # parked elsewhere, then the vote returns and the ball stops. Any
    # candidate opening now inherits a large anchor displacement with no
    # motion of its own, and must not fire.
    sim = rack()
    sim.run(1.0)
    sim.velocity(CUE, 700.0, 0.0)
    sim.run(0.25)  # ~175 mm from the rest spot
    sim.velocity(CUE, 0.0, 0.0)
    assert ShotTaken not in map(type, sim.run(0.6))


def test_slow_newborn_is_not_a_strike() -> None:
    # cue_grab_0801: a gloved hand lifts the resting cue at 130 mm/s and,
    # once the impulse floor dropped for the sub-impulse class, confirmed as
    # a birth-path strike. The relaxation is justified by chain of custody —
    # the tracker watched that ball sit still and then move — and a newborn
    # track has none, so the birth path keeps the original 250 mm/s floor.
    # Every real birth-path strike in the corpus is fast (1294+ mm/s).
    sim = rack()
    sim.run(1.0)
    sim.occlude(CUE)  # no observed ball holds the vote
    sim.run(0.2)
    sim.place(7, 800.0, 300.0)
    sim.velocity(7, 150.0, 0.0)  # slow birth: carried, not struck
    assert ShotTaken not in map(type, sim.run(0.5))


def test_fast_newborn_strike_still_fires() -> None:
    # The positive control for the floor above: the occluded-address strike
    # the birth path exists for (measured 1572 mm/s) must be unaffected.
    sim = rack()
    sim.run(1.0)
    sim.occlude(CUE)
    sim.run(0.2)
    sim.place(7, 800.0, 300.0)
    sim.cue_id = 7  # crowned on emergence (see test_no_cue_newborn_strike_fires)
    sim.velocity(7, 1500.0, 0.0)
    assert ShotTaken in map(type, sim.run(0.4))


def test_newborn_confirm_cannot_beat_its_own_gates() -> None:
    # #8, pickup_snatch_0801: a bare-hand snatch confirmed on the fast path
    # in 0.070 s / 3 observations, with merged_frac exactly 0.00 — while
    # reading 71% merged across the whole carry. The carried-ball gates are
    # fractions over the confirm window, so a window that short makes them a
    # lottery. newborn_raw_step_ratio_max does catch this carry (ratio 2.24 at
    # age 0.096 s); the candidate just fired one frame before it could.
    #
    # Here the birth-path ball travels far enough to satisfy the fast confirm
    # immediately, then its raw speed lurches the way a carried ball's does.
    # The lurch must still be seen.
    sim = rack()
    sim.run(1.0)
    sim.occlude(CUE)
    sim.run(0.2)
    sim.place(7, 800.0, 300.0)
    sim.velocity(7, 1400.0, 0.0)
    sim.run(0.06)  # past 60 mm: the old fast confirm would have fired here
    sim.velocity(7, 200.0, 0.0)  # erratic, the way a hand is
    assert ShotTaken not in map(type, sim.run(0.5))


def test_the_confirm_delay_lands_only_on_the_birth_path() -> None:
    # The delay is justified by a newborn's missing chain of custody, so it
    # must not tax ordinary shots — which is nearly all of them. A
    # rest-anchored strike still fires inside the old fast-confirm window.
    sim = rack()
    sim.run(1.0)
    sim.velocity(CUE, 1400.0, 0.0)
    assert ShotTaken in map(type, sim.run(0.06))


def test_fired_lone_roller_gets_a_settle() -> None:
    # lone_roller_0801: a birth-path strike whose only mover is the fired
    # track itself (nothing else disturbed). The track is not roster, and a
    # streaking ball only becomes judgeable after it stops — so the episode
    # never latched and the settle never came (the live clock froze in
    # MOTION for 33 s). The fire itself must latch table motion.
    sim = rack()
    sim.run(1.0)
    sim.occlude(CUE)  # address occlusion: no observed ball holds the flag
    sim.run(0.2)
    sim.place(7, 800.0, 300.0)
    sim.cue_id = 7  # crowned on emergence (see test_no_cue_newborn_strike_fires)
    sim.velocity(7, 900.0, 0.0)
    assert ShotTaken in map(type, sim.run(0.4))
    sim.velocity(7, 0.0, 0.0)
    assert BallsSettled in map(type, sim.run(1.2))


def test_pickup_discard_never_runs_in_a_fired_episode() -> None:
    # pair_strikes_0801 lost its first settle this way: the struck ball's
    # original track died mid-streak while its continuation was untracked
    # for a beat, so "every latcher died off-pocket under hand-drag travel"
    # held and the discard silently unlatched a live shot. Once a ShotTaken
    # has fired, the quiet that follows IS the shot settling and the clock
    # needs the BallsSettled.
    sim = rack()
    sim.run(1.0)
    sim.velocity(CUE, 900.0, 0.0)
    assert ShotTaken in map(type, sim.run(0.2))
    sim.remove(CUE)  # terminal streak death mid-roll, continuation unseen
    assert BallsSettled in map(type, sim.run(1.5))


def test_scatter_gap_break_still_settles() -> None:
    # break_0801b chain: the break fires, then every track dies in streak
    # churn and a frame or two report zero balls — the pickup guards are
    # vacuously true on an empty frame, and the discard unlatched the live
    # break (a scatter rebirth then fired into the "quiet" table). A fired
    # episode is never a pickup, and an empty frame is never judged one:
    # the break must resolve with a real settle once balls are re-found.
    sim = rack()
    sim.run(1.0)
    sim.velocity(CUE, 900.0, 0.0)
    assert ShotTaken in map(type, sim.run(0.15))  # ~135 mm before the churn
    sim.remove(CUE)
    sim.remove(OBJ)  # whole-frame detection gap: nothing reported at all
    sim.run(0.7)
    sim.place(OBJ, 1200.0, 500.0)  # the gap passes; a ball is re-found still
    assert BallsSettled in map(type, sim.run(1.5))


def test_hand_grab_of_the_resting_cue_is_not_a_shot() -> None:
    # cue_grab_0801: a gloved hand reaches in — a merged, impulse-fast,
    # non-roster blob — grabs the resting flag-holder and yanks it away.
    # Dwell, step, axis and travel all pass (a snatch is impulsive and a
    # drag coherent), and the drag observations are not merged enough for
    # the carried-ball gate. The hand's own approach is the evidence: a
    # merged fast mover reaching kiss range of the rest is a bringer.
    sim = rack()
    sim.run(1.0)
    sim.place(7, 700.0, 480.0)  # the hand blob sweeps toward the cue...
    sim.merge(7)
    sim.velocity(7, -900.0, 0.0)  # ...merged and fast, born mid-play
    sim.run(0.15)  # ends within kiss range of the cue's rest
    sim.velocity(7, 0.0, 0.0)
    sim.velocity(CUE, 1200.0, 0.0)  # the grab: an impulse-fast coherent yank
    assert ShotTaken not in map(type, sim.run(0.4))


def test_phantom_flag_handoff_strike_fires() -> None:
    # phantom_flag_strike_0805: the addressed cue's own track freezes at its
    # rest spot and keeps the whiteness vote while the ball is struck and
    # gone. Three mechanisms compose — the tracker holds a still track in
    # place through merged blobs and coasts it for still_coast_s (3 s), the
    # struck ball re-emerges beyond the 100 mm association gate and so founds
    # a NEW track, and the frozen phantom's colour is never resampled so its
    # stale whiteness outranks the live one.
    #
    # Measured on the clip: the cue was last detected at its rest spot
    # (1529, 872) at 10.030 s, went undetected entirely for 0.173 s while it
    # travelled 171 mm, and re-emerged at 10.228 s as a merged blob 193 mm
    # away at 2281 mm/s — flag still on the phantom for 2.8 s more. Every
    # gate of the flag-borne handoff already passed at that frame (dwell
    # 9.72 s, still-stamp gap 0.013 s, quiet table, no mover within
    # carom_reach_mm of the rest); the strike was missed only because the flag
    # was on the wrong track. Live the clock ran down through the shot and
    # Steven took an extension; cold replay produced ZERO events over 20 s.
    #
    # The emergence is merged on nearly every frame — a ball struck out of an
    # address is inseparable from the stick that struck it — so the birth path
    # correctly reads it as a carry and cannot be the fix. Chain of custody
    # from the cue's own rest is what makes the merge admissible.
    sim = rack()
    sim.run(3.0)
    sim.occlude(CUE)  # phantom: frozen at the rest spot, still holding the flag
    sim.run(0.2)
    sim.place(REBORN, 693.0, 500.0)  # re-emerges 193 mm out, already at speed
    sim.merge(REBORN)  # struck out of an address: merged with the stick
    sim.velocity(REBORN, 900.0, 0.0)
    assert ShotTaken in map(type, sim.run(0.5))


def test_phantom_flag_handoff_still_respects_the_hand_bringer() -> None:
    # The mirror of the fire above, and the hole the first cut of this path
    # had: the player's own hand/stick blob is born the moment the cue goes
    # dark, sweeps along the stroke line, and ends inside the handoff radius —
    # so it passes the birth-order test, the impulse floor, the dwell and a
    # near-perfect axis dot, and it cannot be vetoed by the carom guard the way
    # other movers are, because here the bringer IS the candidate and the guard
    # skips a candidate's own stamps. It fired a phantom shot on the hand.
    #
    # What separates them is that a struck ball *leaves* the rest spot while a
    # bringer arrives at it: distance from the anchor grows on the real strike
    # (193 -> 221 mm) and shrinks here. Pinned on the same geometry as the fire
    # above so it lands on that discriminator and not a side effect.
    sim = rack()
    sim.run(3.0)
    sim.occlude(CUE)
    sim.place(7, 700.0, 480.0)  # hand blob: merged, fast, non-roster...
    sim.merge(7)
    sim.velocity(7, -900.0, 0.0)
    sim.run(0.15)  # ...ending inside kiss range of the cue's rest
    sim.velocity(7, 0.0, 0.0)
    sim.place(REBORN, 620.0, 500.0)  # the "ball" leaves toward the hand
    sim.merge(REBORN)
    sim.velocity(REBORN, 900.0, 0.0)
    assert ShotTaken not in map(type, sim.run(0.5))


def test_phantom_flag_handoff_does_not_fire_mid_scatter() -> None:
    # The latch guard the other two scan paths already carry. Mid-episode a
    # roster ball's streak dies and its continuation is reborn near whatever
    # the cue's stale rest happens to be; that is a scatter rebirth, not a
    # strike (break_0801b fired one as a newborn before the same guard went
    # onto the birth path). Table motion is latched here without a fire, so
    # the rest memory survives and _table_moving is the only thing refusing.
    sim = rack()
    sim.run(3.0)
    sim.velocity(OBJ, 600.0, 0.0)  # latch table motion, far from the cue rest
    sim.run(0.5)
    sim.occlude(CUE)
    sim.run(0.05)
    sim.place(REBORN, 693.0, 500.0)
    sim.merge(REBORN)
    sim.velocity(REBORN, 900.0, 0.0)
    assert ShotTaken not in map(type, sim.run(0.5))


def test_break_outruns_the_handoff_radius_and_still_fires() -> None:
    # break_outrun_0805: the radius is a *distance* budget, and a ball struck
    # hard enough spends the whole budget while it is invisible. Measured on
    # the live break — the cue left its rest at 4.724 s at 3720 mm/s, was not
    # observed on a single frame after that, and the track that carried it
    # onward was born three frames later 320 mm out at 7221 mm/s. Every other
    # handoff gate passed at that frame (dwell 4.67 s, born after the cue went
    # dark, quiet table, no mover near the rest); the strike was refused by
    # 70 mm of geometry anchored on a 2281 mm/s emergence. Live the clock sat
    # in IDLE through the whole rack and Steven had to press Restart.
    #
    # Inside the occlusion the honest bound is what the ball's own speed can
    # account for, so that becomes the reach.
    sim = rack()
    sim.run(3.0)
    sim.occlude(CUE)  # struck and gone: never observed again
    sim.run(3 * DT)  # dark for three frames, as measured
    sim.place(REBORN, 820.0, 500.0)  # 320 mm out — past the 250 mm radius
    sim.merge(REBORN)  # struck out of an address, inseparable from the stick
    sim.velocity(REBORN, 7200.0, 0.0)
    assert ShotTaken in types(sim.run(0.3))


def test_slow_emergence_beyond_the_radius_is_still_refused() -> None:
    # The mirror, on the discriminator itself: identical geometry, identical
    # occlusion, identical merge — only the speed differs. At 3000 mm/s the
    # ball cannot have covered 320 mm in the three frames it was dark, so the
    # rest no longer vouches for it and the radius stands. Without this the
    # reach would just be a bigger constant.
    sim = rack()
    sim.run(3.0)
    sim.occlude(CUE)
    sim.run(3 * DT)
    sim.place(REBORN, 820.0, 500.0)
    sim.merge(REBORN)
    sim.velocity(REBORN, 3000.0, 0.0)
    assert ShotTaken not in types(sim.run(0.3))


def test_break_burying_itself_in_the_rack_still_fires() -> None:
    # The second half of break_outrun_0805, and the reason widening the reach
    # alone changed nothing: the candidate opened, travelled 1483 mm, and was
    # then killed by the handoff observation floor at exactly
    # handoff_confirm_min_s. That floor asks "is a hand losing this ball?",
    # and on a break the answer is a false yes — by 0.20 s the cue has reached
    # the rack and the tracker loses it inside the scatter it caused. Measured
    # at the gate frame: 0.56 observed, *below* cue_grab_0801's 0.64 and
    # pickup_newborn_0801's 0.69, so the fraction cannot be loosened.
    #
    # Speed is what separates: nothing that must stay rejected reads above
    # 2968 mm/s and no real break reads below 7371. Held inside the plain
    # radius here so the reach is not the variable under test.
    sim = rack()
    sim.run(3.0)
    sim.occlude(CUE)
    sim.run(0.2)
    sim.place(REBORN, 600.0, 500.0)  # emerges inside the plain 250 mm radius
    sim.merge(REBORN)
    sim.velocity(REBORN, 6000.0, 0.0)
    sim.run(0.09)  # clean cloth, as far as the rack
    sim.flicker(REBORN)  # then buried: observed a third of the time
    assert ShotTaken in types(sim.run(0.25))


def test_ordinary_speed_emergence_that_keeps_vanishing_does_not_fire() -> None:
    # The mirror. Same rest, same radius, same merge, same flicker — an
    # ordinary 900 mm/s emergence that keeps disappearing is the hand the
    # floor was built for (cue_grab_0801, pickup_newborn_0801), and it must
    # still be refused. Only the speed is exempt, not the flicker.
    sim = rack()
    sim.run(3.0)
    sim.occlude(CUE)
    sim.run(0.2)
    sim.place(REBORN, 600.0, 500.0)
    sim.merge(REBORN)
    sim.velocity(REBORN, 900.0, 0.0)
    sim.run(0.09)
    sim.flicker(REBORN)
    assert ShotTaken not in types(sim.run(0.25))


def test_stroke_gear_behind_the_departing_cue_does_not_reject_strike() -> None:
    # stroke_gear_veto_0802 / stroke_gear_margin_0802: the stroke's own
    # merged stick/bridge blob stamps *inside* carom_reach_mm — measured
    # 138 mm and 152 mm from the cue's rest spot, against the 271-340 mm
    # the v2 guard was anchored on — and killed both real shots live (no
    # ShotTaken; the clock counted down through the stroke). Distance
    # cannot separate it from cue_grab_0801's grab at 116 mm, so the
    # guard reads direction for hand stamps: a stroke drives the cue away
    # from the gear behind it (148-162 deg measured), a grab drags it
    # toward the hand (61 deg). Deliberately the same blob, the same kiss
    # range and the same impulse as the grab test above — only the
    # departure direction differs, so this pins the discriminator itself.
    sim = rack()
    sim.run(1.0)
    sim.place(7, 250.0, 500.0)  # stroke gear approaching from behind...
    sim.merge(7)
    sim.velocity(7, 900.0, 0.0)  # ...merged and fast, sweeping in to contact
    sim.run(0.1)  # ends ~160 mm behind the cue: inside kiss range
    sim.velocity(7, 0.0, 0.0)
    sim.velocity(CUE, 1200.0, 0.0)  # the cue departs AWAY from the gear
    assert ShotTaken in map(type, sim.run(0.4))


def test_hand_stamps_do_not_guard_the_birth_paths() -> None:
    # lone_roller_0801's near-miss regression: the stroke's own merged
    # stick/arm blob stamps 66-72 mm from the birth spot moments before
    # impact — birth-evidence strikes happen exactly where the player is
    # reaching, so hand stamps must guard only the rest-anchored cue path.
    sim = rack()
    sim.run(1.0)
    sim.occlude(CUE)  # address occlusion: the flag goes quiet
    sim.place(7, 820.0, 320.0)  # stroke blob: merged, fast, non-roster
    sim.merge(7)
    sim.velocity(7, 400.0, 0.0)
    sim.run(0.1)
    sim.remove(7)
    sim.run(0.2)  # any candidate the blob itself opened dies (merged gate);
    # its hand stamps are still inside the guard window when the ball births
    sim.place(8, 800.0, 300.0)  # the struck ball emerges at speed
    sim.cue_id = 8  # crowned on emergence (see test_no_cue_newborn_strike_fires)
    sim.velocity(8, 900.0, 0.0)
    assert ShotTaken in map(type, sim.run(0.4))


def test_reported_speed_carries_confirm_kinematics() -> None:
    # break_underread_0801: the struck ball can streak too fast to be
    # *observed*, so the fastest-observed sample alone underreads badly (a
    # real break read 705 mm/s and the Hub's IDLE gate held the clock). The
    # confirm's own displacement over its life is a speed the gates already
    # proved, and no unobserved streak can dodge it.
    engine = EventEngine()
    t, x = 0.0, 500.0
    for _ in range(80):  # a 1 s dwell at rest
        t += DT
        engine.update(t, [BallState(0, x, 500.0, 0.0, is_cue=True, raw_speed_mms=0.0)])
    for _ in range(8):  # ~62 mm/frame ≈ 5000 mm/s, raw samples underreading
        t += DT
        x += 62.0
        events = engine.update(t, [BallState(0, x, 500.0, 300.0, is_cue=True, raw_speed_mms=300.0)])
        shots = [e for e in events if isinstance(e, ShotTaken)]
        if shots:
            assert shots[0].speed_mms is not None and shots[0].speed_mms >= 3000
            return
    raise AssertionError("strike never fired")


def test_coasting_phantom_candidate_releases_the_slot_to_the_handoff() -> None:
    # flag_missed / flag_missed_3, checkpoint 66. The flag-borne open is right
    # at the impulse frame and then the tracker loses the ball: the struck cue
    # outruns the association gate and founds a new track, while the candidate
    # advances on extrapolated positions that can never reach the confirm. The
    # single candidate slot is exactly what the handoff needs, and it needs it
    # now — the struck ball is only inside strike_handoff_radius_mm for a
    # moment. Live, the slot stayed shut until the candidate died and the real
    # ball was 469 mm out (flag_missed) / 292 mm out (flag_missed_3).
    # The phantom must *ride to expiry*, which is the live shape: it has to
    # keep enough apparent speed that the feasibility check cannot kill it,
    # while never travelling the 22 mm the slow confirm wants. A jitter in
    # place does both — exactly what flag_missed_3's abandoned track did,
    # drifting (675,88) -> (651,74) at a stuck raw of 119 mm/s for the whole
    # 0.30 s window. Let it creep in a straight line instead and it confirms
    # on its own at 0.25 s, and the test stops testing anything.
    sim = rack()
    sim.run(1.0)  # dwell: the rest memory earns its credit
    sim.velocity(CUE, 1200.0, 0.0)
    sim.run(DT)  # one frame at impulse speed: the rest candidate opens
    sim.occlude(CUE)  # the tracker loses the ball; the candidate rides a coast
    sim.place(REBORN, 660.0, 500.0)  # the struck cue surfaces inside the radius
    sim.velocity(REBORN, 2500.0, 0.0)
    events: list[Event] = []
    for _ in range(40):  # 0.5 s of the phantom jittering in place
        sim.velocity(CUE, 0.0, 150.0)
        events += sim.run(DT)
        sim.velocity(CUE, 0.0, -150.0)
        events += sim.run(DT)
    assert ShotTaken in types(events)


def test_parked_vote_newborn_confirms_on_travel() -> None:
    # flag_missed_2, checkpoint 66. The vote parks on an *observed, still* ball
    # — the object ball the cue is about to pot — so `cue_quiet` is False and
    # the birth path used to be skipped outright, while _maybe_open_rested
    # cannot help (a newborn is not in the roster). With the vote corroborating
    # nothing, the identity comes from the ball's own travel: measured 962 mm
    # on the real strike. See parked_confirm_travel_mm.
    sim = rack()
    sim.cue_id = OBJ  # the vote parks on a ball that stays observed and still
    sim.run(1.0)
    sim.remove(CUE)  # the addressed cue's own track dies under the occlusion
    sim.run(0.5)
    sim.place(REBORN, 300.0, 500.0)  # it re-emerges at speed, out of nowhere
    sim.velocity(REBORN, 2000.0, 0.0)
    assert ShotTaken in types(sim.run(0.5))  # 600 mm at 2000 mm/s = 0.30 s


def test_parked_vote_newborn_rejects_a_short_traveller() -> None:
    # The mirror of the test above, and the reason the gate is travel rather
    # than speed: everything this configuration falsely admitted corpus-wide
    # moved 202-382 mm and stopped (a hand placing a ball, a stroke blob, a
    # rebirth in streak churn) — including scratch_replace's 3179 mm/s throw,
    # which is *faster* than the real strike's 1927. Deliberately the same
    # setup, the same impulse and the same birth conjunction as above; only
    # the distance travelled differs, so this pins the discriminator itself.
    sim = rack()
    sim.cue_id = OBJ
    sim.run(1.0)
    sim.remove(CUE)
    sim.run(0.5)
    sim.place(REBORN, 300.0, 500.0)
    sim.velocity(REBORN, 2000.0, 0.0)
    sim.run(0.15)  # ~300 mm — the false class's whole budget
    sim.velocity(REBORN, 0.0, 0.0)
    assert ShotTaken not in types(sim.run(0.6))


def test_event_config_at_cadence_keeps_debounce_time() -> None:
    from cueclock.vision.events import TUNED_HZ, EventConfig

    cfg = EventConfig()
    assert cfg.at_cadence(TUNED_HZ) is cfg
    full = cfg.at_cadence(2 * TUNED_HZ)
    assert full.cue_id_debounce_frames == 2 * cfg.cue_id_debounce_frames
