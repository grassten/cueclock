"""§16: a freeze on a table where nothing moved is not a shot.

The tracker reports a ball it lost the same way whether a pot or a sleeve
took it; the settle's ball list reads the pixels and finds every ball where
it stood. The pure rule (`server/scratch.py`) and what the hub does with it.
Anchored on session 20261119-020755 at 176 s: a sleeve beside the ball at the
side pocket, a freeze, a settle reporting one ball vanished, and a fresh 30 s
with 20 left on the clock.
"""

import asyncio

from cueclock.clock.state import State
from cueclock.server.hub import SETTLE_LIST_WAIT_S, Hub
from cueclock.server.scratch import nothing_moved
from cueclock.vision.ball_list import BallList
from cueclock.vision.events import SettleMover
from cueclock.vision.pipeline import RearrangeSummary
from tests.unit.test_scratch_list import REST, _list

SAME = _list(("cue", 100.9), ("3", 401.2), ("8", 699.5))  # jitter under 3 px
POTTED = _list(("cue", 100.0), ("8", 700.0))  # the 3 is gone
NUDGED = _list(("cue", 106.0), ("3", 400.0), ("8", 700.0))  # the cue moved 6 px
VANISHED = RearrangeSummary(moved=0, appeared=0, vanished=1, moved_mm=0.0, baseline_balls=3)
MOVED = RearrangeSummary(moved=1, appeared=0, vanished=1, moved_mm=310.0, baseline_balls=3)


# ---------- the rule ----------


def test_nothing_moved_needs_every_ball_read_in_place() -> None:
    assert nothing_moved(SAME, REST, True)
    assert nothing_moved(BallList(balls=SAME.balls, arm=True), REST, True)  # an arm, all seen
    assert not nothing_moved(POTTED, REST, True)  # an object ball left
    assert not nothing_moved(NUDGED, REST, True)  # only the cue moved: a stick may have
    assert not nothing_moved(SAME, None, True)  # nothing to compare with
    assert not nothing_moved(SAME, REST, False)  # no cue on the table before
    # A hand read as a ball, a ball hidden by the arm, a second "cue": not proof.
    assert not nothing_moved(
        _list(*[("cue", 100.0), ("3", 400.0), ("8", 700.0), ("5", 900.0)]), REST, True
    )
    assert not nothing_moved(_list(("cue", 100.0), ("3", 400.0), arm=True), REST, True)
    assert not nothing_moved(
        _list(("cue", 100.0), ("cue", 250.0), ("3", 400.0), ("8", 700.0)), REST, True
    )


# ---------- the hub ----------


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _hub() -> tuple[Hub, _Clock]:
    clock = _Clock()
    hub = Hub(now=clock)
    asyncio.run(hub.start())
    asyncio.run(hub.ball_list(1.0, "rest", REST))
    return hub, clock


def _kinds(hub: Hub, kind: str) -> list[dict[str, object]]:
    return [r for r in hub.journal.tail(10_000.0) if r["kind"] == kind]


def _freeze_and_settle(hub: Hub, clock: _Clock, rearrange: RearrangeSummary) -> float:
    """A latch freeze at 5 s of clip time, its settle 0.6 s later. Returns the
    remaining the freeze held."""
    clock.now += 9.5
    asyncio.run(hub.episode_started(5.0))
    assert hub.clock.state is State.MOTION
    frozen = hub.clock.remaining
    clock.now += 0.6
    movers = [SettleMover(travel_mm=18.0, alive=False)]
    asyncio.run(hub.balls_settled(5.6, rearrange=rearrange, movers=movers, tracker_cue=True))
    return frozen


def test_a_freeze_with_nothing_moved_is_released_with_its_time() -> None:
    """The live case: the tracker lost a ball under a sleeve, the list read
    all of them in place. No shot, and the countdown resumes where it froze."""
    hub, clock = _hub()
    frozen = _freeze_and_settle(hub, clock, VANISHED)
    assert hub.clock.state is State.MOTION  # held for its list, not yet decided
    clock.now += 0.17  # the list's own latency at 13 balls that night
    asyncio.run(hub.ball_list(5.6, "settle", BallList(balls=SAME.balls, arm=True)))
    assert hub.clock.state is State.RUNNING and hub.clock.rack_shots == 0
    assert hub.clock.remaining == frozen
    (revert,) = _kinds(hub, "shot_provisional_reverted")
    assert revert["reason"] == "nothing_moved"
    (settle,) = _kinds(hub, "vision_balls_settled")
    assert settle["rejected"] == "nothing_moved" and settle["held"] == "list"
    (row,) = _kinds(hub, "scratch_list")
    assert row["row"] == "still" and row["counted"] is False


def test_a_missed_strike_still_counts_when_its_list_shows_a_ball_gone() -> None:
    """The same tracker report from a real pot the strike machinery missed:
    the list says the 3 left, so the settle counts as it always did."""
    hub, clock = _hub()
    _freeze_and_settle(hub, clock, VANISHED)
    clock.now += 0.05
    asyncio.run(hub.ball_list(5.6, "settle", POTTED))
    assert hub.clock.rack_shots == 1 and hub.clock.remaining == 30.0
    assert not _kinds(hub, "shot_provisional_reverted")
    assert _kinds(hub, "scratch_list")[-1]["row"] == "shot"


def test_a_stick_shot_that_touched_nothing_still_counts() -> None:
    hub, clock = _hub()
    _freeze_and_settle(hub, clock, VANISHED)
    clock.now += 0.05
    asyncio.run(hub.ball_list(5.6, "settle", NUDGED))
    assert hub.clock.rack_shots == 1
    assert _kinds(hub, "scratch_list")[-1]["row"] == "cue_moved"


def test_a_list_that_never_comes_counts_as_before() -> None:
    hub, clock = _hub()
    _freeze_and_settle(hub, clock, VANISHED)
    clock.now += SETTLE_LIST_WAIT_S + 0.01
    hub.tick()
    assert hub.clock.rack_shots == 1
    assert _kinds(hub, "vision_balls_settled")[-1]["held"] == "timeout"


def test_a_settle_the_tracker_saw_a_ball_move_in_never_waits() -> None:
    hub, clock = _hub()
    _freeze_and_settle(hub, clock, MOVED)
    assert hub.clock.state is State.RUNNING and hub.clock.rack_shots == 1
    assert "held" not in _kinds(hub, "vision_balls_settled")[-1]


def test_no_wait_without_a_list_from_before_the_motion() -> None:
    clock = _Clock()
    hub = Hub(now=clock)
    asyncio.run(hub.start())  # no list was ever written
    _freeze_and_settle(hub, clock, VANISHED)
    assert hub.clock.rack_shots == 1
    assert "held" not in _kinds(hub, "vision_balls_settled")[-1]


def test_the_break_is_never_held() -> None:
    """Out of IDLE the fire is the break and its settle starts the first
    shot's window: no provisional, so nothing to release."""
    clock = _Clock()
    hub = Hub(now=clock)
    asyncio.run(hub.new_rack())
    asyncio.run(hub.ball_list(1.0, "rest", REST))
    clock.now += 5.0
    asyncio.run(hub.shot_taken(5.0, ball_id=1, speed_mms=6000.0))
    clock.now += 0.5
    asyncio.run(hub.balls_settled(5.5, rearrange=VANISHED, tracker_cue=True))
    assert hub.clock.state is State.RUNNING and hub.clock.rack_shots == 1
