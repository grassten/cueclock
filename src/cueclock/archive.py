"""After-session housekeeping: `cueclock session maintain` (#57).

A session leaves two kinds of evidence behind. The **session directory**
(`sessions.nosync/<stamp>/`, written by `run`) is the detection log, raw stills
and replay header — kept on the T7 under a byte budget, pinned (stripped
to what replaying its verdicts needs) if Steven judged any of its moments.
The **OBS recording** (`~/Movies`, listed
in `sessions.nosync/recordings.jsonl` by the deck's Session key) is ~4.4 GB an
hour and disposable once the moments worth a look have been cut out of it.

One pass, for every session directory still on the internal SSD:

1. copy the journal in (`logs/session-<stamp>.jsonl` → `journal.jsonl`; the
   original stays in `logs/`, where `report` and `session-stats` read it);
2. `cueclock session verify <dir>` → `replay.json` (the parity check);
3. `cueclock review moments <dir>` → `moments.json` (what the review page lists);
4. cut each moment out of the recording that covers it → `cuts/<id>.mp4`;
5. when the T7 is mounted, hard-link the session's full OBS recording into
   `recording/` (listed in `recording.json`, for the review page's
   full-session player), then move the directory to the T7
   (`/Volumes/T7/cueclock_sessions/<stamp>/`) — copied, sizes checked, then
   the local copy deleted. The link costs no space on the internal SSD; the
   original in `~/Movies` stays under the footage budget below.

Then footage is pruned oldest-first until it fits a byte budget (10 GB by
default), and the T7's retention rules run (`Maintainer.retain`): judged cuts
go, footage lasts the newest 10 sessions, logs + stills, pinned sessions and
journals each have a budget. Nothing grows without bound. "Footage" on the
internal disk is exactly the files the deck listed plus the deck's own
Replay-key saves; nothing else in `~/Movies` is ever looked at.

Every step skips work that is already done, so a pass is safe to repeat — the
launchd agent runs one every 15 minutes. And nothing happens at all while a
session is live: the T7 copy is the kind of USB burst that stalled detection
in the Flag era, and a live session's directory is still being written.

Steps 2 and 3 are other commands, run as subprocesses; one that does not exist
(yet) is skipped with a line in the log rather than failing the pass.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess  # nosec B404: fixed argv lists, no shell
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
# `.nosync` keeps iCloud off it: the launchd agent cannot materialise an
# evicted file (EDEADLK — CLAUDE.md, "Dev workflow").
SESSIONS_ROOT = REPO_ROOT / "sessions.nosync"
RECORDINGS_LOG = SESSIONS_ROOT / "recordings.jsonl"
ARCHIVE_ROOT = Path("/Volumes/T7/cueclock_sessions")
MOVIES = Path.home() / "Movies"
FFMPEG = "/opt/homebrew/bin/ffmpeg"  # absolute: launchd's PATH has no Homebrew

DEFAULT_BUDGET_GB = 10.0
# Retention (Steven, 2026-11-17: no linear growth — a purge rule for everything).
# Footage lasts the newest DEFAULT_KEEP_FULL archived sessions; a moment's cut
# goes as soon as it is judged. Session logs + stills share one T7 budget,
# oldest session first; a session with verdicts is *pinned* on the way out —
# stripped to its detection log, inputs, journal and the stills around each
# judged moment, which is all `review check` needs to replay it — and pinned
# sessions have a budget of their own. Journals in logs/ get one too.
DEFAULT_KEEP_FULL = 10
DEFAULT_ARCHIVE_GB = 100.0  # ~60 hours of play at ~1.7 GB/hour of log + stills
DEFAULT_PINNED_GB = 5.0  # ~50 hours of detection log, if every session had a verdict
DEFAULT_JOURNALS_GB = 1.0  # ~1 MB per session journal: decades, but bounded
VERDICTS_FILE = "verdicts.json"  # written by the review page
PINNED_MARK = "pinned.json"  # a stripped session: what was kept, and when
# Either side of a moment. A shot's wind-up and the settle after it both fit,
# and at ~6 Mbps a 20 s cut is ~16 MB.
CUT_PAD_S = 10.0
FULL_DIR = "recording"  # the session's own copy of its full recording(s)
FULL_INDEX = "recording.json"  # [{"file", "start_wall", "stop_wall"}], read by the review page
HOOK_TIMEOUT_S = 600.0  # a whole-session replay measured well under a minute (#57)

_STAMP = re.compile(r"^\d{8}-\d{6}$")  # the journal's `session-<stamp>.jsonl`
_MOMENT_ID = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]*$")  # a safe file name
# OBS names a replay-buffer save `<RecRBPrefix> <date> <time>`; the prefix is
# OBS's default "Replay" and the only thing that saves the buffer is the deck's
# Replay key (deck/replay.py), which nothing else ever pruned.
_REPLAY_SAVE = re.compile(r"^Replay \d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2}\.(mov|mp4|mkv)$")


@dataclass(frozen=True)
class Recording:
    """One OBS recording the deck logged, and the wall span it covers."""

    path: Path
    start: datetime
    stop: datetime


def parse_wall(text: str) -> datetime:
    """An ISO wall time with its offset (the journal's format). Naive is refused:
    comparing a naive time to an aware one is a TypeError waiting to happen."""
    when = datetime.fromisoformat(text)
    if when.tzinfo is None:
        raise ValueError(f"wall time without a UTC offset: {text!r}")
    return when


def load_recordings(log: Path) -> list[Recording]:
    """Every well-formed line of `recordings.jsonl`; a bad line is skipped."""
    try:
        lines = log.read_text().splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        try:
            raw = json.loads(line)
            out.append(
                Recording(
                    Path(raw["path"]), parse_wall(raw["start_wall"]), parse_wall(raw["stop_wall"])
                )
            )
        except (ValueError, KeyError, TypeError):
            continue
    return out


@dataclass(frozen=True)
class Cut:
    """Where a moment sits inside a recording: what ffmpeg is asked for."""

    source: Path
    offset_s: float
    duration_s: float


def plan_cut(
    moment: dict[str, Any], recordings: Sequence[Recording], pad_s: float = CUT_PAD_S
) -> Cut | None:
    """The stretch of footage around a moment, from the recording covering most of it.

    The window is the moment's own span (`start_wall`..`end_wall`, or just
    `wall`) padded `pad_s` each side, then clipped to the recording — a moment
    in the first seconds of a session gets a shorter lead-in, not a failure.
    None when no recording overlaps it at all (none made, or none logged).
    """
    centre = parse_wall(moment["wall"])
    start = parse_wall(moment["start_wall"]) if moment.get("start_wall") else centre
    end = parse_wall(moment["end_wall"]) if moment.get("end_wall") else centre
    lo, hi = start - timedelta(seconds=pad_s), end + timedelta(seconds=pad_s)

    best: tuple[float, Recording] | None = None
    for rec in recordings:
        overlap = (min(hi, rec.stop) - max(lo, rec.start)).total_seconds()
        if overlap > 0 and (best is None or overlap > best[0]):
            best = (overlap, rec)
    if best is None:
        return None
    overlap, rec = best
    return Cut(rec.path, (max(lo, rec.start) - rec.start).total_seconds(), overlap)


def cut_command(cut: Cut, out: Path) -> list[str]:
    """Stream copy — no re-encode, so a cut costs a disk read, not a CPU minute.

    `-ss` before `-i` seeks on the container, so the cut starts on the
    keyframe at or before the offset: up to a GOP early, never late.
    """
    return [
        FFMPEG,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{cut.offset_s:.3f}",
        "-i",
        str(cut.source),
        "-t",
        f"{cut.duration_s:.3f}",
        "-c",
        "copy",
        "-movflags",
        "+faststart",  # the review page streams it; the index has to come first
        str(out),
    ]


def _size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _json_dict(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _json_list(path: Path) -> list[Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def journal_span(journal: Path, stamp: str) -> tuple[datetime, datetime]:
    """Wall span a session covers: its stamp to the journal's last record."""
    start = datetime.strptime(stamp, "%Y%m%d-%H%M%S").replace(tzinfo=UTC)
    end = start
    with contextlib.suppress(OSError):
        for line in journal.read_text().splitlines():
            try:
                end = max(end, parse_wall(json.loads(line)["wall"]))
            except (ValueError, KeyError, TypeError):
                continue
    return start, end


