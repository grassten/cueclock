# Build progress

Running log of what's done, in flight, and decided along the way. Updated at every
checkpoint commit so a fresh session (human or Claude) can pick up without re-deriving
anything. Build order lives in `cv-app-rewrite.md` §7; this file tracks execution.

## Status by build-order step (§7)

| # | Step | Status |
|---|---|---|
| 1 | Camera bring-up + `record` CLI + test corpus | **DONE 2026-07-27** — bring-up, `record`, and the full §2.3 corpus (17 clips / 47 GB, integrity-verified; see checkpoint 10) |
| 2 | Scaffold: package/tooling/CI ✅ · clock state machine · server/API/overlay | **In progress** — camera-free |
| 3 | Calibration tooling (ChArUco + homography) | **Corpus-grade DONE 2026-07-27**: rails-fit homography (`cueclock calibrate`, 0.89 px residual). ChArUco intrinsics still pending → post-install (§4.3) |
| 4 | Detection core offline vs. corpus | **DONE — 17/17 scenarios correct 2026-07-28** (checkpoints 10–12, 15): detect/track/events pipeline, `analyze` CLI, corpus scoreboard in `tests/corpus/`. Growth continues via accuracy-ideas §7/§8 as live edge cases appear |
| 5 | Live integration | **DONE 2026-07-27** (checkpoint 14): `cueclock run` — camera + vision drive the clock/server/display in one process. First at-table session 2026-07-27 evening: working, Steven judges it already better than the old app. Accuracy beta ongoing (edge cases → corpus via accuracy-ideas §8) |
| 6 | Display path → OBS | **Code DONE + verified headless 2026-07-27** (Syphon publish, rails-fit undistort, `replay` CLI, OBS LUTs); remaining: eyes-on OBS/TV setup |
| 7 | Backlog items | Not started (by demand only) |

## Camera-free work queue (this is the order)

1. ✅ Fetch old `state.py` + `config.yaml` from grassten/pool-room for verbatim port
   (copies in session scratchpad; semantics captured in the port + its tests).
2. ✅ `clock/` — `Ruleset` + rulesets registry + pure `ShotClock` (injected monotonic
   time, no `time.monotonic()` calls inside) + unit tests.
3. ✅ `server/` — FastAPI app + WS hub + `shotclock.html` overlay + `control.html`,
   Stream Deck API contract per §2.1. Run it with
   `uv run python -m cueclock.server` (the old `PYTHONPATH=src` prefix is obsolete —
   the .pth gotcha is root-caused and fixed, checkpoint 13); OBS browser source →
   `http://localhost:8000/shotclock`
   (200×200, "Control audio via OBS"), control panel → `/control`.
4. ✅ `vision/events.py` — pure §4.2 event logic (strike conjunction, settle hysteresis,
   scratch latch) + synthetic-track unit tests. **Thresholds are §4.2's
   research-backed defaults, provisional until tuned on the recorded corpus**; the
   `BallState` input contract is likewise provisional until the tracker lands.
5. ✅ `cli.py` — every subcommand implemented; `cueclock run` wired (checkpoint 14).
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

1. **Level the camera (bubble level on the body) before locking screws** — the dry-run
   mount tilts ~1° (right/top of frame closer: +2.8% / +1.25% keystone, measured from
   the empty_table clip 2026-07-27), plus ~0.3° roll and ~3–4 cm off-center. Only the
   tilt is worth fixing physically; re-measure after install (ask Claude — boundary-line
   fit on a fresh empty-table clip).
2. Re-peak focus via the live-view SHARP metric; re-lock lens screws if anything moved.
2. Re-verify WB against white paper (lighting unchanged ⇒ ratios should hold).
3. Re-save UserSet1 if anything changed.
4. Re-run `cueclock fit-undistort` on a fresh empty-table clip (rewrites the
   gitignored `config/display_undistort.json`) and re-set the OBS crop.
5. **Don't invest in §4.3 homography/intrinsics calibration until after final
   install** — it's void the moment the camera moves or refocuses.
