"""#67: a cue ball moved by hand counts no shot, in open play as after a
scratch (docs/design.md §21). The settle's evidence is how long the cue ball
was seen rolling (`vision/roll.py`); under 0.1 s the settle waits for its
list, and with no object ball missing from the list before, it was a hand."""

import asyncio

from cueclock.clock.state import State
from cueclock.server.hub import SETTLE_LIST_WAIT_S
from cueclock.server.scratch import SettleFacts, moved_by_hand, object_missing, only_cue_changed
from cueclock.vision.ball_list import BallList
from cueclock.vision.events import SettleMover
from tests.unit.test_ball_in_hand import (
    AFTER,
    PLACED,
    PLACED_AGAIN,
    SHOT,
    _hub,
    _kinds,
    _move,
    _row,
    _scratch,
)
from tests.unit.test_scratch_list import FACTS, GONE_STILL, REST, _list

HAND = SettleFacts(**{**FACTS.__dict__, "cue_roll_s": 0.0})  # a hand: never seen rolling
CARRIED = _list(("cue", 160.0), ("3", 400.0), ("8", 700.0))  # REST's cue, moved
WITH_GLOVE = _list(("cue", 160.0), ("3", 400.0), ("8", 700.0), ("10", 900.0))  # a hand as a 10


# ---------- the rule ----------


def test_the_object_question_is_asked_one_way() -> None:
    assert not object_missing(REST, CARRIED)
    assert not object_missing(REST, WITH_GLOVE)  # a new circle is not asked about
    assert object_missing(REST, _list(("cue", 160.0), ("8", 700.0)))  # the 3 gone
    assert object_missing(REST, _list(("cue", 160.0), ("3", 409.0), ("8", 700.0)))  # moved


def test_moved_by_hand_each_guard_on_and_off() -> None:
    assert moved_by_hand(CARRIED, REST, HAND)
    assert moved_by_hand(GONE_STILL, REST, HAND)  # lifted off the table
    assert moved_by_hand(WITH_GLOVE, REST, HAND)
    assert not moved_by_hand(CARRIED, REST, FACTS)  # no evidence counts as rolling
    rolled = SettleFacts(**{**HAND.__dict__, "cue_roll_s": 0.19})  # tip_bump
    assert not moved_by_hand(CARRIED, REST, rolled)
    assert moved_by_hand(CARRIED, REST, SettleFacts(**{**HAND.__dict__, "cue_roll_s": 0.099}))
    assert not moved_by_hand(CARRIED, None, HAND)  # nothing to compare with
    assert not moved_by_hand(SHOT, REST, HAND)  # an object ball moved


def test_a_carry_during_ball_in_hand_stays_placing_however_far() -> None:
    far = SettleFacts(**{**HAND.__dict__, "travel_mm": 1200.0})
    assert only_cue_changed(PLACED, AFTER, far)
    rolled = SettleFacts(**{**far.__dict__, "cue_roll_s": 0.6})
    assert not only_cue_changed(PLACED, AFTER, rolled)  # rolled 400 mm or more: the shot
    short = SettleFacts(**{**rolled.__dict__, "travel_mm": 200.0})
    assert only_cue_changed(PLACED, AFTER, short)  # a short roll or tap stays placing


# ---------- open play, through the hub ----------


def test_a_hand_move_in_open_play_keeps_the_countdown_running() -> None:
    hub, clock = _hub()
    clock.now += 5.0
    hub.tick()
    _move(hub, clock, 5.0, CARRIED, roll=0.0)
    settle = _kinds(hub, "vision_balls_settled")[-1]
    assert settle["held"] == "list" and settle["rejected"] == "by_hand"
    assert settle["cue_roll_s"] == 0.0
    row = _row(hub)
    assert row["row"] == "by_hand" and row["hand"] == "applied" and row["counted"] is False
    (revert,) = _kinds(hub, "shot_provisional_reverted")
    assert revert["reason"] == "by_hand"
    assert hub.clock.rack_shots == 0 and hub.clock.state is State.RUNNING
    hub.tick()
    assert hub.clock.remaining < 26.0  # resumed, not restarted
    assert not _kinds(hub, "ball_in_hand")  # no ball in hand state, no hold


