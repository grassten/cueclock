"""#63: the ball list's say in the scratch hold — the pure verdict
(`server/scratch.py`) and what the hub does with it."""

import asyncio
import time

from cueclock.clock.state import State
from cueclock.server.hub import Hub
from cueclock.server.scratch import (
    TRAVEL_MIN_MM,
    RestLists,
    SettleFacts,
    judge,
    object_moved,
)
from cueclock.vision.ball_list import BallList, ListedBall
from cueclock.vision.events import EventConfig, SettleMover


def _ball(label: str, x: float, y: float = 300.0) -> ListedBall:
    return ListedBall(
        cx=x,
        cy=y,
        r_pred=24.0,
        cue=label == "cue",
        number=int(label) if label.isdigit() else None,
        rim=0.9 if label == "cue" else 0.3,
        white=0.1,
        margin=12.0,
        colour_px=200,
        vote=5.0,
        coverage=0.9,
    )


def _list(*balls: tuple[str, float], arm: bool = False) -> BallList:
    return BallList(balls=tuple(_ball(label, x) for label, x in balls), arm=arm)


REST = _list(("cue", 100.0), ("3", 400.0), ("8", 700.0))
GONE_STILL = _list(("3", 400.0), ("8", 700.0))  # the cue gone, nothing else moved
GONE_MOVED = _list(("3", 409.0), ("8", 700.0))  # ... and the 3 moved 9 px
FACTS = SettleFacts(t=10.0, epoch=1, counted=True, travel_mm=150.0, tracker_cue=False, motion_t=5.0)


def test_the_travel_bound_is_the_engines_pickup_bound() -> None:
    assert EventConfig().pickup_travel_max_mm == TRAVEL_MIN_MM


def test_an_object_ball_moved_needs_more_than_jitter() -> None:
    assert not object_moved(REST, _list(("3", 401.4), ("8", 700.0)))  # 1.4 px: still
    assert object_moved(REST, GONE_MOVED)
    assert object_moved(REST, _list(("8", 700.0)))  # the 3 left the table
    assert object_moved(REST, _list(("3", 400.0), ("8", 700.0), ("5", 900.0)))  # one appeared


def test_each_guard_on_and_off() -> None:
    assert judge(GONE_MOVED, REST, True, FACTS, False).call == "scratch"
    # 1. the cue was not on the table before the shot
    assert judge(GONE_MOVED, REST, False, FACTS, False).call == "none"
    # 2. the list sees the cue, or an arm over the table hides what is gone
    assert judge(REST, REST, True, FACTS, False).says == "cue"
    arm = BallList(balls=GONE_MOVED.balls, arm=True)
    assert judge(arm, REST, True, FACTS, False).call == "none"
    assert judge(arm, REST, True, FACTS, False).says == "arm"
    # 3. the tracker still holds a cue-grade ball
    tracker = SettleFacts(**{**FACTS.__dict__, "tracker_cue": True})
    assert judge(GONE_MOVED, REST, True, tracker, False).call == "none"
    # 4. lifted by hand: nothing else moved, short travel, no candidate, and
    # the cue ball never seen rolling (#67) ...
    hand = SettleFacts(**{**FACTS.__dict__, "cue_roll_s": 0.0})
    assert judge(GONE_STILL, REST, True, hand, False).call == "lifted"
    # ... unless any one of the four says otherwise
    far = SettleFacts(**{**hand.__dict__, "travel_mm": 400.0})
    assert judge(GONE_STILL, REST, True, far, False).call == "scratch"
    assert judge(GONE_STILL, REST, True, hand, True).call == "scratch"
    rolled = SettleFacts(**{**hand.__dict__, "cue_roll_s": 0.19})  # tip_bump's reading
    assert judge(GONE_STILL, REST, True, rolled, False).call == "scratch"
    # No evidence counts as rolling.
    assert judge(GONE_STILL, REST, True, FACTS, False).call == "scratch"
    # and only at a settle that counted a shot
    uncounted = SettleFacts(**{**FACTS.__dict__, "counted": False})
    assert judge(GONE_MOVED, REST, True, uncounted, False).call == "none"


def test_before_is_the_last_arm_free_list_or_a_later_one_with_the_cue() -> None:
    lists = RestLists()
    assert lists.before(5.0) == (None, False)
    lists.add(1.0, REST)
    lists.add(2.0, _list(("3", 400.0), arm=True))  # a hand over the cue
    lists.add(6.0, GONE_STILL)  # read after the motion began: not "before"
    assert lists.before(5.0) == (REST, True)
    nocue = RestLists()
    nocue.add(1.0, GONE_STILL)  # ball in hand, cue not yet placed ...
    assert nocue.before(5.0) == (GONE_STILL, False)
    nocue.add(2.0, _list(("cue", 100.0), arm=True))  # ... then placed under a hand
    assert nocue.before(5.0) == (GONE_STILL, True)


