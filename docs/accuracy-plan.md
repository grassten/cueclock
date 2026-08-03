# Accuracy plan

Scoped, decided accuracy work with a validation path for each item. **This is
not an idea bank** — the retired `docs/accuracy-ideas.md` was one, and it was
deleted 2026-08-02 because a list of maybes competes with the board for
attention. Everything here is either a decision already made or work with a
named clip that grades it. An item with no evidence and no gradeable clip does
not belong in this file; it belongs nowhere until a failure implicates it.

Board: <https://github.com/users/grassten/projects/3>. Issues live on
`grassten/cueclock`.

## Decisions that set this up (2026-08-02)

1. **The acoustic click corroborator is rejected.** Steven is not committing to
   a mic for shot detection — different tips, stroke powers, and room noise
   make the click unreliable. It had been the designed answer for the whole
   sub-impulse class since 2026-07-27. Issues #5, #8, #11 no longer depend on
   it or on any purchase. `hardware.md` Part D records it as rejected.
   Mic hardware is still wanted for the shipped voice "extension" (#2) —
   a separate use, not rejected.
2. **The idea bank is retired.** Accuracy work is derived from evidence: a
   flagged clip or a ground-truth disagreement implicates a specific failure,
   and that becomes a card.
3. **Fouls reverse the 2026-07-27 "v1 detects shots, not fouls" rule** — see
   item 1 below. This is the single largest change on this page.

## 1. The foul reversal — a shaft/tip bump should reset the clock

**Status: decided in principle, one open question (below).**

`shaft_adjust` is *not* a benign motion. `design.md` §9 defines it as
"accidentally graze the cue ball with the shaft while settling into aim (a foul
in play)". A foul ends the visit, so the incoming player gets a fresh clock.
Same for `tip_bump` (bumping the ball with the tip while feathering).

The 2026-07-27 decision treated both as *no event, clock keeps running, human
hits reset*, reasoning that a legal ball-in-hand shaft nudge and a foul shaft
bump are the same pixels and only game state separates them. Sound, but it
stopped one step short of the clock outcomes:

| Event | Correct clock outcome |
|---|---|
| Soft legal shot | **reset** |
| Foul shaft/tip bump | **reset** (visit ends) |
| Legal ball-in-hand nudge | keep running (still your visit) |

Two of three reset. The detector never needed to separate a soft shot from a
foul — they do the same thing. Only legal ball-in-hand placement differs, and
ball-in-hand is not spontaneous: it follows a *called* foul, which is already
human input.

### Why this unblocks #5

Both constants that make sub-impulse touch shots undetectable are anchored on
the rule being reversed:

| Constant | Value | Anchored on |
|---|---|---|
| `strike_confirm_displacement_mm` | 60.0 | rejecting `shaft_adjust`'s 59 mm (the code comment says so outright) |
| `strike_impulse_mms` | 250.0 | sits just above `soft_touch_0731`'s 225 mm/s raw peak |

`soft_touch_0731` moves 43 mm and peaks at 225 mm/s — under both. It was
called an unfixable hole needing a mic. It is not: the corpus expectation
`shaft_adjust: (0, 0, None)` is what forces the floor above it. **#5 was
policy-blocked, not mic-blocked.**

### Ball-in-hand: DECIDED — eat the error

`shaft_adjust` (should fire) and `ball_in_hand_nudge` (should not) are the
**same pixels**. Without a ball-in-hand state the corpus cannot hold both
expectations — it is unsatisfiable.

**Steven's call (2026-08-02): no ball-in-hand key. Fire on both.**
Re-expect `ball_in_hand_nudge` to fire and accept a wrong clock reset on the
rare occasions he legally fine-tunes a placement — in solo practice
ball-in-hand is uncommon, and the reset is recoverable.

Do **not** add a ball-in-hand key speculatively. If those phantom resets turn
out to be annoying in real sessions, that annoyance is the evidence that
justifies building one.

### Plan

1. Re-expect `shaft_adjust` and `tip_bump` from `(0, 0, None)` to fire; same
   for `ball_in_hand_nudge` under option (b).
2. **Measure before tuning** — re-run the cold battery with only the
   expectations changed, and record exactly which pins go red. No threshold
   moves until that list exists.
3. Lower `strike_confirm_displacement_mm` and `strike_impulse_mms` to admit
   43 mm / 225 mm/s, anchored to the re-measured corpus.
