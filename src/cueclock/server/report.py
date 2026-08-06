"""Session accuracy report: the clock's own missed-strike compensation, read back.

The accuracy work in this repo was ranked by argument for weeks because nothing
produced a **denominator** — "the clock missed a shot" was only ever observed
when Steven noticed and hit Flag, which samples the failures he saw and nothing
else. The 2026-08-02 answer was a Stream Deck key pressed every shot; it was
used in two sessions and never again, because a key you must remember at the
table is a key that stops being pressed (#19, retired 2026-08-05).

**The denominator was already being written.** Two detectors run per shot, and
only one of them is fragile:

    "he hit the ball"          needs to identify the cue ball and catch it
                               leaving its spot, through the moment the stick
                               and hand are one blob with it.  THIS one misses.

    "everything has stopped"   needs no ball identity at all.  Reliable.

When the first fails and the second fires, `ShotClock.balls_settled` reasons
that balls came to rest without a detected shot, so a shot must have happened,
and bumps the rack shot count (`clock/state.py`, 2026-07-31 — written for
correctness, because a short count replayed the WNT §5 60 s window). Every
journal record carries `rack_shots`. Therefore:

> a `vision_balls_settled` record whose `rack_shots` exceeds the preceding
> record's **is the engine confessing it missed a strike**.

Settles recorded in `IDLE` are excluded: those are racking motion the clock
correctly ignored, not shots.

## Why derived, never accumulated

Nothing here runs live and nothing is tallied into a counter. The journal is
the system of record, so the rate is recomputed from it on demand — which means
it survives a crash, a `kill -9`, a dead deck daemon, and can be recomputed
over every session ever recorded the day the reader improves.

## Honest limits

- **A floor, not a ceiling.** A shot whose *settle* was also missed leaves no
  trace here at all.
- **Phantoms are candidates, not verdicts.** A frozen clock with no shot behind
  it cannot be proven from the journal. What can be measured is an implausibly
  short shot→settle interval, and that is reported as a review candidate.
- **The press stream is gone**, so the two 2026-08-03 sessions that carry
  `ground_truth_press` records are read here like any other: by compensation.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median

SETTLE_KIND = "vision_balls_settled"
SHOT_KIND = "vision_shot_taken"
SCRATCH_KIND = "vision_scratch"
FLAG_KIND = "flag_dump"
HEARTBEAT_KIND = "detect_heartbeat"
SUMMARY_KIND = "session_summary"

# Emitted-then-gated shots. "Vision saw it, the clock declined it" is a
# different bug from "vision never saw it", and only the second is a miss.
GATED_SHOT_KINDS = {
    "vision_shot_taken_ignored_idle": "gated_idle",
    "vision_shot_taken_no_clock": "gated_no_clock",
    "vision_shot_taken_killed": "gated_kill_switch",
}

# The head of a session, judged separately. Not a statistical unit — a
# perception one: a miss on the first shot after walking downstairs costs far
# more than the same miss mid-rack (Steven, 2026-08-05). Measured over every
# session to date, 12% opened with a miss inside this many shots.
OPENING_SHOTS = 3

# Shot → settle below this is a phantom *candidate*: the clock froze and
# released with no time for a roll-out. Anchored to the measured roll-out range
# on real shots — 2.27-8.62 s (2026-08-03 session, `logs/session-20260803-121204`)
# — so 1 s sits far below the shortest real one. A review candidate, never a
# verdict: the journal cannot prove a phantom.
PHANTOM_SETTLE_S = 1.0

# Detect-rate floor for calling a shot's detection starved. The live loop runs
# 70-78 Hz healthy (stride 2 of the camera's 156 fps); flag dumps and corpus
# batteries drag it to 11-30 Hz, and a shot detected down there failed for a
# reason that is not the vision logic.
STARVED_HZ = 50.0

_SINCE = re.compile(r"^(\d+(?:\.\d+)?)([hdw])$")
_SINCE_SECONDS = {"h": 3600.0, "d": 86400.0, "w": 604800.0}


def parse_since(text: str) -> float:
    """`"7d"` → seconds. Accepts hours, days, weeks."""
    match = _SINCE.match(text.strip().lower())
    if match is None:
        raise ValueError(f"expected a span like 7d / 24h / 2w, got {text!r}")
    return float(match.group(1)) * _SINCE_SECONDS[match.group(2)]


@dataclass(frozen=True)
class Shot:
    """One shot, as the session recorded it.

    Created at the *settle*, because that is the event that exists whether or
    not the strike was detected — which is the whole point.
    """

    index: int  # 1-based within the session
    t: float | None  # capture clock, at the settle
    seen: bool  # False = the strike was missed and the settle stood in
    settle_gap_s: float | None  # strike → settle, when the strike was seen
    hz: float | None  # detect rate reported just before it

    @property
    def starved(self) -> bool:
        return self.hz is not None and self.hz < STARVED_HZ

    @property
    def phantom_candidate(self) -> bool:
        return self.settle_gap_s is not None and self.settle_gap_s < PHANTOM_SETTLE_S


@dataclass
class SessionReport:
    """One journal, read for accuracy."""

    path: Path
    shots: list[Shot] = field(default_factory=list)
    scratches: int = 0
    flags: int = 0
    racking_settles: int = 0
    gated: dict[str, int] = field(default_factory=dict)
    hz: list[float] = field(default_factory=list)
    torn_lines: int = 0

    @property
    def total(self) -> int:
        return len(self.shots)

    @property
    def missed(self) -> list[Shot]:
        return [s for s in self.shots if not s.seen]

    @property
    def miss_rate(self) -> float | None:
        return len(self.missed) / self.total if self.total else None

    @property
    def opening_miss(self) -> bool:
        """Did a miss land in the first `OPENING_SHOTS` shots of the session?"""
        return any(not s.seen for s in self.shots[:OPENING_SHOTS])

    @property
    def starved_misses(self) -> list[Shot]:
        """Misses that coincided with a starved detect loop — not vision's fault."""
        return [s for s in self.missed if s.starved]

    @property
    def phantom_candidates(self) -> list[Shot]:
        return [s for s in self.shots if s.phantom_candidate]


