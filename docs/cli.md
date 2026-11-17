# cueclock CLI reference

Every command goes through one entry point, run from the repo root:

```bash
uv run python -m cueclock.cli <command> [flags]
```

**Keep this file in the same commit as any `cli.py` command/flag change**
(same discipline as `hardware.md` Part C ↔ `deck/layout.py`).

Ground rules that apply to every command:

- **The camera is single-consumer.** `run`, `record`, and `live` each own it
  exclusively — stop the session before recording. Everything else
  (`replay`, `analyze`, `fit-undistort`, `calibrate`, `listen`, `deck`)
  never touches the camera and can run alongside.
- **Concurrent `uv run`s contend.** While a long-lived `uv run` process is
  alive (a `run` session, `live`, …), further `uv run` invocations can block
  or die silently — call `.venv/bin/python3 -m cueclock.cli …` directly
  instead.
- **`tests/clips` is a symlink to the T7** (`/Volumes/T7/pool_room_clips`).
  Unmounted ⇒ the symlink dangles: `record` fails fast, corpus tests skip.
- **A live `run` session must have the machine to itself.** Corpus batteries
  (`pytest -n 4`, even the cached tier — it still saturates four cores) and
  other CPU/T7-heavy jobs starve the detect
  loop — measured 2026-08-01 evening: 78 Hz → 20 Hz (a "20 s" capture
  spanned 78.4 s; a same-night one with the machine quiet was a flawless
  12.8 ms cadence). At starved cadence a break streaks across in 2–3 detect
  frames, reads a few hundred mm/s, and the IDLE break floor eats it: "the
  clock sees nothing" with zero errors anywhere. The journal's
  `detect_heartbeat` `hz` field (~5 s cadence) is the tell — glance there
  first whenever detection seems blind.
- Ctrl-C and `kill <pid>` (SIGTERM) both unwind cleanly: clips close,
  Syphon servers retire.
