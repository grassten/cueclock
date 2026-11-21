"""Replay a session log through current code, and check it against the live
journal (#57).

`replay` rebuilds what `cueclock run` did after the detector: the logged
detections go through `VisionPipeline.advance` (churn was logged, so the
pixel half is not needed), the events through `bridge.hub_calls`, each logged
ball list right behind the frame it reached the hub after (#63), and the
hub runs on a virtual clock — each frame at the ``mono`` its stateful half
ran at live, each logged input at its own ``mono``, and the tick loop on a
`BROADCAST_INTERVAL_SECONDS` grid. The result is a journal in the live
format.

`verify` compares the decision records of that journal with the live one.
Under the code that ran live (``same_code``) they must agree: a divergence
there means the log is missing an input, which is the log's own self-check.
Under newer code a divergence is the point — what a fix changes.

Exact timing is not expected to match. Live, a vision event reaches the hub a
few milliseconds after its frame's stateful half (it crosses to the asyncio
loop), and the tick loop's phase is not logged — so tick-driven transitions
land up to one tick apart. Hence `REMAINING_TOL_S`.
"""

import asyncio
import difflib
import json
import tempfile
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from cueclock.bridge import hub_calls
from cueclock.capture.session_log import SessionLog, git_state
from cueclock.clock import get_mode, get_ruleset
from cueclock.server.bracket import BracketError
from cueclock.server.hub import BROADCAST_INTERVAL_SECONDS, Hub
from cueclock.vision.ball_list import BallList
from cueclock.vision.calibration import TableCalibration
from cueclock.vision.detect import DetectConfig
from cueclock.vision.pipeline import VisionPipeline

REPLAY_JOURNAL = "replay-journal.jsonl"
VERDICT_NAME = "replay.json"

# Two ticks: the tick phase (up to one 0.1 s tick) plus dispatch latency.
REMAINING_TOL_S = 2 * BROADCAST_INTERVAL_SECONDS
# The records that ARE the session's decisions: every vision verdict, the #33
# provisional and its resolutions, the hub's own policies, and every command's
# effect. Heartbeats, voice transcripts and summaries are telemetry.
DECISION_PREFIXES = (
    "vision_",
    "shot_provisional",
    "break_adjudicated",
    "settle_synthesized",
    "scratch_",
    "tick_transition",
    "cmd_",
)
# Compared exactly on every decision record that carries them.
VERDICT_FIELDS = (
    "state",
    "rack_shots",
    "provisional",
    "rejected",
    "accepted",
    "reason",
    "tier",
    "verdict",  # #63's scratch_list
    "row",  # #64's scratch_list: which row of the ball-in-hand table
    "by",  # #63's scratch_hold / scratch_call
)
MAX_DIFFS = 50


@dataclass
class VirtualClock:
    """The hub's ``now``/``wall`` pair during a replay: monotonic seconds, and
    the wall time the live session had at that monotonic instant."""

    now: float
    started_mono: float
    started_wall: datetime

    def mono(self) -> float:
        return self.now

    def wall(self) -> datetime:
        return self.started_wall + timedelta(seconds=self.now - self.started_mono)


def to_mono(header: dict[str, Any], value: str) -> float:
    """A ``--from``/``--to`` value as session mono: seconds since the session
    started, or an ISO-8601 wall time (UTC unless it says otherwise)."""
    started_mono = float(header["started_mono"])
    try:
        return started_mono + float(value)
    except ValueError:
        pass
    when = datetime.fromisoformat(value)
    started = datetime.fromisoformat(header["started_wall"])
    if when.tzinfo is None:
        when = when.replace(tzinfo=started.tzinfo)
    return started_mono + (when - started).total_seconds()


def replay(
    log: SessionLog,
    out: Path,
    *,
    mono_from: float | None = None,
    mono_to: float | None = None,
) -> int:
    """Replay ``log`` into the journal ``out``; returns the frames fed.

    Always runs from the top — tracker and clock state need the whole history,
    and a session replays in about a thirtieth of its length from a terminal
    (36 s for 17 minutes of play; several times slower under the archive
    agent's background scheduling) — but stops after ``mono_to`` and keeps
    only the records from ``mono_from`` on.
    """
    out.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="cueclock-replay-") as tmp:
        frames = asyncio.run(_replay(log, out, Path(tmp), mono_to))
    if mono_from is not None or mono_to is not None:
        lo = float("-inf") if mono_from is None else mono_from
        hi = float("inf") if mono_to is None else mono_to
        kept = [r for r in read_journal(out) if lo <= float(r["mono"]) <= hi]
        out.write_text("".join(json.dumps(r) + "\n" for r in kept))
    return frames


