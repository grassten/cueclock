---
name: session-stats
description: Accuracy percentages for a cueclock session — detection, break auto-catch, hands-off play. Use after a session, or when Steven asks "how did the session go", "session stats", "what's my accuracy", or wants the percentage table. Journal-only and read-only — safe to run any time, including while a session is live.
---

# Session stats — the accuracy table

One command turns a session journal into the percentage table Steven asked for
(2026-08-06). **Detection is one number** (Steven, 2026-11-21: "That is 99%
detection."): a shot the settle counted was detected, exactly like one the
strike path caught. Never split it, and never present the internal
strike-fired rate beside it.

```bash
.venv/bin/python3 .claude/skills/session-stats/stats.py             # newest session + all-time context
.venv/bin/python3 .claude/skills/session-stats/stats.py --last 2    # newest N sessions merged
.venv/bin/python3 .claude/skills/session-stats/stats.py --all       # everything, no context block
.venv/bin/python3 .claude/skills/session-stats/stats.py logs/session-<stamp>.jsonl
.venv/bin/python3 .claude/skills/session-stats/stats.py --internals # tracker work only: adds the strike-fired row
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
| **Detection** | The shot was counted and the clock ended in the right state with no key pressed. A shot counted at its settle, with the countdown a few seconds long until then, is detected like any other. Two things count against it: a false foul, and (journals from checkpoint 132 on, which carry the ball list's `row`) a shot counted where the list read the cue ball on its spot — see **Counted with the cue ball on its spot** below. Older journals cannot see that second one, so their number is an upper bound: 17 of the 947 counted settles in the #33-era journals had no strike and no ball seen to move. |
| **Break auto-catch** | The clock started off the break with no press: vision fires + (checkpoint 75+) E8 `break_adjudicated`, **both tiers since #36 (2026-08-11)** — the dark tier now counts a no-fire break off the pack un-merging (`appeared >= 5`), which is the 15-25% of breaks that never started the clock. Denominator is `cmd_new_rack` presses. **The row #36 predicts will move.** A strike that started the clock and was taken back because the rack never moved (design §17, checkpoint 135+) is not a caught break; the row names how many. Before 135 those counted as fired, which is how a session could read over 100%. |
| **Hands-off play** | Shots minus every manual intervention: break-rescue presses, frozen-clock rescues, false fouls. The felt-accuracy headline (§13.9 judges the whole arc on this). |
| **False fouls** | Buzzer sounded through a live shot. E9's grace hold should pin this at 0 from checkpoint 75 on — a nonzero value here is news, flag it. |
| **Shots / racks** | Counted settles and `cmd_new_rack` presses. Settles while he racks with the clock still running are left out, and the note says how many: from the arm-free list that brings three or more numbers back at once until New Rack is pressed (`report.Racking`, #69). Before checkpoint 136 those read as shots, some of them as shots with no strike. Also left out: a break counted with the cue ball on its spot, which is the clock started while he racked (every break moves the cue ball; Steven, 2026-11-26: "I don't want this counted against the session stats"). 3 all-time to that day: two dark-tier fires on balls set into the rack, one a ball rolled down the table after New Rack. |
| **Phantom candidates** | `report`'s review symptoms (no roll-out / snap-back). Candidates, never verdicts. |
| **Balls appeared mid-rack** | Mid-rack settles (`rack_shots >= 2`, `baseline >= 3`) whose rearrangement reports `appeared >= 1`, over all such settles (`report.mid_rack_appearances`). Past the break balls only leave the table, so every appearance is a ball the tracker lost and found again, or a shadow it took for one. **Lower is better.** 88 / 711 (12.4%) all-time at the 2026-11-15 audit (#48/#52); the shadow-filter work is judged on this row. |
| **Scratches journaled** | Journals **with `vision_scratch_candidate` records** (checkpoint 77+, #26): `vision_scratch` is an adjudicated verdict — present it as a count, with candidates/refuted beside it (`report` prints all three). Journals **without** candidate records predate the two-stage verdict: **upper bound only**, over-counted ~6x (potted object balls mis-crowned as the cue; Steven ground truth 2026-08-06) — never present those as a real count. The adjudicator's one known leak keeps even new counts a slight over-bound: a real cue at rest in deep rail shade (0.53 whiteness vs the 0.72 witness floor) cannot refute its false candidate. |

**The ball list (#54, checkpoint 127+)** adds six rows, shown only for
journals that hold `ball_list` lines. They judge the list, not the clock;
since #63 a settle's list also has a say in the scratch hold (rows below).

| Row | Definition |
|---|---|
| **Ball lists written** | Every settle's list plus each list read at rest that differed from the last, and how many had an arm over the table (an arm list can show a ball is there, never that one is gone, so it is left out of the three checks below). |
| **List count = tracker's at a settle** | Settle lists whose ball count equals the tracker's present tracks at that frame. A disagreement says one of the two is wrong, not which. |
| **Lists with a number twice** | Arm-free lists giving one number to two balls. **Always an error.** The corpus had 2 in 162 rests. From checkpoint 131 (#65) it is 0 by construction: each number goes to one ball per list, so read the next row instead. |
| **Lists with a second cue ball** | Lists (an arm in frame or not) where a second circle passed the cue test: `cues_dropped` on the `ball_list` line, or two `cue` labels in journals before checkpoint 139. **Never a ball**: 21 of the 549 lists of the first four logged sessions, 19 the white logo on a black sleeve and 2 a gloved hand on the cloth (#73). From checkpoint 139 the list keeps the whitest as the cue and leaves the other off (design §20), so the strip and the clock never see two; this row is how often something that is not a ball still passes for one. It undercounts: a list that differs from the last one only by this is not written. |
| **Numbers back after leaving** | Within one rack, from the break on (`rack_shots >= 1`): a number missing from an arm-free list that turns up again later. **Always an error** (balls only leave the table past the break). Three or more back on one list is racking begun with the clock running, not three errors: nothing is judged from that list to the next rack (#69; session 20261121-153046 read 47 before, 8 after). |
| **Numbers given up** | Object balls on arm-free lists that read a number another ball matched better, and took their next best (`reassigned` on the `ball_list` line, checkpoint 131+; `own` in `balls.jsonl` says what each read). Not an error by itself: each one is a duplicate the list resolved, and only a by-eye look at its frame (`cueclock balls <session>`) says which way. Corpus: 2 in 1,482 readings at 162 rests, both a hand read as a ball. |
| **Balls with no number** | `?` on arm-free lists over all object balls listed: mostly a stripe resting with its white cap straight up, the known open case. |

**The scratch hold (#63, checkpoint 132+)** adds five rows, shown only for
journals that hold `scratch_list` lines. A `scratch_call` line closes each
shot that had a scratch called, at the next shot's motion, so the session's
very last shot can be missing from them.

| Row | Definition |
|---|---|
| **Scratches, by who called them** | Scratches that stood (not refuted, not cleared): today's logic only (a `ScratchCandidate`), the ball list only, or both. **The list-only count is the number nobody had before #63: scratches the tracker never noticed.** A list-only call that was not a scratch is a wrong call; check it on the review page (`list_scratch` moments). |
| **Scratch holds granted** | Holds granted out of the scratches above, with the reason for each one not granted: the clock's state when the shot's question closed (`IDLE`, `MOTION`, …), or `no_clock` / `vision_off`. A scratch with a live countdown and no hold is a defect. |
| **Candidates cleared by the list** | Today's logic raised a candidate and the settle's list saw the cue, so no hold (`list_cleared` moments on the review page). A real scratch here is a wrong clear. |
| **Refuted while the list held no cue** | Today's refutation stands even when the list misses the cue (either one seeing it counts). How often the two disagree that way. |
| **Settles with no list / with an arm** | Settles whose list never arrived before the next motion, and settles whose list had an arm over the table and no cue: today's logic decided those alone. Nonzero no-list counts are worth a look (a list takes 20–75 ms). |

**Ball in hand (#64, checkpoint 133+; #67, checkpoint 140+)** adds three rows, shown only for
journals whose `scratch_list` lines carry a `row`. Ball in hand runs from a
standing scratch to the first settle that moves an object ball.

| Row | Definition |
|---|---|
| **Ball in hand (after a scratch)** | Periods begun (`ball_in_hand` lines), and how each ended (`ball_in_hand_ended`: `shot`, `new_rack`, `refuted`). |
| **Placings kept off the clock** | Hand moves of the cue ball that counted nothing, so the clock kept running, split two ways. **After a scratch**: settles during ball in hand that moved only the cue (`row` `ball_in_hand`, `hand` `applied`), plus placings #37 refused first because the tracker saw nothing move at all (`rejected: phantom` during ball in hand; named in the row). **In open play** (checkpoint 140+, #67): settles whose cue ball was never seen rolling and that left every object ball in place (`row` `by_hand`, `hand` `applied`). They are never misses. In parentheses, for either: `refused` (a GRACE/IDLE fire had already counted a shot), `late` (the 0.3 s wait ran out, or a command, candidate or refutation decided it first; since checkpoint 136 the next motion or strike waits behind the settle instead), `not_held` (clock idle or off). A `late` or `refused` one restarted the countdown. **A placing that was a real shot sends #64 or #67 back** (the worst error); each is a `placing` moment on the review page, and each counted settle that ended ball in hand an `in_hand_shot` moment (Steven's shot, or a placing missed). |
| **Only the cue moved, open play** | Counted settles with no scratch before them where the cue moved and no object ball did, out of settles with a row (`row` `cue_moved`). From checkpoint 140 the cue ball was seen rolling at every one: a stick shot that touched nothing (a foul, so the fresh countdown is right), or a cue ball rolled or pushed by hand or shaft, which #67 leaves counting. Before 140 the row also held hand moves. Each is a `cue_only` moment on the review page. |
| **Counted with the cue ball on its spot** | Settles that counted a shot while their ball list read the cue within 3 px of where it stood before the motion (`cue_px` ≤ 3 on the `scratch_list` line, or `row` `still`; in journals before checkpoint 138 only `row` `still`, which also needed every object ball read in place), out of settles with a row. **Always an error**: every shot moves the cue ball, so the countdown restarted and no shot was played (first seen 20261119-020755 at 176 s: a sleeve beside the ball at the side pocket; then 20261121-153046 at 699 s and 1565 s: a gloved bridge hand read as a second cue ball, #73). It counts against Detection. The hub refuses these when the settle is held for its list (a freeze with no ball displaced, or one ball and no strike: design §16, §18.2, §19) and the list arrives in time; the note gives how many it refused. What is left here is a list that came late, or a settle with a strike or two balls moved: each is a `no_change` moment on the review page. One at the break (`rack_shots` 1) is not counted here: it is a clock start while racking, left out of the shots (see **Shots / racks**). |

Rows that appear only when nonzero: **dark break candidates** (E8's no-fire
tier while it was shadow-only, checkpoint 75 → #36 — **historical, pre-2026-08-11
journals only**; the tier went live in #36 keying on `appeared >= 5`, so a dark
catch now counts inside break auto-catch as a `break_adjudicated` with
`tier="dark"`), **settles synthesized** (E9 watchdog rescues; a break over a standing rack is taken back instead since checkpoint 136 and shows as a false start), **strike fired (internal)** (`--internals` only: how often the strike path fired, for tracker work; never the detection number), **provisional
shots** (#33: latch freezes / reverted-not-a-shot / suppressed settles —
reverts and suppressions are placements the clock now correctly ignores, not
misses).

## Known caveats, so the numbers are read honestly

- **Break presses are only countable from 2026-08-05** — before that the
  Restart key didn't bump `rack_shots` 0→1, so all-time break rows undercount
  manual rescues (the true pre-08-05 rate was ~15-25% of racks, mined from
  press timing in accuracy plan §13.0).
- **A shot with neither a strike nor a settle leaves no trace** (`report`'s
  floor, not ceiling) — the backstop (checkpoint 75) narrows but does not
  close this.
- **Journals from #33 on defer RUNNING fires to the settle**, so `rack_shots`
  bumps at the settle even for seen strikes; `report` marks those settles
  seen via the open strike record. The internal strike-fired rate stays
  comparable across the boundary.
- Strategy questions ("is it getting better?") get answered from this table
  plus the trend across sessions — never by opening clips (Steven, 2026-08-05).

## After printing the table

Give the one-line summary in plain terms (which tier moved, which didn't, and
what that means at the table), and check the standing predictions: after
checkpoint 75, `break auto-catch` should rise toward ~100% of *fired* breaks
and `false fouls` should read 0. Report the numbers back whether or not they
moved — including when they got worse. If a tier regressed, that is a
reason to dig into the session's journal, not a reason to soften the summary.
