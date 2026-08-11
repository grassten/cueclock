---
name: session-stats
description: Tiered accuracy percentages for a cueclock session — strike detection, accuracy including self-healing, break auto-catch, hands-off play. Use after a session, or when Steven asks "how did the session go", "session stats", "what's my accuracy", or wants the percentage table. Journal-only and read-only — safe to run any time, including while a session is live.
---

# Session stats — the tiered accuracy table

One command turns a session journal into the percentage table Steven asked for
(2026-08-06): not one accuracy number but tiers, because "vision missed the
strike" and "the clock ended up wrong" are different failures with different
costs, and most misses self-heal invisibly.

```bash
.venv/bin/python3 .claude/skills/session-stats/stats.py             # newest session + all-time context
.venv/bin/python3 .claude/skills/session-stats/stats.py --last 2    # newest N sessions merged
.venv/bin/python3 .claude/skills/session-stats/stats.py --all       # everything, no context block
.venv/bin/python3 .claude/skills/session-stats/stats.py logs/session-<stamp>.jsonl
```

Use `.venv/bin/python3`, never `uv run` (concurrent `uv run`s block). The
script only reads `logs/*.jsonl` — no camera, no corpus, no contention — so
unlike replays and batteries it is safe while `cueclock run` is live.

## The tiers, and what each one means

Definitions are load-bearing; don't improvise new ones in conversation — if a
new tier is worth having, add it to `stats.py` so every future session
computes it identically.

| Tier | Definition |
|---|---|
| **Strike detection** | Vision fired on the stroke itself. The raw number; E7 (tracker) territory. ~97% and flat as of 2026-08-06. |
| **Clock accuracy incl. self-healing** | The clock ended in the right state with no human help. A self-healed miss (countdown ran a few seconds long, then the settle reset it — `report`'s confession mechanism) counts as success; only a false foul counts against it. |
| **Break auto-catch** | The clock started off the break with no press: vision fires + (checkpoint 75+) E8 `break_adjudicated`. Denominator is `cmd_new_rack` presses. **The row checkpoint 75 predicts will move.** |
| **Hands-off play** | Shots minus every manual intervention: break-rescue presses, frozen-clock rescues, false fouls. The felt-accuracy headline (§13.9 judges the whole arc on this). |
| **False fouls** | Buzzer sounded through a live shot. E9's grace hold should pin this at 0 from checkpoint 75 on — a nonzero value here is news, flag it. |
| **Phantom candidates** | `report`'s review symptoms (no roll-out / snap-back). Candidates, never verdicts. |
| **Scratches journaled** | Journals **with `vision_scratch_candidate` records** (checkpoint 77+, #26): `vision_scratch` is an adjudicated verdict — present it as a count, with candidates/refuted beside it (`report` prints all three). Journals **without** candidate records predate the two-stage verdict: **upper bound only**, over-counted ~6x (potted object balls mis-crowned as the cue; Steven ground truth 2026-08-06) — never present those as a real count. The adjudicator's one known leak keeps even new counts a slight over-bound: a real cue at rest in deep rail shade (0.53 whiteness vs the 0.72 witness floor) cannot refute its false candidate. |

Rows that appear only when nonzero: **dark break candidates** (E8's no-fire
tier — measured OFF 2026-08-10, #33 Part C: the armed-rack racking settle at
21:07:56 sat inside the break population on both involved-count and
settle-spread, so the tier stays journal-only until a discriminator separates
on data), **settles synthesized** (E9 watchdog rescues), **provisional
shots** (#33: latch freezes / reverted-not-a-shot / suppressed settles —
reverts and suppressions are placements the clock now correctly ignores, not
misses), **flags pressed** (each lands in the `triage-clips` loop).

## Known caveats, so the numbers are read honestly

- **Break presses are only countable from 2026-08-05** — before that the
  Restart key didn't bump `rack_shots` 0→1, so all-time break rows undercount
  manual rescues (the true pre-08-05 rate was ~15-25% of racks, mined from
  press timing in accuracy plan §13.0).
- **A miss whose settle was also missed leaves no trace** (`report`'s floor,
  not ceiling) — the backstop (checkpoint 75) narrows but does not close this.
- **Journals from #33 on defer RUNNING fires to the settle**, so `rack_shots`
  bumps at the settle even for seen strikes; `report` marks those settles
  seen via the open strike record. Strike-detection rates stay comparable
  across the boundary — a pre-#33 confessed miss and a post-#33 one read the
  same way.
- Strategy questions ("is it getting better?") get answered from this table
  plus the trend across sessions — never by opening clips (Steven, 2026-08-05).

## After printing the table

Give the one-line summary in plain terms (which tier moved, which didn't, and
what that means at the table), and check the standing predictions: after
checkpoint 75, `break auto-catch` should rise toward ~100% of *fired* breaks
and `false fouls` should read 0. Report the numbers back whether or not they
moved — including when they got worse. If a tier regressed, that is a
`triage-clips` trigger, not a reason to soften the summary.
