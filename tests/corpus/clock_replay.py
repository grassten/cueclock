"""Drive a real ``Hub`` from a corpus clip's frames — issue #34 work item 1.

The battery grades the event stream. Since #33 (checkpoint 83) the event
stream and the product deliberately diverge: when the strike machinery
misses, the motion-episode latch freezes the clock ~0.25 s after the stroke
and the settle counts the shot, which is *faster* than the ~0.5 s a confirmed
``ShotTaken`` takes. A clip where vision misses the strike but the clock stops
on time is behaving correctly while the battery scores it a failure.

This module replays a clip the way ``cli.py``'s run loop does — same events,
same latch edges, same per-frame telemetry — into a real ``Hub``, with the
hub's clock driven by **frame timestamps** instead of wall time. Everything it
touches is pure or takes ``now``, so a replay is deterministic and a clip's
clock outcome becomes an assertable fact.

What it deliberately does NOT do is re-implement policy. The provisional
freeze, the settle-time shot adjudication, the break tiers and the frozen-clock
watchdog all live in the hub, and the harness drives the real one so the
grade can never drift from what ships.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from cueclock.bridge import hub_calls
from cueclock.capture.list_reads import REST_S, rest_due
from cueclock.clock.state import State
from cueclock.server import hub as hub_module
from cueclock.server.hub import Hub
from cueclock.vision.ball_list import BallList, same_table
from cueclock.vision.events import BallsSettled, ShotTaken

if TYPE_CHECKING:
    from cueclock.vision.pipeline import VisionPipeline


# The imperceptibility budget, anchored on measurement (#34: "anchor it, do
# not pick it by taste"). `scripts/clock_budget_probe.py` replays every clip
# with ground truth and reports freeze latency against the golden's
# `ShotTaken.t`; over n=49 strokes the distribution is:
#
#     min -0.256   median -0.013   p90 +0.000   max +0.019
#
# Note what the reference point is: a golden's `ShotTaken.t` is the strike
# *confirm*, not the stroke. Under #33 the latch freezes at motion onset,
# which precedes the confirm — so negative values are the norm and are the
# improvement, not an error.
#
# LATE bound 0.25 s: ~13x the measured worst lateness (+0.019), and the lower
# end of the 0.25-0.5 s window #34 names as imperceptible. Anything slower is
# the player watching the countdown run through their shot.
# EARLY bound 0.5 s: the confirm path's own latency. A freeze earlier than
# that relative to the confirm cannot belong to this stroke's episode — it is
# a different episode, which is what the phantom check below is for.
FREEZE_LATE_S = 0.25
FREEZE_EARLY_S = 0.5

# Clips recorded on an armed rack, the clock IDLE and waiting for the break.
# WNT §5 makes the break untimed and starts the window at the rest, so there is
# no countdown to freeze: the criterion for a break is "counted exactly once
# out of IDLE", and a freeze is not expected at all. One of them holds no
# break (`break_0801b`, see `FALSE_STARTS` in `test_corpus_events.py`).
BREAK_CLIPS = frozenset(
    {
        "break_0801",
        "break_0801b",
        "break_outrun_0805",
        "break_underread_0801",
        "break_dark_flight_0806",
        "break_dark_total_0806",
        "break_shot",
    }
)


def golden_shot_times(name: str, goldens_dir: Path = Path("tests/goldens")) -> list[float] | None:
    """The ``ShotTaken`` times a clip's golden holds, or None if it has none.

    Read straight off the file rather than from a replay, deliberately: on a
    clip whose fire has since been lost (``stroke_tug_strike_0801``) the
    golden is the only surviving record of when the stroke was, and it is
    what makes that clip gradeable at all.
    """
    path = goldens_dir / f"{name}.json"
    if not path.exists():
        return None
    doc = json.loads(path.read_text())
    return [float(e["t"]) for e in doc["events"] if e["type"] == "ShotTaken"]


def ground_truth(
    name: str, expected: object, golden_shot_times: list[float] | None
) -> tuple[list[float] | None, str]:
    """Per-clip stroke times, seeded mechanically — never invented (#34).

    Inventing a stroke time to fill a gap is the same failure as the #33
    Part C error, so this only ever combines two things the repo already
    asserts, and reports which one it used:

    * **how many** real shots — the ``EXPECTED`` table, whose tuples were
      argued over and carry written justifications;
    * **roughly when** — the golden's ``ShotTaken.t``, usable on any clip
      that ever fired one, including clips current code now misses
      (``stroke_tug_strike_0801``'s golden still holds its 13.198 s stroke).

    Where the two disagree, or either is absent, the answer is ``None`` and
    the clip is reported unasserted. A clip left uncovered is fine; a test
    asserting a fabricated truth is worse than no test.
    """
    shots_expected = expected[0] if isinstance(expected, tuple) else None
    if shots_expected is None:
        return None, "shots unasserted in EXPECTED"
    if golden_shot_times is None:
        return None, "no golden recorded"
    if len(golden_shot_times) != shots_expected:
        return None, (
            f"EXPECTED says {shots_expected} shots, golden holds "
            f"{len(golden_shot_times)} — disagreement, not ground truth"
        )
    return golden_shot_times, "golden ShotTaken.t"


@dataclass(frozen=True)
class ClockMark:
    """One user-visible thing the clock did, stamped in clip time."""

    t: float
    kind: str  # "freeze" | "count" | "uncount" | "resume" | "expire"
    state: str
    rack_shots: int


@dataclass
class ClockTimeline:
    """Every clock transition a clip produced, plus the event diagnostics."""

    scenario: str
    events: list[Any] = field(default_factory=list)  # the clip's vision events
    marks: list[ClockMark] = field(default_factory=list)
    shots_fired: list[float] = field(default_factory=list)  # ShotTaken event times
    settles: list[float] = field(default_factory=list)
    provisional_opens: list[float] = field(default_factory=list)  # latch freezes
    # Balls resolved by the quiet layout each settle was diffed against.
    # `None` means there was no quiet layout at all.
    baselines: list[int | None] = field(default_factory=list)
    # #63, read off the hub's own journal: when a scratch hold was granted
    # (and by whom), and the ball list's verdict at each settle it judged.
    holds: list[tuple[float, str]] = field(default_factory=list)
    list_verdicts: list[tuple[float, str]] = field(default_factory=list)
    # #64: which row of the ball-in-hand table each judged settle fell in,
    # and when a scratch hold was released early.
    rows: list[tuple[float, str]] = field(default_factory=list)
    releases: list[float] = field(default_factory=list)
    # How the clip left the clock: its state and the rack's shot count.
    final: tuple[str, int] = ("", 0)

    @property
    def rearrangement_is_anchored(self) -> bool:
        """Did every settle have a baseline worth diffing against?

        A clip that opens mid-action never gets a quiet layout, so its
        baseline resolves nothing (or one ball of an unresolved rack) and
        everything that resolves at the settle greedy-matches nothing and
        lands in `appeared`. The resulting summary is an artifact of the clip,
        not a fact about the table, and no rearrangement-derived policy can be
        graded on it.

        The floor is the hub's own (`QUIET_BASELINE_MIN_BALLS`) rather than a
        second number, because it answers the same question the live verdict
        asks — see that constant for the measurement. A clip whose baseline
        this harness would not trust is one the hub would not act on either.
        """
        return bool(self.baselines) and all(
            b is not None and b >= hub_module.QUIET_BASELINE_MIN_BALLS for b in self.baselines
        )

    @property
    def freezes(self) -> list[float]:
        """When the countdown stopped — the moment the player sees."""
        return [m.t for m in self.marks if m.kind == "freeze"]

    @property
    def counts(self) -> list[float]:
        """When a shot was actually counted (``rack_shots`` incremented)."""
        return [m.t for m in self.marks if m.kind == "count"]


@contextmanager
def _hub_time(cell: list[float]) -> Iterator[None]:
    """Pin the hub module's clock to a mutable cell of clip time.

    The same trick ``tests/unit/test_server.py::_freeze_hub_time`` uses, minus
    the fixture: patch the hub's own ``time`` binding, not ``time.monotonic``
    globally, so nothing else in the process is disturbed.
    """
    original = hub_module.time
    hub_module.time = SimpleNamespace(monotonic=lambda: cell[0])  # type: ignore[assignment]
    try:
        yield
    finally:
        hub_module.time = original  # type: ignore[assignment]


def replay_clock(
    scenario: str,
    pipeline: VisionPipeline,
    timestamps: list[float],
    frames: list[tuple[list[Any], list[float | None]]],
    indices: list[int],
    *,
    mid_rack: bool = True,
    lists: Callable[[int], BallList] | None = None,
) -> ClockTimeline:
    """Replay one clip's detections through a real ``Hub``; return the timeline.

    ``mid_rack`` starts the clock RUNNING, which is the state nearly every
    corpus clip was actually recorded in — a countdown is live and the shot
    has to stop it. Without it the clock sits in IDLE, where a settle is a
    no-op by design and there is no freeze to grade at all. The break clips
    are the exception and pass ``mid_rack=False`` so the IDLE break path
    (E8's two tiers) is the thing under test.

    ``lists`` reads the ball list of frame ``i`` (#63). Given, the harness
    reads one at every settle and on the frames `cueclock run` reads one at
    rest (`list_reads.rest_due`), writes the same ones it would, and hands
    each to the hub right behind its frame's calls — live's order with no
    latency. Without it the hub hears no lists, which is today's logic alone.
    """
    timeline = ClockTimeline(scenario=scenario)
    cell = [0.0]
    with _hub_time(cell):
        hub = Hub()
        if mid_rack:
            asyncio.run(hub.start())  # a live countdown, as at the table
        else:
            asyncio.run(hub.new_rack())  # armed rack, waiting for the break

        prev_state = hub.clock.state
        prev_rack_shots = hub.clock.rack_shots
        episode_prev = pipeline.episode_active
        next_rest = 0.0
        last_written: BallList | None = None

        for i, (detections, churn, intrusions) in zip(indices, frames, strict=True):
            t = float(timestamps[i])
            cell[0] = t  # the hub's `now` for everything this frame does

            events = pipeline.advance(t, detections, churn, intrusions)
            timeline.events += events
            reads: list[tuple[str, BallList]] = []
            for event in events:
                if isinstance(event, ShotTaken):
                    timeline.shots_fired.append(t)
                elif isinstance(event, BallsSettled):
                    timeline.settles.append(t)
                    # How many balls the quiet layout this settle was diffed
                    # against resolved — `None` when there was none and the
                    # settled frame stood in for one. Read off the summary the
                    # hub itself receives (#37) rather than reconstructed here,
                    # so the harness cannot grade a different number from the
                    # one the live verdict reads.
                    summary = pipeline.last_settle_rearrange
                    timeline.baselines.append(None if summary is None else summary.baseline_balls)
                    if lists is not None:
                        reads.append(("settle", lists(i)))
                        next_rest = t + REST_S
            if lists is not None and rest_due(t, next_rest, pipeline, churn):
                next_rest = t + REST_S
                found = lists(i)
                if last_written is None or not same_table(last_written, found):
                    reads.append(("rest", found))  # a still table repeats itself

            # The run loop's own translation (`bridge.hub_calls`): events,
            # then the #33 latch edges, so a same-frame fire opens the
            # provisional and a same-frame settle resolves it.
            calls, episode_prev = hub_calls(hub, pipeline, t, events, episode_prev)
            for call in calls:
                asyncio.run(call)
            for reason, found in reads:
                last_written = found
                asyncio.run(hub.ball_list(t, reason, found))

            hub.note_frame(t)
            hub.note_motion(pipeline.last_motion)

            # The clock-affecting half of `Hub.tick_loop`, minus broadcast.
            hub._decide_overdue_settle(t)
            hub.clock.tick(t)
            hub._watch_frozen_clock(t)

            state = hub.clock.state
            rack_shots = hub.clock.rack_shots
            if rack_shots != prev_rack_shots:
                # Down is the break taken back (design §17), never a shot.
                kind = "count" if rack_shots > prev_rack_shots else "uncount"
                timeline.marks.append(ClockMark(t, kind, state.value, rack_shots))
            if state is not prev_state:
                if state is State.MOTION:
                    timeline.marks.append(ClockMark(t, "freeze", state.value, rack_shots))
                elif state is State.RUNNING and prev_state is State.MOTION:
                    timeline.marks.append(ClockMark(t, "resume", state.value, rack_shots))
                elif state is State.EXPIRED:
                    timeline.marks.append(ClockMark(t, "expire", state.value, rack_shots))
            # Harness precondition, not policy: a clip is graded on what the
            # clock did with its SHOTS, and several clips run longer than one
            # countdown (drill_shot_0801's stroke lands at 40.9 s against a
            # 30 s base). A clock sitting in EXPIRED counts a shot without
            # ever freezing — real behaviour, but it would grade the
            # harness's synthetic start time rather than the clip. Re-arm so
            # every stroke meets a live countdown, which is the state these
            # clips were actually recorded in.
            if mid_rack and state is State.EXPIRED:
                asyncio.run(hub.start())
                state = hub.clock.state

            prev_state = state
            prev_rack_shots = hub.clock.rack_shots

        timeline.final = (hub.clock.state.value, hub.clock.rack_shots)
        records = hub.journal.tail(10_000.0)
        timeline.provisional_opens = [
            float(r["t"])
            for r in records
            if r["kind"] == "shot_provisional" and r.get("t") is not None
        ]
        # The hub's clock is clip time here, so a record's mono is its t.
        timeline.holds = [
            (float(r["mono"]), str(r["by"])) for r in records if r["kind"] == "scratch_hold"
        ]
        timeline.list_verdicts = [
            (float(r["frame_t"]), str(r["verdict"]))
            for r in records
            if r["kind"] == "scratch_list" and r.get("frame_t") is not None
        ]
        timeline.rows = [
            (float(r["frame_t"]), str(r["row"]))
            for r in records
            if r["kind"] == "scratch_list" and r.get("row") is not None
        ]
        timeline.releases = [
            float(r["mono"]) for r in records if r["kind"] == "scratch_hold_released"
        ]
    return timeline
