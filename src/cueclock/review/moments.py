"""Candidate moments in one session, for Steven to judge right or wrong (#57).

A session is ~30 minutes and ~70 shots; nobody re-watches that. What is worth
a look is the handful of places where the journal itself says something may
have gone wrong, so this module reads the journal and lists those:

    reverted     a freeze the hub later released as not-a-shot (#33)
    phantom      `report`'s phantom candidates: settled under 1 s after the
                 strike, or froze again under 2.5 s after the last settle
    correction   Steven had to press Restart / Start / Reset, or New Rack
                 while the clock sat frozen
    idle_strike  vision saw a strike while the clock was IDLE (a missed break
                 looks exactly like this)
    synthesized  the frozen-clock watchdog had to invent a settle
    list_scratch the ball list called a scratch today's logic never noticed (#63)
    list_cleared the ball list saw the cue and cleared today's scratch candidate
    placing      a hand move of the cue ball was kept off the clock: during
                 ball in hand after a scratch (#64), or in open play, where
                 the cue ball was never seen rolling (#67). Where a real shot
                 refused would show
    in_hand_shot ball in hand ended on a counted settle: Steven's shot, or a
                 placing the ball list missed
    cue_only     open play: a counted settle that moved only the cue — a stick
                 shot that touched nothing, a ball rolled or pushed, or a hand
                 move counted because its list came late
    no_change    a shot was counted at a settle whose ball list read the cue
                 ball on its spot: the clock restarted and no shot was played
                 (§16 refuses these when it can; this is the rest)
    no_strike    `report`'s "counted at settle": a shot was counted and no
                 strike was seen — a soft shot, or nothing at all (#73)
    quiet        the clock ran longer than any legal turn with nothing seen
    replay       the offline replay disagreed with the live session

Each is a **candidate**, never a verdict: the page shows the footage and
Steven decides. The phantom rules are not re-derived here — they are
`server/report.py`'s, read through `report.build`, so the review page and the
accuracy report can never disagree about what a phantom candidate is.

Candidates raised within `MERGE_S` of each other are **one moment**: one
thing happened at the table, and it gets one card and one verdict (#69). The
card carries every candidate's sentence and kind; its id and first kind are
the earliest candidate's.

Ids are ``<kind>-<mono in ms>`` of the record that raised the moment, so a
re-run over the same journal yields the same ids and a verdict already given
stays attached to its moment.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

from cueclock.server import report

MOMENTS_FILE = "moments.json"
JOURNAL_FILE = "journal.jsonl"
REPLAY_FILE = "replay.json"

# The span a moment's cut covers around its record: 12 s before (the address
# and the strike) and 8 s after (the roll-out or the reaction). ~20 s is the
# cut size the #57 research pass costed at ~16 MB.
PRE_S = 12.0
POST_S = 8.0

# Records of one kind closer together than this are one moment. Racking writes
# idle strikes in bursts and a fumbled correction is several key presses; each
# burst is one thing to look at.
CLUSTER_S = 10.0

# Candidates raised this close together are one moment, whatever their kinds.
# In session 20261121-153046 one settle raised `in_hand_shot` and `phantom`
# 1 ms apart and the adjustment after it `cue_only` 1.4 s later; as three
# cards they were judged wrong, right and wrong. The next nearest pair of
# candidates in the seven archived sessions is 7.0 s apart.
MERGE_S = 2.0

# No strike, settle or key for this long with the clock not IDLE is a turn
# that outran the longest legal allowance — the 60 s first shot plus a 30 s
# extension (WNT §5, `clock/rulesets.py`). Either nothing was played or
# vision missed a shot *and* its settle, which `report` cannot see at all.
QUIET_S = 90.0

# Commands that only ever mean "the clock got it wrong and I fixed it".
# `cmd_new_rack` is the normal end of a rack, so it counts only when it had
# to rescue a frozen clock.
CORRECTION_KINDS = {"cmd_restart", "cmd_start", "cmd_reset"}
RESCUE_KIND = "cmd_new_rack"

IDLE_STRIKE_KIND = "vision_shot_taken_ignored_idle"

# What keeps a turn from being "quiet": anything vision or Steven did.
_ACTIVITY_PREFIXES = ("vision_", report.COMMAND_PREFIX, "shot_provisional", "settle_", "break_")


@dataclass(frozen=True)
class Moment:
    id: str
    wall: str  # ISO UTC, the record that raised it
    kind: str
    summary: str
    start_wall: str  # the span a cut should cover
    end_wall: str
    kinds: tuple[str, ...] = ()  # every candidate on the card, `kind` first


def _iso(when: datetime) -> str:
    return when.isoformat(timespec="milliseconds")


def _records(path: Path) -> list[dict[str, object]]:
    """The journal's records; torn lines are skipped (the journal's contract)."""
    out: list[dict[str, object]] = []
    for line in path.read_text().splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and isinstance(record.get("wall"), str):
            out.append(record)
    return out


def _mono(record: dict[str, object]) -> float:
    value = record.get("mono")
    return float(value) if isinstance(value, int | float) else 0.0


def _capture_t(record: dict[str, object]) -> float | None:
    """The capture time `report.Shot.t` keeps, read the way `report.build` reads it."""
    for key in ("frame_t", "t"):
        value = record.get(key)
        if isinstance(value, int | float) and value:
            return float(value)
    return None


def _moment(
    kind: str,
    record: dict[str, object],
    summary: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> Moment:
    wall = datetime.fromisoformat(str(record["wall"]))
    return Moment(
        id=f"{kind}-{round(_mono(record) * 1000)}",
        wall=_iso(wall),
        kind=kind,
        summary=summary,
        start_wall=_iso(start or wall - timedelta(seconds=PRE_S)),
        end_wall=_iso(end or wall + timedelta(seconds=POST_S)),
        kinds=(kind,),
    )


def _merged(moments: list[Moment]) -> list[Moment]:
    """One moment per run of candidates each within `MERGE_S` of the one
    before: the earliest keeps its id, wall and kind, the summaries follow
    each other, and the span covers them all. ``moments`` is oldest first.

    A `quiet` stretch stays its own card: it is the 90 s *after* the record
    it is stamped with, a different thing to watch than whatever that record
    also raised.

    A `no_strike` candidate never names a card it shares. It is raised at the
    settle record, ahead of everything that settle's list raises a few
    milliseconds later, and a card judged before the kind existed has to keep
    its id for its verdict to stay attached."""
    out: list[Moment] = []
    last: datetime | None = None
    for moment in moments:
        if moment.kind == "quiet":
            out.append(moment)
            last = None
            continue
        wall = datetime.fromisoformat(moment.wall)
        if out and last is not None and (wall - last).total_seconds() <= MERGE_S:
            card = out[-1]
            lead, tail = (moment, card) if card.kind == "no_strike" else (card, moment)
            out[-1] = replace(
                lead,
                summary=f"{lead.summary} {tail.summary}",
                start_wall=min(card.start_wall, moment.start_wall),
                end_wall=max(card.end_wall, moment.end_wall),
                kinds=(*lead.kinds, *(k for k in tail.kinds if k not in lead.kinds)),
            )
        else:
            out.append(moment)
        last = wall
    return out


def _clustered(
    kind: str,
    records: list[dict[str, object]],
    describe: Callable[[list[dict[str, object]]], str],
) -> list[Moment]:
    """One moment per burst of `records` (each within `CLUSTER_S` of the last)."""
    bursts: list[list[dict[str, object]]] = []
    for record in records:
        if bursts and _mono(record) - _mono(bursts[-1][-1]) <= CLUSTER_S:
            bursts[-1].append(record)
        else:
            bursts.append([record])
    out = []
    for burst in bursts:
        last = datetime.fromisoformat(str(burst[-1]["wall"]))
        out.append(_moment(kind, burst[0], describe(burst), end=last + timedelta(seconds=POST_S)))
    return out


def _rack(record: dict[str, object]) -> str:
    shots = record.get("rack_shots")
    return f"rack shot {shots}" if isinstance(shots, int) else "rack shot ?"


def _key_name(kind: str) -> str:
    return kind.removeprefix(report.COMMAND_PREFIX).replace("_", " ").title()


def find(journal: Path, replay: Path | None = None) -> list[Moment]:
    """Every candidate moment in one session journal, oldest first."""
    records = _records(journal)
    moments: list[Moment] = []

    # Phantom candidates, exactly as `cueclock report` classifies them. A
    # `Shot` is born at its settle record, whose capture time it keeps.
    settles = {
        t: r
        for r in records
        if r.get("kind") in (report.SETTLE_KIND, report.SETTLE_SYNTHESIZED_KIND)
        and (t := _capture_t(r)) is not None
    }
    accuracy = report.build(journal)
    for shot in accuracy.phantom_candidates:
        settle = settles.get(shot.t) if shot.t is not None else None
        if settle is None:
            continue
        if shot.no_rollout and shot.settle_gap_s is not None:
            why = f"settled {shot.settle_gap_s:.2f} s after the strike — no time for a roll-out"
        else:
            why = f"froze again {shot.address_gap_s or 0:.2f} s after the previous settle"
        moments.append(_moment("phantom", settle, f"Possible phantom: {why} ({_rack(settle)})."))
    # Every shot counted with no strike behind it (#73). Until this kind the
    # count was only a line in `report`: in session 20261121-153046 two of
    # them restarted the countdown with no shot played and raised nothing.
    # 100 in the 56 journals with shots, 1.8 a session.
    for shot in accuracy.missed:
        settle = settles.get(shot.t) if shot.t is not None else None
        if settle is not None:
            moments.append(
                _moment(
                    "no_strike",
                    settle,
                    f"A shot was counted and no strike was seen: a soft shot, or nothing "
                    f"at all? ({_rack(settle)}).",
                )
            )

    prev_state: object = None
    quiet_from: dict[str, object] | None = None  # last activity while not IDLE
    quiet_flagged = False
    corrections: list[dict[str, object]] = []
    idle_strikes: list[dict[str, object]] = []
    list_settle: dict[str, object] | None = None  # the last settle the list judged
    for record in records:
        kind = str(record.get("kind"))
        state = record.get("state")
        if kind == "scratch_list":
            list_settle = record
            if record.get("hand") == "applied" and record.get("row") == "ball_in_hand":
                moments.append(
                    _moment(
                        "placing",
                        record,
                        f"Ball in hand: only the cue moved, so no shot was counted and the "
                        f"clock kept running ({_rack(record)}).",
                    )
                )
            elif record.get("hand") == "applied" and record.get("row") == "by_hand":
                moments.append(
                    _moment(
                        "placing",
                        record,
                        f"The cue ball was moved by hand (never seen rolling, no other ball "
                        f"moved), so no shot was counted and the clock kept running "
                        f"({_rack(record)}).",
                    )
                )
            elif record.get("row") == "still" and record.get("counted"):
                moments.append(
                    _moment(
                        "no_change",
                        record,
                        f"A shot was counted, and the ball list read the cue ball on its "
                        f"spot: the clock restarted with no shot played ({_rack(record)}).",
                    )
                )
            elif record.get("row") in ("cue_moved", "lifted", "by_hand") and record.get("counted"):
                # Labels for the open-play cue-only settles that counted: a
                # stick shot that touched nothing, a ball rolled or pushed, or
                # a hand move whose list came too late (#67's limits).
                moments.append(
                    _moment(
                        "cue_only",
                        record,
                        f"Only the cue moved and a shot was counted: a stick shot that "
                        f"touched nothing, or the cue moved by hand? ({_rack(record)}).",
                    )
                )
        elif kind == "ball_in_hand_ended" and record.get("reason") == "shot":
            # Journaled by the settle that counted, so it marks that settle.
            moments.append(
                _moment(
                    "in_hand_shot",
                    record,
                    f"Ball in hand ended on a counted shot after {record.get('placed')} "
                    f"placing(s) kept off the clock; if this was still placing, the clock "
                    f"restarted wrongly ({_rack(record)}).",
                )
            )
        elif kind == "scratch_call":
            # One line per shot with a scratch called, closed at the next
            # shot's motion: the cut belongs at the settle the list judged.
            moment = _list_moment(record, list_settle or record)
            if moment is not None:
                moments.append(moment)
        elif kind == report.PROVISIONAL_REVERTED_KIND and record.get("reason") not in (
            "ball_in_hand",
            "by_hand",
        ):
            # A placing's revert is its own moment, at its list.
            remaining = record.get("remaining")
            back = f"{remaining:.0f}" if isinstance(remaining, int | float) else "?"
            if record.get("reason") == "rack_standing":
                # §17: not a freeze at all, the break itself was taken back.
                what = (
                    "A strike started the clock before the break, the rack never moved, "
                    "and the clock went back to waiting for the break."
                )
            else:
                what = (
                    f"A freeze was released as not-a-shot; the clock went back to "
                    f"{back} s ({_rack(record)})."
                )
            moments.append(_moment("reverted", record, what))
        elif kind == report.SETTLE_SYNTHESIZED_KIND:
            quiet = record.get("quiet_s")
            moments.append(
                _moment(
                    "synthesized",
                    record,
                    f"The clock sat frozen {quiet} s with nothing moving, so the "
                    f"watchdog released it ({_rack(record)}).",
                )
            )
        elif kind in CORRECTION_KINDS or (kind == RESCUE_KIND and prev_state == "MOTION"):
            corrections.append({**record, "_before": prev_state})
        elif kind == IDLE_STRIKE_KIND:
            idle_strikes.append(record)

        # Quiet stretches: reset on any activity or on IDLE.
        if state == "IDLE":
            quiet_from, quiet_flagged = None, False
        elif kind.startswith(_ACTIVITY_PREFIXES) or quiet_from is None:
            quiet_from, quiet_flagged = record, False
        elif not quiet_flagged and _mono(record) - _mono(quiet_from) >= QUIET_S:
            start = datetime.fromisoformat(str(quiet_from["wall"]))
            moments.append(
                _moment(
                    "quiet",
                    quiet_from,
                    f"The clock ran {QUIET_S:.0f} s with no strike, settle or key "
                    f"seen ({_rack(quiet_from)}).",
                    start=start,
                    end=start + timedelta(seconds=QUIET_S),
                )
            )
            quiet_flagged = True
        if state is not None:
            prev_state = state

    moments += _clustered(
        "correction",
        corrections,
        lambda burst: (
            "Pressed "
            + ", then ".join(_key_name(str(r["kind"])) for r in burst)
            + f" (clock was {burst[0]['_before'] or '?'}, {_rack(burst[0])})."
        ),
    )
    moments += _clustered(
        "idle_strike",
        idle_strikes,
        lambda burst: (
            f"Vision saw {'a strike' if len(burst) == 1 else f'{len(burst)} strikes'} "
            "while the clock was IDLE — a missed break looks like this."
        ),
    )
    if replay is not None and replay.exists():
        moments += _replay_moments(replay, records)
    return _merged(sorted(moments, key=lambda m: m.wall))


def _list_moment(call: dict[str, object], settle: dict[str, object]) -> Moment | None:
    """#63: the two ways the ball list can be wrong in front of Steven — a
    scratch it called alone, and a candidate it cleared."""
    if call.get("by") == "list":
        held = "held 5 s" if call.get("held") else f"not held ({call.get('missed', '?')})"
        return _moment(
            "list_scratch",
            settle,
            f"The ball list called a scratch the tracker never noticed; the clock was "
            f"{held} ({_rack(settle)}).",
        )
    if call.get("closed") == "cleared":
        return _moment(
            "list_cleared",
            settle,
            f"Today's logic suspected a scratch; the ball list saw the cue and cleared it, "
            f"so the clock was not held ({_rack(settle)}).",
        )
    return None


def _replay_moments(path: Path, records: list[dict[str, object]]) -> list[Moment]:
    """The parity check's disagreements, each placed by its wall or mono time."""
    try:
        verdict = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    diffs = verdict.get("diffs") if isinstance(verdict, dict) else None
    if not isinstance(diffs, list):
        return []
    out = []
    for diff in diffs:
        if not isinstance(diff, dict):
            continue
        anchor = _anchor(diff, records)
        if anchor is None:
            continue
        text = diff.get("summary") or diff.get("detail") or json.dumps(diff, sort_keys=True)
        out.append(_moment("replay", anchor, f"Replay disagrees with the live session: {text}"))
    return out


