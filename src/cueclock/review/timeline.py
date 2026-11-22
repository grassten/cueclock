"""A session's journal around one instant, compactly: `cueclock review timeline` (#70).

The review page shows a moment's journal lines as sentences for Steven. This
is the same window for whoever diagnoses a wrong verdict afterwards, so it
keeps every field a decision was made from (`held`, `rejected`, `reason`,
`row`, `hand`, `counted`, …) and drops what only repeats:

- `detect_heartbeat` lines (five a second; the detect rate is a dossier fact);
- `voice_final` lines with no words in them (mic noise, checkpoint 121);
- a `ball_list` that reads exactly what the previous one read.

In session 20261121-153046 those three were over half the journal's lines.
`everything=True` keeps them all.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path

from cueclock.review import moments as finder
from cueclock.review.app import clock_after

# Fields the line's fixed columns already show.
_SHOWN = {"wall", "mono", "kind", "state", "remaining", "rack_shots"}

# A moment id is `<kind>-<mono in ms>` (`moments._moment`).
_MOMENT_ID = re.compile(r"^[a-z_]+-(\d+)$")


def center_of(target: str) -> float:
    """The mono time a moment id or a plain number names."""
    match = _MOMENT_ID.match(target)
    if match:
        return int(match.group(1)) / 1000
    try:
        return float(target)
    except ValueError:
        raise ValueError(
            f"expected a moment id like in_hand_shot-1717566108 or a mono time, got {target!r}"
        ) from None


def records(journal: Path) -> list[dict[str, object]]:
    """The journal's records that carry a wall and a mono time (torn lines skipped)."""
    out = []
    for line in journal.read_text().splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(record, dict)
            and isinstance(record.get("wall"), str)
            and isinstance(record.get("mono"), int | float)
        ):
            out.append(record)
    return out


def _list_reading(record: dict[str, object]) -> tuple[object, ...]:
    return (record.get("balls"), record.get("arm"), record.get("pack"))


def filtered(found: Iterable[dict[str, object]]) -> Iterator[dict[str, object]]:
    """Drop heartbeats, wordless voice finals and repeated ball lists."""
    last_list: tuple[object, ...] | None = None
    for record in found:
        kind = record.get("kind")
        if kind == "detect_heartbeat":
            continue
        if kind == "voice_final" and not record.get("text"):
            continue
        if kind == "ball_list":
            reading = _list_reading(record)
            if reading == last_list:
                continue
            last_list = reading
        yield record


def window(
    found: list[dict[str, object]],
    center: float,
    before: float = finder.PRE_S,
    after: float = finder.POST_S,
    everything: bool = False,
) -> list[dict[str, object]]:
    """The records from `before` s ahead of `center` (mono) to `after` s past it.

    Repeats are judged inside the window, so its first ball list always shows:
    it is what the table read as the window opened.
    """
    inside = [r for r in found if center - before <= float(r["mono"]) <= center + after]  # type: ignore[arg-type]
    return inside if everything else list(filtered(inside))


def line(record: dict[str, object], center: float) -> str:
    """One record: offset from the center, local time, the clock, the rack's
    shot count, the kind, and every other field as compact JSON."""
    offset = float(record["mono"]) - center  # type: ignore[arg-type]
    local = datetime.fromisoformat(str(record["wall"])).astimezone().strftime("%H:%M:%S.%f")[:-3]
    rest = {k: v for k, v in record.items() if k not in _SHOWN}
    mark = ">" if abs(offset) < 0.0005 else " "
    return (
        f"{mark}{offset:+7.2f} {local} {clock_after(record):<13} "
        f"r{record.get('rack_shots', '-')!s:<3} {record.get('kind')!s:<26} "
        f"{json.dumps(rest, separators=(',', ':'))}"
    )


def render(found: list[dict[str, object]], center: float) -> str:
    return "\n".join(line(r, center) for r in found)
