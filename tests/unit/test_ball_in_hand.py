"""#64: ball in hand after a scratch keeps the clock running. From the
scratch until an object ball moves, a settle that moved only the cue —
placed, picked up, placed again — is placing, not a shot. The pure rule
(`server/scratch.py`) and what the hub does with it."""

import asyncio

from cueclock.clock.state import State
from cueclock.server.hub import SCRATCH_HOLD_SECONDS, SETTLE_LIST_WAIT_S, Hub
from cueclock.server.scratch import RestLists, SettleFacts, only_cue_changed, table_row
from cueclock.vision.ball_list import BallList
from cueclock.vision.events import SettleMover
from tests.unit.test_scratch_list import FACTS, GONE_STILL, REST, _list

# After the scratch: the cue is gone and the 3 has moved from REST's 400.
AFTER = _list(("3", 409.0), ("8", 700.0))
PLACED = _list(("cue", 520.0), ("3", 409.0), ("8", 700.0))
PLACED_AGAIN = _list(("cue", 300.0), ("3", 409.0), ("8", 700.0))
SHOT = _list(("cue", 160.0), ("3", 470.0), ("8", 700.0))  # the 3 moved: a shot


# ---------- the rule ----------


def test_only_the_cue_changed_ignores_the_cue() -> None:
    assert only_cue_changed(PLACED, AFTER, FACTS)  # put down
    assert only_cue_changed(AFTER, PLACED, FACTS)  # picked up
    assert only_cue_changed(PLACED_AGAIN, PLACED, FACTS)  # moved spot to spot
    assert only_cue_changed(BallList(balls=PLACED.balls, arm=True), AFTER, FACTS)  # hand on it
    # Off: an object ball moved, travel at the hand bound, nothing to compare
    assert not only_cue_changed(SHOT, PLACED, FACTS)
    far = SettleFacts(**{**FACTS.__dict__, "travel_mm": 400.0})
    assert not only_cue_changed(PLACED, AFTER, far)
    assert not only_cue_changed(PLACED, None, FACTS)
    # An arm hiding an object ball reads as moved: the settle counts.
    assert not only_cue_changed(BallList(balls=PLACED.balls[:2], arm=True), AFTER, FACTS)


def test_the_comparison_list_is_arm_free_and_since_the_scratch() -> None:
    lists = RestLists()
    lists.add(1.0, REST)
    lists.add(10.0, AFTER)
    lists.add(11.0, BallList(balls=PLACED.balls, arm=True))
    assert lists.arm_free(since=10.0, before=12.0) == AFTER
    assert lists.arm_free(since=10.5, before=12.0) is None  # only an arm list since
    assert lists.arm_free(since=None, before=5.0) == REST


def test_every_settle_has_a_row() -> None:
    assert table_row(PLACED, AFTER, False, True, "none") == "ball_in_hand"
    assert table_row(SHOT, REST, True, False, "none") == "shot"
    assert table_row(AFTER, REST, True, False, "scratch") == "scratch"
    assert table_row(GONE_STILL, REST, True, False, "lifted") == "lifted"
    carried = _list(("cue", 160.0), ("3", 400.0), ("8", 700.0))
    assert table_row(carried, REST, True, False, "none") == "cue_moved"
    assert table_row(REST, REST, True, False, "none") == "still"
    assert table_row(BallList(balls=AFTER.balls, arm=True), REST, True, False, "none") == "arm"
    assert table_row(REST, None, True, False, "none") == "no_rest"


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


def _row(hub: Hub) -> dict[str, object]:
    return _kinds(hub, "scratch_list")[-1]


def _move(hub: Hub, clock: _Clock, t: float, found: BallList | None, travel: float = 120.0) -> None:
    """One episode: motion at ``t``, its settle 1 s later, the list 50 ms on."""
    clock.now += 1.0
    asyncio.run(hub.episode_started(t))
    clock.now += 1.0
    asyncio.run(hub.balls_settled(t + 1.0, movers=[SettleMover(travel_mm=travel, alive=False)]))
    if found is not None:
        clock.now += 0.05
        asyncio.run(hub.ball_list(t + 1.0, "settle", found))


