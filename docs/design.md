# Design — architecture, detection design, and engineering standards

The plan of record for cueclock's vision core, event logic, and repo standards.
Section numbers (§2.3, §4.2, …) are cited throughout the codebase — do not
renumber existing sections; append new ones. (This file began life as
`cv-app-rewrite.md`, the bridge document from the archived iPhone prototype;
older changelog entries reference it by that name.)

**Origin (2026-07-27):** the prototype codebase ([grassten/pool-room](https://github.com/grassten/pool-room)) was scrapped entirely — including the tests and the iPhone-era test videos. This document records what the old app did (so the new one re-implements the right behaviors), what the old app *learned* (so the new one doesn't re-discover known failure modes), and the research-backed design + engineering standards for the rewrite.

---

## 1. Why rewrite instead of adapt

The old `vision.py` plateaued at ~90–95% shot-detection accuracy, and nearly all of its complexity — the two-stage arm/confirm window, `max_jump_px` teleport guards, cue-axis consensus smoothing, occlusion grace, stillness bookkeeping — exists to fight one root cause: **single-blob HSV detection on a noisy, auto-everything iPhone feed**. The detector could only find *one* ball, couldn't trust any single frame, and had no notion of real-world position or speed. The Basler rig (locked exposure/WB, global shutter, 160 fps, fixed geometry, calibrated optics) removes those error classes at the source and makes a fundamentally better architecture possible: **track all 16 balls in true table coordinates and derive events from per-ball velocities**. That architecture isn't a patch to the old code; it replaces its core assumption. Every pixel-unit tuning constant would have needed recalibration for the new optics anyway.

The clock state machine, web/API surface, and audio listener were fine — but they're small, and rewriting them inside a clean package costs less than grafting old top-level modules into an open-source-quality repo.

## 2. Inventory of the existing app — what it does

Old architecture: one asyncio process; vision + audio worker threads feeding a `ClockState` state machine via callbacks; FastAPI serving two HTML surfaces; WebSocket broadcast at 10 Hz.

### 2.1 Behaviors to re-implement (the spec hiding in the old code)

| Old component | Behavior worth carrying forward (as spec, not code) |
|---|---|
| `state.py` `ClockState` | `IDLE → RUNNING → MOTION (shot in progress) → RUNNING (reset to full) → GRACE (2 s after 0) → EXPIRED (flash) → auto-reset for next player`. Extension allowed in RUNNING/GRACE only; adds `extension_seconds`; re-arms urgency cues; respects `extensions_per_frame`. Edge-triggered audio cues `ten`/`five`/`expired`, fired exactly once per countdown. Amended 2026-08-01 (checkpoint 26): **a settle in IDLE is a no-op** — IDLE means the operator said hold (New Rack, pause), and racking produces genuine settles that must not start the countdown; the untimed break still enters via ShotTaken (IDLE→MOTION). And **a settle with no ShotTaken since the previous settle counts the shot anyway** — balls moved and came to rest, so a missed strike (or a sub-impulse touch shot) no longer leaves the rack shot count short and replays the WNT 60 s window. Trade-offs accepted: a missed *break* now leaves the clock IDLE (visible; Restart recovers), and a phantom settle inside the first-shot window burns it (Push recovers). |
| `Ruleset` config | `ultimate_pool_8ball` (30 s base / 1×30 s extension / 2 s grace / 2 s post-expiry), `ultimate_pool_8ball_final10` (15 s variant), `ultimate_9ball`. Values carry over verbatim. |
| HTTP API | `POST /api/{start,pause,reset,extension}`, `POST /api/ruleset/<name>`, `GET /api/{state,rulesets}` — this is the **Stream Deck contract**; keep the URLs stable so the Stream Deck buttons keep working. Added 2026-07-29: `POST /api/dump?label=<x>` saves `cueclock run`'s in-RAM ring (last ~12 s of raw frames) as a labeled corpus clip — the "that was wrong / interesting" button (BACKLOG.md idea bank §8). Added 2026-07-31 (WNT match mode): `POST /api/rack` (per-rack reset: shot count + extension pool, IDLE at the first-shot time), `POST /api/restart` (fresh countdown, RUNNING immediately — missed-auto-reset recovery), `POST /api/push` (push out called: re-enter the WNT §5 60 s first-shot window; press again on a pass-back), `GET /api/score`, `POST /api/score/{p1\|p2}/{increment\|decrement}` (decrement floors at 0; unknown player/op → 404), `POST /api/score/reset` (0–0, names/race kept), `POST /api/score/config` (partial JSON body `{p1_name, p2_name, race_to}`; `race_to: null` clears), `POST /api/vision/{on,off}` (kill switch: while off, vision events don't move the clock; operator commands still work). |
| WebSocket `/ws` | Broadcast `{state, time, display, ruleset, extensions_used, cue}` at 10 Hz + snapshot on connect and on every event. Added 2026-07-31: the broadcast is the composed Hub payload — the clock snapshot plus `vision` (bool, kill-switch state) and `score` (`{p1: {name, score}, p2: {name, score}, race_to}`); `GET /api/state` returns the identical shape. |
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
- Publish the display feed to OBS (see `hardware.md` (video output part)).
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
- **`balls_settled` — per-ball velocities with hysteresis**, not global pixel motion: every tracked ball's filtered speed **< 0.03–0.05 m/s** (RoboCup SSL autoref uses 0.05–0.2 m/s for officiating-grade "stationary") **and** max any-ball displacement over the last ~500 ms < ~2–3 mm (catches pocket-edge creep), sustained for a dwell; re-enter "moving" only above ~0.1–0.15 m/s (Schmitt-trigger hysteresis so it can't chatter). Per-ball velocity is immune to the failure modes that plagued global frame-diff: hands in frame, player shadows, a ball spinning in place. **Rules caveat (verified 2026-07-27, Steven's catch):** spin-in-place immunity overshoots — a shot officially ends only when all balls have "stopped moving **and spinning**" ([WPA def. 8.2](https://www.poolbilliards.co/rules/wpa-rules-of-play/definitions/); International Rules 6k's "come to rest" implies the same), so `balls_settled` must additionally wait for visible spin to stop. **Implemented 2026-07-28 as the appearance-churn gate:** mean |Δgray| in a 16 px disk inside each stationary ball, EMA-smoothed by the tracker, blocks stillness above 2.0 gray levels. Measured on `spin_in_place` at the 78 Hz detect cadence: still-ball noise floor 1.2–1.5 (max 1.66), spinning cue 2.6–6.5 decaying to the floor exactly at visible spin stop (t≈7.0 s in that clip); the gate moves settle from 6.48 s (mid-spin) to after the stop, graded by a corpus timing test. Hands/shadow immunity is unaffected — the gate never looks outside the ball, and unobserved (coasting) balls are exempt.
- **`scratch`** — the cue-ball track terminates (not re-associated, not coasting) with last position near a pocket zone; latch during motion exactly as the old system learned. Pocket zones come from the homography, so this is finally geometric rather than "ball missing anywhere." **"Last position" means last *observed* position (corpus lesson, 2026-07-28):** a dying track's final coordinates are up to a coast-budget of constant-velocity extrapolation, and an occluded passing cue's phantom can coast into the zone and die there (`spin_in_place`: last real observation 359 mm from the pocket, coasted death 89 mm). A real sink needs no such generosity — its final observations come from the pocket jaw itself (`scratch_replace`: last observation 110 mm from the pocket center).
- **Optional corroborator (backlog):** a broadband transient ("click") detector on the existing audio stream, coincident within ~100 ms of the velocity impulse — patent-precedented (US10226685), nearly free since an audio thread already exists, and it cleanly separates the one genuinely ambiguous case (slow tip nudge vs. soft shot: no click vs. click). Fresh motivation 2026-07-31: `soft_touch_0731` is a real live shot whose cue peaks at **225 mm/s raw** — below any impulse floor that still rejects nudges, so it is undetectable optically by design; the clock's settle-counts-the-shot rule (§2.1 semantics, checkpoint 26) absorbs it, but only audio can name it a shot.
- **Occluded-address strike — the newborn-cue path (added 2026-08-01, checkpoint 26, from `occluded_address_0731`):** when the player's body/stick hides the addressed cue ball long enough, its track dies, the whiteness vote parks on some other bright ball, and the real strike surfaces as a **brand-new track born already at impulse speed** (measured: born at 1572 mm/s, 964 mm from the wrong flag-holder's rest — every rest-anchored gate correctly rejected it; that live session's break and post-push shot were both lost this way). A ball cannot teleport into existence at speed, so a fresh cue-vote-winning track born ≥ impulse floor opens a candidate anchored at its **birth spot**, provided the table had no genuine mover for a pre-strike dwell before the birth and none within kiss range of the birth spot. Confirm adds two measured carried-ball gates (`scratch_replace`'s fetch carry, born 2220 mm/s, is the false-positive twin): ≥ 90 % of confirm-window frames observed (a carried ball coasts through finger occlusions; a struck cue on open cloth is observed every frame) and adjacent-frame raw-speed ratio ≤ 2 (carry: 575 → 4140 mm/s between frames; struck cue: ±5 %). The path is **armed** once per rest cycle: disarmed when it fires and when a Scratch fires, re-armed by a cue-flagged ball at rest that has *translated since the disarm* — the post-scratch whiteness-vote fallback (a still ball that never moved) must not re-arm it. **No-cue variant (2026-08-01, from `no_cue_strike_0801` + `scratch_shot_0801`):** the tracker can lose the cue outright — the address occlusion kills its track, the regrab grace and whiteness floor keep the flag empty, a fast newborn's blur-corrupted frozen color sample can't win the vote back, and a dead cue's track **coasts as a flagged phantom for ~0.15 s after impact**, longer than the whole newborn window. So when no *observed* ball holds the cue flag, the scan runs over every observed ball and the birth conjunction alone carries the identity (on a quiet table, only the struck ball can materialize at impulse speed); the candidate then follows that track by id, not the flag. Founding tracks are excluded (first sight at engine start is not a birth), and candidate expiry is centralized so a stuck cue-follow candidate can't block the scan.
- **Ball roster — who may latch table motion (added 2026-08-01, checkpoint 26):** address gear (bridge-glove knuckles, watch, stick fragments) reaches 79–94 % observed — past the flicker filter — and a 30 mm gear drift used to latch `table_moving` and mint a phantom settle ≈ a phantom clock reset mid-address (three in one live session, video-verified). Structural discriminator measured across every clip: real latches come from tracks alive since the last quiet period; gear tracks are born mid-play already moving. Only **roster** balls may latch: membership founded at engine start, then admitted by union whenever the table has been continuously still for a settle dwell (union, not replacement — a roster ball occluded by the addressing player must not fall off mid-aim), pruned wholesale at each settle. Racked/placed balls join at the next quiet dwell; a gear fragment sweeping in cannot join mid-sweep because its own motion breaks the quiet streak. Carom-guard mover stamps inherit the roster filter, so gear can no longer starve strikes either. Known residuals, each with a recorded provoker: a planted glove that later *slides* ≥ 30 mm without lifting (needs `glove_address`-style recording to measure), the addressed cue's stick-merge centroid yank reaching 31 mm net (`merge_yank_0731`, `triage_0801b` — wants a detector-level merge flag, BACKLOG), and a hand picking up a roster ball mid-rack (`triage_0801c`).
- **Cue-rest slide guard (added 2026-08-01):** the rest memory keeps its original timestamp while following the flagged cue's coordinates; when the whiteness vote jumps to a different still ball, that used to teleport the rest spot with ancient dwell credit intact. A still-stamp landing > handoff radius from the current rest now restarts the rest (fresh dwell) — a genuinely creeping cue moves ~0.5 mm between stamps and never trips it.

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