def test_the_frozen_countdown_resumes_at_what_it_froze_with() -> None:
    hub, clock = _hub()
    clock.now += 10.0
    hub.tick()
    clock.now += 1.0
    asyncio.run(hub.episode_started(5.0))
    frozen = hub.clock.remaining
    clock.now += 1.0
    asyncio.run(hub.balls_settled(6.0, movers=[], cue_roll_s=0.006))
    assert hub.clock.state is State.MOTION  # held: the countdown stays frozen
    clock.now += 0.07
    asyncio.run(hub.ball_list(6.0, "settle", CARRIED))
    assert hub.clock.state is State.RUNNING and abs(hub.clock.remaining - frozen) < 1e-6


def test_a_new_circle_on_the_settle_s_list_is_not_asked_about() -> None:
    hub, clock = _hub()
    _move(hub, clock, 5.0, WITH_GLOVE, roll=0.0)
    assert _row(hub)["row"] == "by_hand" and hub.clock.rack_shots == 0


def test_an_object_ball_missing_counts() -> None:
    hub, clock = _hub()
    _move(hub, clock, 5.0, SHOT, roll=0.0)
    assert _row(hub)["hand"] is None and hub.clock.rack_shots == 1


def test_seen_rolling_counts_and_is_not_held() -> None:
    """A stick shot that touched nothing: counted at once, row cue_moved."""
    hub, clock = _hub()
    clock.now += 1.0
    asyncio.run(hub.episode_started(5.0))
    clock.now += 1.0
    asyncio.run(hub.balls_settled(6.0, movers=[], cue_roll_s=0.19))
    assert hub.clock.rack_shots == 1  # decided at the settle, not held
    asyncio.run(hub.ball_list(6.0, "settle", CARRIED))
    assert _row(hub)["row"] == "cue_moved" and _row(hub)["hand"] is None


def test_no_evidence_counts_and_is_not_held() -> None:
    hub, clock = _hub()
    _move(hub, clock, 5.0, CARRIED, roll=None)
    assert "held" not in _kinds(hub, "vision_balls_settled")[-1]
    assert _row(hub)["row"] == "cue_moved" and hub.clock.rack_shots == 1


def test_the_backstop_settle_is_never_held() -> None:
    hub, clock = _hub()
    clock.now += 1.0
    asyncio.run(hub.episode_started(5.0))
    clock.now += 1.0
    asyncio.run(hub.balls_settled(6.0, backstop=True, movers=[], cue_roll_s=0.0))
    assert "held" not in _kinds(hub, "vision_balls_settled")[-1]


def test_no_list_before_the_motion_counts_as_today() -> None:
    hub, clock = _hub()
    hub._rest_lists = type(hub._rest_lists)()  # nothing read before the motion
    _move(hub, clock, 5.0, CARRIED, roll=0.0)
    assert "held" not in _kinds(hub, "vision_balls_settled")[-1]
    assert hub.clock.rack_shots == 1


def test_no_list_in_time_counts_as_today() -> None:
    hub, clock = _hub()
    _move(hub, clock, 5.0, None, roll=0.0)
    assert hub.clock.state is State.MOTION  # waiting for the list
    clock.now += SETTLE_LIST_WAIT_S + 0.01
    hub.tick()
    assert _kinds(hub, "vision_balls_settled")[-1]["held"] == "timeout"
    assert hub.clock.rack_shots == 1
    asyncio.run(hub.ball_list(6.0, "settle", CARRIED))  # the late list
    assert _row(hub)["row"] == "by_hand" and _row(hub)["hand"] == "late"


def test_a_candidate_first_decides_it_as_today() -> None:
    hub, clock = _hub()
    clock.now += 1.0
    asyncio.run(hub.episode_started(5.0))
    clock.now += 1.0
    asyncio.run(hub.balls_settled(6.0, movers=[], cue_roll_s=0.0))
    asyncio.run(
        hub.scratch_candidate(
            6.1,
            x_mm=60.0,
            y_mm=80.0,
            pocket_x_mm=0.0,
            pocket_y_mm=0.0,
            whiteness=0.5,
            color_age_s=0.0,
        )
    )
    assert _kinds(hub, "vision_balls_settled")[-1]["held"] == "candidate"
    assert hub.clock.rack_shots == 1


