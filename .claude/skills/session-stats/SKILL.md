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
| **Break auto-catch** | The clock started off the break with no press: vision fires + (checkpoint 75+) E8 `break_adjudicated`, **both tiers since #36 (2026-08-11)** — the dark tier now counts a no-fire break off the pack un-merging (`appeared >= 5`), which is the 15-25% of breaks that never started the clock. Denominator is `cmd_new_rack` presses. **The row #36 predicts will move.** |
| **Hands-off play** | Shots minus every manual intervention: break-rescue presses, frozen-clock rescues, false fouls. The felt-accuracy headline (§13.9 judges the whole arc on this). |
| **False fouls** | Buzzer sounded through a live shot. E9's grace hold should pin this at 0 from checkpoint 75 on — a nonzero value here is news, flag it. |
| **Phantom candidates** | `report`'s review symptoms (no roll-out / snap-back). Candidates, never verdicts. |
| **Balls appeared mid-rack** | Mid-rack settles (`rack_shots >= 2`, `baseline >= 3`) whose rearrangement reports `appeared >= 1`, over all such settles (`report.mid_rack_appearances`). Past the break balls only leave the table, so every appearance is a ball the tracker lost and found again, or a shadow it took for one. **Lower is better.** 88 / 711 (12.4%) all-time at the 2026-11-15 audit (#48/#52); the shadow-filter work is judged on this row. |
| **Scratches journaled** | Journals **with `vision_scratch_candidate` records** (checkpoint 77+, #26): `vision_scratch` is an adjudicated verdict — present it as a count, with candidates/refuted beside it (`report` prints all three). Journals **without** candidate records predate the two-stage verdict: **upper bound only**, over-counted ~6x (potted object balls mis-crowned as the cue; Steven ground truth 2026-08-06) — never present those as a real count. The adjudicator's one known leak keeps even new counts a slight over-bound: a real cue at rest in deep rail shade (0.53 whiteness vs the 0.72 witness floor) cannot refute its false candidate. |

**The ball list (#54, checkpoint 127+)** adds six rows, shown only for
journals that hold `ball_list` lines. They judge the list, not the clock:
the list never moves the clock until #63/#64 build on it.

| Row | Definition |
|---|---|
| **Ball lists written** | Every settle's list plus each list read at rest that differed from the last, and how many had an arm over the table (an arm list can show a ball is there, never that one is gone, so it is left out of the three checks below). |
| **List count = tracker's at a settle** | Settle lists whose ball count equals the tracker's present tracks at that frame. A disagreement says one of the two is wrong, not which. |
| **Lists with a number twice** | Arm-free lists giving one number to two balls. **Always an error.** The corpus had 2 in 162 rests. From checkpoint 131 (#65) it is 0 by construction: each number goes to one ball per list, so read the next row instead. |
| **Numbers back after leaving** | Within one rack, from the break on (`rack_shots >= 1`): a number missing from an arm-free list that turns up again later. **Always an error** (balls only leave the table past the break). |
| **Numbers given up** | Object balls on arm-free lists that read a number another ball matched better, and took their next best (`reassigned` on the `ball_list` line, checkpoint 131+; `own` in `balls.jsonl` says what each read). Not an error by itself: each one is a duplicate the list resolved, and only a by-eye look at its frame (`cueclock balls <session>`) says which way. Corpus: 2 in 1,482 readings at 162 rests, both a hand read as a ball. |
| **Balls with no number** | `?` on arm-free lists over all object balls listed: mostly a stripe resting with its white cap straight up, the known open case. |

Rows that appear only when nonzero: **dark break candidates** (E8's no-fire
tier while it was shadow-only, checkpoint 75 → #36 — **historical, pre-2026-08-11
journals only**; the tier went live in #36 keying on `appeared >= 5`, so a dark
catch now counts inside break auto-catch as a `break_adjudicated` with
`tier="dark"`), **settles synthesized** (E9 watchdog rescues), **provisional
shots** (#33: latch freezes / reverted-not-a-shot / suppressed settles —
reverts and suppressions are placements the clock now correctly ignores, not
misses).

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
reason to dig into the session's journal, not a reason to soften the summary.