# ---------- the hub ----------


def _hub() -> Hub:
    hub = Hub()
    hub.clock.start(time.monotonic())
    asyncio.run(hub.ball_list(1.0, "rest", REST))
    return hub


def _shot(hub: Hub, t: float = 5.0) -> None:
    asyncio.run(hub.episode_started(t))
    assert hub.clock.state is State.MOTION


def _settle(
    hub: Hub,
    t: float = 10.0,
    travel: float = 150.0,
    tracker_cue: bool = False,
    roll: float | None = None,
) -> None:
    movers = [SettleMover(travel_mm=travel, alive=False)]
    asyncio.run(hub.balls_settled(t, movers=movers, tracker_cue=tracker_cue, cue_roll_s=roll))


def _candidate(hub: Hub, t: float) -> None:
    asyncio.run(
        hub.scratch_candidate(
            t,
            x_mm=60.0,
            y_mm=80.0,
            pocket_x_mm=0.0,
            pocket_y_mm=0.0,
            whiteness=0.5,
            color_age_s=0.0,
        )
    )


def _kinds(hub: Hub, kind: str) -> list[dict[str, object]]:
    return [r for r in hub.journal.tail(600.0) if r["kind"] == kind]


def test_the_list_calls_a_scratch_today_s_logic_missed() -> None:
    """kick_scratch_0811's order: the list calls it at the settle, the
    candidate follows 0.63 s later and must not grant a second hold."""
    hub = _hub()
    _shot(hub)
    _settle(hub, travel=1711.0)
    asyncio.run(hub.ball_list(10.0, "settle", GONE_STILL))
    assert hub.clock.holding(time.monotonic())
    (verdict,) = _kinds(hub, "scratch_list")
    assert verdict["verdict"] == "scratch" and verdict["says"] == "no_cue"
    assert verdict["before"] == "cue" and verdict["travel_mm"] == 1711
    assert _kinds(hub, "scratch_hold")[0]["by"] == "list"
    _candidate(hub, 10.63)
    assert len(_kinds(hub, "scratch_hold")) == 1  # one hold per shot
    assert _kinds(hub, "vision_scratch_candidate")[0]["listed"] is True
    _shot(hub, 20.0)  # the next shot retires this one's question
    (call,) = _kinds(hub, "scratch_call")
    assert call["by"] == "both" and call["held"] is True


def test_a_cue_lifted_by_hand_is_not_a_scratch() -> None:
    """Never seen rolling, nothing else moved: a hand took it (#67), so the
    settle counts nothing and calls nothing."""
    hub = _hub()
    _shot(hub)
    _settle(hub, roll=0.0)
    asyncio.run(hub.ball_list(10.0, "settle", GONE_STILL))
    assert not hub.clock.holding(time.monotonic())
    (row,) = _kinds(hub, "scratch_list")
    assert row["verdict"] == "none" and row["row"] == "by_hand" and row["counted"] is False
    _shot(hub, 20.0)
    assert not _kinds(hub, "scratch_call")  # nothing was called


def test_a_cue_lifted_whose_list_comes_late_is_lifted() -> None:
    """The same lift decided before its list came (here: a command): it
    counted, so the list journals it `lifted` and still calls no scratch."""
    hub = _hub()
    _shot(hub)
    _settle(hub, roll=0.0)
    asyncio.run(hub.extension())  # a press decides the held settle as today
    asyncio.run(hub.ball_list(10.0, "settle", GONE_STILL))
    (row,) = _kinds(hub, "scratch_list")
    assert row["verdict"] == "lifted" and row["counted"] is True and row["hand"] == "late"
    assert not hub.clock.holding(time.monotonic())


def test_a_cue_rolled_into_a_pocket_touching_nothing_is_a_scratch() -> None:
    """Under 400 mm, nothing else moved, no candidate: seen rolling, so the
    cue ball went down by itself (#67, §21). Before, this read as lifted."""
    hub = _hub()
    _shot(hub)
    _settle(hub, travel=250.0, roll=0.9)
    asyncio.run(hub.ball_list(10.0, "settle", GONE_STILL))
    assert _kinds(hub, "scratch_list")[0]["verdict"] == "scratch"
    assert hub.clock.holding(time.monotonic())