4. Append a dated decision to `design.md` reversing the 2026-07-27 rule.
   Never renumber — append.

**Cost warning:** this moves detection thresholds, so it is a full cold battery
(~5 min) and will likely disturb other pins. The sub-impulse region is where
most nudge-rejection tuning lives. Real work, not cleanup.

**Clips:** `shaft_adjust`, `tip_bump`, `ball_in_hand_nudge`,
`ball_in_hand_place`, `soft_touch_0731`, `practice_strokes`, `chalk_near`
— all graded and on T7.

## 2. Ground-truth button (#19)

**Status: designed, ready to build. The highest-leverage item here.**

A deck key pressed once per real shot; the server diffs the press sequence
against emitted `ShotTaken` events. Press with no event = miss; event with no
press = phantom; a pair with an abnormal delay = late detection. Each dumps the
ring. Full design lives in the issue.

Why it leads: it is the only item that produces a **denominator**. Every other
fix on this page is currently ranked by argument rather than by its share of
live error. It also closed #1, #4 and #10, whose method was "play sessions and
Flag anything wrong."

**Always on, always available — not a mode** (Steven, 2026-08-02). Once the
decision tree collapsed to a single unconditional key, the only remaining cost
is the press itself, and there is no reason to hide it behind a toggle. The key
lives on the deck permanently. Deck key — no pedal.

**The self-arming window** — how always-on stays honest. If the key is always
live but Steven does not always press it, "event with no press" would flag
every unmarked real shot as a phantom. Fixing that must not become a toggle he
has to remember, so **the presses arm the diff themselves**:

- Group presses into runs, split wherever the gap between consecutive presses
  exceeds some idle threshold (start ~3 min, measure it).
- A run's window spans its **first press to its last press** — no trailing
  extension.
- The diff runs only inside those windows. Shots outside are ignored entirely.

Deliberately conservative at the trailing edge: a genuine phantom just after
the final press of a run goes uncounted. That is the right bias — under-report
phantoms rather than fabricate them out of a stretch he simply was not marking.

**The press stream still has its own error rate** (forgotten press → fake
phantom; double-tap → fake miss), so a disagreement is a *review candidate*,
not an auto-label.

**Dump length:** ground-truth presses are prompt, unlike the reactive Flag
press that sized `--ring-seconds` at 20 s (a measured press came 12.7 s late).
Save ~8 s instead — ~2.5x smaller per dump.

### The governing rule: the player never classifies a failure

**One key. "I shot." Pressed every shot, always, with no judgement attached.**

That is the only fact a human holds that the machine does not. Everything else
the server derives:

| What the server sees | What it concludes |
|---|---|
| press, no event | missed shot |
| event, no press | phantom |
| press, event arrives late or only via the settle fallback | late detection |
| Start pressed | the clock needed recovery |

Nothing in the right column requires Steven to know which category he is in.

**This is the design constraint, not a nicety.** An earlier draft of this plan
had him choosing between Start, Flag, and the ground-truth key depending on
*how* the clock had misbehaved. He rejected it on 2026-08-02 — correctly. Every
branch in that tree existed because the design was asking him to classify
failures at the table, which is precisely the work the ground-truth button was
supposed to take off him. He is practising pool, not operating an instrument.

**If a future change adds a branch to what the player must decide, the change
is wrong.** Push the branch into the server.

### Where the Flag key fits

Flag survives for exactly one thing: **problems that are not about shots.**
Overlay wrong, replay broken, OBS dead, scoreboard stuck. The ground-truth diff
is structurally blind to these — a broken overlay produces no `ShotTaken`
disagreement.

No overlap, so no decision:

> Did you shoot? Press the shot key. Is something broken that is not a shot?
> Press Flag.

Flag stays as-is otherwise, and stays the always-on net for sessions where
ground-truth mode is off — which will be most of them.

### Considered and rejected (do not reinvent)

All three were patches on the branching design, and die with it:

- **Auto-flag on a Start press.** Was "the cheap half worth doing first." With
  a shot key pressed unconditionally, Start carries no information the press
  does not already carry — it is clock recovery, nothing more. Still viable on
  its own merits when ground-truth mode is *off*, but it is not part of this
  item and must never become a second thing to remember.