async def _replay(log: SessionLog, out: Path, tmp: Path, mono_to: float | None) -> int:
    header = log.header
    state = header["hub"]
    # The hub persists score and bracket as files; give it scratch copies of
    # what the live session started from, never the live config.
    paths: dict[str, Path | None] = {}
    for name in ("score", "bracket"):
        paths[name] = tmp / f"{name}.json"
        if state.get(name) is not None:
            paths[name].write_text(json.dumps(state[name]))  # type: ignore[union-attr]
    cal_path = tmp / "calibration.json"
    cal_path.write_text(json.dumps(header["calibration"]))
    clock = VirtualClock(
        now=float(header["started_mono"]),
        started_mono=float(header["started_mono"]),
        started_wall=datetime.fromisoformat(header["started_wall"]),
    )
    hub = Hub(
        score_path=paths["score"],
        journal_path=out,
        bracket_path=paths["bracket"],
        archive_dir=None,
        now=clock.mono,
        wall=clock.wall,
    )
    hub.clock.set_ruleset(get_ruleset(state["ruleset"]))
    hub.clock.set_mode(get_mode(state["mode"]))
    hub.vision_enabled = bool(state["vision"])
    vision = VisionPipeline(
        TableCalibration.load(cal_path),
        None,
        detect_config=DetectConfig(**header["detect_config"]),
        cadence_hz=float(header["cadence_hz"]),
    )
    timeline = _Timeline(hub, clock, log.inputs())
    lists = deque(log.ball_lists())  # in the order they were written
    episode = False
    fed = 0
    for frame in log.frames():
        if mono_to is not None and frame.mono > mono_to:
            break
        await timeline.run_until(frame.mono)
        clock.now = frame.mono
        events = vision.advance(frame.t, frame.detections, frame.churn, frame.intrusions)
        calls, episode = hub_calls(hub, vision, frame.t, events, episode)
        hub.note_frame(frame.t, balls=len(vision.last_balls))
        hub.note_motion(vision.last_motion)
        for call in calls:
            await call
        # The ball lists (#54) are a replayed input (#63): each reached the
        # hub right behind the hub calls of the frame its record names
        # (`after`). Lists logged before `after` existed (checkpoints
        # 127-131) go in behind their own frame.
        while lists and _due(lists[0], frame.frame_i, frame.t):
            record = lists.popleft()
            await hub.ball_list(
                float(record["t"]),
                str(record["reason"]),
                BallList.from_dict(record),
                record.get("tracker"),
            )
        fed += 1
    await timeline.run_until(timeline.last_input_mono if mono_to is None else mono_to)
    return fed


def _due(record: dict[str, Any], frame_i: int, t: float) -> bool:
    after = record.get("after")
    return int(after) <= frame_i if after is not None else float(record["t"]) <= t + 1e-6


class _Timeline:
    """Logged inputs and tick-loop beats, merged in mono order."""

    def __init__(self, hub: Hub, clock: VirtualClock, inputs: list[dict[str, Any]]) -> None:
        self._hub = hub
        self._clock = clock
        self._inputs = sorted(inputs, key=lambda r: float(r["mono"]))
        self._next_input = 0
        self._next_tick = clock.now
        self.last_input_mono = float(self._inputs[-1]["mono"]) if self._inputs else clock.now

    async def run_until(self, mono: float) -> None:
        while True:
            pending = (
                self._inputs[self._next_input] if self._next_input < len(self._inputs) else None
            )
            input_mono = float(pending["mono"]) if pending is not None else float("inf")
            if min(input_mono, self._next_tick) > mono:
                return
            if pending is not None and input_mono <= self._next_tick:
                self._next_input += 1
                self._clock.now = input_mono
                await self._apply(pending)
            else:
                self._clock.now = self._next_tick
                self._hub.tick()
                self._next_tick += BROADCAST_INTERVAL_SECONDS

    async def _apply(self, record: dict[str, Any]) -> None:
        kwargs = {
            k: tuple(v) if isinstance(v, list) else v for k, v in record["kwargs"].items()
        }  # JSON turned tuples (a reported score) into lists
        try:
            await getattr(self._hub, record["method"])(**kwargs)
        except BracketError as exc:
            # A bracket call the live session refused, or one that needs files
            # a replay does not have (an archived bracket): journaled, not fatal.
            self._hub.journal.log("replay_input_failed", method=record["method"], error=str(exc))