def test_the_list_clears_a_candidate_and_a_later_one() -> None:
    """A candidate open at the settle holds the fresh countdown; the settle's
    list sees the cue and releases it. A second candidate for the same shot
    finds it cleared."""
    hub = _hub()
    _shot(hub)
    _candidate(hub, 7.0)
    _settle(hub)
    assert hub.clock.holding(time.monotonic())
    asyncio.run(hub.ball_list(10.0, "settle", REST))
    assert not hub.clock.holding(time.monotonic())
    assert _kinds(hub, "scratch_list")[0]["verdict"] == "cleared"
    assert _kinds(hub, "scratch_hold_released")[0]["reason"] == "cleared"
    _candidate(hub, 10.5)
    assert not hub.clock.holding(time.monotonic())
    assert _kinds(hub, "vision_scratch_candidate")[-1]["cleared"] is True
    _shot(hub, 20.0)
    (call,) = _kinds(hub, "scratch_call")
    assert call["closed"] == "cleared" and call["says"] == "cue"


def test_a_list_cannot_clear_once_something_has_moved() -> None:
    """The next motion began before the settle's list arrived: today's logic
    decides alone, and the journal says the list never came."""
    hub = _hub()
    _shot(hub)
    _candidate(hub, 7.0)
    _settle(hub)
    _shot(hub, 10.05)
    asyncio.run(hub.ball_list(10.0, "settle", REST))
    (verdict,) = _kinds(hub, "scratch_list")
    assert verdict["verdict"] == "no_list" and verdict["frame_t"] == 10.0
    (call,) = _kinds(hub, "scratch_call")
    assert call["by"] == "candidate" and call["held"] is True and call["closed"] is None


def test_a_settle_with_no_list_is_today_s_logic() -> None:
    hub = _hub()
    _shot(hub)
    _candidate(hub, 7.0)
    _settle(hub)
    assert hub.clock.holding(time.monotonic())  # granted exactly as before #63
    _shot(hub, 20.0)
    assert _kinds(hub, "scratch_list")[0]["verdict"] == "no_list"


def test_an_arm_or_the_tracker_leaves_today_s_logic_alone() -> None:
    hub = _hub()
    _shot(hub)
    _settle(hub, travel=1500.0)
    asyncio.run(hub.ball_list(10.0, "settle", BallList(balls=GONE_MOVED.balls, arm=True)))
    assert not hub.clock.holding(time.monotonic())
    _shot(hub, 20.0)
    _settle(hub, t=25.0, travel=1500.0, tracker_cue=True)
    asyncio.run(hub.ball_list(25.0, "settle", GONE_MOVED))
    assert not hub.clock.holding(time.monotonic())
    assert [r["verdict"] for r in _kinds(hub, "scratch_list")] == ["none", "none"]


def test_a_refutation_stands_over_the_list() -> None:
    """The cue counts as present if either one sees it."""
    hub = _hub()
    _shot(hub)
    _candidate(hub, 7.0)
    _settle(hub)
    asyncio.run(hub.ball_list(10.0, "settle", GONE_MOVED))
    assert hub.clock.holding(time.monotonic())
    asyncio.run(hub.scratch_refuted(10.5, witness_id=1, x_mm=0.0, y_mm=0.0, whiteness=0.9))
    assert not hub.clock.holding(time.monotonic())
    assert _kinds(hub, "scratch_hold_released")[0]["reason"] == "refuted"


def test_a_hold_is_owed_across_a_verdict_and_a_reverted_freeze() -> None:
    """2026-08-13: a candidate during a provisional freeze, the confirmed
    verdict 0.14 s later, then the freeze reverted as a pickup discard. The
    countdown came back with no hold; now the revert grants it."""
    hub = _hub()
    _shot(hub)
    _candidate(hub, 6.0)
    asyncio.run(hub.scratch_seen(6.14))
    assert not hub.clock.holding(time.monotonic())  # frozen: nothing to hold yet
    asyncio.run(hub.episode_discarded(7.0))
    assert hub.clock.state is State.RUNNING
    assert hub.clock.holding(time.monotonic())
    assert _kinds(hub, "scratch_hold")[0]["by"] == "candidate"


def test_a_hold_owed_with_no_countdown_says_why() -> None:
    hub = _hub()
    asyncio.run(hub.set_mode("no_clock"))
    asyncio.run(hub.episode_started(5.0))
    _candidate(hub, 6.0)
    asyncio.run(hub.balls_settled(10.0))
    asyncio.run(hub.episode_started(20.0))
    (call,) = _kinds(hub, "scratch_call")
    assert call["held"] is False and call["missed"] == "no_clock"
