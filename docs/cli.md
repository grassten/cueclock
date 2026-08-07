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
  **Flag dumps do not land here** — they stage on the internal SSD
  (`run --dump-dir`, default `~/cueclock-dumps`) and reach the corpus only
  when triage promotes them. Writing them straight to the T7 was starving
  the live session that produced them (see `--dump-dir`).
- **A live `run` session must have the machine to itself.** Corpus batteries
  (`pytest -n 4`, even the cached tier — it still saturates four cores) and
  other CPU/T7-heavy jobs starve the detect
  loop — measured 2026-08-01 evening: 78 Hz → 20 Hz (`flag_missed`'s "20 s"
  ring spans 78.4 s; a same-night dump with the machine quiet is a flawless
  12.8 ms cadence). At starved cadence a break streaks across in 2–3 detect
  frames, reads a few hundred mm/s, and the IDLE break floor eats it: "the
  clock sees nothing" with zero errors anywhere. The journal's
  `detect_heartbeat` `hz` field (~5 s cadence) is the tell — glance there
  first whenever detection seems blind.
- Ctrl-C and `kill <pid>` (SIGTERM) both unwind cleanly: clips close,
  Syphon servers retire.
- **Footgun:** `--reference` on `run` and `analyze` defaults to
  `tests/clips/empty_table` — the *original undated* clip, long stale.
  Always pass the current dated reference (the README launch command names
  it — keeping that current is part of the §E2 refresh recipe).

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
| `listen` | Voice-"extension" bring-up/soak (parked until a mic exists) | no |
| `report` | Accuracy report: missed strikes per session, read back from the journals | no |
| `deck` | Stream Deck control-surface daemon | no |

## Recipes

### Start the session

The canonical launch command lives in README ("Running it") — currently:

```bash
uv run python -m cueclock.cli run --rectify \
    --reference tests/clips/empty_table_0801 --host 0.0.0.0
```

At startup watch for `reference check ok (boundary drift X.X, noise ~2)`.
A `WARNING … drift > 8` means the reference has gone stale — detection may
be silently blind (no events, clock never starts). Refresh it (next recipe)
before trusting the clock.

### Refresh the empty-table reference (~10 min)

Condensed from `hardware.md` §E2 steps 4–5 (the full §E2 covers a bumped or
moved camera). Stage the room in playing state — bare brushed cloth, match
lighting, TV on — stop the session, step clear of the table, then:

```bash
uv run python -m cueclock.cli record --label empty_table_MMDD --seconds 3
uv run python -m cueclock.cli fit-undistort tests/clips/empty_table_MMDD
uv run python -m cueclock.cli calibrate tests/clips/empty_table_MMDD
```

`calibrate` prints the rails-fit residual — healthy is ≈ 0.76 px. If it
moves meaningfully, the camera itself shifted: do the full §E2 (level,
focus, WB) before continuing. Both fits write into `config/` where `run`
picks them up by default. Relaunch with the new `--reference`, confirm the
drift check is green, update the README launch command, and keep the old
reference clip — an end-of-session re-record compared against it is how
reference drift gets measured.

### Record a corpus clip

```bash
uv run python -m cueclock.cli record --label <scenario>_MMDD --seconds 10
```

Streams ~360 MB/s through the symlink onto the T7 — watch the frame-drop
report. Refuses to eat the last 5 GiB of the disk. Scenario naming and
recording discipline: `design.md` §9.

### Grade / triage a clip offline

```bash
uv run python -m cueclock.cli analyze --clip tests/clips/<label> \
    --reference tests/clips/empty_table_0801 [--warm] [--verbose]
```

Flag dumps live in `~/cueclock-dumps/` until triage promotes them
(`--clip ~/cueclock-dumps/<label>`). The full flag→corpus workflow — ground
truth, journal correlation, warm/cold matrix, promotion, draining staging —
is the **`/triage-clips` skill** (`.claude/skills/triage-clips/`); the flow it
implements is `hardware.md` §C5.

Deterministic replay of the pure vision core. Two modes, and knowing which
you're in matters for triage:

- **Cold (default):** the core starts from empty state — this shows what
  *current code* does with the frames, which is what corpus grading wants.
- **Warm (`--warm`, flag dumps only):** restores the live session's state
  snapshot the dump carries (`state.pkl`, written by `run` at ~1 Hz), skips
  the frames the snapshot already contains, and replays the rest —
  reproducing what the live session *actually computed*, accumulated state
  included (whiteness-vote position, roster membership, motion/episode
  latches: the divergence class cold replay can never show, e.g. the
  checkpoint-32 parked-vote shot-1 miss). Timestamps print in session time,
  matching the dump's `events.jsonl`. No snapshot in the clip → cold replay
  plus a printed note. A snapshot written by different vision-core code is
  refused loudly (check out the recording commit, or rerun cold) — never a
  silent mis-run. Use the same `--reference` the live session ran with.

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
| `--reference` | `tests/clips/empty_table` | stale default — always pass the current dated clip |
| `--calibration` | `config/table_calibration.json` | |
| `--stride` | `2` | detect every Nth frame (2 ≈ 78 Hz) |
| `--ring-seconds` | `20.0` | in-RAM rolling buffer that `POST /api/dump` saves as a labeled clip (~3.6 GB at 20 s; `0` disables). 20 s because a measured live Flag press came 12.7 s after the phantom it reacted to |
| `--dump-dir` | `~/cueclock-dumps` | where `POST /api/dump` stages flag clips. **Internal SSD on purpose, not the T7:** the T7 is USB, and a 3.6 GB dump saturates the bus it shares with every peer — one measured press (2026-08-01) dropped the detect loop 75 → 45 Hz and froze the Camo side-angle source in OBS for ~1 s, still blipping 20 s later. Flagging a failure was degrading the session being flagged. In-repo staging is out too: the Desktop is iCloud-synced. Refuses to eat the last 5 GiB |
| `--verbose` | off | per-second track status lines |
| `--no-mic` | off | disable the voice-"extension" mic |