def _number(record: dict[str, object], key: str) -> float | None:
    value = record.get(key)
    return float(value) if isinstance(value, int | float) else None


def build(path: Path) -> SessionReport:
    """Read one session journal into a `SessionReport`.

    Unknown kinds and unknown fields are ignored, per the journal's additive
    contract; a torn final line from a killed session is counted, not an error.
    """
    report = SessionReport(path=path)
    prev_rack: float | None = None
    pending_shot_t: float | None = None
    last_hz: float | None = None

    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            report.torn_lines += 1
            continue
        if not isinstance(record, dict):
            continue
        kind = record.get("kind")
        rack = _number(record, "rack_shots")

        if kind == HEARTBEAT_KIND:
            hz = _number(record, "hz")
            if hz is not None:
                last_hz = hz
                report.hz.append(hz)
        elif kind == SHOT_KIND:
            pending_shot_t = _number(record, "frame_t") or _number(record, "t")
        elif kind == SCRATCH_KIND:
            report.scratches += 1
        elif kind == FLAG_KIND:
            report.flags += 1
        elif isinstance(kind, str) and kind in GATED_SHOT_KINDS:
            label = GATED_SHOT_KINDS[kind]
            report.gated[label] = report.gated.get(label, 0) + 1
        elif kind == SETTLE_KIND:
            if record.get("state") == "IDLE":
                # Racking motion: `balls_settled` returned early, so the clock
                # never counted it and it is not a shot.
                report.racking_settles += 1
            else:
                settle_t = _number(record, "frame_t") or _number(record, "t")
                # The engine bumped the count at the settle ⇒ no strike was
                # seen. The first record of a session has nothing to compare
                # against, so it is credited as seen.
                seen = prev_rack is None or rack is None or rack <= prev_rack
                gap = (
                    settle_t - pending_shot_t
                    if seen and settle_t is not None and pending_shot_t is not None
                    else None
                )
                report.shots.append(
                    Shot(
                        index=len(report.shots) + 1,
                        t=settle_t,
                        seen=seen,
                        settle_gap_s=gap,
                        hz=last_hz,
                    )
                )
                pending_shot_t = None

        if rack is not None:
            prev_rack = rack
    return report


def find_journals(logs_dir: Path, since_s: float | None = None) -> list[Path]:
    """Session journals, oldest first, optionally limited to a recent span."""
    paths = sorted(logs_dir.glob("session-*.jsonl"))
    if since_s is None:
        return paths
    newest = max((p.stat().st_mtime for p in paths), default=0.0)
    return [p for p in paths if p.stat().st_mtime >= newest - since_s]


