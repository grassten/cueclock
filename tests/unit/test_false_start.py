"""§17: a strike out of an armed rack is the break only if the rack moved.

Before a break the cue ball gets set down, rolled out of a hand, picked up and
moved, all in view of the camera, and the tracker reads some of that as a
strike at break speed (6,520 mm/s in session 20261119-020755; 7,104 mm/s in
the clip filed for three months as `break_0801b`). Out of IDLE any strike over
the floor was the break, so the clock started before the break did. The
settle's ball list knows better: the rack is standing where it stood.
"""

import asyncio

from cueclock.clock import get_ruleset
from cueclock.clock.state import ShotClock, State
from cueclock.server.hub import SETTLE_LIST_WAIT_S, Hub
from cueclock.server.scratch import RACK_MIN_BALLS, RestLists, holds_rack, rack_stands
from cueclock.vision.ball_list import BallList
from tests.unit.test_scratch_list import _ball

RACK_XS = [1000.0 + 40.0 * n for n in range(9)]


def _table(
    cue: float | None, *extra: tuple[str, float], shift: float = 0.0, arm: bool = False
) -> BallList:
    """A nine-ball rack (numbers 1-9), the cue where given, anything extra."""
    balls = [_ball(str(n + 1), x + shift) for n, x in enumerate(RACK_XS)]
    if cue is not None:
        balls.append(_ball("cue", cue))
    balls += [_ball(label, x) for label, x in extra]
    return BallList(balls=tuple(balls), arm=arm)


# ---------- the clock ----------


def test_the_break_can_be_taken_back_and_nothing_else() -> None:
    clock = ShotClock(get_ruleset("wnt_9ball"), now=0.0)
    clock.new_rack(0.0)
    assert not clock.break_reverted(1.0)  # nothing fired
    clock.shot_taken()
    assert (clock.state, clock.rack_shots) == (State.MOTION, 1)
    assert clock.break_reverted(2.0)
    assert (clock.state, clock.rack_shots, clock.remaining) == (State.IDLE, 0, 60.0)

    clock.shot_taken()  # the real break
    assert clock.balls_settled(5.0)
    assert not clock.break_reverted(6.0)  # settled: it stands
    clock.shot_taken()
    assert not clock.break_reverted(7.0)  # a later shot is not the break
    assert clock.rack_shots == 1 and clock.state is State.MOTION


# ---------- the rule ----------


def test_the_rack_stands_when_every_ball_of_it_is_read_in_place() -> None:
    before = _table(200.0, arm=True)  # a hand in the frame: the rack is read all the same
    assert holds_rack(before) and len(RACK_XS) == RACK_MIN_BALLS
    assert rack_stands(_table(310.0), before)  # the cue set down somewhere else
    assert rack_stands(_table(None, ("14", 330.0), arm=True), before)  # a hand on it, read as a 14
    assert rack_stands(_table(200.0, shift=1.4), before)  # jitter
    assert not rack_stands(_table(200.0, shift=9.0), before)  # the rack moved
    broken = BallList(balls=_table(200.0).balls[1:], arm=False)  # one ball gone
    assert not rack_stands(broken, before)
    assert not rack_stands(_table(200.0), None)
    eight = BallList(balls=_table(200.0).balls[1:], arm=False)
    assert not holds_rack(eight) and not rack_stands(eight, eight)  # too few to be a rack


def test_the_list_before_a_strike_may_have_an_arm_in_it() -> None:
    lists = RestLists()
    lists.add(1.0, _table(200.0))
    lists.add(2.0, _table(200.0, arm=True))
    assert lists.last_before(3.0) == _table(200.0, arm=True)
    assert lists.last_before(1.5) == _table(200.0)
    assert lists.last_before(0.5) is None


# ---------- the hub ----------


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _armed() -> tuple[Hub, _Clock]:
    clock = _Clock()
    hub = Hub(now=clock)
    asyncio.run(hub.new_rack())
    asyncio.run(hub.ball_list(1.0, "rest", _table(200.0, arm=True)))
    return hub, clock


