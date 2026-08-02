---
name: triage-clips
description: Triage flagged cueclock ring dumps — the off-table half of the Stream Deck Flag keys. Use when Steven says "triage the clips", "process the flags", "what did I flag last night", after a live session that produced flag dumps, or when a clip in ~/cueclock-dumps needs to become a graded corpus scenario or be deleted. Covers warm/cold replay, journal correlation, and promotion to the T7 corpus.
---

# Triage flagged clips

At the table, a Flag key press dumps the ring buffer as a labeled clip. This
skill is the other half: turn each dump into a **graded corpus scenario**,
**kept evidence**, or a **deletion** — and drain the staging directory.

Flow spec: `docs/hardware.md` §C5. Clip conventions: `docs/design.md` §9.

## Step 0 — Preflight

```bash
pgrep -fl "cueclock.cli run"     # MUST be empty
ls -d /Volumes/T7 && df -h /Volumes/T7 | tail -1
ls -la ~/cueclock-dumps/
```

Three hard rules:

- **Never triage while a session is live.** Replays and corpus batteries
  starve the detect loop (78 Hz → 11–20 Hz measured) and the clock goes
  silently blind. If a `run` is up, stop or wait.
- **Use `.venv/bin/python3 -m cueclock.cli …`, not `uv run`,** whenever any
  long-lived `uv run` is alive — concurrent `uv run`s block or die silently.
- **T7 must be mounted** to promote anything (`tests/clips` is a symlink to
  it) and to read the reference clip. Unmounted ⇒ the symlink dangles.

## Step 1 — Inventory

```bash
for d in ~/cueclock-dumps/*/; do
  echo "$(basename "$d")  $(du -sh "$d" | cut -f1)  $(python3 -c "
import json,sys; m=json.load(open('$d/meta.json')); print(m['recorded_at'], m['frame_count'],'frames')")"
done
```

Each dump holds `frames.raw` (~3.6 GB), `timestamps.npy`, `meta.json`,
`events.jsonl` (the live journal tail around the press), and — if a snapshot
covered the ring — `state.pkl` + `state.json` for warm replay.

## Step 2 — Ground truth, BEFORE any analysis

**Ask Steven what actually happened at the table for each clip.** Do not
infer it from stills or from the event stream; sparse frames mislead, and a
clip named for a reason still doesn't say what the correct behavior was.

You need, per clip: what he did (shot / address / racking / picked the ball
up), what the clock did, and what it *should* have done. Without that you
cannot write a correct `EXPECTED` entry, and a wrong pin is worse than no pin.

## Step 3 — Route on the reason code

The clip name carries why it was flagged:

| Name | Meaning | Start here |
|---|---|---|
| `flag_phantom` | an event fired that shouldn't have | vision replay (Step 5) |
| `flag_missed` | a real shot/settle wasn't detected, or came late | vision replay (Step 5) |
| `flag_other` | display problems, anything else | **presentation side first** |

For `flag_other`, check the presentation path *before* assuming a vision bug
— an OBS-side failure once burned two full triage passes as a phantom vision
bug. If the journal shows correct events at the right times, the vision core
was fine and the fault is downstream: OBS logs
(`~/Library/Application Support/obs-studio/logs/`, newest file), the recording
in `~/Movies/`, the overlay, or Syphon. In an OBS log, `Number of lagged
frames due to rendering lag/stalls` near 0% means OBS was healthy and the
fault is upstream in one source.

## Step 4 — Correlate with the session journal

`meta.json`'s `recorded_at` (UTC) locates the session log in `logs/`, whose
filenames are UTC stamps (`session-YYYYMMDD-HHMMSS.jsonl`).

```bash
python3 -c "
import json,glob,datetime as dt
for p in sorted(glob.glob('logs/session-*.jsonl')):
    rows=[json.loads(l) for l in open(p) if l.strip()]
    if not rows: continue
    print(p, rows[0]['wall'], '->', rows[-1]['wall'])
"
```

In the session journal, read three things around the flag press:

1. **The `flag_dump` record** — its `warm: true/false` says whether a state
   snapshot made it into the clip, and `path` names the dump.
2. **The vision events** near it — `vision_shot_taken`, `vision_balls_settled`,
   `vision_scratch`, `*_ignored_idle`, `*_killed`. Compare against ground truth.
3. **`detect_heartbeat` `hz`** (every ~5 s; healthy ≈ 75–78). **Check this
   first whenever detection looks blind.** A starved loop reads a break in
   2–3 frames at a few hundred mm/s, the IDLE break floor eats it, and the
   rack is silently ignored with zero errors — that is a contention artifact,
   not a detection bug, and the clip should not be pinned as one.

