---
name: triage-clips
description: Triage flagged cueclock ring dumps and fix what they expose — the off-table half of the Stream Deck Flag keys. Use when Steven says "triage the clips", "process the flags", "what did I flag last night", after a live session that produced flag dumps, or when a clip in ~/cueclock-dumps needs to become a graded corpus scenario or be deleted. Covers warm/cold replay, journal correlation, promotion to the T7 corpus, and fixing every defect found.
---

# Triage flagged clips

At the table, a Flag key press dumps the ring buffer as a labeled clip. This
skill is the other half: turn each dump into a **graded corpus scenario**,
**kept evidence**, or a **deletion**, **fix every defect it exposes**, and
drain the staging directory.

**Triage ends when the bugs are gone, not when they are named** — fix them
all in the same pass, without asking permission (Step 7b).

Flow spec: `docs/hardware.md` §C5. Clip conventions: `docs/design.md` §9.

## Step 0 — Preflight, and **start from the number, not the clip**

```bash
pgrep -fl "cueclock.cli run"     # if non-empty, kill it (below) before anything else
ls -d /Volumes/T7 && df -h /Volumes/T7 | tail -1
ls -la ~/cueclock-dumps/
.venv/bin/python -m cueclock.cli report --since 7d    # the week the flags came from
.venv/bin/python -m cueclock.cli report --all         # the trend they sit in
```

**Read the report before opening a single clip**, and quote it when you report
back. Flag presses sample only the failures Steven noticed; `report` counts
every strike the engine missed, because the clock's own compensation records
one on every shot (`server/report.py`, `docs/cli.md` §report). A flagged clip
that turns out to be one instance of a mechanism already costing 3% is a very
different object from one that is the only evidence of something new — and you
cannot tell which without the aggregate first.