6. Re-judge the grade (Apply LUT amount / Color Correction) on the actual TV through
   OBS (Mac-monitor tuning is provisional).

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
- **2026-07-27** **No game-state (rules) awareness in v1** — settled while writing the
  corpus checklist. A legal ball-in-hand shaft nudge and a foul shaft/tip bump are the
  same physical motion; only game state (is ball-in-hand live? whose visit?) separates
  their correct clock outcomes, and vision cannot see that state. v1 treats both
  identically: no shot detected, clock keeps running, the human calls the foul and hits
  reset (Stream Deck / API). If match-grade foul handling is ever wanted, the state must
  come from manual input (e.g. a "foul → ball in hand" button on the §2.1 API), not
  detection — backlog.

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

- **2026-07-27** Corpus checklist adapted to two facts: (1) **Steven's table has a
  ball return** — `scratch_retrieve` (drop-pocket "reach into the pocket") is not a
  real scenario; replaced by `rack_balls` (gather from return + rack — same
  hands-and-balls-over-table provoker class) and `scratch_replace` extended to the
  full scratch → fetch-from-return → place sequence. Scratch *detection* is
  unaffected (track lost near pocket zone, §4.2). (2) Added optional
  `charuco_sweep` clip so the corpus banks intrinsics data at its own focus state —
  insurance against the post-install refocus voiding §4.3 calibration for the
  already-recorded clips. Corpus itself survives a camera move regardless: it's
  self-contained (own `empty_table` reference, homography recoverable from rail
  corners), so **recording proceeds now, not after final cable-track install**.
  Also renamed `draw_spin` → `spin_in_place` with a concrete recipe: the
  persistent spinning-while-settled state is side spin (vertical axis — back/top
  spin at rest would move the ball); the old "draw" wording conflated that with
  the draw shot's brief pause-then-snap-back, now a bonus take testing settle dwell.