- **Start implies a ground-truth press.** Collapses two presses into one
  exactly when Steven is busiest, but Start is also pressed to begin a rack and
  to recover from a phantom, so it would inject fake ground truth. Only
  revisit with data: run a session pressing both manually and check whether
  every Start had a press beside it.
- **A dedup window between triggers.** Only needed because one failure could
  fire two dumps. With one key it cannot.

### Implementation sketch

Not yet built; this is the starting shape, not a settled contract. Anything
here can change once the first session's data exists.

**1. The press path.**
- `POST /api/groundtruth` — records a press. Journal record
  `ground_truth_press` carrying `t` and `frame_t` (the capture-clock anchor,
  same as vision events since checkpoint 36) so alignment never has to cross
  the emit-time/capture-time boundary.
- **No mode endpoints, no `--ground-truth` flag, no `clock/modes.py` entry.**
  The key is always live; arming is derived from the press runs above. Anything
  that adds a toggle here is the branching design coming back.
- The press must never touch the clock. It is an observation, not a command.

**2. The deck key.**
- One key, armed only while ground-truth mode is on; the same key toggles the
  mode on long-press (or lives on the flag page — implementer's call).
- **`deck/layout.py` and `hardware.md` Part C must change in the same
  commit** — that is a standing repo rule, not a nicety.

**3. Alignment.**
Sequence diff, not timestamp matching — N presses against M `ShotTaken`
events, in order. Pair each press with the nearest unpaired `ShotTaken` within
a window `W`; leftovers on either side are the findings:

| Leftover | Meaning | Action |
|---|---|---|
| unpaired press | vision **missed** a shot | dump `gt_missed` |
| unpaired `ShotTaken` | vision invented one | dump `gt_phantom` |

A *paired* press is not automatically a pass. Record the press→event delay on
every pair: a shot detected only via the settle-counts-the-shot fallback pairs
successfully but arrives seconds late, and that is a real strike-detection
failure the player should never have to notice. Flag pairs whose delay exceeds
the measured normal distribution as `gt_late` — same pipeline, third label.

`W` wants measuring, not guessing — the press lands after the stroke when the
hand comes off the cue. Start around 5 s and **re-derive it from the first
session's paired distribution**; record the measurement in this file.

**4. When to decide.**
A miss cannot be declared until `W` has elapsed with no event, and a phantom
likewise. So run the diff on a `W`-lagged trailing window rather than at press
time. Consequence: the dump fires ~`W` after the disagreement, which the ring
must still cover — with `--ring-seconds 20` and an 8 s save slice, fine.

**5. Reporting.**
Offline, from the journal alone — no new storage. A CLI subcommand
(`cueclock report --journal <path>`) printing presses, events, pairs, misses,
phantoms, lates, and the accuracy fraction. The journal is already the system
of record; this is a reader.

**6. Order of build.**
Press path → deck key → alignment → report. The press path and the key are
worth landing alone: a session that only *records* presses, with the diff run
offline afterwards, already yields the denominator and needs no live decision
logic. Auto-dumping can follow once `W` has been measured from real data.

**Open sub-decisions for the implementer:** whether a press during `no_clock`
counts (probably yes — drills are still shots); whether double-tap should be
detectable as a correction (press twice within ~1 s = "ignore my last press");
whether to cap dumps per session.

**Not open:** anything that adds a decision for the player. See the governing
rule above.

## 3. Golden event timelines

**Status: recommended, not yet an issue.**

Persist each clip's full event list (type, timestamp, ball, speed) as a
committed golden file. Today a tuning change that moves a settle by 300 ms
passes green — only counts are asserted.

This is the regression net for item 1, which is the reason to build it first:
item 1 moves thresholds across the whole sub-impulse region, and count
assertions will not show the collateral.

**Validation:** generate on the current engine → battery green → perturb one
`EventConfig` constant → confirm goldens go red.

**Catches a class the ground-truth button structurally cannot:** silent
regression on clips already recorded. The button only sees live play.

## 4. Carried-ball height cue (#8 part 1, #11)

**Status: lever identified, untested.**

A carried ball is closer to the overhead camera — chest height is roughly
**1.9x** the blob area of a ball on the cloth. Apparent size is therefore a
height cue, and it would also merge-flag the carry.

Covers two failures with one mechanism:

- **#11 quiet-table hand-rolls/placements** — **evidence under review, do not
  build a fix yet.** See "The #11 evidence problem" below. Secondary lever if
  it survives review: release-point arm-blob escort analysis (a hand-rolled
  ball is escorted by an arm right up to release; a struck ball is not).

> **Terminology:** say **hand-rolled** (or hand-roll), never "bowled" — this is
> pool. `hand-rolled` also disambiguates from a ball merely rolling.
- **#8.1 the bare-hand snatch** — beats all three carried-ball gates because
  bare fingers from above never oversize the blob.

**Validation:** `pickup_snatch_0801` is **pinned known-wrong** at
`(1, 0, 2, 2)` — that 1 shot *is* the phantom, and it reproduces cold. A
working fix flips the pin to 0 shots. This is the cleanest validation target on
the board.

### The #11 evidence problem (opened 2026-08-02)

`#11`'s headline claim — "hand-rolled returns and placements counted rack shots
live" — rests on `flagged_9` and `flagged_14`. **Steven watched both on
2026-08-02 and says they are 9-ball racks and breaks, with no hand-rolls
visible.** That contradicts the claim the issue was built on.

What the journals actually record:

| Clip | Live event | Ball | Speed | State |
|---|---|---|---|---|
| `flagged_9` | `vision_shot_taken` | 231 | **3926 mm/s** | newborn, IDLE→MOTION, rack_shots 0→1 |
| `flagged_14` | `vision_shot_taken_ignored_idle` | 626 | 458 mm/s | newborn, eaten by the IDLE gate |
| `flagged_14` | `vision_shot_taken` | 648 | **3489 mm/s** | newborn, IDLE→MOTION, rack_shots 0→1 |

Those two speeds are the exact numbers `hub.py` cites as the measured
hand-rolled-return anchor for the IDLE newborn gate. **If they are not
hand-rolls, that anchor is wrong** and the gate is justified by a
misreading.

The competing explanation, which fits Steven's account: 3489–3926 mm/s sits
*between* every ordinary shot (≤ 2868) and every real break (5845–7361). A
scatter ball reborn mid-break lands in exactly that band — `break_0801b` has a
documented case of a scatter ball reborn at 906 mm/s firing a newborn
ShotTaken 2.2 s into the scatter. If that is what these are, `#11` is not a
hand-roll bug at all; it is the break being detected through the wrong ball.

**Reliability caveat, independently:** both clips were recorded during
detect-loop starvation — `flagged_9` averaged 41.9 Hz and `flagged_14`
**34.0 Hz** against a ~78 Hz nominal, with frame gaps to 115 ms. Roughly half
the frames are missing. Whatever they show, they are weak evidence, and
Steven has raised whether to keep them at all.

**Do this before any #11 work:**

1. Re-render both with the fixed `clipview` (real-time grid — see
   `triage-clips` skill) and have Steven confirm what is at the cold-replay
   event times: `flagged_9` 22.19 s, `flagged_14` 30.42 s.
2. If they are breaks, rewrite `#11` around what actually misfires, re-anchor
   or remove the `hub.py` IDLE-gate comment citing 3489/3926, and re-check
   `flagged_10/11/13` the same way rather than inheriting their labels.
3. If they are unusable, delete them and say so — a half-rate clip that
   nobody can interpret is not corpus.

Only after that does `#11` need the promotion pass in item 6.

## 5. Phantom Scratch in the pocket jaw (#6)

**Status: journal-only, five levers inlined on the issue.**

A follow-to-the-jaw stop is indistinguishable from a pot at track-death time.
Levers: exit prediction, speed-at-loss gate, occluder awareness, re-spawn
suppression after a pot, per-pocket scratch zones.

**Validation:** `hanger_pot_0801` is pinned `(1, 1, 1, 1)` — the scratch=1 *is*
the phantom, cold-reproducible. A fix flips it to 0. `jaw_strike_0801` guards
the regression that removing the scratch disarm already closed.

**Priority raised 2026-08-02 — Steven's call: this is a real bug, work it.**
He watched `hanger_pot_0801` and confirmed the ground truth: it is a **legal
shot**, not a scratch. The event is wrong on its own terms, independent of
whether Scratch drives the clock today. The earlier "journal-only, fix only if
Scratch drives the clock" posture is superseded.

## 6. Promote the flagged dumps (#11's evidence)

`flagged_9/10/11/13/14` are all on T7 (3.4 GB each) but **none is in the graded
corpus** — they are raw ring dumps. Until they are promoted (assign a reference
clip, write expectations) they cannot validate anything.

Standard triage path, roughly an hour. Prerequisite for item 4's #11 half.

## No known path

**#8 part 2 — the white-shaft sweep.** The cue's pale shaft laid over the cloth
during a walk-around births a clean, coherent, whiteness-eligible track at
~1 m/s. It fired twice live and once counted a rack shot. Height does not help
— the shaft lies flat. A shape test (a shaft blob is elongated, a ball is
circular) is the obvious first look but is untested speculation.

Cold replay never reproduces it; it depends on live vote/roster state. Future
occurrences will be warm-replayable (ring dumps carry live state since
checkpoint 36).

## Issues this plan closes

| Issue | Closed by | Condition |
|---|---|---|
| **#5** sub-impulse touch shots | item 1 | The foul reversal lowers `strike_confirm_displacement_mm` / `strike_impulse_mms` below 43 mm / 225 mm/s. `soft_touch_0731` fires a shot. |
| **#6** phantom jaw Scratch | item 5 | `hanger_pot_0801` grades to 0 scratches, 1 shot. Steven confirmed the ground truth. |
| **#8** silent hand motion | item 4, **after splitting** | `pickup_snatch_0801` grades to 0 shots. |
| **#19** ground-truth button | item 2 | Mode ships, a session produces a report, dumps land on disagreement. |
| **#11** phantom shots mid-rack | re-derivation | Resolves to a rewritten issue, or closes as invalid. Not closeable as written. |

**Split #8 first.** It bundles two failures with very different prospects: the
bare-hand snatch (height cue, gradeable, closeable) and the white-shaft sweep
(no lever, not cold-reproducible). As one issue it can never close. Split it,
close the snatch half, and let the shaft half sit open honestly.

**Not closed by this plan:** #2 (mic for voice "extension" — parked on
hardware, Steven wants it eventually) and the shaft-sweep half of #8.

