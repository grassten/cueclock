---
name: session-review
description: Turn Steven's review-page verdicts on one cueclock session into one GitHub issue of proposed changes, with the evidence in it (#70). Use when Steven types /session-review <stamp>, or asks to "review the session's verdicts", "turn my verdicts into an issue", or "do what you did for #69" for a session he has judged. Read-only on code; files or comments on one issue.
---

# Session review: verdicts → one findings issue

Steven judges a session's moments on the review page (`cueclock review serve`,
:8001). This skill does what was done by hand for #69: for every wrong verdict
it finds the mechanism in the session log, measures each proposed rule across
every journal, and files **one** issue of changes. It never changes code: a
normal checkpoint session implements what Steven accepts.

```
/session-review <stamp> [--as-of-session] [--dry-run]
```

- `<stamp>`: the session, e.g. `20261121-153046`. Missing → the newest session
  directory that has a `verdicts.json`.
- `--as-of-session`: review as if the session's own code were HEAD. Read code
  with `git show <session sha>:<path>`, skip `review check`, and drop nothing
  as "already changed". This is the golden test's first half (below).
- `--dry-run`: write the issue body and print its path; file nothing and
  comment nothing. **Always use it for the golden test**, or it files a
  duplicate of #69.

## Scope of action: hard limits

- **Read** anything. **Write** only to `$TMPDIR/cueclock-review/` (the issue
  draft) and the temp files `review frames` makes. No edits to the repo, no
  commits, no pushes, no closing or editing issues, no daemon restarts, no
  `launchctl`.
- GitHub: `gh issue create` once per session, `gh issue comment` only on the
  issue this skill filed for that session. Read anything with `gh`.
- **No replays or batteries while a session is live** (step 0 checks). `review
  check` is a replay: about 68 s per 27 min of play from a terminal, about 6x
  slower under launchd Background QoS. Give it `timeout: 600000`.
- Use `.venv/bin/python3 -m cueclock.cli …`, never `uv run` (concurrent `uv`
  runs block).
- **Footage, stills and clips never leave the machine.** The issue quotes
  journal lines only, never an image.

## Standing decisions: never propose against these

Restate this list, word for word, in every subagent prompt. Subagents don't
inherit it and have ignored it before.

1. **Audio for detection is permanently disqualified.** Never propose or
   mention sound, a mic or clicks for detection. The voice "extension"
   listener is separate; its accept and false-accept counts may be reported.
2. **#67 (checkpoint 140, `docs/design.md` §21):** a cue ball never seen
   rolling was moved by hand and counts no shot in open play. A cue ball
   rolled or pushed by hand still counts, by design: report that as "worked
   as designed". Don't propose changing it.
3. **One detection number:** the clock-accuracy row from `session-stats`,
   called "Detection". Never a strike-only rate beside it, never
   "self-healing".
4. **Live, not shadow.** No shadow modes and no assessment sessions; measure
   from journals already on disk.
5. **No unreproducible issues.** A change must be gradable by `review check` /
   `session verify` on a session log. A journal-only oddity goes under
   observations.
6. **No parked failures.** Every wrong verdict gets a proposed change or an
   explicit "worked as designed, because …".
7. **No blind thresholds.** Every constant cites the measurement it came from.
8. **No clip-by-clip strategy, and don't blame the room.** Lighting is
   settled. Argue from aggregates.
9. **Predictable over partial.** Rate a rule against each step of Steven's
   real sequence (placing, racking, breaking), not just the failing frame.
10. **In-game only.** Between racks nothing should move the clock or raise a
    card. Split evidence into in-game and racking, and never headline a
    racking event.
11. **Closed, don't reopen:** #71 (when the clock pauses: the motion-onset
    freeze stays), #72 auto rack end (Steven's call, pending; a note asking
    for it maps there), #46 minigames, #41 ball in hand resets the clock.
12. **Built for real matches:** judge a rule against a match room (talking,
    music, a second player), and flag evidence that only practice can give.

Also read `CLAUDE.md` and the memory index before step 1. A memory note that
says "don't re-measure" or "his call" is a standing decision too.

## The run

### 0. Preflight

```sh
ls /Volumes/T7/cueclock_sessions >/dev/null    # the T7 must be mounted
pgrep -fl "cueclock.*run"; curl -s -m2 127.0.0.1:8000/api/state   # both empty = no live session
```

If a session is live, stop and say so. If a read fails with EDEADLK, run
`find . -type f -flags +dataless` before anything else (CLAUDE.md, iCloud).

### 1. The dossier: the one entry read

```sh
.venv/bin/python3 -m cueclock.cli review dossier <stamp>
```

About 2.5k tokens. It contains the code (the session's sha against HEAD, and
the checkpoints landed since), parity, footage, presses, every verdict joined
to its moment in local time with its full-recording position, unjudged ids,
co-located moments, shots classified, racking stretches, held-by counts,
freeze shapes, scratches and ball in hand, detect rate and voice.

