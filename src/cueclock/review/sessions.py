"""Session directories, their footage and their verdicts, as the review page sees them.

Sessions live under two roots with the same per-session layout (#57 contract):
the internal SSD's `sessions.nosync/` and the T7's `cueclock_sessions/`. The T7
is often unmounted, so a missing root is simply empty — the page must still
come up with whatever is local.

Footage for a moment comes from the best source still on disk: the session's
own cut (`cuts/<id>.mp4`), else the full OBS recording that covered it (from
the global `recordings.jsonl`), else nothing — recordings are pruned by a byte
budget, and the verdict can still be given from the journal.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cueclock.archive import RECORDINGS_LOG, SESSIONS_ROOT
from cueclock.review.moments import MOMENTS_FILE, write_json

LOCAL_ROOT = SESSIONS_ROOT
RECORDINGS_FILE = RECORDINGS_LOG.name
VERDICTS_FILE = "verdicts.json"
VERDICTS = ("right", "wrong")

# A session directory is named by its journal stamp and nothing else; a moment
# id is `<kind>-<digits>`. Both are checked before either touches a path.
STAMP = re.compile(r"^\d{8}-\d{6}$")
MOMENT_ID = re.compile(r"^[a-z_]+-\d+$")


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


@dataclass(frozen=True)
class Session:
    stamp: str
    path: Path

    @property
    def started(self) -> datetime:
        return datetime.strptime(self.stamp, "%Y%m%d-%H%M%S").replace(tzinfo=UTC)

    def moments(self) -> list[dict[str, object]] | None:
        """`moments.json`, or None when the finder has not run on this session yet."""
        data = _read_json(self.path / MOMENTS_FILE)
        return [m for m in data if isinstance(m, dict)] if isinstance(data, list) else None

    def verdicts(self) -> dict[str, dict[str, object]]:
        data = _read_json(self.path / VERDICTS_FILE)
        if not isinstance(data, dict):
            return {}
        return {str(k): v for k, v in data.items() if isinstance(v, dict)}

    def unreviewed(self) -> int | None:
        moments = self.moments()
        if moments is None:
            return None
        judged = self.verdicts()
        return sum(1 for m in moments if m.get("id") not in judged)

    def set_verdict(self, moment_id: str, verdict: str | None, note: str) -> None:
        """Record (or, with `verdict=None`, withdraw) Steven's call on one moment."""
        verdicts = self.verdicts()
        if verdict is None:
            verdicts.pop(moment_id, None)
        else:
            verdicts[moment_id] = {
                "verdict": verdict,
                "note": note,
                "wall": datetime.now(UTC).isoformat(timespec="milliseconds"),
            }
        write_json(self.path / VERDICTS_FILE, verdicts)

    def cut(self, moment_id: str) -> Path | None:
        if not MOMENT_ID.match(moment_id):
            return None
        path = self.path / "cuts" / f"{moment_id}.mp4"
        return path if path.is_file() else None


def discover(roots: list[Path]) -> list[Session]:
    """Every session directory under `roots`, newest first.

    A root that is missing or unreadable (the T7 unplugged, or mid-unmount)
    contributes nothing. A stamp present under both roots — an archive copy
    still in flight — is listed once, from the earlier root, so pass the
    local root first.
    """
    found: dict[str, Session] = {}
    for root in roots:
        try:
            entries = sorted(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            if STAMP.match(entry.name) and entry.name not in found and entry.is_dir():
                found[entry.name] = Session(entry.name, entry)
    return sorted(found.values(), key=lambda s: s.stamp, reverse=True)


@dataclass(frozen=True)
class Recording:
    """One OBS recording, as the deck logged it."""

    index: int  # line number in recordings.jsonl — the URL names this, never a path
    path: Path
    start: datetime
    stop: datetime | None  # None while still recording, or if the stop was never logged

    def covers(self, when: datetime) -> bool:
        return self.start <= when and (self.stop is None or when <= self.stop)


def recordings(local_root: Path) -> list[Recording]:
    """The recordings listed in `recordings.jsonl` whose file is still on disk."""
    try:
        lines = (local_root / RECORDINGS_FILE).read_text().splitlines()
    except OSError:
        return []
    out = []
    for index, line in enumerate(lines):
        try:
            entry = json.loads(line)
            path = Path(entry["path"])
            start = datetime.fromisoformat(entry["start_wall"])
            stop_text = entry.get("stop_wall")
            stop = datetime.fromisoformat(stop_text) if stop_text else None
        except (ValueError, KeyError, TypeError):
            continue
        if path.is_file():
            out.append(Recording(index, path, start, stop))
    return out


def recording_for(when: datetime, available: list[Recording]) -> Recording | None:
    """The newest recording covering `when`, if one survives."""
    covering = [r for r in available if r.covers(when)]
    return max(covering, key=lambda r: r.start) if covering else None