## Step 5 — Replay: warm first, then cold

```bash
REF=tests/clips/empty_table_0801     # the CURRENT dated reference — never the default
CLIP=~/cueclock-dumps/flag_missed

.venv/bin/python3 -m cueclock.cli analyze --clip $CLIP --reference $REF --warm
.venv/bin/python3 -m cueclock.cli analyze --clip $CLIP --reference $REF
```

- **`--warm`** restores the live state snapshot and reproduces what the live
  session *actually computed* — accumulated whiteness-vote position, roster
  membership, motion/episode latches included. Timestamps print in session
  time, matching the dump's `events.jsonl`.
- **Cold** (default) starts from empty state: what *current code* does with
  these frames. This is what corpus grading pins.

Use the **same `--reference` the live session ran with** (README names the
current one). `--verbose` adds per-second track status.

If warm replay refuses with a fingerprint/schema mismatch, the vision core
changed since the dump. Either check out the recording commit to reproduce,
or go cold and say so — never work around it silently.

## Step 6 — Read the warm/cold matrix

| Warm | Cold | Reading |
|---|---|---|
| fails | fails | Reproducible with current code. Best case — promote it (Step 8). |
| fails | passes | **Accumulated-state divergence.** Cold replay can never show this class. Valuable evidence; the cold pin won't capture the bug, so fix the state handling and note in the CHANGELOG what warm showed. |
| passes | passes | Live-only. Suspect starvation (Step 4 `hz`) or the presentation path. Usually not a vision bug. |
| n/a | either | No snapshot in the clip — cold only, plus a printed note. |

## Step 7 — Decide

Per clip, pick one and tell Steven which:

- **Promote** — reproducible, and it pins behavior nothing else covers.
- **Keep** — real evidence, not yet gradeable (e.g. waiting on the acoustic
  corroborator). Move to T7 under a descriptive name; do not add `EXPECTED`.
- **Delete** — duplicate of a covered scenario, or explained as contention /
  presentation. Record the finding, drop the 3.6 GB.

## Step 8 — Promote to the corpus

1. **Rename to a scenario name** — `<scenario>_MMDD`, describing the
   *behavior*, not the reason code (`settle_stall_0801`, not `flag_missed_2`).
2. **Move to the T7** (cross-device copy of ~3.6 GB, tens of seconds; the T7
   is USB, so don't do this with a session live):
   ```bash
   mv ~/cueclock-dumps/<dump> tests/clips/<scenario>_MMDD
   ```
3. **Add the `EXPECTED` entry** in `tests/corpus/test_corpus_events.py`:
   `scenario -> (shots, scratches, min_settles[, max_settles])`, `None` = don't
   assert. Match the file's existing style — **a comment stating the ground
   truth and what the entry defends against**, in Steven's own terms. Use
   `max_settles` for address/junk provokers that must produce no settle at
   all. Assert hard on §2.3 failure modes; only pin what's knowable elsewhere.
4. **Add a bespoke test** when counts alone would stay green through the bug
   (e.g. a settle that fires but 12 s late — pin the *timing*).
5. **Run the battery** (T7 mounted, no live session):
   ```bash
   .venv/bin/python3 -m pytest -n 4
   ```
   `-n 4` is the number — 4 workers hit the T7's sequential-read ceiling;
   `-n auto` picks 8 here and is slower.
6. **Append a CHANGELOG checkpoint** — newest first, in the established
   voice: mechanism, the measurement that anchors it, the fix, the scenarios
   added, test counts.

Every constant introduced along the way must be anchored to a measurement
from the corpus. No blind threshold tuning — that discipline is how the old
app's 90–95% plateau gets avoided.

## Step 9 — Drain

```bash
rm -rf ~/cueclock-dumps/<dump>      # deleted + promoted clips both leave staging
du -sh ~/cueclock-dumps/            # confirm it's drained
```

Staging lives on the internal SSD (~68 GB free ≈ 19 dumps). It is not
backed up and not synced — a dump that matters belongs on the T7.

## Why staging is not the T7

`run --dump-dir` defaults to `~/cueclock-dumps` because dumping to the T7
saturates the USB bus it shares with every other peer: one measured press
(2026-08-01, 3.6 GB) dropped the detect loop 75 → 45 Hz and froze the Camo
side-angle source in OBS for ~1 s, still blipping 20 s later. Flagging a
failure was degrading the session being flagged. The repo is also on an
iCloud-synced Desktop, so in-repo staging would land in `bird`/`cloudd`'s
path. `~` is internal, off the USB bus, and outside iCloud's scope.