def _scratch(hub: Hub, clock: _Clock) -> None:
    _move(hub, clock, 5.0, AFTER, travel=900.0)  # the cue gone, the 3 moved: a scratch
    assert _row(hub)["row"] == "scratch" and hub.clock.holding(clock.now)
    assert hub.clock.rack_shots == 1


def test_place_pick_up_place_again_keeps_the_clock_running() -> None:
    """Steven's sequence: scratch, place the cue, pick it up, place it again,
    then shoot. Only the scratch and the shot count."""
    hub, clock = _hub()
    _scratch(hub, clock)
    _move(hub, clock, 8.0, PLACED)  # placed
    assert _kinds(hub, "ball_in_hand")[0]["frame_t"] == 6.0
    _move(hub, clock, 10.0, AFTER)  # picked up: no cue, no arm
    _move(hub, clock, 12.0, PLACED_AGAIN)  # placed somewhere else
    assert [r["row"] for r in _kinds(hub, "scratch_list")][1:] == ["ball_in_hand"] * 3
    assert {r["hand"] for r in _kinds(hub, "scratch_list")[1:]} == {"applied"}
    assert hub.clock.rack_shots == 1 and hub.clock.state is State.RUNNING
    reverts = _kinds(hub, "shot_provisional_reverted")
    assert [r["reason"] for r in reverts] == ["ball_in_hand"] * 3
    _move(hub, clock, 20.0, SHOT, travel=1500.0)  # the real shot
    assert hub.clock.rack_shots == 2
    (ended,) = _kinds(hub, "ball_in_hand_ended")
    assert ended["reason"] == "shot" and ended["placed"] == 3


def test_the_scratch_hold_runs_through_the_placing() -> None:
    hub, clock = _hub()
    _scratch(hub, clock)
    _move(hub, clock, 8.0, PLACED)  # 2.05 s after the hold was granted
    assert hub.clock.holding(clock.now)
    assert not _kinds(hub, "scratch_hold_released")
    clock.now += SCRATCH_HOLD_SECONDS - 3.0
    hub.tick()
    assert hub.clock.remaining == 30.0


def test_a_short_shot_that_moves_an_object_ball_counts() -> None:
    hub, clock = _hub()
    _scratch(hub, clock)
    _move(hub, clock, 8.0, SHOT, travel=200.0)  # held (short travel), then counted
    assert _row(hub)["row"] != "ball_in_hand"
    assert hub.clock.rack_shots == 2
    assert _kinds(hub, "ball_in_hand_ended")[0]["reason"] == "shot"


def test_an_arm_hiding_an_object_ball_counts() -> None:
    hub, clock = _hub()
    _scratch(hub, clock)
    _move(hub, clock, 8.0, BallList(balls=PLACED.balls[:2], arm=True))  # the 8 hidden
    assert hub.clock.rack_shots == 2


def test_no_scratch_no_ball_in_hand() -> None:
    """A cue lifted in open play still counts, as before #64: only a scratch
    starts ball in hand."""
    hub, clock = _hub()
    _move(hub, clock, 5.0, GONE_STILL)
    assert _row(hub)["verdict"] == "lifted" and _row(hub)["row"] == "lifted"
    assert hub.clock.rack_shots == 1
    _move(hub, clock, 8.0, REST)
    assert hub.clock.rack_shots == 2
    assert not _kinds(hub, "ball_in_hand")


def test_a_refuted_scratch_ends_ball_in_hand() -> None:
    hub, clock = _hub()
    clock.now += 1.0
    asyncio.run(hub.episode_started(5.0))
    asyncio.run(
        hub.scratch_candidate(
            5.5,
            x_mm=60.0,
            y_mm=80.0,
            pocket_x_mm=0.0,
            pocket_y_mm=0.0,
            whiteness=0.5,
            color_age_s=0.0,
        )
    )
    clock.now += 1.0
    asyncio.run(hub.balls_settled(6.0, movers=[SettleMover(travel_mm=900.0, alive=False)]))
    asyncio.run(hub.ball_list(6.0, "settle", BallList(balls=AFTER.balls, arm=True)))
    asyncio.run(hub.episode_started(8.0))  # the next motion: ball in hand begins
    assert _kinds(hub, "ball_in_hand")
    asyncio.run(hub.scratch_refuted(8.2, witness_id=1, x_mm=0.0, y_mm=0.0, whiteness=0.9))
    assert _kinds(hub, "ball_in_hand_ended")[0]["reason"] == "refuted"