def summary_detail(report: SessionReport) -> dict[str, object]:
    """The `session_summary` journal record's payload."""
    rate = report.miss_rate
    return {
        "shots": report.total,
        "missed": len(report.missed),
        "miss_rate": round(rate, 4) if rate is not None else None,
        "opening_miss": report.opening_miss,
        "scratches": report.scratches,
        "flags": report.flags,
    }


def _rate(report: SessionReport) -> str:
    rate = report.miss_rate
    return f"{rate:6.2%}" if rate is not None else "     —"


def format_session(report: SessionReport) -> str:
    """Human-readable single-session summary."""
    out: list[str] = []
    out.append(f"{report.path.name}")
    out.append("=" * 64)
    if not report.total:
        out.append("  no shots in this journal")
        return "\n".join(out)

    out.append(f"  shots              {report.total}")
    out.append(f"  MISSED             {len(report.missed)}   {_rate(report).strip()}")
    if report.missed:
        marks = ", ".join(
            f"#{s.index}" + (f" @{s.t:.0f}s" if s.t is not None else "") for s in report.missed
        )
        out.append(f"    at               {marks}")
    if report.opening_miss:
        out.append(f"    ⚠ a miss landed in the first {OPENING_SHOTS} shots of the session")
    if report.starved_misses:
        out.append(
            f"    {len(report.starved_misses)} of those ran below {STARVED_HZ:.0f} Hz "
            "— starved detect loop, not a vision failure"
        )
    out.append("")

    if report.phantom_candidates:
        out.append(
            f"  phantom candidates {len(report.phantom_candidates)}   "
            f"(settled <{PHANTOM_SETTLE_S:.0f}s after the strike — review, not a verdict)"
        )
    out.append(f"  scratches          {report.scratches}")
    if report.total:
        out.append(
            f"    rate             1 per {report.total / max(report.scratches, 1):.0f} shots"
        )
    out.append(f"  flag dumps         {report.flags}")
    if report.racking_settles:
        out.append(f"  racking settles    {report.racking_settles}   (ignored in IDLE, not shots)")
    for label, count in sorted(report.gated.items()):
        out.append(f"  {label:<18} {count}   (vision saw it, the clock declined it)")
    if report.hz:
        ordered = sorted(report.hz)
        out.append("")
        out.append(
            f"  detect loop        median {median(ordered):.1f} Hz   "
            f"p05 {ordered[len(ordered) // 20]:.1f} Hz   min {ordered[0]:.1f} Hz"
        )
    if report.torn_lines:
        out.append(f"  torn lines         {report.torn_lines}   (killed session — not an error)")
    return "\n".join(out)


def format_rollup(reports: list[SessionReport]) -> str:
    """Per-session table plus the total. The total is the number that matters."""
    played = [r for r in reports if r.total]
    out: list[str] = []
    out.append(f"{'session':<34}{'shots':>7}{'missed':>8}{'miss':>8}{'scr':>6}{'flag':>6}")
    out.append("-" * 69)
    for report in played:
        out.append(
            f"{report.path.name:<34}{report.total:>7}{len(report.missed):>8}"
            f"{_rate(report):>8}{report.scratches:>6}{report.flags:>6}"
        )
    shots = sum(r.total for r in played)
    missed = sum(len(r.missed) for r in played)
    scratches = sum(r.scratches for r in played)
    out.append("-" * 69)
    out.append(
        f"{'TOTAL':<34}{shots:>7}{missed:>8}{missed / shots if shots else 0:>7.2%}{scratches:>6}"
    )
    if not shots:
        return "\n".join(out)

    out.append("")
    out.append(f"  miss rate          {missed / shots:.2%}")
    if missed:
        out.append(f"  felt as            a miss every {shots / missed / 14:.1f} racks (14 shots)")
    opening = [r for r in played if r.opening_miss]
    out.append(
        f"  bad first impression  {len(opening)}/{len(played)} sessions "
        f"({len(opening) / len(played):.0%}) opened with a miss in {OPENING_SHOTS} shots"
    )
    if scratches:
        out.append(f"  scratches          {scratches}   (1 per {shots / scratches:.0f} shots)")
    return "\n".join(out)
