"""A few seconds of a session's footage as one tiled image: `cueclock review frames` (#70).

Whoever diagnoses a wrong verdict sometimes needs what the table or the wall
showed, and a clip is the wrong shape for that: twelve frames side by side
in one JPEG can be read in one look. For #69 it settled two questions the
journal could not (what the wall's digits did through a §17 revert, and
whether a buzzer sounded mid-shot or mid-racking).

The footage, best first:

1. the session's kept copy of its full recording (`recording.json`);
2. a recording listed in `recordings.jsonl` that is still in `~/Movies`;
3. the moment's cut, `cuts/<id>.mp4`.

Recordings come before the cut although the cut is smaller, because only a
recording's start is known exactly: a cut is a stream copy that starts on the
keyframe at or before its span (`archive.cut_command`), up to a GOP early, so
its times are approximate and the legend says so. Either way the position
in a recording is plain `wall - start_wall` (checkpoint 137).

Images go to the temp directory unless told otherwise: they are footage, and
the session directory holds no file without a purge rule.
"""

from __future__ import annotations

import json
import math
import subprocess  # nosec B404: fixed argv lists, no shell
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from cueclock.archive import FFMPEG
from cueclock.review import sessions as sess

COLUMNS = 4
TILE_WIDTH_PX = 480
TIMEOUT_S = 120.0
OUT_DIR = Path(tempfile.gettempdir()) / "cueclock-frames"


@dataclass(frozen=True)
class Source:
    path: Path
    start: datetime  # the wall time of the file's first frame
    exact: bool  # False for a cut, whose first frame is up to a GOP early


def when(session: sess.Session, target: str) -> datetime:
    """The wall time a moment id, an ISO wall time or a mono time names."""
    for moment in session.moments() or []:
        if moment.get("id") == target:
            return datetime.fromisoformat(str(moment["wall"]))
    try:
        return datetime.fromisoformat(target)
    except ValueError:
        pass
    try:
        mono = float(target)
    except ValueError:
        raise ValueError(f"no moment {target!r} in {session.stamp}, and not a time") from None
    header = json.loads((session.path / "header.json").read_text())
    started = datetime.fromisoformat(header["started_wall"])
    return started + timedelta(seconds=mono - float(header["started_mono"]))


def source(
    session: sess.Session, at: datetime, moment_id: str | None, local_root: Path
) -> Source | None:
    """The footage that covers `at`, best first (see the module docstring)."""
    for rec in [*session.full_recordings(), *sess.recordings(local_root)]:
        if rec.covers(at):
            return Source(rec.path, rec.start, exact=True)
    cut = session.cut(moment_id) if moment_id else None
    if cut is None:
        return None
    moment = next(m for m in session.moments() or [] if m.get("id") == moment_id)
    return Source(cut, datetime.fromisoformat(str(moment["start_wall"])), exact=False)


def command(src: Source, start: datetime, span_s: float, fps: float, out: Path) -> list[str]:
    """`-ss` before `-i` with a filter re-encodes, so the seek is frame-exact."""
    count = max(1, round(span_s * fps))
    rows = math.ceil(count / COLUMNS)
    offset = max(0.0, (start - src.start).total_seconds())
    return [
        FFMPEG,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{offset:.3f}",
        "-i",
        str(src.path),
        "-t",
        f"{span_s:.3f}",
        "-vf",
        f"fps={fps},scale={TILE_WIDTH_PX}:-1,tile={COLUMNS}x{rows}",
        "-frames:v",
        "1",
        "-q:v",
        "3",
        str(out),
    ]


def legend(src: Source, at: datetime, start: datetime, span_s: float, fps: float) -> str:
    """Which tile is which instant, relative to the moment."""
    count = max(1, round(span_s * fps))
    lead = (start - at).total_seconds()
    first, last = lead, lead + (count - 1) / fps
    lines = [
        f"{count} tiles, {COLUMNS} per row, left to right then down, every {1 / fps:.2f} s: "
        f"tile 1 = {first:+.2f} s, tile {count} = {last:+.2f} s from the moment "
        f"({at.astimezone():%H:%M:%S} local)",
        f"from {src.path.name}"
        + (
            f" at {(start - src.start).total_seconds():.1f} s"
            if src.exact
            else " (a cut: times can be up to a GOP early)"
        ),
    ]
    return "\n".join(lines)


def tile(
    session: sess.Session,
    target: str,
    span_s: float = 6.0,
    fps: float = 2.0,
    before_s: float | None = None,
    out: Path | None = None,
    local_root: Path = sess.LOCAL_ROOT,
) -> tuple[Path, str]:
    """Write the tiled JPEG for `target`; returns its path and its legend.

    The span is centred on the moment unless `before_s` says how much of it
    comes first. FileNotFoundError when no footage covers it.
    """
    at = when(session, target)
    moment_id = target if sess.MOMENT_ID.match(target) else None
    src = source(session, at, moment_id, local_root)
    if src is None:
        raise FileNotFoundError(f"no footage left that covers {at.isoformat()} in {session.stamp}")
    start = at - timedelta(seconds=span_s / 2 if before_s is None else before_s)
    label = moment_id or at.strftime("%H%M%S")
    path = out or OUT_DIR / f"{session.stamp}-{label}.jpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    argv = command(src, start, span_s, fps, path)
    subprocess.run(argv, check=True, timeout=TIMEOUT_S)  # nosec B603: fixed argv, shell=False
    return path, legend(src, at, start, span_s, fps)