def test_new_rack_ends_ball_in_hand() -> None:
    hub, clock = _hub()
    _scratch(hub, clock)
    clock.now += 1.0
    asyncio.run(hub.episode_started(8.0))
    asyncio.run(hub.new_rack())
    assert _kinds(hub, "ball_in_hand_ended")[0]["reason"] == "new_rack"


def test_a_placing_whose_list_never_comes_counts_as_today() -> None:
    hub, clock = _hub()
    _scratch(hub, clock)
    _move(hub, clock, 8.0, None)
    assert hub.clock.state is State.MOTION  # waiting for the list
    clock.now += SETTLE_LIST_WAIT_S + 0.01
    hub.tick()
    assert hub.clock.state is State.RUNNING and hub.clock.rack_shots == 2
    assert _kinds(hub, "vision_balls_settled")[-1]["held"] == "timeout"


def test_the_countdown_a_late_decision_starts_runs_from_the_settle() -> None:
    hub, clock = _hub()
    _scratch(hub, clock)
    clock.now += SCRATCH_HOLD_SECONDS  # the hold is over
    _move(hub, clock, 8.0, None)
    clock.now += SETTLE_LIST_WAIT_S + 0.01
    hub.tick()  # decided at the timeout, as of the settle
    clock.now += 1.0
    hub.tick()
    assert abs(hub.clock.remaining - (30.0 - SETTLE_LIST_WAIT_S - 1.01)) < 1e-6


def test_a_command_first_decides_the_settle_as_today() -> None:
    hub, clock = _hub()
    _scratch(hub, clock)
    _move(hub, clock, 8.0, None)
    asyncio.run(hub.extension())  # a command
    assert _kinds(hub, "vision_balls_settled")[-1]["held"] == "input"
    assert hub.clock.rack_shots == 2


def test_the_hand_still_moving_at_the_settle_does_not_decide_it() -> None:
    """#69, session 20261121-153046 at 400.66 s: the motion latch rose 32 ms
    after a placing's settle and 47 ms before its list. The motion waits
    behind the settle, the list keeps the placing off the clock, ball in hand
    goes on, and the next adjustment is placing too."""
    hub, clock = _hub()
    _scratch(hub, clock)
    _move(hub, clock, 8.0, None)  # the settle at 9.0, held for its list
    frozen = hub.clock.remaining
    clock.now += 0.032
    asyncio.run(hub.episode_started(9.032))  # the hand again
    assert not _kinds(hub, "vision_balls_settled")[1:]  # still undecided
    clock.now += 0.015
    asyncio.run(hub.ball_list(9.0, "settle", PLACED))
    settle = _kinds(hub, "vision_balls_settled")[-1]
    assert settle["held"] == "list" and settle["rejected"] == "ball_in_hand"
    assert _row(hub)["hand"] == "applied" and _row(hub)["counted"] is False
    assert hub.clock.rack_shots == 1 and not _kinds(hub, "ball_in_hand_ended")
    # The motion that waited is applied behind the list: the countdown the
    # placing gave back is frozen again, with nothing lost.
    kinds = [r["kind"] for r in hub.journal.tail(10_000.0)]
    assert kinds.index("scratch_list", kinds.index("ball_in_hand")) < len(kinds) - 1
    assert kinds[-1] == "shot_provisional"
    assert hub.clock.state is State.MOTION and hub.clock.remaining == frozen
    clock.now += 1.0
    asyncio.run(hub.balls_settled(10.4, movers=[SettleMover(travel_mm=374.0, alive=False)]))
    clock.now += 0.05
    asyncio.run(hub.ball_list(10.4, "settle", PLACED_AGAIN))
    assert _row(hub)["row"] == "ball_in_hand" and hub.clock.rack_shots == 1
    _move(hub, clock, 20.0, SHOT, travel=1500.0)
    (ended,) = _kinds(hub, "ball_in_hand_ended")
    assert ended["reason"] == "shot" and ended["placed"] == 2


