"""Session directories, their footage and their verdicts, as the review page sees them.

Sessions live under two roots with the same per-session layout (#57 contract):
the internal SSD's `sessions.nosync/` and the T7's `cueclock_sessions/`. The T7
is often unmounted, so a missing root is simply empty — the page must still
come up with whatever is local.

Footage for a moment is its cut (`cuts/<id>.mp4`): the archive pass makes
them, and the page makes the one a card asks for when it is not there yet,
from the listed recording (`recordings.jsonl`) or the session's own kept copy.
With neither left there is no footage: recordings are pruned by a byte
budget, and the verdict can still be given from the journal.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cueclock import archive
from cueclock.archive import ARCHIVE_ROOT, FULL_INDEX, RECORDINGS_LOG, SESSIONS_ROOT
from cueclock.review.moments import MOMENTS_FILE, session_journal, write_json

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

    def fingerprint(self) -> str:
        """Short sha256 of `verdicts.json`: any verdict or note changed, it changes.

        The session review records it with what it filed (#70), so a re-run
        can tell whether anything changed since.
        """
        try:
            return hashlib.sha256((self.path / VERDICTS_FILE).read_bytes()).hexdigest()[:12]
        except OSError:
            return "none"

    def unreviewed(self) -> int | None:
        moments = self.moments()
        if moments is None:
            return None
        judged = self.verdicts()
        return sum(1 for m in moments if m.get("id") not in judged)

    def set_verdict(self, moment_id: str, verdict: str | None, note: str) -> None:
        """Record (or, with `verdict=None`, withdraw) Steven's call on one moment.

        FileNotFoundError when the directory is gone: the archive pass moved
        the session between the lookup and this write. Look it up again.
        """
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

    def full_recordings(self) -> list[Recording]:
        """The session's own copy of its full recording(s), kept by `session maintain`.

        Only files inside the session directory: `recording.json` names them,
        and a name that resolves anywhere else is ignored.
        """
        data = _read_json(self.path / FULL_INDEX)
        root = self.path.resolve()
        out = []
        for index, entry in enumerate(data if isinstance(data, list) else []):
            try:
                path = (self.path / str(entry["file"])).resolve()
                start = datetime.fromisoformat(entry["start_wall"])
                stop_text = entry.get("stop_wall")
                stop = datetime.fromisoformat(stop_text) if stop_text else None
            except (ValueError, KeyError, TypeError, OSError):
                continue
            if path.is_relative_to(root) and path.is_file():
                out.append(Recording(index, path, start, stop))
        return out

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


def locate(stamp: str, roots: list[Path] | None = None) -> Session | None:
    """One session by its stamp: the local copy, else the T7's."""
    if not STAMP.match(stamp):
        return None
    found = discover(roots if roots is not None else [LOCAL_ROOT, ARCHIVE_ROOT])
    return next((s for s in found if s.stamp == stamp), None)


def journal_of(stamp: str, logs_dir: Path = Path("logs")) -> Path | None:
    """A session's journal: its directory's copy, else the live one in `logs/`."""
    found = locate(stamp)
    if found is not None:
        return session_journal(found.path, logs_dir)
    live = logs_dir / f"session-{stamp}.jsonl"
    return live if STAMP.match(stamp) and live.is_file() else None


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


def overlapping(start: datetime, end: datetime, available: list[Recording]) -> list[Recording]:
    """Recordings overlapping a wall span, oldest first."""
    return sorted(
        (r for r in available if r.start < end and (r.stop is None or r.stop > start)),
        key=lambda r: r.start,
    )


def clip_sources(session: Session, local_root: Path) -> list[archive.Recording]:
    """What a moment of this session can be cut from, in the cutter's terms.

    The listed recordings first: they are on the internal SSD, and while one
    is there it is the same footage as the session's kept copy on the T7. A
    recording still being written has no stop time and is left out.
    """
    found = [*recordings(local_root), *session.full_recordings()]
    return [archive.Recording(r.path, r.start, r.stop) for r in found if r.stop is not None]