- **2026-07-27** Spin-in-place counts as motion (Steven's catch, verified against
  published rules): a shot ends only when all balls have "stopped moving **and
  spinning**" — [WPA def. 8.2](https://www.poolbilliards.co/rules/wpa-rules-of-play/definitions/);
  [International Rules 6j/6k](https://www.epra.uk/rules/) ("complete when all balls
  have come to rest"; clock "will commence once all balls come to rest"); WNT
  inherits WPA wording via "Standard Rules and Regulations". So §4.2's
  centroid-velocity settle alone is NOT rules-correct for this case — `spin_in_place`
  corpus ground truth is settle-at-spin-stop, and settle needs a per-ball
  appearance-churn gate on stationary balls (noted in §4.2; build with the tracker,
  detection-core step). The old app's never-settle-while-anything-churns behavior was
  accidentally rules-correct here — the rewrite keeps the per-ball architecture and
  adds the gate scoped to the ball's own pixels.

- **2026-07-28** Clip corpus relocated to `/Volumes/T7/pool_room_clips/` (4 TB
  Samsung T7, ExFAT over USB): the internal disk was down to ~14 GB free and corpus
  expansion (accuracy-ideas §7) needs room. `tests/clips` is now a **local gitignored
  symlink** (same pattern as `.venv`) instead of a tracked one — no machine-specific
  absolute path lands in the open-source repo. `tests/clips.nosync/` deleted after a
  checksum-verified copy; its README became `tests/README.md`. T7 unmounted ⇒ corpus
  tests skip cleanly (dangling symlink) and `record` fails fast. NOTE: `record` now
  writes through the symlink to ExFAT-over-USB — smoke-test sustained ~360 MB/s write
  (watch the drop detector) before the next real corpus recording session.

## Checkpoint log (append-only, newest first)

- **2026-07-31 (checkpoint 22 — telemetry session read-out: cadence EXONERATED,
  carom guard confirmed as the sole accuracy blocker):** Steven ran the
  instrumented session (~6.2 min, log `~/Library/Logs/cueclock-telemetry.log`).
  - **Cadence healthy**: 156.2 grabs/s (camera full rate) at median AND p5;
    detect avg 4.0–4.5 ms vs the 12.8 ms stride-2 budget. Only 9/371 intervals
    below 156; worst 144.3 grabs/s for a single interval (detect max spike
    47 ms). Every ShotTaken and every miss happened at full rate. The §2
    remedies list is moot; item closed unused.
  - **Miss rate quantified**: ~10 of ~21 motion episodes produced no ShotTaken
    (10 ShotTaken / 22 BallsSettled / 1 Scratch). One shot→settle stretch
    stalled 11.8 s with the cue at rest while the ball count inflated 4→7 —
    hand/cue blobs holding settle, the "clock stays paused with hands in frame"
    behavior Steven reported live. All at full cadence ⇒ the checkpoint-20
    carom-guard mechanism explains misses, stalls, AND the live-vs-offline
    divergence (offline cold-start resets `settled_age`, so replays pass the
    guard that live play starves).
  - **Reference drift warning fired** (8.1 vs noise ~2, warn > 8) — the 07-29
    reference is stale (lighting or small camera shift). Refresh recipe is now
    todo §2; worth doing before the guard-fix session so corpus additions are
    recorded against a clean reference.
  - Next session: the guard design fix (junk-track filtering / guard rework) +
    promote `missed_strike_0731` & friends to corpus scenarios + full re-grade.

- **2026-07-31 (checkpoint 21 — detect-loop telemetry, todo §2 Part 1):** the
  `run --verbose` status line now reports per-interval `grabs/s` (retrieve rate
  == processing rate under LatestImageOnly, so a deficit vs. 156.25 is the skip
  count — the only visible sign of falling behind) plus `vision.process` wall
  time avg/max. Print-only change inside `_cmd_run`'s grab loop; dispatch left
  outside the timed section. Purpose: settle whether live cadence ever sags
  during shots — now the prime suspect for the `live_divergence_0731a/b`
  offline-correct/live-wrong pair (checkpoint 20). Next: Steven's instrumented
  at-table session (todo §2 Part 2), read-out per Part 3.

- **2026-07-31 (checkpoint 20 — first Flag-key triage: missed-strike ROOT CAUSE +
  deck dump-timeout fix):** Steven flagged three live incidents; gate-by-gate
  EventEngine replay of the dumps (scratchpad probes over the pure pipeline):
  - **`missed_strike_0731`**: shot with no ShotTaken, reproduced offline. Root
    cause is the **carom guard** — the address-time cue-stick/bridge-hand blob
    is confirmed as a "ball" track creeping on the stroke line behind the cue
    ball (ids 10/12 at ~(1465,275)), latching `table_moving`; borderline 5–6 mm
    centroid jitter (settle_creep_mm = 5) then keeps settle from re-stamping, so
    `strike_settled_guard_s` (0.4 s) rejects a textbook impulse (raw
    1→282→1185 mm/s; rest + step gates green every cycle). The 07-29 track-loss
    theory is dead for this failure: the cue track id never broke.
  - **`live_divergence_0731a`/`b`**: offline replay CORRECT (ShotTaken + settle)
    but live missed one and phantom-paused the other at 30 — live-only factor;
    detect-cadence telemetry (todo §2) is now the prime suspect, with evidence.
  - Phantom corollary: a freshly settled table satisfies the guard, so a hand
    blob yanking the cue centroid can PASS the conjunction — the guard both eats
    real strikes and admits phantoms. Fix = design session (guard rework +
    junk-track handling), then corpus scenarios from these clips + full
    re-grade. No blind threshold tuning.
  - Deck fix: the Flag key's 3 s HTTP timeout was shorter than the dump's
    ~2.2 GB T7 write, so all three successful dumps flashed red. `ApiPost` now
    carries a per-action timeout (60 s for dump) + a `SAVING…` pending face;
    regression test pins it.

- **2026-07-31 (checkpoint 19 — Stream Deck phase 1: in-repo deck daemon + four live
  keys):** Deck arrived (MK.2); approach decided with Steven: **no Elgato app, ever**
  (it would claim exclusive HID access) — buttons are versioned code, per the new
  living doc `docs/stream-deck.md` (replaces `stream-deck-plan.md`, which see for
  the model-choice history).
  - **`deck/` package**: `layout.py` (declarative key map — the doc mirrors it),
    `faces.py` (Pillow 72×72 text tiles; color-coded outcome flashes), `client.py`
    (stdlib HTTP, transport errors become return values), `daemon.py` (the only
    hardware module; StreamDeck imports function-local so tests import everywhere).
    `cueclock deck` CLI; SIGTERM unwinds via the existing `_sigterm_to_interrupt`.
  - **Live keys**: Reset, Extension (+15 s), Ruleset cycle (face tracks the active
    ruleset via `/api/rulesets`), Flag (`POST /api/dump?label=flagged`). Keys dim
    when the server is down; presses no-op. Session toggle/replay/music are
    phases 2–3.
  - **launchd agent** `com.cueclock.deck` (`scripts/install_deck_agent.sh`,
    KeepAlive) installed and running. Smoke-tested live: deck opens via brew
    hidapi with **no Input Monitoring prompt** (vendor-usage HID isn't TCC-gated).
  - Phase-1 checklist items that turned out to already exist: dump-label
    collisions auto-suffix (`_unique_dir`), T7-unmounted surfaces as a 503 the
    Flag key renders as a red `NO T7`.
  - deps: streamdeck + pillow (uv), hidapi (brew). 93 unit tests green (16 new),
    mypy --strict (StreamDeck.* override) + ruff clean. Corpus untouched.

- **2026-07-30 (checkpoint 18 — the "don't discover it at the table" layer):**
  Clock confirmed working live after checkpoint 17; overnight session built the
  instrumentation that turns silent failures into loud ones and live misses into
  corpus evidence:
  - **Startup reference check**: `run` measures `boundary_drift` (new, in
    `vision/calibration.py`) between the live first frame and the reference clip
    across a sampled ring straddling the cloth/rail boundary — the
    highest-contrast position-locked structure, immune to balls on the cloth.
    Measured levels: same mount ≈ 1.9 (balls or not), the 20 mm TV-install shift
    ≈ 35; warn at > 8. A stale reference now announces itself instead of
    presenting as "the clock never starts".
  - **Ring buffer + dump endpoint** (stream-deck-plan build item 1):
    `capture/ring.py` FrameRing (thread-safe, copies in/out) holds the last
    `--ring-seconds` (12 s ≈ 2.2 GB) of detect-cadence raw frames inside `run`;
    `POST /api/dump?label=<x>` (additive §2.1 contract) writes them through
    ClipWriter to `tests/clips/<label>/`. Dump stalls the grab ~1 s (snapshot
    under lock) — acceptable by design. The Stream Deck flag/save keys are now
    one curl shell action each.
  - **Corpus grows TV-era scenarios**: scoreboard supports per-scenario
    references (`REFERENCES` map); `shot_debug` (1 shot / 1 scratch / ≥1 settle
    — scratch verified frame-by-frame: the measle cue visibly sinks at 8.5 s)
    and `empty_table_0729` are graded permanently. 20 corpus tests green.
  - **Known bug, blocked on capture**: two live shots (t≈42 s, t≈81 s in the
    2026-07-29 evening session) fired no ShotTaken — shared signature: cue track
    lost > 1 s at impact (`no cue` mid-flight), reborn far from the rest spot, so
    the launch impulse was never seen on a cue-flagged track. The settle-driven
    reset (by design) masks it operationally. No blind threshold changes; the
    dump key exists precisely to capture the next occurrence. Related pin:
    events are cadence-sensitive (stride 4 adds a spurious pre-shot settle on
    `shot_debug`) — `run` stays at stride 2, where checkpoint 14 measured 1.38×
    realtime headroom.
  77 unit + 20 corpus tests green, mypy --strict + ruff clean. Camera-free
  except the two clips Steven recorded at the table.

- **2026-07-29 (checkpoint 17 — TV-install fallout: recalibration + the churn gate's
  first live lesson):** TV mounted; first live session went deaf ("no shots
  registering"). Two independent causes, both traced on evidence:
  1. **Camera moved ~20 mm during the install** (corners shifted 10–21 px vs. the
     Jul-27 homography) — enough to break reference-relative segmentation. Fixed by
     the standing recipe: fresh `empty_table_0729` reference + `fit-undistort` +
     `calibrate` (both 0.76 px, better than Jul-27's 0.89); corpus re-graded 18/18
     under the new calibration. **`run` must now pass
     `--reference tests/clips/empty_table_0729`** — the default still points at the
     stale Jul-27 clip, and a stale reference is silent death (only symptom: no events).
  2. **Checkpoint 16's churn gate blocked settle forever under the TV.** Glossy
     balls mirror the TV's moving image: the still 8-ball's churn read a flat
     2.2 (TV-less floor was 1.2–1.5; cloth and rail-edge churn unchanged, so it's
     ball-surface reflection, not vibration or global light). With settle never
     latching, the table stayed "moving" forever — which also suppressed ShotTaken
     via the carom guard: total event silence, though detection itself was healthy
     (verified live via `run --verbose`, then offline on a recorded `shot_debug`
     clip — same frames fired ShotTaken offline... until settle, which never came).
     Fix in `vision/events.py`, both layers measured before coding: `spin_churn_max`
     2.0 → **2.5** (spin_in_place decay crosses 2.5 at 6.69 s → settle 7.19 s, still
     over the 7.0 s corpus pin; tonight's still-ball max was 2.34) and a new
     **`spin_watch_s` = 10 s** — churn may only hold settle within 10 s of that
     ball's last real translation (spin physically decays ~2.5 s after translation
     stops; reflection noise persists forever and must not deadlock the clock).
     `shot_debug` now grades ShotTaken 6.375 s + BallsSettled 11.918 s (the green
     6-ball truly rolled until ~11.4 s). 71 unit + 18 corpus green, mypy --strict
     clean. New regression test: `test_glossy_still_ball_churn_does_not_block_settle`.
  Also tonight: control page reachable from other devices via `--host 0.0.0.0`
  (loopback default documented in `--host` help); OBS browser source shows blank if
  it loads before the server is up — right-click → "Refresh cache of current page".
  Short-term punch list (Stream Deck day-one, camera leveling baseline, ChArUco
  status) lives in `docs/todo.md`.

- **2026-07-28 (checkpoint 16 — settle-at-spin-stop churn gate: the §4.2 rules
  caveat is implemented):** BallsSettled now waits for visible spin to stop
  (WPA def. 8.2, Steven's 2026-07-27 rules catch). Evidence first: measured
  per-frame appearance churn (mean |Δgray| in a 16 px disk inside the ball)
  on `spin_in_place` — still-ball noise floor 1.2–1.5 gray levels dead flat
  for 8 s (max 1.66), the stationary spinning cue 2.6–6.5 decaying smoothly
  to the floor at t≈7.0 s, exactly the visible spin stop. Clean separation ⇒
  gate built: pipeline computes per-detection churn vs. the previous gray
  frame (`detect.ball_churn`), tracker EMA-smooths it (churn_ema 0.3) and
  reports it only for observed balls, `_is_still` treats a churning
  stationary ball like creep (threshold 2.0, `spin_churn_max`; recalibrate
  if the detect stride ever changes). Result: `spin_in_place` settles at
  **7.31 s** (was 6.48 s, mid-spin), graded by a corpus timing test
  (settle ≥ 7.0 s) + a synthetic unit test; settle count now pinned. The
  old app's accidental rules-correctness is now deliberate, scoped to the
  ball's own pixels — hands/shadows elsewhere still can't block settle.
  70 unit + 18 corpus tests green, mypy --strict clean. Camera-free.

- **2026-07-28 (checkpoint 15 — spin_in_place false Scratch fixed: corpus 17/17):**
  The checkpoint-12 xfail is gone. Root cause, traced on real frames: a dying
  track's final position includes up to a coast-budget (0.25 s) of
  constant-velocity extrapolation — in `spin_in_place` the cue was last
  *observed* mid-flight at (932,357), 359 mm from the top-middle pocket, but
  the coasting phantom slid at 1310 mm/s to (967,89), died inside the 120 mm
  pocket zone, and latched a false Scratch. A real sink is the opposite: in
  `scratch_replace` the cue's last observation is the pocket-jaw blob itself,
  110 mm from the corner-pocket center. So "lost while passing" vs. "lost
  while sinking" = **where was the ball last actually seen**. Fix:
  `BallState.observed` (False while the tracker coasts a ball through
  occlusion) and the scratch latch now records only observed cue positions
  (`vision/events.py`; §4.2 doc bullet updated). One new unit test encodes
  the lesson (`test_cue_coasting_into_pocket_zone_is_not_scratch`); the
  coast/reattach tracker test now asserts the flag both ways. 69 unit tests +
  17/17 corpus green, mypy --strict clean. Done camera-free while the rig is
  down for the TV install (TV arrives 2026-07-29).

- **2026-07-27 (checkpoint 14 — §7 step 5: `cueclock run` wired, the automated
  shot clock exists):** One process owns camera + vision + clock + server +
  Syphon display. Shape: camera/Syphon on the main thread
  (GrabStrategy_LatestImageOnly — a slow detect cycle skips frames instead of
  queueing; the event engine is cadence-agnostic), uvicorn on a background
  thread (log_level warning so events own the terminal), vision events crossing
  into the server's asyncio loop via `run_coroutine_threadsafe`
  (`app.state.loop`, stashed in lifespan). Event→clock policy: ShotTaken →
  freeze (MOTION), BallsSettled → fresh full countdown (RUNNING), Scratch →
  logged only (v1 decision above). Bootstrap: EventEngine starts not-moving, so
  launching over a still table leaves the clock IDLE until the first shot
  settles — no operator action needed to begin practice. Hub grew
  `shot_taken`/`balls_settled`; display args deduped into `_add_display_args`.
  Verified camera-free (the camera was mid-practice-session): 68 unit tests +
  mypy --strict green; the server-thread/dispatch mechanics exercised through
  the real HTTP API (IDLE→RUNNING→MOTION→RUNNING, clean shutdown); the exact
  run cadence (stride-2 detect + rectified 30 fps display) timed on real corpus
  frames at **1.38× realtime** with ShotTaken firing on `soft_shot` — measured
  while `live` + two servers shared the machine, so headroom is conservative.
  NOTE: `run` replaces both a standalone `cueclock.server` (same port) and
  `live` (camera is exclusive) — stop those first. Next: first at-table run,
  then accuracy tuning (the §4 threshold work continues on live evidence + the
  `spin_in_place` false-scratch fix).

- **2026-07-27 (checkpoint 13 — checkpoint-1 .pth mystery solved, venv out of iCloud):**
  Root cause of "this Mac's venvs don't process `.pth` files": **iCloud Drive stamps
  the macOS `UF_HIDDEN` flag on files it manages under `~/Desktop` — including inside
  `.venv` — and CPython ≥3.11 silently skips hidden `.pth` files** (`ls -lO` showed
  `hidden` on every `.pth`; `python -v` prints "Skipping hidden .pth file"). That's why
  the editable install of cueclock was never importable, and why it failed
  *intermittently*: a fresh `uv run` re-sync rewrites the `.pth` (works until iCloud
  re-stamps it minutes later). iCloud had also left a conflict copy
  (`_editable_impl_cueclock 2.pth`) and evicted `_virtualenv.pth` to dataless. Fix:
  same pattern as the clips — venv rebuilt at `.venv.nosync/` with `.venv` as a local
  gitignored symlink. Consequences: `PYTHONPATH=src` is no longer needed anywhere
  (older recipes in docs still carry it; harmless), `uv sync --no-editable` workaround
  retired, and the checkpoint-1 note is superseded. The `PYTHONPATH=src` habit for
  concurrent `.venv/bin/python3` runs still works but is now optional. The faithful
  rebuild also surfaced an undeclared runtime dep: bare `uvicorn` ships no WebSocket
  protocol, so `/ws` 404'd under the real server (starlette's TestClient speaks WS
  in-process, which is why all 22 server tests passed anyway) — `websockets` is now
  a declared dependency, live-verified with a real client (state frame received).

- **2026-07-27 (checkpoint 11 — detection core vs. real footage + rectified display):**
  The whole §4.1 chain landed camera-free in one evening (`vision/calibration.py`,
  `detect.py`, `track.py`, `pipeline.py`, `calibrate`/`analyze` CLI) and then got
  tuned frame-by-frame against `soft_shot` until it graded correctly: **ShotTaken
  fires 20 ms after contact, BallsSettled after motion ends.** Every fix was a §2.3
  lesson in modern costume; the load-bearing ones (full detail in the commit):
  - The cue stick merging with the addressed cue ball's blob disrupts the cue's own
    track in EVERY strike — strike detection now keeps a rest-memory that survives a
    ~0.35 s track handoff (+ cue-flag debounce, slow whiteness EMA).
  - Table moving/settled judged on **displacement over windows, not filtered speed**
    (blob merges corrupt velocity estimates; position survives), with an age gate
    that keeps short-lived junk tracks (hand sweeps, pocket-jaw rattle) out.
  - Detector rejects mask-border-touching stubs (lesson-5 identity thieves) and
    reference-relative shadows (measured: black ball stays outside on both axes).
  - Still balls coast frozen for 3 s (address occlusion is seconds, not the 200 ms
    the moving-ball budget assumes).
  - Rails homography → `config/table_calibration.json` (0.89 px line residual,
    1.20 mm/px at center) — corpus-grade §7 step 3; ChArUco §4.3 post-install.
  - **Display rectification**: `live --rectify` / `replay --rectify` compose the
    homography into the display remap — the TV shows the table perfectly
    rectangular and axis-aligned (verified on the real empty_table frame; answers
    "the camera looks crooked in OBS" in software, zero extra runtime cost).
  Corpus scoreboard (`tests/corpus/test_corpus_events.py`) encodes expected events
  per scenario; first full-corpus grading run is the next step.

- **2026-07-27 (checkpoint 12 — full corpus graded: 16/17 scenarios correct):**
  Three grade→trace→fix rounds against the whole corpus
  (`.venv/bin/pytest tests/corpus -q`, ~90 s). Final score: **16 of 17 pass**,
  including every §2.3 hard case: `tip_bump`, `ball_in_hand_nudge`,
  `shaft_adjust`, `practice_strokes`, `chalk_near`, `rack_balls` all correctly
  produce **zero** ShotTaken; all four real shots fire exactly one (soft 20 ms
  after contact); `scratch_replace` fires ShotTaken + Scratch. Round-2/3 fixes,
  each traced to a physical mechanism on real frames:
  - **Carom guard replaces dwell-reset** (medium/power missed): the final stroke
    itself trips the motion state ~20 ms before contact; a strike now requires
    the table settled as of ~0.4 s before the impulse (a carom's cause is older).
  - **Impulse shape** (§2.3 lesson 6): impulse must arrive ≤0.12 s after the cue
    was last still — a visible shaft/hand push ramps and is rejected.
  - **Confirm displacement 60 mm** (shaft_adjust false positive): a shaft
    adjustment can leave the ball at 570 mm/s but it stops within ~4 cm; any
    deliberate shot covers 60 mm inside the 0.25 s window.
  - **Whiteness frozen while moving fast** (cue flag jumped mid-flight): motion
    blur mixes cloth into the color sample; the flying cue now keeps its flag.
  - **Cue-regrab grace 1 s** (scratch_replace missed Scratch): when the flagged
    track dies, report *no cue* instead of crowning the next-whitest ball — the
    palest yellow measures 0.77 whiteness vs. the cue's 0.85, and scratch keys
    on absence. Pocket zone widened to 120 mm (the sinking ball's jaw-blob
    centroid drifts past the table edge).
  - **Known defect (xfail, visible in the suite):** `spin_in_place` fires a
    false Scratch — the cue occluded mid-flight near the top-middle pocket;
    needs "lost while passing" vs. "lost while slowing into the jaw" logic.
  **No clips need re-recording.** `tip_bump`'s drop gaps (checkpoint 10) don't
  matter: the bump is visible in-track at ~2.7 s (cue displaced 47 mm), well
  before the 6.2 s gap, and grades non-vacuously. Optional someday: re-record
  medium/power/break a few seconds longer so settle can be graded too (balls
  still roll when those clips end — settle expectations are unpinned for them).
  Next (build order §7): the false-scratch fix, then step 5 live integration
  (`cueclock run`: grab thread → pipeline → clock/server wiring).

- **2026-07-27 (checkpoint 10 — corpus complete + two infrastructure fixes):**
  - **Corpus recorded and verified**: 17 clips (16 scenarios + `empty_table`), 47 GB.
    Every clip's `frames.raw` size matches its `meta.json` frame count; 16/17 have a
    flawless 6.40 ms cadence (max gap = nominal frame period, zero drops). `tip_bump`
    has two drop gaps (544 ms at t=6.23 s, 77 ms at t=7.55 s — cause below); its first
    6.2 s are clean. Whether the bump moment fell inside a gap gets answered for free
    by the detection core; **re-record `tip_bump` only if it did.**
  - **iCloud Drive was strangling the machine.** Desktop is iCloud-synced, so the
    corpus queued for upload — and the account has ~20 GB quota left, so every 2.88 GB
    `frames.raw` upload died with "Quota exceeded" and retried forever (`brctl status`
    showed the loop). That I/O churn caused the `tip_bump` drops, the silently-killed
    `uv run` invocations, and a 4-minute pytest run (normally ~10 s). Fix: clips moved
    to `tests/clips.nosync/` (iCloud skips `.nosync` names) with `tests/clips` as a
    tracked symlink — all code/doc paths unchanged. Worth considering: move the repo
    off `~/Desktop` entirely (out of iCloud's domain) — Steven's call, path references
    everywhere would need updating.
  - **Syphon restarts now self-heal in OBS** (the "randomly stopped seeing my Syphon
    client" bug): OBS re-binds a server whose app+name match on *every* announce it
    hears (verified in obs-studio `mac-syphon` source), and announces are otherwise
    one-shot — so `SyphonOutput.publish()` re-broadcasts every 2 s (framework's own
    `broadcastServerAnnounce`; verified with a live directory-watcher experiment:
    graceful Ctrl-C/SIGTERM posts a clean retire, `kill -9` leaves a stale entry that
    gets pruned ~6 s after the next Syphon process starts). CLI now maps SIGTERM →
    KeyboardInterrupt so `kill <pid>` also closes clips and retires servers cleanly.
    44 tests green, mypy --strict clean.

- **2026-07-27 (checkpoint 9 — OBS eyes-on: channel order fixed, `live` added):**
  First real OBS render (replayed `break_shot`): geometry/undistort perfect, but
  **red↔blue swapped** — Syphon frames land in OBS in **RGBA byte order**, not the
  BGRA checkpoint 8 assumed (the headless client's "swap" was the truth, not an
  artifact). Pipeline now emits RGBA (`to_rgba`). Also added `cueclock live` —
  camera → display pipeline → Syphon at ~30 fps (LatestImageOnly; exclusive with
  `record`) — the interim table-on-TV feed until `cueclock run` exists. Remaining
  for the look: Apply LUT (`assets/obs/cueclock-tone.cube`) + crop, both OBS-side.

- **2026-07-27 (checkpoint 8 — display path to OBS, camera-free):** `display/` landed:
  `pipeline.py` (debayer EA → precomputed-remap undistort with **square pixels forced**
  — alpha=0's anisotropic matrix would render balls ~14% elliptical → BGRA),
  `syphon_out.py` (SyphonOutput; **must pump NSRunLoop while publishing or the server
  is invisible to all clients** — syphon-python 0.1.1 lessons in
  `video-output-pipeline.md` §3), `undistort_fit.py` (**display undistort fitted from
  the table's own rails, no printed target**: cloth-boundary extraction → Brown k1/k2
  grid search → 0.86 px residual on this mount; k1=−0.31 k2=0.09,
  `config/display_undistort.json`, gitignored — refit after every camera move).
  CLI grew `replay` (clip → Syphon at 30 fps, looping; `--test-pattern` = labeled RGB
  bars for the channel-order eyes-on check in OBS) and `fit-undistort`. OBS starter
  grades generated: `assets/obs/cueclock-tone.cube` (the dialed-in live-view look) and
  `cueclock-tone-ccm.cube` (adds banked CCM; noisier — temper with Apply LUT's amount
  slider at the TV). syphon-python darwin-gated in pyproject so CI stays green on Linux.
  Verified headless end-to-end: replay of a synthetic clip → Syphon directory discovery
  → external client received 1920×1200 BGRA with blue-dominant cloth. 44 tests green,
  mypy --strict clean. Remaining for step 6: eyes-on OBS check + filters/crop/scenes at
  the TV (waiting on corpus clips + Steven at OBS).

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
