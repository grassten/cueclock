"""Accuracy table for cueclock sessions — the session-stats skill's miner.

Builds on `cueclock.server.report.build()` (the system of record for shot /
phantom / break counters) and adds the tier math plus two supplemental counts
the report does not carry: racks and vision-fired breaks, and which shots ran
through EXPIRED before their settle counted them.

Usage (repo root; safe any time — journal reading only, no camera, no corpus):
    .venv/bin/python3 .claude/skills/session-stats/stats.py             # newest session
    .venv/bin/python3 .claude/skills/session-stats/stats.py --last 2    # newest N merged
    .venv/bin/python3 .claude/skills/session-stats/stats.py --all       # everything
    .venv/bin/python3 .claude/skills/session-stats/stats.py logs/session-20260806-171905.jsonl
    ... --internals    # add the tracker's own rows (how often the strike path fired)
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, "src")

from cueclock.server.report import (
    Racking,
    SessionReport,
    build,
    find_journals,
    mid_rack_appearances,
)
from cueclock.server.scratch import OBJECT_MOVED_PX


@dataclass
class Tiers:
    shots: int = 0
    missed: int = 0  # counted at the settle with no strike fired (internal)
    expired_through: int = 0  # ... of those, hit EXPIRED before the settle counted them
    racking_live_settles: int = 0  # settles while racking with the clock on: not shots
    racks: int = 0  # cmd_new_rack presses
    breaks_fired: int = 0  # vision fire counted the break (rack_shots 0 -> 1)
    false_starts: int = 0  # ... taken back: the rack never moved (design §17)
    breaks_adjudicated: int = 0  # E8 hub adjudication (checkpoint 75+)
    break_presses: int = 0  # cmd_restart counted the break (valid from 2026-08-05)
    dark_candidates: int = 0
    settles_synthesized: int = 0
    left_frozen: int = 0
    phantom_candidates: int = 0
    scratches_journaled: int = 0
    scratches_unadjudicated: int = 0  # ... of those, from journals with no candidate records
    scratch_candidates: int = 0
    scratch_refuted: int = 0
    mid_rack_appeared: int = 0  # mid-rack settles reporting a ball that was not there
    mid_rack_settles: int = 0  # eligible: rack_shots >= 2 on a 3+ ball baseline
    # The ball list (#54), from its `ball_list` journal lines (checkpoint 127+).
    lists: int = 0
    lists_arm: int = 0
    settle_lists: int = 0  # settle lists with the tracker's count beside them
    settle_lists_agree: int = 0  # ... whose ball count equals the tracker's
    lists_duplicate: int = 0  # arm-free lists giving one number to two balls
    lists_two_cues: int = 0  # lists where a second circle passed the cue test (#73)
    numbers_returned: int = 0  # a number back after it left, within one rack
    listed_balls: int = 0  # object balls on arm-free lists
    unnumbered: int = 0  # ... listed with no number
    reassigned: int = 0  # ... that read another ball's number and took their next best (#65)
    # The scratch hold (#63), from `scratch_list` / `scratch_call` lines (checkpoint 132+).
    settles_judged: int = 0  # settles whose list the hub judged, or journaled missing
    settles_no_list: int = 0
    settles_arm: int = 0  # ... whose list had an arm over the table and no cue
    scratch_by: dict[str, int] = field(default_factory=dict)  # stood: candidate / list / both
    scratch_held: int = 0  # ... of those, the hold was granted
    scratch_missed: dict[str, int] = field(default_factory=dict)  # not granted: why
    cleared_by_list: int = 0  # candidates the list saw the cue against
    refuted_list_no_cue: int = 0  # refuted by today's logic while the list held no cue
    # Ball in hand (#64), from `scratch_list` rows and `ball_in_hand*` lines (checkpoint 133+).
    rows_judged: int = 0  # settles whose list said what they were
    placing: dict[str, int] = field(default_factory=dict)  # hand -> n, row ball_in_hand
    placing_phantom: int = 0  # ... kept off by #37 instead: the tracker saw nothing move
    in_hand: int = 0  # ball in hand periods begun (a scratch stood)
    in_hand_ended: dict[str, int] = field(default_factory=dict)  # why each one ended
    cue_moved: int = 0  # outside ball in hand: only the cue moved, seen rolling, counted
    # #67 (checkpoint 140+): open play, the cue ball moved by hand (row by_hand).
    by_hand: dict[str, int] = field(default_factory=dict)  # hand -> n
    # The cue ball on its spot (design §16, checkpoint 135+; the cue alone from 138).
    still_counted: int = 0  # a shot counted where the list read the cue on its spot
    # ... where that "shot" was the break: no break was played (every break
    # moves the cue ball), so the clock started while he racked. Not a shot.
    racking_starts: int = 0
    still_refused: int = 0  # freezes released because the list read the cue on its spot

    def add_report(self, r: SessionReport) -> None:
        self.shots += r.total
        self.missed += len(r.missed)
        self.racking_live_settles += r.racking_live_settles
        self.breaks_adjudicated += r.breaks_adjudicated
        self.break_presses += r.break_presses
        self.dark_candidates += r.dark_candidates
        self.settles_synthesized += r.settles_synthesized
        self.left_frozen += r.left_frozen
        self.phantom_candidates += len(r.phantom_candidates)
        self.scratches_journaled += r.scratches
        self.scratch_candidates += r.scratch_candidates
        self.scratch_refuted += r.scratch_refuted
        if not r.scratch_candidates:
            self.scratches_unadjudicated += r.scratches  # a pre-#26 journal's count

    def add_supplemental(self, path: Path) -> None:
        """Racks, vision-fired breaks, and expired-through misses.

        Expired-through: a shot counted at its settle (the settle bumps
        rack_shots) whose window contains an EXPIRED tick_transition — the
        false-foul buzz sounded before the settle counted the shot.
        """
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        prev_rack: float | None = None
        last_settle_i = -1
        for i, r in enumerate(rows):
            kind = r.get("kind")
            rack = r.get("rack_shots")
            if kind == "cmd_new_rack":
                self.racks += 1
            elif kind == "vision_shot_taken" and rack == 1 and prev_rack == 0:
                self.breaks_fired += 1
            elif kind == "shot_provisional_reverted" and r.get("reason") == "rack_standing":
                self.breaks_fired -= 1  # that fire was not a break
                self.false_starts += 1
            elif kind == "vision_balls_settled" and r.get("state") != "IDLE":
                if prev_rack is not None and rack is not None and rack > prev_rack:
                    window = rows[last_settle_i + 1 : i]
                    states = {w.get("state") for w in window if w.get("kind") == "tick_transition"}
                    if "EXPIRED" in states:
                        self.expired_through += 1
                last_settle_i = i
            if rack is not None:
                prev_rack = rack
        self.add_ball_lists([r for r in rows if r.get("kind") == "ball_list"])
        self.add_scratch_calls(rows)

    def add_scratch_calls(self, rows: list[dict[str, object]]) -> None:
        """#63: who called each scratch, whether it was held, and the list's
        side of every settle. A ``scratch_call`` line closes each shot that
        had a scratch called, at the next shot's motion."""
        by: Counter[str] = Counter(self.scratch_by)
        missed: Counter[str] = Counter(self.scratch_missed)
        in_hand = False
        for r in rows:
            kind = r.get("kind")
            if kind == "ball_in_hand":
                in_hand = True
            elif kind == "ball_in_hand_ended":
                in_hand = False
            if kind == "scratch_list":
                self.settles_judged += 1
                self.settles_no_list += r.get("verdict") == "no_list"
                self.settles_arm += r.get("says") == "arm"
                row = r.get("row")
                if row is not None:
                    self.rows_judged += 1
                    self.cue_moved += row == "cue_moved"
                    # `cue_px` from checkpoint 138; before it, `still` meant
                    # every ball read in place, a subset of the same error.
                    cue_px = r.get("cue_px")
                    on_spot = row == "still" or (
                        isinstance(cue_px, int | float) and cue_px <= OBJECT_MOVED_PX
                    )
                    if on_spot and r.get("counted"):
                        if r.get("rack_shots") == 1:
                            # Racking, not play (Steven, 2026-11-26): out of
                            # the shots `add_report` already counted.
                            self.racking_starts += 1
                            self.shots -= 1
                        else:
                            self.still_counted += 1
                    if row == "ball_in_hand":
                        how = str(r.get("hand"))
                        self.placing[how] = self.placing.get(how, 0) + 1
                    elif row == "by_hand":
                        how = str(r.get("hand"))
                        self.by_hand[how] = self.by_hand.get(how, 0) + 1
            elif kind == "vision_balls_settled":
                self.still_refused += r.get("rejected") == "nothing_moved"
                # A placing the tracker saw nothing move in is refused by #37
                # before #64 is asked: kept off the clock all the same.
                self.placing_phantom += in_hand and r.get("rejected") == "phantom"
            if kind == "ball_in_hand":
                self.in_hand += 1
            elif kind == "ball_in_hand_ended":
                why = str(r.get("reason"))
                self.in_hand_ended[why] = self.in_hand_ended.get(why, 0) + 1
            elif kind == "scratch_call":
                closed = r.get("closed")
                if closed == "cleared":
                    self.cleared_by_list += 1
                elif closed == "refuted":
                    self.refuted_list_no_cue += r.get("says") == "no_cue"
                else:
                    by[str(r.get("by"))] += 1
                    if r.get("held"):
                        self.scratch_held += 1
                    else:
                        missed[str(r.get("missed", "?"))] += 1
        self.scratch_by, self.scratch_missed = dict(by), dict(missed)

    def add_ball_lists(self, rows: list[dict[str, object]]) -> None:
        """The ball list's checks. Duplicates and returns are always errors.

        A return is judged on arm-free lists from the break on (`rack_shots`
        >= 1), so racking by hand does not count; the scope resets when
        `rack_shots` falls back (a new rack). A list with an arm can show a
        ball is there, never that one is gone, so it is never judged. Three
        or more numbers back on one list is racking begun with the clock
        still running, not three errors: nothing is judged from there to the
        next rack (`report.Racking`, #69).
        """
        racking = Racking()
        for r in rows:
            labels = str(r.get("balls") or "").split()
            self.lists += 1
            rack = r.get("rack_shots")
            racking.note_rack(rack if isinstance(rack, int) else 0)
            tracker = r.get("tracker")
            if r.get("reason") == "settle" and isinstance(tracker, int):
                self.settle_lists += 1
                self.settle_lists_agree += len(labels) == tracker
            # Two `cue` labels before checkpoint 139; since then the list keeps
            # one and journals how many it left off.
            self.lists_two_cues += labels.count("cue") > 1 or bool(r.get("cues_dropped"))
            if r.get("arm"):
                self.lists_arm += 1
                continue
            numbers = [n for n in labels if n.isdigit()]
            self.listed_balls += sum(n != "cue" for n in labels)
            self.unnumbered += labels.count("?")
            reassigned = r.get("reassigned")
            self.reassigned += reassigned if isinstance(reassigned, int) else 0
            self.lists_duplicate += len(numbers) != len(set(numbers))
            self.numbers_returned += racking.feed(r)


