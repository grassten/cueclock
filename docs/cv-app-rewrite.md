# CV Application — Existing App Inventory & Greenfield Rewrite Plan

**Decision (2026-07-27):** the existing codebase is scrapped entirely — including the tests and the iPhone-era test videos. This document is the bridge: it records what the old app does (so the new one re-implements the right behaviors), what the old app *learned* (so the new one doesn't re-discover known failure modes), and the research-backed design + engineering standards for the rewrite. The new project will be open-sourced.

---

## 1. Why rewrite instead of adapt

The old `vision.py` plateaued at ~90–95% shot-detection accuracy, and nearly all of its complexity — the two-stage arm/confirm window, `max_jump_px` teleport guards, cue-axis consensus smoothing, occlusion grace, stillness bookkeeping — exists to fight one root cause: **single-blob HSV detection on a noisy, auto-everything iPhone feed**. The detector could only find *one* ball, couldn't trust any single frame, and had no notion of real-world position or speed. The Basler rig (locked exposure/WB, global shutter, 160 fps, fixed geometry, calibrated optics) removes those error classes at the source and makes a fundamentally better architecture possible: **track all 16 balls in true table coordinates and derive events from per-ball velocities**. That architecture isn't a patch to the old code; it replaces its core assumption. Every pixel-unit tuning constant would have needed recalibration for the new optics anyway.

The clock state machine, web/API surface, and audio listener were fine — but they're small, and rewriting them inside a clean package costs less than grafting old top-level modules into an open-source-quality repo.

## 2. Inventory of the existing app — what it does

Old architecture: one asyncio process; vision + audio worker threads feeding a `ClockState` state machine via callbacks; FastAPI serving two HTML surfaces; WebSocket broadcast at 10 Hz.

### 2.1 Behaviors to re-implement (the spec hiding in the old code)

| Old component | Behavior worth carrying forward (as spec, not code) |
|---|---|
| `state.py` `ClockState` | `IDLE → RUNNING → MOTION (shot in progress) → RUNNING (reset to full) → GRACE (2 s after 0) → EXPIRED (flash) → auto-reset for next player`. Extension allowed in RUNNING/GRACE only; adds `extension_seconds`; re-arms urgency cues; respects `extensions_per_frame`. Edge-triggered audio cues `ten`/`five`/`expired`, fired exactly once per countdown. |
| `Ruleset` config | `ultimate_pool_8ball` (30 s base / 1×30 s extension / 2 s grace / 2 s post-expiry), `ultimate_pool_8ball_final10` (15 s variant), `ultimate_9ball`. Values carry over verbatim. |
| HTTP API | `POST /api/{start,pause,reset,extension}`, `POST /api/ruleset/<name>`, `GET /api/{state,rulesets}` — this is the **Stream Deck contract**; keep the URLs stable so the Stream Deck buttons keep working. |
| WebSocket `/ws` | Broadcast `{state, time, display, ruleset, extensions_used, cue}` at 10 Hz + snapshot on connect and on every event. |
| `shotclock.html` | OBS browser source, transparent 200×200; Web Audio chimes (10 s: E5 sine; 5 s: A5 triangle; expired: G4→D5 cadence); visual states: motion dims, grace red border, expired flashing. Requires "Control audio via OBS" for stream audio. |
| `control.html` | Buttons over the same API + live state display. |
| `audio.py` | Vosk small-EN model, grammar `["extension", "[unk]"]`, 16 kHz mono, min confidence 0.9, 2 s cooldown. Proven; re-implement as-is. |
| `server.py` Hub | Worker-thread → asyncio bridge (`run_coroutine_threadsafe`), client set management. Pattern carries over. |
| `calibrate.py` | Click-4-corners table ROI. **Superseded** by real calibration (ChArUco intrinsics + homography, §4.3). |
| Vision config block | Dies with the heuristics. The new detector has far fewer, physically meaningful knobs (m/s thresholds, dwell times). |

### 2.2 Known limitations of the old app (unfinished business, not bugs)

- No config hot-reload; extensions counter not persisted across frames; no auto-switch to final-10 ruleset; no match timer/frame counter/stats. All optional backlog for the new project — YAGNI until asked for.

### 2.3 Hard-won domain lessons that carry forward as requirements & test cases

These came from weeks of iteration on real footage. The new system must not re-learn them:

1. **Shaft contact ≠ shot.** Adjusting the cue ball with the shaft, or shuffling during ball-in-hand, puts a cue-like line adjacent to the ball. Only tip-end contact + subsequent motion along the cue axis indicates a strike. (The patent literature independently converged on this: US8992315's "shot taken" flag requires tip contact, and it validates direction against the cue's principal axis.)
2. **Never shape-filter "is anything moving."** Fast-rolling balls streak into elongated blobs; a circularity filter falsely reports stillness mid-shot. (Independently confirmed by the GoCardless pool tracker's "blurry, elongated blobs" experience.)
3. **Scratch must be latched during motion**, not evaluated at settle time — the player walks up and replaces the ball before settle fires.
4. **Brief occlusions are normal.** Hands/cue pass over balls constantly; a track must survive ~200 ms of missing detections without declaring motion or loss.
5. **Blob confusion was the #1 failure mode** — chalk, shirts, ferrules, other balls stealing the "cue ball" identity and producing 500–1000 px phantom jumps. Per-ball tracking with gated association kills this class outright, but the test corpus must still include the provoking scenes.
6. **The genuinely hard ambiguity: slow ball-in-hand nudges with the cue tip** are visually near-identical to a soft legal shot in pixel-motion terms. The new architecture has three real answers unavailable to the old one: pre-strike stillness dwell on a *reliable* per-ball track, velocity-impulse shape (a strike is a step from rest to ≥0.2 m/s in 1–2 frames; a placement is continuous low-speed drag), and optionally an acoustic strike-click coincidence (see §4.4).
7. **Detection cadence ≠ event cadence.** The clock only needs events; detection can run at half rate while capture runs at full rate for replay.

**Test-corpus shopping list** (record with the Basler in week 1 — these are the old failure modes as labeled clips): soft/medium/power shots, break shots, ball-in-hand placement by hand, ball-in-hand nudge with cue tip, practice strokes over the ball, shaft-adjustment of cue ball, hand retrieving ball after scratch, walking around the table mid-settle, chalking near the table, scratch + replace, balls settling with a slow-creep finish, ball spinning in place after a draw shot.

## 3. Requirements for the rewrite

**Functional:**
- Emit `shot_taken`, `balls_settled`, `scratch` events driving the shot-clock state machine (semantics of §2.1).
- Voice "extension" keyword → extension (§2.1 audio row).
- HTTP/WS surface per §2.1 (Stream Deck compatibility).
- OBS overlay + control panel equivalents.
- Publish the display feed to OBS (see `video-output-pipeline.md`).
- Later (backlog): break-speed measurement, ball-position stream, replay export from ring buffer, match stats.

**Non-functional:**
- Detection accuracy target: very high 90s on the labeled corpus; every regression reproducible offline.
- Deterministic replay testing: the detection core is pure (frames + timestamps in, events out) and runs against recorded clips in CI.
- Open-source quality: src-layout package, typed, linted, documented, security-scanned (§5).
- KISS/YAGNI: build the smallest thing that beats the old accuracy; the fancy stages (replay, break speed, stats) come after the clock works.

## 4. New detection design (research-backed)

The research behind every claim here is summarized with sources in §6. Frame budget at 160 fps is 6.25 ms; measured costs on this M2 make the budget comfortable: Bayer→gray 0.12 ms, full-frame background diff ≈ one vectorized op, `connectedComponentsWithStats` linear-time, `undistortPoints` on all detections ~0.01 ms.

### 4.1 Pipeline shape

```
pypylon grab thread (BayerRG8 @ 160 fps)
 ├─► ring buffer (raw Bayer, last N seconds)          → replay export (later)
 ├─► detection path (every 2nd frame ≈ 80 Hz):
 │     Bayer→gray (0.12 ms) + color sampling from raw Bayer at ball patches
 │     → |frame − empty-table reference| → threshold → morphology
 │     → connectedComponentsWithStats + area/circularity gates (ball ≈ 41–45 px ⇒ tight area window)
 │     → tracker: Hungarian assignment + per-ball Kalman (coast through occlusion)
 │     → cue-ball ID: white-fraction + hue stats on central disk, resolved globally
 │     → undistortPoints + homography → positions/velocities in table mm
 │     → event logic (§4.4) → clock state machine
 └─► display path (30–60 Hz): debayer (EA) → full-frame remap → Syphon → OBS
```

Key architectural choices, each the researched consensus for this setting:

- **Classical segmentation, not a neural detector.** Fixed camera + locked exposure + known background is the textbook case where background differencing against a captured empty-table reference beats everything (POOL-AID and the >99%-detection snooker literature use exactly this). Stationary balls never fade (unlike MOG2/KNN adaptive subtractors, which are the wrong tool — they absorb stopped balls). HoughCircles is fragile and unnecessary when radius is known. A CoreML nano-detector (3–10 ms on ANE) stays in reserve as a low-cadence auditor if ever needed — not in v1 (YAGNI).
- **Track all balls, minimally.** At 160 fps even a 13 m/s break moves a ball ~1.4 diameters/frame; at 80 Hz detection it's ~2.8. Nearest-neighbor/Hungarian association with a generous gate is nearly unambiguous; Kalman filters earn their keep only for coasting through hand occlusions and for smoothed velocity — not for association. Norfair is the off-the-shelf fit (centroid input, custom distance, ReID hook for color re-identification after occlusion) if we don't hand-roll; pedestrian MOT stacks (ByteTrack/OC-SORT/BoT-SORT) are dead weight here.
- **Cue ball identification** by white-fraction + hue statistics on a central disk of each ball patch, resolved **globally** (Hungarian against ball identities) rather than per-patch — POOL-AID's 72% → 98.6% lesson. For the shot clock only cue-vs-rest matters; full 16-ball identity is backlog.
- **Undistort points, not pixels.** Detection runs on distorted frames; only detected coordinates go through `undistortPoints` + homography (~200× cheaper than full-frame remap, and the detector sees unresampled pixels). Full remap runs only on the display path. Straight-line logic (if we ever detect the cue stick again) must account for curvature in distorted space — undistort line endpoints too.

### 4.2 Event logic (the actual product)

- **`shot_taken` — a conjunction, not a single signal** (patent US10226685 + US8992315 + launch-monitor pattern):
  1. cue ball stationary ≥ dwell (the old `pre_strike_stillness_ms`, now on a reliable track),
  2. velocity impulse: from below the settle floor to **≥ ~0.2–0.3 m/s within 1–2 detection frames** (softest deliberate shot ≈ 0.45 m/s — Dr. Dave; noise floor ≈ 0.007–0.022 m/s — §4.3), and
  3. displacement consistent over the next few frames (coherent trajectory — the confirm-window idea, now trivial with tracks).
  Ball-in-hand placement fails (1); practice strokes fail (2); hand-waves fail (2)+(3).
- **`balls_settled` — per-ball velocities with hysteresis**, not global pixel motion: every tracked ball's filtered speed **< 0.03–0.05 m/s** (RoboCup SSL autoref uses 0.05–0.2 m/s for officiating-grade "stationary") **and** max any-ball displacement over the last ~500 ms < ~2–3 mm (catches pocket-edge creep), sustained for a dwell; re-enter "moving" only above ~0.1–0.15 m/s (Schmitt-trigger hysteresis so it can't chatter). Per-ball velocity is immune to the failure modes that plagued global frame-diff: hands in frame, player shadows, a ball spinning in place. **Rules caveat (verified 2026-07-27, Steven's catch):** spin-in-place immunity overshoots — a shot officially ends only when all balls have "stopped moving **and spinning**" ([WPA def. 8.2](https://www.poolbilliards.co/rules/wpa-rules-of-play/definitions/); International Rules 6k's "come to rest" implies the same), so `balls_settled` must additionally wait for visible spin to stop. Candidate mechanism when the tracker lands: a temporal-churn gate on pixels *inside* a stationary ball's own radius (same dwell/hysteresis machinery; graded by the `spin_in_place` corpus clip). Hands/shadow immunity is unaffected — the gate never looks outside the ball.
- **`scratch`** — the cue-ball track terminates (not re-associated, not coasting) with last position near a pocket zone; latch during motion exactly as the old system learned. Pocket zones come from the homography, so this is finally geometric rather than "ball missing anywhere." **"Last position" means last *observed* position (corpus lesson, 2026-07-28):** a dying track's final coordinates are up to a coast-budget of constant-velocity extrapolation, and an occluded passing cue's phantom can coast into the zone and die there (`spin_in_place`: last real observation 359 mm from the pocket, coasted death 89 mm). A real sink needs no such generosity — its final observations come from the pocket jaw itself (`scratch_replace`: last observation 110 mm from the pocket center).
- **Optional corroborator (backlog):** a broadband transient ("click") detector on the existing audio stream, coincident within ~100 ms of the velocity impulse — patent-precedented (US10226685), nearly free since an audio thread already exists, and it cleanly separates the one genuinely ambiguous case (slow tip nudge vs. soft shot: no click vs. click).

### 4.3 Calibration & coordinates

- **Intrinsics:** the Kowa 3.5 mm has −28% TV distortion — right at the boundary where the plain 5-coefficient pinhole model fails. Calibrate **both** `CALIB_RATIONAL_MODEL` and `cv2.fisheye` from the same corner data; pick by RMS + corner residuals (expect < 0.5 px RMS; > 3 px = wrong model). Use a **ChArUco board** (not plain checkerboard): partial views are usable, so the board can hang off the frame edges where distortion is worst. 20–30 poses, ±45° max tilt, squares ≥ 20 px, focus/aperture locked first (calibration is invalid if the lens is refocused — order of operations: mount, focus, lock screws, then calibrate).
- **Homography:** undistorted rail-corner points → table mm. All thresholds in §4.2 are then physical units — portable, meaningful, and tunable without re-deriving pixel scales. Scale sanity check: ~1440 px across 1.93 m ⇒ ≈ 0.75 px/mm; centroid noise σ ≈ 0.1–0.3 px ⇒ velocity noise ≈ 7–22 mm/s raw at 40 Hz, halved by light smoothing — which is why the 30–50 mm/s settle floor is ≥ 3σ clean.
- Recalibrate if the camera is ever bumped (keep the calibration script as a first-class package command, not a one-off).

### 4.4 Ring buffer & threading

- Grab thread (pypylon, `MaxNumBuffer` ≈ 64) writes raw Bayer into a preallocated numpy ring buffer and a "latest frame" slot. Detection thread consumes every 2nd frame; display thread every ~3rd–5th. GIL is fine: all heavy ops are NumPy/OpenCV releasing the GIL; the pypylon-converter-per-frame bottleneck reported in pypylon issues is avoided by doing `cvtColor` ourselves.
- Ring sizing: Bayer8 frame = 2.3 MB; 160 fps ⇒ 369 MB/s ⇒ **6 s ≈ 2.2 GB** — comfortable on 24 GB. 10 s (3.7 GB) is possible but start at 6 (YAGNI; it's one constant).
- Replay export (backlog): dump ring to disk on demand → 160 fps clip played at 30 = 5.3× slow-mo.

## 5. Project scaffold — open-source engineering standards

Greenfield repo (new directory/repo, not this one). 2026 Python best practice, kept deliberately minimal:

```
cueclock/
├── pyproject.toml                # uv-managed; PEP 621 metadata; hatchling build
├── uv.lock
├── .pre-commit-config.yaml
├── .github/workflows/ci.yml     # lint + typecheck + tests on push/PR
├── LICENSE                      # MIT
├── README.md
├── CLAUDE.md                    # fresh one, written for the new architecture
├── src/
│   └── cueclock/
│       ├── capture/             # pypylon grab loop, ring buffer, camera config/UserSet
│       ├── calib/               # ChArUco intrinsics, homography, image↔table transforms
│       ├── vision/              # pure detection core: segmentation, tracker, events
│       ├── clock/               # state machine + rulesets (port of state.py semantics)
│       ├── audio/               # Vosk extension listener
│       ├── server/              # FastAPI app, WS hub, static overlay/control pages
│       └── cli.py               # `cueclock run`, `cueclock calibrate`, `cueclock record`
└── tests/
    ├── unit/                    # state machine, tracker, event logic (synthetic tracks)
    └── clips/                   # Basler clip harness (local fixtures, gitignored)
```

- **Tooling:** `uv` for deps/venv/lock (pipenv is not carried over), `ruff` for lint **and** format, `mypy` (strict on the pure core) or `pyright`, `pytest`.
- **Pre-commit hooks** (`pre-commit` framework): `ruff` + `ruff-format`; `gitleaks` (secret scanning); `bandit` (Python security lint); standard hygiene hooks (end-of-file, trailing whitespace, `check-yaml`, `check-added-large-files` — the last one matters with video clips around). Dependency vulnerability audit (`pip-audit` or `osv-scanner`) belongs in **CI, not pre-commit** (network-dependent and slow; shift-left but not into every commit).
- **CI:** GitHub Actions — ruff, mypy, pytest with the synthetic-track unit suite always; the clip-replay suite behind a cache/fetch step.
- **Test videos:** raw Basler clips are huge (2.3 MB/frame uncompressed; even encoded, a corpus is GBs). **Decision: the corpus is not published.** Clips live in a local, gitignored `tests/clips/` directory (path configurable); the clip-replay test suite skips cleanly when fixtures are absent, so CI and outside cloners run the synthetic-track unit suite only. No fetch script, no LFS.
- **Purity rule (the one dogma worth keeping from the old repo):** the vision core takes `(frame, timestamp)` and returns events/state — no I/O, no clocks, no threads — so the clip harness stays deterministic.
- **KISS enforcement:** v1 scope = clock + events + overlay + API. No replay export, no break speed, no stats, no 16-ball identity until the clock is trustworthy.

## 6. Research appendix — sources behind §4

- **Detection approach** (classical-over-NN for fixed rigs; empty-table reference diff; MOG2/KNN absorb stopped balls; HoughCircles fragility; POOL-AID's 13.2 ms loop and 72→98.6% identification via global assignment): [POOL-AID (UCSD)](https://kastner.ucsd.edu/ryan/wp-content/uploads/sites/5/2014/03/admin/pool-aid.pdf) · [Roboflow classical-vs-DL guidance](https://blog.roboflow.com/deep-learning-vs-traditional-computer-vision/) · [PoolLiveAid](https://www.researchgate.net/publication/266394007_PoolLiveAid_Augmented_reality_pool_table_to_assist_inexperienced_players) · [>99% snooker ball detection thesis](http://eprints.fri.uni-lj.si/2590/) · [OpenCV MOG2 docs](https://docs.opencv.org/3.4.20/d7/d7b/classcv_1_1BackgroundSubtractorMOG2.html) · [PyImageSearch on HoughCircles tuning](https://pyimagesearch.com/2014/07/21/detecting-circles-images-using-opencv-hough-circles/)
- **Tracking** (assignment triviality at high fps; Kalman for coasting only; Norfair; MOT-stack overkill): [Norfair](https://github.com/tryolabs/norfair) · [PyImageSearch centroid tracking](https://pyimagesearch.com/2018/07/23/simple-object-tracking-with-opencv/) · [OC-SORT](https://arxiv.org/pdf/2203.14360) · [billiards-specific MOLT tracker](https://link.springer.com/article/10.1007/s10489-023-04542-3) · ball-speed anchors: [Dr. Dave typical speeds](https://drdavepoolinfo.com/faq/speed/typical/), [break speed](https://drdavepoolinfo.com/faq/break/speed/)
- **Strike detection** (conjunction design; dual-detector + strike-sound patent; tip-contact + axis-agreement patent; accelerometer ground truth that tip impacts are discriminable; arm-then-confirm practitioner pattern): [US10226685 — automated pool shot clock](https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/10226685) · [US8992315 — tip-contact "shot taken" + miscue axis check](https://patents.google.com/patent/US8992315B2/en) · [DigiCue FAQ](https://www.obcues.com/digicue-blue-frequently-asked-questions/) · [RPi shot-recorder thread](https://forums.raspberrypi.com/viewtopic.php?t=260271) · CUSUM/hysteresis background: [Schmitt trigger](https://en.wikipedia.org/wiki/Schmitt_trigger), [CUSUM change-point notes](https://www.lancaster.ac.uk/~romano/teaching/2425MATH337/1_intro_cusum.html)
- **Settle detection** (officiating-grade stationarity thresholds; per-ball vs global motion failure modes; spinning-in-place as a real state): [RoboCup ssl-game-controller `IsSteady` = 0.2 m/s](https://github.com/RoboCup-SSL/ssl-game-controller/blob/master/internal/app/engine/common.go) · [Erlangen autoref 0.05 m/s](https://github.com/robotics-erlangen/autoref/blob/master/autoref/validation-rules/placementsuccess.lua) · [pooltool ball states](https://ekiefl.github.io/2020/12/20/pooltool-alg/) · [US8016665 — skin/motion/contour occlusion flags](https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/8016665)
- **Undistortion & debayering** (model choice at −28% TV; ChArUco practice; undistort-points pattern; measured M2 costs: remap 2.0 ms, bilinear debayer 0.135 ms, EA 0.224 ms, VNG 7.7 ms — VNG can't do 160 fps; USB controller ceilings <350 MiB/s): [CalibrX camera-model guide](https://calibrx.io/blog/pinhole-wide-fisheye-camera-models/) · [calib.io best practices](https://calib.io/blogs/knowledge-base/calibration-best-practices) · [OpenCV ChArUco tutorial](https://docs.opencv.org/4.13.0/da/d13/tutorial_aruco_calibration.html) · [OpenCV fisheye module](https://docs.opencv.org/4.x/db/d58/group__calib3d__fisheye.html) · [PiVR undistort-points-not-pixels](https://gitlab.com/LouisLab/pivr/-/issues/64) · [Basler debayering guidance](https://www.baslerweb.com/en-us/learning/avoiding-color-artifacts/) · [Basler USB3 controller bandwidth app note](https://assets-ctf.baslerweb.com/dg51pdwahxgw/4N0SupZ9UpIdxerGU2bXoL/3feef7a06f126ab852eaf3f69997aedf/AW00126010000_USB3.0_Host_Controllers_Max_Bandwidth.pdf) · [pypylon converter bottleneck #588](https://github.com/basler/pypylon/issues/588)
- **Prior art worth reading before coding:** [AISnooker](https://github.com/AISnooker/AISnooker) (shot segmentation), [pooltool](https://github.com/ekiefl/pooltool) (settle physics), [cassapa](https://github.com/aporto/cassapa), [snooker-ball-tracker](https://github.com/Danthewaann/snooker-ball-tracker), [GoCardless tracker writeup](https://gocardless.com/blog/hacking-on-side-projects-the-pool-ball-tracker), [FusionCue](https://fusioncue.com/) (closest commercial analog). Nobody open-source robustly solves strike/settle event logic — that's the novel part of this project, and a good open-source hook.

## 7. Build order

1. **Camera bring-up** (`basler-camera-setup.md`) + a `record` CLI that dumps timestamped clips — then immediately record the §2.3 test corpus.
2. **Scaffold** the repo (§5): package, tooling, pre-commit, CI, ported clock state machine + server/API/overlay (pure logic, testable day 1 without the camera).
3. **Calibration tooling**: ChArUco intrinsics (both models, pick by RMS), homography, persisted to config.
4. **Detection core offline**: segmentation → tracker → events against the recorded corpus; accuracy scoreboard per labeled clip in pytest.
5. **Live integration**: grab thread + events → clock; run real practice sessions; every miss becomes a labeled clip.
6. **Display path** → OBS (`video-output-pipeline.md`).
7. Backlog by demand: replay export, break speed, acoustic click corroborator, stats, 16-ball identity.

## 8. Decisions (resolved with Steven, 2026-07-27)

- **Name:** `cueclock` — repo, src package, and CLI.
- **License:** MIT. Patent note: MIT (like Apache-2.0) cannot shield the project from *third-party* patents, and this space has active ones (US10226685 automated shot clock, US8992315 tip-contact detection — see §6). Practical read: enforcement against a personal, free, non-commercial project is essentially unheard of, and 20+ similar open-source billiards-vision projects sit on public GitHub untroubled. **Start the repo private** (Steven's stated comfort); flipping public later is one click, and nothing in the scaffold changes either way.
- **Acoustic click corroborator:** backlog. Ship per-ball tracking first; build it only if the corpus shows slow nudges still leaking through.
- **Test clips:** not published — local gitignored fixtures, tests skip when absent (§5).