- `run` takes its empty-table reference from `config/reference.json`, the
  pointer `recalibrate` writes (#49), unless `--reference` is passed. It
  refuses to start with no pointer and no flag.
- **Footgun:** `--reference` on `analyze` defaults to `tests/clips/empty_table`,
  the *original undated* clip, long stale. Pass the reference the clip was
  recorded against.

## Commands at a glance

| Command | What it does | Camera? |
|---|---|---|
| `run` | The product: camera + vision drive the clock, overlay server, and Syphon display feed | owns it |
| `record` | Record a raw timestamped clip to `tests/clips/<label>` (corpus growth) | owns it |
| `live` | Live camera → Syphon to OBS, no recording (framing/bring-up) | owns it |
| `replay` | Publish a recorded clip (or test pattern) to OBS via Syphon | no |
| `analyze` | Run the detection pipeline over a clip offline; print events | no |
| `fit-undistort` | Fit display undistort params from the table's rails | no |
| `calibrate` | Fit the px→mm table homography from the rails | no |
| `recalibrate` | Record a fresh empty-table reference, refit both, swap them in only if good (the deck's RECAL key) | owns it |
| `listen` | Voice-"extension" bring-up/soak (parked until a mic exists) | no |
| `report` | Accuracy report: missed strikes per session, read back from the journals | no |
| `review` | Session review (#57): find a session's candidate moments, or serve the always-up review page | no |
| `deck` | Stream Deck control-surface daemon | no |
| `sidecam` | Side-angle camera settings: show, save the current ones, apply the saved ones | side camera only |
| `session maintain` | After-session housekeeping: journal copy, parity check, moments, footage cuts, move to the T7, footage budget (refuses while a session is live) | no |
| `session` | `replay` / `verify` a session log through current code against the live journal | no |
| `balls` | The ball list (#54) offline: over a session's stills (beside what live wrote), or a clip every N s | no |

## Recipes

### Start the session

The canonical launch command lives in README ("Running it") — currently:

```bash
uv run python -m cueclock.cli run --rectify --host 0.0.0.0
```

At startup watch for `reference check ok (boundary drift X.X, noise ~2)`.
A `WARNING … drift > 8` means the reference has gone stale — detection may
be silently blind (no events, clock never starts). Refresh it (next recipe)
before trusting the clock. The reading is also journaled
(`reference_drift`) and shown on the control page's health line, and it
turns the deck's Session dot amber.

### Refresh the empty-table reference (~1 min)

Stage the room in playing state (bare brushed cloth, match lighting, TV on),
stop the session, step clear of the table, then hold the deck's **RECAL** key
2 s, or:

```bash
uv run python -m cueclock.cli recalibrate
```

It records 3 s into a staging directory, and refuses without changing
anything if a ball is still on the cloth, no table is found, or either fit
residual is over 2 px (healthy is ≈ 0.76 px). On success it replaces
`config/display_undistort.json`, `config/table_calibration.json` and the
`config/reference.json` pointer together, keeping the previous three in
`config/backup/`. The reference lives in `~/cueclock-reference/` (internal
disk, so a session start doesn't need the T7) and is copied into
`tests/clips/` when the T7 is mounted. It keeps the new reference and the
previous one there and deletes older ones (~1 GB each). It prints the
residual and the drift before → after. The next session start picks it up;
nothing to edit. To undo, copy the three files back from `config/backup/`.

The command refuses while a session answers on `--server` (that session owns
the camera). A residual that moves meaningfully from ≈ 0.76 px means the
camera itself shifted: do the full `hardware.md` §E2 (level, focus, WB) first.

### Record a corpus clip

```bash
uv run python -m cueclock.cli record --label <scenario>_MMDD --seconds 10
```

Streams ~360 MB/s through the symlink onto the T7 — watch the frame-drop
report. Refuses to eat the last 5 GiB of the disk. Scenario naming and
recording discipline: `design.md` §9.

### Grade a clip offline

```bash
uv run python -m cueclock.cli analyze --clip tests/clips/<label> \
    --reference tests/clips/empty_table_0801 [--verbose]
```

Deterministic replay of the pure vision core, from empty state: it shows
what *current code* does with the frames, which is what corpus grading
wants. Use the reference the clip was recorded against.

`--verbose` adds per-second track status lines.

### Display-path checks

```bash
uv run python -m cueclock.cli replay --test-pattern     # labeled RGB bars → OBS
uv run python -m cueclock.cli replay --clip tests/clips/<label> --once
uv run python -m cueclock.cli live                      # live framing check
```

### Voice "extension" bring-up (parked — no mic yet)

```bash
uv run python -m cueclock.cli listen --devices           # list input devices
uv run python -m cueclock.cli listen --mic-device NAME   # every recognition + verdict
uv run python -m cueclock.cli listen --mic-device NAME \
    --post http://localhost:8000/api/extension           # end-to-end soak
```

The model comes from `scripts/fetch_vosk_model.sh`. Full plan:
`hardware.md` Part D.

## Per-command flags

### `run`

| Flag | Default | Notes |
|---|---|---|
| `--host` | `127.0.0.1` | `0.0.0.0` to reach the API from other devices |
| `--port` | `8000` | |
| `--reference` | the clip `config/reference.json` names | override the pointer `recalibrate` writes |
| `--calibration` | `config/table_calibration.json` | |
| `--stride` | `1` | detect every Nth camera frame. `1` = every frame (156 Hz, checkpoint 106); `2` = the 78 Hz cadence every constant was tuned at — the vision core converts its frame-count constants to the same span of time either way (`TUNED_HZ`) |
| `--verbose` | off | per-second track status lines |
| `--no-mic` | off | disable the voice-"extension" mic |

Plus the shared display and mic args below.

The session journal stamps
vision events with `frame_t` (the confirming frame's capture timestamp,
vs. the record's emit-time `mono`) and writes a `detect_heartbeat` line
every ~5 s (effective detect Hz + age of the newest processed frame, and
since #52 the mean ms per frame in each stage: `queue_ms` waiting for a
worker, `blobs_ms` inside `find_blobs`, `loop_ms` for churn + advance +
dispatch), so detect-loop starvation is measurable from the journal alone,
and so is where the time went. A `reference_drift` record at launch carries
the drift reading and whether it crossed the warn level.

Every session also writes a **session log** (#57) to
`sessions.nosync/<stamp>/`, `<stamp>` being the journal's: the detector's
output for every frame (rejected blobs included), every non-vision hub input,
and raw stills at each strike, half a second before it, each settle and each
operator command. `session replay` / `session verify` read it back; the layout
and the measured costs are in `design.md` §12. The writer runs on its own
thread and drops rather than waits when it falls behind; a `session_log`
journal line at exit carries its counters, and any drop journals a
`session_log_drop` line as it happens.

It also reads the **ball list** (#54, `design.md` §13) on a detect worker at
every settle and about once a second while nothing moves: each settle's list,
and each rest list that differs from the last, goes to `balls.jsonl` in the
session log, a `ball_list` journal line, and the bar's ball strip.

The stills come from a ~1 s ring of raw frames (~360 MB at 156 Hz). Until
checkpoint 114 `run` held 20 s (~7.2 GB) for the deck's Flag key
(`POST /api/dump`, `--ring-seconds`, `--dump-dir`, `analyze --warm`); all of
that was removed when the key went unpressed for three months (#55).

### `record`

| Flag | Default | Notes |
|---|---|---|
| `--label` | required | scenario name = directory name under `--out` |
| `--seconds` | `10.0` | reference clips use 3 |
| `--out` | `tests/clips` | |

### `replay`

| Flag | Default | Notes |
|---|---|---|
| `--clip` | — | clip directory to replay |
| `--once` | off | play once instead of looping |
| `--test-pattern` | off | labeled RGB bars instead of a clip |

Plus the shared display args below.

### `live`

Shared display args only.

### `fit-undistort`

| Flag | Default | Notes |
|---|---|---|
| `frame` (positional) | required | clip directory (uses its middle frame) or a `.npy` frame |
| `--out` | `config/display_undistort.json` | |

### `calibrate`

| Flag | Default | Notes |
|---|---|---|
| `frame` (positional) | required | empty-table clip directory (middle frame) or `.npy` frame |
| `--undistort` | `config/display_undistort.json` | run `fit-undistort` first |
| `--out` | `config/table_calibration.json` | |

### `recalibrate`

`record` + `fit-undistort` + `calibrate` + the pointer, in one step with the
checks a person used to make by eye (recipe above; code in
`src/cueclock/recalibrate.py`). Exit status 0 = swapped in, 1 = refused
(nothing changed). The new reference directory carries the calibration
fitted on it (`table_calibration.json`), so the corpus grades clips recorded on
that mount under it and keeps grading older clips under theirs.

| Flag | Default | Notes |
|---|---|---|
| `--server` | `http://127.0.0.1:8000` | refuse (`SESSION ON`) while a session answers here: it owns the camera |
| `--reference-root` | `~/cueclock-reference` | where the live reference is kept. Internal disk and outside iCloud, on purpose |
| `--json` | off | print the result as one JSON object on the last line (what the deck's RECAL key reads) |

Refusal reasons, as the key shows them: `SESSION ON`, `CAMERA BUSY`,
`NO CAMERA`, `DISK FULL`, `BALLS ON CLOTH` (a ball-sized hole in the cloth
mask: ≥ 450 px, where the empty references' spots measure ≤ 291 px and
balls 500+), `NO TABLE`, `BAD FIT n.npx` (either residual over 2 px).

### `analyze`

| Flag | Default | Notes |
|---|---|---|
| `--clip` | required | clip directory to analyze |
| `--reference` | `tests/clips/empty_table` | stale default — always pass the current dated clip |
| `--calibration` | `config/table_calibration.json` | |
| `--stride` | `1` | detect every Nth *stored* frame. The default replays every frame at the clip's own cadence (`ClipReader.cadence_hz`): clips from checkpoint 106 on store 156 fps, older live captures the 78 Hz stream they were detected at — so a capture always replays at the cadence its live session ran (the checkpoint 33 lesson). Larger strides are for experiments; the pipeline is built for `cadence / stride` |
| `--verbose` | off | per-second track status lines |

### `listen`

| Flag | Default | Notes |
|---|---|---|
| `--devices` | off | list input devices and exit |
| `--post` | — | URL to POST on accepted hits (e.g. `http://localhost:8000/api/extension`) |

Plus the shared mic args below.

### `report`

**The accuracy number.** Reads the session journals and counts the strikes
vision missed. Offline, read-only, no camera — run it on any journal, any time,
including one copied off the machine.

| Flag | Default | What |
|---|---|---|
| `--journal PATH` | *(newest)* | one session journal `.jsonl`; `run` writes one per session to `logs/session-<stamp>.jsonl` |
| `--logs DIR` | `logs` | where the journals live |
| `--since SPAN` | — | roll up every session in a recent span: `7d`, `24h`, `2w` |
| `--all` | off | roll up every journal on disk |

```bash
uv run python -m cueclock.cli report              # the session that just ended
uv run python -m cueclock.cli report --since 7d   # this week, one table
uv run python -m cueclock.cli report --all        # the whole history
```

`run` also prints the single-session report on its way out (so holding the deck's
Session key ends with the number on screen) and appends it to the journal as a
`session_summary` record.

**Where the number comes from — this is the part worth understanding.** Nothing
in the journal says "missed". Two detectors run per shot: *"he hit the ball"*,
which must identify the cue ball through the moment the stick and hand are one
blob with it, and *"everything has stopped"*, which needs no ball identity and
is reliable. When the first fails and the second fires, `ShotClock.balls_settled`
reasons that balls came to rest with no detected shot, so a shot must have
happened, and bumps `rack_shots` (`clock/state.py`, written 2026-07-31 for
correctness, not for measurement). Every journal record carries that count, so:

> a `vision_balls_settled` record whose `rack_shots` exceeds the preceding
> record's **is the engine confessing it missed a strike**.

Derived, never accumulated — nothing runs live, so the rate survives a crash or
a `kill -9`, and improving the reader retroactively improves every session ever
recorded.

Four things about reading it:

- **It is a floor, not a ceiling.** A shot whose *settle* was also missed leaves
  no trace at all.
- **Settles in `IDLE` are not shots.** Those are racking motion the clock
  correctly ignored; counting them would pad the denominator and make the number
  look better than the table does. The report prints them separately.
- **Phantoms are candidates, never verdicts.** The journal cannot prove the clock
  froze with no shot behind it. What it can show is an implausibly short
  shot→settle interval (<1 s against a measured 2.27–8.62 s roll-out), which is
  reported for review.
- **A miss below ~50 Hz is not a vision failure.** Corpus batteries and
  other heavy jobs starve the detect loop; those misses are counted separately so
  contention never gets mistaken for a detection bug.
- **Scratch counts come in two vintages (2026-08-07, #26).** A journal carrying
  `vision_scratch_candidate` records is adjudicated: `vision_scratch` there is a
  candidate the whiteness census could not refute, and the report prints
  candidates/refuted beside it. A journal without them predates the two-stage
  verdict and its count is an **upper bound** — the channel over-counted ~6x
  (potted object balls mis-crowned as the cue); the report marks those lines
  `(upper bound: pre-#26 journal)`. Even adjudicated counts lean high by design:
  every gate prefers failing to refute (status quo) over falsely refuting a
  real scratch.

Retired 2026-08-05: the ground-truth key (#19) and its press-vs-detection diff.
One key pressed every shot was the right idea and the wrong mechanism — it was
used in 2 sessions out of 29, because a key you must remember is a key that
stops being pressed. The compensation signal above needs nothing at the table
and covers every session ever recorded, including the two that carry presses.

### `review`

**Desk-side review of a session (#57).** Two actions, neither touches the
camera or needs `run`:

- `review moments <session dir>` reads the session's journal (`journal.jsonl`
  in the directory, else `logs/session-<stamp>.jsonl`) and writes
  `moments.json` beside it: the handful of places worth a look. Kinds:
  `reverted` (a freeze released as not-a-shot), `phantom` (exactly `report`'s
  phantom candidates), `correction` (Restart / Start / Reset, or New Rack out
  of a frozen clock; presses within 10 s are one moment), `idle_strike`
  (strikes seen with the clock IDLE, burst-merged), `synthesized` (the
  frozen-clock watchdog's settle), `quiet` (90 s of a running clock with
  nothing seen — longer than any legal turn) and `replay` (the parity check's
  diffs, when `replay.json` has any). About one per ten shots over the
  history. Ids are `<kind>-<mono ms>`, so re-running keeps verdicts attached.
  `cueclock session maintain` runs it on every session it archives.
- `review check [stamp ...]` replays every judged session (or just those
  named) through the code checked out now and grades each verdict on the
  differences inside its moment's window: a **right** verdict *holds* or is
  **REGRESSED**; a **wrong** one is *still wrong* or *changed* (maybe fixed —
  re-judge it). Exit 1 on any regression. Roughly 3 minutes per hour of play:
  the check for `events.py` / `track.py` / hub-policy work, beside the corpus
  battery.
- `review serve` serves the review page on the LAN (`0.0.0.0:8001`) and prints
  the URL a phone should open. It lists the session directories under
  `sessions.nosync/` and `/Volumes/T7/cueclock_sessions/` (an unmounted T7 is
  just skipped), and shows each moment with its footage — the session's
  `cuts/<id>.mp4`, else the full OBS recording from `sessions.nosync/
  recordings.jsonl` seeked to the moment, else "footage pruned" — the journal
  lines around it in plain words. Judging is two steps: **Clock right / Clock
  wrong** opens a note box, **Save** writes the session's `verdicts.json` and
  collapses the card to a one-line summary (**Change** reopens it, **Clear
  verdict** withdraws it); the page then scrolls to the next open moment. **Watch the full
  session** (`/s/<stamp>/full`) plays the whole recording — the session's own
  kept copy (`recording.json`), else the listed recording while it is still in
  `~/Movies` — above a list of strikes, settles, reverts and key presses; tap
  one to jump there. It only serves those cuts, kept copies and listed
  recordings. Always up under launchd as
  `com.cueclock.review` (`scripts/install_review_agent.sh`).

| Flag | Default | What |
|---|---|---|
| `--logs DIR` | `logs` | where the live journals live (both actions) |
| `--host` | `0.0.0.0` | `serve` only: bind address |
| `--port` | `8001` | `serve` only — never 8000, which is `run`'s |

```bash
uv run python -m cueclock.cli review moments sessions.nosync/20261116-225226
uv run python -m cueclock.cli review serve
```

### `deck`

| Flag | Default | Notes |
|---|---|---|
| `--server` | `http://127.0.0.1:8000` | cueclock server base URL |
| `--brightness` | `60` | key backlight 0–100 |
| `--reference` | none | pin the empty-table reference the **Session** key launches `run` with. Normally left unset, so `run` reads `config/reference.json` and a RECAL hold takes effect on the next start |

The Session key (1,1) owns a whole session: tap to spawn `run` + a
`caffeinate -d` assertion and launch OBS once the server answers, hold 1 s to
quit OBS and SIGINT `run`. It only ever signals children it spawned itself — a
`run` started from a terminal reads `DEV` and is left alone. `run`'s output
goes to `~/Library/Logs/cueclock-run.log`; the daemon's own log stays at
`~/Library/Logs/cueclock-deck.log`. Full behavior: `hardware.md` §C4.

Never install the Elgato Stream Deck app — it would claim exclusive HID
access and starve this daemon.

### `sidecam`

**The side-angle camera's settings, without Insta360 Link Controller open.**
Link Controller's live preview costs the session real frames (2026-11-12:
detection ~125 → ~80 Hz, 3.6% of OBS frames lost while it was open), so the
settings are captured once and enforced from cueclock instead (`hardware.md` §F2).

| Action | What |
|---|---|
| `show` *(default)* | every setting the camera reports, with the saved value beside any that differ |
| `save` | write the camera's current settings to `config/sidecam.json`, minus values an auto mode owns (shutter/ISO under auto exposure, Kelvin under auto white balance, focus under autofocus) |
| `apply` | write the saved settings now and read them back; exit 1 names any the camera refused |

```bash
# Set the camera up in Link Controller, QUIT Link Controller, then:
uv run python -m cueclock.cli sidecam save
uv run python -m cueclock.cli sidecam          # check it, any time
```

Every deck session start re-applies whatever drifted, once OBS has the camera
open. Needs `scripts/fetch_insta360_sdk.sh` run once: it downloads Insta360's
SDK into gitignored `vendor.nosync/` and builds the `link-settings` helper.

### `session maintain`

**Everything a finished session needs, done unattended** (#57). The
`com.cueclock.maintain` launchd agent runs it every 15 minutes
(`hardware.md` §C7); running it by hand is the same pass. It does **nothing
at all** while a session is live — the deck's pidfile names a live `run`, a
server answers `--server`'s `/api/state`, or a `cueclock run` process exists —
and exits 0, so the agent simply tries again later.

For each session directory under `sessions.nosync/` (the layout `run` writes):

1. copy `logs/session-<stamp>.jsonl` in as `journal.jsonl` (the original stays in `logs/`);
2. `cueclock session verify <dir>` → `replay.json`, if that file isn't there yet;
3. `cueclock review moments <dir>` → `moments.json`, likewise;
4. cut each moment out of the OBS recording covering it → `cuts/<id>.mp4`
   (ffmpeg stream copy, 10 s either side, matched by wall time against
   `sessions.nosync/recordings.jsonl`);
5. when that drive is mounted, hard-link the OBS recording(s) overlapping the
   session into `recording/` (indexed in `recording.json`; free on the SSD,
   and the budget still prunes the original; only the newest `--keep-full`
   archived sessions keep it), then move the directory to
   `--archive-root`: copy,
   check every file's size, then delete the local copy.

A step that is already done is skipped, so passes repeat safely. A hook command
that doesn't exist in this build is logged and skipped, and the session then
stays local until it does. Without the T7 the session is finished in place and
moved on a later pass.

Then the **footage budget**: the recordings the deck listed in
`recordings.jsonl`, plus the Replay key's saves (`~/Movies/Replay <date>
<time>.mov`, which nothing else prunes), are deleted oldest-first until they
total ≤ `--budget-gb`. Nothing else in `~/Movies` is ever touched. One
recording larger than the whole budget goes too, once its moments are cut.

Then **retention on the T7** — every kind of evidence has a purge rule, so
nothing grows without bound:

| What | Rule |
|---|---|
| a moment's cut | deleted once the moment is judged (the verdict and the log remain) |
| full recordings + unjudged cuts | only the newest `--keep-full` archived sessions keep any footage |
| session logs + stills | `--archive-gb` for all full sessions, oldest first. A session with verdicts is **pinned** instead of deleted: stripped to its header, detection log, inputs, journal, moments, verdicts and the stills inside each judged moment's window (`pinned.json` marks it), which is everything `review check` needs |
| pinned sessions | `--pinned-gb`, oldest first |
| journals in `logs/` | `--journals-gb`, oldest `session-*.jsonl` first |

| Flag | Default | Notes |
|---|---|---|
| `--dry-run` | off | print every action, change nothing (hooks and ffmpeg aren't run) |
| `--budget-gb` | `10` | footage kept on the internal disk, in GB (10⁹ bytes); ~3 GB per hour of recording |
| `--keep-full` | `10` | archived sessions that keep any footage (full recording, unjudged cuts), newest first |
| `--archive-gb` | `100` | session logs + stills on the T7 (~1.7 GB per hour of play) |
| `--pinned-gb` | `5` | pinned sessions (judged, stripped to their detection log) |
| `--journals-gb` | `1` | session journals in `logs/` (~1 MB each) |
| `--root` | `sessions.nosync/` | local session root |
| `--archive-root` | `/Volumes/T7/cueclock_sessions` | under `/Volumes` it must be a mounted volume, or sessions stay local |
| `--logs` | `logs/` | where the session journals are |
| `--movies` | `~/Movies` | OBS's recording folder (Simple output → Recording Path) |
| `--server` | `http://127.0.0.1:8000` | a server answering here means a session is live |

```bash
uv run python -m cueclock.cli session maintain --dry-run   # what would it do?
uv run python -m cueclock.cli session maintain
tail -f ~/Library/Logs/cueclock-maintain.log                 # the agent's passes
```

Exit 1 when a step failed (that session stays local and is retried next pass).

### `session`

**Replay a session log through the code checked out now** (#57). `<session>`
is a stamp (`20261116-225226`), looked up in `sessions.nosync/` and then the
T7 archive `/Volumes/T7/cueclock_sessions/`, or a session directory.

| Subcommand | What |
|---|---|
| `replay <session>` | logged detections → tracker → events → hub on a virtual clock, inputs re-applied at their logged times, ticks at 10 Hz; writes a journal in the live format |
| `verify <session>` | replay, then compare its decision records (vision verdicts, provisionals, commands, tick transitions: kind, state, rack_shots, verdict fields, remaining within 0.2 s) with the live journal; writes `<session>/replay.json`. Exit 0 = reproduced, 1 = diverged, 2 = nothing to compare |

| Flag | Default | Notes |
|---|---|---|
| `--out` | `<session>/replay-journal.jsonl` | the replay's journal |
| `--from`, `--to` (`replay`) | whole session | keep records from / stop at: seconds into the session, or an ISO wall time (UTC unless it says otherwise). Replay still starts at the top, since the tracker and clock need the history |
| `--journal` (`verify`) | `<session>/journal.jsonl`, else `logs/session-<stamp>.jsonl` | the live journal |

```bash
uv run python -m cueclock.cli session verify 20261116-225226
uv run python -m cueclock.cli session replay 20261116-225226 --from 600 --to 660
```

`replay.json` carries `ok`, `code_sha` (HEAD now), `same_code` (HEAD equals
the session's `git_sha` and neither tree was dirty) and up to 50 `diffs`, each
with the live record's `wall`/`mono`, a one-line `summary` and both records.
**A divergence under `same_code` means the log is missing an input**: under
the code that ran live, replay must reproduce the session. Under newer code a
divergence is what the change did.


### `balls`

**The ball list (#54) offline**, from the same pure function `run` reads the
table with (`design.md` §13). One line per frame: time, reason, `arm` when a
hand was over the table, and the labels in strip order (`cue`, `1`–`15`, `?`).

| Target | What |
|---|---|
| a session (stamp or directory) | every still, read against the reference and calibration the session's header names; a still with a list written live gets `= live`, or `live: <labels>` where they differ |
| a clip directory | a frame every `--every` seconds; needs `--reference` |

| Flag | Default | Notes |
|---|---|---|
| `--reference` | the session's own | the empty-table clip; required for a clip |
| `--calibration` | `config/table_calibration.json` | for a clip whose reference carries no `table_calibration.json` |
| `--every` | `1.0` | seconds between a clip's frames |

```bash
uv run python -m cueclock.cli balls 20261117-001351
uv run python -m cueclock.cli balls tests/clips/pickup_snatch_0801 --reference tests/clips/empty_table_0801 --every 2
```

Reads only; safe beside a live session apart from the CPU it takes (~20 ms a
frame).
### Shared display args (`run`, `replay`, `live`)

| Flag | Default | Notes |
|---|---|---|
| `--fps` | `30.0` | display publish rate (detection rate is unaffected; publishing runs on its own thread in `run`). The production launch passes `--fps 60` — the deck's Session key and the README command both carry it (TV runs 4K60, 2026-08-07) |
| `--name` | `cueclock` | Syphon server name (what the OBS Syphon Client source selects) |
| `--undistort` | `config/display_undistort.json` | |
| `--no-undistort` | off | publish distorted frames |
| `--rectify` | off | render the table axis-aligned via `config/table_calibration.json` |

### Shared mic args (`run`, `listen`)

| Flag | Default | Notes |
|---|---|---|
| `--mic-device` | `Insta360 Link 2C Pro` (substring match) | CoreAudio name, exact then substring match (`listen --devices` lists them). The side-angle camera's own mic (hardware.md Part F). A camera that has never run Insta360 Link Controller opens fine and streams digital silence (hardware.md Part D). Defaulted so deck-started sessions get voice — their argv is fixed and takes no flags. Still name-matched, never "system default": if the device is absent the listener prints why and stays off |
| `--mic-model` | `models/vosk-model-small-en-us-0.15` | fetch via `scripts/fetch_vosk_model.sh` |

## Not in this CLI, but nearby

- `uv run pytest -n 4` — the corpus checkpoint battery, ~5 min (needs the T7
  mounted). Add `--detect-cache` for a ~21 s run while iterating on
  `events.py`/`track.py`; cold is the gate before committing. Two-tier
  rationale in `tests/README.md`.
- `scripts/live_view.py` — camera bring-up viewer with focus/WB/grade
  hotkeys (`hardware.md` §A3); pylon Viewer doesn't exist on Apple Silicon.
- `scripts/fetch_vosk_model.sh` — downloads the Vosk voice model.
- `scripts/setup_obs_replay.py` / `scripts/setup_obs_bracket.py` — build the
  Replay and Bracket scenes over obs-websocket. Idempotent; never assemble
  either by hand (`hardware.md` §B3).
- The HTTP/WebSocket API the deck and overlays speak: `design.md` §2.1.
- Tournament nights have no CLI at all: the bracket is built, driven and
  archived from `/control`'s Bracket tab and the Stream Deck (`design.md` §10).
  A live one persists to `config/bracket.json` beside the score, so a server
  restart mid-tournament is survivable; finished ones archive into `logs/`.