Then read the context: the newest `CHANGELOG.md` checkpoint and its "next
session checks", the memory note of the live test it names, and
`gh issue list --state open`.

**Session sha ≠ HEAD** (and no `--as-of-session`): run the baseline now:

```sh
.venv/bin/python3 -m cueclock.cli review check <stamp>    # timeout 600000
```

A wrong verdict graded **changed** has already had its decision altered by a
checkpoint listed under "Code". Name that checkpoint, list the verdict under
"already changed", and don't diagnose it again. For verdicts still wrong, the
journal was written by the session's code: where a field or reason doesn't
match HEAD's writer, read the file at the session's sha. Propose the fix
against HEAD.

### 2. Read the verdicts as Steven meant them

The buttons say "Clock right / Clock wrong", but he uses them more loosely:

- **Trust the note over the button.** `reverted-1718077079` ✗ "wasn't a break
  just put the cue ball on the table": the clock *recovered* as §17 designs,
  so the ✗ judges the detection. Report "worked as designed" and ask what
  looked wrong on the wall. Don't invent a fix.
- **Co-located moments are one episode.** The dossier lists them; a CONFLICT
  is the same settle judged both ways (`phantom-1717566109` ✓ vs
  `in_hand_shot-1717566108` ✗, 1 ms apart). Diagnose the episode once and
  trust the verdict whose note describes what happened.
- **Questions in notes** ("Had I clicked new rack prior to racking?") get an
  answer from the log, in the issue.
- **Feature requests in notes** ("detect the end of a rack") are problem
  statements. Each one gets its own issue (or maps to an open one), never a
  section of the fixes, so Steven can park it on its own.
- **Unjudged moments are out of scope:** list their ids and do nothing else.
- **Racking:** a moment inside a racking stretch with the clock running is not
  play (standing decision 10). Say so; don't build a play rule from it.

### 3. Aggregates, classified

```sh
.venv/bin/python3 .claude/skills/session-stats/stats.py logs/session-<stamp>.jsonl
```

Take the **Detection** row from it and no other accuracy number. Never quote a
row raw: the dossier's "Shots, classified" section names every shot counted
with no strike (after the watchdog / cue ball on its spot / nothing seen to
move / unexplained) and every racking stretch. Use those to say what the raw
rows really were. The dossier's "as journaled at the session's end" line is
the session's own code's count; "read by HEAD's report" is today's.

### 4. Diagnose each wrong episode

One wrong verdict or co-located cluster at a time:

```sh
.venv/bin/python3 -m cueclock.cli review timeline <stamp> <moment-id>        # −12 s / +8 s, filtered
.venv/bin/python3 -m cueclock.cli review timeline <stamp> <mono> --before 30 --after 5 --all
```

- In the settle line, read `held` (what decided a held settle: `list`,
  `motion`, `shot`, `timeout`, …), `rejected`, `reason`; in `scratch_list`,
  read `row`, `verdict`, `hand`, `counted`, `cue_px`.
- Find the field's writer (`grep -n` in `src/cueclock/server/hub.py`, at the
  session's sha when it differs), read the deciding function, and read its
  design § (`docs/design.md` §13–§21). Say whether today's behaviour was
  **intended**: #69's change 1 was intended by §15, and the evidence is what
  overturned it.
- Quote the journal lines in the finding, with frame `t` values. Evidence
  before cause: never name a mechanism you haven't quoted.
- **Footage only when the journal can't answer** what the wall showed or what
  Steven did:

  ```sh
  .venv/bin/python3 -m cueclock.cli review frames <stamp> <moment-id> [--span 6 --fps 2 --before 3]
  ```

  It prints the JPEG's path and a legend (which tile is which second); read
  the image with the Read tool. #69 needed it twice in 5 wrong verdicts.

**More than ~3 wrong episodes:** give each its own diagnosis subagent, in
parallel (normal effort: this is real reasoning). Input: the moment ids, the
dossier excerpt for them, the timeline commands, code pointers (functions by
name, design §), the standing decisions word for word, and "read-only: no
replays, no batteries, no edits". Output, ≤ 400 words in this schema:
mechanism · quoted journal lines · intended by design (yes/no, §) · proposed
rule · the measurement it needs. Footage stays inside the subagent.

### 5. Measure every proposed rule across every journal

Two numbers per rule: **how often the triggering shape occurs**, and
**whether any right or real case shares it** (the false-positive check). Use
only journals that carry the fields the rule needs: list rows from checkpoint
132, `held` from 133, `cue_px` from 138. The recurring counts are functions
in `cueclock.review.dossier`:

