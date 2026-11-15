"""Tiered accuracy table for cueclock sessions — the session-stats skill's miner.

Builds on `cueclock.server.report.build()` (the system of record for miss /
phantom / break counters) and adds the tier math plus two supplemental counts
the report does not carry: racks and vision-fired breaks, and which confessed
misses ran through EXPIRED on their way to the rescue.

Usage (repo root; safe any time — journal reading only, no camera, no corpus):
    .venv/bin/python3 .claude/skills/session-stats/stats.py             # newest session
    .venv/bin/python3 .claude/skills/session-stats/stats.py --last 2    # newest N merged
    .venv/bin/python3 .claude/skills/session-stats/stats.py --all       # everything
    .venv/bin/python3 .claude/skills/session-stats/stats.py logs/session-20260806-171905.jsonl
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, "src")

from cueclock.server.report import SessionReport, build, find_journals, mid_rack_appearances


@dataclass
class Tiers:
    shots: int = 0
    missed: int = 0
    expired_through: int = 0  # confessed misses that hit EXPIRED before the rescue
    racks: int = 0  # cmd_new_rack presses
    breaks_fired: int = 0  # vision fire counted the break (rack_shots 0 -> 1)
    breaks_adjudicated: int = 0  # E8 hub adjudication (checkpoint 75+)
    break_presses: int = 0  # cmd_restart counted the break (valid from 2026-08-05)
    dark_candidates: int = 0
    settles_synthesized: int = 0
    left_frozen: int = 0
    phantom_candidates: int = 0
    scratches_journaled: int = 0
    flags: int = 0
    mid_rack_appeared: int = 0  # mid-rack settles reporting a ball that was not there
    mid_rack_settles: int = 0  # eligible: rack_shots >= 2 on a 3+ ball baseline

    def add_report(self, r: SessionReport) -> None:
        self.shots += r.total
        self.missed += len(r.missed)
        self.breaks_adjudicated += r.breaks_adjudicated
        self.break_presses += r.break_presses
        self.dark_candidates += r.dark_candidates
        self.settles_synthesized += r.settles_synthesized
        self.left_frozen += r.left_frozen
        self.phantom_candidates += len(r.phantom_candidates)
        self.scratches_journaled += r.scratches
        self.flags += r.flags

    def add_supplemental(self, path: Path) -> None:
        """Racks, vision-fired breaks, and expired-through misses.

        Expired-through: a confessed miss (settle bumps rack_shots) whose
        freeze window contains an EXPIRED tick_transition — the false-foul
        buzz sounded before the settle rescued the shot. E9's grace hold
        (checkpoint 75) should pin this at 0 going forward.
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
            elif kind == "vision_balls_settled" and r.get("state") != "IDLE":
                if prev_rack is not None and rack is not None and rack > prev_rack:
                    window = rows[last_settle_i + 1 : i]
                    states = {w.get("state") for w in window if w.get("kind") == "tick_transition"}
                    if "EXPIRED" in states:
                        self.expired_through += 1
                last_settle_i = i
            if rack is not None:
                prev_rack = rack


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


def table(label: str, t: Tiers) -> str:
    breaks_caught = t.breaks_fired + t.breaks_adjudicated
    hands_on = t.break_presses + t.left_frozen + t.expired_through
    rows = [
        ("Shots / racks", f"{t.shots} / {t.racks}", ""),
        (
            "Strike detection",
            pct(t.shots - t.missed, t.shots),
            "vision fired on the stroke itself",
        ),
        (
            "Clock accuracy incl. self-healing",
            pct(t.shots - t.expired_through, t.shots),
            "clock ended up right on its own (self-heal = ran long, then reset)",
        ),
        (
            "Break auto-catch",
            f"{pct(breaks_caught, t.racks)}  ({t.breaks_fired} fired"
            + (f" + {t.breaks_adjudicated} adjudicated" if t.breaks_adjudicated else "")
            + f", {t.break_presses} by press)",
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
            "UPPER BOUND ONLY — channel over-counts ~6x, issue #26",
        ),
        (
            "Balls appeared mid-rack",
            f"{t.mid_rack_appeared} / {t.mid_rack_settles}  "
            f"({pct(t.mid_rack_appeared, t.mid_rack_settles)})",
            "settles past the break reporting a ball that was not there — lower is better",
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
                "E9 watchdog healed a frozen clock (the miss is counted above)",
            )
        )
    if t.flags:
        rows.append(("Flags pressed", str(t.flags), "each one lands in the triage-clips loop"))
    width = max(len(r[0]) for r in rows)
    out = [label, "=" * len(label)]
    for name, value, note in rows:
        out.append(f"  {name:<{width}}  {value}" + (f"   — {note}" if note else ""))
    return "\n".join(out)


def main() -> None:
    args = sys.argv[1:]
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
    print(table(label, mine(paths)))
    if not (args and args[0] == "--all"):
        print()
        print(table("ALL-TIME (context)", mine(find_journals(logs))))


if __name__ == "__main__":
    main()