def session_recordings(
    span: tuple[datetime, datetime], recordings: Sequence[Recording]
) -> list[Recording]:
    """The recordings overlapping a session's span, oldest first."""
    lo, hi = span
    return sorted(
        (r for r in recordings if min(hi, r.stop) > max(lo, r.start)), key=lambda r: r.start
    )


@dataclass(frozen=True)
class Footage:
    path: Path
    size: int
    mtime: float


def footage(recordings: Sequence[Recording], movies: Path) -> list[Footage]:
    """What the budget covers: logged recordings plus the Replay-key saves."""
    paths = {rec.path for rec in recordings}
    with contextlib.suppress(OSError):  # no ~/Movies: only the listed files count
        paths |= {p for p in movies.iterdir() if _REPLAY_SAVE.match(p.name)}
    out = []
    for path in paths:
        try:
            st = path.stat()
        except OSError:
            continue  # already pruned, or never landed
        if path.is_file():
            out.append(Footage(path, st.st_size, st.st_mtime))
    return out


def prune_plan(files: Sequence[Footage], budget_bytes: int) -> list[Footage]:
    """Oldest first, until what is left fits the budget.

    Literal: a single recording bigger than the whole budget goes too, once
    its moments are cut — the cuts and the detection log are what is kept.
    """
    doomed = []
    total = sum(f.size for f in files)
    for f in sorted(files, key=lambda f: f.mtime):
        if total <= budget_bytes:
            break
        doomed.append(f)
        total -= f.size
    return doomed


