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
from dataclasses import fields
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cueclock.vision.events import Event

GOLDENS = Path(__file__).parent.parent / "goldens"

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


def _mismatch(actual: list[dict[str, Any]], golden: list[dict[str, Any]]) -> str | None:
    """The first difference between two timelines, or None if they agree."""
    actual_types = [r["type"] for r in actual]
    golden_types = [r["type"] for r in golden]
    if actual_types != golden_types:
        return f"event sequence changed\n  golden: {golden_types}\n  actual: {actual_types}"

    for i, (got, want) in enumerate(zip(actual, golden, strict=True)):
        label = f"event {i} ({want['type']}@{want['t']:.2f})"
        if abs(got["t"] - want["t"]) > T_TOL_S:
            return f"{label}: fired at {got['t']:.3f}s, golden {want['t']:.3f}s"
        for key in sorted(set(got) | set(want)):
            if key in ("type", "t") or key in UNASSERTED:
                continue
            a, b = got.get(key), want.get(key)
            if isinstance(a, float) and isinstance(b, float):
                if abs(a - b) > max(SPEED_ABS_TOL_MMS, abs(b) * SPEED_REL_TOL):
                    return f"{label}: {key} {a:.1f}, golden {b:.1f}"
            elif a != b:
                return f"{label}: {key} {a!r}, golden {b!r}"
    return None


def check(scenario: str, reference: str, events: list["Event"]) -> None:
    """Compare a replay against its golden, or rewrite it under --update-goldens.

    Raises AssertionError with the first difference — full timelines included,
    since the whole point is to make the collateral of a threshold move
    readable without a second run.
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
    problem = _mismatch(actual, golden["events"])
    if problem is not None:
        raise AssertionError(
            f"{scenario}: {problem}\n"
            f"  golden: {json.dumps(golden['events'])}\n"
            f"  actual: {json.dumps(actual)}\n"
            f"If the change is intended, regenerate with --update-goldens and "
            f"cite the moved timelines in the CHANGELOG entry."
        )
