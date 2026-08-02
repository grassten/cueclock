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
        self.cue_id = CUE  # whiteness-vote holder; tests park it elsewhere
        self.occluded: set[int] = set()
        self.flickering: set[int] = set()
        self.merged_ids: set[int] = set()
        self.churn: dict[int, float] = {}
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
    assert types(sim.run(0.8)) == [Scratch, BallsSettled]
    sim.run(1.0)  # table quiet; no cue-flagged track exists
    return sim


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