def test_a_compensation_in_running_is_suppressed() -> None:
    """A settle with no freeze open (no latch before it): nothing to revert,
    the count is refused and journaled as a suppressed compensation."""
    hub, clock = _hub()
    clock.now += 1.0
    asyncio.run(hub.balls_settled(6.0, movers=[], cue_roll_s=0.0))
    assert hub.clock.state is State.RUNNING
    clock.now += 0.05
    asyncio.run(hub.ball_list(6.0, "settle", CARRIED))
    (refused,) = _kinds(hub, "compensation_suppressed")
    assert refused["reason"] == "by_hand" and hub.clock.rack_shots == 0


def test_a_hand_move_in_grace_leaves_the_buzzer_alone() -> None:
    hub, clock = _hub()
    clock.now += 40.0
    hub.tick()
    assert hub.clock.state is State.GRACE
    _move(hub, clock, 50.0, CARRIED, roll=0.0)
    assert hub.clock.state is State.GRACE and _row(hub)["hand"] == "applied"


def test_a_hand_move_the_clock_refuses_still_counts() -> None:
    hub, clock = _hub()
    clock.now += 40.0
    hub.tick()
    asyncio.run(hub.shot_taken(49.0, ball_id=1, speed_mms=900.0))  # a GRACE fire commits
    _move(hub, clock, 50.0, CARRIED, roll=0.0)
    assert _row(hub)["hand"] == "refused"
    assert _kinds(hub, "vision_balls_settled")[-1]["hand_refused"] == "by_hand"


def test_the_cue_on_its_spot_is_still_nothing_moved() -> None:
    """The older refusal is asked first: its reason and row stand."""
    hub, clock = _hub()
    _move(hub, clock, 5.0, REST, roll=0.0)
    assert _kinds(hub, "vision_balls_settled")[-1]["rejected"] == "nothing_moved"
    assert _row(hub)["row"] == "still" and _row(hub)["hand"] is None


def test_idle_is_never_held() -> None:
    hub, clock = _hub()
    asyncio.run(hub.new_rack())
    assert hub.clock.state is State.IDLE
    _move(hub, clock, 5.0, CARRIED, roll=0.0)
    assert "held" not in _kinds(hub, "vision_balls_settled")[-1]
    assert _row(hub)["hand"] == "not_held"


# ---------- ball in hand after a scratch ----------


def test_a_long_carry_during_ball_in_hand_is_placing() -> None:
    """Two live placings read 372 and 374 mm; a carry past 400 mm that was
    never seen rolling stays placing (it counted before)."""
    hub, clock = _hub()
    _scratch(hub, clock)
    _move(hub, clock, 8.0, PLACED, travel=650.0, roll=0.0)
    assert _row(hub)["row"] == "ball_in_hand" and _row(hub)["hand"] == "applied"
    assert hub.clock.rack_shots == 1


def test_a_long_roll_during_ball_in_hand_is_the_shot() -> None:
    hub, clock = _hub()
    _scratch(hub, clock)
    _move(hub, clock, 8.0, PLACED_AGAIN, travel=650.0, roll=0.8)
    assert hub.clock.rack_shots == 2
    assert _kinds(hub, "ball_in_hand_ended")[0]["reason"] == "shot"


def test_a_placing_counts_once_in_placed() -> None:
    """A placing is ball in hand's own refusal, not also a hand move."""
    hub, clock = _hub()
    _scratch(hub, clock)
    _move(hub, clock, 8.0, PLACED, roll=0.0)
    _move(hub, clock, 20.0, SHOT, travel=1500.0)
    (ended,) = _kinds(hub, "ball_in_hand_ended")
    assert ended["placed"] == 1


def test_an_arm_list_hiding_the_cue_is_still_a_hand_move() -> None:
    hub, clock = _hub()
    _move(hub, clock, 5.0, BallList(balls=GONE_STILL.balls, arm=True), roll=0.0)
    assert _row(hub)["row"] == "by_hand" and hub.clock.rack_shots == 0


def test_the_evidence_is_on_every_settle_record() -> None:
    hub, clock = _hub()
    clock.now += 1.0
    asyncio.run(hub.balls_settled(6.0, movers=[SettleMover(travel_mm=40.0, alive=False)]))
    assert _kinds(hub, "vision_balls_settled")[-1]["cue_roll_s"] is None
    asyncio.run(hub.balls_settled(9.0, movers=[], cue_roll_s=1.23456))
    assert _kinds(hub, "vision_balls_settled")[-1]["cue_roll_s"] == 1.235