def live_session(server: str) -> str | None:
    """Why a session counts as live right now, or None when nothing is.

    Three questions, because each alone has a blind spot: the deck's pidfile
    misses a terminal `run`, the server misses a `run` still opening the
    camera, and the process table misses nothing but costs a fork.
    """
    from cueclock.deck import client
    from cueclock.deck.session import PIDFILE, PidProcess

    try:
        pid = json.loads(PIDFILE.read_text()).get("run")
    except (OSError, ValueError, AttributeError):
        pid = None
    if isinstance(pid, int) and PidProcess(pid).poll() is None:
        return f"the deck's session is up (run pid {pid})"
    if client.get_json(server, "/api/state") is not None:
        return f"a cueclock server answers at {server}"
    # Anchored on what follows `run`, so a shell whose command line merely
    # mentions the phrase (`pgrep -fl "cueclock.cli run"`) isn't a session.
    probe = subprocess.run(  # nosec B603: fixed argv
        ["/usr/bin/pgrep", "-f", r"cueclock\.cli run( |$)"], capture_output=True, check=False
    )
    if probe.returncode == 0:
        return "a `cueclock run` process is alive"
    return None


def _run_command(argv: Sequence[str], timeout: float) -> tuple[int, str]:
    """The real subprocess runner: (exit code, stderr). Never raises."""
    try:
        done = subprocess.run(  # nosec B603: fixed argv, shell=False
            list(argv), cwd=REPO_ROOT, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError as exc:
        return 127, str(exc)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return done.returncode, done.stderr


def _cli(*args: str) -> list[str]:
    return [sys.executable, "-m", "cueclock.cli", *args]


def archive_available(root: Path) -> bool:
    """Is the archive's drive there? Under /Volumes that means mounted — an
    unmounted T7 leaves an empty /Volumes/T7 behind sometimes, and writing into
    it would fill the internal disk instead."""
    parts = root.parts
    if len(parts) >= 3 and parts[1] == "Volumes":
        return Path(*parts[:3]).is_mount()
    return root.parent.is_dir()


@dataclass
class Maintainer:
    """One maintenance pass. All I/O that matters to the logic is injectable."""

    root: Path = SESSIONS_ROOT
    archive_root: Path | None = ARCHIVE_ROOT  # None: the drive isn't mounted
    logs: Path = REPO_ROOT / "logs"
    movies: Path = MOVIES
    recordings_log: Path | None = None  # default: <root>/recordings.jsonl
    budget_bytes: int = int(DEFAULT_BUDGET_GB * 1e9)
    keep_full: int = DEFAULT_KEEP_FULL
    archive_bytes: int = int(DEFAULT_ARCHIVE_GB * 1e9)
    pinned_bytes: int = int(DEFAULT_PINNED_GB * 1e9)
    journals_bytes: int = int(DEFAULT_JOURNALS_GB * 1e9)
    dry_run: bool = False
    run: Callable[[Sequence[str], float], tuple[int, str]] = _run_command
    say: Callable[[str], None] = field(default=lambda line: print(line, flush=True))

    def sessions(self) -> list[Path]:
        try:
            return sorted(p for p in self.root.iterdir() if p.is_dir() and _STAMP.match(p.name))
        except FileNotFoundError:
            return []

    def pass_(self) -> int:
        """Run every step; returns 0, or 1 if any step failed (the rest still run)."""
        recordings = load_recordings(self.recordings_log or self.root / "recordings.jsonl")
        ok = True
        for session in self.sessions():
            ok &= self.maintain_session(session, recordings)
        ok &= self.prune(recordings)
        ok &= self.retain()
        return 0 if ok else 1

    def archived(self) -> list[Path]:
        """Session directories on the T7, oldest first ([] when it isn't mounted)."""
        if self.archive_root is None:
            return []
        try:
            return sorted(
                p for p in self.archive_root.iterdir() if p.is_dir() and _STAMP.match(p.name)
            )
        except OSError:
            return []

    def _remove(self, path: Path) -> None:
        if self.dry_run:
            return
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)

    def retain(self) -> bool:
        """Every purge rule, in order. Each is idempotent; a failure stops the rest."""
        try:
            self.drop_judged_cuts()
            self.expire_footage()
            self.fit_archive()
            self.fit_journals()
        except OSError as exc:
            self.say(f"retention: stopped ({exc})")
            return False
        return True

    def drop_judged_cuts(self) -> None:
        """A judged moment's cut has done its job: the verdict and the log remain."""
        for session in self.sessions() + self.archived():
            judged = _json_dict(session / VERDICTS_FILE)
            for mid in judged:
                cut = session / "cuts" / f"{mid}.mp4"
                if _MOMENT_ID.match(mid) and cut.is_file():
                    self._remove(cut)
                    self.say(f"{session.name}: {self._verb} the judged cut {mid}")

    def expire_footage(self) -> None:
        """Only the newest `keep_full` archived sessions keep any footage."""
        archived = self.archived()
        for session in archived[: max(0, len(archived) - self.keep_full)]:
            gone = [
                p
                for p in (session / FULL_DIR, session / FULL_INDEX, session / "cuts")
                if p.exists()
            ]
            for path in gone:
                self._remove(path)
            if gone:
                self.say(
                    f"{session.name}: {self._verb} its footage "
                    f"(only the newest {self.keep_full} sessions keep any)"
                )

    def fit_archive(self) -> None:
        """Hold full sessions to `archive_bytes` and pinned ones to `pinned_bytes`.

        Oldest first. A session leaving the full budget is pinned if it has
        verdicts (they are regression evidence), deleted otherwise.
        """
        full = [s for s in self.archived() if not (s / PINNED_MARK).exists()]
        total = sum(_size(s) for s in full)
        for session in full:
            if total <= self.archive_bytes:
                break
            before = _size(session)
            if _json_dict(session / VERDICTS_FILE):
                self.pin(session)
            else:
                self._remove(session)
                self.say(
                    f"{session.name}: {self._verb} (session budget "
                    f"{self.archive_bytes / 1e9:.0f} GB)"
                )
            total -= before
        pinned = [s for s in self.archived() if (s / PINNED_MARK).exists()]
        total = sum(_size(s) for s in pinned)
        for session in pinned:
            if total <= self.pinned_bytes:
                break
            total -= _size(session)
            self._remove(session)
            self.say(
                f"{session.name}: {self._verb} the pinned session "
                f"(pinned budget {self.pinned_bytes / 1e9:.0f} GB)"
            )

    def pin(self, session: Path) -> None:
        """Strip a judged session to what replaying its verdicts needs.

        Kept: header, detection log, inputs, journal, moments, verdicts, the
        parity verdict, and the stills within each judged moment's window.
        Gone: every other still, footage, the replay journal.
        """
        judged = _json_dict(session / VERDICTS_FILE)
        windows = []
        for moment in _json_list(session / "moments.json"):
            if isinstance(moment, dict) and moment.get("id") in judged:
                with contextlib.suppress(KeyError, ValueError, TypeError):
                    windows.append(
                        (parse_wall(moment["start_wall"]), parse_wall(moment["end_wall"]))
                    )
        index = session / "stills" / "index.jsonl"
        keep: list[dict[str, Any]] = []
        drop: list[dict[str, Any]] = []
        with contextlib.suppress(OSError):
            for line in index.read_text().splitlines():
                try:
                    entry = json.loads(line)
                    when = parse_wall(entry["wall"])
                except (ValueError, KeyError, TypeError):
                    continue
                (keep if any(lo <= when <= hi for lo, hi in windows) else drop).append(entry)
        self.say(
            f"{session.name}: {'would pin' if self.dry_run else 'pinned'} "
            f"({len(judged)} verdicts, {len(keep)} still sets kept)"
        )
        if self.dry_run:
            return
        kept_files = {name for entry in keep for name in entry.get("files", [])}
        for entry in drop:
            for name in entry.get("files", []):
                # Two triggers on one frame share a PNG: never delete a kept one.
                if isinstance(name, str) and _MOMENT_ID.match(name) and name not in kept_files:
                    (session / "stills" / name).unlink(missing_ok=True)
        if index.exists():
            index.write_text("".join(json.dumps(e) + "\n" for e in keep))
        for path in (
            session / FULL_DIR,
            session / FULL_INDEX,
            session / "cuts",
            session / "replay-journal.jsonl",
        ):
            self._remove(path)
        (session / PINNED_MARK).write_text(
            json.dumps(
                {
                    "pinned_at": datetime.now(UTC).isoformat(timespec="seconds"),
                    "verdicts": len(judged),
                    "still_sets": len(keep),
                }
            )
            + "\n"
        )

    def fit_journals(self) -> None:
        """Session journals in logs/, oldest first, under `journals_bytes`."""
        try:
            journals = sorted(self.logs.glob("session-*.jsonl"))
        except OSError:
            return
        total = sum(p.stat().st_size for p in journals)
        for path in journals:
            if total <= self.journals_bytes:
                break
            total -= path.stat().st_size
            self._remove(path)
            self.say(
                f"{path.name}: {self._verb} (journal budget {self.journals_bytes / 1e9:.1f} GB)"
            )

    @property
    def _verb(self) -> str:
        return "would drop" if self.dry_run else "dropped"

    def maintain_session(self, session: Path, recordings: Sequence[Recording]) -> bool:
        name = session.name
        ok = self.copy_journal(session)
        verify = self.hook(session, "replay.json", _cli("session", "verify", str(session)))
        moments = self.hook(session, "moments.json", _cli("review", "moments", str(session)))
        ok &= verify != "failed" and moments != "failed"
        ok &= self.cut_moments(session, recordings)
        if not ok:
            self.say(f"{name}: kept local — a step failed, retried next pass")
            return False
        if "absent" in (verify, moments):
            # Not archived without its parity check and moments: once the
            # command exists, the next pass finishes the job here, where the
            # footage to cut from still is.
            self.say(f"{name}: kept local until both hooks exist")
            return True
        if self.archive_root is not None:
            # Only on the way to the T7: a link left in a session stuck on
            # the internal SSD would keep a pruned recording's bytes alive.
            self.keep_recording(session, recordings)
        return self.move(session)

    def keep_recording(self, session: Path, recordings: Sequence[Recording]) -> None:
        """Hard-link the full recording(s) in, so they travel to the T7 with the session.

        Same APFS volume as `~/Movies`, so the link is free; `move` copies the
        bytes to the T7 and the budget still prunes the original. A recording
        already pruned is simply not kept — the cuts still are.
        """
        index = session / FULL_INDEX
        if index.exists():
            return
        span = journal_span(session / "journal.jsonl", session.name)
        kept = []
        for rec in session_recordings(span, recordings):
            if not rec.path.is_file():
                continue
            dst = session / FULL_DIR / rec.path.name
            if self.dry_run:
                self.say(f"{session.name}: would keep {rec.path.name} as the full recording")
                continue
            dst.parent.mkdir(exist_ok=True)
            if not dst.exists():
                try:
                    os.link(rec.path, dst)
                except OSError:
                    shutil.copyfile(rec.path, dst)  # another volume: pay for a copy
            kept.append(
                {
                    "file": f"{FULL_DIR}/{rec.path.name}",
                    "start_wall": rec.start.isoformat(timespec="milliseconds"),
                    "stop_wall": rec.stop.isoformat(timespec="milliseconds"),
                }
            )
        if kept:
            index.write_text(json.dumps(kept, indent=1) + "\n")
            self.say(f"{session.name}: kept {len(kept)} full recording(s)")

    def copy_journal(self, session: Path) -> bool:
        src = self.logs / f"session-{session.name}.jsonl"
        dst = session / "journal.jsonl"
        if not src.is_file():
            self.say(f"{session.name}: no journal at {src} — skipped the copy")
            return True  # a session that never journaled has nothing to copy
        if dst.is_file() and dst.stat().st_size == src.stat().st_size:
            return True
        self.say(f"{session.name}: {'would copy' if self.dry_run else 'copied'} the journal")
        if not self.dry_run:
            shutil.copyfile(src, dst)
        return True

    def hook(self, session: Path, output: str, argv: list[str]) -> str:
        """Run another command that writes `output` into the session, once.

        Returns "done", "absent" (the command doesn't exist in this build) or
        "failed".
        """
        if (session / output).exists():
            return "done"
        what = " ".join(argv[3:5])  # "session verify" / "review moments"
        if self.dry_run:
            self.say(f"{session.name}: would run `cueclock {what}`")
            return "done"
        code, err = self.run(argv, HOOK_TIMEOUT_S)
        if (session / output).exists():
            # Written is done, whatever the exit: `session verify` exits 1 on a
            # divergence, which is a finding for the review page, not a failure.
            self.say(f"{session.name}: `cueclock {what}` wrote {output} (exit {code})")
            return "done"
        if code == 127 or (code == 2 and "invalid choice" in err):
            self.say(f"{session.name}: `cueclock {what}` doesn't exist here — skipped")
            return "absent"
        if code == 2:
            # Both hooks exit 2 for "nothing to work from" (no detection log, no
            # journal: a `run` that died at startup). Retrying can't help.
            tail = err.strip().splitlines()[-1:] or ["no stderr"]
            self.say(f"{session.name}: `cueclock {what}` had nothing to work from: {tail[0]}")
            return "done"
        tail = err.strip().splitlines()[-1:] or ["no stderr"]
        self.say(f"{session.name}: `cueclock {what}` failed ({code}): {tail[0]}")
        return "failed"

    def cut_moments(self, session: Path, recordings: Sequence[Recording]) -> bool:
        try:
            moments = json.loads((session / "moments.json").read_text())
        except FileNotFoundError:
            return True  # no finder yet, or it found nothing to list
        except (OSError, ValueError) as exc:
            self.say(f"{session.name}: unreadable moments.json ({exc})")
            return False
        ok = True
        for moment in moments if isinstance(moments, list) else []:
            ok &= self.cut_one(session, moment, recordings)
        return ok

    def cut_one(self, session: Path, moment: Any, recordings: Sequence[Recording]) -> bool:
        mid = moment.get("id") if isinstance(moment, dict) else None
        if not isinstance(mid, str) or not _MOMENT_ID.match(mid):
            self.say(f"{session.name}: moment with no usable id skipped ({mid!r})")
            return True
        out = session / "cuts" / f"{mid}.mp4"
        if out.exists():
            return True
        try:
            cut = plan_cut(moment, recordings)
        except (KeyError, ValueError, TypeError) as exc:
            self.say(f"{session.name}: moment {mid} has no usable wall time ({exc})")
            return True
        if cut is None or not cut.source.is_file():
            # Not a failure: no recording was made or logged, or the budget
            # already took it. The detection log still holds the moment.
            self.say(f"{session.name}: no footage for moment {mid}")
            return True
        if self.dry_run:
            self.say(f"{session.name}: would cut {mid} ({cut.duration_s:.0f} s)")
            return True
        out.parent.mkdir(exist_ok=True)
        part = out.with_name(f".{mid}.part.mp4")  # never a half-written cut under the real name
        code, err = self.run(cut_command(cut, part), HOOK_TIMEOUT_S)
        if code != 0:
            part.unlink(missing_ok=True)
            self.say(f"{session.name}: cut {mid} failed ({code}): {err.strip()[-200:]}")
            return False
        part.rename(out)
        self.say(f"{session.name}: cut {mid} ({cut.duration_s:.0f} s from {cut.source.name})")
        return True

    def move(self, session: Path) -> bool:
        """Copy to the archive, check every size, then delete the local copy.

        Plain `copyfile`, no metadata: the T7 is ExFAT, and copying flags or
        xattrs onto it is a way to fail for nothing.
        """
        if self.archive_root is None:
            self.say(f"{session.name}: archive drive not mounted — kept local")
            return True
        dest = self.archive_root / session.name
        files = [p for p in session.rglob("*") if p.is_file()]
        if self.dry_run:
            self.say(f"{session.name}: would move {len(files)} files to {dest}")
            return True
        for src in files:
            dst = dest / src.relative_to(session)
            if dst.is_file() and dst.stat().st_size == src.stat().st_size:
                continue  # landed on an earlier, interrupted pass
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
        short = [
            p
            for p in files
            if not (dest / p.relative_to(session)).is_file()
            or (dest / p.relative_to(session)).stat().st_size != p.stat().st_size
        ]
        if short:
            self.say(f"{session.name}: {len(short)} files didn't verify on {dest} — kept local")
            return False
        shutil.rmtree(session)
        self.say(f"{session.name}: moved to {dest} ({len(files)} files)")
        return True

    def prune(self, recordings: Sequence[Recording]) -> bool:
        files = footage(recordings, self.movies)
        doomed = prune_plan(files, self.budget_bytes)
        total = sum(f.size for f in files)
        verb = "would delete" if self.dry_run else "deleted"
        for f in doomed:
            if not self.dry_run:
                try:
                    f.path.unlink()
                except OSError as exc:
                    self.say(f"footage: could not delete {f.path} ({exc})")
                    return False
            self.say(f"footage: {verb} {f.path.name} ({f.size / 1e9:.2f} GB)")
        left = total - sum(f.size for f in doomed)
        self.say(
            f"footage: {left / 1e9:.2f} GB of {self.budget_bytes / 1e9:.0f} GB budget "
            f"({len(files) - len(doomed)} files)"
        )
        return True