## Parallel execution (worktrees + subagents)

The vision items conflict; the infrastructure items do not. Fan out
accordingly, and reconcile in the main agent.

### Wave A — fully disjoint, run in parallel

| Agent | Item | Files |
|---|---|---|
| A1 | #19 ground-truth button (item 2) | `server/app.py`, `deck/layout.py`, `cli.py`, `hardware.md` Part C |
| A2 | Golden event timelines (item 3) | `tests/` harness only |

Neither touches `vision/`. They can merge in any order.

### Wave B — vision, parallel in worktrees, serialized on merge

| Agent | Item | Primary file |
|---|---|---|
| B1 | Foul reversal (item 1) | `vision/events.py` thresholds + corpus expectations |
| B2 | Phantom jaw Scratch (item 5) | `vision/events.py` scratch path |
| B3 | Carried-ball height cue (item 4) | `vision/detect.py` + `vision/track.py` + `events.py` gates |

All three touch `vision/events.py` and `tests/corpus/test_corpus_events.py`.
Work them in parallel worktrees, but **merge one at a time and re-run the full
cold battery after each** — the thresholds interact. B1 in particular lowers
the impulse and confirm floors globally, which changes what B2's and B3's gates
observe. Expect B2/B3 to need rebasing onto B1's result rather than a clean
three-way merge.