def _anchor(diff: dict[str, object], records: list[dict[str, object]]) -> dict[str, object] | None:
    """A `{wall, mono}` for a diff: its own if it has both, else the nearest record's."""
    if isinstance(diff.get("wall"), str) and isinstance(diff.get("mono"), int | float):
        return diff
    for key in ("mono", "wall"):
        value = diff.get(key)
        if isinstance(value, int | float | str) and records:
            if key == "mono":
                return min(records, key=lambda r: abs(_mono(r) - float(value)))
            target = datetime.fromisoformat(str(value))
            return min(
                records,
                key=lambda r: abs(
                    (datetime.fromisoformat(str(r["wall"])) - target).total_seconds()
                ),
            )
    return None


def session_journal(session_dir: Path, logs_dir: Path = Path("logs")) -> Path | None:
    """The session's journal: the archived copy, else the live one in `logs/`."""
    archived = session_dir / JOURNAL_FILE
    if archived.exists():
        return archived
    live = logs_dir / f"session-{session_dir.name}.jsonl"
    return live if live.exists() else None


def write(session_dir: Path, logs_dir: Path = Path("logs")) -> list[Moment]:
    """Find a session's moments and write `moments.json` beside them (atomically)."""
    journal = session_journal(session_dir, logs_dir)
    if journal is None:
        raise FileNotFoundError(f"no journal for session {session_dir.name}")
    moments = find(journal, session_dir / REPLAY_FILE)
    session_dir.mkdir(parents=True, exist_ok=True)
    write_json(session_dir / MOMENTS_FILE, [asdict(m) for m in moments])
    return moments


def write_json(path: Path, data: object) -> None:
    """Write-then-rename, so a reader never sees half a file.

    Into a directory that exists: a session the archive pass has just moved
    to the T7 must not come back as a directory holding one file (the next
    pass would copy that file over the archived one), so a write into a
    directory that is gone fails with FileNotFoundError.
    """
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=1) + "\n")
    os.replace(tmp, path)