def read_journal(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def decisions(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in records if str(r.get("kind", "")).startswith(DECISION_PREFIXES)]


def _keys(records: list[dict[str, Any]]) -> list[tuple[object, ...]]:
    """What each decision record is compared on: its kind and verdict fields,
    with the rack's shot count as its step from the record before.

    The step is what the record did to the count. Compared as a total, one
    settle that newer code no longer counts made every later record of the
    rack differ, the matcher then paired each shot with the one after it,
    and `review check` called verdicts regressed that nothing had touched
    (#69: 34 such differences behind 3 changed decisions). A count going
    down is a reset (New Rack, a break taken back), whatever it stood at.
    """
    keys: list[tuple[object, ...]] = []
    before = 0
    for record in records:
        fields = {f: record.get(f) for f in VERDICT_FIELDS}
        shots = record.get("rack_shots")
        if isinstance(shots, int):
            fields["rack_shots"] = shots - before if shots >= before else "reset"
            before = shots
        keys.append((record["kind"], *fields.values()))
    return keys


def _brief(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if record is None:
        return None
    keep = ("wall", "mono", "kind", "remaining", *VERDICT_FIELDS)
    return {k: record[k] for k in keep if k in record}


def _summary(record: dict[str, Any] | None) -> str:
    if record is None:
        return "none"
    parts = [str(record["kind"]), str(record.get("state", ""))]
    if isinstance(record.get("remaining"), int | float):
        parts.append(f"{record['remaining']:.1f}")
    if "rack_shots" in record:
        parts.append(f"rack_shots {record['rack_shots']}")
    parts += [f"{f} {record[f]}" for f in VERDICT_FIELDS[2:] if record.get(f) is not None]
    return " ".join(p for p in parts if p)


def _diff(op: str, live: dict[str, Any] | None, replayed: dict[str, Any] | None) -> dict[str, Any]:
    """One difference: where it is (the live record's wall/mono when there is
    one), what it is in a line, and both records in brief."""
    anchor = live if live is not None else replayed or {}
    return {
        "op": op,
        "wall": anchor.get("wall"),
        "mono": anchor.get("mono"),
        "summary": f"live: {_summary(live)}; replay: {_summary(replayed)}",
        "live": _brief(live),
        "replay": _brief(replayed),
    }


def compare(live: list[dict[str, Any]], replayed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Decision-record differences between two journals, in session order.

    A record kind the live journal never wrote is left out of the replay's
    side. The code that ran live had no such record, so finding one in the
    replay is newer reporting, not a different decision: checkpoint 132 began
    journaling a `scratch_list` line at every settle, and `review check` then
    called every older "right" verdict regressed for holding one. What newer
    logic *did* to the clock still shows in the kinds both journals hold: a
    refused settle, a reverted freeze, a different ``remaining``.

    A decision that changes the shot count is one difference, at the record
    that counted (`_keys`), not one at every record until the next rack.
    """
    a = decisions(live)
    written = {r["kind"] for r in a}
    b = [r for r in decisions(replayed) if r["kind"] in written]
    diffs: list[dict[str, Any]] = []
    matcher = difflib.SequenceMatcher(a=_keys(a), b=_keys(b), autojunk=False)
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            for same_a, same_b in zip(a[i1:i2], b[j1:j2], strict=True):
                lr, rr = same_a.get("remaining", 0.0), same_b.get("remaining", 0.0)
                if abs(float(lr) - float(rr)) > REMAINING_TOL_S:
                    diffs.append(_diff("remaining", same_a, same_b))
            continue
        for k in range(max(i2 - i1, j2 - j1)):
            la = a[i1 + k] if i1 + k < i2 else None
            rb = b[j1 + k] if j1 + k < j2 else None
            what = (
                "changed"
                if la is not None and rb is not None
                else "missing"
                if rb is None
                else "extra"
            )
            diffs.append(_diff(what, la, rb))
    return diffs


def verify(log: SessionLog, live_journal: Path, out: Path) -> dict[str, Any]:
    """Replay ``log``, compare with ``live_journal``, write and return the verdict."""
    replay(log, out)
    live, replayed = read_journal(live_journal), read_journal(out)
    sha, dirty = git_state()
    diffs = compare(live, replayed)
    verdict = {
        "ok": not diffs,
        "code_sha": sha,
        "same_code": sha is not None
        and sha == log.header.get("git_sha")
        and not log.header.get("git_dirty")
        and not dirty,
        "diffs": diffs[:MAX_DIFFS],
        "diff_count": len(diffs),
        "live_decisions": len(decisions(live)),
        "replay_decisions": len(decisions(replayed)),
    }
    (log.root / VERDICT_NAME).write_text(json.dumps(verdict, indent=2) + "\n")
    return verdict


def live_journal_of(log: SessionLog) -> Path:
    """The live journal: the archived copy beside the log, else `logs/`."""
    archived = log.root / "journal.jsonl"
    return archived if archived.exists() else Path("logs") / f"session-{log.stamp}.jsonl"
