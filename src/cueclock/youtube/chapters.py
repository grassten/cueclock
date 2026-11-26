"""Game chapters on a stream's recording, written after the session (#81).

YouTube turns lines of `time label` in a video's description into chapter
markers, on its own terms: the first line is `0:00`, there are at least three,
and each lasts at least 10 seconds. With fewer than three, none are written.

**What a chapter is.** One per game, `Game 3 (1-1)` (set with an en dash): the
game's number and the score going into it. With a bracket loaded, one per match instead,
`Match 7 · Okafor vs Lee` (per-game lines for a whole draw would not fit the
description). Practice, which is a stream with no players set, gets none.
When a stream holds more than one match, each match's first chapter names its
players.

**Where the times come from.** While a stream is on air the server journals a
`stream_mark` whenever the players, the score or the loaded match change
(`server/stream.py`), so this module never has to replay a score or a
bracket: each mark is the truth as it stood. A game starts at the mark that
ended the one before it, and its chapter is put **10 seconds before the
game's first detected shot**, so a jump lands on the break and skips the
racking. With no shot found it stays at the mark. A point taken back removes
the game end before it.

`build` is pure. `write` is the archive pass's step (`archive.py`): it reads
the session's own journal copy, asks YouTube when the video really started
(`actualStartTime`, which is the recording's 0:00), and appends the lines to
the description once, noting what it did in `chapters.json` beside the
journal so a later pass does not do it again.
"""

from __future__ import annotations

import contextlib
import json
from bisect import bisect_left
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from cueclock.youtube.api import SignedOutError, YouTube, YouTubeError
from cueclock.youtube.title import player_set, surname

MIN_CHAPTERS = 3  # YouTube's rules (support.google.com/youtube/answer/9884579)
MIN_LENGTH_S = 10.0
LEAD_S = 10.0  # how far before a game's first shot its chapter starts
DESCRIPTION_MAX = 5000
DONE_FILE = "chapters.json"
# The journal records that mean a ball was struck in play: a counted shot, and
# a break the settle adjudicated after its strike was missed.
SHOT_KINDS = frozenset({"vision_shot_taken", "break_adjudicated"})


@dataclass(frozen=True)
class Mark:
    """The match as it stood from ``t`` seconds into the video."""

    t: float
    p1: str
    p2: str
    s1: int
    s2: int
    over: bool = False  # somebody has reached their race: no next game
    tournament: str | None = None
    match: int | None = None  # the bracket match on the wall

    @property
    def players(self) -> tuple[str, str] | None:
        if player_set(self.p1) and player_set(self.p2):
            return self.p1.strip(), self.p2.strip()
        return None

    @property
    def total(self) -> int:
        return self.s1 + self.s2


@dataclass
class _Anchor:
    """A chapter before its time is settled: the mark that opened it."""

    at: float
    label: str
    names: tuple[str, str]
    total: int | None = None  # games played going in; None for a bracket match
    opens_match: bool = False


def _versus(names: tuple[str, str]) -> str:
    return f"{surname(names[0])} vs {surname(names[1])}"


def _game(mark: Mark) -> str:
    return f"Game {mark.total + 1} ({mark.s1}\u2013{mark.s2})"


def _anchors(marks: Sequence[Mark]) -> list[_Anchor]:
    anchors: list[_Anchor] = []
    prev: Mark | None = None
    for mark in marks:
        names = mark.players
        if mark.tournament:
            fresh = prev is None or not prev.tournament or prev.match != mark.match
            if mark.match is not None and names is not None and fresh:
                anchors.append(_Anchor(mark.t, f"Match {mark.match} · {_versus(names)}", names))
        elif names is not None:
            same_match = prev is not None and not prev.tournament and prev.players == names
            if not same_match:
                anchors.append(_Anchor(mark.t, _game(mark), names, mark.total, opens_match=True))
            elif prev is not None and mark.total > prev.total:
                if not mark.over:
                    anchors.append(_Anchor(mark.t, _game(mark), names, mark.total))
            elif prev is not None and mark.total == 0 and prev.total >= 2:
                # Scores reset with the same two players: their next match.
                anchors.append(_Anchor(mark.t, _game(mark), names, 0, opens_match=True))
            elif prev is not None:
                # A point taken back, or moved to the other player: the game
                # ends it opened are gone, and the game now being played is
                # the one this score goes into.
                while (
                    len(anchors) > 1
                    and not anchors[-1].opens_match
                    and (anchors[-1].total or 0) > mark.total
                ):
                    anchors.pop()
                current = anchors[-1] if anchors else None
                if (
                    current is not None
                    and current.total is not None
                    and current.total >= mark.total
                ):
                    current.label, current.total = _game(mark), mark.total
        prev = mark
    return anchors


