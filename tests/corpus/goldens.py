"""Golden event timelines: pin *when* events fire, not just how many.

`EXPECTED` in `test_corpus_events.py` asserts counts. Counts are blind to
timing: a tuning change that slides a settle 300 ms, swaps which ball fired,
or reorders a shot and a scratch passes green. That blindness is why threshold
work on this repo has historically been unmeasured — the collateral does not
show up until a live session.

A golden is the full event list for one clip, committed as JSON:

    {"scenario": "hanger_pot_0801",
     "reference": "empty_table_0801",
     "events": [{"type": "ShotTaken", "t": 11.59, "ball_id": 12,
                 "speed_mms": 1843.2, "newborn": false}, ...]}

Clips are local-only and gitignored; goldens are small text and **are**
committed, so a fresh clone still carries the record of what the engine did on
2026-08-02 even without the pixels.

## What is asserted, and what is not

- **the type sequence** — exact. Catches added, dropped and reordered events.
- **`t`** — within `T_TOL_S`. An event fires only on a detect frame (~12.8 ms
  at 78 Hz), so the tolerance is ~1.5 frames of slack for library drift.
- **`speed_mms`** — 2%, with a 5 mm/s floor. The IDLE break gate reads this
  number; a silent drift past `MATCH_BREAK_FLOOR_MMS` is a live failure.
- **`newborn`** — exact. Which strike path fired is a design-level fact.
- **`ball_id`** — *not* asserted. Recorded for forensics only; see below.

`ball_id` is a tracker-assigned birth counter. Any change to track birth order
renumbers every id in the clip, which would turn a harmless refactor into 50
red goldens. Reds that are routinely "fixed" by regenerating teach the reader
to regenerate reflexively, and the net stops catching anything. So the id is
written to the file — it is genuinely useful when reading a diff, and
`parked_vote_strike_0801` exists because the *wrong ball* fired — but the
assertion stays off.

## Regenerating

    uv run pytest tests/corpus -n 4 --update-goldens

Then **read the diff**. A golden diff is the measurement: it is the only place
that shows what a threshold move did to the rest of the corpus. Regenerating
without reading it is the same as not having goldens.
"""

import json
import os
import warnings
from dataclasses import fields
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cueclock.vision.events import Event

GOLDENS = Path(__file__).parent.parent / "goldens"
FAILURES_DIR = ".failures"
"""Where a red golden's full diff is written, under `tests/goldens/`. Gitignored:
it is per-run output, not a record. Stale files from an earlier run are
overwritten per scenario, never appended to."""

# One detect frame is ~12.8 ms at the 78 Hz nominal cadence, and an event can
# only fire on a frame boundary — so this is ~1.5 frames. Replay is
# deterministic on fixed inputs; the slack absorbs OpenCV/NumPy version drift,
# not run-to-run noise. It is 15x tighter than the 300 ms settle slide that
# motivated this file.
T_TOL_S = 0.02
SPEED_REL_TOL = 0.02
SPEED_ABS_TOL_MMS = 5.0

# Written to the file for forensics, never asserted. See the module docstring.
UNASSERTED = ("ball_id",)


class GoldenDrift(UserWarning):
    """A golden diff on a clip whose clock outcome is graded and correct (#34).

    Not a failure: since #33 an event-stream change matters only when it
    changes what the clock did. It is still news, so it warns rather than
    passing silently — `pytest` prints warnings in its end-of-run summary.
    """


def updating() -> bool:
    """True when the run was started with `--update-goldens`."""
    return bool(os.environ.get("CUECLOCK_UPDATE_GOLDENS"))


def path_for(scenario: str) -> Path:
    return GOLDENS / f"{scenario}.json"


def to_records(events: list["Event"]) -> list[dict[str, Any]]:
    """One JSON-able dict per event, rounded to a stable number of digits.

    Rounding is not cosmetic: unrounded float repr makes every regeneration a
    noisy diff even where nothing moved, which is exactly what stops people
    reading diffs.
    """
    records = []
    for event in events:
        record: dict[str, Any] = {"type": type(event).__name__}
        for f in fields(event):
            value = getattr(event, f.name)
            if value is None:
                continue
            record[f.name] = round(value, 4) if isinstance(value, float) else value
        records.append(record)
    return records


def write(scenario: str, reference: str, events: list["Event"]) -> None:
    GOLDENS.mkdir(parents=True, exist_ok=True)
    payload = {
        "scenario": scenario,
        "reference": reference,
        "events": to_records(events),
    }
    path_for(scenario).write_text(json.dumps(payload, indent=2) + "\n")


def _differences(actual: list[dict[str, Any]], golden: list[dict[str, Any]]) -> list[str]:
    """*Every* difference between two timelines — not just the first.

    Reporting one difference at a time turns triage into a rerun loop, and a
    cold corpus rerun is ~5 minutes. It also hides the shape of a change: one
    settle sliding is a timing question, but ten sliding together is a
    threshold move, and you cannot tell those apart from the first of them.
    """
    actual_types = [r["type"] for r in actual]
    golden_types = [r["type"] for r in golden]
    if actual_types != golden_types:
        # Field-level comparison needs the two lists to correspond; once the
        # sequence differs they do not, and pairing them by index invents
        # differences that say nothing.
        return [f"event sequence changed\n  golden: {golden_types}\n  actual: {actual_types}"]

    out: list[str] = []
    for i, (got, want) in enumerate(zip(actual, golden, strict=True)):
        label = f"event {i} ({want['type']}@{want['t']:.2f})"
        if abs(got["t"] - want["t"]) > T_TOL_S:
            delta = got["t"] - want["t"]
            out.append(
                f"{label}: fired at {got['t']:.3f}s, golden {want['t']:.3f}s "
                f"({delta:+.3f}s, {'later' if delta > 0 else 'earlier'})"
            )
        for key in sorted(set(got) | set(want)):
            if key in ("type", "t") or key in UNASSERTED:
                continue
            a, b = got.get(key), want.get(key)
            if isinstance(a, float) and isinstance(b, float):
                if abs(a - b) > max(SPEED_ABS_TOL_MMS, abs(b) * SPEED_REL_TOL):
                    pct = (a - b) / b * 100.0 if b else float("inf")
                    out.append(f"{label}: {key} {a:.1f}, golden {b:.1f} ({pct:+.1f}%)")
            elif a != b:
                out.append(f"{label}: {key} {a!r}, golden {b!r}")
    return out


