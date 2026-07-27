# Build progress

Running log of what's done, in flight, and decided along the way. Updated at every
checkpoint commit so a fresh session (human or Claude) can pick up without re-deriving
anything. Build order lives in `cv-app-rewrite.md` §7; this file tracks execution.

## Status by build-order step (§7)

| # | Step | Status |
|---|---|---|
| 1 | Camera bring-up + `record` CLI + test corpus | **Blocked on hardware** (camera arriving 2026-07-27 afternoon) |
| 2 | Scaffold: package/tooling/CI ✅ · clock state machine · server/API/overlay | **In progress** — camera-free |
| 3 | Calibration tooling (ChArUco + homography) | Not started (code can be written pre-camera; only capture needs hardware) |
| 4 | Detection core offline vs. corpus | Not started — event logic (§4.2) is pure and can be built pre-camera against synthetic tracks |
| 5 | Live integration | Blocked on hardware |
| 6 | Display path → OBS | Blocked on hardware |
| 7 | Backlog items | Not started (by demand only) |

## Camera-free work queue (this is the order)

1. ✅ Fetch old `state.py` + `config.yaml` from grassten/pool-room for verbatim port
   (copies in session scratchpad; semantics captured in the port + its tests).
2. ✅ `clock/` — `Ruleset` + rulesets registry + pure `ShotClock` (injected monotonic
   time, no `time.monotonic()` calls inside) + unit tests.
3. ✅ `server/` — FastAPI app + WS hub + `shotclock.html` overlay + `control.html`,
   Stream Deck API contract per §2.1. Run it with
   `PYTHONPATH=src uv run python -m cueclock.server` (PYTHONPATH needed — see the
   .pth gotcha below); OBS browser source → `http://localhost:8000/shotclock`
   (200×200, "Control audio via OBS"), control panel → `/control`.
4. ✅ `vision/events.py` — pure §4.2 event logic (strike conjunction, settle hysteresis,
   scratch latch) + synthetic-track unit tests. **Thresholds are §4.2's
   research-backed defaults, provisional until tuned on the recorded corpus**; the
   `BallState` input contract is likewise provisional until the tracker lands.
5. ⬜ `cli.py` skeleton (`cueclock run` wiring clock + server; `record`/`calibrate` stubs).
6. ⬜ `audio/` — Vosk extension listener port (needs mic + model download to verify; port
   last). **Plan written: `docs/audio-mic-setup.md`** — covers uses, mic-agnosticism,
   the Studio-Display-mic-now → discrete-USB-mic-later path, and the bring-up checklist.

Non-code prep Steven can do without the camera: install the pylon Camera Software Suite
system-wide (required before pypylon works — `basler-camera-setup.md` §3), mount/level
the camera bracket, print a ChArUco board for §4.3 calibration.

## Camera-day runbook (next session, in order)

1. Before unboxing: install pylon Suite system-wide (`basler-camera-setup.md` §3);
   `uv sync` already has pypylon.
2. Mount + focus + lock lens screws **before** anything else — calibration is void
   if refocused later (`cv-app-rewrite.md` §4.3 order-of-operations).
3. Bring-up via the pypylon live-view script (`basler-camera-setup.md` §5 — no
   pylon Viewer GUI on Apple Silicon). Sanity-check BayerRG8 vs. the blue cloth.
4. Build `capture/` + a minimal `record` CLI (build order §7 step 1).
5. Record the §2.3 test-corpus shopping list into `tests/clips/` (gitignored),
   labeling clips by scenario name.
6. Then (or while camera ships stay pending): calibration tooling, step 3.

## Decisions made during build (append-only)

- **2026-07-27** Ruleset values ported verbatim from old `config.yaml`; all three
  rulesets currently share 30/30/1/2s/2s except `final10` (15s base). Rulesets are
  hardcoded in `clock/rulesets.py` — a config file is YAGNI until hot-reload is asked for.