def _kinds(hub: Hub, kind: str) -> list[dict[str, object]]:
    return [r for r in hub.journal.tail(10_000.0) if r["kind"] == kind]


def _fire_and_settle(hub: Hub, clock: _Clock, found: BallList | None) -> None:
    clock.now += 5.0
    asyncio.run(hub.shot_taken(5.0, ball_id=9, speed_mms=6520.0))
    assert (hub.clock.state, hub.clock.rack_shots) == (State.MOTION, 1)
    clock.now += 0.5
    asyncio.run(hub.balls_settled(5.5, tracker_cue=True))
    if found is not None:
        clock.now += 0.15
        asyncio.run(hub.ball_list(5.5, "settle", found))


def test_a_strike_that_leaves_the_rack_standing_is_not_the_break() -> None:
    """Session 20261119-020755, 58 s: the cue ball rolled 100 mm out of his
    hand. The clock goes back to waiting, and the real break still counts."""
    hub, clock = _armed()
    _fire_and_settle(hub, clock, _table(310.0))
    assert (hub.clock.state, hub.clock.rack_shots) == (State.IDLE, 0)
    assert hub.clock.remaining == float(hub.clock.current_base())  # the first shot's window
    (revert,) = _kinds(hub, "shot_provisional_reverted")
    assert revert["reason"] == "rack_standing"
    (settle,) = _kinds(hub, "vision_balls_settled")
    assert settle["rejected"] == "rack_standing" and settle["state"] == "IDLE"
    assert _kinds(hub, "scratch_list")[-1]["counted"] is False

    scattered = BallList(
        balls=tuple(_ball(str(n + 1), 300.0 + 150.0 * n) for n in range(9)), arm=False
    )
    clock.now += 20.0
    asyncio.run(hub.shot_taken(30.0, ball_id=9, speed_mms=7000.0))
    clock.now += 6.0
    asyncio.run(hub.balls_settled(36.0, tracker_cue=False))
    clock.now += 0.15
    asyncio.run(hub.ball_list(36.0, "settle", scattered))
    assert (hub.clock.state, hub.clock.rack_shots) == (State.RUNNING, 1)
    assert len(_kinds(hub, "shot_provisional_reverted")) == 1


def test_a_hand_on_the_cue_ball_does_not_hide_the_rack() -> None:
    """`break_0801b`: no cue on the settle's list, a hand read as a 14, an arm
    in the frame. The nine rack balls are where they were."""
    hub, clock = _armed()
    _fire_and_settle(hub, clock, _table(None, ("14", 330.0), arm=True))
    assert (hub.clock.state, hub.clock.rack_shots) == (State.IDLE, 0)
    assert not _kinds(hub, "scratch_hold")  # and nothing calls a scratch on it


def test_a_break_whose_list_never_comes_counts_as_before() -> None:
    hub, clock = _armed()
    _fire_and_settle(hub, clock, None)
    assert hub.clock.state is State.MOTION  # waiting for its list
    clock.now += SETTLE_LIST_WAIT_S + 0.01
    hub.tick()
    assert (hub.clock.state, hub.clock.rack_shots) == (State.RUNNING, 1)


def test_without_a_rack_on_the_list_before_nothing_waits() -> None:
    clock = _Clock()
    hub = Hub(now=clock)
    asyncio.run(hub.new_rack())
    spread = BallList(
        balls=tuple(_ball(str(n + 1), 300.0 + 150.0 * n) for n in range(5)), arm=False
    )
    asyncio.run(hub.ball_list(1.0, "rest", spread))  # five balls: not a rack
    _fire_and_settle(hub, clock, None)
    assert (hub.clock.state, hub.clock.rack_shots) == (State.RUNNING, 1)
    assert "held" not in _kinds(hub, "vision_balls_settled")[-1]