### Ordering constraint

**A2 (goldens) must land before B1 (foul reversal).** B1 moves thresholds
across the whole sub-impulse region, and count-only assertions will not show
the collateral — a settle sliding 300 ms passes green today. Without goldens
first, B1 is an unmeasured change.

### Worktree gotchas — read before spawning anything

A fresh worktree is **not** a working repo here, and the failure is silent:

- **`tests/clips` is a gitignored symlink** to `/Volumes/T7/pool_room_clips`.
  A new worktree has no such symlink, so **every corpus test skips** and the
  battery reports green having graded nothing. An agent can honestly report
  "357 passed" while validating zero clips.
- **`.venv` is a gitignored symlink** to `.venv.nosync/` (iCloud stamps
  `UF_HIDDEN`, which makes CPython skip `.pth` files and breaks the editable
  install — CHANGELOG checkpoint 13). A worktree has neither.

- **The detect cache is per-root too.** `.detect-cache.nosync` is gitignored, so
  a fresh worktree starts cold even for work the cache would cover.

So each worktree needs, before any test run:

```bash
ln -s /Volumes/T7/pool_room_clips tests/clips
uv sync                      # then ensure .venv is a symlink to .venv.nosync/,
                             # never a real directory
uv run pytest -n 4 -q        # must report corpus tests RUN, not skipped
```

