"""Ground-truth report: press stream vs. detected shots (#19).

The accuracy work in this repo has always been ranked by argument, because
nothing produced a **denominator** — "the clock missed a shot" was only ever
observed when Steven noticed and hit Flag. One unconditional key pressed every
shot fixes that: presses are the ground truth, emitted ShotTaken events are the
prediction, and the difference is the error rate.

Offline by construction. The journal is already the system of record, so this
is a reader over it — no new storage, nothing running live, and a session can
be re-analysed with better logic later.

## What is derived, and from what

    press with no shot        vision MISSED a shot
    shot with no press        vision invented one (PHANTOM)
    pair with a long delay    late detection — the shot was only caught by the
                              settle-counts-the-shot fallback

The player classifies nothing; every one of those is a diff. A paired press is
not automatically a pass, which is why the delay distribution is printed.

## The self-arming window

The key is always live, but Steven will not always press it — and "shot with no
press" would then flag every unmarked real shot as a phantom. Making him
remember a mode toggle is the failure this whole design avoids, so **the
presses arm the diff themselves**: they are grouped into runs, split wherever
the gap exceeds `IDLE_SPLIT_S`, and each run's window spans its first press to
its last. Shots outside a window are ignored entirely.

Deliberately conservative at the trailing edge — no extension past the final
press — so a genuine phantom just after the last press of a run goes uncounted.
That is the right bias: under-report phantoms rather than manufacture them out
of a stretch he simply was not marking.

## Honest limits

The press stream has its own error rate (a forgotten press reads as a phantom,
a double tap as a miss), so **a disagreement is a review candidate, not a
label**. And `PAIR_WINDOW_S` is a starting guess, not a measurement: it wants
re-deriving from the first real session's paired-delay distribution, which is
what this report prints. Nothing here auto-flags a "late" verdict against an
invented threshold.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median

# A press lands after the stroke, when the hand comes off the cue. 5 s is
# generous on purpose — see the module docstring: this is the provisional
# value, to be replaced by a measurement from the printed distribution.
PAIR_WINDOW_S = 5.0

# Gap between consecutive presses that ends a run. Steven racks, walks, drinks
# water; 3 minutes is well past any of that and well under "I stopped
# bothering". Also provisional.
IDLE_SPLIT_S = 180.0

# Every journal kind that means the vision core emitted a ShotTaken. The
# suffixed ones were emitted and then *gated* by the clock — which is a real
# outcome worth separating, because "vision saw it, the clock declined it" and
# "vision never saw it" are different bugs with different fixes.
SHOT_KINDS = {
    "vision_shot_taken": "clocked",
    "vision_shot_taken_ignored_idle": "gated_idle",
    "vision_shot_taken_no_clock": "gated_no_clock",
    "vision_shot_taken_killed": "gated_kill_switch",
}
PRESS_KIND = "ground_truth_press"


@dataclass(frozen=True)
class Event:
    """A press or a detected shot, on the capture clock."""

    t: float
    kind: str
    detail: dict[str, object] = field(default_factory=dict)


@dataclass
class Report:
    presses: list[Event] = field(default_factory=list)
    shots: list[Event] = field(default_factory=list)
    unanchored_presses: int = 0
    windows: list[tuple[float, float]] = field(default_factory=list)
    pairs: list[tuple[Event, Event]] = field(default_factory=list)
    missed: list[Event] = field(default_factory=list)
    phantom: list[Event] = field(default_factory=list)

    @property
    def detected(self) -> int:
        return len(self.pairs)

    @property
    def accuracy(self) -> float | None:
        """Detected over pressed, inside the armed windows."""
        total = len(self.pairs) + len(self.missed)
        return len(self.pairs) / total if total else None

    @property
    def delays(self) -> list[float]:
        return sorted(shot.t - press.t for press, shot in self.pairs)


def _anchor(record: dict[str, object]) -> float | None:
    """The capture-clock stamp, or None when the record has no anchor."""
    for key in ("frame_t", "t"):
        value = record.get(key)
        if isinstance(value, int | float):
            return float(value)
    return None


def load(path: Path) -> tuple[list[Event], list[Event], int]:
    """Presses and detected shots from a journal file, plus unanchored presses.

    Unknown kinds and unknown fields are ignored, per the journal's additive
    contract.
    """
    presses: list[Event] = []
    shots: list[Event] = []
    unanchored = 0
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue  # a torn final line from a killed session is not an error
        if not isinstance(record, dict):
            continue
        kind = record.get("kind")
        anchor = _anchor(record)
        if kind == PRESS_KIND:
            if anchor is None:
                unanchored += 1  # no vision running: nothing to align against
            else:
                presses.append(Event(anchor, PRESS_KIND, record))
        elif isinstance(kind, str) and kind in SHOT_KINDS and anchor is not None:
            shots.append(Event(anchor, kind, record))
    presses.sort(key=lambda e: e.t)
    shots.sort(key=lambda e: e.t)
    return presses, shots, unanchored


def arm_windows(
    presses: list[Event], idle_split_s: float = IDLE_SPLIT_S
) -> list[tuple[float, float]]:
    """Press runs → the windows the diff is allowed to judge.

    First press to last press, with no trailing extension. See the module
    docstring for why the trailing edge is deliberately tight.
    """
    if not presses:
        return []
    windows: list[tuple[float, float]] = []
    start = prev = presses[0].t
    for press in presses[1:]:
        if press.t - prev > idle_split_s:
            windows.append((start, prev))
            start = press.t
        prev = press.t
    windows.append((start, prev))
    return windows


def _in_windows(t: float, windows: list[tuple[float, float]], lead: float = 0.0) -> bool:
    """Is `t` inside an armed window, optionally reaching `lead` before it?

    The lead exists only for pairing — see `build`. Phantom judgement always
    uses the strict window.
    """
    return any(lo - lead <= t <= hi for lo, hi in windows)


def build(
    path: Path, pair_window_s: float = PAIR_WINDOW_S, idle_split_s: float = IDLE_SPLIT_S
) -> Report:
    """Diff the press stream against detected shots.

    A sequence diff in time order, not a global optimum: each press takes the
    nearest unpaired shot within `pair_window_s`. Shots may precede their press
    (the press comes after the stroke) or follow it (a late detection), so the
    search runs both ways.
    """
    presses, shots, unanchored = load(path)
    windows = arm_windows(presses, idle_split_s)
    report = Report(presses=presses, shots=shots, unanchored_presses=unanchored, windows=windows)

    # Two different spans, and the asymmetry is the point.
    #
    # PAIRING reaches back a pair window before the first press, because a
    # press always lands *after* its stroke — the hand has to come off the cue.
    # Without that reach the opening shot of every run sits outside the window
    # and its press reads as a miss.
    #
    # PHANTOM judgement uses the strict window and never reaches past the last
    # press. A shot just after Steven stopped pressing is not evidence that
    # vision invented anything; it is evidence he stopped pressing. Under-report
    # phantoms, never manufacture them.
    pairable = [s for s in shots if _in_windows(s.t, windows, lead=pair_window_s)]
    taken: set[int] = set()
    for press in presses:
        best: int | None = None
        best_gap = pair_window_s
        for i, shot in enumerate(pairable):
            if i in taken:
                continue
            gap = abs(shot.t - press.t)
            if gap <= best_gap:
                best, best_gap = i, gap
        if best is None:
            report.missed.append(press)
        else:
            taken.add(best)
            report.pairs.append((press, pairable[best]))
    report.phantom = [
        s for i, s in enumerate(pairable) if i not in taken and _in_windows(s.t, windows)
    ]
    return report


def _percentile(values: list[float], fraction: float) -> float:
    index = min(len(values) - 1, round(fraction * (len(values) - 1)))
    return values[index]


def format_report(report: Report, pair_window_s: float = PAIR_WINDOW_S) -> str:
    """Human-readable summary. The delay distribution is the point of it."""
    out: list[str] = []
    out.append("Ground-truth report (#19)")
    out.append("=" * 60)
    out.append(f"  presses            {len(report.presses)}")
    out.append(f"  shots detected     {len(report.shots)}")
    if report.unanchored_presses:
        out.append(
            f"  unanchored presses {report.unanchored_presses}"
            "   (no vision running — excluded from the diff)"
        )
    out.append(f"  armed windows      {len(report.windows)}")
    for lo, hi in report.windows:
        out.append(f"    {lo:9.1f} s → {hi:9.1f} s   ({hi - lo:.0f} s)")
    out.append("")

    if not report.presses:
        out.append("  No presses in this journal — nothing to diff.")
        return "\n".join(out)

    out.append(f"  paired             {len(report.pairs)}")
    out.append(f"  MISSED             {len(report.missed)}   (pressed, no shot detected)")
    out.append(f"  PHANTOM            {len(report.phantom)}   (shot detected, no press)")
    accuracy = report.accuracy
    if accuracy is not None:
        out.append(
            f"  detection rate     {accuracy:.1%}   ({len(report.pairs)}/"
            f"{len(report.pairs) + len(report.missed)} pressed shots)"
        )
    out.append("")

    gated = [s for _, s in report.pairs if SHOT_KINDS[s.kind] != "clocked"]
    if gated:
        out.append(f"  detected but NOT clocked: {len(gated)}")
        for shot in gated:
            out.append(f"    {shot.t:9.2f} s   {SHOT_KINDS[shot.kind]}")
        out.append("")

    delays = report.delays
    if delays:
        out.append("  press → detection delay (this is what re-derives the pair window)")
        out.append(
            f"    min {delays[0]:+.2f} s   median {median(delays):+.2f} s   "
            f"p90 {_percentile(delays, 0.9):+.2f} s   max {delays[-1]:+.2f} s"
        )
        out.append(f"    pair window in force: {pair_window_s:.1f} s")
        slowest = sorted(report.pairs, key=lambda p: abs(p[1].t - p[0].t), reverse=True)[:5]
        out.append("    slowest pairs (review candidates for late detection):")
        for press, shot in slowest:
            out.append(
                f"      press {press.t:9.2f} s → shot {shot.t:9.2f} s ({shot.t - press.t:+.2f} s)"
            )
        out.append("")

    for label, events in (("MISSED", report.missed), ("PHANTOM", report.phantom)):
        if events:
            out.append(f"  {label}:")
            for event in events:
                speed = event.detail.get("speed")
                extra = f"   speed {speed}" if speed is not None else ""
                out.append(f"    {event.t:9.2f} s{extra}")
            out.append("")

    out.append("  A disagreement is a review candidate, not a label: a forgotten")
    out.append("  press reads as a phantom and a double tap reads as a miss.")
    return "\n".join(out)