def build(marks: Sequence[Mark], shots: Sequence[float], end: float) -> list[tuple[float, str]]:
    """The chapters of one video: `(seconds from its start, label)`, or [].

    ``marks`` in time order, the first being the match as the stream began;
    ``shots`` the seconds at which a shot was detected, in order; ``end`` the
    video's length.
    """
    anchors = _anchors(marks)
    chapters: list[tuple[float, str]] = []
    played_last = False
    several = sum(a.opens_match for a in anchors) > 1
    for i, anchor in enumerate(anchors):
        bound = anchors[i + 1].at if i + 1 < len(anchors) else end
        first = bisect_left(shots, anchor.at)
        shot = shots[first] if first < len(shots) and shots[first] < bound else None
        played_last = shot is not None
        start = anchor.at if shot is None else max(anchor.at, shot - LEAD_S)
        label = anchor.label
        if several and anchor.opens_match:
            label = f"{_versus(anchor.names)} · {label}"
        if not chapters:
            chapters.append((0.0, label))  # YouTube's first line is 0:00
        elif start - chapters[-1][0] < MIN_LENGTH_S:
            chapters[-1] = (chapters[-1][0], label)  # too short to stand alone
        else:
            chapters.append((start, label))
    # A last chapter nobody shot in is not a game: the stream ended on a
    # score press, or with the next match loaded and not begun.
    if len(chapters) > 1 and (not played_last or end - chapters[-1][0] < MIN_LENGTH_S):
        chapters.pop()
    return chapters if len(chapters) >= MIN_CHAPTERS else []


def stamp(seconds: float) -> str:
    """`0:00`, `12:34`, `1:02:03`."""
    whole = int(seconds)
    hours, minutes, secs = whole // 3600, whole % 3600 // 60, whole % 60
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def render(chapters: Iterable[tuple[float, str]]) -> str:
    return "\n".join(f"{stamp(t)} {label}" for t, label in chapters)


# ---------- the archive pass's step ----------


@dataclass(frozen=True)
class Aired:
    """One stream in a session's journal: its video and what was journaled of it."""

    video_id: str
    start: datetime
    end: datetime
    marks: list[dict[str, Any]]
    shots: list[datetime]


def _wall(record: dict[str, Any], key: str = "wall") -> datetime | None:
    with contextlib.suppress(KeyError, TypeError, ValueError):
        when = datetime.fromisoformat(record[key].replace("Z", "+00:00"))
        if when.tzinfo is not None:
            return when
    return None


def aired(journal: Iterable[dict[str, Any]]) -> list[Aired]:
    """Every stream the journal recorded going on air with a video behind it."""
    records = [r for r in journal if _wall(r) is not None]
    out = []
    for record in records:
        video_id = record.get("video_id")
        start = _wall(record, "start_wall")
        if record.get("kind") != "stream_on_air" or not isinstance(video_id, str) or start is None:
            continue
        # Until its own off-air line; a stream stopped with the session has
        # none, and runs to the journal's last record.
        end = next(
            (
                _wall(r)
                for r in records
                if r.get("kind") == "stream_off_air" and r.get("video_id") == video_id
            ),
            None,
        ) or max(w for r in records if (w := _wall(r)) is not None)
        out.append(
            Aired(
                video_id,
                start,
                end,
                [
                    r
                    for r in records
                    if r.get("kind") == "stream_mark" and r.get("video_id") == video_id
                ],
                [w for r in records if r.get("kind") in SHOT_KINDS and (w := _wall(r)) is not None],
            )
        )
    return out


