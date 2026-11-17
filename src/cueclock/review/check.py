"""Steven's verdicts as regression checks: `cueclock review check` (#57).

Each judged session is replayed through the code checked out now and compared
with its live journal (`capture.session_replay`); only the differences that
fall inside a judged moment's window count, so a change elsewhere in the
session doesn't blame a moment it never touched.

| Verdict | Replay agrees with live | Replay differs |
|---|---|---|
| right | **holds** | **regressed** — the code now gets wrong what was right |
| wrong | **still wrong** — not fixed yet | **changed** — maybe fixed: re-judge it |

The run is from the top of the session (tracker and clock state need the
whole history), so it costs a replay per judged session: roughly 3 minutes
per hour of play. It is the check for `events.py` / `track.py` / hub-policy
work, run beside the corpus battery, not part of it.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from cueclock.capture.session_log import SessionLog
from cueclock.capture.session_replay import compare, live_journal_of, read_journal, replay
from cueclock.review import sessions as sess

STATUS = {
    ("right", False): "holds",
    ("right", True): "REGRESSED",
    ("wrong", False): "still wrong",
    ("wrong", True): "changed",
}


@dataclass(frozen=True)
class Result:
    stamp: str
    moment_id: str
    verdict: str
    status: str
    diffs: list[dict[str, Any]]


def _within(diff: dict[str, Any], start: datetime, end: datetime) -> bool:
    try:
        return start <= datetime.fromisoformat(str(diff["wall"])) <= end
    except (KeyError, ValueError):
        return False


def check_session(session: sess.Session) -> list[Result]:
    """Replay one judged session and grade each of its verdicts."""
    verdicts = session.verdicts()
    moments = {str(m.get("id")): m for m in session.moments() or []}
    judged = [(mid, v) for mid, v in verdicts.items() if mid in moments]
    if not judged:
        return []
    log = SessionLog(session.path)
    with tempfile.TemporaryDirectory(prefix="cueclock-check-") as tmp:
        out = Path(tmp) / "replay.jsonl"
        replay(log, out)
        diffs = compare(read_journal(live_journal_of(log)), read_journal(out))
    results = []
    for mid, given in sorted(judged):
        moment = moments[mid]
        verdict = str(given.get("verdict"))
        try:
            start = datetime.fromisoformat(str(moment["start_wall"]))
            end = datetime.fromisoformat(str(moment["end_wall"]))
        except (KeyError, ValueError):
            continue
        inside = [d for d in diffs if _within(d, start, end)]
        status = STATUS.get((verdict, bool(inside)), "unknown verdict")
        results.append(Result(session.stamp, mid, verdict, status, inside))
    return results


def judged_sessions(roots: list[Path], stamps: list[str] | None = None) -> list[sess.Session]:
    """Sessions with at least one verdict and a detection log, oldest first."""
    found = [
        s
        for s in sess.discover(roots)
        if s.verdicts() and (s.path / "header.json").is_file() and (not stamps or s.stamp in stamps)
    ]
    return sorted(found, key=lambda s: s.stamp)