Plus the shared display and mic args below.

Flag dumps (`POST /api/dump`) land in `--dump-dir` as `<label>_N` and carry,
next to the frames and `events.jsonl`:
`state.pkl` + `state.json` — the warm-replay snapshot `analyze --warm`
consumes (the oldest of `run`'s ~1 Hz rolling state snapshots that falls
inside the ring window; measured cost 2.3 MB / ~1 ms per snapshot —
negligible at the detect cadence). The session journal additionally stamps
vision events with `frame_t` (the confirming frame's capture timestamp,
vs. the record's emit-time `mono`) and writes a `detect_heartbeat` line
every ~5 s (effective detect Hz + age of the newest processed frame), so
detect-loop starvation is measurable from the journal alone. That is what
convicted the T7 as the dump target: the heartbeat around a flag press read
75 → 45 Hz, and the same seconds froze the Camo source in OBS. Dumps stage
locally now (`--dump-dir`), so a flag press no longer costs the session.

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

### `analyze`

| Flag | Default | Notes |
|---|---|---|
| `--clip` | required | clip directory to analyze |
| `--reference` | `tests/clips/empty_table` | stale default — always pass the current dated clip |
| `--calibration` | `config/table_calibration.json` | |
| `--stride` | adaptive | detect every Nth frame. Default adapts to the clip's stored rate so replay always runs at the live ~78 Hz cadence: record clips store 156 fps (→ 2), **ring dumps store detect-stride frames already (→ 1)** — a fixed 2 used to replay dumps at half cadence and produce different events than the live session under triage (checkpoint 33) |
| `--warm` | off | restore the flag dump's live-state snapshot (`state.pkl`) and replay from it — reproduces what the live session computed, accumulated state included. Without a snapshot: cold replay + a note. See the triage recipe above |
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
- **A miss below ~50 Hz is not a vision failure.** Flag dumps and corpus
  batteries starve the detect loop; those misses are counted separately so
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

### `deck`

| Flag | Default | Notes |
|---|---|---|
| `--server` | `http://127.0.0.1:8000` | cueclock server base URL |
| `--brightness` | `60` | key backlight 0–100 |
| `--reference` | `tests/clips/empty_table_0801` | empty-table reference the **Session** key launches `run` with. Refresh it after a camera bump, same as the README command (`hardware.md` §E2) |

The Session key (1,1) owns a whole session: tap to spawn `run` + a
`caffeinate -d` assertion and launch OBS once the server answers, hold 1 s to
quit OBS and SIGINT `run`. It only ever signals children it spawned itself — a
`run` started from a terminal reads `DEV` and is left alone. `run`'s output
goes to `~/Library/Logs/cueclock-run.log`; the daemon's own log stays at
`~/Library/Logs/cueclock-deck.log`. Full behavior: `hardware.md` §C4.

Never install the Elgato Stream Deck app — it would claim exclusive HID
access and starve this daemon.

### Shared display args (`run`, `replay`, `live`)

| Flag | Default | Notes |
|---|---|---|
| `--fps` | `30.0` | display publish rate (detection rate is unaffected) |
| `--name` | `cueclock` | Syphon server name (what the OBS Syphon Client source selects) |
| `--undistort` | `config/display_undistort.json` | |
| `--no-undistort` | off | publish distorted frames |
| `--rectify` | off | render the table axis-aligned via `config/table_calibration.json` |

### Shared mic args (`run`, `listen`)

| Flag | Default | Notes |
|---|---|---|
| `--mic-device` | `Camo Microphone` (substring match) | CoreAudio name, exact then substring match (`listen --devices` lists them). Camo's device, not macOS's Continuity `iPhone Microphone`: same physical mic, but opening the Continuity one makes macOS claim the phone that Camo is already streaming the side angle from. Needs audio switched **on** in Camo Studio — off, it opens fine and streams digital silence (hardware.md Part D). Defaulted so deck-started sessions get voice — their argv is fixed and takes no flags. Still name-matched, never "system default": if the device is absent the listener prints why and stays off |
| `--mic-model` | `models/vosk-model-small-en-us-0.15` | fetch via `scripts/fetch_vosk_model.sh` |

## Not in this CLI, but nearby

- `uv run pytest -n 4` — the corpus checkpoint battery, ~5 min (needs the T7
  mounted). Add `--detect-cache` for a ~21 s run while iterating on
  `events.py`/`track.py`; cold is the gate before committing. Two-tier
  rationale in `tests/README.md`.
- `scripts/live_view.py` — camera bring-up viewer with focus/WB/grade
  hotkeys (`hardware.md` §A3); pylon Viewer doesn't exist on Apple Silicon.
- `scripts/fetch_vosk_model.sh` — downloads the Vosk voice model.
- The HTTP/WebSocket API the deck and overlays speak: `design.md` §2.1.