- **2026-07-27** Old `pause()` semantics carried verbatim: pause → IDLE, and a later
  `start()` restarts from full base time (remaining is not preserved). Flagged here in
  case Steven wants resume-from-pause later.
- **2026-07-27** `scratch` does not touch the clock state machine in v1 (old app had no
  clock handling for it; §2.1 defines none). It stays a vision event for overlay/stats
  wiring when events land.

- **2026-07-27** Rulesets validated against published rules (supersedes the
  "verbatim port" bullet above). Corrections vs. the old config: Ultimate Pool
  singles extension is **15 s, not 30 s** (one per player per frame; sources:
  [Ultimate Pool Masters format](https://www.ultimatepoolgroup.com/article/68),
  [Professional Cup format](https://www.ultimatepoolgroup.com/article/2021-ultimate-pool-professional-cup-or-format-and-broadcast-details),
  AZBilliards Ultimate Pool USA event threads). `ultimate_9ball` was the old
  app's invented name; its values match the real
  [WNT rules](https://worldnineballtour.com//wp-content/uploads/World-Nineball-Tour-Event-Rules-1.pdf)
  (30 s clock, one 30 s extension per rack), so the canonical ruleset is now
  `wnt_9ball` with `ultimate_9ball`/`matchroom_9ball` as aliases
  (`ALIASES`/`get_ruleset()` in `clock/rulesets.py`). Kept to 3 canonical
  rulesets. Not modeled (YAGNI until asked): Ultimate Pool doubles (15 s
  extension shared per team), any per-shot base-time variation.

## Checkpoint log (append-only, newest first)

- **2026-07-27 (checkpoint 5, docs only):** `docs/audio-mic-setup.md` written —
  mic uses (voice "extension" v1; click corroborator backlog), why it's not
  mic-specific, hardware swap path, bring-up + false-positive-soak checklist.
- **2026-07-27 (checkpoint 4):** `vision/events.py` landed — `EventEngine`
  (per-ball table-mm track states in → `ShotTaken`/`BallsSettled`/`Scratch` out),
  pure and cadence-agnostic. 11 synthetic-track tests encode the §2.3 lessons
  (ball-in-hand drag, no-dwell nudge, single-frame spike, incoherent trajectory,
  pocket-edge creep, settle hysteresis, scratch latched during motion, object-ball
  pocketing, mid-table track loss). 33 tests total, all green. Not wired to the
  clock/server yet — integration happens with the tracker (step 5).
- **2026-07-27 (checkpoint 3):** `server/` landed — `Hub` (owns wall-clock time +
  WS clients + 10 Hz tick loop), FastAPI app factory with the §2.1 API contract,
  overlay + control pages ported from the old app (only change: extension button
  label no longer hardcodes "+30s"). 22 tests green incl. WS + alias contract
  tests; smoke-tested live (countdown, alias switch, overlay all verified via
  curl). The clock is now usable for practice today: server + OBS overlay +
  Stream Deck/control-page driving, no camera required.
- **2026-07-27 (checkpoint 2):** rulesets validated + corrected (15 s Ultimate Pool
  extension; `wnt_9ball` + aliases). Queue item 2 (`clock/`) done. Next: `server/`.
- **2026-07-27 (checkpoint 1):** `clock/` landed — pure `ShotClock` (injected time),
  `Ruleset` registry, 16 unit tests green, mypy --strict clean. Environment gotcha
  found today: **this Mac's venvs don't process `.pth` files**, so uv's editable
  install of `cueclock` is never importable. Tests now use pytest `pythonpath =
  ["src"]` (install-independent). If a future entry point (`cueclock` CLI) hits
  `ModuleNotFoundError`, `uv sync --no-editable` is the workaround; root cause in
  the local pyenv 3.12.12 site setup, still undiagnosed.
- **2026-07-27 (checkpoint 0):** progress doc created; old-app reference files fetched.
  Next: clock port + tests.
