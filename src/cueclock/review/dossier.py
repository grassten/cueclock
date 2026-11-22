"""Everything a session review starts from, on one page: `cueclock review dossier` (#70).

After Steven judges a session, whoever turns his wrong verdicts into changes
(by hand for #69, the `session-review` skill since) begins with the same
inventory: the verdicts joined to their moments in local time, which moments
share an episode, whether the replay agreed, what code ran, the presses, the
footage, and the session's aggregates already classified. Classified matters
most: raw rows mislead. Session 20261121-153046 listed 7 shots counted with
no strike and 47 numbers back on the ball list; two racking stretches with
the clock running made most of both, two of the "misses" were no shot at all
and one was the watchdog. The dossier states each of those as what it is.

It reads only files: the session directory, its journal, and `git` for the
checkpoints since the session's code. No replay, so it is safe while a
session is live, and it costs about a second. `review check` is the replay.

The counts that recur across reviews are named functions (`held_by`,
`freeze_shapes`, `racking_stretches`, `classify_misses`), so a cross-journal
measurement calls them rather than re-deriving them:

    .venv/bin/python3 -c "from cueclock.review import dossier; ..."
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
import subprocess  # nosec B404: fixed argv lists, no shell
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from itertools import pairwise
from pathlib import Path

from cueclock.archive import REPO_ROOT
from cueclock.review import moments as finder
from cueclock.review import sessions as sess
from cueclock.server import report
from cueclock.server.scratch import OBJECT_MOVED_PX

Record = dict[str, object]

GIT = "/usr/bin/git"  # absolute: the review may run under launchd (#70 step 3)

SETTLE_KINDS = (report.SETTLE_KIND, report.SETTLE_SYNTHESIZED_KIND)

# Why a shot counted with no strike behind it, as the dossier names it. The
# racking ones never reach this list: `report` stopped counting racking
# settles as shots in checkpoint 136 (`report.Racking`), and the dossier
# lists the stretches themselves instead.
WATCHDOG = "after the watchdog (the next settle after a synthesized one)"
CUE_ON_SPOT = "no shot (the list read the cue ball on its spot)"
NOTHING_MOVED = "no shot? (the tracker saw no ball move)"
UNEXPLAINED = "unexplained (a soft shot?)"


def _num(record: Record, key: str) -> float | None:
    value = record.get(key)
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _wall(record: Record) -> datetime:
    return datetime.fromisoformat(str(record["wall"]))


def _local(when: datetime) -> str:
    return when.astimezone().strftime("%H:%M:%S")


def _clock(seconds: float) -> str:
    m, s = divmod(int(max(0.0, seconds)), 60)
    return f"{m // 60}:{m % 60:02d}:{s:02d}" if m >= 60 else f"{m}:{s:02d}"


# ---------------------------------------------------------------- measurements


def settle_lists(records: Sequence[Record]) -> list[tuple[Record, Record | None]]:
    """Each settle with the `scratch_list` line that judged it (None if the
    list never came before the next settle)."""
    out: list[tuple[Record, Record | None]] = []
    for record in records:
        kind = record.get("kind")
        if kind in SETTLE_KINDS:
            out.append((record, None))
        elif kind == "scratch_list" and out and out[-1][1] is None:
            out[-1] = (out[-1][0], record)
    return out


def held_by(records: Sequence[Record]) -> Counter[str]:
    """What decided each held settle (`held` on the settle line, checkpoint 133+)."""
    return Counter(
        str(r["held"]) for r in records if r.get("kind") in SETTLE_KINDS and r.get("held")
    )


def _moved_bucket(settle: Record) -> str:
    moved = _num(settle, "moved")
    if moved is None:
        return "?"
    return "2+" if moved >= 2 else str(int(moved))


def freeze_shapes(records: Sequence[Record]) -> Counter[tuple[bool, str]]:
    """Settles that ended a freeze, by (a strike was seen in it, balls the
    tracker moved: 0 / 1 / 2+). A freeze is a `shot_provisional` or a strike
    since the previous settle."""
    shapes: Counter[tuple[bool, str]] = Counter()
    froze = struck = False
    for record in records:
        kind = record.get("kind")
        if kind == report.SHOT_PROVISIONAL_KIND:
            froze = True
        elif kind == report.SHOT_KIND:
            froze = struck = True
        elif kind in SETTLE_KINDS:
            if froze:
                shapes[(struck, _moved_bucket(record))] += 1
            froze = struck = False
    return shapes


@dataclass(frozen=True)
class Stretch:
    """Racking with the clock running: from the arm-free list that brought
    `report.RACKING_MIN_RETURNS` or more numbers back, to the next New Rack."""

    start: Record  # the ball list that began it
    returned: int  # numbers back on that list
    settles: int  # settles the clock journaled during it (not shots)
    end: Record | None  # the record whose rack count fell; None = the session ended
    raw_back: int = 0  # numbers back from its first list on, as pre-136 stats counted them


def racking_stretches(records: Sequence[Record]) -> list[Stretch]:
    """`report.Racking`'s stretches, with what began and ended each one.

    `raw_back` is what "numbers back after leaving" read during the stretch
    before checkpoint 136, when racking was not told apart: in session
    20261121-153046, 39 of the 47 it reported (`raw_numbers_back`); the
    other 8 are what `session-stats` reads today."""
    racking = report.Racking()
    raw = report.Racking(min_returns=math.inf)
    out: list[Stretch] = []
    for record in records:
        rack = _num(record, "rack_shots")
        if rack is not None:
            was = racking.on
            racking.note_rack(int(rack))
            raw.note_rack(int(rack))
            if was and not racking.on:
                out[-1] = replace(out[-1], end=record)
        kind = record.get("kind")
        if kind == report.BALL_LIST_KIND:
            was = racking.on
            racking.feed(record)
            back = raw.feed(record)
            if racking.on and not was:
                out.append(Stretch(record, racking.returned, 0, None))
            if racking.on:
                out[-1] = replace(out[-1], raw_back=out[-1].raw_back + back)
        elif kind in SETTLE_KINDS and racking.on and record.get("state") != "IDLE":
            out[-1] = replace(out[-1], settles=out[-1].settles + 1)
    return out


def raw_numbers_back(records: Sequence[Record]) -> int:
    """ "Numbers back after leaving" as `session-stats` counted it before
    checkpoint 136: every number back, racking included."""
    raw = report.Racking(min_returns=math.inf)
    total = 0
    for record in records:
        rack = _num(record, "rack_shots")
        if rack is not None:
            raw.note_rack(int(rack))
        if record.get("kind") == report.BALL_LIST_KIND:
            total += raw.feed(record)
    return total


@dataclass(frozen=True)
class Miss:
    """A shot counted with no strike seen (`report`'s "counted at settle")."""

    shot: report.Shot
    settle: Record | None
    listing: Record | None  # the settle's `scratch_list` line
    why: str


def miss_class(settle: Record | None, listing: Record | None, after_watchdog: bool = False) -> str:
    """Why a shot was counted with no strike, from its settle and its list.

    `after_watchdog`: the settle before it was the watchdog's. A synthesized
    settle stands in for one that never came; when the real one comes after
    all, it counts a second shot with no strike (20261121-153046, 1 s later).

    The cue on its spot uses the hub's own bound (`scratch.OBJECT_MOVED_PX`,
    the 3 px §16 and §19 refuse a settle at); journals before checkpoint 138
    carry only the list's `row`.
    """
    if after_watchdog or (
        settle is not None and settle.get("kind") == report.SETTLE_SYNTHESIZED_KIND
    ):
        return WATCHDOG
    if listing is not None:
        px = _num(listing, "cue_px")
        if listing.get("row") == "still" or (px is not None and px <= OBJECT_MOVED_PX):
            return CUE_ON_SPOT
    if settle is not None and _num(settle, "moved") == 0 and not _num(settle, "vanished"):
        return NOTHING_MOVED
    return UNEXPLAINED


def classify_misses(records: Sequence[Record], built: report.SessionReport) -> list[Miss]:
    """`report`'s misses, each joined to its settle (by capture time, as the
    moments finder joins them) and named."""
    pairs = settle_lists(records)
    by_t: dict[float, tuple[Record, Record | None, bool]] = {}
    for n, (settle, listing) in enumerate(pairs):
        t = finder._capture_t(settle)
        if t is not None:
            prior = pairs[n - 1][0].get("kind") if n else None
            by_t[t] = (settle, listing, prior == report.SETTLE_SYNTHESIZED_KIND)
    out = []
    for shot in built.missed:
        found = by_t.get(shot.t) if shot.t is not None else None
        if found is None:
            out.append(Miss(shot, None, None, miss_class(None, None)))
        else:
            out.append(Miss(shot, found[0], found[1], miss_class(*found)))
    return out


# ------------------------------------------------------------------- the page


def _read(path: Path) -> object:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _jsonl(path: Path) -> list[Record]:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def verdicts_fingerprint(session: sess.Session) -> str:
    """Short sha256 of `verdicts.json`: any verdict or note changed, it changes."""
    try:
        return hashlib.sha256((session.path / sess.VERDICTS_FILE).read_bytes()).hexdigest()[:12]
    except OSError:
        return "none"


def _git(*args: str) -> str:
    try:
        done = subprocess.run(  # nosec B603: fixed argv, shell=False
            [GIT, "-C", str(REPO_ROOT), *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip()


class _Page:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def h(self, title: str) -> None:
        self.lines += ["", f"## {title}"]

    def add(self, text: str = "") -> None:
        self.lines.append(text)

    def __str__(self) -> str:
        return "\n".join(self.lines).strip() + "\n"


def _brief(record: Record | None, keys: Sequence[str]) -> str:
    if record is None:
        return "-"
    return " ".join(f"{k}={record[k]}" for k in keys if record.get(k) is not None)


def build(session: sess.Session, journal: Path) -> str:
    """The dossier for one session, as plain text (target: under 5k tokens)."""
    page = _Page()
    records = [r for r in _jsonl(journal) if isinstance(r.get("wall"), str)]
    header = _read(session.path / "header.json")
    header = header if isinstance(header, dict) else {}
    hub = header.get("hub")
    hub = hub if isinstance(hub, dict) else {}
    built = report.build(journal)
    racks = sum(1 for r in records if r.get("kind") == "cmd_new_rack")
    first, last = (_wall(records[0]), _wall(records[-1])) if records else (session.started,) * 2
    page.add(f"# Session {session.stamp}")
    page.add(
        f"{first.astimezone():%a %d %b %Y}, {_local(first)}-{_local(last)} local "
        f"({_clock((last - first).total_seconds())}); {built.total} shots, "
        f"{racks} New Rack presses; "
        f"ruleset {hub.get('ruleset', '?')}, mode {hub.get('mode', '?')}"
    )
    page.add(f"dir {session.path}  journal {journal}")

    # Code: the session's against HEAD, and the checkpoints between.
    page.h("Code")
    sha = str(header.get("git_sha") or "")
    head = _git("rev-parse", "HEAD")
    page.add(
        f"session {sha[:7] or '?'}{' (dirty)' if header.get('git_dirty') else ''}  HEAD {head[:7]}"
    )
    if sha and head and sha != head:
        since = _git("log", "--reverse", "--format=%h %s", f"{sha}..HEAD")
        page.add("Landed since the session (a wrong verdict `review check` grades *changed* was")
        page.add("altered by one of these; read the session's code with `git show <sha>:<path>`):")
        for line in since.splitlines():
            page.add(f"  {line}")

    # Parity.
    page.h("Parity (replay.json)")
    replay = _read(session.path / finder.REPLAY_FILE)
    if isinstance(replay, dict):
        page.add(
            f"diff_count {replay.get('diff_count')}  ok {replay.get('ok')}  same_code "
            f"{replay.get('same_code')}  decisions {replay.get('live_decisions')}/"
            f"{replay.get('replay_decisions')}"
        )
        for diff in (replay.get("diffs") or [])[:5]:
            page.add(f"  DIFF {json.dumps(diff)[:200]}")
    else:
        page.add("no replay.json: parity was never checked (`cueclock session verify`)")

    # Footage.
    page.h("Footage")
    kept = session.full_recordings()
    starts = [r.start for r in kept]
    for rec in kept:
        stop = _local(rec.stop) if rec.stop else "?"
        page.add(f"kept recording {rec.path.name}: {_local(rec.start)}-{stop} local")
    if not kept:
        page.add("no kept recording (the newest 10 archived sessions keep theirs)")
    cuts = sorted(p.stem for p in (session.path / "cuts").glob("*.mp4"))
    page.add(f"cuts on disk: {len(cuts)}" + (f" ({', '.join(cuts)})" if cuts else ""))

    def position(when: datetime) -> str:
        before = [s for s in starts if s <= when]
        return _clock((when - max(before)).total_seconds()) if before else "-"

    # Presses.
    page.h("Presses (inputs.jsonl)")
    for press in _jsonl(session.path / "inputs.jsonl"):
        try:
            when = datetime.fromisoformat(str(press["wall"]))
        except (KeyError, ValueError):
            continue
        page.add(f"{_local(when)}  rec {position(when):>7}  {press.get('method')}")

    # Verdicts joined to moments.
    found = session.moments() or []
    verdicts = session.verdicts()
    page.h("Moments and verdicts (local time, full-recording position)")
    page.add(f"verdicts fingerprint {verdicts_fingerprint(session)} (a re-run compares it)")
    ids = {str(m.get("id")) for m in found}
    judged = [m for m in found if str(m.get("id")) in verdicts]
    wrong = [m for m in judged if verdicts[str(m["id"])].get("verdict") == "wrong"]
    page.add(
        f"{len(found)} moments, {len(judged)} judged ({len(wrong)} wrong), "
        f"{len(found) - len(judged)} unjudged"
    )
    for moment in sorted(found, key=lambda m: str(m.get("wall"))):
        mid = str(moment.get("id"))
        given = verdicts.get(mid)
        if given is None:
            continue
        when = datetime.fromisoformat(str(moment["wall"]))
        kinds = ",".join(moment.get("kinds") or [moment.get("kind")])  # type: ignore[arg-type]
        note = str(given.get("note") or "")
        page.add(
            f"{_local(when)}  rec {position(when):>7}  "
            f"{str(given.get('verdict')).upper():5}  {mid}  [{kinds}]"
            + (f'  note: "{note}"' if note else "")
        )
        page.add(f"    {moment.get('summary')}")
    orphans = sorted(set(verdicts) - ids)
    if orphans:
        page.add(
            f"verdicts for moments no longer listed (moments.json re-run?): {', '.join(orphans)}"
        )
    page.h("Unjudged (out of scope: ids only)")
    page.add(
        ", ".join(str(m.get("id")) for m in found if str(m.get("id")) not in verdicts) or "none"
    )

    # Episodes: what happened once, and what `review check` grades together.
    page.h("Co-located moments")
    timed = sorted(found, key=lambda m: str(m.get("wall")))
    episodes, shared = [], []
    for a, b in pairwise(timed):
        gap = (
            datetime.fromisoformat(str(b["wall"])) - datetime.fromisoformat(str(a["wall"]))
        ).total_seconds()
        if gap <= finder.MERGE_S:
            episodes.append((a, b, gap))
        elif str(b["start_wall"]) <= str(a["end_wall"]):
            shared.append((a, b, gap))

    def verdict_of(m: dict[str, object]) -> str:
        return str((verdicts.get(str(m.get("id"))) or {}).get("verdict", "unjudged"))

    for a, b, gap in episodes:
        conflict = "  CONFLICT" if {verdict_of(a), verdict_of(b)} == {"right", "wrong"} else ""
        page.add(
            f"same episode ({gap:.3f} s apart): {a['id']} {verdict_of(a)} / {b['id']} "
            f"{verdict_of(b)}{conflict}"
        )
    for a, b, gap in shared:
        page.add(
            f"shared window ({gap:.1f} s apart; `review check` grades a diff in one as both): "
            f"{a['id']} {verdict_of(a)} / {b['id']} {verdict_of(b)}"
        )
    if not episodes and not shared:
        page.add("none")

    # Aggregates, classified.
    page.h("Shots, classified")
    summary = next((r for r in reversed(records) if r.get("kind") == report.SUMMARY_KIND), None)
    if summary is not None:
        page.add(
            "as journaled at the session's end, by its own code: "
            + _brief(summary, ("shots", "missed", "phantom_candidates", "left_frozen", "scratches"))
        )
    rejected = ", ".join(f"{k} {n}" for k, n in sorted(built.settles_rejected_by_reason.items()))
    page.add(
        f"read by HEAD's report: {built.total} shots, {len(built.missed)} counted with no strike, "
        f"{len(built.phantom_candidates)} phantom candidates, {built.left_frozen} left frozen, "
        f"{built.settles_synthesized} synthesized, {built.provisional_reverts} reverts, "
        f"settles refused {built.settles_rejected} ({rejected or 'none'}), "
        f"{built.racking_live_settles} racking settles (not shots)"
    )
    page.add("The Detection row is `session-stats`' (one number; never a strike rate beside it).")
    for miss in classify_misses(records, built):
        at = _local(_wall(miss.settle)) if miss.settle else "?"
        t = f"@{miss.shot.t:.0f}s" if miss.shot.t is not None else "@?"
        page.add(
            f"  #{miss.shot.index} {t} {at}  {miss.why}  "
            f"settle[{_brief(miss.settle, ('moved', 'vanished', 'appeared', 'moved_mm', 'held'))}] "
            f"list[{_brief(miss.listing, ('row', 'cue_px', 'hand', 'counted'))}]"
        )
    stretches = racking_stretches(records)
    raw_back = raw_numbers_back(records)
    if raw_back:
        inside = sum(s.raw_back for s in stretches)
        page.add(
            f'ball list "numbers back", counted as before checkpoint 136: {raw_back}, '
            f"{inside} of them inside the racking stretches below"
        )
    for stretch in stretches:
        start = _wall(stretch.start)
        end = (
            f"until New Rack {_local(_wall(stretch.end))}"
            if stretch.end
            else "until the session ended"
        )
        page.add(
            f"racking on a live clock from {_local(start)} (rec {position(start)}): "
            f"{stretch.returned} numbers back on its first list ({stretch.raw_back} while it "
            f"lasted), {stretch.settles} settle(s), {end}"
        )
    revert_reasons = Counter(
        str(r.get("reason")) for r in records if r.get("kind") == report.PROVISIONAL_REVERTED_KIND
    )
    if revert_reasons:
        page.add(
            "reverts by reason: " + ", ".join(f"{k} {n}" for k, n in sorted(revert_reasons.items()))
        )
    held = held_by(records)
    page.add(
        "held settles, decided by: "
        + (", ".join(f"{k} {n}" for k, n in held.most_common()) or "none")
    )
    shapes = freeze_shapes(records)
    page.add(
        "freezes by (strike seen, tracker moved): "
        + ", ".join(
            f"{'strike' if s else 'no strike'}/{m} {n}" for (s, m), n in sorted(shapes.items())
        )
    )

    # Scratches and ball in hand.
    page.h("Scratches and ball in hand")
    for record in records:
        kind = record.get("kind")
        if kind in ("scratch_call", "ball_in_hand", "ball_in_hand_ended", report.SCRATCH_KIND):
            keys = ("by", "held", "closed", "says", "reason", "placed")
            page.add(
                f"{_local(_wall(record))}  r{record.get('rack_shots')}  {kind}  "
                f"{_brief(record, keys)}"
            )

    # Detect rate and voice.
    page.h("Detect rate and voice")
    hz = sorted(built.hz)
    if hz:
        starved = sum(1 for h in hz if h < report.STARVED_HZ)
        page.add(
            f"detect Hz: min {hz[0]:.0f}, p5 {hz[len(hz) // 20]:.0f}, median "
            f"{statistics.median(hz):.0f} over {len(hz)} heartbeats; "
            f"{starved} under {report.STARVED_HZ:.0f}"
        )
    voices = [r for r in records if r.get("kind") == "voice_final" and r.get("text")]
    extensions = [r for r in records if r.get("kind") == "cmd_extension"]
    page.add(
        f"voice: {len(voices)} finals with words, {sum(1 for r in voices if r.get('accepted'))} "
        f"accepted; {len(extensions)} extension commands"
    )
    return str(page)


def for_stamp(stamp: str, logs_dir: Path = Path("logs")) -> str:
    """The dossier for a session by its stamp. FileNotFoundError when either
    the session directory or its journal is missing."""
    session = sess.locate(stamp)
    if session is None:
        raise FileNotFoundError(f"no session directory {stamp} (is the T7 mounted?)")
    journal = finder.session_journal(session.path, logs_dir)
    if journal is None:
        raise FileNotFoundError(f"no journal for session {stamp}")
    return build(session, journal)
