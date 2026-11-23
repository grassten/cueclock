"""§23: on a live clock, a settle with no motion behind it counts no shot.

The pipeline's backstop settles a table whose quiet layout changed by five
balls or more while the event engine latched nothing: built for a break whose
every track dies. A stance or an arm hiding five balls reads the same, and on
a live clock the hub used to count it as a missed shot. Anchored on session
20261123-004951 at 39.03 s (#78: getting down on a shot, five balls hidden,
25.0 s back to 30 with no shot played) and on the other three such settles in
the journals, two of them racking; none was a shot.
"""

import asyncio

from cueclock.clock.state import State
from cueclock.server.hub import Hub
from cueclock.vision.pipeline import RearrangeSummary
from tests.unit.test_scratch_list import REST, _list

AFTER_SHOT = _list(("cue", 160.0), ("3", 400.0))  # the shot before: cue moved, the 8 potted
STANCE = _list(("cue", 160.0), arm=True)  # the settle's own list: his arm hides the 3
HIDDEN = RearrangeSummary(moved=0, appeared=0, vanished=5, moved_mm=0.0, baseline_balls=15)
SHOWN = RearrangeSummary(moved=0, appeared=5, vanished=0, moved_mm=0.0, baseline_balls=10)
SHOT = RearrangeSummary(moved=1, appeared=0, vanished=1, moved_mm=310.0, baseline_balls=3)


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _kinds(hub: Hub, kind: str) -> list[dict[str, object]]:
    return [r for r in hub.journal.tail(10_000.0) if r["kind"] == kind]


def _after_a_shot() -> tuple[Hub, _Clock]:
    """A live countdown 4 s after a real shot settled: a list from before
    it, its strike at 5 s of clip time, its settle and list at 6 s."""
    clock = _Clock()
    hub = Hub(now=clock)
    asyncio.run(hub.start())
    asyncio.run(hub.ball_list(1.0, "rest", REST))
    clock.now += 5.0
    asyncio.run(hub.shot_taken(5.0, ball_id=1, speed_mms=1500.0))
    clock.now += 1.0
    asyncio.run(hub.balls_settled(6.0, rearrange=SHOT, tracker_cue=True, cue_roll_s=0.4))
    asyncio.run(hub.ball_list(6.0, "settle", AFTER_SHOT))
    assert hub.clock.state is State.RUNNING and hub.clock.rack_shots == 1
    clock.now += 4.0
    hub.tick()
    return hub, clock


def test_a_stance_hiding_balls_does_not_restart_the_countdown() -> None:
    """The live case: no strike, no latch, five balls gone from the quiet
    layout. Nothing is counted and the countdown runs on."""
    hub, _clock = _after_a_shot()
    running = hub.clock.remaining
    assert running < 30.0
    asyncio.run(hub.balls_settled(10.0, rearrange=HIDDEN, backstop=True, tracker_cue=True))
    assert hub.clock.state is State.RUNNING and hub.clock.rack_shots == 1
    assert hub.clock.remaining == running
    settle = _kinds(hub, "vision_balls_settled")[-1]
    assert settle["backstop"] is True and settle["rejected"] == "backstop"
    (refused,) = _kinds(hub, "compensation_suppressed")
    assert refused["reason"] == "backstop"


def test_its_list_is_compared_with_the_newest_one_before_the_settle() -> None:
    """No motion began, so the motion on record is the shot before. Compared
    with the list from before that shot the cue ball read 213.7 px away, a
    shot; it had not moved since that shot's own settle."""
    hub, _clock = _after_a_shot()
    asyncio.run(hub.balls_settled(10.0, rearrange=HIDDEN, backstop=True, tracker_cue=True))
    asyncio.run(hub.ball_list(10.0, "settle", STANCE))
    row = _kinds(hub, "scratch_list")[-1]
    assert row["row"] == "still" and row["cue_px"] == 0.0 and row["counted"] is False
    assert _kinds(hub, "scratch_list")[0]["cue_px"] == 60.0  # the shot's own row, as before


def test_balls_coming_back_into_view_do_not_count_either() -> None:
    """Standing back up, or balls going back on the table while racking on a
    live clock: five new, none moved."""
    hub, _clock = _after_a_shot()
    running = hub.clock.remaining
    asyncio.run(hub.balls_settled(10.0, rearrange=SHOWN, backstop=True, tracker_cue=True))
    assert hub.clock.rack_shots == 1 and hub.clock.remaining == running


def test_a_backstop_settle_with_motion_behind_it_counts_as_before() -> None:
    """A latch or a strike since the settle before: the backstop is then the
    settle of something the engine did see, and refusing it could leave the
    clock frozen. The failing-open side."""
    hub, clock = _after_a_shot()
    asyncio.run(hub.episode_started(10.0))
    assert hub.clock.state is State.MOTION
    clock.now += 1.0
    asyncio.run(hub.balls_settled(11.0, rearrange=HIDDEN, backstop=True, tracker_cue=True))
    assert hub.clock.state is State.RUNNING and hub.clock.rack_shots == 2
    assert hub.clock.remaining == 30.0
    assert "rejected" not in _kinds(hub, "vision_balls_settled")[-1]


def test_the_break_with_the_clock_idle_is_still_the_backstops() -> None:
    """IDLE is where the backstop has its one catch on record (2026-08-11:
    one moved, seven new, no fire). Break adjudication decides as before."""
    clock = _Clock()
    hub = Hub(now=clock)
    asyncio.run(hub.new_rack())
    scatter = RearrangeSummary(moved=1, appeared=7, vanished=0, moved_mm=268.0, baseline_balls=2)
    asyncio.run(hub.balls_settled(10.0, rearrange=scatter, backstop=True))
    assert hub.clock.state is State.RUNNING and hub.clock.rack_shots == 1
    assert _kinds(hub, "break_adjudicated")[0]["tier"] == "dark"