def pct(num: float, den: float) -> str:
    return f"{num / den:.2%}" if den else "—"


def mine(paths: list[Path]) -> Tiers:
    t = Tiers()
    for p in paths:
        t.add_report(build(p))
        t.add_supplemental(p)
        appeared, eligible = mid_rack_appearances(p)
        t.mid_rack_appeared += appeared
        t.mid_rack_settles += eligible
    return t


def table(label: str, t: Tiers, internals: bool = False) -> str:
    breaks_caught = t.breaks_fired + t.breaks_adjudicated
    hands_on = t.break_presses + t.left_frozen + t.expired_through
    left_out = [
        f"{n} {what}"
        for n, what in (
            (t.racking_live_settles, "settles while racking with the clock on"),
            (
                t.racking_starts,
                "clock starts while racking (a break with the cue ball on its spot)",
            ),
        )
        if n
    ]
    rows = [
        (
            "Shots / racks",
            f"{t.shots} / {t.racks}",
            "; ".join(left_out) + " left out" if left_out else "",
        ),
        (
            "Detection",
            pct(t.shots - t.expired_through - t.still_counted, t.shots),
            "the shot was counted and the clock was right with no key pressed"
            + (
                f"; {t.still_counted} counted with the cue ball on its spot"
                if t.still_counted
                else ""
            ),
        ),
        (
            "Break auto-catch",
            f"{pct(breaks_caught, t.racks)}  ({t.breaks_fired} fired"
            + (f" + {t.breaks_adjudicated} adjudicated" if t.breaks_adjudicated else "")
            + f", {t.break_presses} by press"
            + (f"; {t.false_starts} false starts taken back" if t.false_starts else "")
            + ")",
            "clock started off the break with no press (press count valid from 2026-08-05)",
        ),
        (
            "Hands-off play",
            pct(t.shots - hands_on, t.shots),
            f"no manual key needed ({t.break_presses} break presses, "
            f"{t.left_frozen} frozen rescues, {t.expired_through} false fouls)",
        ),
        (
            "False fouls",
            f"{t.expired_through}  ({pct(t.expired_through, t.shots)})",
            "buzzer through a live shot — E9 should hold this at 0",
        ),
        (
            "Phantom candidates",
            f"{t.phantom_candidates}  ({pct(t.phantom_candidates, t.shots)})",
            "review flags, not verdicts (mostly sub-1 s blips)",
        ),
        (
            "Scratches journaled",
            f"{t.scratches_journaled}",
            "none"
            if not t.scratches_journaled
            else f"UPPER BOUND ONLY — {t.scratches_unadjudicated} from pre-#26 journals, "
            "over-counted ~6x"
            if t.scratches_unadjudicated
            else f"adjudicated: {t.scratch_candidates} candidates, {t.scratch_refuted} refuted",
        ),
        (
            "Balls appeared mid-rack",
            f"{t.mid_rack_appeared} / {t.mid_rack_settles}  "
            f"({pct(t.mid_rack_appeared, t.mid_rack_settles)})",
            "settles past the break reporting a ball that was not there — lower is better",
        ),
    ]
    if t.lists:
        rows += [
            (
                "Ball lists written",
                f"{t.lists}  ({t.lists_arm} with an arm over the table)",
                "#54: each settle, and at rest whenever the list changed",
            ),
            (
                "List count = tracker's at a settle",
                f"{t.settle_lists_agree} / {t.settle_lists}  "
                f"({pct(t.settle_lists_agree, t.settle_lists)})",
                "disagreement says one of them is wrong, not which",
            ),
            (
                "Lists with a number twice",
                f"{t.lists_duplicate}  ({pct(t.lists_duplicate, t.lists - t.lists_arm)})",
                "arm-free lists; always an error",
            ),
            (
                "Lists with a second cue ball",
                f"{t.lists_two_cues}  ({pct(t.lists_two_cues, t.lists)})",
                "not a ball: a sleeve logo, a glove; left off the list since checkpoint 139",
            ),
            (
                "Numbers back after leaving",
                str(t.numbers_returned),
                "within one rack, from the break on; always an error",
            ),
            (
                "Numbers given up",
                f"{t.reassigned} / {t.listed_balls}  ({pct(t.reassigned, t.listed_balls)})",
                "read a number a better match had; took their next best (#65)",
            ),
            (
                "Balls with no number",
                f"{t.unnumbered} / {t.listed_balls}  ({pct(t.unnumbered, t.listed_balls)})",
                "mostly a stripe resting white cap up (the known open case)",
            ),
        ]
    if t.settles_judged:
        called = sum(t.scratch_by.values())
        why = ", ".join(f"{n} {k}" for k, n in sorted(t.scratch_missed.items()))
        rows += [
            (
                "Scratches, by who called them",
                f"{called}  ({t.scratch_by.get('candidate', 0)} today's logic only, "
                f"{t.scratch_by.get('list', 0)} the list only, {t.scratch_by.get('both', 0)} both)",
                "#63: the list-only count is the scratches the tracker never noticed",
            ),
            (
                "Scratch holds granted",
                f"{t.scratch_held} / {called}" + (f"  (not granted: {why})" if why else ""),
                "the reason is the clock's state, or no_clock / vision_off",
            ),
            (
                "Candidates cleared by the list",
                str(t.cleared_by_list),
                "the list saw the cue at the settle: no hold",
            ),
            (
                "Refuted while the list held no cue",
                str(t.refuted_list_no_cue),
                "today's refutation stands (either one seeing the cue counts)",
            ),
            (
                "Settles with no list / with an arm",
                f"{t.settles_no_list} / {t.settles_arm} of {t.settles_judged}",
                "today's logic decided those alone",
            ),
        ]
    if t.rows_judged:
        other = ", ".join(
            f"{n} {k}{' in open play' if where else ''}"
            for where, counts in ((False, t.placing), (True, t.by_hand))
            for k, n in sorted(counts.items())
            # In open play `not_held` is the clock idle: racking, not a game.
            if k != "applied" and not (where and k == "not_held")
        )
        ended = ", ".join(f"{n} {k}" for k, n in sorted(t.in_hand_ended.items()))
        rows += [
            (
                "Ball in hand (after a scratch)",
                f"{t.in_hand}" + (f"  (ended: {ended})" if ended else ""),
                "#64: from a standing scratch to the first settle that moves an object ball",
            ),
            (
                "Placings kept off the clock",
                f"{t.placing.get('applied', 0) + t.placing_phantom} after a scratch, "
                f"{t.by_hand.get('applied', 0)} in open play"
                + (
                    f"  ({t.placing_phantom} by #37: nothing seen to move)"
                    if t.placing_phantom
                    else ""
                )
                + (f"  (also {other})" if other else ""),
                "hand moves of the cue ball, never misses: after a scratch only the cue "
                "moved (#64); in open play it was never seen rolling (#67); refused = a "
                "shot already counted, late = decided before its list",
            ),
            (
                "Only the cue moved, open play",
                f"{t.cue_moved} of {t.rows_judged}",
                "counted; from checkpoint 140 the cue ball was seen rolling (a stick shot "
                "that touched nothing, a ball rolled or pushed), before it hand moves too",
            ),
            (
                "Counted with the cue ball on its spot",
                f"{t.still_counted} of {t.rows_judged}",
                "a shot counted where the list read the cue where it stood — always an "
                f"error; {t.still_refused} more were refused (§16)",
            ),
        ]
    if t.dark_candidates:
        rows.append(
            (
                "Dark break candidates",
                str(t.dark_candidates),
                "E8 shadow tier — the measurement that decides if it goes live",
            )
        )
    if t.settles_synthesized:
        rows.append(
            (
                "Settles synthesized",
                str(t.settles_synthesized),
                "E9 watchdog released a frozen clock",
            )
        )
    if internals:
        rows.append(
            (
                "Strike fired (internal)",
                pct(t.shots - t.missed, t.shots),
                f"tracker work only: {t.missed} shots were counted at their settle instead",
            )
        )
    width = max(len(r[0]) for r in rows)
    out = [label, "=" * len(label)]
    for name, value, note in rows:
        out.append(f"  {name:<{width}}  {value}" + (f"   — {note}" if note else ""))
    return "\n".join(out)


def main() -> None:
    args = sys.argv[1:]
    internals = "--internals" in args
    args = [a for a in args if a != "--internals"]
    logs = Path("logs")
    if args and args[0] == "--all":
        paths = find_journals(logs)
        label = f"ALL SESSIONS ({len(paths)} journals)"
    elif args and args[0] == "--last":
        n = int(args[1]) if len(args) > 1 else 1
        paths = find_journals(logs)[-n:]
        label = f"LAST {n} SESSION(S): " + ", ".join(p.stem for p in paths)
    elif args:
        paths = [Path(a) for a in args]
        label = ", ".join(p.stem for p in paths)
    else:
        paths = find_journals(logs)[-1:]
        label = f"NEWEST SESSION: {paths[0].stem}" if paths else "no journals"
    paths = [p for p in paths if p.exists()]
    if not paths:
        print("no session journals found under logs/")
        return
    print(table(label, mine(paths), internals))
    if not (args and args[0] == "--all"):
        print()
        print(table("ALL-TIME (context)", mine(find_journals(logs)), internals))


if __name__ == "__main__":
    main()