def for_video(stream: Aired, zero: datetime, end: datetime) -> list[tuple[float, str]]:
    """One stream's chapters, on a video whose 0:00 is ``zero``."""

    def at(when: datetime) -> float:
        return max(0.0, (when - zero).total_seconds())

    marks = [
        Mark(
            t=at(when),
            p1=str(m.get("p1", "")),
            p2=str(m.get("p2", "")),
            s1=int(m.get("s1", 0)),
            s2=int(m.get("s2", 0)),
            over=bool(m.get("over")),
            tournament=m.get("tournament") or None,
            match=m.get("match") if isinstance(m.get("match"), int) else None,
        )
        for m in stream.marks
        if (when := _wall(m)) is not None
    ]
    shots = sorted(at(s) for s in stream.shots if zero <= s <= end)
    return build(marks, shots, (end - zero).total_seconds())


def _journal(path: Path) -> list[dict[str, Any]]:
    out = []
    with contextlib.suppress(OSError):
        for line in path.read_text().splitlines():
            with contextlib.suppress(ValueError):
                record = json.loads(line)
                if isinstance(record, dict):
                    out.append(record)
    return out


def write(
    session: Path,
    say: Callable[[str], None],
    *,
    youtube: YouTube | None = None,
    dry_run: bool = False,
) -> bool:
    """Append each of a session's streams' chapters to its video, once.

    False only when YouTube could not be reached, so the caller tries again
    on its next pass. A video YouTube refuses to change, or one that is gone,
    is noted and left: trying again could not help.
    """
    streams = aired(_journal(session / "journal.jsonl"))
    done_path = session / DONE_FILE
    done: dict[str, Any] = {}
    with contextlib.suppress(OSError, ValueError):
        loaded = json.loads(done_path.read_text())
        done = loaded if isinstance(loaded, dict) else {}
    pending = [s for s in streams if s.video_id not in done]
    if not pending:
        return True
    if dry_run:
        for stream in pending:
            say(f"{session.name}: would write chapters to video {stream.video_id}")
        return True
    yt = youtube or YouTube()
    ok = True
    for stream in pending:
        try:
            note = _write_one(yt, stream)
        except SignedOutError as exc:
            # Nobody is signed in, and nobody may be for days: the session is
            # not held back for it, and the video keeps its plain description.
            note = {"chapters": 0, "why": str(exc)}
        except YouTubeError as exc:
            if exc.unreachable:
                say(f"{session.name}: chapters for {stream.video_id} not written yet ({exc})")
                ok = False
                continue
            note = {"chapters": 0, "why": str(exc)}
        done[stream.video_id] = note
        done_path.write_text(json.dumps(done, indent=1) + "\n")
        say(
            f"{session.name}: video {stream.video_id}: "
            + (
                f"{note['chapters']} chapters written"
                if note["chapters"]
                else f"no chapters ({note['why']})"
            )
        )
    return ok


def _write_one(yt: YouTube, stream: Aired) -> dict[str, Any]:
    zero, end = stream.start, stream.end
    broadcast = yt.broadcast(stream.video_id)
    if broadcast is None:
        return {"chapters": 0, "why": "the video is gone"}
    # YouTube's own start is the recording's 0:00; the deck's is OBS's, which
    # connected a moment earlier.
    zero = _wall(broadcast.get("snippet", {}), "actualStartTime") or zero
    end = _wall(broadcast.get("snippet", {}), "actualEndTime") or end
    chapters = for_video(stream, zero, end)
    if not chapters:
        return {"chapters": 0, "why": "fewer than three games"}
    snippet = broadcast.get("snippet", {})
    description = str(snippet.get("description") or "").rstrip()
    text = (description + "\n\n" if description else "") + render(chapters)
    if len(text) > DESCRIPTION_MAX:
        return {"chapters": 0, "why": "they would not fit the description"}
    yt.describe(stream.video_id, str(snippet.get("title") or ""), text)
    return {
        "chapters": len(chapters),
        "zero": zero.isoformat(timespec="seconds"),
        "lines": render(chapters).splitlines(),
    }