def test_what_waited_behind_a_settle_is_applied_in_order_when_the_wait_runs_out() -> None:
    hub, clock = _hub()
    _scratch(hub, clock)
    _move(hub, clock, 8.0, None)
    clock.now += 0.03
    asyncio.run(hub.episode_started(9.03))
    clock.now += 0.2
    asyncio.run(hub.shot_taken(9.23, ball_id=1, speed_mms=900.0))
    assert hub.clock.rack_shots == 1  # the settle, the motion and the strike all wait
    clock.now += SETTLE_LIST_WAIT_S
    hub.tick()
    wanted = ("vision_balls_settled", "shot_provisional", "vision_shot_taken")
    kinds = [r["kind"] for r in hub.journal.tail(10_000.0) if r["kind"] in wanted]
    assert kinds[-3:] == list(wanted)
    assert _kinds(hub, "vision_balls_settled")[-1]["held"] == "timeout"
    assert hub.clock.rack_shots == 2 and hub.clock.state is State.MOTION
    # Each as of its own arrival: the freeze is stamped when the motion came.
    assert hub._provisional_open_mono == clock.now - SETTLE_LIST_WAIT_S - 0.2


def test_a_call_past_the_wait_does_not_queue() -> None:
    """The tick loop decides an overdue settle, but it beats at 10 Hz: a
    call arriving after the wait and before the next beat decides it too."""
    hub, clock = _hub()
    _scratch(hub, clock)
    _move(hub, clock, 8.0, None)
    clock.now += SETTLE_LIST_WAIT_S + 0.01
    asyncio.run(hub.episode_started(9.31))
    assert _kinds(hub, "vision_balls_settled")[-1]["held"] == "timeout"
    assert hub.clock.rack_shots == 2 and hub.clock.state is State.MOTION


def test_a_command_applies_what_waited_before_it_runs() -> None:
    hub, clock = _hub()
    _scratch(hub, clock)
    _move(hub, clock, 8.0, None)
    clock.now += 0.03
    asyncio.run(hub.episode_started(9.03))
    asyncio.run(hub.new_rack())
    kinds = [r["kind"] for r in hub.journal.tail(10_000.0)]
    assert kinds.index("shot_provisional", kinds.index("ball_in_hand")) < kinds.index(
        "cmd_new_rack"
    )
    assert _kinds(hub, "vision_balls_settled")[-1]["held"] == "input"
    assert hub.clock.state is State.IDLE


def test_a_settle_that_waited_behind_another_gets_its_own_list() -> None:
    """Two placings whose settles come closer together than a list takes:
    the second waits behind the first, is held in its turn, and its own list
    decides it."""
    hub, clock = _hub()
    _scratch(hub, clock)
    _move(hub, clock, 8.0, None)  # settle at 9.0, held
    clock.now += 0.02
    asyncio.run(hub.episode_started(9.02))
    clock.now += 0.02
    asyncio.run(hub.balls_settled(9.04, movers=[SettleMover(travel_mm=40.0, alive=False)]))
    clock.now += 0.02
    asyncio.run(hub.ball_list(9.0, "settle", PLACED))
    assert _row(hub)["hand"] == "applied"
    assert hub.clock.state is State.MOTION  # the second settle is held now
    clock.now += 0.05
    asyncio.run(hub.ball_list(9.04, "settle", PLACED_AGAIN))
    assert [r["hand"] for r in _kinds(hub, "scratch_list")[1:]] == ["applied", "applied"]
    assert hub.clock.state is State.RUNNING and hub.clock.rack_shots == 1


def test_placing_in_grace_leaves_the_buzzer_alone() -> None:
    hub, clock = _hub()
    _scratch(hub, clock)
    clock.now += 40.0
    hub.tick()
    assert hub.clock.state is State.GRACE
    _move(hub, clock, 50.0, PLACED)
    assert hub.clock.state is State.GRACE and _row(hub)["hand"] == "applied"


def test_a_placing_the_clock_refuses_still_counts() -> None:
    """A GRACE fire commits its shot at once; placing cannot un-count it, and
    the journal says the verdict was not applied."""
    hub, clock = _hub()
    _scratch(hub, clock)
    clock.now += 40.0
    hub.tick()
    asyncio.run(hub.shot_taken(49.0, ball_id=1, speed_mms=900.0))
    _move(hub, clock, 50.0, PLACED)
    assert _row(hub)["hand"] == "refused"
    assert _kinds(hub, "vision_balls_settled")[-1]["hand_refused"] == "ball_in_hand"
    assert hub.clock.remaining == 30.0 and hub.clock.state is State.RUNNING