1. **Camera bring-up** (`hardware.md` (camera part)) + a `record` CLI that dumps timestamped clips — then immediately record the §2.3 test corpus.
2. **Scaffold** the repo (§5): package, tooling, pre-commit, CI, ported clock state machine + server/API/overlay (pure logic, testable day 1 without the camera).
3. **Calibration tooling**: ChArUco intrinsics (both models, pick by RMS), homography, persisted to config.
4. **Detection core offline**: segmentation → tracker → events against the recorded corpus; accuracy scoreboard per labeled clip in pytest.
5. **Live integration**: grab thread + events → clock; run real practice sessions; every miss becomes a labeled clip.
6. **Display path** → OBS (`hardware.md` (video output part)).
7. Backlog by demand: replay export, break speed, acoustic click corroborator, stats, 16-ball identity.

## 8. Decisions (resolved with Steven, 2026-07-27)

- **Name:** `cueclock` — repo, src package, and CLI.
- **License:** MIT. Patent note: MIT (like Apache-2.0) cannot shield the project from *third-party* patents, and this space has active ones (US10226685 automated shot clock, US8992315 tip-contact detection — see §6). Practical read: enforcement against a personal, free, non-commercial project is essentially unheard of, and 20+ similar open-source billiards-vision projects sit on public GitHub untroubled. **Start the repo private** (Steven's stated comfort); flipping public later is one click, and nothing in the scaffold changes either way.
- **Acoustic click corroborator:** backlog. Ship per-ball tracking first; build it only if the corpus shows slow nudges still leaking through.
- **Test clips:** not published — local gitignored fixtures, tests skip when absent (§5).

## 9. Test-corpus recording checklist

One session at the table (~30–40 min). These clips are the ground truth every
detection change is graded against (§2.3) — the false-strike provokers matter
as much as the real shots. The original 17-scenario session was recorded
2026-07-27; the corpus has since grown TV-era scenarios (`empty_table_0729`,
`shot_debug`) and Flag-key live captures (`missed_strike_0731`,
`live_divergence_0731a/b`) — expectations live in
`tests/corpus/test_corpus_events.py`.

### Pre-flight

- [ ] Table light on (it's on/off only — on IS the canonical lighting state)
- [ ] **Live view / `run` closed** — only one program can hold the camera
- [ ] Full ball set, cue, chalk at the table
- [ ] T7 mounted (`tests/clips` symlink resolves); each 8–10 s clip is ~3–3.6 GB
      and the CLI refuses to start if space is short

### Per-clip flow (same every time)

1. Set the table up for the scenario.
2. Run (swap label + seconds from the scenario list):

   ```
   uv run python -m cueclock.cli record --label soft_shot --seconds 8
   ```

3. Start the action **~2 s after hitting Enter**; let the tail of the clip
   capture the balls settling.
4. When it prints `done:`, check **~156 fps** and **no WARNING** on that line.
5. Re-runs with the same label auto-suffix (`soft_shot_2`) — delete rejects
   from `tests/clips/` whenever. Ctrl-C stops early; the clip stays valid.

### Scenarios (the original session, all recorded)

References first: `empty_table` · 4 s (the segmentation reference — no balls,
nobody near) and `static_spread` · 6 s (detector noise floor).

Real shots: `soft_shot` 8 s (lag speed) · `medium_shot` 8 s (ordinary pot) ·
`power_shot` 10 s (hard, multiple cushions) · `break_shot` 12 s (full rack).

False-strike provokers (the old app's failure modes — act them out honestly):

- `ball_in_hand_place` · 8 s — pick the cue ball up, place it by hand, step away
- `ball_in_hand_nudge` · 8 s — place by hand, then fine-tune a few inches
  **with the shaft** (the legal way), cue away, step back. Dangerous because
  placement satisfies the stationary dwell and the shaft push adds an impulse +
  short coherent trajectory — only the impulse's low speed separates it from a shot
- `practice_strokes` · 8 s — address the ball, several practice strokes, never touching
- `shaft_adjust` · 8 s — accidentally graze the cue ball with the shaft while
  settling into aim (a foul in play, but it happens by accident and must not
  count as the shot — foul-calling stays human; v1 detects shots, not fouls)
- `tip_bump` · 8 s — bump the cue ball with the **tip** while feathering, so it
  rolls an inch or two. **The hardest case in the corpus**: dwell + small
  impulse along the cue line is nearly a real soft shot's signature (§4.2 calls
  this the one genuinely ambiguous pair; the backlog audio click corroborator
  exists for exactly this)
- `chalk_near` · 8 s — chalk the cue directly above the table surface
- `rack_balls` · 12 s — gather balls from the ball return and rack them (this
  table's stand-in for the drop-pocket "reach in and retrieve" provoker)

Scratch + settle behaviors:

- `scratch_replace` · 12 s — scratch, fetch the cue ball from the ball return,
  carry it back over the table, place it ready to play
- `walk_around_settle` · 10 s — shoot, then walk around the table leaning over
  it while balls are still settling
- `slow_creep` · 10 s — a shot ending with a ball barely creeping to a stop
  (bonus: dying right at a pocket edge)
- `spin_in_place` · 8 s — stun shot with heavy side spin so the cue ball sits
  spinning like a top as the spin dies. Ground truth per WPA def. 8.2: the shot
  is NOT over until the spin stops (§4.2 rules caveat)

Calibration insurance (unrecorded): `charuco_sweep` · 10 s — sweep a printed
ChArUco board through center + edges/corners at up to ~45° tilt, banking
lens-distortion data at the current focus (§4.3).

### After

- Spot-check one clip plays back (`ClipReader` frame count, or replay it to OBS
  via `uv run python -m cueclock.cli replay --clip tests/clips/<label>`).
- New failure captures come from the Stream Deck Flag key (`POST /api/dump`) —
  see `hardware.md` (Stream Deck part) for the triage flow.
