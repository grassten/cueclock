# Build progress

Running log of what's done, in flight, and decided along the way. Updated at every
checkpoint commit so a fresh session (human or Claude) can pick up without re-deriving
anything. Build order lives in `cv-app-rewrite.md` §7; this file tracks execution.

## Status by build-order step (§7)

| # | Step | Status |
|---|---|---|
| 1 | Camera bring-up + `record` CLI + test corpus | **Bring-up + `record` CLI DONE 2026-07-27**; §2.3 corpus recording is next (Steven at the table) |
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

## Post-install recalibration (current mount is a DRY RUN)

The rig will be disturbed when the proper cable track and the wall TV are installed.
After that work, redo (fast now that the workflow exists — ~15 min):

1. Re-peak focus via the live-view SHARP metric; re-lock lens screws if anything moved.
2. Re-verify WB against white paper (lighting unchanged ⇒ ratios should hold).
3. Re-save UserSet1 if anything changed.
4. **Don't invest in §4.3 homography/intrinsics calibration until after final
   install** — it's void the moment the camera moves or refocuses.
5. Re-judge display tone mapping on the actual TV through OBS (Mac-monitor tuning
   is provisional).

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

- **2026-07-27** Live-view display pipeline (display-only; raw linear data untouched
  for detection/recording), evolved over three iterations with Steven judging live:
  **CCM → luminance tone curve → saturation → clarity.** The CCM (3×3, in
  `scripts/live_view.py`) was fitted using the ball set + felt as color references —
  luminance-normalized chroma least squares, rows constrained to sum to 1 (neutrals
  immutable), tempered to 85% — because the bare sensor renders red→orange,
  orange→yellow (channel overlap; per-channel gamma made it worse). Tone curve on
  luminance only (per-channel curves bend hue); defaults black 0.04 / white 0.94 /
  γ2.2 / contrast 1.15 / saturation 1.10 / clarity 0.22, `,` `.` `;` `'` hotkeys.
  The same pipeline shape should seed the §video-output-pipeline display path
  (GPU-side) when it lands.
- **2026-07-27 (camera day)** Bring-up findings, all empirical:
  - **pylon Suite install unnecessary** — pypylon 26.7 wheel enumerated + streamed the
    camera standalone (doc §3 assumption superseded; suite kept as fallback reference).
  - **Debayer constant resolved: `cv2.COLOR_BayerBG2BGR`** for GenICam `BayerRG8`
    (OpenCV names the pattern from the opposite corner). Doc §5's `COLOR_BayerRG2BGR`
    guess was wrong — white wall rendered blue, blue cushion rendered yellow. Also
    recorded in CLAUDE.md; use BG everywhere.
  - **156.25 fps, not 160**, with zero incomplete frames: that's the camera's default
    `DeviceLinkThroughputLimit` (360 MB/s), not a host-controller ceiling. Raise/disable
    the limit if 160 ever matters; 156 is fine for v1.
  - Doc §6 WB step corrected: balance on a neutral white target laid on the cloth,
    never the bare cloth (grey-world auto-WB would neutralize the blue).

- **2026-07-27** Live-view aesthetics ROLLED BACK per Steven: the CCM + clarity pass
  amplified felt chroma noise ~3× (static previews hid the temporal dancing; live it
  looked worse). Live view is now a plain diagnostic (luminance tone map + mild
  saturation, SHARP ROI outlined on screen). The fitted CCM is banked here for the
  future OBS/GPU display path, where it should be applied WITH denoise:
  `[[2.4454, -1.256, -0.1894], [-0.1135, 1.4985, -0.385], [-0.109, -1.2503, 2.3593]]`
  (BGR order, 85%-tempered, rows sum to 1; from ball-set + felt references).
  Also verified focus empirically: ball edges 1–4 px 20–80% rise, rail diamonds 2 px,
  carpet 3–10 px at low contrast — table-level objects are the sharpest in the scene;
  "carpet looks sharper" was texture-density illusion. Focus needs no change for CV.

## Checkpoint log (append-only, newest first)

- **2026-07-27 (checkpoint 7 — `record` CLI):** `capture/` landed (`camera.py` pypylon
  wrapper re-asserting production settings, WB ratios left to the camera's UserSet1;
  `clip.py` ClipWriter/ClipReader — raw BayerRG8 `frames.raw` + `timestamps.npy`
  seconds-from-first + `meta.json`, interruption-safe, memory-mapped replay) plus
  `cli.py` (`record` implemented with disk-space guard + drop detection via timestamp
  gaps; `run`/`calibrate` stubs). 37 tests green, mypy --strict clean (pypylon override
  added). Live smoke test: 783 frames / 5.01 s = 156.2 fps, every gap exactly 6.40 ms,
  zero drops; replay round-trip verified, smoke clip deleted. Record the corpus with:
  `PYTHONPATH=src uv run python -m cueclock.cli record --label <scenario> --seconds 10`
  (~360 MB/s of disk — keep clips 8–12 s; 62 GB free ⇒ budget ~14 clips of 10 s with
  headroom). Also this session: live-view fps readout split into camera vs. view rate
  (view redraw ~40–50 fps on the monitor confused Steven; camera rate is the truth).

- **2026-07-27 (checkpoint 6 — camera bring-up complete):** camera mounted above table
  (hung inside the perimeter LED frame), focused via live-view SHARP metric
  (`scripts/live_view.py` grew focus-assist: Laplacian-variance readout + peak hold +
  exposure hotkeys), settings locked and **persisted to UserSet1 as power-on default,
  verified across `DeviceReset`**. 30 s soak: 156.2 fps, 0 incomplete frames. Full
  production state + two new hardware quirks (factory `BalanceWhiteAuto=Continuous` +
  `Daylight6500K` preset; gain/balance-ratio half-strength scale) recorded in
  `basler-camera-setup.md` §6. Light budget reality: perimeter LED ≈8× below the doc's
  assumption → iris 1 stop down + gain ≈×2 instead of f/4–5.6. Framing verified: full
  table + cushions with margin, long axis on the wide dimension. WB balanced against
  white paper (R=127/G=130/B=127 post). Next: `capture/` + `record` CLI, then the §2.3
  test corpus.

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