def _mismatch(actual: list[dict[str, Any]], golden: list[dict[str, Any]]) -> str | None:
    """All differences as one message, or None if the timelines agree."""
    problems = _differences(actual, golden)
    return "\n".join(problems) if problems else None


def _timeline(actual: list[dict[str, Any]], golden: list[dict[str, Any]]) -> str:
    """Golden and actual side by side, one row per event, differences marked.

    Two JSON blobs are technically complete and practically unreadable — and
    reading them is the whole job, because the question a red golden asks is
    "is this better, worse, or merely different?" and that is answered by
    looking at the timeline, not by the pass/fail bit.
    """

    def render(rec: dict[str, Any] | None) -> str:
        if rec is None:
            return "-"
        extra = " ".join(
            f"{k}={rec[k]:.1f}" if isinstance(rec[k], float) else f"{k}={rec[k]}"
            for k in sorted(rec)
            if k not in ("type", "t")
        )
        return f"{rec['type']}@{rec['t']:.3f} {extra}".rstrip()

    rows = ["  {:<3} {:<52} {:<52}".format("#", "golden", "actual")]
    for i in range(max(len(golden), len(actual))):
        g = golden[i] if i < len(golden) else None
        a = actual[i] if i < len(actual) else None
        mark = " " if g == a else "*"
        rows.append(f"{mark} {i:<3} {render(g):<52} {render(a):<52}")
    return "\n".join(rows)


def _write_failure(scenario: str, report: str) -> Path:
    """Persist a golden diff so it survives however the run was captured.

    A cold battery is ~5 minutes, and the diff is the measurement. Losing it to
    a truncated pytest capture, an `xdist` worker's interleaving, or a `tail`
    on the log means paying that 5 minutes again to read something that was
    already computed. Written per scenario so parallel workers never collide.
    """
    directory = GOLDENS / FAILURES_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{scenario}.txt"
    path.write_text(report)
    return path


def check(scenario: str, reference: str, events: list["Event"], gate: bool = True) -> None:
    """Compare a replay against its golden, or rewrite it under --update-goldens.

    Raises AssertionError with the first difference — full timelines included,
    since the whole point is to make the collateral of a threshold move
    readable without a second run.

    ``gate=False`` (#34) reports the diff and writes the full report to
    ``.failures/`` without failing. That is for clips the clock-outcome
    criterion already grades: since #33 an event-stream change is only a
    failure when it changes what the clock did, and a golden diff on a clip
    whose clock is provably correct is *drift to read*, not a defect. The
    golden file itself is still the record — never regenerate one to make
    something green, and on `stroke_tug_strike_0801` in particular it is the
    only surviving record of when that stroke was.
    """
    if updating():
        write(scenario, reference, events)
        return

    path = path_for(scenario)
    if not path.exists():
        raise AssertionError(
            f"no golden for {scenario!r}. A clip in EXPECTED with no golden is "
            f"unpinned in time. Generate it with:\n"
            f"    uv run pytest tests/corpus -n 4 --update-goldens\n"
            f"then read the diff before committing."
        )

    golden = json.loads(path.read_text())
    actual = to_records(events)
    problems = _differences(actual, golden["events"])
    if problems:
        report = (
            f"{scenario} (reference {reference}): {len(problems)} difference(s)\n"
            + "\n".join(f"  - {p}" for p in problems)
            + "\n\n"
            + _timeline(actual, golden["events"])
            + "\n\n"
            f"  golden json: {json.dumps(golden['events'])}\n"
            f"  actual json: {json.dumps(actual)}\n"
        )
        written = _write_failure(scenario, report)
        if not gate:
            # Reported, not fatal — the clock grade is this clip's gate. A
            # warning rather than a bare print: pytest surfaces warnings in
            # its end-of-run summary, so drift stays visible on a green run
            # instead of being buried in captured stdout.
            print(f"GOLDEN DRIFT (clock outcome graded separately): {report}\nreport: {written}")
            warnings.warn(
                f"golden drift on {scenario}: {len(problems)} difference(s), "
                f"clock outcome still correct — full report at {written}",
                GoldenDrift,
                stacklevel=2,
            )
            return
        raise AssertionError(
            report + "\n"
            "A red golden is a diff to read, not a verdict. It has three causes and\n"
            "they need different answers:\n"
            "  1. a bug in the new code      -> fix the code (check the table, not\n"
            "                                   the pass/fail bit: is the new timing\n"
            "                                   physically right?)\n"
            "  2. a deliberate improvement   -> regenerate with --update-goldens and\n"
            "                                   cite the moved timelines in CHANGELOG\n"
            "  3. an over-specific assertion -> widen the tolerance, and say why\n"
            f"Full report also written to {written}."
        )