If a strategy question is on the table ("is this working", "why isn't it
getting better"), **answer it from the report and the trend, never by opening a
clip.** Steven has rejected clip-diving as a way to reason about direction
(2026-08-05).

Three hard rules:

- **Never triage while a session is live — and if one is, kill it.** Replays
  and corpus batteries starve the detect loop (78 Hz → 11–20 Hz measured) and
  the clock goes silently blind. Invoking this skill *is* the instruction to
  take the session down: stop it as Step 0 and say so, don't ask and don't
  wait. (Steven, 2026-08-05: he's off the table by the time he asks for
  triage; blocking on a confirmation just stalls the pass.)
- **Use `.venv/bin/python3 -m cueclock.cli …`, not `uv run`,** whenever any
  long-lived `uv run` is alive — concurrent `uv run`s block or die silently.
- **T7 must be mounted** to promote anything (`tests/clips` is a symlink to
  it) and to read the reference clip. Unmounted ⇒ the symlink dangles.

### Killing a live session

**SIGINT, never SIGKILL** — SIGINT is the documented clean stop and it closes
the pylon device so the camera cools (`deck/session.py`, `STOP_ESCALATE_S`);
`cli.py` maps SIGTERM to the same path, so that's the one escalation.

```bash
RUN=$(pgrep -f "cueclock.cli run")
[ -n "$RUN" ] && kill -INT $RUN
for i in $(seq 1 40); do kill -0 $RUN 2>/dev/null || break; /bin/sleep 0.5; done
kill -0 $RUN 2>/dev/null && kill -TERM $RUN    # only if still up after ~20 s
pgrep -fl "cueclock.cli run" || echo "(no live run)"
```

`run` is normally a **child of the deck daemon** (`ppid` = the
`com.cueclock.deck` launchd job), not a launchd job itself, so it does **not**
respawn. The deck's state machine handles the child vanishing on its own
(`session.py` `tick()` → "run exited on its own" → `_release()`), dropping the
caffeinate assertion and putting the key back to OFF — no daemon restart, no
`launchctl kickstart` needed. Leave the daemon up.

This leaves **OBS running** (the deck's own Stop key also quits OBS; a bare
SIGINT doesn't). That's fine for triage — only the detect loop contends — but
if OBS was recording, say so rather than leaving him to find a half-written
file in `~/Movies/`.

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

**Every dump recorded before 2026-08-02 is in exactly this state**: the
detect/advance split touched `pipeline.py`, which `_vision_fingerprint()`
hashes, so their snapshots are refused. Expected, not a bug. Cold replay is
unaffected, and dumps taken since are fine.

## Step 5b — Let Steven watch the clip

Whenever a finding rests on what physically happened at the table — and
always when he asks to see it — hand him a scrubable file rather than a
description. Stills mislead (see the ground-truth note in Step 2); 22 s of
video with a burned-in clock does not.

```bash
cat > .git/clipview.py <<'PY'
"""Render a clip as a real-time mp4: video position == elapsed clip seconds."""
import cv2, sys, numpy as np
from pathlib import Path
from cueclock.capture.clip import ClipReader

FPS = 30.0
clip = Path(sys.argv[1] if "/" in sys.argv[1] else f"tests/clips/{sys.argv[1]}")
out = Path.home() / "Desktop" / f"{clip.name}.mp4"
r = ClipReader(clip)
ts = np.asarray(r.timestamps, dtype=float)
t0 = float(ts[0]); span = float(ts[-1]) - t0
n_out = max(1, int(round(span * FPS)))
w = cv2.VideoWriter(str(out), cv2.VideoWriter_fourcc(*"avc1"), FPS, (r.meta.width, r.meta.height))
for k in range(n_out):
    t = k / FPS
    i = int(np.clip(np.searchsorted(ts, t0 + t), 0, len(ts) - 1))
    bgr = cv2.cvtColor(r.frame(i), cv2.COLOR_BayerBG2BGR)
    cv2.putText(bgr, f"{t:6.2f}s", (30, 70), cv2.FONT_HERSHEY_SIMPLEX, 2.0,
                (0, 255, 255), 4, cv2.LINE_AA)
    w.write(bgr)
w.release(); r.close()
print(f"wrote {out}  ({span:.2f}s real, {len(ts)} src frames -> {n_out} @ {FPS:g}fps)")
PY
.venv/bin/python3 .git/clipview.py <scenario-or-path>   # ~20 s for a ring dump
open ~/Desktop/<scenario>.mp4
```

**It MUST resample onto a real-time grid** (2026-08-02 lesson). Ring dumps have
non-uniform frame gaps — starvation swings the detect rate, measured 13–115 ms
between frames on `flagged_14` — so writing stored frames straight out at a
fixed fps desynchronises the scrub bar from the burned-in clock, by different
factors per clip (2.6x on `hanger_pot_0801`, 1.1x on `flagged_14`). An earlier
version of this recipe did exactly that, and Steven was sent to timestamps
where nothing happens. With the resample, **scrub position = burned-in clock =
`analyze` timestamp**, so all three can be cited interchangeably.

- Takes a corpus scenario name or any path (`~/cueclock-dumps/flag_missed`).
- **The burned-in seconds are replay-relative** — the same clock `analyze`
  prints cold, so "shot at 13.04 s" lands on the right frame. Warm replay
  prints *session* time instead; don't hand him one and cite the other.
- `avc1` so QuickTime opens it directly. ~7 MB for a 22 s dump.
- **Raw camera frames**: no undistort, no CCM. Straight lines bow and the
  cloth reads wrong — that is what detection sees, which is the point, but
  say so or it looks like a broken export.
- It lives in `.git/` on purpose: a viewing convenience, not part of the app,
  so it stays out of the repo, out of CI and out of `mypy --strict`. Recreate
  it from the heredoc whenever it is missing.
- Always quote the timestamps of the moments that matter alongside the file
  (stroke, detected shot, settle), so he can jump straight there.
- T7 must be mounted for anything under `tests/clips`.

The rig path — real display pipeline, on the TV, but no frame-by-frame
scrubbing — is `cueclock replay --clip <dir> --rectify --once`, with OBS up
and the Syphon Client source selected.

## Step 6 — Read the warm/cold matrix

| Warm | Cold | Reading |
|---|---|---|
| fails | fails | Reproducible with current code. Best case — promote it (Step 8). |
| fails | passes | **Accumulated-state divergence.** Cold replay can never show this class, so the cold pin won't capture the bug — fix the state handling anyway (Step 7b) and pin it with a unit test that reproduces the divergence, then note in the CHANGELOG what warm showed. Latches cleared by one specific track's state are the repeat offender: if the whiteness vote parks on the wrong ball, they never clear (checkpoint 51). |
| passes | passes | Live-only. Suspect starvation (Step 4 `hz`) or the presentation path. Usually not a vision bug. |
| n/a | either | No snapshot in the clip — cold only, plus a printed note. |

## Step 7 — Decide. **The default is delete.**

More clips have not meant more accuracy. Between 2026-07-27 and 2026-08-05 the
corpus went 17 → 57 scenarios and `vision/events.py` went 257 → 1842 lines
(51 tuned constants), while the miss rate stayed flat at ~3.3% — and six of
the last nine detection fixes were repairing damage done by earlier ones. A
clip is evidence of a **mechanism**, not a scenario to be covered; there are
infinite shots and a small number of causes, so coverage was never reachable
and was never the goal.

Per clip, pick one and tell Steven which:

- **Delete** — *the default.* A duplicate of a mechanism already represented,
  or explained as contention / presentation. Record the finding, drop the
  3.6 GB. "This exact shot fails" is **not** a reason to keep it.
- **Promote** — reproducible **and** it demonstrates a cause the corpus cannot
  currently represent. Say which existing scenario is the nearest neighbour and
  why it does not already cover this. If you cannot name that, delete.
- **Keep** — real evidence, not yet gradeable (e.g. waiting on the acoustic
  corroborator). Move to T7 under a descriptive name; do not add `EXPECTED`.

## Step 7b — Fix the cause. A new threshold is not a fix.

**Triage is not done when the bug is named. It is done when the bug is
gone.** Fix what you find, in this pass, without stopping for permission —
Steven has said so explicitly, and a diagnosis handed back is a defect left
live at the table. There is nobody else to pick it up.

But *what counts as a fix changed on 2026-08-05*, because the old rule is what
produced 51 interacting constants and a flat miss rate:

> A change is a fix if it **removes rules** or **corrects a cause upstream**.
> A change that adds a threshold or a special case fitted to this clip is not a
> fix, however well it is measured.

If the only available change is a new constant tuned to the clip in hand,
**write the mechanism down in the CHANGELOG and stop.** Say plainly that you
are stopping and why. That is not a parked failure in the sense Steven
prohibits (Step 7b still forbids `xfail`-and-move-on); it is refusing to add
the 52nd constant that will veto a real shot three weeks from now — which is
exactly what checkpoints 50, 51, 56, 57 and 66 each had to undo.

Nearly every miss traces to one upstream cause: **the tracker loses cue-ball
identity during the address and strike**, and the event engine then guesses.
Prefer a fix in `track.py` (association, using physics) over one more rescue
path in `events.py`.

Specifically forbidden as an ending:

- Parking a reproducible miss in `KNOWN_FAILURES` / `xfail` and calling the
  triage complete. `KNOWN_FAILURES` is only for defects that are *understood
  and unfixable today* (silent hand motion — the acoustic-corroborator
  class), never a holding pen for work not yet attempted.
- Filing the fix as a board card and stopping.
- Adding a constant to make one clip pass and reporting the corpus green as if
  that were evidence of improvement. It is evidence of no regression, which is
  a different and much weaker claim (Step 10).

When an anchor turns out to be falsified, **measure the candidate
discriminators before writing any code**: instrument the engine over the
clips that must keep passing as well as the one that must start passing,
and implement whichever actually separates them. Reasoning from the source
is not enough — checkpoint 50's obvious candidate (`merged`, the
carried-ball flag §2.3 already trusted) read `False` on all three clips
including the grab, and only instrumenting showed it.

Prove each fix with a test that **fails on the pre-fix engine and passes on
the post-fix one**, and say so. Mirror the scenario it must stay separable
from, so the pin lands on the discriminator rather than a side effect. Clear
`__pycache__` around any such before/after check (see the shell-gotchas note
on 1-second mtime granularity).

Two things that are genuinely allowed to be deferred — say plainly that you
are deferring them and why:

- Work the evidence cannot yet support (the acoustic-corroborator class).
- A defect in a **different** subsystem that the flagged clip merely
  revealed, once the flagged failure itself is fixed.

A fix that changes the vision core invalidates every `state.pkl` recorded
before it (`_vision_fingerprint()` hashes `events.py`, `track.py`,
`pipeline.py`), so **warm-replay your evidence before you change the core**,
not after. If you must confirm a warm hypothesis afterwards, unpickle the
snapshot and read the fields directly rather than bypassing the fingerprint
check — a snapshot recorded *before* the fix can still carry the damaged
state the fix prevents, so warm replay may correctly still fail. That is
evidence about the snapshot, not about the fix.

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
5. **Run the battery** (T7 mounted, no live session). Iterate cached, commit
   cold:
   ```bash
   .venv/bin/python3 -m pytest -n 4 --detect-cache   # ~21 s — while tuning the pin
   .venv/bin/python3 -m pytest -n 4                  # ~5 min — before committing
   ```
   `--detect-cache` reuses stored detector output for clips it has already
   seen; a **newly promoted clip is always computed from its real frames** on
   the first run (nothing is cached for it yet), so the new pin is honest
   either way. Cold is still the gate — run it once before the commit. If a
   cached result ever looks impossible, `rm -rf .detect-cache.nosync`; it is
   derived and rebuilds. Details in `tests/README.md`.

   `-n 4` because the M2 has exactly 4 performance cores; `-n auto` picks 8
   and spills onto the efficiency cores.
6. **Append a CHANGELOG checkpoint** — newest first, in the established
   voice: mechanism, the measurement that anchors it, the fix, the scenarios
   added, test counts.

Every constant introduced along the way must be anchored to a measurement
from the corpus. No blind threshold tuning — that discipline is how the old
app's 90–95% plateau gets avoided. **Anchoring a constant is necessary and no
longer sufficient**: see Step 7b: every one of the 51 constants in `events.py`
was anchored to a measurement, and the miss rate is flat.

## Step 9 — Drain

```bash
rm -rf ~/cueclock-dumps/<dump>      # deleted + promoted clips both leave staging
du -sh ~/cueclock-dumps/            # confirm it's drained
```

Staging lives on the internal SSD (~68 GB free ≈ 19 dumps). It is not
backed up and not synced — a dump that matters belongs on the T7.

## Step 10 — Prove it on shots nobody picked

**A green corpus is not evidence that accuracy improved.** It is evidence of no
regression, which is weaker and easily mistaken for the real thing — the corpus
went 17 → 57 scenarios and stayed green throughout the nine days the miss rate
did not move.

The test set is Steven's ordinary play: ~130 uncurated shots a night, already
counted, nothing asked of him. So the pass ends like this:

1. Record the pre-change baseline from `report --all` (764 shots / 3.27% as of
   2026-08-05) and the last week from `report --since 7d`.
2. State, in the CHANGELOG entry, **what the miss rate should do** if the fix is
   real, and roughly how many shots it takes to tell. Rough arithmetic at the
   current baseline: ~630 shots (≈5 sessions) to confirm a drop to 1%; ~2500
   (≈19 sessions) to confirm 3.3% → 2%. A fix whose predicted effect is smaller
   than the noise at this volume is a fix nobody can validate — say so.
3. Next session, run `report` again and compare. **Report the number back to
   Steven whether or not it moved**, including when it got worse.

Deletions are a result worth reporting on their own: lines removed from
`events.py` with the rate unchanged is a win, because the interaction surface
that produces self-inflicted regressions got smaller.

## Why staging is not the T7

`run --dump-dir` defaults to `~/cueclock-dumps` because dumping to the T7
saturates the USB bus it shares with every other peer: one measured press
(2026-08-01, 3.6 GB) dropped the detect loop 75 → 45 Hz and froze the Camo
side-angle source in OBS for ~1 s, still blipping 20 s later. Flagging a
failure was degrading the session being flagged. The repo is also on an
iCloud-synced Desktop, so in-repo staging would land in `bird`/`cloudd`'s
path. `~` is internal, off the USB bus, and outside iCloud's scope.