**Verify the corpus actually ran.** A cold battery is ~5 min and ~357 tests; a
21 s run with no shared cache means the corpus skipped. Check the count and the
wall time before believing any agent's green.

### Test cost per agent — it is not 5 minutes for everyone

`--detect-cache` keys on `detect.py`'s bytes, `DetectConfig`'s values, the
reference digest, clip identity and the OpenCV/NumPy versions — deliberately
**not** on `events.py` or `track.py`. So the cost splits by which file an agent
edits:

| Agent | Edits | Cache | Iteration cost |
|---|---|---|---|
| B1 foul reversal | `events.py` thresholds | **hits** | ~21 s |
| B2 jaw Scratch | `events.py` scratch path | **hits** | ~21 s |
| B3 height cue | `detect.py` + `track.py` | **misses by design** | ~5 min, unavoidable |
| A1 #19 · A2 goldens | no `vision/` | n/a | unit suite, seconds |

**Point every worktree at one shared cache root** — an absolute
`--detect-cache /path/to/.detect-cache.nosync` — so B1 and B2 inherit the
already-warm entries instead of each rebuilding them. This is safe to do
concurrently: `store()` writes tmp+fsync+rename with pid/uuid-unique tmp names,
and identical bytes make last-writer-wins harmless.

B3 pays the cold cost every run because changing the detector is precisely
what invalidates the cache. That is the design working, not a problem to route
around — a cached run must never grade stale detections.

**The cold battery is still the commit gate.** Cached runs are the iteration
loop; every agent runs one full cold battery before returning, and the
reconciler runs one after each merge.

### Contention — restate this in every subagent prompt

Subagents do not inherit environment constraints and have repeatedly ignored
them:

- **No batteries or heavy jobs while `cueclock run` is live.** Detection
  starves and the live session is degraded.
- **T7 must be mounted** or the corpus silently skips (above).
- Do not write dumps to the T7; it saturates the USB bus shared with the Camo
  feed.

### Reconciliation in the main agent

1. Merge Wave A in any order; run the unit suite.
2. Merge B1; run the **full cold battery**; record which pins moved and why.
3. Rebase B2 onto that, merge, full cold battery again.
4. Same for B3.
5. Append one CHANGELOG checkpoint per merged item — that is the repo rule, and
   with goldens in place each entry can cite exactly which timelines shifted.

## Verified reproductions (2026-08-02)

Cold `analyze` output, replay-relative. With the fixed `clipview` these equal
the scrub position and the burned-in clock, so all three can be cited
interchangeably.

| Clip | Reference | Events | Status |
|---|---|---|---|
| `hanger_pot_0801` | `empty_table_0729` | 11.59 ShotTaken · **12.34 Scratch** · 14.56 BallsSettled | **#6 CONFIRMED.** Steven's ground truth: a legal shot. The Scratch fires 0.75 s after the strike while the cue is still rolling to the jaw, stops on the lip, never drops. |
| `pickup_snatch_0801` | `empty_table_0801` | **12.28 ShotTaken** · 17.79 + 29.65 BallsSettled | **#8.1 CONFIRMED.** Steven's ground truth: the cue ball is picked up and placed at the far end for a 9-ball break. No shot exists in the clip; the ShotTaken fires ~1 s into the pickup. |
| `flagged_9` | `empty_table_0801` | 22.19 ShotTaken · 29.63 BallsSettled | **DISPUTED** — see the #11 evidence problem. |
| `flagged_14` | `empty_table_0801` | 3.39 BallsSettled · 30.42 ShotTaken · 37.84 BallsSettled | **DISPUTED** — same. |

The two confirmed clips reproduce cold and are directly gradeable: a working
fix removes the bolded event and the corpus catches it.

> **Never cite a timestamp from a clip you have not converted correctly.**
> The ring stores non-uniform frame gaps; the earlier `clipview` wrote them at
> a flat 30 fps and desynchronised the scrub bar by up to 2.6x. Two rounds of
> wrong timestamps were handed to Steven before this was caught. Ground truth
> comes from him watching the right frame — see `clip-triage-ground-truth`.