```sh
.venv/bin/python3 - <<'EOF'
from collections import Counter
from pathlib import Path
from cueclock.review import dossier, timeline
held, shapes = Counter(), Counter()
for journal in sorted(Path("logs").glob("session-*.jsonl")):
    found = timeline.records(journal)
    held += dossier.held_by(found)
    shapes += dossier.freeze_shapes(found)
print(held); print(shapes)
EOF
```

`settle_lists` pairs each settle with its list, `racking_stretches` finds
racking on a live clock, and `classify_misses` names no-strike counts. Write
anything else ad hoc. With several rules, one measurement subagent (lower
effort: it's mechanical) runs every query, deduped, and returns numbers only.
Its prompt carries the standing decisions too.

Every constant in a proposed rule cites its number. A threshold the data
can't separate is reported as that (#69: real shots go down to 42 mm and the
false one was 38 mm, so a millimetre rule couldn't do it).

### 6. Predict what the fix flags

From step 1's `review check` grades: name each wrong verdict's pass criterion
(*still wrong* → *changed*), and predict which right verdicts will read
REGRESSED because they share a window with a fixed one (the dossier's
"shared window" lines). In #69 that was `phantom-1717566109`.

### 7. Sort before writing

- **Changes:** evidence + rule + measurement + "done when".
- **Questions for Steven:** ruleset, intent, what looked wrong on the wall.
- **Observations:** facts that justify nothing yet.
- Drop anything a standing decision rules out, and anything not reproducible
  from the session log (it goes to observations or nowhere).

Speak **local time** (stamps and journal walls are UTC) and give each
moment's full-recording position from the dossier.

### 8. File, or don't

Nothing wrong (no wrong verdicts, or every one "already changed" or "worked
as designed" with nothing to ask): **file nothing**. Say so in one line.

Otherwise write the body to `$TMPDIR/cueclock-review/<stamp>.md`, ending with
the marker line:

```
<!-- session-review <stamp> verdicts <fingerprint from the dossier> -->
```

Then find any earlier run's issue:

```sh
gh issue list --state all --search "<stamp> in:body \"session-review\"" --json number,title,body
```

Only an issue whose body holds the marker line `<!-- session-review <stamp>
verdicts …-->` is this skill's. The search also finds issues that merely
mention the stamp (#69, #70), and those don't count.

- None: `gh issue create --title "Session <YYYY-MM-DD> review: <the changes in a few words>" --label area:accuracy --label needs:code-only --body-file <file>`
  (use another `area:` label when the changes clearly belong there).
- One whose marker has the **same fingerprint**: file nothing. Say it's
  already filed, with its number.
- One with a **different fingerprint** (Steven changed verdicts since):
  `gh issue comment <n>` with only what changed (new or flipped verdicts, and
  the findings that changed because of them), ending with the new marker.
  Never a second issue.

With `--dry-run`, stop after writing the file and print its path.

## The issue

1. **Header:** stamp, local date and times, shots and racks, ruleset, code
   sha (clean or dirty) and which checkpoint that is, judged/right/wrong
   counts, unjudged count, and which open test issue it serves.
2. **Summary table:** # · change · verdicts it answers · size (e.g. "hub
   only", "one predicate", "review app").
3. **Health checks:** parity (`diff_count`), detect Hz (min / p5 / median),
   the rows the live test cares about, and voice (accepted / false accepts).
4. **One section per change:** the verdicts with their notes quoted · the
   journal excerpt (`review timeline` lines, UTC walls as printed) · the
   mechanism with `file:line` · intended by design? (§) · the proposed rule ·
   the cross-journal measurement (shape frequency and false-positive check) ·
   the alternative rejected and why · **done when**, in `review check` terms.
5. **Judged wrong, no change proposed:** each with its reason ("worked as
   designed: §17 …", "already changed by checkpoint N").
6. **Questions for Steven** (answers to his note questions go in the
   sections they belong to).
7. **Unjudged moments:** ids only.
8. **Stats honesty:** each raw row that misled, and what it really was.
9. **Verification plan:** `review check <stamp>` and the expected grade for
   every judged moment, plus the cached battery if `events.py` or `track.py`
   change.
10. The marker line.

End the body with the `🤖 Generated with Claude Code` attribution line. Keep it
plain: short sentences, tables where they help, no hedging. Steven reads it
on his phone.

## The golden test

`20261121-153046` was reviewed by hand and became #69 (changes 1–3 hub
mechanisms, 5 racking evidence, 6 the co-located-moment conflict; 4 = #72,
out of scope). The session ran on `03f9183` (checkpoint 135), and #69 shipped
as checkpoint 136.

- `/session-review 20261121-153046 --as-of-session --dry-run` must reproduce
  #69's mechanisms for changes 1–3 and the evidence for 5 and 6, without
  help. Compare the draft with `gh issue view 69`.
- `/session-review 20261121-153046 --dry-run` (at HEAD) must report those
  verdicts as already changed by checkpoint 136 and propose nothing for them.
- Run against the live issue list (no `--dry-run`), a second run files
  nothing new.
