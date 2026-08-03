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
| `report` | Ground-truth report: press stream vs. detected shots, from a session journal | no |
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

Reads a session journal and diffs the ground-truth key's presses (#19) against
the `ShotTaken` events vision actually emitted. Offline, read-only, no camera
— run it on any journal, any time, including one copied off the machine.

| Flag | Default | What |
|---|---|---|
| `--journal PATH` | *(required)* | session journal `.jsonl` — `run` writes one per session to `logs/session-<stamp>.jsonl` |
| `--press-lag SECS` | `12.0` | how long *after* its shot a press may land and still pair |
| `--late-window SECS` | `5.0` | how long *after* its press a shot may land and still pair (late detection) |
| `--idle-split SECS` | `180.0` | gap between presses that ends an armed window |

```bash
uv run python -m cueclock.cli report --journal logs/session-20260802-1930.jsonl
```

Output: presses, shots, armed windows, pairs, **MISSED** (pressed, nothing
detected), **PHANTOM** (detected, never pressed), the detection rate, and the
stroke→press lag distribution.

Four things about reading it:

- **A disagreement is a review candidate, not a label.** The press stream has
  its own error rate — a forgotten press reads as a phantom, a double tap as a
  miss.
- **The reach is asymmetric, and that is not a detail.** A press always lands
  after its stroke, by a variable amount, because the roll-out is inside that
  interval — so `--press-lag` is wide (12 s) and `--late-window` is not (5 s).
  Widening `--late-window` to match will pair a press with the *next* shot on
  any long roll-out, inventing a miss and a phantom from two correct
  detections.
- **`--press-lag` is measured; `--late-window` is still a guess.** 12 s comes
  from the 2026-08-03 session: the press follows the *settle* by 0.10–2.87 s
  (median 0.81), and the roll-out ahead of it ran 2.27–8.62 s, for an observed
  stroke→press span of 4.10–11.48 s. `--late-window` has no real session behind
  it yet — nothing has ever paired forward. Nothing auto-flags a "late" verdict.
- **Shots outside an armed window are ignored entirely.** The key is always
  live but Steven will not always press it, so presses arm the diff themselves:
  they are grouped into runs (split at `--idle-split`) and each run's window
  spans its first press to its last. **Phantom judgement gets no trailing
  extension** — a genuine phantom just after the final press of a run goes
  uncounted, the deliberate bias being to under-report phantoms rather than
  manufacture them out of a stretch he simply was not marking. Pairing alone
  reaches `--late-window` past the edge, which can rescue the last press from a
  false miss but can never invent a phantom.

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
| `--mic-device` | none — must be passed | CoreAudio name, exact then substring match (`listen --devices` lists them). No default on purpose: the Mac mini has no built-in mic |
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
