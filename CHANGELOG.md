# Changelog

The project's append-only history: dated **checkpoint entries** (one per
checkpoint commit — update this file in the same commit) and dated **build
decisions**. Not semver — cueclock is a continuously-evolving personal rig;
dates and checkpoint numbers are the versioning. A fresh session (human or
Claude) reads the newest entries to pick up without re-deriving anything.

Entries older than 2026-07-31 reference the original doc layout
(`cv-app-rewrite.md`, `basler-camera-setup.md`, `progress.md`, `todo.md`,
`accuracy-ideas.md`, …). Those files were consolidated on 2026-07-31 into
`docs/design.md`, `docs/hardware.md`, `BACKLOG.md`, and this file; the
originals are archived in `~/Documents/cueclock-docs-archive/`. Historical
entries keep their original wording — the § numbers of `cv-app-rewrite.md`
survive unchanged in `design.md`.

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

- **2026-08-01** Corpus battery parallelized with pytest-xdist: `uv run pytest
  -n 4` is the checkpoint-battery command (tests/README.md). Measured on the
  36-scenario corpus: serial 6:40 → `-n 4` 3:57 → `-n 8` 4:57 — the battery
  streams ~90 GB off the T7, four workers reach its sequential-read ceiling,
  and more workers only add contention (and spill onto the M2's efficiency
  cores), so `-n auto` (8) is explicitly NOT the number. Kept opt-in (no
  `addopts` default): unit-only runs finish in a second anyway, and worker
  startup would just slow them. Tests share no state (each builds its own
  pipeline), so parallel results are identical to serial. Deeper speedups
  would mean cutting I/O per run (e.g. zstd-compressed clips at ~2:1 with
  decode on read) — idea-bank material, not worth the ClipReader complexity
  while the battery is under 4 minutes.

- **2026-08-01** Stream Deck music/media controls dropped from the backlog —
  Steven controls music from his phone (the BT-amp caveat went with them).
  Voice "extension" monitoring parked until a wired USB mic is bought;
  match-day bring-up trimmed to the OBS scoreboard source + a dry-run rack,
  with extensions deck-key only.

- **2026-08-01** Empty-table reference refreshed to `empty_table_0801`
  (`hardware.md` §E2 steps 4–5): the 07-29 reference had drifted to 8.1
  (warn > 8, same-mount noise ~2); the fresh drift check reads ok. README
  launch command updated; `empty_table_0729` kept on the T7 for a
  drift-comparison later (idea bank §7D). `docs/cli.md` added to the plan
  of record — the user/agent-facing CLI reference (commands, flags,
  recipes); it must match `cli.py` in the same commit, like the Part C
  button map.

- **2026-08-01** Live sessions must run alone on the Mac mini — settled after a
  full evening's "accuracy crisis" turned out to be environmental. Two sessions
  reported "no shots detected"; both exactly coincided with corpus batteries
  running (subagent worktrees + a merge-gate run at `-n 4`), which starved the
  detect loop to 11–20 Hz (heartbeat `hz`; `flag_missed`'s ring: 1562
  detect-stride frames spanning 78.4 s instead of 20). Starved cadence makes a
  real break read 469/939 mm/s (corpus range 5845–7361), so the checkpoint-28
  IDLE floor eats it and the clock never starts — vision code exonerated (a
  same-night quiet-machine dump shows a flawless 12.8 ms cadence). Rule
  recorded in `docs/cli.md`; the checkpoint-36 `detect_heartbeat` proved it on
  its first day in service. Also resolved the same evening: the
  `cluster_shot_0801` open question from checkpoint 33 — Steven's report ("a
  jump shot, correctly handled") plus the OBS log closed it as the iPhone
  Continuity Camera side-angle dying mid-session (15:23:30 disconnect event,
  never reconnected; the Camo/USB migration replaces that source). The clip
  stays in the corpus as the only jump-shot scenario, graded green.

- **2026-08-02** UX doc-drift sweep — four interface facts reconciled with the
  rig as built, checked against the live OBS scene collection and
  `deck/layout.py` rather than from memory. (1) **The side angle is Camo**
  (USB, device "Camo Camera"), not Continuity Camera: the swap completed
  2026-08-01 but `hardware.md` §B3, `CLAUDE.md`, Part D's TCC note, and
  `spotter.py`'s device-pinning docstring all still named Continuity. (2)
  **OBS scene names are Stream Detail / Shot Clock / Canvas**, not the planned
  Table / Clock — §B3 documents what actually runs, with Replay still marked
  unbuilt. (3) **Canvas settled at 1080p crop** (1920×1080, judged on the TV);
  the letterbox-vs-crop taste call leaves the backlog. (4) **The flag keys
  flash ERROR, not NO T7** — checkpoint 37 moved dumps to the internal SSD, so
  an unmounted T7 is no longer the likely failure and the deck can't tell
  disk-full from server-down; the writer names the remedy in the journal.
  Backlog closures in the same sweep: match-day bring-up (scoreboard source is
  in both scenes, dry-run done), deck phase 4 (shipped in checkpoint 37), and
  the flag-dump starvation item — trimmed to its one unverified residual
  (whether a *local* dump dents detect Hz; one heartbeat read around the next
  live flag press closes it).

- **2026-08-02** Deck keys read as the **action**, not the state, wherever
  there is an action to take — Steven, on the Session key: "I'm holding the
  OFF button which counterintuitively turns the system on." At the table you
  read a button for what pressing it does. So idle is `START`, not `OFF`.
  Running keeps a state face (`ON`) because it has no tap action — a stray tap
  must never kill the camera mid-rack — plus a second line, `HOLD 1s`, that
  names the way out; the transient `STARTING…` / `STOPPING…` stay states
  because nothing is pressable then. `SessionState`'s enum values are now
  plain identifiers with faces in a separate map, so wording changes never
  touch the state machine.

- **2026-08-02** **`BACKLOG.md` is retired as a task list — open work moves to a
  GitHub project board** ([grassten/projects/3](https://github.com/users/grassten/projects/3)),
  cards backed by issues on `grassten/cueclock`. Steven wanted a visual board he can
  transition tasks on, categorisation, and phone access. The board wins on three
  repo-specific counts, not general preference: (1) **nothing runs on the Mac mini** —
  a self-hosted board (Planka/Vikunja) would have satisfied the LAN requirement but
  costs a Docker daemon on the production box, and live sessions must run alone
  (2026-08-01 post-mortem); (2) **work here is already commit-shaped**, so `Closes #N`
  in a checkpoint commit transitions the card and there is no second place to update;
  (3) it survives open-sourcing, which a Trello or self-hosted board does not.
  Known limitation, accepted: the GitHub *mobile app*'s Projects support is partial —
  full drag-between-columns is mobile web (github.com in Safari), which is fine.
  - **Categorisation is two orthogonal label axes, not one field**: `area:*`
    (accuracy · corpus · deck-ui · obs-display · hardware · docs-infra) and `needs:*`
    (table-time · purchase · code-only). A card is routinely both an accuracy item and
    a table-time item; folding them into one select loses a filter. Labels rather than
    project fields because labels work in the mobile app, the issue list, `gh`, and
    search — the board filters on them fine.
  - **Columns** add one beyond the default three: *Awaiting live validation*, which is
    the honest state of most of the old punch list — built and corpus-pinned, waiting
    on judgment at the table.
  - **The idea bank split off to `docs/accuracy-ideas.md`** (un-gitignored — the
    content was already published inside `BACKLOG.md`, so the "personal scratch notes"
    exclusion was stale). §1–§9 are research notes, not work; the existing house rule
    that ideas graduate when a live failure implicates them becomes "open an issue".
    Keeps the board ~20 real cards instead of ~60. **Noted while porting: there is no
    §5** — it was lost in the 2026-07-31 consolidation, and prose citing "idea bank §5"
    for the acoustic corroborator means the §4 bullet. Left unrenumbered per the
    never-renumber rule; the gap is documented in the file header.
  - 20 issues opened (#1–#20) carrying the old prose verbatim. Dropped rather than
    filed: the v1-scope-exclusions entry (a decision record, already `design.md` §5)
    and the completed VIDAA/Google-TV item (history lives here).

- **2026-08-20** **#46 minigames design agreed — the mockups are the record**
  (`docs/mockups/games-v1.html`; implementation handoff in `games-handoff.md`
  beside it). Reviewed same-day with no amendments. The calls the issue left
  open, now settled: Games scene = overhead feed full-bleed + the live bar's
  own plinth grammar, no side panel and no side-angle camera; one full-canvas
  `/games` source with two postures (opaque menu / transparent chrome);
  on-cloth markings are white (cue) / slate dashed (object) / red miss line
  only — accent never touches blue cloth, so verdict colour lives in the bar's
  answer cell (PASS fills accent, FAIL fills alarm, escalation by area);
  picker and game verbs on deck row 3 with capture as a transient overlay
  (Flag's mechanism), all wearing the MATCH stripe; PLAY and NEXT merged into
  one two-faced key (the Replay precedent — the one deviation from the issue's
  key list), RETRY separate and dark until served because same-shot and
  next-shot must never be one mispress apart; ghosts drop at arm and the zone
  stays lit; lag scores every settle unarmed; the phone gains a Drills tab
  that edits zones on the 8×4 grid but never moves balls.

## Checkpoint log (append-only, newest first)

- **2026-10-17 (checkpoint 106 — detection runs at the camera's full 156 Hz):**
  Steven has asked for this repeatedly; the honest record is that "more frames
  won't help" was never measured, only argued. 53 of the 73 corpus clips are
  ring dumps, which stored only the stride-2 frames, so every live-failure clip
  was 78 fps and the corpus could not answer the question. Checkpoint 104's own
  note already named frame rate as a lever for identity, the dominant miss
  class. Shipped live for Steven to judge by feel over sessions, per his
  standing preference.
  - **Dose-response first.** The same corpus replayed at 39 Hz (stride 2 over
    the dumps, 4 over the full-rate clips): 91/91 → 79/91 passing, strokes
    fired 53/54 → 45/54 (47/54 with frame counts converted to time). The clock
    ran through real shots (`no_cue_strike_0801`, `tip_bump`), a phantom count
    appeared (`soft_shot`), a scratch never reached the adjudicator
    (`pot_scratch_nohold_0811`), and break speed under-read (1731 vs ≥2500
    mm/s). Part of that is constants tuned at 78 Hz, but the break-speed
    under-read is sampling, not tuning. Halving the rate costs accuracy, so
    there was no measured reason to assume doubling it is free of gains.
  - **Constants convert, not retune.** `events.TUNED_HZ` (78.125) names the
    cadence every frame-denominated constant was measured at.
    `EventConfig.at_cadence` / `TrackConfig.at_cadence` re-express
    `birth_frames` (3 → 6), `cue_id_debounce_frames` (2 → 4) and the
    per-frame gains (alpha, beta, whiteness/churn EMA) as the same span of
    time; `VisionPipeline(cadence_hz=)` applies them. At `TUNED_HZ` both
    return the config unchanged.
  - **The impulse signals keep their tuned step.** A one-frame raw speed at
    156 Hz divides the same centroid jitter by half the dt: on
    `static_spread`, still balls' p50 went 18 → 36 and p99 581 → 1156 mm/s,
    across `strike_impulse_mms` (110) and `parked_fast_confirm_mms` (1200). A
    naive stride flip would have added phantom strikes and been judged as
    "full rate is worse". Raw speed now spans the newest observation at least
    `raw_baseline_s` (0.75 tuned intervals) old, which is exactly the previous
    one at 78 Hz and two back at 156. Churn differences against the gray one
    tuned interval back (`churn_lag`), so `spin_churn_max` reads the same
    rotation.
  - **Throughput: 3 detect workers.** One thread measured 152 frames/s on a
    quiet table, and a break's busy frames cost 15–40 ms; the live loop
    already sagged to a median of 75 Hz (p10 54–68) at stride 2.
    `VisionPipeline.find_blobs` is stateless and runs on a `ThreadPoolExecutor`
    (OpenCV drops the GIL): ~420 frames/s sustained on `power_shot`,
    `break_shot` and `rack_balls` replayed through the live loop's own shape.
    Churn, tracker, events and hub stay on the loop thread in frame order;
    `DETECT_INFLIGHT` (6) bounds the queue. Two free savings came with it:
    the gray was converted twice per frame, and the table-edge erode (a
    constant) was recomputed every frame.
  - **Ring holds references.** pypylon's `grab.Array` is already a copy, so
    `FrameRing` stores the arrays it is given, and `snapshot` is a list copy
    rather than a pixel copy. At 156 Hz a 20 s ring is 7.2 GB, and the old
    copy-under-lock would have frozen the grab loop for seconds on every Flag.
    Dumps record `cadence_hz` in meta.json. `ClipReader.cadence_hz` infers it
    for older clips (>120 fps measured ⇒ full rate).
  - **Corpus replays at each clip's own cadence** (`detect_indices()` now
    returns every stored frame), so the 20 full-rate clips are graded at 156 Hz
    for the first time. `analyze --stride` defaults to 1, and `run --stride`
    to 1 (`2` reproduces the old cadence). The detect-cache key carries
    `churn_lag`.
  - **Gate:** cold battery **675 passed** in 5:26, 0 failed; the single strike
    miss is the pre-existing `stroke_tug_strike_0801`. Goldens regenerated
    for the 7 full-rate clips whose timelines moved: `medium_shot` /
    `slow_creep` / `walk_around_settle` break speed read 1–8% higher (peak
    sampled better); `tip_bump` fires 70 ms earlier; `shaft_adjust`'s
    reverted pseudo-shot fires 90 ms later; `scratch_replace` **loses** a
    spurious pre-shot settle at 2.10 s. `soft_shot`'s settle lands 1.0 s
    later (5.95 → 6.95 s) — **changed, not judged**: nobody has watched the
    clip, and 78 Hz settling a second early is as plausible as 156 settling
    late (Steven's call: footage decides, not assertion). `stroke_tug_strike_0801` and `parked_vote_strike_0801`
    drifted before this change and were left untouched. 584 unit tests (7 new).
  - **Measuring it:** no shadow run. Compare `session-stats` from the next
    sessions against the last 5 at 78 Hz: 96.06% strike detection, 98.69%
    clock accuracy incl. self-healing, 97.38% hands-off, 5 false fouls in 381
    shots (that last one was already a regression worth chasing).

- **2026-09-01 (checkpoint 105 — the rig comes back from a power cycle):** a
  power cycle broke four things at once and every one of them failed *quietly*.
  Nothing here is a detection change; the corpus was not run (unit tests are
  the gate for deck/TV work).
  - **The camera was not on the USB bus.** `run` died at `open_camera()` in
    under a second, and the Session key flashed `STARTING…` and dropped back to
    `START` — the reason only ever reaching `~/Library/Logs/cueclock-run.log`,
    behind the TV. Pressed again, same. The cable was reseated; no software
    fixes an absent device. What *is* fixed is the silence: the key now
    **preflights** the bus with pylon (0.6 s, in a subprocess so the always-on
    daemon never holds the pylon runtime) and refuses to spawn a `run` that
    cannot work, and any unrequested exit lands on a **named, sticky, red**
    face — `NO CAMERA` / `CHECK USB` or `START FAILED` / `SEE LOG` — that
    survives until a press retries it. The failure yields to a server that is
    actually answering, so the key can never lie about a terminal `run`.
  - **A failed start orphaned OBS.** The first attempt got far enough to launch
    OBS, then `run` died; OBS was left running and sat through the TV's HDMI
    renegotiation. Half an hour later the real session inherited it, and every
    projector it opened had no renderer behind it — thirteen
    `obs_display_init: Failed to create swap chain` in the OBS log, a grey wall
    that only a manual OBS restart cleared. A start that dies now takes OBS
    down with it.
  - **The TV had moved.** DHCP put it at `192.168.4.29`, `config/tv.json` still
    said `.27`, and from a closed port that is indistinguishable from standby:
    `ensure_on` fired nineteen magic packets at an awake TV and never
    confirmed (`setup incomplete — TV`), then the held stop's `power_off` timed
    out and left the wall lit. The MAC does not move, so `tv.resolve()` asks
    the ARP cache who owns it — the same three candidates `wol_targets`
    already covers, because the TV answers the remote protocol on one NIC and
    appears in ARP on the other — and repairs the address in place. A repair,
    not discovery: a TV in standby has no ARP entry, which is fine, because the
    magic packet is a broadcast. A DHCP reservation would fix it at source and
    is still worth doing.
  - **iCloud had evicted `config/` and `models/`.** All 22 files were
    *dataless*, and a launchd daemon cannot materialise those — it gets
    `EDEADLK`, "Resource deadlock avoided", which is what the deck log's
    `no usable tv.json` / `no usable obs_websocket.json` actually were. That
    alone took out TV control, the Syphon rebind, the TV projector and the Vosk
    model. Everything was downloaded back (405 MB), then both directories moved
    to `config.nosync/` / `models.nosync/` with symlinks — the same escape
    hatch `.venv` has used since checkpoint 13 — and both names added to
    `.gitignore`.

- **2026-08-29 (checkpoint 104 — one refusal deleted, one attempted fix
  withdrawn):** three flagged clips triaged with ground truth from Steven.
  `hand_placement` (#41) is **deleted** for the second time, on live evidence.
  A repair to `phantom_settle` (#37) was written, measured against the corpus,
  **falsified, and reverted** — the entry below records the attempt because
  the negative result is the most useful thing in this checkpoint. Net: −90
  lines of `src/`, `events.py` untouched, and **no new constant anywhere**.

  **The aggregate came first.** Across all 43 sessions (1991 shots, 3.16% miss,
  1.96% phantom), the two settle-time refusals have fired **16 times ever** —
  `phantom_settle` 6, `hand_placement` 10 — so between them they can account
  for up to a quarter of the standing miss rate.

  **`flag_missed_2` (2026-08-23) — #41 deleted.** Steven took a real shot and
  **never touched a ball**; it read `arrivals 1 / hand 1` because the object
  ball was potted (vanished, never arrived) and he was still leaning over the
  cue where it stopped. That is not an edge case, it is the shape of *a pot* —
  and **9 of the 10 fires this predicate ever produced read `arrivals == 1`**,
  the shape in which its safety argument ("**every** arrival, not any") has no
  conjunction left to make. Every discriminator was measured before deciding,
  and the populations are identical in all of them: arrivals, hand-coverage,
  and episode mover travel — the labelled 2026-08-11 carries read **30, 140,
  159 and 238 mm** against this real pot's **252 mm**. Out with the verdict go
  `_hand_delivered`, `SettleFrame`, the retained settle window,
  `ARRIVAL_CLEAR_MM`, `ARRIVAL_HAND_WINDOW_S`, `SETTLE_WINDOW_MAX_S`,
  `_intrusion_boxes_mm` and the `arrivals`/`hand_arrivals` fields.
  **Ball-in-hand resets the clock again** — Steven's call this session with the
  trade-off measured in front of him. Two removals (#38, this) now agree the
  verdict needs evidence the rig does not produce; the next attempt must bring
  a new *measurement*, not a third arrangement of this one.
  `test_0811_carries_do_not_reset_the_clock` becomes
  `test_0811_carries_still_count_the_real_stroke`, so the clips stay graded on
  what remains true and stay available as labelled ground truth.

  **`flag_missed` (2026-08-20) — diagnosed, and left unfixed on purpose.**
  Steven potted a ball with a cue the tracker had never held before the strike,
  so the cue was not in the quiet baseline; the potted ball's track died, and
  the cue rolled to rest **23.1 mm** from where that ball had stood. With
  `moving_enter_mm` at 30 mm the identity-blind greedy match paired the *cue*
  with the *potted ball's* position and called it unmoved: 7 baseline balls,
  7 settled balls, `involved 0`, real shot revoked, countdown run from 14.25 s
  into GRACE. The hub journaled `mover_max_mm: 489` in that same record.

  **The attempt, and why it failed — read this before trying it again.** The
  fix looked clean: a mover that travelled past the diff's own movement floor
  refutes "the table is unchanged", reusing `moving_enter_mm` rather than
  inventing a constant. It fixed the clip, passed 657 corpus tests, and was
  **wrong**. `parked_vote_strike_0801` — the graded phantom this predicate was
  built for — carries a mover that travelled **122 mm**. Measured together the
  populations interleave: real shot 489 mm (`flag_missed`), phantom 364 mm
  (`flag_phantom`), phantom 122 mm (`parked_vote_strike_0801`), phantom 26 mm
  (the live 2026-08-11 one). Any line separating 489 from 122 is fitted to one
  clip. Reverted whole, and the reasoning left in the docstring so the next
  reader does not re-derive it. Two things this cost nothing to learn: the
  earlier read that "5 of 6 `phantom_settle` fires were wrong because their
  mover travelled" **does not survive** — mover travel is not evidence of a
  shot, so those five are unjudged, and only `flag_missed` is confirmed wrong.

  **`flag_phantom` (2026-08-25) — mechanism recorded, no fix.** Steven leaned
  over the table in a white tank top. At 17.698 s a track is born mid-table at
  **1220 mm/s** — the garment — while his torso occludes ball #4, whose track
  dies at 17.544 s. The settle then reads `involved 1` purely from that
  occlusion-vanish, so #37 (which needs `involved == 0`) cannot reach it, and
  #33's compensation counts a shot and resets 25.3 → 30 s. Both candidate
  fixes were measured and both fail: *"a ball-shaped blob inside a hand-scale
  intrusion is not a ball"* fires on **3.09%** of detections in
  `occluded_address_0731`, a legitimate address, against **0.62%** here — it
  would trade a phantom for blindness at the address, the #1 upstream cause of
  misses; and *"a vanish away from a pocket is not a pot"* dies because a track
  ends where the tracker loses it, not at the pocket — `flag_missed`'s
  **genuinely potted** ball vanished 490 mm from the nearest pocket against
  this occlusion's 469 mm.

  **The cause under two of these three, stated plainly because it will be
  back:** `_match_rearrange` is identity-blind by construction — `present`
  drops `ball_id`, so a ball coming to rest within `moving_enter_mm` of where a
  *different* ball stood is indistinguishable from nothing having happened. An
  identity-aware match is the real repair. It reaches break adjudication and
  the settle backstop, and every cheap approximation of it tried here inflates
  `involved` exactly when a person is leaning over the table — which is when
  phantoms happen. It wants its own measured pass, not a clause bolted to a
  predicate.

  **On frame rate** (Steven asked): the camera already free-runs at 156.25 fps
  and `run --stride 2` samples it at 78 Hz. None of these three failures is
  temporal — a matcher coincidence, a body over an arrival, and a garment
  segmented as a ball all reproduce identically at any rate. Raising it is a
  real lever for the *identity* problem (a 3000 mm/s ball steps 38 mm per frame
  at 78 Hz against 19 mm at 156 Hz, on a 57 mm ball), but not for detection:
  the 6 ms exposure streaks that ball 18 mm regardless. It is also not free —
  the live loop already sags to 62–67 Hz under load in these very sessions, so
  stride 1 needs the CPU budget solved first. Worth its own pass, alongside the
  identity-aware match it would serve.

  **Clip dispositions.** Staging drained, all three on the T7. One promoted:
  `pot_single_arrival_0823` (1 shot / 0 scratches / 1 settle, clock-graded) —
  the negative control the corpus never had. `CARRY_CLIPS` grades hand carries;
  nothing graded *a pot whose only arrival is hand-covered*, and that is the
  population both #38 and #41 selected wrongly, which is precisely why the
  verdict shipped twice. Two kept as evidence, deliberately ungraded (no
  `EXPECTED` entry, so they cost no battery time): `cue_substitutes_potted_0820`
  and `tank_top_phantom_0825`. Neither is promotable — pinning either would be
  pinning a known failure, which is a holding pen, not a test — and both are
  the labelled inputs the identity-aware-match pass will need.

  **Prediction, to be checked next session (§10 discipline).** Baseline
  `report --all` 3.16% miss / 1.96% phantom over 1991 shots; `--since 7d`
  2.45% over 571. Only #41 was removed, so the miss rate should fall by roughly
  **0.5 points** — toward ~2.6–2.7% — and it takes ~600 shots (≈5 sessions) to
  see that against the noise at this volume. The phantom rate should **not**
  move; if it does, something else did. Ball-in-hand carries will now reset the
  clock — expected, not a regression. Report the number back either way.

- **2026-08-21 (checkpoint 103 — the minigames come out):** #46 is reverted in
  full. Checkpoints 97–102 built a platform, a lag trainer, a drill core, a shot
  catalog and a phone-side editor; Steven played it and did not want any of it.
  No defect drove this — the feature worked as specified and the specification
  was the thing that turned out to be wrong, which is the cheapest kind of wrong
  to find and the only kind you find by playing.

  **The tree returns to exactly what it was after checkpoint 96.** Out: the
  server's games layer and its hub wiring, the deck's games row and picker,
  `display/marks.py` and the pipeline hook that drew target zones onto the cloth,
  the `/games` page and its OBS scene (plus `scripts/setup_obs_games.py`), the
  Drills tab on `/control`, `report.py`'s `game_stats`, the games clock mode, and
  the mockups. The series was almost purely additive — 6,371 lines in against 24
  lines of pre-existing code touched — so the revert is a tree restore rather
  than a surgical extraction, and the diff against checkpoint 96 is empty.

  **Nothing in `vision/` was ever part of #46**, so no detection constant, no
  threshold and no corpus expectation moves here. The unit battery is the whole
  gate; the cold corpus run has nothing to say about this commit.

  Two things deliberately survive. `design.md` **§11 stays as a tombstone** —
  this log's 97–102 entries cite `§11.x` subsections, and the rule that § numbers
  are never renumbered or recycled outranks the tidiness of deleting a heading;
  the next section appends as §12. And **97–102 stay in this log**, because an
  append-only history that quietly loses the six checkpoints it spent a day on is
  no longer a history. What was built is in git; what happened is here.

  Left on disk: `config/games/shots/*.json`, the two shots captured on the rig.
  Gitignored, unread by anything now, harmless — deleted whenever Steven says so.

- **2026-08-20 (checkpoint 102 — the Drills editor says it is an editor):**
  "How do I modify the drill? There's nothing clickable" (Steven). The editor
  was reachable the whole time — tapping a list row opens it — but nothing said
  so: a drill row was a stat card and a tappable row wearing the same
  rectangle. It now carries a **disclosure chevron**, which is the one thing a
  row needs to read as a way in.

  Inside it, the same fault a level down. Tapping a grid cell moves *the
  selected ball's* target, and the only sign of which ball that was, was the
  label turning blue — indistinguishable from a heading colour. The role labels
  are now **pills, with the selected one filled**, so the editor reads as two
  controls and a grid rather than two captions and a picture.

  And the reason a fix could have been on disk while the phone kept painting
  the old one: **every UI page is now served `no-cache, must-revalidate`.**
  These pages ship with the code and change with it, and both of their
  audiences hold one open for hours — a phone in a pocket, an OBS browser
  source that only reloads at a scene cut. The ETag makes the revalidation a
  304, so on a LAN it costs one conditional request per load. (Found the
  honest way: two re-renders of the "fixed" page showed the unfixed page,
  because WKWebView had cached it too.)

- **2026-08-20 (checkpoint 101 — a pot is a target):** the first live shots
  session found the judge answering the wrong question. Miss the pot, leave the
  cue dead in the middle of its zone, and the wall said **PASS** — because the
  only requirement the shot carried was the cue's leave. A leave from a shot
  that never went in is not a shot you made.

  Not a special case: **the missing half of the model.** A requirement is
  `(role → target)`, and a target is a block of diamond cells **or `POTTED`**.
  That is exactly the addition §11.5's typed envelope was put in for —
  `{"type": "potted"}` beside `{"type": "cells"}`, one new type, no migration,
  unknown ones still raising. The envelope earned its keep the same day.

  **The evidence is absence, and it was already trusted.** Nothing in `vision/`
  reports a pot — scratch is cue-specific by construction, its whiteness census
  existing precisely to *refuse* a scratch when a potted object ball wore the
  crown — so a ball in a pocket is a ball missing from the resting roster. The
  judge already read that fact in the other direction, to fail a safety whose
  object ball dropped. Asking for it on purpose leans on nothing new.

  **The per-shot setting needs no key**, because capture already has the
  gesture: place a ball at a spot and it must *finish there* (a safety); leave
  it where it stands and it must *drop*. A potting drill is captured by picking
  up only the cue, which is what capturing one already looked like. The old
  default for an untouched object ball — no requirement at all — was the bug,
  and it is no longer a state a `Shot` can be in: one function fills the list
  from the layout and both the capture path and the file reader go through it,
  so **the shot already saved on the rig judges the way a new one does**.

  Two things fall out. A **safety is derived, not declared** — it is what giving
  an object ball a patch of cloth to finish on *means*, so the flag cannot
  disagree with what the shot is judged against. And **the cue is never given a
  pot**: a potted cue is a scratch, a way to fail a shot and never a way to set
  one, so the phone offers POT on object rows only.

  Surfaces: the bar reads `CUE IN ZONE · OBJECT POTTED` armed and `✓ POTTED` /
  `— NOT POTTED` at the verdict, no millimetres either way. The cloth draws an
  **alarm ring around the ball that did not drop** — no direction to point, but
  on a two-object layout very much a *which*. The phone's Drills editor gets one
  control per ball, `Pot · 1×1 · 2×1 · 1×2 · 2×2`, whose lit button is the
  setting; tapping a cell for a ball set to POT makes it a zone there, because
  the tap said *where*, which is the whole difference between the two. Its
  mini-table now plots balls against the playing surface too (checkpoint 100),
  so the dots and the cells finally agree.

  Verified by rendering both surfaces — the editor in a real WebKit view
  against a live server, both postures; the failed-pot ring over a corpus frame.
  Unit-only otherwise: `vision/` is untouched.

- **2026-08-20 (checkpoint 100 — the games wall, corrected on the wall):** two
  faults Steven spotted the first time #46's picture was up on the TV, both of
  them things no unit test could have known to ask.

  **The diamond grid was drawn on the cushions.** Its outer lines ran along the
  rails and every cell was a rail's width too big, because the games layer had
  taken the calibrated rectangle for the playing area. It is not, and
  `vision/calibration.py` has always said so in its own docstring: the fit is to
  the **cloth outline**, and cloth runs up the cushion face to the rail. Nothing
  had ever cared, because every consumer until now works in differences or in
  zones whose thresholds were measured against that same convention.

  **The rail sights are the ruler**, and they are in every frame ever recorded:
  eighths of the playing length, quarters of its width — exactly the grid being
  drawn. Rectify a corpus frame with the live calibration, find the sight
  centres, least-squares fit `coord(k) = A + B·k`, and the playing rectangle
  falls out in calibrated mm. No printed target, no table time, and re-measurable
  from the corpus after any recalibration. Over four clips across three dates
  (`rack_place_0801`, `stroke_gear_margin_0802`, `ball_in_hand_carry_0811` +
  `_b_`) the sights sit within **1.2 mm** of a straight line and the spans agree
  to ~2 mm: **1830.3 × 872.8 mm**, centred, ~50 mm of cushion at each end and
  ~46 mm at each side. Grid, cells and zones now quote that rectangle, and lag
  measures its gap from the cushion nose — it had been reporting every lag
  ~50 mm long.

  **That measurement also puts a number on the calibration's own error.** A
  physical 1930 × 965 reading as 1830.3 × 872.8 means the calibrated millimetre
  is 0.948 along the table and 0.904 across it — both short, and **4.9% apart
  from each other**. §4.2's thresholds were all measured in these units so
  nothing is wrong today and nothing should be rescaled, but that is the size of
  the prize for **#16**, measured for the first time (design.md §11.6).

  **The HUD bar was sitting on the bottom rail.** The feed is 1.84:1 in a 1.78:1
  canvas, so full-bleed put the opaque plinth across the bottom rail and both
  foot pockets — on a game judged by where balls stop, the wrong 12% to lose.
  The Games scene now fits the feed into the strip *above* the bar and lets it
  pillarbox: ~12% smaller, and all of it visible. The transform belongs to the
  scene item, so the play scene keeps its own full-bleed framing, and
  `deck/games.py` owns the bar height with a unit test holding it to the
  stylesheet.

  Verified by rendering: the grid over real corpus frames (the lines land on the
  sights) and the scene over the live wall. Unit-only otherwise — `vision/`
  is untouched, so the corpus has nothing to say about any of it.

- **2026-08-20 (checkpoint 99 — minigames phase 3: the catalog, safeties, and
  a Drills tab):** #46's last of three, and the card closes.

  **Shots persist as one JSON per shot, definitions only** (`config/games/shots/`)
  — the bracket's pattern at the scale it deserves: a directory of small files,
  no index to maintain, deleting a shot is deleting a file, a torn one skipped
  rather than taking the catalog down with it. **Attempts, makes and last
  played are deliberately not in the file**; they come from the journal
  (`report.shot_records`), which is what makes the phone's make % and `cueclock
  report` agree by construction, keeps a rename from losing a shot's history,
  and lets the record be recomputed over every session ever recorded.

  A zone's typed envelope now does the job it was added for: **an unknown type
  raises rather than degrading.** Silently reading a future circle zone as a
  cell block would judge the shot against a target nobody set.

  **PLAY serves uniform random**, excluding whatever is currently served so two
  presses never land on the same shot and read as a stuck key. Weighting toward
  the shots you miss stays out of scope until the catalog is big enough for the
  weights to mean anything — the journal already records everything weighting
  would need, so waiting costs nothing.

  **Safeties came out of the mechanism rather than needing one.** Attribution
  by vacated ghost is role-general, and because a ball that has already been
  placed is accounted for against the target it set, picking up a second one
  leaves exactly one vacated ghost and one unaccounted ball — so the second
  requirement is captured the same way the first is, with nothing new to learn
  and no new code path. A scratch fails the cue's zone and an object ball
  potted during a safety attempt fails its own, both because they are not in
  the zone they were asked to be in. No pocket attribution anywhere.

  **A third phone tab: Play · Drills · Setup.** The list is the weakness report
  (name, ball count, last played, make % over attempts); the editor renames,
  re-sizes and re-places zones on the same 8×4 grid the TV and the table speak,
  serves one, deletes. It **never** edits ball or ghost positions — the table
  is a millimetre-accurate input device and a map you can drag balls around on
  is a second, worse one that would disagree with what the camera measured.
  "Serve this shot" is the one live control and it refuses rather than
  switching the mode for you, on the same rule the picker POSTs follow.
  The tab refetches while it is the one on screen, because a shot captured at
  the table is the one change nothing on the WebSocket announces.

  Both surfaces rendered for real before shipping, list and editor.

- **2026-08-20 (checkpoint 98 — minigames phase 2: the drill core):** #46's
  second of three. The shot catalog's whole loop — capture, ghosts, auto-arm,
  the grid judge, PASS/FAIL — on the platform checkpoint 97 built.

  **Capture is by placing, not configuring.** Set the shot up, press CAPTURE to
  snapshot the settled layout (the crown names the cue), then **pick a ball up
  and put it where the shot should leave it**; the cell it lands in becomes
  that ball's target and the ZONE key sizes the block around it. The system
  attributes each placement by **which ghost was vacated**, so there is nothing
  to tell it — and because a ball that has already been placed is accounted for
  against the target it set, picking a second one up for a safety still leaves
  exactly one vacated ghost and one unaccounted ball. Anything more ambiguous
  than that is left alone rather than guessed at. Re-placing in the *same* cell
  deliberately does not re-anchor, which is what lets a ZONE resize survive the
  hand still hovering over the ball.

  Placement rather than demonstration is the point: these are shots being
  practised *because* they don't come off, so a demonstration would rarely land
  in its own target.

  **The shot arms itself.** Ghosts up, per-ball offsets counting down in the
  bar and collapsing to a quiet tick inside tolerance; when every ball is
  placed and the table is still, it arms — no press, because the press would be
  one more thing to remember while bent over a layout you are rebuilding.
  "Still" is the vision layer's identity-free motion bit, so a hand still
  lowering the last ball keeps the shot unarmed. At the arm the **ghosts drop**
  (the balls are standing on them, so from there they are only noise under the
  shot) and the **zone stays lit** the whole way, because it is the thing being
  aimed at.

  **Zones are diamond-grid cells and the criteria is a list.** Point-in-
  rectangle in table millimetres, one entry per (role → zone) — the list schema
  in on day one even though most shots carry one entry, because retrofitting a
  list under a hardcoded cue judge later is pointless churn. A zone grows
  *toward the middle of the table* when the ZONE key sizes it: growing outward
  from a rail target would push half the block off the cloth for nothing.
  Roles at settle use #46's three tiers — crown, then elimination, then
  lineage — so a mid-shot label swap cannot change the cue's verdict, which
  a unit test pins by crowning a ball carrying the wrong track id.

  **Both new constants are asserted from `EventConfig`, not tuned.** Placement
  tolerance 15 mm: 3x `settle_creep_mm` (below it a resting ball's own wobble
  flips the verdict) and half `moving_enter_mm` (at or above it, a ball the
  event layer would call *moved* still reads as placed). Cell edge margin
  5.0 mm, straight off `settle_creep_mm`, applied **outward** so the
  chalk-line dispute fails toward the player.

  **Two visual defects the real render caught**, both the same shape — a mark
  drawn underneath the ball it describes. Ghost rings were 30 mm against a
  28.575 mm ball, so a ball arriving on its ghost hid the ring exactly when it
  mattered (now 38 mm). And the miss line ran from the ball's *centre*, so a
  miss of a ball radius or less was entirely invisible (now from the ball's
  edge, and under a radius it is not drawn at all — the bar's millimetres say
  it instead).

  **One caption is not the mockup's.** A miss reads `ONE CELL OUT`, not
  `ONE CELL SHORT`: short and long are relative to the path the cue was meant
  to take, which nothing here knows. The direction is on the cloth instead,
  drawn exactly, as the miss line from the ball to the edge it missed — so the
  caption says how big and the picture says which way, and neither guesses.

- **2026-08-20 (checkpoint 97 — minigames phase 1: the platform, and lag as its
  proof):** #46's first of three. A **games** session mode, fourth on the mode
  axis, hosting a registry of on-table minigames — and the deliberately minimal
  lag trainer, whose whole job is to prove the contract is right before the
  shot catalog leans on it (the rule of two: the contract is frozen to exactly
  what these two games need, nothing speculative). `docs/design.md` §11 is the
  contract's home; `docs/mockups/games-v1.html` remains the design of record for
  the surfaces.

  **A game is a registered event consumer**, same pattern as `RULESETS`. It
  receives the event stream plus the resting layout and the cue crown, and
  provides deck verbs, HUD bar state, on-cloth marks and journal kinds
  (`server/games.py`). **The vision core is untouched by construction** — games
  sit at the Hub seam the clock and `last_settle_movers` already use, so the
  corpus stays green without anything in `vision/` moving, and this checkpoint
  gates on unit tests rather than a cold battery. Games are fed after the kill
  switch and before the mode gate: a vision the operator has disowned must not
  drive a game either, but a mode gate that exists to keep events off the
  *clock* has no business keeping them off a game.

  **Everything on the cloth is drawn server-side into the display frame**
  (`display/marks.py` as the language, `display/pipeline.py` as the only
  rasterizer). The pipeline already owns the table image and the mm→display
  mapping, so orientation is right for free and OBS stays dumb; the rejected
  alternative — a browser overlay aligned on top of the Syphon feed — needs an
  exported mapping that has to survive every recalibration. The cost is stated
  rather than hidden: **no `--rectify`, no markings**, and `run` says so at
  startup instead of drawing something approximately right.

  **The picker is deck row 3** — the row that is the mode's own block in every
  other mode, and whose declared keys are all gated to `match`/`run out`, so
  nothing collides. It is the one part of the deck the *server* describes
  rather than `layout.py` declaring, because what is on it depends on which
  games are registered and which is active. All of it wears the MATCH stripe:
  every games press is a between-shots press, so there is no new colour.

  **One Games scene, one HUD source.** `/games` is a single browser source with
  two postures — opaque menu of cards while the picker is up, chrome-only bar
  once a game owns the table — over the play scene's own Syphon input added
  full-bleed. Adding a future game touches zero OBS configuration
  (`scripts/setup_obs_games.py`). The scene follows the Mode key with no dwell
  and no scene key: games mode is a thing you are *in*, and the mode is the
  choice. Rendered for real in both postures before shipping (checkpoint 92/96
  rule) — which is how the menu's never-played card was caught printing `—MM`,
  a unit attached to nothing.

  **Lag scores every settle, and arms nothing.** Only the best attempt matters,
  so a bad one is free and a retrieval push you did not mean is a number you
  ignore rather than a state you have to clear. It measures the **gap** — the
  ball's near edge to the head rail, not its centre — because that is what a
  player measures with a piece of chalk, and a ball frozen on the cushion
  should read ~0 rather than one ball radius. Which ball: the crown (identity
  tier 1), falling back to elimination on a one-ball table, refusing anything
  else. `lag_attempt` journal lines; `report.game_stats` derives the cards.

  **One deviation from the mockups, and it is the issue's own rule winning:**
  the bar's per-ball lockups read `CUE` / `OBJECT` / `BALL n`, not `RED` /
  `BLACK`. Colour names are an identity claim, and #46 puts 16-ball identity
  out of scope on every surface — the three tiers it does specify (crown,
  elimination, lineage) can name a role but never a colour.

- **2026-08-13 (checkpoint 96 — the UX pass: two tabs, freestyle, unlimited
  extensions, a clean morning):** Six items from Steven in one sweep, all
  surface and policy, no detection.

  **The control page is two tabs — Play and Match.** With a handicapped
  tournament live, the old Setup tab's single "Race to" had nothing true to
  say (`Race to –`) and its name fields could fight the draw that set them:
  two workflows, one page, both claiming it. Setup and Bracket merged into
  one Match tab behind a **Single match / Tournament** segmented control that
  follows the bracket (a tap wins until the bracket changes); with a bracket
  loaded the single-match editors yield to a read-only card naming the
  tournament, the loaded match and its races. Chosen from rendered mockups
  (option B) over the minimal fix (yield card alone, three tabs).

  **Mode and Vision left the phone.** Mode is always `match` when a phone is
  out — the deck's Mode key still cycles it for drills — and the vision kill
  switch was never once used from a phone. Both APIs stay (§2.1). The Session
  header that would have held one orphaned select became a **Ruleset** section:
  friendly labels and a caption of the rulebook's numbers off `GET
  /api/rulesets`' new `details`, so the pick never needs the docs.

  **Freestyle** (`clock/rulesets.py`, `timed: false`): match play with no shot
  clock — the thing `no_clock` mode is not, because that is the drill surface
  (no racks, no score keys, rep counters). Freestyle keeps everything match
  mode has except the countdown: operator start/restart/push/extension are
  no-ops in the clock itself, the hub gates vision exactly as it does for
  `no_clock` (same journal kinds — evidence keeps flowing, Flag and Replay
  keep working), the wall shows `FREE STYLE` where the digits were, and the
  deck's amber clock keys go dark with the countdown (`TIMED` in
  `deck/layout.py`, derived so the next untimed ruleset costs no edit).

  **Extensions are unlimited** behind `ENFORCE_EXTENSION_LIMIT = False`
  (`clock/rulesets.py`). The verbal extension can mishear table-side talk, and
  against a limited pool a false grant burns a real extension that has to be
  argued about mid-rack; against an unlimited one it costs nothing. The
  snapshot reports `extensions_allowed: 0` while unenforced, so the deck's
  pips and the phone's counter disappear rather than drawing a pool that
  cannot run out. Per-ruleset allowances stay in the data for the day the mic
  earns the flag back.

  **Shutdown ends the match day.** The Session hold now fires `POST
  /api/match/clear` before the SIGINT: score back to PLAYER 1 / PLAYER 2, a
  loaded bracket archived-and-cleared (reopenable from "Load past bracket").
  Deliberately on the deliberate gesture and not on server exit — a mid-match
  restart to deploy a fix must keep the score, which is why `MatchScore`
  persists at all. Best-effort: a wedged server costs yesterday's match
  reappearing, never a camera left open.

  **The join QR left the phone's bracket view** — whoever is reading that page
  already has it open; the wall's bracket scene keeps the code for everyone
  else.

  New: `POST /api/match/clear` (`cmd_match_clear`), snapshot `timed`,
  `/api/rulesets` `details`. Verification: 566 unit tests (16 new), ruff,
  mypy --strict; no detection change, so no corpus battery (the gate is for
  detection diffs).

- **2026-08-13 (checkpoint 95 — #43: what the first live hour said):** Five
  things from Steven driving a real bracket, four of them found at the table.

  **The score kept counting past the race** — a race to five with a player
  sitting on seven. A press past the target is a finger, not a score, and every
  one of them buries the confirm the deck is already offering. Increments cap at
  that player's target (at `race_to` in casual play, at nothing when neither is
  set). The cap is on the way **up** only: clamping a decrement would snap a six
  under a race to four down to four in one press, and minus has to stay worth
  exactly one point wherever it is pressed. A score already above its race is
  held rather than pulled down, because plus must never *lower* a score.

  **Conceding a match did not move the wall.** The scene flip lived entirely in
  the confirm key, so a result that arrived any other way — a concession, a
  score reported from a phone — left the TV on the play scene. The deck now
  watches the draw's count of **decided matches** and does the same thing for
  any of them, because to the room it is the same event. It watches that count
  and not `revision` deliberately: revision also moves for loads, edits and race
  changes, and a wall that flips every time somebody taps a stepper is a wall
  nobody can play under. The first count a loop sees is never news, so a deck
  restarted mid-event does not flip over results already in the draw.

  **The champion plate is gone**, one day old: it did not show at the table, and
  rather than debug a thing nobody had asked for twice, the header line that
  preceded it is back. The wall after a final is the draw with the winner's row
  already reading as won.

  **The race line stops saying the score twice.** `3 OF 5 · 2 OF 4` repeated
  both scores — they are 48 px above it — and made four numbers in a row that
  had to be read left to right to work out who was playing to what. One fact
  goes in the centre; two facts go under the players they belong to, so a
  handicapped match draws `TO 5` and `TO 4` beneath each player's own score, on
  their own side of the tally rule. Equal targets collapse back to `RACE TO n`,
  because printing it twice would make one fact look like two. Chosen from four
  rendered options at the bar's real geometry.

  **And a note on how this went wrong.** Two full corpus batteries were run
  while `cueclock run` was live, and `test_deck_loop.py` leaked its `_loop`
  threads — a leaked loop outlives its test and still resolves `bracket.hide`
  through whatever the next test patched in, so **the suite was firing real
  `obs-websocket` scene switches at the wall** while Steven was at the table.
  Both fixtures now stop their loop through one shared event. The rule that
  already existed (no batteries while a session is live) applies to anything
  driving `_loop` as much as to the corpus.

- **2026-08-13 (checkpoint 94 — #43: the night before the tournament):** Three
  of the things #43 parked for evidence from a real event, built on the eve of
  one, and the fourth settled as a decision instead of as code.

  **A concession is a withdrawal narrowed to one match.** Somebody gives up a
  single match and keeps the rest of their draw — the cousin who has had enough
  of the B side but wants to be at the table for the final. Withdraw then
  reinstate was the standing answer and is two presses, but it concedes every
  *other* unplayed match of theirs on the way through, so the two are equivalent
  only when nothing else of theirs is waiting, which on the B side is exactly
  when it is not. What is stored is **who gave the match up**, never the win it
  produces, and the walkover is expressed in `_auto_advance` beside byes and
  withdrawals — one rule down the chain rather than a second way for the draw to
  advance somebody. It is the one walkover written to disk, because nothing else
  in the draw implies it, and the derived half stays derived, so it is as
  reversible as the rest. **A withdrawal outranks a concession** in the same
  match: somebody who has gone home cannot be handed a win, or the match below
  waits on a player who is not in the room.

  **The champion is a moment rather than a header line** — a plate over the whole
  page with the draw ghosted behind it, spending the accent, which is free
  exactly then: the one colour on that page belongs to the live match and when
  the tournament is over there isn't one. It carries **no entrance animation**,
  and that is the finding worth keeping. The fade-and-rise built first rendered
  as *nothing at all*: a browser source in a scene that is not on program paints
  nothing, so the animation never starts and `animation-fill-mode: both` holds
  the plate at its `from` keyframe — opacity zero — for as long as that lasts.
  The same class of bug as checkpoint 92's `requestAnimationFrame` connector, and
  the same rule follows: the page must be correct in a source that has never
  painted, because that is its state every time the scene is cut to. Caught by
  rendering it, not by reading it.

  **The wall carries the address** (`GET /api/lan`, `server/lan.py`, `segno`).
  Scoring from phones needs no install and no auth by design, which left the
  tournament director reciting an IP to eleven relatives one at a time; the
  bracket page now shows a QR of `/control` in its header while a tournament is
  live, and the Bracket tab a larger one for the phone in your hand. The page
  cannot work it out for itself — OBS loads it over loopback — so the host is
  discovered server-side and the port comes off the request.

  **Amended the same day — the scene flip takes itself back down** (30 s, or
  the moment the clock leaves IDLE). The flip out was automatic and the flip
  back waited on somebody remembering the scene key, and the bracket scene has
  no shot clock and no table on it, so the wall could sit on a bracket through a
  live match. The flip is still right — the instant a result lands is when the
  bracket has what everybody wants to know, and during racking there is nothing
  to watch on the play scene — so it is bounded rather than dropped: a beat, not
  a mode. A showing somebody *chose* with the scene key carries no deadline and
  never expires.

  **And the last result of the night gets no dwell**, which is where the champion
  plate and the timer turned out to disagree: the plate is drawn the moment the
  final lands, and the dwell then swept it off the wall half a minute later, back
  to an empty table — the opposite of the moment it exists for. There is no next
  pair to rack for, so there is nothing for the return to be in aid of. The deck
  knows at press time because the confirm's own answer carries the champion.
  Also fixed on the way through: `test_deck_loop.py` leaked its `_loop` threads,
  and a leaked loop resolves `bracket.hide` through whatever the *next* test has
  patched in — which is a test seeing a scene change nobody in it made.

  **Late entry is a hard cutoff**, decided rather than built: the field freezes
  at Build. A bye is not a parking space — it has already advanced somebody — so
  dropping a latecomer in un-resolves a walkover, and once that player has played
  their next match it collides with the rule that refuses corrections behind a
  played match. A feature that works at 6:05 and refuses at 6:35 is worse at a
  party than one that never worked: the second is a rule, the first is a bug with
  a schedule. `docs/design.md` §10.3 and the §2.1 API row; the mockup note gains
  two more deliberate departures.

- **2026-08-13 (checkpoint 93 — #43: state that outlives the wall):** A UX pass
  over the bracket from the player / tournament-director side, and the five
  things it turned up. Two were defects. **A hand-set race target reverted
  silently**: `set_targets` wrote into `Match.targets`, which every resolve
  recomputes off the handicap chart, so an override held until the next result
  landed anywhere in the draw and then vanished — and on the loaded match the
  old number had already gone to `MatchScore`, so the wall and the bracket
  disagreed, which is the one failure the single-source rule exists to prevent.
  **Switching match destroyed the live score**: `load_match` called
  `reset_scores()` unconditionally and the Switch match list offered the *live*
  match as a tappable row, on a page passed round the room with no auth.

  Underneath both: `MatchScore` is the right single source of truth for what is
  on the TV, but it is a *single slot*, so anything not loaded had nowhere to
  live and the answer to "I need a minute" was to destroy what was there.
  Overrides are now stored against the pairing they were chosen for; an
  interrupted match is parked on itself and restored on reload (new `paused`
  status); clearing a result keeps its score, so a correction behind a played
  match does not cost re-keying eight presses while the wall counts through
  them; and clearing a bracket archives anything contested on the way out
  instead of unlinking it.

  **Forfeits, by way of withdrawal.** There was no honest way to advance an
  opponent when somebody went home — only plussing them to their target and
  confirming, recording a match nobody played. A withdrawal concedes every
  unplayed match down both sides, invents no score, and is expressed in
  `_auto_advance` beside byes, because that is what it is: a bye that happens
  mid-tournament. It is derived and never stored, so reinstating is one press
  and it survives a restart as the player's state; matches already played are
  untouched in both directions, a win never turning into a forfeit against
  them. **And the base races can change mid-tournament** (`PATCH /api/bracket`)
  — the 9pm lever, unplayed matches only.

  **On the wall the result is said by the winner's row and nowhere else.** A
  caption per state was built and cut on sight: "Final" already means the A
  Final and the B Final, and "Forfeit" was the third place one box said the
  same thing. So a finished match steps back — it is history, and the eye
  belongs on what is still to play — and the winner is the one thing in it left
  at full strength: white edge, lifted ground, name and score bold white
  against a loser that drops back. A conceded match scores **FF** against
  whoever gave it up and **W** against the player who advanced, which is what
  every bracket in every pool room already says. An estimated rating is
  prefixed `~`, so the bracket stops showing plainly what the score bar already
  refuses to put under a FARGO label. Verified by rendering both pages against
  a live server at 8 and 16 players, which caught the forfeit box contradicting
  itself and the withdraw buttons landing on a grid built for a different
  form. `docs/design.md` §10.2 and the §2.1 API row;
  mockup note amended with four more deliberate departures.

- **2026-08-13 (checkpoint 92 — #43: the bracket, and it never keeps a score of
  its own):** A double-elimination bracket as its own OBS scene, an auto-built
  and editable draw, and player-facing scoring that feeds it. Prompted by a
  family tournament, but the requirement was general: some nights it is two
  people keeping score, some nights it is a bracket, and the same pages have to
  serve both.
  * **The governing constraint, and every design call falls out of it.** The
    bracket is *optional state layered on top of the score already kept*.
    `MatchScore` stays the single source of truth for what is on the TV — the
    bracket hands it two players and two race targets and takes a result back,
    and never draws a scoreboard of its own. So the bracket page reads the live
    match's scores out of the score payload rather than storing them, and with
    no bracket loaded every page and every deck key behaves exactly as it did
    before. That is what makes casual night and tournament night the same
    software, and it is why "does the bracket disagree with the wall" is not a
    failure mode this can have.
  * **The cross-drop is the part that would have failed silently.** Where the A
    side's second round drops its losers, the incoming order is reversed against
    the B-side matches waiting for them. Drop straight down instead and the
    bracket still *runs* — it quietly replays first-round pairings, which is
    exactly the kind of bug nobody notices during a party. Pinned by a test that
    walks the reference graph and proves the branches feeding each first-drop
    match descend from disjoint halves of Round 1.
  * **The handicap chart is FargoRate's, not ours.** Game odds
    `1/(1 + 2^(−diff/100))`, exact negative-binomial match odds, and a race
    picked off a ladder (5-5, 5-4, 6-4, 6-3, 7-3, 7-2). Each mode has a target
    for the underdog's match odds with 50% as a hard ceiling — the target is
    what stops the rule degenerating into "always take the biggest legal spot",
    which at a race to five hands a 68-point gap the same 6-4 as a 100-point
    one. **First cut got this wrong in the other direction**: a spread cap
    expressed as a fraction of the base race reproduced the two published
    columns but could never reach 7-2, the issue's own worked example. Replacing
    it with a ceiling near a third above the base reproduces every published
    endpoint on record — their base-3 ladder ending at 5-2, race-to-5 at 6-4,
    race-to-11 hot at 14-8 — *and* lands 563-vs-240 on exactly 7-2.
  * **Reaching the target arms the result; it never fires it.** Plus/minus is
    the easiest control on the deck to fat-finger, so auto-advance was rejected
    deliberately: one stray press would corrupt a bracket somebody has to unwind
    mid-party. The arm is **derived** from the live score against each player's
    own target rather than stored, which is what makes holding the winner's key
    to take a point back clear it by itself — there is no undo to find.
  * **Two deck keys, both dark on every other night.** `(3,5)` confirm (free
    since "I shot" was pulled 2026-08-05, so no existing key changes meaning)
    and `(2,3)` scene toggle. Confirm is one action with four effects: result
    written and both players routed, next ready match loaded at 0-0 with its own
    targets, `/api/rack` fired, and the TV flipped to the bracket so the update
    is on the wall while the next pair racks. The scene flip is the deck's half
    because obs-websocket lives out there. The reason page borrows `(2,3)` while
    it is open — existing overlay behaviour, and the accepted cost, since every
    free cell on a 15-key deck is one of those three.
  * **Corrections walk back one step at a time.** Re-deriving downstream slots
    is free while they are empty. Behind a *played* match an edit is refused by
    name (`Match 5 has already been played — clear its result first`) and
    clearing that result is its own act, so nothing real is ever silently
    erased. A walkover is not a result and cannot be cleared.
  * **Rendered rather than assumed.** The bracket page was checked at 8 and 16
    players by snapshotting it through an off-screen WKWebView, which caught
    four things reading the code would not have: the empty-state overlay sitting
    on top of the draw (a `display:flex` rule outranking `[hidden]`), a
    `requestAnimationFrame` callback that never fires in a browser source that
    is not painting — the same condition an OBS source in a dark scene is in —
    unstarted matches showing `0` where they should show a dash, and at sixteen
    the B final landing under the *second* final, where a straight connector
    would have run through it.
  * **Follow-up the same day, found while writing the walkthrough:** building a
    draw left nothing loaded, and the Play tab hid the whole bracket panel —
    including Switch match — when no match was loaded, so there was no way to
    start the first one. Building now puts the first ready match up (which is
    what you built the draw for), and the panel follows the *bracket* rather
    than the loaded match.
  * **Second follow-up, from Steven's first real build: the races he chose were
    not the races that got built.** He set 3 and 2, cleared the draw, rebuilt,
    and got 5 and 4. `clearBracket()` reset the page's in-memory copy of the
    races to the defaults and never repainted the steppers, so the readout said
    3/2 while the copy said 5/4 and Build sent 5/4. The tell was in the saved
    file: `handicap` and `seeding` survived the clear because they are read out
    of their own `<select>` at build time, and only the two fields that were
    *duplicated* in JS state were lost. Fixed by deleting the duplicate — the
    readout is the value now, read back at build time like every other field —
    rather than by repainting, which would have left the same trap for the next
    field. `bracket_created` also gains `a_race`/`b_race`: without them the
    journal could not say a build had gone out with races nobody chose, and
    diagnosing it took reading the page's JavaScript instead of one log line.
    Both directions verified in a real browser, driving the exact sequence:
    old page rebuilds at [5,4], new page at [3,2].
  * Shipped with it: `/bracket` (1920×1080, geometry computed from the field so
    3-16 players draw without scrolling), the Bracket tab on `/control` plus the
    match header / per-player targets / report / switch-match additions to the
    Play tab, two race targets on the score bar when they differ, per-player
    `target` on `MatchScore`, `scripts/setup_obs_bracket.py`, and persistence
    beside the score with archives beside the journals. 505 unit tests green
    (47 new for the bracket core, 13 for the API, 6 for the deck), full cold
    corpus battery green, mypy --strict and ruff clean.

- **2026-08-11 (checkpoint 91 — #41: the detector was destroying the evidence,
  so ball-in-hand now stops the clock resetting):** Steven's directive was the
  lead and it was right — *"an arm / a hand does not look like a cue"*. The
  reason nothing downstream could act on that is upstream: `detect_balls`
  already **classifies** every non-ball blob (arm → `max_area_px`, stick →
  `min_extent_px`, anything reaching in from off-table → the mask-border rule)
  and then throws the verdict away. When a hand *engulfs* a ball the combined
  blob clears 6000 px² and is discarded outright, so the ball simply vanishes
  and there is no detection left to carry `merged` — which is the whole
  explanation for `merged` reading ~0 on hand events across checkpoints 50, 66
  and #38. The arm was never absent from the image, only from the exported
  evidence. So: export it.
  * **The journal contrast first (#41's stated lead), and it came back
    negative — usefully.** Every one of the 84 settles in
    `session-20260811-182341` that landed while the clock was RUNNING counted
    a shot, except the 4 provisional reverts. The carries Steven remembers
    *not* resetting the clock are the four settles that landed in **IDLE**
    (t=127.19, 474.41, 894.69, 1424.41 — two of them textbook carries,
    `involved=1 moved=1` at 424 mm and 700 mm), where a settle is a no-op by
    design. "It works sometimes" was the clock's state, not a discriminator
    hiding in the live system. Recorded so nobody hunts for it again.
  * **What separates a carry from a stroke is not the hand's presence, it is
    where the ball ends up.** During a stroke the bridge hand and forearm are
    on the cloth beside the cue ball — which is why every presence-shaped
    axis #38 measured overlaps, and why "arm near ball" was never going to
    work. Measured over the rearrangement window (the frames the settle's own
    summary describes — *not* the episode latch, which on
    `ball_in_hand_carry_b_0811` opens only as the hand withdraws, 0.6 s after
    the carry is over): for each ball that **arrived** somewhere new, walk
    back to the last frame that spot was empty and ask whether a hand-scale
    intrusion covered it then.

    | population | n | verdict |
    |---|---|---|
    | ball-in-hand carries (Steven's ground truth) | 4 | **every** arrival hand-delivered (1/1 each) |
    | strikes, pickups, retrievals, replaces | 12 | at least one arrival with **no** covering blob at all |

    The conjunction — *every* arrival, not any — is what holds the fail-safe
    direction: a real stroke always leaves at least one ball resting on open
    cloth. `scratch_replace`'s shot is the case that proves it, with the
    player leaning fully across the table (core 101 px over two of its three
    arrivals) and still reading 1 of 3.
  * **New evidence, two new constants, both empty-band anchored.**
    `DetectConfig.intrusion_min_core_px = 21.9` — the radius of the largest
    disc that fits inside the blob, set to the nominal ball radius on the
    argument that a hand covering a ball is at least as fat as the ball.
    Measured: a hand carrying a ball reads **23–62 px** (n=4); the closest
    competing population, a bridge hand at address with the shaft running
    through its fingers, reads **8–20 px** (n=7). `ARRIVAL_HAND_WINDOW_S =
    0.25` — the worst measured gap between a carry's arrival frame and a
    covering hand is **0.064 s**, and the population that must not be
    admitted stays empty out to 0.5 s, so the band is empty in both
    directions. `ARRIVAL_CLEAR_MM = 60.0` is one ball diameter (WPA 57.15 mm),
    not a fitted number.
  * **Where it lives.** `detect_frame()` returns `(balls, intrusions)` and
    `detect_balls()` is now its ball half — the accept path is byte-identical
    and §2.3 lesson 2 is untouched (balls are still never shape-filtered).
    `pipeline` keeps the rearrangement window and reports `arrivals` /
    `hand_arrivals` on `RearrangeSummary`; the hub's
    `_settle_is_hand_placement` is the single line of policy (§13.1) and
    re-supplies the evidence test checkpoint 87 removed, on evidence rather
    than on shape. `ShotClock.balls_settled(suppress=...)`, deliberately kept
    at 87, is what it drives. The detect cache carries the intrusion channel
    (SCHEMA 2): a cached replay that dropped it would grade a different
    verdict than a cold one.
  * **Before / after, on the clips that carry Steven's ground truth.**
    `ball_in_hand_carry_0811` counted 2 shots → **1**; `..._carry_b_0811`
    2 → **1**; `..._triple_0811` 3 → **1** (that is the rack 9 → 12 episode:
    three counted shots in 3.6 s for one real stroke). Every carry still
    *settles* — the clock freezes at the motion latch and resumes at the
    remaining it froze with, which is #33's headline behaviour restored. All
    three promoted to the corpus with `shots` deliberately unasserted (a carry
    fires an honest-looking 16 274 mm/s ShotTaken — the strike-layer artifact
    #38 recorded as dead) and the clock outcome pinned by
    `test_0811_carries_do_not_reset_the_clock`, which fails on the pre-fix
    engine and passes on this one.
  * **Not fixed, stated plainly:** a settle with *no* arrivals — a pickup, a
    post-scratch retrieval, the #37 phantom shape — is never refused here.
    #38 measured that class unresolvable (a retrieval and a no-contact kick
    are byte-identical evidence) and absence of evidence must not refuse a
    shot. The shaft cases are also untouched: a legal ball-in-hand tip nudge
    and a foul shaft bump remain the same pixels and both still reset, exactly
    as the 2026-08-02 decision says. This fixes the *hand*, which is the case
    Steven actually hits.
- **2026-08-11 (checkpoint 90 — #40: the motion latch stops being roster-only):**
  `latch_blind_shot_0811` (Steven's flag from `session-20260811-182341`) is a
  real shot at **2047.8 mm/s** on which the engine emitted **zero events over
  all 20.74 s** and `episode_active` was False throughout; live the countdown
  ran through the stroke and hit GRACE. Root cause was upstream of every
  policy layer: the motion-episode latch only ever considered **roster**
  balls, roster admission happens only during sustained quiet, and the struck
  cue was reborn as a new track *at the impact* — so the fastest object on the
  table was structurally unable to latch. No episode ⇒ no provisional freeze
  (#33 Part B) ⇒ no settle ⇒ no compensation. The strike path's designed
  escape hatch (a fire certifies the track, admits it to the roster and
  latches directly) never ran either: the crown sat on the cue's dead
  identity, still and unobserved at a position the ball had already left.
  * **The fix is positive evidence, not a wider roster.** A track outside the
    mover set may latch when it is *visibly a struck ball*
    (`EventEngine._struck_mover`): two consecutive **observed** raw reports at
    `parked_fast_confirm_mms` (struck-ball kinematics) **and** an observed
    history at most `newborn_merged_frac_max` merged (a ball in a hand is
    inseparable from the hand), on top of the ordinary `_solidly_observed` /
    `moving_enter_mm` bar. **No new constant** — both gates already existed in
    the strike path for exactly this question, so this is reuse, not tuning.
    Age is deliberately dropped: `_judgeable`'s age gate is what a newborn
    cannot satisfy in the frames that matter (this cue needed 0.40 s to become
    judgeable, by which time the clock had run through the stroke), and
    `_is_still` already makes the same trade in the other direction.
  * **Measured before writing any code**, over every non-roster track
    corpus-wide (70 clips) that clears the ordinary motion bar. Best
    consecutive-observed raw pair: real strokes **1210–3803** (jaw_strike
    1210, scratch_shot 1296, late_emergence 1315, occluded_address 1419,
    drill 1598, parked_vote_newborn 1828, latch_blind 1917, pot_scratch 2646,
    phantom_candidate_block 3803) against hand junk **≤ 1122**
    (scratch_replace's throw 1122, lone_roller 1025, pickup_snatch 1022,
    address_chatter 871). The hand events that *do* reach struck-ball speed
    are the carries, and they read **0.67–1.00 merged** against **0.00–0.17**
    for every real stroke admitted. The two gates share no failure mode:
    nothing in the corpus clears both without being a ball that was just
    struck. The bare "solidly observed + 30 mm" predicate was measured first
    and rejected — it flips the latch on 30+ clips, including every pickup,
    grab, snatch and carry.
  * **The new latchers do not stamp `_recent_movers` or `_last_mover_t`.** A
    reborn cue must stay invisible to the carom guard and to the birth path's
    pre-strike-dwell test; stamping it there would have refused the very
    strikes the admission exists to catch (`jaw_strike`, `late_emergence`,
    `occluded_address`, `scratch_shot` all latch at ages 0.02–0.11 s, inside
    their own candidates' age budgets).
  * **What it does to the breaks** — the class checkpoint 87's reverted
    attempt broke. `break_0801`, `break_0801b`, `break_outrun_0805`,
    `break_underread_0801`, `break_dark_flight_0806`, `break_shot`: **every
    event identical**, pre and post. Two of them latch the same episode a
    touch earlier (`break_0801` 12.83 → 12.82, one frame; `break_underread`
    0.93 → 0.88) and nothing downstream moves. `break_dark_total_0806` — the
    fully dark break — is the one that moves, and it moves the right way: the
    engine now sees the scatter and settles it at **14.29 s** instead of
    leaving it to the identity-free backstop at **18.51 s**, while the balls
    are measurably at rest by 13.2–13.8 s. The break still counts through the
    hub's dark tier (`appeared` 10, floor 5) — nothing about `_adjudicate_break`
    changed. `test_break_dark_total_settles_via_the_backstop` is therefore
    **retired deliberately**, exactly as its own docstring instructed, and
    replaced by `test_break_dark_total_settles_at_the_rest`, which pins the
    thing that actually matters: one settle, at the rest, reaching the dark
    tier. The backstop itself is untouched and still covers a genuinely
    sleeping engine.
  * **Ball-in-hand is untouched.** All four un-promoted 0811 carry clips
    (`ball_in_hand_carry`, `_carry_b`, `_triple`, `phantom_still_down`) replay
    **event-for-event identical** pre and post; the only difference anywhere
    is `_carry_b`'s first latch moving 0.11 s earlier inside an episode it
    already had. The open ball-in-hand problem is neither helped nor worsened.
  * **One clock-grade premise was wrong and is fixed with it.**
    `_assert_clock_outcome` rejected a freeze older than `FREEZE_EARLY_S` on
    the premise that it "cannot belong to this stroke's episode" — a proxy for
    *held since then*, which the timeline can answer directly. On
    `pot_scratch_nohold_0811` the golden's fire is **1.11 s late** (the stroke
    is at 6.509, a track born at 2646 mm/s; the fire lands on the ball's third
    rebirth at 7.623), so the freeze at the true stroke read as too early
    against displaced ground truth. A freeze the clock never resumed from
    before the stroke now counts. Nothing is loosened for a real miss: a
    missed stroke meets a RUNNING clock, which means a resume.
  * `latch_blind_shot_0811` is in `EXPECTED` at `(None, 0, 1, 1)` with a
    golden and a named pin, `test_latch_blind_shot_stops_the_clock`. Shots
    stay unasserted on purpose — the strike layer is still blind on this clip
    (the crown never leaves the corpse) and #34's criterion is the clock, not
    the fire. Both new named tests fail on the pre-fix engine ("the countdown
    never stopped at all"; "dark break settled at 18.51s") and pass after.
  * Goldens regenerated deliberately: `break_dark_total_0806`
    (18.510 → 14.293, above) and `live_divergence_0731a` (gains a
    `BallsSettled@0.960` — the clip opens mid-motion with a track at
    4751 mm/s, which now latches and settles; its clock outcome is unchanged
    and the count falls outside the graded window). `stroke_tug_strike_0801`'s
    golden drift is pre-existing (checkpoint 73) and untouched.
  * Corpus: **510 tests, 1 failure — the pre-existing `parked_vote_strike_0801`
    red, byte-identical to its baseline message.**
- **2026-08-11 (checkpoint 89 — #37 Part B: the newborn disarm was dead by
  construction):** The audit's first sweep, under the Chesterton rule — cite
  the original clip, then show what covers it today.
  * **DELETED: the one-shot-per-rest-cycle newborn disarm**
    (`_newborn_disarmed_at`, 5 sites). Its only setter was the newborn fire,
    and that same statement block sets `_table_moving = True` and
    `_fired_this_episode = True`; the latch can then only fall at the settle
    (the pickup discard refuses a fired episode) and the settle cleared the
    disarm in the same block. So the stamp was never non-None while the table
    was unlatched, which makes the `_table_moving` refusal directly below it
    an **exact superset** on the newborn and own-rest paths. The one path
    where it could still bite (`_maybe_open_rested`'s re-qualify scan, whose
    latch check is conditional) is retired on a different argument: since #33
    a second fire inside one latched episode has no clock consequence at all —
    `ShotClock.shot_taken` returns early in MOTION and the settle counts
    exactly one shot per episode. Corpus flat: seven real strikes fire newborn
    (checkpoint 57's list) and none re-fires without it; no clip changes
    outcome, no golden moves. Its own history closes the case — it caused two
    live failures (checkpoint 31's jaw strike, checkpoint 51's session-long
    shutdown of all three paths) and prevented none. Removal note left in
    `_maybe_open_newborn`; `design.md` §2.3's checkpoint-51 paragraph now says
    so; `test_settle_rearms_the_birth_paths_when_the_vote_is_parked` survives,
    re-scoped from mechanism to outcome.
  * **KEPT: the axis-kill re-open credit** (`_axis_killed_t`) — the "0.01 s
    knife-edge" the issue nominated. Measured, not assumed: neutralising it
    **loses `address_chatter_miss_0801`'s real 1396 mm/s stroke outright**
    (its `ShotTaken@10.190` disappears, `test_address_chatter_shot_fires_at_
    impulse` goes red). It is load-bearing on a clip it was not written for.
  * **KEPT: the carom guard and the hand/arm merged-blob stamps.** The
    failures they cover — a caromed ball crowned as a strike, `cue_grab_0801`'s
    gloved snatch minting a phantom rack shot — all *rearrange the table*, so
    the settle verdict fails open on them by design. The #38 removal note is
    the measurement: its predicate caught none of a session's five
    ball-in-hand carries because a carry moves a ball. Part A covers
    `involved == 0` and nothing else.
  * **KEPT: the 3000 mm/s IDLE break floor.** #36 landed settle-time break
    adjudication, so the floor's *cost* (84 eaten fires, 15 manual rescues) is
    largely paid at the settle now — but its named hazard is a phantom fire
    during racking starting the clock 19 s early and swallowing the break
    (2026-08-01 11:14:49, journal-pinned), and E8 does not cover that: it acts
    at the settle, while a fire above the floor moves IDLE→MOTION immediately.
    The corpus cannot prove this one either way — its racking clips are
    event-silent, which is exactly the "mere silence proves nothing" trap
    checkpoint 57 recorded. Unprovable offline, so it stays.

- **2026-08-11 (checkpoint 88 — #37 Part A: the settle learns to say no):**
  #33 gave the clock a window — motion onset through settle — and spent it in
  one direction only: a missed strike is rescued, an invented one still stood,
  froze the clock, counted a shot and reset the countdown. The symmetric half
  is live. `parked_vote_strike_0801` was the corpus's one standing red and is
  **green**; the corpus is at **zero red**.
  * **The trap first, because it is the whole problem.** `involved == 0`
    against the pipeline's rearrangement summary says two opposite things:
    the table is provably unchanged, or there was never a quiet layout and
    `self._settled_baseline or present` diffed the settled frame against
    itself. #33 could not tell them apart and fails open on the shape, and the
    comment anchoring that branch called `involved == 0` "a clip-start
    artifact shape, never a measured placement". **Measured over every settle
    the corpus produces (55 settles, 51 clips): THIRTEEN read `involved == 0`,
    and twelve of them have no baseline at all** — `address_chatter_miss_0801`,
    `break_underread_0801`, `cue_grab_0801`, `kick_scratch_0811`,
    `missed_strike_0731`, `occluded_address_0731`, `parked_vote_newborn_0805`,
    `scratch_replace`, `scratch_shot_0801`, `settle_stall_0801`,
    `stroke_tug_strike_0801`, `tip_bump`, all clips that open mid-action. The
    thirteenth is `parked_vote_strike_0801`'s phantom, against a 7-ball quiet
    layout. So the shape was never the discriminator; **where the baseline
    came from is.**
  * **`RearrangeSummary.baseline_balls`** (`vision/pipeline.py`) is the whole
    vision-side change: how many balls the quiet layout resolved, `None` when
    the fallback stood in for one. Additive, evidence-only, journaled on every
    settle as `baseline` — the live `flag_phantom` record had to be settled by
    replaying the dump because the journal could not say.
  * **`Hub._settle_is_phantom`** is the verdict, and every clause is positive
    evidence: **MOTION only** (the clock is sitting on a provisional freeze,
    so being wrong costs exactly what #33 already accepts — in GRACE the same
    refusal would buzz a false violation over a live shot), a **real quiet
    baseline** of `QUIET_BASELINE_MIN_BALLS` (3) or more, `involved == 0`, and
    not the pipeline backstop. Anything missing or degraded counts, as before.
  * **The floor is measured, not chosen.** Baselines resolving 1–2 balls are
    all unresolved-pack or mid-action clips (`break_0801`, `break_0801b`,
    `break_outrun_0805`, `pickup_snatch_0801` at 1; `break_dark_flight_0806`,
    `jaw_strike_0801`, `merge_yank_0731` at 2). Baselines of 3+ are honest
    layouts, **including late-rack tables with genuinely few balls left**
    (`pot_scratch_nohold_0811` 3, `lone_roller_0801` 3, `cluster_shot_0801` 4,
    and the live phantom's own 4 — which is why the 2026-08-11 corpus note
    claiming an empty 3–4 band is superseded: those clips are anchored, the
    band is not empty, and the split is fallback-vs-real, not small-vs-large).
    A table down to two balls cannot be adjudicated and every phantom on it
    stands — the failing-open direction, accepted.
  * **Blast radius: 2 rejections over 63 settle verdicts**, across the corpus
    plus the four un-promoted 2026-08-11 dumps. Both are the known phantoms.
    Every real-shot settle with an anchored baseline reads `involved >= 1`,
    including the softest touches in the collection (`soft_touch_0731` 2,
    `tip_bump` 2) and the three ball-in-hand clips #41 is filed against
    (involved 1–4) — so this rejection does not touch, and must not be read as
    covering, the ball-in-hand problem.
  * **Two evidence axes the issue proposed were measured and rejected.**
    *Mover travel:* real shots read 0 mm of surviving-mover travel on eight
    corpus clips (every mover dies in the scatter) while the live phantom's
    surviving mover read 26 mm — any floor that rejects the phantom refuses
    real shots. *Roll-out length:* `report.PHANTOM_SETTLE_S` (1 s) is a
    diagnostic and must stay one — **`pot_scratch_nohold_0811` is a real pot
    whose settle lands 0.73 s after its fire**, and the two proof cases sit at
    1.37 s and 1.65 s, so promoting it would refuse a real shot and catch
    neither phantom.
  * **`phantom_still_down_0811`** is promoted to the corpus (from
    `flag_phantom`, 2026-08-11 18:30:20Z, session t=386.36 — Steven's ground
    truth: nothing happened, he was still down on the ball). The cold replay
    reproduces the live journal event for event: same settle, same zero
    rearrangement, the real stroke's 3064 mm/s to the millimetre, the same
    second settle at involved=3. Its baseline was a genuine 4-ball quiet
    layout. `EXPECTED` cannot grade it — the phantom precedes the clip's only
    real stroke and `_assert_clock_outcome` ignores counts before the first
    ground-truth stroke — so it is pinned by
    `test_phantom_still_down_is_rejected_at_the_settle`, which fails on the
    pre-fix engine (2 counts: 10.50 and 18.92) and passes now (1 count, the
    real one, with the phantom's freeze released at 10.50).
  * **Measurement story, honestly (issue open question 3).** `report.py` is
    right that a phantom cannot be *proven* from a journal, and the candidate
    count cannot fall to prove this worked — a rejected settle stops producing
    a `Shot` at all. So the report gains `settles_rejected`, tallied off the
    settle record's new `rejected` marker and deliberately **excluded from
    `shots`** (counting it would inflate every rate with the exact class the
    rejection removes). The pair to read live is `settles_rejected` rising
    while `missed` stays flat. Offline, the load-bearing measurement is the
    one above: 2 rejections in 63 verdicts, both ground-truthed, no clip
    changing outcome.
  * **Golden drift, deliberately not regenerated.** `parked_vote_strike_0801`
    still emits the phantom's `ShotTaken@10.363` + `BallsSettled@11.732` — the
    event layer still crowns a hoodie, the hub now neutralises it (§13.1:
    vision emits evidence, the hub applies policy). Its golden keeps the
    2-shot/2-settle stream, which is the only surviving record that the engine
    should not have fired there; regenerating it would canonize the phantom.
    Same treatment `stroke_tug_strike_0801`'s golden already gets. Both drifts
    pre-date this checkpoint — `parked_vote`'s was simply invisible while the
    clip failed before reaching the golden check.
  * `stroke_tug_strike_0801` re-verified: unchanged, still green under #34's
    clock-outcome criterion, still a `StrikeMiss` warning and a golden drift.

- **2026-08-11 (checkpoint 87 — #38 deleted rather than tuned, #39 root-caused
  at 3 mm):** The measurement pass #38 demanded, run before touching
  `_settle_not_a_shot`, on the seven flagged dumps from
  `session-20260811-182341` (preserved to the T7 under behaviour names, five
  replaying warm field-for-field against the live journal) plus 11 corpus
  scenarios — 23 motion episodes. Probes: `episode_dump.py` (replay once,
  dump the substrate) + `episode_report.py` (axes computed from the dump, free
  to re-run) + `scratch_latch_probe.py`, all in
  `~/Desktop/cueclock-accuracy-probes/`.
  * **Gate A: nothing separates a carry from a strike.** All five candidate
    axes overlap across 9 hand and 6 strike episodes — deceleration
    (time-to-peak HAND 0.0–1.0 vs STRIKE 0.0–0.085; plateau fraction
    0.038–0.88 vs 0.146–1.0; accel/decel runs 1–6 vs 1–3), path curvature
    (tortuosity 1.03–4.22 vs 1.00–1.30), cue-stick presence at onset
    (44–341 px vs 48–168 px), and onset→30 mm latency (0.026–0.633 s vs
    0.025–0.768 s). **`merged` is the one asymmetry and still cannot carry
    the predicate:** exactly 0.0 on all six strikes and positive on 3 of the
    4 session carries (1.0, 1.0, 0.55) — but ~0 on every *corpus* hand event
    (`cue_grab` 0.003, `pickup_newborn` 0.002, `pickup_snatch` 0.032,
    `scratch_replace` 0.0/0.006). That is **checkpoint 50's finding
    reproduced on a fourth clip set**, and it means `merged` cannot replace
    the shape test — the retrieval class the shape test exists to refuse
    reads exactly like a strike on it. Measured, refuted, recorded as dead.
  * **#38: the refusal is deleted, not retuned** (Steven's call, with the
    numbers). Live it fired **3 times in 80 shots** — right once (a
    retrieval), wrong twice (a jump shot and a no-contact kick, both real
    shots refused, the jump shot's clock then running 15.7 → 0 → GRACE) — and
    caught **none of the session's five ball-in-hand carries**, because a
    carry moves a ball and is counted at the predicate's first test. Removal
    was measured against the full corpus first: **zero cost, no clip changes
    outcome**; the only failures were the 7 unit tests that existed solely to
    pin it, now removed with it. `ShotClock.balls_settled(suppress=...)` is
    deliberately kept — the clock-side mechanism was never what failed.
    **Ball-in-hand must still not reset the clock (Steven, 2026-08-11); that
    problem stays open** rather than being served by a rule that is wrong
    twice as often as it is right. `episode_discarded` (the mid-table pickup
    discard) is a different path and is untouched.
  * **#39: two real scratches missed by 3 mm and 4 mm, and it was never
    identity.** Both went into the same corner; the cue's last *observed*
    position was **123 mm** (the pot) and **124 mm** (the kick) from the
    pocket centre against `pocket_zone_radius_mm` = 120, so `_near_pocket`
    refused and neither reached the #26 adjudicator. The whiteness vote held
    the cue throughout on both (0.989 and 1.0) — §2.3's usual suspect,
    positively excluded by replay. On the pot the cue was tracked marching in
    at ~850 mm/s (164 → 148 → 134 → **123 mm** over six frames): the track
    ended **one frame short of the zone**. Fix: `_near_pocket` allows one
    frame of flight (`speed × measured frame interval`, capped at one more
    zone radius), where the interval is the engine's own measured detect
    cadence rather than a new constant — so it tracks a starved loop instead
    of assuming 78 Hz. Deliberately **not** a bigger zone, which would admit
    slow deaths near a pocket and re-open the ~6× over-fire #26 closed;
    `spin_in_place`'s coasted phantom sat **359 mm** out and no single frame
    bridges that. Before: both clips emitted zero scratch events of any kind.
    After: both emit a candidate, and the pot reaches a full `Scratch`.
    Promoted as `pot_scratch_nohold_0811` and `kick_scratch_0811` with a
    bespoke test pinning the *candidate* — the kick's verdict never lands
    inside its clip, so counts alone would stay green through the bug.
  * **The #39 coupling defect is real but was latent, not live.** The early
    return at the refused settle sat before `_maybe_hold_for_scratch()`, but
    guard 3 made refusal and an open candidate mutually exclusive, so it
    could not have contributed to either missed scratch. Deleting the
    predicate removes the early return outright and the hold is now
    unconditional at the settle.
  * **A flagged clip was misread by this session and by the last one, and the
    correction matters more than the clip.** `flag_missed_4` was renamed
    `jump_shot_0811` on the theory that its subject (a jump shot at session
    t=585.40) fell 7.6 s before the dump's 20 s window. The window claim is
    true; the conclusion was wrong. The clip contains **a different, later
    shot that current code misses completely**, and it reproduces cold: peak
    ball speed **2047.8 mm/s**, `episode_active` **False for all 20.74 s**,
    `all_still` **True through the whole crossing**, and **zero events**. The
    cue crown sat on track 3 at (1011, 191) reading `observed=False` — a pure
    extrapolation at a position the ball had already left — while the real
    cue flew as track 8 at 1668→1852 mm/s; two dead tracks then coasted *off
    the table* (y → −59) at 380–692 mm/s before being dropped. Live, the
    clock hit GRACE 1.7 s later. That is §4.11's stale-flag-holder mechanism
    and the E7 class, and it is now the best-evidenced instance of it we
    have. Renamed **`latch_blind_shot_0811`**; deliberately NOT yet in
    `EXPECTED` — it is the next piece of work, not a parked failure.
    Method note: the burned-in renderer said *"nothing is **detected** for the
    entire 21 s"*, which was read as "nothing happened". Read the frames.
  * **`--update-goldens` is not safe to run corpus-wide, and
    `stroke_tug_strike_0801.json` is why.** Eight goldens change under it
    (`break_0801` t and speed, `break_shot`, `pair_strikes_0801`, `soft_shot`,
    `soft_touch_0731`, `spin_in_place`, `stroke_tug_strike_0801`, `tip_bump`).
    None are this checkpoint's: regenerating at HEAD with every source change
    reverted produces **byte-identical** files (62/62), which is also the
    proof that this checkpoint perturbs no clip's timeline at all. They were
    absorbed here and then **reverted**, because one of them is load-bearing.
    `stroke_tug`'s golden is *deliberately stale* — the fire was lost at
    checkpoint 73 and the golden is the surviving record of the 13.198 s
    stroke, which is what `ground_truth()` reads. Refreshing it makes the
    golden hold 0 shots against `EXPECTED`'s 1, the two disagree, the clip
    loses its clock ground truth entirely, and it falls back to the hard
    count assertion and fails — turning a documented, understood residual
    into a red. Regenerate goldens **per clip, for clips you changed**; never
    across the corpus.
  * Housekeeping: the internal volume went 94% → 73% (12 → 56 GB free). The
    six pre-#33/#36 dumps were deleted with Steven's approval; the seven
    2026-08-11 dumps moved to the T7. `kick_scratch_0811` lost its
    `state.json` to a SIGINT, but the camera-ns → session-t map is
    bit-identical across all five dumps that kept one
    (A = −1118566.678510), so it still locates exactly (session
    1574.22–1594.58) — worth reusing whenever a dump loses its sidecar.
    Raising `--ring-seconds` was proposed and **declined by Steven**; it stays
    at 20.

- **2026-08-11 (checkpoint 86 — #34 closed: the phantom is real, E7 re-scoped):**
  * **`parked_vote_strike_0801`'s surplus shot is a phantom — confirmed from
    the footage, not assumed.** Item 2 said to check it before believing it,
    so the frames around t=10.36 went to Steven and his ground truth is
    **"nothing"**: no ball moves there. The third `ShotTaken` has no motion
    behind it. The clip stays red, and it is now the corpus's only red and a
    *verified* defect rather than a suspected one.
  * **Strike misses are now counted, not just gated away.** Checkpoint 85
    demoted the fire from a gate; this restores its visibility as the
    diagnostic #34 asks for — a `StrikeMiss` warning per real stroke that
    never fired, which pytest tallies in its end-of-run summary. Current
    reading: **1 strike miss across 49 graded strokes** (`stroke_tug`), i.e.
    the substrate is fine and the latch is covering exactly one clip.
  * **E7 re-scoped (item 3), and the answer is "not justified".** Under the
    corrected criterion the residual collapsed to **one class with one
    instance**: phantom counts, `parked_vote_strike_0801`. The other two
    classes the issue hypothesized are empty — breaks moved to #36, and
    "shots the latch itself misses" measures **zero** (every graded stroke in
    the corpus froze within budget). Stated honestly: that is a fact about 61
    corpus clips, not about the live distribution. E7 remains the
    highest-regression-risk surface in the system, so one verified phantom
    does not buy touching it; **tuning stays gated on the §13.6 trigger** —
    the next identity-family live flag, or phantom / frozen-clock rates that
    fail to fall. The instrument to measure that now exists, which was the
    point.
  * Not graded, and deliberately: a **reverted** provisional freeze. Measured
    corpus-wide, there are exactly two (`ball_in_hand_pickup_0801` 0.27 s,
    `cue_grab_0801` 0.61 s), both restoring the countdown to the exact
    remaining they froze at — zero time lost — and the overlay renders
    `math.ceil(remaining)`, so a sub-second hold cannot change the digit on
    the wall. Steven's call: don't grade it.

- **2026-08-11 (checkpoint 85 — the corpus grades the clock, #34 items 1+2):**
  The battery graded the event stream, and since #33 the event stream and the
  product diverge *on purpose*: a missed strike whose latch still stops the
  countdown ~0.25 s after the stroke is indistinguishable at the table from a
  detected one. Steven: *"If the clock stops 0.25 s after the shot actually
  occurs, for all intents and purposes, that IS shot detection. From a test
  perspective too."* So the gate moved.
  * **The harness** (`tests/corpus/clock_replay.py`) replays a clip exactly as
    `cli.py`'s run loop does — same events, same latch edges posted after the
    frame's events, same `note_frame`/`note_motion` — into a **real `Hub`**
    with the hub's clock driven by frame timestamps. It re-implements no
    policy: provisional freeze, settle adjudication, break tiers and the
    frozen-clock watchdog all run in the shipping hub, so the grade cannot
    drift from what ships. Asserted per real shot: the countdown **stopped
    within budget** and the shot was **counted exactly once**.
  * **Ground truth is seeded mechanically, never invented** — the #33 Part C
    lesson applied to tests. *How many* shots from `EXPECTED`, *roughly when*
    from the golden's `ShotTaken.t`. Where the two disagree or either is
    missing, the clip is **skipped as "no ground truth"** (2 clips) rather
    than asserted on a guess, with the provenance reported.
  * **Budget anchored, not chosen** (`scripts/clock_budget_probe.py`): freeze
    latency vs the golden stroke over **n=49** — min −0.256, median −0.013,
    p90 +0.000, max **+0.019**. Bounds **[−0.5, +0.25]**; the late bound is
    ~13× the measured worst lateness, the early bound is the confirm path's
    own latency. A guard test fails if anyone tightens them to pass a clip.
    Negative is the norm and *is* #33 showing up as a number: the golden's
    `t` is the strike **confirm**, and the latch precedes it.
  * **What is now reported instead of asserted:** shot/settle counts and the
    **golden timeline**, on any clip the criterion can grade — a golden diff
    there is drift to read, not a defect, and it raises a `GoldenDrift`
    warning that pytest surfaces in its end-of-run summary plus a full report
    under `.failures/`. Still hard assertions: **scratch counts** (a Scratch
    verdict deliberately never moves the clock, #25/#26, so the clock grade
    cannot see it) and **everything on a clip with no clock ground truth**,
    where the golden remains the only gate. **No golden was regenerated** —
    on `stroke_tug_strike_0801` the golden is the only surviving record of
    when that stroke was, and it is what makes the clip gradeable at all.
  * **Item 2 settled by construction.** `stroke_tug_strike_0801` is **green**:
    its fire was lost at checkpoint 73, and the latch stops the clock
    **0.128 s earlier** than that fire ever did, then counts it — the clip was
    behaving correctly and the event grade was what was wrong about it.
    `parked_vote_strike_0801` is **red and stays red**: 3 counts against a
    recorded truth of 2, phantom freeze at 10.36 s mid-clip. Late is fine,
    extra is not. It is now the corpus's **only** red, down from two.
  * **`pickup_snatch_0801`**, surfaced by the new criterion and then resolved
    as a **clip artifact, not a live defect**: it counts a phantom shot, but
    its rearrangement is diffed against a **one-ball baseline** (opens
    mid-action, no quiet layout), so it reads `moved=1, appeared=6` and misses
    #33 Part A's placement shape. Two measurements settle it — every live
    ball-in-hand settle with an anchored baseline *does* match that shape and
    is refused, and across the 111 live settles carrying `appeared` **every
    RUNNING settle at ap≥5 is a caught break**; this clip's shape has never
    occurred live (bounded by three sessions, stated as such). The gate is
    therefore `ClockTimeline.rearrangement_is_anchored`, not the clip's name:
    `parked_vote` measures 7/7/7, stays graded, stays red. Baseline floor 3
    is read off the measured split (degenerate clips 1–2, honest ones 5+,
    nothing between) — the same empty-band anchoring #36's break floor uses.
  * **One replay, graded twice.** The clock grade first landed as a second
    parametrized pass and doubled the battery to 9:07; folding it into
    `test_corpus_clip` brought it back to **5:00 for 514 tests** — faster than
    the 5:11 before this work, while grading strictly more. Two probe bugs
    fixed on the way: the wrong default empty-table reference
    (`empty_table_0801` vs the battery's `empty_table`, which silently
    degraded detection on every clip outside `REFERENCES` — `tip_bump` read as
    a total miss), and a clip outliving one countdown grading the harness's
    synthetic start rather than the clip.
  * Item 3 (re-scope E7) deliberately untouched: it needs a failure list from
    this criterion, and one clip is not a list. E7 tuning still waits on its
    §13.6 trigger.

- **2026-08-11 (checkpoint 84 — the break starts its own clock, #36):** The
  head of the §13.0 Pareto closed: 15–25% of breaks never started the clock,
  and the evidence to catch them was already computed, already journaled, and
  discarded. E8's dark tier — the no-fire case, which IS this failure class —
  has journalled `break_dark_candidate` and done nothing since checkpoint 75.
  It now counts the break at the settle, where WNT §5 starts the 60 s window
  anyway. **No vision change was needed; the whole fix is one hub predicate.**
  * **The axis moved from `involved` to `appeared`, and `involved >= 8` is
    dead.** Steven recorded four 9-ball breaks (2026-08-11, one of them
    missed): all four measure `involved = 7`, and 9-ball *racking* measures 6
    — a one-ball gap that no absolute floor and no population-normalised
    version of one survives. The old floor would have missed every 9-ball
    break, silently, forever. `appeared` separates structurally rather than
    statistically: a break **un-merges** the racked pack, so balls that shared
    one unresolvable blob position acquire their own — which is exactly what
    an unmatched current ball is. Racking runs it backwards (singles merge
    into the pack) and lands in `vanished`. The axis therefore keys on the
    un-merge itself and is rack-size independent.
  * **Re-derived from every journal carrying the field** (111 settles across
    2026-08-07/08-10/08-11), IDLE domain — the only domain the tier acts in:
    9-ball breaks 5, 6, 6, 6; 8-ball breaks 6, 7, 10, 10, 12; **all 13
    non-break IDLE settles at 0** — racking gathers (mv=0 vn=6), placements
    to 1313 mm, a 2278 mm two-ball gather, cue positioning. Travel does not
    lift them off zero; only un-merging does. **The 1–4 band is empty.** Floor
    at 5, the lowest measured break, with the whole gap beneath it.
  * **`pickup_snatch_0801` priced** — the loudest hand event in the corpus and
    the clip that had to be cleared before lowering the floor below 8. It
    measures `appeared = 6`, inside the break population, and is **not** a
    counterexample: its baseline is a **single ball** (the clip opens
    mid-action with no quiet layout), as is every corpus break clip's (1–2),
    so the corpus anchors nothing on this axis and the live journals are the
    load-bearing record. Mechanism check, which is what makes it safe rather
    than convenient: a hand kills tracks by *occlusion* and they are reborn
    where they already were, so against an anchored baseline they match and
    land as moved/unchanged. `pickup_newborn_0801`, the one hand clip with an
    honest baseline (5 balls → 5), measures `appeared = 0` with 814 mm of
    travel. `settle_probe.py` now reports the baseline/settled population
    triple so a bare `appeared` can never again be read out of context.
  * **Residuals, named rather than solved.** A *soft* break that barely
    separates the pack reads low on `appeared` and stays missed — one press,
    as today; the settle journals its `appeared`, so the class is visible if
    it appears. Tearing a **built** rack apart by hand un-merges the pack and
    is genuinely break-shaped here — not in the live record, costs one New
    Rack press, the pre-accepted §13.4 worst case. A settle with **no
    anchored baseline** (`_settled_baseline is None`) yields an all-zero
    summary and cannot reach the tier at all — fail-closed, and the reason a
    cold replay of `flag_missed_2` reads `appeared = 0` where the live
    session read 12.
  * **Part 5 (`run_out`) refused on measurement — it cannot work as
    specified, and was not shipped.** Two facts: an ordinary shot never
    reaches the floor (77 settles at ap=0, then 9/3/1/1 at ap=1–4, **none**
    ≥5), so widening the tier would rescue no missed `run_out` opener; and
    `run_out` has `break_floor_mms=None`, so the IDLE gate never eats a fire
    there and the *fired* tier has nothing to rescue either. Against that,
    widening opens a hazard `match` does not have — fifteen balls thrown onto
    an empty table is by definition a large-`appeared` IDLE settle, and would
    start a 30 s countdown (no first-shot allowance) during setup, with a
    false-foul buzz possible. It cannot be priced: `appeared` exists in three
    journals, two never left `match`, and the third was in `run_out` for
    3m57s and produced **no IDLE settle at all**. Zero measured `run_out`
    IDLE settles on record. Gating on `_rack_pending` resolves nothing — it
    turns on **press order**, and inferring physics from press timing is the
    exact error corrected below. The press stays authoritative in `run_out`.
    **Steven closed this out 2026-08-11 on reading the measurement — `run_out`
    should not have been in this issue's scope in the first place**, so this
    is settled rather than deferred. If it is ever revisited, the predicate is
    the *opposite* of this one (opener = shot-shaped with `appeared` low), and
    it needs one recorded `run_out` session to anchor it.
  * **Part 3 verified, not built:** the pipeline's quiet-rearrangement
    backstop already synthesizes the settle for a break the engine sleeps
    through, and `break_dark_total_0806`'s corpus pin now asserts that settle
    clears the dark floor — the only channel by which a fully dark break can
    start the clock, since a backstop settle carries no mover evidence at
    all. Corroborator absence must never block the verdict (fail-open,
    unit-pinned), or the class silently reverts to missed.
  * **Corrects the false record (#33 Part C).** Checkpoint 83 claimed the
    2026-08-10 21:07:56 settle was racking that sat inside the break
    population, killing both discriminators. It is the **missed break**: its
    dump replays 10 pack tracks dying in ~0.9 s and 13 newborns flying to
    1534 mm. The identity was inferred from *when Steven pressed buttons
    afterwards*, in a dump that had already been replayed. **When ground
    truth decides a policy, verify it from the episode physics in a replay,
    never from press timing.** 83's bullet is marked superseded in place, not
    rewritten; `hub.py`'s "THE DARK TIER STAYS OFF" block is replaced with the
    corrected ground truth; `session-stats/SKILL.md` now states break
    auto-catch covers both tiers, with dark candidates relabelled historical.
    `report` still parses `break_dark_candidate` so pre-#36 journals read
    right; live journals never carry it again — a dark catch is a
    `break_adjudicated` with `tier="dark"`.
  * **Live contract Steven assesses by feel:** break a rack while vision
    misses the strike → the clock starts by itself at the settle, no press.
    Rack the balls → nothing starts. Kill switch and `no_clock` park all of
    it. Gate: ruff, `mypy --strict`, cold battery 511 tests / ~5 min; the two
    standing reds (`parked_vote_strike_0801`, `stroke_tug_strike_0801`, red
    since checkpoint 73, tracked in #34) unchanged and verified pre-existing.

- **2026-08-10 (checkpoint 83 — the shot becomes a settle-time decision, #33):**
  Ball-in-hand counted as shots and reset the clock (live 2026-08-10: 4/4
  journaled placement/retrieval settles counted — e.g. rack_shots 3→4 at
  20:57:13 on a hand lifting the cue from the pocket — and, the stage-0
  finding that reshaped Part B, placements also FIRE honest newborn
  ShotTakens: 1463 and 399 mm/s, 2 of ~6 ball-in-hand cycles counted through
  fires rather than compensation), while missed strikes self-healed only at
  the settle, ~5 s late. One resolver landed for both, the §13 hub-policy
  shape — vision emits evidence, the settle decides:
  * **Provisional freeze:** a RUNNING countdown stops at the engine's
    motion-episode latch (~0.25 s after the stroke — the hardened
    roster-mover latch, exported as `VisionPipeline.episode_active` and edge-
    dispatched by the run loop; the raw E9 motion bit still cannot freeze
    RUNNING, both behaviors unit-pinned side by side). Freeze latency is
    settled — the 0.2 s displacement window dominates; stride 1 is a
    measured null (plan §3.1 item 4). Gated by kill switch + `no_clock`.
  * **Deferred fires:** a RUNNING ShotTaken no longer commits — it joins the
    provisional and the settle counts exactly one shot per episode, fired or
    compensated; lateness within the episode is unbounded and can neither
    double-count nor lose a shot. GRACE and IDLE fires stay immediate;
    `push()` mid-flight absorbs the deferred fire into its declaration.
  * **The settle adjudicates:** refusal takes POSITIVE placement-shaped
    evidence — rearrangement `moved == 0 and involved == 1` (measured: all
    four live placements match; every measured real-shot settle has
    moved ≥ 1, down to hanger_pot's 33 mm — the axis is the moved count, no
    travel floor), no surviving episode mover ≥ `moving_enter_mm` (stun-pot
    guard, upgrade-only: real shots routinely end with zero alive movers via
    handoff rebirths — soft_shot, hanger_pot, every measured break), and no
    scratch candidate anchored inside the episode (thin-cut scratch; a
    retrieval's candidate predates its episode and does not protect it).
    Everything else — no summary, degraded summary, involved==0 (a
    clip-start artifact, never a measured placement), vanished ≥ 2 — fails
    open and counts. A refused settle reverts fire and freeze together:
    nothing counted, remaining restored to the tick; the engine's pickup
    discard (falling latch edge with no settle) reverts the same way.
  * **Part C measured off** — ⚠ **SUPERSEDED, and the claim below is false;
    see checkpoint 84 (#36).** 21:07:56 is not a racking settle, it is the
    missed break: its dump replays 10 pack tracks dying in ~0.9 s and 13
    newborns flying up to 1534 mm. The identity was inferred from *when
    Steven pressed buttons afterwards* instead of from the episode physics in
    a dump that had already been replayed — the triage error this project
    must not repeat. Kept verbatim below because the log is append-only:
    the E8 dark tier's gating data arrived and
    refused both axes — the 21:07:56 armed-rack racking settle (ground
    truth: the real break came ~15 s later, was missed, flag_missed_2,
    Restart rescue) reads involved=12 and settle-spread 1619 mm, both inside
    the break population (7–12 involved; 1103–1684 mm) — a mid-racking
    gather is table-wide, not pack-local. Blocker documented at the hub
    constants; `spread_mm` now journals on every settle so the record
    accumulates. Fired tier untouched (1/1 live catch this session).
  * Plumbing: vision side is additive evidence only — `SettleMover`
    (per-mover episode travel + liveness, exported before the settle clears
    episode state), `RearrangeSummary.spread_mm`, the latch-edge export; no
    golden moved; warm snapshots from pre-#33 commits refuse to load
    (fingerprint). Journal marks `shot_provisional` /
    `shot_provisional_reverted` / `compensation_suppressed`, plus
    `provisional`, `spread_mm` and mover fields on existing kinds; `report`
    tallies all three and reads the deferral correctly (an open strike
    record marks its settle seen — pre-#33 journals read unchanged, so
    accuracy rates stay comparable across the boundary); session-stats
    skill updated.
  Predicted live feel: ball-in-hand pauses the clock through the carry and
  resumes at the exact remaining — never resets, never counts; missed
  strikes freeze ~0.25 s after the stroke instead of bleeding ~5 s; phantom
  placement fires revert at their settle. Live in one commit chain, stage-by-
  stage measurements and commits on issue #33 (probe:
  `scripts/settle_probe.py`). Unit suite 429 green (+27 across stages);
  corpus cold battery 476 passed + the two checkpoint-73 standing reds,
  stash-verified unchanged; ruff + mypy --strict clean.

- **2026-08-07 (checkpoint 82 — control page: mobile-first Play/Setup tabs, #31):**
  `/control` was one long scroll with Match first, which on a phone put the
  clock readout astride the fold and every clock key under it — mid-rack,
  the moment the page gets grabbed in a hurry, Start needed a scroll before
  it could be tapped, and `:hover`-only feedback never fires on touch. The
  page is now two tabs matching its two jobs (picked from mockups, option B
  of three): **Play**, the default so the page is never on the wrong tab
  mid-rack — big centred readout, full-width rack-aware `Start N`, New Rack
  / Extension / Push / Pause at ≥54 px, both score bumpers with names +
  Fargo badges, race chip, all inside one-handed thumb reach — and
  **Setup**, touched once a match: names + Fargo lookup, race stepper,
  score reset, ruleset / mode, vision toggle. Mobile plumbing regardless of
  tab: fixed bottom tab bar padded by `env(safe-area-inset-bottom)`,
  `:active` press feedback, `touch-action: manipulation`, ≥48 px targets
  (HIG 44 pt / Material 48 dp). All JS behaviour carried over unchanged —
  Fargo search/pick flow, don't-clobber-while-editing input sync, deck
  dropdown re-sync, WS reconnect. No new endpoints; the Reset clock button
  is dropped from the page per the issue's design (the endpoint itself is
  untouched — the deck and API still have it).

- **2026-08-07 (checkpoint 81 — 4K60 presentation: sharp scoreboard, 60 fps feeds):**
  The scoreboard read pixelated on the wall because the TV is a 4K panel
  (macOS drives it at native 3840×2160@60) and the canvas was 1080p — the
  projector upscaled every browser-source pixel into a soft 2×2 blob. Steven
  raised the canvas to 3840×2160@60 and set Camo to 60 fps; this checkpoint
  is everything downstream of that. Overlay: the bar source becomes
  **2668×2160 native** — `?view=bar`'s px-exact geometry now scales itself
  to the source width (`fitBar()`, a body transform: not CSS `zoom`, whose
  viewport-unit behaviour shifted across CEF versions), so the type rasters
  at 4K. Found en route: OBS injects `body { margin: 0px auto }` into every
  browser source, which silently centres any body narrower than the
  viewport — the bar rendered 667 px off-seam until an inline `margin: 0`
  outranked it. Full-clock view + replay badge are vh/vw-sized and just got
  native-size sources; `setup_obs_replay.py` canvas constants follow.
  Camera path: the deck's canonical `run` argv gains `--fps 60` (CLI default
  stays 30). Publishing inline at 60 starved detection — **77 → 51 Hz**, a
  third of the cadence — so the display branch moved to its own thread
  (`DisplayWorker`, `display/syphon_out.py`): the grab loop hands over a
  ~0.3 ms Bayer copy, drop-oldest, Syphon server lives on the worker (its
  runloop answers discovery). Second thief: encoding the 4K60 canvas for the
  replay buffer cost another ~20 Hz, so Settings → Video keeps **output
  (scaled) resolution at 1920×1080** — the projector renders the canvas
  (wall stays native 4K) while recordings/replays encode 1080p60 like the
  old rig; buffer memory cap re-sized for 60 fps (est. 2.0 MB/s, measure and
  tighten). Steady state after both: **~72–74 Hz** detect with the buffer
  running (the last ~4 Hz vs the 77 baseline is OBS compositing 4K60 +
  Camo 60 — machine-wide, not in the loop). Verified through OBS's own
  renderer (`GetSourceScreenshot` of the bar at 2668×2160: plinth spans
  0..2667, text native-sharp) and journal heartbeats across the A/B
  (buffer-off ~70–73, buffer-at-1080p60 ~72–74). Session cycled through
  `SessionManager` so the deck key kept ownership (pidfile re-adopt).
  Startup drift warning note: today's ~73 px readings were **Steven's
  laptop sitting on the left rail** crossing the cloth/cushion boundary the
  rail-fit measures (rectified frame is otherwise square); the chronic
  10–19 px warn-level drift predates today and still deserves a §E2
  reference refresh some empty-table day. Docs: hardware.md §B1/§B3,
  README launch command, cli.md `--fps` row.

- **2026-08-07 (checkpoint 80 — solo panel: run_out/no_clock trade players for pace, #30):**
  The bar stops showing a two-player score in the two modes that have one
  player. Decided on mockups (Option D of four): in `run_out` / `no_clock` the
  `.scores` block stands down and the same 1044 px zone carries per-mode stat
  lockups — `SHOTS / LAST / AVG` in run_out (pace against the clock: wall time
  from each fresh countdown's start to the ShotTaken that ends it, dash until
  the first timed shot since the opening shot leaves IDLE untimed), `REPS /
  TIME` in a drill (fires while `no_clock`, approximate by design; elapsed
  since mode entry). Nothing else moves — plinth, gauge, divider and clock
  cell are identical across modes, so the Mode key changes words, never
  furniture, which was the continuity requirement. Fitment is #29's own rule
  (label 15 px at 55 over number 48 px at 96, pair centred on y=84) in the
  bar's two existing text voices; count stats reuse the score digits' pulse.
  Server: the `/ws` + `/api/state` payload gains a strictly-additive `solo`
  field (design.md §2.1) — `null` in match — composed in the hub, which owns
  all the wall-clock bookkeeping (`_pace_since_mono` opens on settle/start/
  restart/push and the E9 synthesized settle; consumed once per countdown so
  a mid-motion re-fire can't double-measure; IDLE settles stamp nothing, so
  placement can't time a phantom). Panel rescopes on mode switch and New
  Rack; kill switch outranks REPS like every vision consumer. New page + old
  server degrades to a quiet bar with the scoreboard correctly stood down.
  Verified: 402 unit tests (+3), mypy --strict clean; match-mode wall render
  screenshotted through OBS's own CEF (unchanged), solo states rendered
  offscreen through WKWebView against the live server's page with injected
  payloads (`scratchpad/snap_overlay.swift` pattern — see the overlay-check
  memory). Square/full views untouched.

- **2026-08-07 (checkpoint 79 — scoreboard fitment: centred lockups, Fargo labeled, #29):**
  Two defects Steven flagged after #27 put the rating on the bar. The vertical
  one: a race line lifted the *entire* name/score row to y=72 with `RACE TO N`
  hung at 122 under the score only, so the names read as centred in a top band
  with dead space beneath them ("two stacked containers"); no-race sat at 84 —
  the bar's vertical grammar changed with state and only one of the two states
  was actually centred. The rating itself was a 17 px span baseline-aligned to
  the 24 px name at 62% opacity: sagging ~2.5 px below the name's optical
  centre, unlabeled, and living inside the name's 400 px ellipsis container so
  a long name would truncate into its own rating. The fix is one rule: **every
  column of the bar is a lockup centred on the content zone's centre (y=84;
  the gauge owns the top 8 px), and the names never move.** The rating became
  its own labeled line under the name (`FARGO 471` — 11 px slate label, 18 px
  near-white number, shared baseline), the convention FargoRate's LMS overlays
  and Pro Billiard Series coverage use: a label attached to the number, not
  context. With a race, digits (cy 69) + race line (115) centre as a pair; a
  player with no rating shows the name line alone; with no ratings at all
  `.no-fargo` recentres both names to 84, which is exactly the pre-#27 layout.
  Design settled on mockups from the #23 pipeline
  (`~/Desktop/cueclock-mockups/render_fargo.py`, `fargo-fitment-*.png`);
  verified rendered through OBS's own CEF — cache-refresh +
  `GetSourceScreenshot` of the bar source, which needs no studio mode while
  the source is hot in program (detect hz dipped 77.8→53.9 during the reload
  and recovered to 76.9; table IDLE throughout; recording checked off first).
  `?view=bar` only; square/full views untouched; no API or payload change.

- **2026-08-07 (checkpoint 78 — post-scratch grace, #25):** The clock no longer
  runs against the walk to the ball return. Built on checkpoint 77's verdict
  split exactly as planned there: an un-refuted `ScratchCandidate` grants a 5 s
  hold over the fresh countdown (`ShotClock.hold` — RUNNING simply doesn't
  decrement, no new state, nothing on the overlay surface), `ScratchRefuted`
  releases it early — typically same-frame, so a refuted phantom never visibly
  touches the wall, which is what makes this safe to ship the day after the
  adjudicator and would have been indefensible before it (~29 candidates a
  session would each have frozen the clock). The confirmed `Scratch` verdict
  still never moves the clock (the 2026-07-27 decision survives for the
  verdict; the *candidate* is what drives policy, per §13). One grant per
  candidate at the first opportunity — candidate-after-settle holds
  immediately (scratch_shot_0801's order, candidate trails its settle by
  0.64 s), candidate-during-MOTION holds at the settle (shot_debug's order) —
  so a blocked census riding toward the 30 s horizon cannot also hold the
  next shot's clock. Every countdown-starting path (settle, start, restart,
  push, reset, new rack) clears the hold; straddling ticks resume exactly.
  Gated like every clock-moving event (kill switch, `no_clock` — the
  candidate stays journaled evidence either way). 5 s is the issue's spec,
  not a measurement — a felt number, flagged in the config comment for
  revisiting from the wall. Journal kinds (additive): `scratch_hold`
  (hold_s) / `scratch_hold_released`. Unit suite 399 green (+9: hold
  freeze/straddle/release/leak/IDLE pins on the state machine; grant orders,
  once-per-candidate, verdict-keeps-hold, kill-switch on the hub). Vision
  core untouched — no corpus impact, goldens unmoved.

- **2026-08-07 (checkpoint 77 — scratch becomes a two-stage verdict, #26):** The
  scratch channel over-counted ~6x (Steven's ground truth: ~5 real in a session
  that journaled 29) because a potted object ball mis-crowned as the cue passes
  every latch gate *honestly* — it dies near a pocket while moving and genuinely
  never reappears; the reappear watch acquits only hangers. The one unchecked
  fact: after a real scratch there is no cue on the table until the player
  replaces it; after a false one the real cue is sitting right there. Scratch is
  log-only, so the verdict now waits for a **whiteness census** (design.md §4.2,
  plan on issue #26). The watch's confirm emits `ScratchCandidate` (death spot,
  pocket, the dying crown's whiteness EMA + staleness — every live verdict
  journals its trigger evidence); refutation takes **positive evidence only** —
  a roster ball, judgeable, still + observed, whiteness EMA ≥ 0.72, born before
  the dead track's last observation → `ScratchRefuted` with the witness's
  position and reading; confirmation takes a **complete census** — quiet table,
  every live roster ball observed, none qualifying → `Scratch`; a census still
  incomplete 30 s after the latch confirms (absence of evidence must not
  acquit — `drill_shot_0801` hides the real cue from the census for a 41 s
  address). Anchors, all measured today: floor 0.72 sits mid-corridor between
  the corpus-wide sustained never-crowned rest-EMA ceiling 0.613 (a white-face-
  up stripe; pool p99 0.542 over 495 tracks) and the real cue's converged rest
  band ≥ ~0.83 (`~/Desktop/cueclock-accuracy-probes/scratch_floor_sweep.py`,
  cache-served, measuring the exact EMA the gate reads); horizon 30 s covers
  68/68 live candidates' trigger→settle tail (max 19.4 s, p90 5.5 s, all 15
  sessions). The **birth gate is mandatory** and held on real pixels:
  `scratch_replace`'s hand-placed replacement reads 0.880 within ~1 s, sits in
  the census, and the Scratch stands. The known leak is priced, not hidden: a
  real cue in deep top-rail shade reads 0.53 (`cluster_rest_shot_0806`) and
  cannot refute — that false candidate confirms, i.e. status quo; every gate
  leans toward not refuting because a failed refutation costs nothing new while
  a false one eats a real scratch. Plumbing: `BallState` gains `whiteness` /
  `color_age_s` (tracker EMA passthrough, behavior-neutral alone); journal
  kinds `vision_scratch_candidate` / `vision_scratch_refuted` beside the kept
  `vision_scratch` verdict (strictly additive); `report` prints candidates/
  refuted and stamps journals without candidate records `(upper bound: pre-#26
  journal)`; session-stats skill updated to match. Goldens: `Scratch.t` is now
  census completion — `shot_debug` 9.54 → 11.92 s (waits out the rolling
  6-ball), `scratch_replace` 5.03 → 8.46 s, `scratch_shot_0801` unchanged to
  the millisecond (table already quiet); all three confirm in-clip, counts
  unchanged, no other golden moved. The false population still has no corpus
  representative — that is *why* the battery stayed green through a 6x live
  over-count — so the measure of this checkpoint is the next 1–2 sessions'
  journals (prediction: a Hank-scale session's 29 raw drops to ≤8 confirmed,
  every refutation carrying auditable witness evidence). E7 (crown identity)
  remains the upstream cause; adjudication works regardless of why the crown
  was wrong. Unblocks #25 via "candidate un-refuted at its settle". Unit suite
  390 green (+6: refute/birth-gate/junk-witness/horizon pins, hub journal
  shapes, report tallies); corpus cold: the three scratch goldens moved as
  cited, the two checkpoint-73 standing reds (`parked_vote_strike_0801`,
  `stroke_tug_strike_0801`) unchanged, stash-verified pre-existing.

- **2026-08-06 (checkpoint 76 — FargoRate on the scorecard, #27):** Entering a
  player name on the control page now searches FargoRate and offers the matches
  in a pick-list under the input — name + location left, rating right,
  sub-200-robustness ratings marked `prov` (FargoRate's own established
  threshold), a skip row for unlisted players. Attaching is always a human tap,
  never automatic, even on a single result: the location column is the only
  thing that separates the fifty John Smiths the API returns. The rating rides
  the score payload (`fargo` per player), badges the control-page label, and
  sits one step quieter than the name on the bar scoreboard. Server side:
  `GET /api/fargo/search` proxies `dashboard.fargorate.com/api/indexsearch`
  (no CORS headers upstream, so the browser can't ask directly; httpx moved
  dev→main deps), 502 on upstream failure — an outage degrades to "no rating",
  never touches clock or score. `/api/score/config` gains `p1_fargo`/`p2_fargo`
  (int sets, null clears); a name change without a rating in the same update
  clears the old one — a rating belongs to a name. Persisted with the score,
  survives `score/reset` (next match, same players). Unit suite +5 (normalize,
  502, empty query, clear-on-rename, persistence); detection untouched, corpus
  not rerun. Not visually verified on the wall: OBS was closed overnight — the
  no-rating bar renders pixel-identical to before, so only the with-rating
  layout needs a glance. Follow-up (2026-08-07, live feedback): an explicit
  Fargo button beside each name field — the `change` trigger never fires for
  a name that is already right (Steven's persisted name retyped identically
  searched nothing while a fresh name beside it worked first try).
  Seven flag dumps triaged from today's two sessions. Three were checkpoint 74's
  own clips, already promoted but left in staging by a `cp`-not-`mv` — deleted.
  The four new ones divided exactly along §13.0's Pareto, and the fixes went
  where §13.1 says discrimination is cheap: the hub. (a) **E8, fired tier,
  live:** an armed rack's IDLE settle with break-scale rearrangement plus *any*
  ShotTaken fire since SET UP — including the sub-floor fires the break gate
  eats — counts the break and starts the 60 s window (`break_adjudicated`).
  Anchored: quiet-baseline rearrangement probe (`rearrange2_probe.py`) measures
  breaks at 7-12 balls involved, ordinary settles 1-2, pickups 1, corpus
  racking zero settles at all; the fired floor sits at 5, the fire must be
  within 15 s of its settle (longest measured roll-out 8.62 s). Journal mining:
  84 eaten IDLE fires across 31 sessions, 15 manually rescued (flag_missed_7's
  1981 mm/s among them); the dark-flight clip's own 443 mm/s fire + involved-12
  settle now adjudicates in unit test. (b) **E8, dark tier, shadow:** a no-fire
  break-scale settle only journals `break_dark_candidate` — the pack-resolve
  racking settle is appear-heavy exactly like a break and the corpus cannot
  price it (racking is settle-silent there; live sessions log ~3 IDLE settles
  each, rearrangement never recorded until now). Every settle now journals its
  rearrangement, so the next sessions ARE the measurement; flip the tier live
  when racking settles prove to stay under 8. (c) **E9 expiry hold:** vision
  exports an identity-free motion bit (observed raw ≥ settle floor on two
  consecutive reports — a coasting phantom cannot hold it); GRACE re-arms to a
  full window while it is set, so the buzzer can never sound through live
  flight (flag_missed_6 reached EXPIRED mid-roll-out at 19:20). (d) **E9
  frozen-clock watchdog:** MOTION with the bit low for 12 s (settle-latency
  p99 8.5 s, longest roll-out 8.62 s) synthesizes the lost settle,
  journal-marked `settle_synthesized` (14 operator rescues over 30 sessions).
  (e) **The settle backstop** (`break_dark_total_0806`, promoted from
  flag_missed_4; Steven's ground truth: a full 15-ball break, whole flight
  in-ring at ~70 Hz, zero engine events warm AND cold): the racked pack never
  confirms a single track, the scatter erupts as rosterless newborns, and the
  engine's per-track/roster channels all sleep. The pipeline now keeps an
  identity-free quiet-layout baseline (smoothed-speed calm + present-layout
  step; raw is sub-noise at 78 Hz and dead pack tracks coast at remembered
  ~0 speed — both lessons measured on this clip) and emits the BallsSettled
  the engine provably missed when the quiet layout rearranged ≥ 5 balls
  (same measured line as adjudication; a ball-in-hand replacement moves 1 and
  must never reset a live clock). Backstop settles carry a journal mark; the
  named test pins mechanism + break scale and is built to go red the day the
  tracker layer starts seeing this clip itself (§13.6 news, retire
  deliberately). (f) `report` grew the break class: auto-caught vs
  press-rescued breaks, dark candidates, synthesized settles — the instrument
  §13.0 faulted can no longer go blind to the head of the Pareto. flag_missed_5
  (mid-rack soft-roll miss, settle-rescued live) deleted: §13.0's self-healing
  class, E7's queue, no new mechanism. **Prediction (§13.9): presses at
  rack_shots 0 (13-22 per 30 sessions, 15-25% of racks) drop to ~0 for fired
  breaks; ~2 sessions of racking data price the dark tier. The mid-rack miss
  rate (3.14% over 1243 shots tonight) is NOT predicted to move — E8/E9 are
  felt-failure fixes, not strike-detection fixes.** Corpus 3 red / 453 green
  (the same three deliberate reds as checkpoint 74; break_dark_flight's red is
  now redundant with the hub adjudication test and should move to the hub per
  §13.5(3) — needs Steven's sign-off). Unit suite +12 (grace hold, watchdog,
  adjudication tiers, staleness, armed-fire clearing). Warm snapshots recorded
  before this checkpoint are fingerprint-refused (pipeline.py changed) —
  expected, all seven dumps were warm-replayed before the core was touched.

- **2026-08-06 (checkpoint 74 — the first annulus live session: three flagged
  misses, three mechanisms, two fixed):** Steven's 20-minute session flagged a break
  the clock sat through, a missed soft roll, and a missed cluster shot. Ring dumps
  replay bit-exact (the ring stores the exact frames the live loop processed —
  `dump_trace.py` warm-restores state.pkl), and triage found none of them was
  checkpoint 73's fault: each was an engine assumption the corpus never exercised.
  (a) `occluded_roll_0806`: a live address emergence is a stick-merge split whose
  centroid reads slow (first honest raw at age 0.179 s, crown at 0.31 s) — the 0.1 s
  newborn age gate, calibrated on clean 0.012 s corpus emergences, anti-selects for
  junk. New `newborn_dead_rest_max_age_s` (0.6) on the dead-rest fallback branches
  only, and only for tracks born into a rest story already broken (the still-stamp
  predates the birth) — `stroke_tug`'s fragment, which broke the story *by crowning*,
  stays refused. (b) `cluster_rest_shot_0806`: a ball resting in a cluster is
  merged-still forever (the blob is handed around its member tracks), so it can never
  show the roster's observed fraction — measured 176 s at ~0% observed through six
  settles — and when the struck cue's blob was re-associated onto its track,
  `_maybe_open_rested` refused the shot on roster membership alone. Cluster residency
  (old + present through sustained quiet) now admits; the own-rest dwell splits into
  presence-witnessed dwell + pixel-witnessed departure run (`_translate_run`,
  tip_bump's 666 ms dark push still refused). (c) The parked confirm gained a fast
  tier (`parked_fast_confirm_mms` 1200: two consecutive observed raws, so a single
  3179 mm/s hand-throw spike cannot certify) — in-flight tracks fragment before
  covering 600 mm (the cluster shot's cue fragmented into three tracks). (d) Rest and
  still-stamp upkeep no longer stops while a follow-ball candidate holds the slot
  (stroke_gear_veto_0802 had lost its fire to a squatting parked fragment freezing
  the stamp; byte-identical to golden again). (e) The break (`break_dark_flight_0806`,
  deliberate standing red): the cue crossed 1390 mm unobserved, its "departure" one
  stick-smeared 2441 mm/s step, and every re-qualification honestly refuses. An
  eruption restore fired it perfectly (10.139 s, corroborated 6503 mm/s) — and fired
  `pickup_snatch_0801` at 5780 through the identical mechanism. Twelve event-layer
  discriminators measured against the snatch; all twelve failed (ledger in the
  accuracy plan §12 and the tombstone at the kill-rule site). A pickup must never
  fire, so the restore is removed and the break class waits on tracker/detect-level
  evidence. Corpus 3 red / 441 green (the two old standing reds unchanged + the
  break); unit suite 370 green; `parked_vote_newborn_0805` regenerated (fires 0.33 s
  earlier at honest corroborated 2019 mm/s via the fast tier) with its named test
  updated to pin both confirm tiers.

- **2026-08-06 (checkpoint 73 — the annulus shipped, and the engine learned that
  identity moves):** the accuracy-plan §5.1 line landed end to end; corpus 13 red →
  2 red / 439 green, all iteration on the warm detect cache. (a) Cue whiteness moved
  to the perspective-scaled 0.55–0.83 ball-radius annulus (`_ring_whiteness`,
  `ReferenceModel.radius_px` from the calibration's local scale, `cue_min_whiteness`
  0.3 → 0.50 re-anchored on the stored-EMA population). (b) The traced phantom re-arm
  died: `BallState.present` (matched-vs-coasting, distinct from `observed`) guards
  rest *founding* — a coasting holder may refresh a rest but never found one.
  (c) The deep one: the annulus crowns a clean rebirth on its first confirmed frame,
  so the flag now moves mid-candidate routinely, and the flag-following candidate
  inherited displacement across the switch — one mechanism that both fired
  `pickup_snatch_0801` at 6108 mm/s and *was how the old code fired the breaks*.
  A debounced flag switch now kills the candidate; the continuation re-qualifies
  through the handoff path on the old holder's timeline, birth-anchored, with a
  departure-speed physics test (`handoff_speed_gain_max`: a struck ball cannot
  re-appear faster than its departure — power_shot 1.03× vs the snatch's 2.37×)
  standing in for the table-motion latch the departure itself raised. (d) "Parked"
  now means *uncorroborated* — an empty flag parks a birth candidate exactly like a
  flag parked elsewhere (the old condition dated from a floor that always crowned
  something). (e) `ShotTaken.speed_mms` requires two consecutive frames of agreement;
  single merged-centroid spikes had been published as shot speeds and several goldens
  had canonized them. 11 goldens regenerated with per-clip justification (8 speed-only,
  3 explained improvements — address_chatter now fires at the impulse and its named
  test passes again). Standing red, both understood and recorded in the accuracy plan
  §12: `parked_vote_strike_0801` (a practice-stroke fragment — Steven's hoodie/cue
  entering frame at EMA 0.70 — crowned on an empty flag; ground truth confirmed by
  Steven from a rendered clip; guard needs measurement, one candidate separator
  already measured dead) and `stroke_tug_strike_0801` (checkpoint 29's 0.01 s
  knife-edge, second cause untraced). Cue-track continuity not re-measured this
  session; run `continuity_probe.py` next.

- **2026-08-06 (checkpoint 72 — nine detection hypotheses dead, the phantom
  traced to identity, and goldens made readable):** a full accuracy session run
  against `~/Desktop/cueclock-accuracy-plan.md`. The headline is a category
  elimination, not a fix.
  - **Every hypothesis about *seeing better* is null.** Adding to the four
    already dead (cue stick as trigger / as veto / tip standoff, stride 1):
    ball recovery inside rejected components (**E1**, built end-to-end,
    corpus-green, cue-track continuity **33/47 → 33/47**, +0.57 ms/frame);
    "require a white core to back onto a ball detection" (single-core frames
    59.7% → 65.4%, but never-lost **23/47 → 13/47** and
    `occluded_address_0731` collapses to none = 0.53 — it re-imports the
    detection failure it was valuable for being independent of). Two more
    supposed defect classes turned out not to exist: late settles were already
    fixed by checkpoint 35 (**3.98% → 0.54%** over 10 s; the plan's "~4%,
    nobody has looked" was pre/post-fix journals pooled), and there is no
    false-scratch population (the 7 RUNNING-state scratches fire 1.74–5.40 s
    after their shot, inside the MOTION population's own range — ordinary
    scratches that confirmed a few tenths after the settle).
  - **Why E1 is inert, having been right about the mechanism.** `detect_balls`
    really does delete the addressed cue ball — measured over 3 270 address
    frames, 3.1% border rule, 0.9% extent, and **0.0%** with no component
    covering the ball. But the gaps are 0.04–0.20 s and `still_coast_s = 3.0`
    already absorbs them, so nothing downstream ever noticed.
  - **The real mechanism, traced to pixels.** On `phantom_flag_strike_0805`
    (Steven: a behind-the-back shot, shaft and shadow left lying across the
    vacated spot) the flag holder is **matched to nothing** from 0.13 s before
    the strike until a second after it, surviving purely on the 3 s
    still-coast, holding whiteness 0.735 that cannot decay because there are no
    observations to decay it — while the real cue ball, born moving at
    2602 mm/s, has its own whiteness frozen by `whiteness_freeze_mms` and can
    never clear 0.735 + the 0.05 switch margin. Three defects compound, and
    none of them is detection.
  - **And the score is sampled from the wrong place.** `patch_radius_px = 10`
    reads a 21×21 patch at the centroid — the specular cap, where every glossy
    ball under an overhead light is white. ("The palest yellow reads
    deceptively white" is a fact about the sample, not the yellow ball.) Radial
    profile of **148 718 detections** in 3 px rings: at **12–18 px** the cue
    ball reads p50 **0.911** against object balls' **0.036** — 25× separation,
    a 0.60 threshold keeping 80.3% and admitting 1.16%. Inside 6 px every ball
    is white; outside 18 px limb shading takes the cue ball itself to 0.45. Both
    bounds forced by data. **This is the live work** (plan §5.1, E6), with the
    hazard already measured: the naive stale-holder rule moves the flag
    correctly *and* mints a second `ShotTaken` at 11.329 s.
  - **Shipped: `tests/corpus/goldens.py` reports every difference, not the
    first** (one-at-a-time triage is a rerun loop at ~5 min a lap), renders
    golden vs actual as an aligned table with deltas in context, names the three
    causes a red golden can have, and writes the full report to
    `tests/goldens/.failures/<scenario>.txt` (gitignored, per scenario so xdist
    workers cannot collide) so a truncated capture never costs a rerun.
    `pyproject.toml` gains `-ra --tb=long`.
  - **Shipped: `track.py` — a recovered core must not compete for a moving
    track.** Found by *reading* a red battery instead of reverting it. Greedy
    pairing let a core lying nearer a rolling ball than its own detection steal
    the match, leaving the track to coast, so a mover read as still and
    `hanger_pot_0801` settled 1.7 s early — against a table still moving at
    ~100 mm/s (both configs agree the last supra-floor frame is t = 13.364).
    Two of that battery's three diffs were same-event/same-time/
    different-magnitude: invisible as a pass/fail count, which is the argument
    for goldens.
  - **Two methodological rules recorded in `tests/README.md`:** *calibrate a
    gate on the distribution it will actually see* (setting the recovery band
    from clean isolated balls rather than from cores inside rejected components
    would have put the floor at 0.4 and admitted 3× the clutter — a fused ball
    is not eroded at the fusion boundary, a free-standing one is); and *a red
    corpus is a diff to read, not a verdict*. Also why cold runs keep getting
    paid: the cache keys on `detect.py`'s bytes (correctly), but `track.py` and
    `events.py` are deliberately outside it — editing both in one loop is what
    forces cold runs. Land the detect-side change once, warm the cache, iterate
    downstream at ~21 s.
  - **E1 reverted, deliberately.** Continuity measured 33/47 in all three
    configurations — baseline, recovery with the association bug, and recovery
    with it fixed — with no flips and no shot-count changes. Correct, green and
    inert is not a reason to carry ~10% of the detect budget, and a
    `recover=False` flag would be dead code in a repo that will be
    open-sourced. `src/` therefore ends this session byte-identical to
    checkpoint 71: **no detection behaviour changed.** The implementation
    survives in history at `22137c5` / `cf2a2a0` if §5.1 ever needs a detection
    at the addressed ball — §4.11's white2 result suggests it will not.
  - **Corpus:** 441 passed cold with the change in, no golden changes; 370 unit
    tests green after the revert. ruff + mypy clean throughout.

- **2026-08-05 (checkpoint 71 — the other half of the error rate: the clock
  freezes when nothing was struck):** checkpoint 70 gave misses a denominator by
  reading back the clock's own compensation. The mirror error had none, and it
  turns out to be roughly half the size of the miss problem and completely
  unexamined.

  A phantom cannot be *proven* from the journal — the engine believes the
  phantom, so it confesses nothing, and positions are recorded only through the
  events they produced. What can be measured is a freeze whose surroundings are
  physically wrong for a shot, and `server/report.py` now reports four such
  symptoms: **no roll-out** (froze and released with nothing in between),
  **snap-back** (froze again before a ball could have been addressed),
  **split freeze** (a second strike arrived past the longest roll-out ever
  measured, so the freeze and the roll-out belong to different strikes) and
  **left frozen** (never released; an operator key had to rescue the clock).

  Across all 764 shots: **13 phantom candidates (1.70%)**, plus **8 split
  freezes** and — the number worth reading twice — **14 clocks that froze and
  never released on their own.** Against a 3.27% miss rate, the clock is doing
  the wrong thing appreciably more often than the miss number alone says.

  The last two are counted apart because each is **shared with a different
  failure and the journal cannot say which it saw**: a split freeze reads
  identically to a real shot whose *settle* was missed, and a left-frozen clock
  reads identically to SET UP pressed over a live roll-out.

  Constants anchored, as ever: `PHANTOM_ADDRESS_S` 2.5 s (below this there was
  no time to address a ball) and `PHANTOM_LEAD_S` 8.62 s (the longest roll-out
  ever measured, 2026-08-03 session). **Read the count as a comparator, not an
  absolute** — recompute it before and after a detection change and watch it
  move. As an absolute it is only an upper bound on those four symptoms, and it
  is neither a floor nor a ceiling: the one phantom in this history ever
  verified against frames (2026-08-01 11:14:49, mid-racking, 19 s before the
  break it then swallowed — checkpoint 28) is a *split freeze*, and a phantom
  that freezes two seconds before the real strike is journal-indistinguishable
  from a clean shot.

  Validation is thin and stated as such: the retired "I shot" key marked 29
  human-confirmed strikes across two 2026-08-03 sessions and the candidate rules
  fire on **none** of them — zero false positives out of 29, which is too small
  to bound the rate (a proxy wrong every time it fired would still clear 29
  shots 61% of the time). The deck's PHANTOM reason key has been live since
  2026-08-01 and has **never been pressed**, against 10 MISSED presses in the
  same span: that bounds the *noticeable* phantom near zero and corroborates
  nothing below it, since every candidate released the clock within 3 s — a blip
  on the wall, not something worth walking over to flag.

  Reader-only: no detection code touched, nothing runs live, and a journal
  reader cannot move the clock. 370 unit tests green, ruff clean, mypy --strict
  clean. Written to judge an OR-path trade that was then measured and abandoned
  (see `~/Desktop/cueclock-accuracy-plan.md` §4a); kept because phantoms are one
  of only two numbers that define accuracy and half of what it found had never
  been counted at all.

- **2026-08-05 (checkpoint 70 — the denominator was already in the journal; the
  key that asked for it is retired):** Steven, after nine days of clip triage:
  *"I'm still seeing accuracy issues almost every rack... are we gonna keep
  chasing down clip after clip until number of clips approaches infinity?"* He
  is not imagining it, and for the first time that is a measurement rather than
  an argument.

  **The number: 764 shots, 25 missed, 3.27% — a miss every 2.2 racks, flat
  across 2026-08-01 → 08-06** (3.32 / 5.13 / 2.84 / 0.00 / 2.48 / 3.85% by day).
  Over the same nine days `vision/events.py` grew **257 → 1842 lines** (7.2x, 51
  tuned constants) and the corpus went 17 → 57 scenarios. Seven times the code,
  no movement. Six of the nine detection checkpoints in that stretch (50, 51,
  56, 57, 66, and 68's own predecessor) were repairing damage done by earlier
  gates rather than covering new phenomena — which is the plateau mechanism
  itself: new failure classes arriving about as fast as clips retire them.

  **Nothing new was instrumented to learn this.** `ShotClock.balls_settled`
  increments `rack_shots` **only** when no ShotTaken arrived since the previous
  settle — the missed-strike compensation added 2026-07-31 for correctness (a
  short count replayed the WNT §5 60 s window), never for measurement. Every
  journal record carries that count. So a `vision_balls_settled` record whose
  `rack_shots` exceeds the preceding record's *is the engine confessing it
  missed a strike*, and it has been writing one on every miss since the day it
  landed. 25 confessions, sitting in `logs/`, unread. Settles recorded in
  `IDLE` are racking motion the clock correctly ignored and are excluded — 69 of
  them across the history, and counting them would have padded the denominator
  and made the number look better than the table does.

  Two detectors run per shot and only one is fragile: *"he hit the ball"* must
  identify the cue ball through the moment the stick and hand are one blob with
  it; *"everything has stopped"* needs no identity at all. The second is
  reliable enough to serve as ground truth for the first, which is the whole
  finding — and the reason §6 of the plan proposes moving the clock's freeze off
  the identity argument entirely.

  **`cueclock report` is rebuilt around that signal** (`server/report.py`, whole
  module replaced): `report` (newest session), `--journal`, `--since 7d`,
  `--all`. Derived, never accumulated — nothing runs live, so the rate survives
  a crash or a `kill -9`, a better reader retroactively improves every session on
  disk, and the two 2026-08-03 sessions that carry presses are read like any
  other. `run` prints the single-session report on clean shutdown (so holding the
  deck's Session key ends with the number on screen) and appends a
  `session_summary` record. Also reported, because the redesign will need them:
  phantom **candidates** (shot→settle under 1 s against a measured 2.27–8.62 s
  roll-out — a review candidate, never a verdict, since the journal cannot prove
  a phantom), misses that coincided with a starved detect loop (<50 Hz: 1 of the
  12 checkable ones, so contention is *not* the story), and the scratch rate —
  **47 in 764 shots, 1 per 16**, which is almost certainly mostly false and has
  been invisible because scratch is journal-only in v1.

  **The "I shot" key (#19) is removed**, three days after it shipped. One
  unconditional key with no judgement attached was the right design and still
  the wrong mechanism: it was pressed in **2 sessions out of 29**, because a key
  the player must remember on every shot is a key that stops being pressed.
  `POST /api/groundtruth` 404s on purpose (the `/score` rule — a deck on an old
  build must fail visibly), the press-pairing reader and its three tuned windows
  are gone, and deck cell (3,5) is empty with a test pinning it empty, so nothing
  quietly reintroduces a table-side ask. Layout and `hardware.md` Part C moved in
  the same commit, as required.

  **The triage-clips skill is rewritten around the premise change.** Step 0 now
  starts from `report`, not from the clip, and forbids answering a strategy
  question by opening one. Step 7's default outcome is **delete** — promotion
  requires naming the nearest existing scenario and why it fails to cover the
  mechanism. Step 7b redefines a fix: *a change is a fix if it removes rules or
  corrects a cause upstream; a threshold fitted to the clip in hand is not*, and
  if that is the only change available, write the mechanism down and stop.
  Anchoring a constant to a measurement is now stated as necessary and **no
  longer sufficient** — all 51 were anchored. New Step 10 sets the evidence bar:
  a green corpus proves no regression, not improvement; the test set is Steven's
  ~130 uncurated shots a night, with the arithmetic for how long a claim takes to
  confirm (~630 shots ≈ 5 sessions for 3.27% → 1%; ~2500 ≈ 19 sessions for
  3.27% → 2%).

  Full plan, including the part not built: `~/Desktop/cueclock-accuracy-plan.md`.
  **No detection code was touched** — this checkpoint is instrument and process
  only, so the corpus battery is not implicated. 370 unit tests green, ruff clean,
  mypy --strict clean. The shipped reader reproduces the same 764 / 25 / 3.27%
  the throwaway analysis found, which is the point of shipping it: the reading is
  now pinned by tests rather than re-derived (and re-derived differently) each
  time.

  Two things corrected on the record, both mine: the miss rate is **not** a
  motion-blur or lighting problem — the room is a Diamond under a full perimeter
  LED frame, the 8x shortfall in `hardware.md` §A6 was already fully compensated
  at bring-up (iris ~2 stops open + x2 gain), and the misses actually root-caused
  in checkpoint 66 were at 1927–2106 mm/s where blur is ~12 mm on a 57 mm ball.
  And 99.9% was an overclaim; 99% (one miss per 7 racks) is the honest target,
  with 99.9% needing ~3000 clean shots just to demonstrate.

- **2026-08-05 (checkpoint 69 — the button that recovered the clock but not the
  rack):** Steven, on WNT match mode: the break doesn't register, he presses
  Start 60 to get going, takes a shot — and gets 60 seconds again for the next
  one. Detection is not the bug here; the recovery key is.

  `current_base()` hands out the WNT §5 first-shot 60 s while the rack has
  counted fewer than two shots, the break being shot 1. A missed break
  increments nothing: the strike is refused by the break floor in IDLE and the
  settle is a no-op in IDLE (checkpoint 26's rule, and the right one — racking
  produces genuine settles). `restart()` then set the countdown and started it
  **without touching `_rack_shots`**, so the rack sat one shot short and the
  60 s window shifted one shot late for the rest of it. Journal-pinned twice:
  `session-20260805-194410` at 19:59:56 (break gated at 2251 mm/s) and
  `session-20260806-015007` at 01:53:03 (no `vision_shot_taken` at all — a bare
  IDLE settle, which he flagged), both `cmd_restart rack_shots=0 rem=60.0`
  followed by a settle at `rem=60.0` one shot later.

  Fixed where the meaning is: pressing Start with no shot counted in a
  rack-scoped mode **counts the break** (`_rack_shots = 1`). Out of a fresh rack
  the clock parks in IDLE precisely so the untimed break can happen, so a press
  from there can only mean the break already did. The 60 the face promises is
  unchanged — `current_base()` still reads 60 at one shot — and only the shot
  after it correctly drops to 30. `run_out` is untouched (no rack, no break to
  count), and a mid-rack press still leaves the count alone.

  Considered and dropped: gating this on evidence (a settle seen while parked)
  rather than the press itself. Across all 20 `cmd_restart` presses in the
  journals, 8 of the 9 in-rack WNT presses carried that evidence and one didn't
  — a press one second after cycling to WNT with no rack behind it, which the
  simpler rule mis-books as a break. Steven's call, and the right one: the
  button means what it says, and a 30 that should be 60 is one Push key away.

- **2026-08-05 (checkpoint 68 — the break that outran its own budget):** Steven
  came down to test checkpoint 66 and the very first break was missed. One
  `flag_missed` dump, ground truth his: a **normal-power break**. The journal is
  unambiguous — the clock sat IDLE at 60.0 s for the whole rack, and there is no
  `vision_shot_taken` record at all, not even a `vision_shot_taken_ignored_idle`,
  so `modes.py`'s break gate never got a vote to reject. He pressed Restart to
  start the match, and every one of the 14 shots after that was detected. The
  detect loop ran 77–78 Hz throughout with uniform 12.8 ms frame gaps in the
  ring, so nothing here is contention. Warm and cold both reproduce.

  Not a checkpoint 66 regression: 66's shelve is what let the handoff run at all
  here. It ran, and was refused by a gate that predates it.

  **A distance budget a fast ball spends while invisible.** The flag-borne open
  is correct — the cue dwells 4.67 s, steps off its rest at 4.724 s at
  3720 mm/s, a candidate opens. It is then never observed again, and the track
  carrying it onward is born three frames later **320 mm from the rest at
  7221 mm/s**. Every handoff gate passes at that frame — born after the cue went
  dark, 0.038 s step gap, quiet table, no mover near the rest — and
  `strike_handoff_radius_mm` refuses it by 70 mm. That radius was anchored on a
  2281 mm/s emergence (`flag_missed_2`, 193 mm); a break simply outruns it
  before anything can see the ball. Distance is the wrong currency. Inside the
  occlusion the honest bound is physics — the ball cannot be further than its
  own speed carried it while unseen — so that becomes the reach, with
  `handoff_reach_margin` (1.5) covering what the product under-counts. Measured
  `d_rest/(raw × dark)` at the rescue frame of every real break in the corpus:
  1.14, 1.16, 1.18.

  The window is `strike_step_max_s` — **no new constant**, and the corpus says
  it is the right one. Across every handoff impulse the radius currently
  refuses, cue-dark time splits the population with nothing in between: the
  three real breaks sit at 0.038–0.102 s, and the nearest thing that must stay
  refused is 0.742 s (`parked_vote_strike_0801`), then 2.9 s
  (`ball_in_hand_pickup_0801`) and 6.5 s (`parked_vote_newborn_0805`). Measured
  from `_cue_last_obs_t` alone, deliberately *not* the `max(...)` the step gate
  uses — a flag parked on some other still ball keeps that stamp fresh
  indefinitely, which is exactly how those long-dark rows get that far.

  **And the floor that reads a rack as a hand.** Widening the reach alone
  changed nothing. The candidate opened, travelled 1483 mm, and was killed at
  exactly `handoff_confirm_min_s` by the observation floor — the carried-ball
  gate that asks whether a hand keeps losing this ball. On a break the answer is
  a false yes: by 0.20 s the cue has reached the rack and the tracker loses it
  inside the scatter it caused. The whole population reaching that floor,
  corpus-wide, by observed fraction at the gate frame: 0.31 `shaft_adjust`,
  **0.56 this break**, 0.64 `cue_grab_0801`, 0.69 `break_underread_0801` and
  `pickup_newborn_0801`, 0.81 `break_0801`, 1.00 ×5. So the fraction cannot be
  loosened — the real break reads *worse* than the grab and the pickup it exists
  to reject — and neither can the unbroken observed run off the open frame
  (7 frames against 9 for the grab, 11 for the pickup). Speed separates, and by
  a wide margin: everything that must stay rejected tops out at 2968 mm/s, every
  break starts at 7371. A hand cannot move a ball that fast — the quickest
  hand-driven ball measured anywhere in the corpus is `scratch_replace`'s throw
  at 3179 mm/s (checkpoint 66) — and at break speed occlusion is expected rather
  than suspicious. `handoff_obs_floor_exempt_mms` = 5000, 1.6× over that throw
  and 1.5× under the slowest real break.

  The break now fires at 4.967 s, 0.243 s after the stroke, reading 9923 mm/s —
  4× the IDLE break floor, so the clock would have started.

  **It also closes a gap that had been pinned as unfixable.**
  `break_underread_0801` — the 2026-08-01 break the live gate ate at 705 mm/s —
  carried `shots` unasserted with the comment "a cold replay never confirms this
  break at all (the streak dies before the candidate can follow it)". The streak
  did not die: it outran the radius (born 305 mm out at 6977 mm/s) and was then
  killed a second time by the observation floor. It now fires at 1.075 s reading
  9469 mm/s, and its `EXPECTED` is asserted hard. That is an independent second
  case for the true class, recorded a week before the failure that explained it.

  Scenario added: `break_outrun_0805` (1 shot, 0 scratches, 1 settle), with
  bespoke tests pinning the fire to the stroke and the reported speed above the
  break floor, plus the same speed assertion for `break_underread_0801`. Four
  unit tests, each paired with its mirror so the pin lands on the discriminator:
  the 320 mm emergence fires at 7200 mm/s and is still refused at 3000, and the
  flickering emergence fires at 6000 mm/s and is still refused at 900. Both
  "must fire" tests verified failing on the pre-fix engine.

  Goldens: **two moved, both breaks, no other clip's timeline shifted.**
  `break_underread_0801` gained its ShotTaken. `break_0801` fires 45 ms later
  (12.974 → 13.019) and anchors on ball 4 instead of ball 0, because the handoff
  now claims the slot the shelve released; the reported speed is **identical**
  (7371.4193 mm/s), the settle is unchanged, and 45 ms is nothing against a 30 s
  clock. Accepted rather than special-cased — telling a doomed candidate from a
  blinking one at that instant is precisely what checkpoint 66 established
  cannot be done.

  **Honest limit:** the reach margin and the speed exemption are both anchored
  on three real breaks. A break slow enough to read under 5000 mm/s at the gate
  frame — a deliberately soft break — would still be refused by the observation
  floor if its cue vanished into the rack. Nothing in the corpus is that shot
  yet. 441 tests pass cold in 5:56.

- **2026-08-05 (checkpoint 67 — the rack you just played, not the one you just
  racked):** first live use of the held Replay key (checkpoint 65) failed, and
  everything about it worked except the anchor. Steven ran a rack out, pressed
  SET UP, held Replay a couple of seconds later, and the TV cut to an empty
  already-run-out table for about a second and came straight back.

  The deck log names it exactly: `setup: POST /api/rack -> 200` and then
  `replay: rack 2.0s ago, 598.5s file, cue 595.1s, 3.4s on air`. A correct
  hold (span `RACK`), a correct 10-minute buffer (598.5 s of file, the
  checkpoint-65 addendum's setting proving itself), a seek that landed, a
  badge, and a clean return — cueing 595.1 s into a 598.5 s file because the
  anchor said the rack was 2.0 s old. The journal has the rack Steven wanted
  breaking ~266 s before the press and running 14 shots to `cmd_new_rack` at
  mono 628378.88, all of it sitting comfortably inside the buffer.

  **The bug is the SET UP stamp, and it was in the press the feature exists
  for.** Checkpoint 65 gave the rack anchor two stamps — the SET UP press
  first, superseded by the next shot (the break) — reasoning that a rack replay
  must not open on a minute of building a triangle. The break half is right.
  The press half is not just imprecise but inverted: between racking and
  breaking there is *by definition* nothing on the table to watch, so pointing
  the anchor at the fresh rack guarantees the empty-table replay, and it does it
  at the exact moment a player asks for a rack replay. Nobody holds Replay
  mid-rack to see the balls they have not hit yet; they hold it after running
  out, which is after SET UP.

  So `Hub.new_rack` now only **arms** the next `shot_taken` to become the
  anchor. It stamps nothing and retires nothing: until the new rack is broken,
  the rack worth replaying is the one that just ended, and the new break
  releases it. `_rack_mono` is gone, `rack_age()` reads one stamp, and null now
  means one thing (no break yet this session) instead of two. The deck's
  fallback for null and for an anchor older than the buffer is unchanged — play
  as much of the buffer as there is.

  No deck-side or `replay.py` change: `Span`, the cue-point arithmetic, the
  lifted ceiling and the tap/hold routing all behaved. The two `test_server.py`
  tests that asserted the old semantics are replaced by two that assert the new
  ones, the second named for this failure; both verified failing against the
  pre-fix hub. 366 unit tests green. No detection code touched, so the corpus
  battery is not implicated.

  Log-line note for next time: `replay:` prints the anchor age precisely because
  a wrong anchor is invisible from the wall — an empty-table replay looks like a
  broken seek, and it took one line of the deck log to tell those apart.

- **2026-08-05 (checkpoint 66 — the candidate slot that shut out the rescue, and
  a vote parked in plain sight):** three `flag_missed` dumps from one afternoon
  session, three real shots vision never saw — a hard/long shot at 15:49 and two
  normal-pace pots at 16:16, ground truth Steven's. The detect loop ran 77–78 Hz
  right up to every press, so none of these is the starvation artifact. All
  three journal the same signature: a `vision_balls_settled` with no
  `vision_shot_taken` before it and `rack_shots` incremented anyway, which is
  `clock/state.py`'s own missed-strike compensation firing — the engine marking
  that a shot must have happened because balls moved and stopped. Live, the
  clock never froze: it counted down through the balls rolling and reset on the
  settle. Warm and cold both reproduced all three, so checkpoint 64's handoff
  (shipped that morning, and running in this very session) does not cover them.

  **The slot, not the gates (`phantom_candidate_block_0805`,
  `late_emergence_strike_0805`).** The flag-borne open is *correct* at the
  impulse frame — the cue dwells 11.1 s, steps off its rest at 2106 mm/s, and a
  candidate opens. Then the tracker loses the ball: the struck cue outruns the
  100 mm association gate and founds a **new** track while the candidate goes on
  advancing on extrapolated positions that can never reach the confirm. Nothing
  is wrong with any gate; what blinds the engine is that a doomed candidate
  **holds the single candidate slot** through exactly the window the identity
  scans needed it. Measured: the struck ball sat 224 mm from the rest at
  11.284 s, inside `strike_handoff_radius_mm` and passing every handoff gate,
  but the scan is guarded by `self._candidate is None`. The candidate died at
  11.355 s on the feasibility check — 21.1 mm travelled against the 22.0 mm it
  still needed, **dead by 0.9 mm**, computed on a phantom the tracker had
  re-stamped still — and by then the real ball was 469 mm out. On
  `late_emergence_strike_0805` the candidate did not even die: it rode a
  coasting phantom for its full 0.30 s window while the struck ball sat 175 mm
  from the rest, and the scan first ran at 15.726 s with everything ≥292 mm away.

  So a candidate whose target is unobserved this frame is **shelved**, and the
  slot goes to the handoff — the one claimant carrying chain of custody from the
  cue's own rest. Shelved, not dropped: if no scan takes it the candidate is
  handed straight back, so a real strike whose cue merely blinks keeps its
  window. Releasing the slot to the *birth* path as well was measured and
  rejected — it cost `break_0801` its flag-borne fire (ball 0 at 7371 mm/s →
  ball 4 at 6060, newborn, 51 ms early). Dark-run length cannot separate those
  (4 frames on `break_0801` against 3 on the rescue); the anchor can.

  `late_emergence_strike_0805` needed one thing more. Its abandoned cue track
  drifted rather than being held still, so `_cue_last_still_t` — which only
  advances while the flag-holder is *reported still* — froze, and the handoff's
  ramp test began billing the occlusion: 0.128 s against the frozen stamp,
  **refused by 8 ms**, where the cue had actually been seen 0.109 s earlier. The
  gate now measures from the last observation. It cannot re-admit a ramp: the
  ordering test already requires the track to be born after the cue went dark,
  so what the window bounds is how long after the cue vanished its continuation
  may surface. Checkpoint 64's own phantom was *held* still through merged
  blobs, so its stamp stayed fresh and the `max` leaves it untouched.

  **A vote parked in plain sight (`parked_vote_newborn_0805`).** Different
  blindness, same session. The whiteness vote sat on an **observed, still** ball
  — the object ball the cue was about to pot — while the struck cue emerged from
  the address occlusion as a track born at 11.310 s already doing 1752 mm/s.
  Both identity paths were gated on `cue_quiet` (no *observed* ball holds the
  flag), so with the holder observed the birth path was skipped outright, and
  `_maybe_open_rested` cannot cover it: it demands roster membership, and the
  roster admits at settle, which a ball born mid-shot has never had. A vote
  parked on an observed ball is the same blindness as one parked on a dark ball.

  What the birth path loses with the holder observed is the vote's
  corroboration, so it has to earn the identity from the ball's own behaviour.
  **No standard signal separates the classes** — instrumented over every
  candidate this configuration admits corpus-wide, impulse speed overlaps badly
  (the true strike opens at 1927 mm/s; `scratch_replace`'s hand-throw reads
  **3179**), and so do rest dwell and distance from the holder. Travel does, and
  by physics: a struck cue crosses the table, a stick fragment or a hand-moved
  ball does not. Measured over 1 s from the birth spot — real strike **962 mm**,
  against 375 (`address_chatter_miss_0801`), 328/382 and 328×4
  (`scratch_replace`), 267/277 (`stroke_tug_strike_0801`) and 256/202
  (`parked_vote_strike_0801`). `parked_confirm_travel_mm` **600** is the
  geometric middle of that 2.5× gap: 1.6× above the worst false, 1.6× below the
  true one. The real strike crosses it 0.435 s after the open, which sets
  `parked_confirm_window_s` **0.60**. Ordinary strikes are untouched — only the
  weakest identity claim in the engine waits, and 0.45 s is nothing on a 30 s
  clock. Because that claim is the weakest *and* holds the slot twice as long as
  anything else, the flag-borne path may pre-empt it: without that, a merged
  stroke blob parks the slot and swallows the real shot behind it (caught by
  `test_stroke_gear_behind_the_departing_cue_does_not_reject_strike`, where the
  gear sweeps in at 900 mm/s and the cue then departs at 1200).

  Honest limit: the true class here is **n=1**. A short pot from an occluded
  address with a parked vote — under 600 mm of cue travel — would still be
  missed, and the corpus has no example either way. That is a real hole, now
  pinned rather than assumed.

  Corpus: three scenarios promoted, all warm-fail/cold-fail before the change.
  **57 scenarios, 433 tests pass cold in 5:49.** Three new goldens and — the
  result worth reading — **no existing golden moved**. Three unit tests added,
  each verified to fail on the pre-fix engine and pass on the post-fix one, the
  travel gate paired with its mirror (a 300 mm traveller that must stay silent,
  same setup, same impulse, same birth conjunction — only the distance differs).

- **2026-08-05 (checkpoint 65 addendum — ten minutes of buffer, and a memory cap
  that can't bind first):** `BUFFER_TARGET_S` 300 → **600 s**, at Steven's ask:
  *just in case there's a safety battle and a long rack*. The buffer is sized
  for the worst rack rather than the average one, because a rack that outruns it
  doesn't fail — the cue point clamps to the file's start and the replay quietly
  begins mid-rack, which is the failure here that looks most like the feature
  working, and a safety battle is exactly the rack worth watching back.

  Cost is ~400 MB held in OBS and **~400 MB written to `~/Movies` per press**
  (0.55–0.66 MB/s measured). Nothing prunes that directory; twenty presses is
  8 GB.

  The setup script now **sets** `RecRBSize` from the time target (×1.5 headroom
  for a busier scene — 594 MB ≈ 900 s) instead of only warning about it. OBS
  honours whichever of the two limits binds first and says nothing, so a memory
  cap left at a value that suited the old length is how a ten-minute buffer
  silently becomes a seven-minute one — and that reads as a bug in the seek
  rather than as a setting. Applied live and the buffer restarted onto it.

  Playback speed deliberately left at 1.0 (Steven: *don't make it 2x*). #17
  keeps the slow-mo/stinger work; this feature's job is the saved footage.

- **2026-08-05 (checkpoint 65 — one Replay key, two spans: tap the shot, hold
  the rack):** Steven asked for a choice at the Replay key — the last shot, or
  the whole rack. It is one key still, on the tap/hold rule the score keys
  already use, because the common press has to stay one press with nothing to
  read (the alternative, a Flag-style chooser page, puts either a second press
  or a 1.5 s auto-default in front of every replay).

  **Almost none of the work was the arithmetic.** `cue_point` was already
  generic — `cursor = duration − anchor_age + display_lag − lead` does not care
  what the anchor is — so a rack replay is the same call against a second age,
  and `Span` (whose value *is* the field name it reads) is the whole branch.
  The one behavioural difference: `MAX_PLAY_S` still caps a tap at 20 s, and a
  hold lifts the cap to the file's own length, because "play the rack" is
  exactly the request to lift it.

  **The blocker was the buffer, not the code.** `RecRBTime` was 20 s, which has
  no rack in it. Now 300 s, written into the active profile by
  `setup_obs_replay.py --buffer-seconds` rather than clicked into Settings, so
  the length the deck assumes and the length OBS keeps cannot drift. Measured
  2026-08-05 (`ffprobe` on real saves: 18.4 s / 12.2 MB ⇒ 5.3 Mbps ≈ 0.66 MB/s):
  five minutes is ~200 MB of RAM in OBS, and every press now writes ~200 MB to
  `~/Movies` instead of 12 MB — hence `SAVE_TIMEOUT_S` 3 → 8 s. OBS also caps
  the buffer by memory, silently, whichever binds first, so the script reports
  `RecRBSize` in *seconds*: a buffer shorter than it claims would otherwise read
  as a bug in the seek.

  **The rack anchor is the break, not the racking.** `Hub` stamps the SET UP
  press and lets the next shot supersede it, ahead of the same gates the shot
  anchor already sits ahead of — so a rack replay opens on the break, and a
  `no_clock` drill (no SET UP key out there) anchors on its first shot. Null
  falls back to "as much of the buffer as there is", which is what a stale shot
  anchor already did.

  `GET /api/last_shot` → **`GET /api/replay_anchors`** (`{shot_age_s,
  rack_age_s}`): renamed rather than grown a field, one round trip, both ages
  measured against the same instant. The old URL 404s on purpose — a deck left
  on the old build should fail visibly, not quietly cue every replay to the top
  of the buffer (`/score`'s rule, checkpoint 61).

  Face grammar amended: a key carrying a hold now names it on its second line
  (`REPLAY` / `HOLD RACK`, beside Session's `ON` / `HOLD 1s`), because a gesture
  nothing states is a feature only its author knows about. A mature hold fires
  under the finger and says `RACK…`; a tap fires on release. The badge stops
  counting `LIVE IN n` above 20 s — over a five-minute replay that number is one
  nobody can use.

  Deferred, deliberately: **2× rack playback.** Fast-forward is honest at 30 fps
  and it is one per-press `speed_percent` away, but nobody has verified OBS
  honours that on a hardware-decoded media source — and if it ignores it,
  `play_s` is half the real airtime and the deck cuts back to live mid-rack.

  **Verified live the same day**, both spans driven against the real OBS with
  the server up and no camera (so the shot anchor was null on purpose). A
  68.2 s buffer — itself the proof the 300 s setting took, since the old one
  capped the file at 20 — gave `rack 37.2s ago → cue 29.6s, 38.6s on air` and
  `no anchor → cue 48.2s, 20.0s on air`; both seeks landed, both cut to Replay,
  both returned to Stream Detail. `show()` blocked 1.2–1.4 s, up from checkpoint
  49's 0.34–0.63 s, which is the bigger file. `RecRBTime` is now 300 s in the
  profile, the buffer was restarted to pick it up, and the deck daemon was
  relaunched onto the new layout.

  363 unit tests green, including a new `tests/unit/test_deck_loop.py` that
  drives `_loop` with a fake device — the first test of the deck's press
  routing, because tap/hold is the one thing no face test can see and its
  failure mode is a key that quietly plays the wrong span. Mutation-checked both
  ways (hold→SHOT, and a release that fires after a hold); each mutant is caught.

- **2026-08-05 (checkpoint 64 — the frozen cue keeps the flag while the real one
  leaves; and a break lost by 4 mm/s):** two `flag_missed` dumps, two
  independent bugs, both fixed.

  **The clock ran down through a real shot (`flag_missed_2`, 13:54 UTC, match
  mode).** Ground truth is Steven's: he shot, and vision never saw it. The
  detect loop was healthy at 76–78 Hz throughout, so this is not the
  contention artifact; the clock counted 30 → 10.95 and he took an extension to
  avoid a false violation. Cold replay produced **zero events over the whole
  20 s**, which makes it the first cold-reproducible member of the
  identity-drift class `stroke_gear_margin_0802` could only show warm.

  The addressed cue's own track froze at its rest spot and **kept the whiteness
  vote** while the ball was struck and gone. Three tracker mechanisms compose:
  a still track matched by a merged blob is *held* in place (checkpoint 28) and,
  unmatched, coasts for `still_coast_s` — 3 s, on the premise that an addressed
  ball cannot move unseen; the struck ball re-emerges past the 100 mm
  association gate and so founds a **new** track; and a frozen track's color is
  never resampled, so its stale whiteness (0.73) outranks the live one
  (0.60–0.72) and the flag never transfers. Measured: the cue was last detected
  at its rest (1529, 872) at 10.030 s, undetected **entirely** for 0.173 s while
  it travelled 171 mm, and back at 10.228 s as a merged blob 193 mm out at
  2281 mm/s. The phantom held the flag until its 3 s coast expired at 13.04 s.

  What made this cheap to fix is that nothing was actually wrong with the
  gates. At the impulse frame **every** gate of the existing flag-borne handoff
  already passed — dwell 9.72 s, still-stamp gap 0.013 s, quiet table, no mover
  within `carom_reach_mm` of the rest, 193 mm inside `strike_handoff_radius_mm`.
  The strike was missed for one reason: the flag was on the wrong track, so
  `_maybe_open_candidate` never evaluated the real ball. So the cue's remembered
  rest becomes the identity-carrying scan's **third anchor**, beside birth
  conjunction and a roster ball's own rest, sharing the flag-borne open logic
  verbatim (`_open_from_rest`). Fires at 10.43 s cold, reporting 2602 mm/s —
  which, note, is under the *old* IDLE break floor: had this stroke been a
  break rather than a mid-rack shot, the two fixes in this checkpoint would
  have been needed together to start the rack.

  **The tracker was deliberately left alone.** The obvious fix — stop a frozen
  phantom from holding the flag — was measured first and abandoned: instrumented
  across the corpus, "flag-holder absent (neither observed nor merged-held) for
  over `coast_s`" fires on `phantom_flag_strike_0805` (1.312 s) but *also* on
  `parked_vote_strike_0801` (2.983 s) and `ball_in_hand_pickup_0801` (2.976 s),
  which must stay silent and whose silence depends on the carom/hand-stamp
  guards. Changing identity would have moved the blast radius to scratch
  detection, which keys on the cue being *absent*. The event-layer path keeps
  every existing guard.

  Scoping the new path cost two rounds, both driven by measurement rather than
  reading the source. At the flag-borne impulse floor (110 mm/s) it handed the
  candidate slot to stick fragments near the rest and **cost `soft_shot` its
  real shot outright** — so it takes the birth path's floor instead
  (`newborn_impulse_mms`): the rest vouches for the *place*, nothing vouches for
  the track. Then the pickup/grab class fired phantoms (`cue_grab_0801` @11.32,
  `pickup_newborn_0801` @9.26, `shaft_adjust` @4.33), closed by an **ordering
  test with no constant at all**: the track must have been first seen *after*
  the flag-holder was last observed, so a stick fragment, a reaching hand or an
  established ball all pre-date the cue going dark and are refused.

  Why it is routed through the rest anchor and not the birth path, which
  already exists for occluded addresses: a ball struck out of an address is
  merged with the stick that struck it — **100 %** merged confirm observations
  on the real strike — and `newborn_merged_frac_max` correctly reads that as a
  carry. Its raw speed is erratic too (2281 → 289 → 215), so it fails the
  smoothness gate as well. Both carried-ball discriminators call this a carry;
  chain of custody from the cue's own rest is what makes the merge admissible,
  exactly as it is for an ordinary streaking shot.

  That leaves the observation fraction carrying the whole carried-ball
  judgement, so it has to be a measurement and not three lucky frames — the
  `pickup_snatch_0801` lesson (checkpoint 56) repeated on a new path. Measured
  against the hand class at increasing confirm age:

  | confirm age | strike 0805 | scratch_shot | cue_grab | pickup_newborn | shaft_adjust |
  |---|---|---|---|---|---|
  | 0.10 s | 1.00 | 1.00 | 1.00 | 1.00 | 0.25 |
  | 0.15 s | 1.00 | 1.00 | 0.82 | 0.92 | 0.21 |
  | 0.20 s | 1.00 | 1.00 | 0.64 | 0.69 | 0.22 |

  0.20 s is the first age at which the **existing** `newborn_confirm_obs_min`
  (0.9) separates the populations — at 0.10 s two of three carries read a
  perfect 1.00, and at 0.15 s `pickup_newborn`'s 0.92 still clears the floor. So
  the fraction needed no new constant; only the wait did
  (`handoff_confirm_min_s`), and it leaves a third of the confirm window for the
  fire to land.

  **One already-passing clip changed hands.** `scratch_shot_0801`'s strike now
  comes through this path instead of the birth path, and being rest-anchored it
  reports a less underread speed — **2008 mm/s against the birth path's 1518**,
  the `break_underread_0801` failure mode showing up on a clip nobody had
  flagged. It costs 0.09 s of latency for the confirm wait. Its golden moves
  accordingly (3.32 → 3.41 s, `newborn: true` → `false`, 1518 → 2008 mm/s);
  that is the only golden that moved, and the shot/scratch/settle counts are
  untouched.

  **A normal break lost by 4 mm/s (`flag_missed`, 2026-08-03 17:37 UTC, match
  mode).** Steven pressed New Rack, broke, and the gate ignored it: **2996 mm/s
  against a 3000 floor**. The clock sat IDLE through the break and he recovered
  by switching to `run_out` and pressing Restart — precisely the table-side
  intervention the floor's "the Start key recovers" trade-off waved off.
  `MATCH_BREAK_FLOOR_MMS` 3000 → **2500**, anchored between the two
  ground-truth-labeled populations mined out of every session journal (all 58
  `vision_shot_taken_ignored_idle` records ever logged): the confirmed break at
  2996 against the journal-pinned racking phantom at 2072 and a placement
  cluster at 251–1118. 2500 is the geometric middle of that boundary
  (√(2072 × 2996) = 2492) — 17 % under the confirmed break, 1.2× over the worst
  labeled racking impulse. It admits five historical IDLE impulses at
  2568–2974 whose ground truth is unknown.

  The asymmetry is what justifies moving toward the racking side: a gated real
  break leaves the clock dead and needs a press at the table, while an early
  start from racking **self-heals** — the real break that follows fires a
  ShotTaken and resets to 30. No floor covers the whole class and this one does
  not pretend to: `break_underread_0801` is a frame-verified rack explosion
  whose ShotTaken read **705 mm/s**, which sits *inside* the placement cluster.
  Speed alone cannot separate that one; the gap stays open and stays documented
  on that scenario rather than papered over with a number that would admit
  every placement on the table.

  Also worth knowing: **the break itself was not in its own clip.** The ring is
  20 s and the flag came 32 s after the stroke, so the dump holds only the
  aftermath — the failure is pinned from the session journal and a unit test,
  and the dump was deleted rather than promoted. Both edges (2996 fires,
  2072 does not) are unit-pinned so neither can drift back over the other.

  Corpus: `phantom_flag_strike_0805` promoted (1 shot, 0 scratches, 1 settle)
  with a bespoke timing pin — counts alone would stay green through the bug
  that matters, because the tracker's own recovery path reaches the same place
  ~3.8 s late (phantom dies at its 3 s coast budget, regrab grace expires, flag
  lands on the real ball ~14.06 s), and a shot minted from *that* would still
  count 1. `flag_missed` (the 08-03 break) was **deleted, not promoted**: its
  break is not in its own ring, so it can pin nothing.

  Four new tests, three of them unit so CI covers the logic without clips: the
  handoff fire (fails on the pre-fix engine with the exact live symptom — the
  engine returns `[]`), the hand-bringer rejection, the mid-scatter rejection,
  and both edges of the moved break floor. Plus the corpus entry and its
  timing pin. **54 corpus scenarios; 408 tests pass cold in 4:10**
  (`pytest -n 4`, T7 mounted, no live session), goldens regenerated with one
  moved timeline.

- **2026-08-03 (checkpoint 63 — recordings get the room, not just the robot):**
  checkpoint 62 put the overlay's cues into recordings and called the recording
  problem solved. It wasn't: the chimes and "Extension called" are *synthesized
  by the page*, so routing that browser source captured the only sounds cueclock
  makes itself — and none of the sounds the session is actually about. Steven's
  first recording after it played back with beeps and no break, no cue strikes
  and no voice. Room sound needs a **microphone**, and OBS's `Mic/Aux` was muted.

  `hardware.md` said it "is muted with monitoring off and must stay that way",
  which conflated two switches that fail in opposite directions. **Monitoring**
  is the hazard — on, the mic feeds the TV speakers the mic is listening to. It
  was already `NONE` and stays `NONE`; recording never needed it, because
  monitoring is the *speaker* path, not the capture path. **Muted** was
  protecting nothing. Unmuted, and pinned to `CamoAudioDevice_UID` rather than
  the `"default"` it shipped as: Camo's driver is the system default input
  today, but a headset or Jump Desktop's virtual mic would silently become the
  recording's audio — the same never-trust-the-default rule the listener has
  followed since Part D was written.

  Then the cues arrived **twice** — once clean from the browser, once as the
  room's echo off the TV a beat later. The bar source is now **"Monitor Only
  (mute output)"**: the TV still beeps, because monitoring *is* the speaker
  path, while the browser's copy leaves the mix. The microphone's version is the
  only one recorded, and it is in sync with the balls that made the rest of the
  sound. So the three states of that pair each fail at a different moment —
  reroute off hides a silent recording until playback, monitoring off silences
  the TV mid-rack, and monitor-and-output doubles every chime.

  A/B'd rather than reasoned about, since the settings are easy to get
  backwards: mic muted + cue fired = `-91.0 dB`, digital silence, so the browser
  reaches the TV and nothing else; mic live + same cue = a ~20 dB transient
  exactly on it (`-14.7 dB` against a −33 to −46 dB room floor), so the TV is
  demonstrably still sounding it. The first attempt at that measurement was
  thrown away — OBS's record start ate ~1.7 s of lead, which put the cue at t=0
  and made a flat room-tone take look like a missing beep.

  Also settled: the replay buffer was never broken. A save measured at −91.0 dB
  earlier the same day was an 18 s window with no cue in it while the mic was
  muted, not a buffer that drops audio; with the room live it records at
  −31.1 dB like everything else.

- **2026-08-03 (checkpoint 62 — the voice extension gets a mic and an answer;
  the rig's microphone was never missing, only muted):** closes #2, which had
  been parked on `needs:purchase` since 2026-08-02. **No mic was bought.** The
  phone already hanging off the USB cable for the side-angle camera has one,
  and it reaches CoreAudio two different ways — Camo Studio's audio driver
  (`Camo Microphone`) and macOS Continuity (`<owner>'s iPhone Microphone`).
  `--mic-device` defaults to Camo's, and is now *defaulted* rather than
  required: the deck's session key spawns a fixed argv with no room for flags,
  so a required flag made every deck-started session — the normal way to start
  one — silently deaf.

  **Camo's device streaming digital silence is a Camo setting, not a property
  of the device**, and getting that wrong cost a day. It opens with no error
  and delivers peak 0 / rms 0.0 / zero non-zero samples over 4 s with speech in
  the room, identically at 48 kHz stereo, 48 kHz mono and 16 kHz mono, whenever
  `audioSettings.enabled` is false in Camo Studio — which it was. The 08-03
  morning conclusion recorded here and in `hardware.md` Part D ("Camo can't
  send audio; use the Continuity device") was therefore wrong, and its fix was
  worse than the bug: opening the Continuity mic makes macOS claim the phone as
  a Continuity device while Camo is streaming the side angle off that same
  phone over that same cable. Toggling audio on in Camo Studio (View → Audio
  Settings) is the whole fix — 4/4 utterances accepted at confidence 1.00
  through the real `listen` path. Patching Camo's preference file does **not**
  work: it rewrites `audioSettings` from the peer connection at launch, and its
  window exposes no accessibility tree, so this is a click no script can make.
  Part D now leads with the level check that tells the two apart in one
  command, because a silent-but-healthy mic and "nobody said the word" are
  indistinguishable from inside the spotter.

  **Every granted extension now announces itself** — "Extension called" out of
  the TV, for the deck key, `/api/extension` and the voice alike, because a ref
  says it out loud and the caller shouldn't have to look at the wall to know it
  landed. It rides the clock's existing one-shot `cue` field, so no `/api/*`
  contract moved. Refusals cue too (`extension_denied`, a falling tone pair):
  silence there would be indistinguishable from a mic that never heard the
  shout. The phrase contains the keyword and the speakers share a room with the
  microphone, so `Hub.on_extension_granted` deafens the listener for
  `ANNOUNCE_MUTE_SECONDS` (2.5 s — the 0.98 s clip plus broadcast→speaker lag).
  That is driven from the *hub*, not from the listener's own fire, because a
  deck-granted extension announces without the mic having fired at all; and the
  spotter's 2 s cooldown is not a substitute, since Vosk finalizes at
  end-of-utterance and the announcement's own final can land after it expires.
  Asset: `static/voice/extension_called.wav`, Kokoro-82M `af_bella`, committed
  because Kokoro is Apache-2.0 (`scripts/make_voice_assets.py` re-renders it).

  **`extensions_per_frame` was silently enforcing half the allowance.** Every
  ruleset's extension is per *player* per rack and the clock has no turn
  tracking, so the pool has to be twice the per-player figure, self-policed at
  the table — which is how the WNT ruleset was always modelled. The two 8-ball
  rulesets said 1, so the second player got none. Both are 2 now; per-player
  enforcement stays backlog (#15).

  Also fixed here, found while testing the above: **the Session key opened the
  TV projector twice.** OBS's "Save projectors on exit" restored one at launch
  and `obsws.open_projector` opened another ~10 s later, and
  `OpenVideoMixProjector` **stacks** rather than reusing an open window — the
  opposite of what the 2026-08-02 note in `obsws.py` claimed. Counted from OBS's own
  `saved_projectors` at quit: three calls, three overlapping fullscreen windows
  on monitor 0, which on a single display are indistinguishable from one. That
  is exactly how the wrong conclusion got made, and the stack persisted across
  sessions through the same saved list. The rig's OBS config now has **"Limit
  one fullscreen projector per screen" ON** (3 opens → 1 window, verified) and
  **"Save projectors on exit" OFF**, leaving the deck the only thing that opens
  a projector — the single-owner design `hardware.md` §C4 already described.
  The cost is the OBS UI on the wall for the ~10 s until the WebSocket answers;
  the per-screen limit is what makes flipping save-on-exit back safe.

  And **the cues are in the recordings now**, which they never were. Checkpoint
  61 and the README both said the bar source had "Control audio via OBS" on; it
  did not (`reroute_audio: false`), so CEF played the chimes and the
  announcement straight out the macOS default output to the TV and OBS never
  saw them — every recording carried a **silent** AAC track, measured at
  `mean_volume -91.0 dB` against `-39.5 dB` for the same 5 s once routed. Two
  settings were needed, not one, and the second is the trap: routing the audio
  into the mixer without also setting Audio Monitoring to **"Monitor and
  Output"** puts it in the recording and takes it off the TV — a shot clock
  that stops beeping, in exchange for audio nobody hears until playback. The
  one real cost is that monitoring buffers the speaker path, which slightly
  narrows a margin that used to be untouched: `ANNOUNCE_MUTE_SECONDS` is
  anchored to the broadcast rather than to the sound leaving the TV, so its
  2.5 s now absorbs broadcast→monitor→speaker instead of broadcast→speaker.
  Also settled while checking it: of the two live `/shotclock` browser sources,
  the full-screen one (`?view=full&audio=off`) is the one that stays silent, so
  no cue fires twice.

- **2026-08-03 (checkpoint 61 — the clock comes off the video: one bar for
  clock and score, plus a control page you can use one-handed):** closes #23,
  and finishes what checkpoint 60 retreated from. `shotclock.html` gains a
  third view, **`?view=bar`** — a 160 px plinth along the bottom of the
  side-angle column, scoreboard left, clock in a 290 px cell hard against the
  seam at x=1334, with a drain along the whole top edge. `score.html` and the
  `GET /score` route are **deleted**, not deprecated: the drain runs the full
  width of the bar, crossing from the score area into the clock cell, and no
  overlay can span two OBS sources, so clock and score had to render from one
  page. No `/api/*` URL changed and the `/ws` payload already carried
  everything the merged page needs (`Hub.payload` composes clock + `vision` +
  `score`), so the Stream Deck contract is untouched.

  What this fixes, in order of how much it mattered: the countdown was a
  193×193 sticker sitting **on live video**, which is why every replay had a
  fake clock counting in it (checkpoint 46) and why checkpoint 49's mask
  existed at all; the scoreboard was centred on the *canvas* rather than on the
  side-angle panel, so its right third lay across the overhead strip's
  bottom-left pocket; and the two overlays were in different design languages.
  A replayed countdown is still in the recorded pixels, but it now sits on a
  plinth under the REPLAY badge — the relationship a broadcast has with its own
  lower third — so whether it still wants covering is a judgement on the wall,
  not a code question (`hardware.md` §C4a).

  Design decisions worth not relitigating. **Escalation is by area, not hue** —
  ink (urgent) → edge (GRACE, a 6 px outline) → field (EXPIRED, filled and
  flashing): GRACE and EXPIRED both read "0" and can't be told apart by the
  digits, and `score.html`'s three reds (`#ff7070`/`#ff5050`/`#ff2020`) differ
  by a few percent, which is nothing at twelve feet. One `#FF4D4D` now.
  **Parked (MOTION/IDLE) is the opposite move from alarm** — slate digits, no
  drain — because a stopped clock must never look like a running one; that is
  the same reasoning that removed the digits entirely in `no_clock`.
  **The drain is anchored to the right**, the clock end: anchored the other way
  the last few seconds leave the red stub ~1300 px from the numeral it is
  describing. **Its dim track is drawn in every state** — a bare fill shrinks
  to an 8 px nub at four seconds and reads as a stray mark, and the constant
  track is also what the alarm states sit *under* instead of cutting through,
  so the bar keeps reading as one object. Palette is `/replay`'s, so the two
  overlays are one family.

  Three things the mockups couldn't predict, found by rendering the page in
  OBS's own CEF and diffing it against the mockups pixel by pixel (studio-mode
  *preview*, so program was never touched): the hairlines came out about half
  as bright in CEF as PIL had drawn them (PIL's `ImageDraw` replaces alpha
  rather than compositing, so the mockup's "on the plinth" lines were really
  lit by the video behind them); a 1 px rule inside a centre-transformed flex
  row lands on a half pixel and antialiases to nothing, so the score divider is
  laid out on its own whole-pixel x; and `steps(2, start)` samples the
  *interpolated* midpoint of a keyframe pair, so the lit half of the EXPIRED
  blink was a muted maroon rather than the alarm red — it is `step-end` over
  explicit halves now. Geometry otherwise landed within 1–4 px of the mockups.

  `control.html` is restyled into **Match / Clock / Session**, Match first, in
  the bar's palette. This is presentation only — the name and race-to fields
  already existed and already posted to `/api/score/config`; what changed is
  that Match was previously the last thing on a long scroll, under the vision
  kill switch. Race-to became a stepper (on a phone a number input is either
  tiny arrows or a whole numeric keyboard, for a value that is almost always
  5/7/9; stepping up from cleared lands on 5, since nobody plays a race to 1),
  destructive controls are outlined rather than filled, and the Clock block
  keeps the deck's exact vocabulary (§C2). One button was dropped: plain
  `POST /api/start`, because `pause()` drops to IDLE and a later `start()`
  restarts from full base — which is what **Start N** already does. The
  endpoint stays; the deck never had a key for it either.

  Deliberately not done: **no deck key for race-to**. It is once-per-match
  setup and `/control` reaches it from a phone (`run` binds `0.0.0.0`). The
  three free cells — (2,3), (2,4), (2,5) — are the ones the Flag reason page
  unfolds into, so a persistent key there would be *replaced* by
  PHANTOM/MISSED/OTHER for the ~60 s a Flag press is pending rather than go
  dark, and a mis-press writes a ~3.4 GB ring dump.

  Rig, changed in the same pass (`hardware.md` §B3 is a living doc): one
  `Clock + Score bar` browser input at (0, 0), 1334×1080, "Control audio via
  OBS" on, present in **both** `Stream Detail` and `Shot Clock` — one input is
  one browser is one chime, so the cues no longer depend on which scene is
  live. The full-screen clock gained `&audio=off` for the same reason, and the
  dead `/score` source was removed.

- **2026-08-03 (checkpoint 60 — the replay clock mask is withdrawn; a black
  rectangle over live footage was worse than the stale clock it hid):**
  checkpoint 49's `?mask=` is removed from `deck/replay.py` and the `/replay`
  page. Nothing replaces it yet — this is a retreat to a known state, chosen
  deliberately over shipping a second cosmetic patch on top of the first.
  - **What it looked like, which is the whole reason.** On the rig the corner
    clock is a **193×193 browser source at (1141, 0)** (probed live), sitting
    on the Camo side angle rather than on any backdrop — so the mask was a
    197×195 black rectangle punched into live footage. The live element at
    that rect is *already* a black square: `shotclock.html` draws black with a
    4 px white border and a 90 px numeral. The mask reproduced the black and
    dropped the border and the digits, which is why it read as damage rather
    than as an overlay (Steven, 2026-08-03: *the black square over the clock
    in replay view looks bad*).
  - **The mechanism was never wrong**, and that is worth recording so it isn't
    re-derived: identity was the *page* (`/shotclock`), measured from the same
    OBS that composed the recorded frame, so a renamed, moved, resized or
    duplicated overlay stayed covered and a hidden one was skipped. It is
    removed for how it looks, not for what it does.
  - **The accepted cost is checkpoint 46's bug, back.** A replay again shows a
    countdown that isn't real, under a badge that says REPLAY. The scrim and
    the wordmark are the only things now saying "not live".
  - **Why not "record a clean scene instead", which was the first instinct:**
    the replay buffer is an output fed by the **program mix**, and stock OBS
    32.1.1 has no per-scene or per-source recording (verified against the
    installed plugin set — bundled only). That path needs Exeldro's
    `obs-source-record`, plus `TriggerHotkeyByName` and newest-file discovery,
    because obs-websocket cannot save a filter-owned buffer. A third-party
    plugin and a second continuous encoder on the machine doing 160 fps vision
    is not the price for a cosmetic defect.
  - **The real fix is a live-scene layout change**, deferred by Steven ("we'll
    figure the rest out later"): put the clock on flat backdrop — a broadcast
    top bar holding clock + score — so a mask would be black on black and the
    corner clock stops floating over the picture, or move it out of OBS
    entirely and accept losing the chime routing. Options recorded in
    `hardware.md` §C4a so the analysis isn't repeated.
  - **A tail that argues against masking as a strategy, not just this mask:**
    pressing Replay from the full-screen **Shot Clock** scene produced a mask
    over **69.6% × 69.8%** of the canvas, since that scene's clock *is* the
    canvas. Found while measuring the live rig for this change; it dies with
    the mask.
  - Also gone, because they existed only to serve it: `obsws.canvas_size`,
    `scene_items`, `input_url` (`GetVideoSettings` / `GetSceneItemList` /
    `GetInputSettings`), and `replay.Mask` / `item_mask` / `clock_masks` /
    `_live_masks`. `badge_url` takes `(base, badge_s)`. A press now makes
    **three fewer OBS round trips** before the anchor read, so the comment
    about keeping nothing between that read and the save goes too.
  - Tests: `tests/unit/test_replay.py` 351 → 338 unit tests; the cue-point
    arithmetic and the whole show/stop ordering suite are untouched. No
    detection code touched, so the corpus battery is unaffected and was not
    re-run. `docs/design.md` §2.1 and `hardware.md` §B3/§C4a updated in this
    commit, per the standing rule.

- **2026-08-03 (checkpoint 59 — the press follows the settle, not the stroke;
  the pair window was measuring the roll-out):** checkpoint 58's provisional
  `PAIR_WINDOW_S = 5.0` is replaced by two measured, asymmetric reaches. The
  first real marked session scored **15.4% (2/13)** on a session where vision
  caught **every shot Steven marked**; the report was wrong, not the clock.
  - **The measurement, which is the whole entry: the press is anchored to the
    settle.** Across the 13 presses of `logs/session-20260803-121204.jsonl`,
    press − settle is 0.10–2.87 s (median 0.81), 11 of 13 within 3 s — plain
    reaction time, he watches the balls stop and taps. Press − *stroke*, which
    is what the window was actually gating on, is 4.10–11.48 s, because the
    roll-out sits inside that interval and roll-out is physics: shot → settle
    measured 2.27–8.62 s in the same session. The two independent measurements
    reconstruct the observed maximum (8.62 + 2.87 = 11.49), which is the check
    that the 12 s reach is anchored and not fitted.
  - **`PRESS_LAG_S = 12.0` cannot over-reach.** The shortest shot-to-shot gap
    in that session is 13.0 s, and pairing takes the *nearest* unpaired shot,
    so a 12 s reach can never skip past an intervening stroke.
  - **One symmetric window could not have worked**, which is why this is two
    constants rather than a bigger one. At 638.11 s the press sat 11.48 s after
    its own stroke but only 8.65 s before the *next* one: a symmetric reach
    wide enough to find the stroke takes the nearer shot instead, pairing a
    press to a stroke that had not happened yet and manufacturing a miss and a
    phantom out of two correct detections. `LATE_DETECT_S = 5.0` stays
    deliberately short and **stays provisional** — no real session has ever
    produced a forward pair, so there is nothing to anchor it to. Said plainly
    in the constant, the CLI help and `docs/cli.md`.
  - **Two further defects the same clip batch exposed, both fixed here.** The
    "slowest pairs (review candidates for late detection)" list sorted by
    `abs(shot - press)`, so with every healthy pair negative it ranked the
    *promptest* detections as the latest — the list meant the opposite of its
    heading. It now sorts on the signed delay and prints only shots emitted
    after their press, saying "none" when there are none. Separately, the
    pairing window's upper edge stopped dead at the last press, so a genuinely
    late detection on a run's final shot could never pair and read as a miss;
    pairing now reaches `LATE_DETECT_S` past the edge. **Phantom judgement is
    untouched** and still uses the strict window — the trailing reach can
    rescue a press from a false miss but can never invent a phantom.
  - `--pair-window` becomes `--press-lag` + `--late-window` (`cli.py` and
    `docs/cli.md` in this commit, per the standing rule).
  - Tests: `tests/unit/test_report.py` 14 → 20. The pin that matters replays
    the 2026-08-03 session verbatim — all 19 detections, all 13 presses — and
    asserts 100% with every press taking the stroke *before* it; it fails on
    any symmetric reach. Companion tests cover the late-detection sort, the
    trailing-edge rescue, and that phantom judgement did not move. 351 unit
    tests pass; the corpus battery is unaffected (no detection code touched)
    and was run cold to confirm.
  - **Six unmarked detections in that session are Steven's own forgotten
    presses**, confirmed by him at triage time — not phantoms, and not
    investigated further. The report calls them PHANTOM because that is what
    the diff says; the "a disagreement is a review candidate, not a label"
    clause is doing exactly its job.

- **2026-08-02 (checkpoint 58 — the ground-truth key; accuracy finally has a
  denominator):** one deck key, `POST /api/groundtruth`, and
  `cueclock report`. Closes #19.
  - **Why it leads the board:** every other accuracy item was ranked by
    argument, because "the clock missed a shot" was only ever observed when
    Steven happened to notice and hit Flag. Pressing one key on every shot
    turns detection accuracy into a number, and turns a disagreement into a
    review candidate with a timestamp.
  - **The governing rule, which is a constraint and not a nicety: the player
    never classifies a failure.** One key, "I shot", pressed every shot, no
    judgement attached — the only fact a human holds that the machine does
    not. Missed / phantom / late are all diffs the server computes. *If a
    future change adds a case the player must recognise, the change is wrong.*
    Recorded in `hardware.md` §C2 and `design.md` §2.1 so it outlives this
    entry.
  - **Not a mode, never dark.** Once the design collapsed to a single
    unconditional key the only remaining cost is the press, so a toggle saves
    nothing and adds one more thing to remember. Arming is inferred offline
    instead: presses group into runs (split at 3 min) and only those windows
    are judged, so a session he did not mark is ignored rather than reported
    as all-phantom.
  - **The window is deliberately asymmetric**, and getting it wrong was the
    one real bug in the build. Pairing reaches back a pair-window *before* the
    first press, because a press always lands after its stroke — without that,
    the opening shot of every run sat outside the window and its press read as
    a miss. Phantom judgement uses the strict window and never reaches past the
    last press: a shot after Steven stopped pressing is evidence he stopped
    pressing, not evidence vision invented anything.
  - Presses are stamped on the **capture** clock by extrapolating from the
    newest processed frame, so alignment never crosses the emit/capture
    boundary — which is exactly where detect-loop starvation hides. With no
    vision running there is no capture clock, and the record is `null`: the
    reader counts those as unanchored rather than guessing.
  - The report separates **gated** shots (`vision_shot_taken_ignored_idle` and
    friends) from misses. "Vision saw it, the clock declined it" is a
    clock-policy bug; "vision never saw it" is a detection bug. Conflating them
    would send the next investigation to the wrong module.
  - **Both constants are provisional and say so.** `--pair-window` (5 s) wants
    re-deriving from the delay distribution the report prints, once a real
    session exists; nothing auto-flags a "late" verdict against a guessed
    number. Auto-dumping on disagreement is deliberately not built yet — the
    press path plus the key already yield the denominator, and the dump
    trigger should wait for a measured window.
  - Key at 3,5 (bottom-right corner, findable by feel like Extension), face
    `I SHOT` — two lines because "SHOT" alone is a noun a command-key reader
    would take as an instruction. `deck/layout.py`, `hardware.md` Part C,
    `cli.py` and `docs/cli.md` all changed in this commit, per the standing
    same-commit rules.

- **2026-08-02 (checkpoint 57 — the IDLE gate reads speed, not birth):** the
  newborn half of the IDLE gate (checkpoint 33) is removed. The **speed floor
  is untouched** — it was never in question. Closes #10.
  - **The positive control did not pass, and that is stated rather than
    glossed.** The plan required proving the corpus exercises rack setup before
    reading silence as "dead code". It does not: nine rack-setup scenarios
    produce exactly two `ShotTaken` between them, and both are the *genuine*
    shots in those clips (`scratch_replace`, `cue_grab_0801`), both
    rest-anchored. Racking and placement are now event-silent — checkpoints
    28–56 fixed precisely that — so the corpus holds no newborn rack-setup fire
    at all, and an absence above the floor could not have proven anything.
  - **What decided it was positive evidence, not silence.** Seven real strikes
    in the corpus fire newborn — `jaw_strike_0801`, `occluded_address_0731`,
    `lone_roller_0801`, `scratch_shot_0801`, `parked_vote_strike_0801`,
    `address_chatter_miss_0801`, `drill_shot_0801` — because the
    occluded-address and own-rest paths are newborn *by design*. Against that,
    zero rack-setup motions fire newborn. The gate's premise, "newborn implies
    not a real shot", is contradicted nine times over (those seven plus the two
    breaks that anchored it) and supported nowhere. It was not dead code; it
    was wrong code.
  - The gate was measured on two clips read as hard hand-rolled returns, which
    Steven identified as **9-ball breaks that fired newborn** — i.e. on
    examples of the very thing it was supposed to admit. The residual its own
    note conceded ("a break from a fully occluded address fires newborn and is
    eaten") was that evidence, not a hypothetical.
  - The speed floor independently covers the failure this was reaching for:
    racking and placement lookalikes measure below 1000 mm/s against breaks at
    5845–7361, and the floor sits at 3000. The removal is recorded in full in
    `hub.py` beside the surviving gate — deleting a guard silently is how it
    comes back.

- **2026-08-02 (checkpoint 56 — the bare-hand snatch; a fraction gate needs a
  sample floor):** `pickup_snatch_0801` flips from its known-wrong
  `(1, 0, 2, 2)` pin to **0 shots**. Closes #8. Cold battery **384 passed,
  4:02**.
  - **The carried-ball gates were never wrong — the confirm was too fast to
    use them.** All three are *fractions over the confirm window*, and a fast
    confirm can close that window in three observations. The snatch fired with
    `merged_frac` exactly **0.00** over 0.070 s while reading 71% merged across
    the whole carry. Traced frame by frame,
    `newborn_raw_step_ratio_max` — the documented discriminator, a carry's raw
    speed being erratic — runs 1.01, 1.00, 1.07 and then **2.24 at age
    0.096 s**. The candidate confirmed at 0.070 s, one frame early.
  - The whole fix is `newborn_confirm_min_s` (0.10 s): a birth-path candidate
    must observe that long before it may confirm. Measured cliff — 0.06 s
    still fires, 0.08 s works; 0.10 keeps a frame of margin and still leaves
    two thirds of the window. Cost falls only on the birth path (the
    occluded-address class, where a tenth of a second is nothing against a
    30 s clock); six newborn strikes shift +0.06 to +0.18 s and every count
    holds. The phantom *settle* went with the phantom shot — a snatch no
    longer resets the clock either.
  - **Generalisable lesson, worth more than the fix:** a gate expressed as a
    fraction needs a floor on how many samples it may be computed from, or a
    short window silently disables it. Nothing in the corpus counts would have
    shown this; it took tracing the gate that was supposed to fire.
  - **The plan's proposed lever does not work, and the measurements are
    recorded so nobody re-derives it.** The apparent-size height cue (camera
    looks straight down, so a carried ball images larger) is real — real
    newborn strikes read 0.82–1.22 against balls resting on the cloth, the
    carry reads 2.45, a clean 2x split — but **at the instant the phantom
    confirms the ball reads 1.14**, mid-band. It has only just been touched
    and has not risen yet; it passes 2.0 about 25 ms later. A gate cannot use
    evidence that does not exist yet. It also misjudges `no_cue_strike_0801`,
    a *real* strike at 2.62 where the cue is merged with the stick through
    impact. Area *growth* is strictly worse: an accelerating ball blurs and
    streaks, so `break_0801b` grows 1.58x against the snatch's 1.27x, and
    growth resets each time the carried ball is reborn as a fresh track.
    Both forms were built, measured, and reverted — an unexercised gate is
    untested code that rots. The numbers live in `events.py` and `design.md`
    §4.2.

- **2026-08-02 (checkpoint 55 — Scratch becomes a deferred verdict; the jaw
  phantom is gone):** `hanger_pot_0801` grades 0 scratches with its shot and
  settle unmoved to the millisecond. Closes #6. Cold battery **382 passed,
  3:51**.
  - **The counter-evidence was never missing, only late.** A cue that stops
    hanging in the jaw dies inside the pocket zone exactly like a potted one,
    which is why this was written off as a known limitation in `design.md`
    §4.2. But measured on the clip: a reborn track appears **0.24 s** after the
    loss, **33 mm** from the death spot, converging to 12 mm. On all three
    genuine scratches (`scratch_shot_0801`, `shot_debug`, `scratch_replace`)
    **nothing at all** is observed within 200 mm for the next 3 s. That is a
    clean discriminator; it only had to be waited for. Scratch drives no clock
    state in v1, so waiting is free.
  - Track death in a pocket zone now *latches*; the event publishes
    `scratch_confirm_s` (1.0 s, 4x the measured reappearance) later and is
    cancelled if any observed ball is within `scratch_reappear_mm` (60 mm) of
    the cue's last observed position.
  - **Cancellation must be positional only** — learned the hard way. The first
    version also cancelled when the cue flag came back, which reads as "the cue
    returned". It is not: after the cue drops, the whiteness vote parks on some
    other white-ish ball within a few frames, and that suppressed **all three**
    genuine scratches. There is now a unit test for exactly this.
  - 60 mm is bounded on both sides by the same episode: above the 33 mm first
    sighting, and well under the 94 mm at which a genuinely different, still
    ball sat — which must not vouch for a cue that really dropped.
  - **Consequence to know about:** the emitted `t` is now the confirmation,
    ~1 s after the ball actually vanished, and a Scratch can land *after* the
    shot's `BallsSettled` (it does in `scratch_shot_0801`). Subtract the delay
    when correlating a Scratch against clip footage.

- **2026-08-02 (checkpoint 54 — the foul reversal; the sub-impulse class is
  detectable):** `shaft_adjust`, `tip_bump` and `soft_touch_0731` all fire a
  ShotTaken for the first time. Closes #5. Cold battery **379 passed, 3:51**.
  The design decision is appended to `design.md` §8 (never renumbered).
  - **#5 was never mic-blocked — it was policy-blocked by a corpus
    expectation.** Both constants that made touch shots invisible were
    anchored on keeping a foul bump silent, not on any measurement of a real
    shot: `strike_impulse_mms` 250 sat just above `soft_touch_0731`'s 225 mm/s
    peak, and `strike_confirm_displacement_mm` 60 existed to reject
    `shaft_adjust`'s ~59 mm — the code comment said so outright. Reversing the
    rule dissolved both anchors. `tip_bump`, which §9 called **the hardest
    case in the corpus**, stopped being hard rather than getting solved: it is
    a foul, a foul resets the clock, and so does the soft shot it could never
    be told apart from.
  - **Measurement that inverts the original design's assumption: speed does
    not separate a shot from a provoker.** `practice_strokes`' stick fragment
    peaks at **1576 mm/s** — faster than every true event in the class
    (`tip_bump` 124, `soft_touch_0731` 225, `shaft_adjust` 881). The impulse
    floor only decides which candidates open; the confirm *displacement* is
    what rejects them, and there the separation is clean: 14.5 mm worst
    provoker against 29.0 mm weakest true event.
  - **60 mm was load-bearing for a second reason nobody had written down** —
    it forced the speed measurement to span enough real travel.
    `ShotTaken.speed_mms` is max(fastest observed, displacement / candidate
    lifetime) and an early confirm truncates both terms. Dropping it to a
    single 22 mm threshold made `break_0801` confirm 0.18 s sooner and report
    **2068 mm/s instead of 7371** — under `MATCH_BREAK_FLOOR_MMS`, handing the
    IDLE gate the exact live failure checkpoint 45 fixed. (At impact the cue's
    blob merges with the stick, so the centroid lags and early displacement
    underreads: a break needs distance, not time, before its speed is honest.)
    Hence a **two-stage confirm** — 60 mm at any time, or 22 mm once the window
    has run — so every ordinary shot still fires on the same frame at the same
    speed. 47 of 53 golden timelines are byte-identical across this change.
  - **Two pre-existing holes surfaced when the floor came down**, both found by
    the goldens and both fixed rather than tuned around:
    - `strike_min_travel_mm` (new, 10 mm). Confirm displacement is measured
      from the rest/birth *anchor*, which the ball may already have left, so it
      could be satisfied entirely by pre-open history: `triage_0801b` opened
      180.6 mm from the rest spot and fired 19 ms later having travelled
      **0.5 mm**, reporting 9434 mm/s. Real candidates gain 12.1–45 mm — a 24x
      gap. This also deleted a 13882 mm/s "shot" from `break_underread_0801`,
      a speed no cue ball reaches (a break is ~7000).
    - `newborn_impulse_mms` (new, 250 mm/s — the original floor, kept). The
      relaxation is justified by **chain of custody**: the tracker watched that
      ball sit still and then move. A newborn track has none by definition, and
      a slow ball appearing from nowhere is likelier carried than struck —
      `cue_grab_0801`'s gloved hand lifts the resting cue at 130 mm/s and
      confirmed as a strike once the floor dropped. Every real birth-path
      strike in the corpus is fast (1294, 1572 mm/s), so this costs nothing.
  - **`ball_in_hand_nudge` is now unasserted for shots, deliberately.** §9
    defines it as fine-tuning a placement *with the shaft* — the same physical
    act as `shaft_adjust`, but legal, so the opposite clock outcome is correct,
    and vision has no ball-in-hand state to tell them apart. Steven's call
    stands (fire on both, eat the rare wrong reset, no key). It happens not to
    fire today, but only because the whiteness vote is parked on another ball
    — the `parked_vote_strike_0801` identity bug, **not** a discriminator.
    Pinning 0 would claim a separation that does not exist; pinning 1 would
    demand fixing an unrelated bug to satisfy an outcome Steven said he does
    not need either way. The golden still pins the timeline exactly.
  - Two real shots report lower speeds (`stroke_gear_veto_0802` 1732 → 866,
    `stroke_tug_strike_0801` 6061 → 3239): a lower floor opens their candidate
    earlier in the acceleration, so the kinematic term divides by a longer
    life. Both stay far from any gate that reads speed, and every break is
    unchanged — `test_break_0801_speed_clears_the_idle_gate` still passes at
    7371.

- **2026-08-02 (checkpoint 53 — golden event timelines; the corpus now pins
  *when*, not just how many):** `tests/goldens/<scenario>.json`, one committed
  timeline per corpus clip, asserted inside the existing `test_corpus_clip` so
  they cost no extra replay. Closes #22. Cold battery **374 passed, 3:54**
  (up from 358 — the new tests are the comparator's own unit coverage).
  - **The blindness was measured, not argued.** Perturbing `settle_dwell_s`
    0.5 → 0.6 — one 100 ms tuning nudge — moved **33 of 53** corpus timelines.
    The `EXPECTED` count assertions caught **one**. That ratio is why threshold
    work here has historically been unmeasured: the collateral of a change did
    not surface until a live session, days later, as a flagged clip.
  - **Asserted:** the event type sequence (exact), `t` (±20 ms ≈ 1.5 detect
    frames), `speed_mms` (±2%, 5 mm/s floor — the IDLE break gate reads it, so
    drift past `MATCH_BREAK_FLOOR_MMS` is a live failure), and `newborn`
    (which strike path fired is a design-level fact).
  - **Not asserted: `ball_id`.** It is a tracker birth counter, so any change
    to track birth order renumbers every id and would turn a harmless refactor
    into 50 red goldens. Reds that are routinely fixed by regenerating teach
    the reader to regenerate reflexively, and then the net catches nothing. The
    id is still written to the file — it is what makes a diff readable, and
    `parked_vote_strike_0801` exists because the *wrong ball* fired.
  - **Goldens are committed though clips are not.** They are small text, and a
    fresh clone keeps the record of what the engine did on 2026-08-02 even with
    no pixels on disk.
  - Generated cached, then verified cold: the committed timelines reproduce
    against real pixels, which is also an independent confirmation of the two
    reproductions the plan recorded (`hanger_pot_0801` Scratch at 12.34 s,
    `pickup_snatch_0801` ShotTaken at 12.28 s).
  - `tests/__init__.py` added — `tests/corpus/` and `tests/unit/` were already
    packages but the root was not, so the comparator was not importable from a
    unit test.
  - **This had to land before the foul reversal (#5)**, which moves thresholds
    across the whole sub-impulse region. Without goldens that change would have
    been unmeasurable by construction.

- **2026-08-02 (checkpoint 52 — the accuracy backlog cut to evidence; three
  standing decisions reversed):** no detection code changed. A planning and
  cleanup pass that ended with a smaller board, one new plan doc, and three
  reversals that later work depends on. 299 unit tests green; the corpus
  battery was not run because nothing under `vision/` changed.
  - **The acoustic click corroborator is rejected.** Since 2026-07-27 it had
    been the designed answer for the whole sub-impulse class, and three open
    issues were parked on buying a mic. Steven's call: different tips
    (phenolic vs leather), stroke power, and room noise make a "click"
    unreliable, and he is not committing to a mic for shot detection.
    Issues #5/#8/#11 lost the dependency and the `needs:purchase` label;
    `hardware.md` Part D records it as rejected, not backlog. Mic hardware is
    still wanted for the shipped voice "extension" (#2), which is a separate
    use and was not rejected — #2 was rescoped to that alone rather than
    closed, because it gates a v1 feature whose code already ships.
  - **A shaft/tip bump is a foul, and a foul resets the clock** — reversing
    "v1 detects shots, not fouls" (2026-07-27). That decision reasoned that a
    legal ball-in-hand nudge and a foul bump are the same pixels, so detect
    neither; it stopped one step short of the *outcomes*, where two of the
    three cases (soft shot, foul bump) reset and only legal ball-in-hand
    placement does not. The consequence is large: `strike_confirm_displacement_mm
    = 60.0` exists to reject `shaft_adjust`'s 59 mm (the code comment says so),
    and `strike_impulse_mms = 250.0` sits just above `soft_touch_0731`'s
    225 mm/s. **#5 was never mic-blocked — it was policy-blocked by a corpus
    expectation.** Ball-in-hand stays keyless by decision: fire on both and eat
    the rare wrong reset, since the annoyance is the evidence that would
    justify a key.
  - **#19 collapsed to one unconditional key.** An intermediate draft had
    Steven choosing between Start, Flag and the ground-truth key depending on
    how the clock had misbehaved; he rejected it — every branch existed because
    the design asked him to classify failures at the table, which is the work
    the button exists to remove. Now: one key, "I shot", pressed every shot,
    always available, no mode toggle. The server derives miss / phantom / late
    / recovery, and arming is inferred from the press runs themselves. Auto-flag
    on Start, Start-implies-a-press, and inter-trigger dedup were all patches on
    the branching design and are recorded as rejected so they are not
    reinvented.
  - **The idea bank is gone** (`docs/accuracy-ideas.md`, deleted). A list of
    maybes competes with the board for attention. Its still-load-bearing
    content was inlined into #6 and #19 first, and six citations were
    de-pointered — three of which already named the wrong file. `CHANGELOG`
    references were deliberately left dangling: they are dated history.
    Replaced by `docs/accuracy-plan.md`, which is explicitly temporary and to
    be deleted once implemented.
  - **Board: 11 accuracy issues to 4.** Closed #14/#20 (decided against),
    #1/#4/#10 (absorbed by #19 — their method was "play sessions and Flag
    anything wrong"), #12 (staged clip for an unevidenced problem), #7 (never
    observed), #9 (structural cause removed at checkpoint 37).
  - **Two clips verified against Steven's ground truth, two disputed.**
    `hanger_pot_0801` is a legal shot that fires a Scratch at 12.34 s (#6
    confirmed, priority raised off journal-only); `pickup_snatch_0801` fires a
    ShotTaken at 12.28 s on a pickup-and-place with no shot in the clip (#8.1
    confirmed). But `flagged_9`/`flagged_14` — #11's entire evidence base and
    the source of the 3489/3926 mm/s anchor in `hub.py` — are 9-ball racks and
    breaks per Steven, with no hand-rolls visible. #11 is now marked evidence-
    disputed and unbuildable until re-derived.
  - **The clip-preview recipe was silently lying.** Ring dumps have
    non-uniform frame gaps (13-115 ms measured), and `clipview` wrote them at a
    flat 30 fps, desynchronising the scrub bar from the burned-in clock by up
    to 2.6x — different per clip. Two rounds of wrong timestamps reached Steven
    before it was caught. Fixed to resample onto a real-time grid, so scrub
    position, burned-in clock and `analyze` output are now interchangeable;
    the `triage-clips` skill carries the fix and the lesson.
  - **#11 closed invalid; #10 reopened in its place — the same two clips.**
    Steven watched `flagged_9` and `flagged_14` at the cold-replay event times
    (22.19 s / 30.42 s): **both are 9-ball breaks**, not hand-rolled returns.
    That kills #11 outright — `flagged_10/11/13` were never examined, their
    labels inherited rather than verified, and all four unexamined dumps were
    deleted (~13.6 GB). Nothing in the corpus now demonstrates a
    hand-rolled-ball phantom. But both breaks fired `newborn=true`, which is
    exactly what the checkpoint-33 IDLE gate rejects, and `hub.py` cites these
    two events as its justification — so **the gate's discriminator is
    falsified and it eats real breaks.** #10 reopened with them as evidence;
    the `hub.py` comment now says so at the top. Not reverted blind: removing
    the gate reopens whatever checkpoint 33 was reacting to, which is no longer
    known. Neither clip is a threshold anchor — both were recorded at 41.9 and
    34.0 Hz against ~78 nominal.
  - #8 split: the bare-hand snatch (gradeable, closeable, `pickup_snatch_0801`
    pinned known-wrong) stays #8; the white-shaft sweep, which has no lever and
    is not cold-reproducible, became #21 so the pair could stop blocking each
    other. Golden event timelines filed as #22 — the corpus asserts counts
    only, so a settle sliding 300 ms passes green, and that is the regression
    net the foul reversal needs before it moves any threshold.
  - **Final board, same day: 4 accuracy issues.** #21 (white-shaft sweep)
    closed without a fix — two live occurrences but no cold-reproducible case
    and no lever that was not speculation; a recurrence will be warm-replayable
    and can reopen it. `flagged_9`/`flagged_14` deleted along with the other
    four dumps (~20.4 GB total): at 34–42 Hz their newborn firing may itself be
    a starvation artifact, since dropped frames kill and respawn tracks — which
    would mean checkpoint 33 gated against an artifact. #10 kept and narrowed
    to exactly that question: replay the *good* corpus, count newborn fires in
    IDLE above the break floor, and either delete the gate as dead code or
    re-anchor it on a real clip. Kept where #11/#21 were closed because this
    one is live code whose rationale is known to be wrong, not a failure
    lacking a repro.
  - Terminology: **hand-rolled**, never "bowled" — this is pool.

- **2026-08-02 (checkpoint 51 — a settle re-arms the birth paths; the disarm
  could never clear):** the second, independent bug behind the same two
  `flag_missed` dumps, and the one checkpoint 50 explicitly did not cover.
  `stroke_gear_margin_0802` passed cold and failed warm, so only the live
  snapshot could show it. `vision/events.py`, +1 unit test (41 in
  `test_events.py`). No corpus change — cold replay cannot express this class.
  - **A latch with no self-clearing path.** A birth-path fire sets
    `_newborn_disarmed_at` — "one shot per rest cycle", so a single emergence
    cannot mint repeat shots. It was cleared in exactly one place,
    `_update_cue_rest`, and only once the **flagged** cue's own
    `_last_translate` stamp advanced past the disarm time. Park the whiteness
    vote on a ball that never moves again and that stamp never advances: the
    latch is permanent, and **both** birth paths — newborn and own-rest — stay
    dead for the rest of the session. This is the same failure shape as the v1
    carom guard (checkpoint 22): persistent state that the player's presence
    can starve, against which §2.3's answer has always been windowed,
    self-clearing evidence.
  - **The snapshot is the proof.** Unpickling `state.pkl` from the dump:
    `_newborn_disarmed_at = 478.29464936` — to the frame, the timestamp of the
    journal's `newborn: true` ShotTaken — captured at session t=491.486 with
    `_table_moving` already `False`. The table had settled **13 s earlier** and
    the latch was still set. Instrumenting every gate of `_maybe_open_rested`
    over the warm replay: every impulse-fast ball in the strike window,
    including the real struck cue running as non-cue track 100 at 990 mm/s,
    was rejected with `newborn-disarmed` before any other gate was consulted.
    The strike 25 s after the disarm had no path left to fire through.
  - **The fix is one line and one concept.** The disarm is scoped to a rest
    cycle, and a `BallsSettled` *is* that rest cycle — so it clears there, in
    the episode-closure block beside `_fired_this_episode`, `_episode_origin`
    and the roster reprune. Unlike one track's stamp, a settle cannot be parked
    on the wrong ball. The `_update_cue_rest` clear stays: it re-arms earlier
    when the cue does rest normally, and it is no longer the only way out.
  - **Warm replay cannot demonstrate this fix, and that is not a caveat to
    hide.** The snapshot was recorded by the pre-fix engine with the latch
    *already* stuck, and no settle falls between t=491.486 and the strike at
    503.07 — so warm still shows one BallsSettled and no shot. The fix acts at
    the settle at 480.56, which is before the snapshot exists. The damaged
    field read out of the pickle is the evidence; the pin is a unit test.
  - **The pin.** `test_settle_rearms_the_birth_paths_when_the_vote_is_parked`
    is `test_parked_vote_strike_fires_from_own_rest` extended: fire one
    wrong-flag strike, settle, then fire a second with the vote still parked.
    It fails on the checkpoint-50 engine and passes on this one, while all
    three existing parked-vote tests pass on both. Full cold battery green:
    357 passed.

- **2026-08-02 (checkpoint 50 — the stroke's own gear was vetoing the strike):**
  two `flag_missed` dumps from the evening session, 2.5 min apart. Ground truth
  from the table: one real struck shot each. Live, neither fired a `ShotTaken`
  — the clock never froze, counted down *through* the stroke (5.3 s left on the
  first) and only jumped back to 30 when the settle landed via the
  counts-the-shot fallback. Steven's read of the risk is the right one: shoot
  at 1 s remaining and the buzzer fires on a legal shot. Fixed, with the guard
  moving from a distance test to a direction test. `vision/events.py`, +1 unit
  test (40 in `test_events.py`), corpus 51 → 53 scenarios.
  - **The vetoer is not a ball.** Instrumenting `_maybe_open_candidate` on
    `stroke_gear_veto_0802`: at the impulse (raw 701 mm/s after a 12.9 s dwell
    — an unambiguous strike) five carom-guard stamps sit **138–148 mm** from
    the cue's rest spot and refuse it. They belong to a transient non-roster
    blob that appears on empty cloth **0.3 s before** the impulse, flickering
    merged and impulse-fast, so the hand-stamp rule (merged + non-roster +
    fast, checkpoint 33) marks it. The roster held 6 balls throughout and none
    was within 279 mm of the cue: it is the player's own stick/bridge gear,
    killing the shot it is in the act of taking.
  - **The 271/340 mm anchor is falsified.** `carom_reach_mm = 200` was
    justified on stroke gear reading "from behind the bridge — measured 271 mm
    (`medium_shot`) and 340 mm (`power_shot`) from the rest spot." These live
    strokes put it at **138 mm** and **152 mm**. §2.3 had already recorded the
    same gear at **66–72 mm** from `lone_roller_0801`'s *birth* spot, which is
    why hand stamps were excluded from the birth paths; the rest-anchored cue
    path kept them on the strength of the 271 mm figure, and that figure does
    not survive a normal stroke.
  - **Distance was exhausted, so the guard now reads direction.**
    `cue_grab_0801` — the phantom the hand stamps exist to reject — grabs from
    **116 mm**. Accept at 138, reject at 116: 22 mm apart, one measurement
    each side. No radius separates them. Physics supplies the axis distance
    could not: a stroke drives the cue **away** from the gear behind it, a
    grab drags it **across toward** the hand. Measured as the angle between
    the cue's departure and the direction to the stamp — **148–162°** for the
    two strokes, **61°** for the grab. The guard now exempts a hand stamp when
    that dot product is negative, a 90° boundary sitting 29° off the grab and
    58° off the nearest stroke, with no constant introduced to tune. It
    applies to hand stamps only: a genuine carom also sends the cue away from
    its bringer, so direction cannot judge ball movers, and they keep the pure
    distance rule. The exemption reuses `strike_dir_seed_min_mm` as its
    minimum trustworthy departure — a floor the candidate must clear anyway.
  - **`cue_merged` was measured and rejected as the discriminator.** The
    obvious alternative — "a carried ball is merged every frame, a struck ball
    on open cloth almost never" (§2.3) — reads `False` on the cue in *all three*
    clips, grab included. It would not have worked, and only instrumenting
    both candidates before writing code showed that.
  - **A second, distinct bug, not fixed here.** `stroke_gear_margin_0802`
    passed cold and failed warm. Warm replay (its snapshot restores clean,
    fingerprint `c68d9cf…`) shows the cue's *identity* had drifted: the
    whiteness vote's rest sat at (1325.4,719.1) while the real struck cue ran
    as non-cue track 100, which closed to **26 mm** — inside a ball diameter,
    so provably not a second ball — and vetoed the strike as a kiss. The cue
    self-vetoed. Cold rebuilds identity from scratch and cannot express it, so
    no cold pin covers it; the clip keeps its `state.pkl` on the T7 as the
    evidence. Own card.
  - **Cold used to pass by one frame.** Same clip, before the fix: gear stamps
    vetoed at 12.097 s and 12.110 s and the candidate opened at 12.123 s —
    **13 ms, one frame at 78 Hz**, after the last stamp aged out of the 0.4 s
    window. That it ever passed was timing luck, not a working guard.
  - **Scenarios:** `stroke_gear_veto_0802` and `stroke_gear_margin_0802`, both
    (1 shot, 0 scratches, exactly 1 settle), both referencing
    `empty_table_0801`. `KNOWN_FAILURES` is empty again. The new unit test is
    deliberately the grab test's twin — same blob, same kiss range, same
    impulse, only the departure direction differs — and it fails on the
    pre-fix engine while the grab test passes on both, so the pin is on the
    discriminator itself. Staging drained.

- **2026-08-02 (checkpoint 49 — the replay stops lying about the clock, and
  says when it is giving the table back):** live validation of checkpoint 46,
  from the table: *"the shot clock is still showing in the replay… and there is
  no transition from replay back to live so it's hard to tell when we're back
  on the live view."* Both are now fixed in the badge. `deck/replay.py`,
  `deck/obsws.py`, `/replay`, `scripts/setup_obs_replay.py`, +17 unit tests.
  - **The patch checkpoint 46 skipped as brittle is in — de-brittled by asking
    OBS.** Issue #13 predicted this exact outcome ("if not, the fix is an opaque
    patch over its rect — five lines, deliberately skipped as brittle"), and the
    objection was right: a hardcoded rect drifts off the overlay the day the
    layout moves. So nothing is hardcoded. At each press the deck reads the live
    scene's items, keeps the **enabled browser sources whose URL contains
    `/shotclock`**, converts each transform to a percentage rect of the canvas
    (`GetVideoSettings` — even the canvas size is asked for, not assumed) and
    hands them to the badge as `?mask=`. Identity is the *page*, so a renamed,
    moved, resized or duplicated overlay stays covered and a hidden one is
    skipped — it was never burned in. Measured against the live scene, the
    corner clock at 1141,0 193×193 becomes `59.32,0.00,10.26,18.06`, which is
    also what the unit test pins.
  - **It reads as layout, not as a hole**, and that was checked rather than
    hoped: the clock overlay's own background is opaque black, so compositing
    the real badge render over a real program frame (OBS screenshots, never
    touching program) shows the number vanishing from a panel that stays.
  - **Nothing was wrong with the transition — Fade was already set.** A replay
    is a pixel-exact rewind of the TV, so a 300 ms cross-fade dissolves one
    picture of the table into a nearly identical one; there is no transition to
    see because there is nothing to see *between*. The badge now fades the whole
    frame to black over its last 0.45 s, so black separates the two pictures and
    Fade reads as a fade-up to live. The seconds readout became a `LIVE IN n`
    countdown to the same instant: it says *when*, never *that* we are live,
    because that page only ever sits over old footage — the same rule the
    burned-in clock breaks.
  - **Two instants that look like one, and the bug lives between them.**
    Measured on the rig, three trials each: the badge page's clock starts
    **0.03–0.05 s** after `SetInputSettings`, but its first frame reaches OBS
    **0.567–0.570 s** later. Cut instantly and the badge is invisible — clock
    unmasked — for half a second. Give it time to paint and it is already half a
    second into its own animation, so the fade to black completes *after* the
    deck has cut away and is never seen. The deck now does both: it sets the URL
    and presses the source's own refresh early (behind the seek, which hides
    most of the wait), sleeps out the rest of `BADGE_PAINT_S`, then switches —
    and hands the page `play_s + BADGE_PAINT_S`, a timeline that includes its
    own invisible head start.
  - **The fade would have introduced a worse bug than it fixed, uncaught.** With
    `restart_when_active` on, the reload fires *at* the cut and OBS shows the
    previous frame for the ~0.57 s it takes — which, once the badge ends on
    black, is a fully blacked-out one. Every replay after the first would have
    opened on half a second of black. Found by cutting the program scene for
    real with nothing live; the setting is now off (a third load-bearing
    inversion in the setup script) and the deck owns the reload.
  - Everything above was measured against the running rig through the studio
    **preview** and `GetSourceScreenshot`, so the program scene and the wall TV
    were never taken over except for one deliberate 2 s cut.
  - Still open on #13: whether a replay dents detect Hz — moved to #9, which is
    already the detect-starvation card.

- **2026-08-02 (checkpoint 47 — the Session key powers the wall TV):** the one
  remaining manual step in "come down in the morning, press one button" was
  picking up the TV remote. The key now does it, and the work was almost
  entirely *measurement* — every plausible assumption about this TV was wrong.
  `deck/tv.py`, `scripts/pair_tv.py`, +18 unit tests (280 total).
  - **The TV is not what the docs said.** `hardware.md` §E1 and `BACKLOG.md`
    both described a VIDAA set and a "pairing-fussy" VIDAA network API. A
    subnet sweep found nothing on 36669 (the VIDAA MQTT port) and instead a
    Google TV answering on 6466/6467: the CanvasTV runs **Google TV**, so it
    speaks the **Android TV Remote** protocol. Both docs corrected.
  - **A short POWER press only reaches art mode.** This is the finding the
    whole afternoon hinged on. Tapping power on a CanvasTV drops it into the
    artwork screen, which keeps the panel lit *and the whole network stack
    up* — so probe after probe reported "the TV never left the network" and
    read as a dead keypress. **A long press (~2.5 s) reaches true standby**,
    and the port dies ~4 s later.
  - **POWER is a toggle, so it is never retried.** An early probe sent SLEEP,
    waited 25 s, decided it had failed, and sent POWER — switching the TV off
    and straight back on. It is now sent only when the TV's own reported state
    disagrees with the target, the same rule Home Assistant's
    `androidtv_remote` uses.
  - **Wake-on-LAN is the only way back.** In standby the TV leaves the network
    entirely and there is no Quick Start / Screenless service toggle on this
    model to keep it alive, so nothing can be *told* to wake. A magic packet
    brings it back — screen included — in about a second. It goes to
    `…:96/:97/:98` because the TV reports one MAC over the protocol while ARP
    shows the next one carrying traffic (two NICs on one board), and which one
    listens in standby isn't observable from the Mac.
  - **The Mac never goes headless.** Across a full off/on cycle the display
    list never emptied — it briefly degraded to a generic `spdisplays_display`
    during the wake handshake, then returned to `HISENSE`. So OBS's projector
    survives the night and **no EDID holder is needed**, which is what the
    fallback plan (a ~$60 Pulse-Eight USB-CEC adapter) would have cost.
  - **Two things outside the code.** Screen lock is now off — a locked Mac
    draws loginwindow *over* the projector, so the morning press would have
    lit the wall on a password prompt — and Start issues `caffeinate -u -t 2`
    to wake the display, because after 10 min idle the mini stops driving HDMI
    and the wall reads "no signal" however awake the TV is.
  - Sequencing reuses the existing harness rather than adding one: `tv_on` is
    idempotent and joins the OBS setup retry on the 2 s poll (renamed
    `_prepare_once`, since it is no longer only about OBS), and `tv_off` runs
    at the very end of the stop path so the wall reads `STOPPING…` until the
    session is actually over. A crashed `run` deliberately leaves the TV on —
    the wall is the one place that failure is visible from the table.

- **2026-08-02 (checkpoint 46 — instant replay: the TV cuts to the last shot,
  seeked):** Stream Deck phase 3. The Replay key saved a file nobody watched;
  it now cuts the wall TV to that file, starting 1.5 s before the shot, and
  comes back on its own. `deck/replay.py`, `scripts/setup_obs_replay.py`,
  `/replay`, `GET /api/last_shot`.
  - **The seek is the feature, and it needed no new vision.** Steven asked
    whether playback could start when the cue stick first appears. It can do
    better: every ShotTaken is already timestamped to ~20 ms of contact, so
    `cursor = duration − shot_age + display_lag − lead` puts playback where it
    belongs with arithmetic. Cue-stick detection would have been new detection
    work with no ground truth (the detector discards the stick blob *by
    design* — it is the oversized occluder, idea bank §2) to answer a question
    already answered. It would also have answered the wrong question: the stick
    appears at *address*, which can be ten seconds of standing still.
  - **Side effect worth naming:** the replay is as long as you make it. Press
    as the balls stop and it is ~5 s; press after wandering off and it is
    longer, capped at 20 s. No shot in the buffer plays the whole thing rather
    than failing — a press always shows something.
  - **The anchor is stamped before every gate** (kill switch, mode, IDLE break
    floor). Those decide whether a shot moves the *clock*; replay asks what is
    on the *video*. That is what makes the seek work in `no_clock`, which is
    where drills happen. Accepted: a phantom shot anchors a phantom replay.
  - **What is in a replay is not a choice the Replay scene can make.** The
    buffer records *program output*, so a replay is a pixel-exact rewind of the
    TV. Steven wanted a full-screen overhead; the overhead in the recording is a
    ~586×1080 portrait strip already downscaled to 57% (rotated 270° in
    `Stream Detail`), so cropping it to a 16:9 canvas would be a 3.3× upscale of
    a downscale. Nothing is cropped: the composed frame is the only pixel-native
    thing in the buffer and it carries both angles. A full-screen overhead
    replay is a *live layout* change, BACKLOG'd as such.
  - **100% speed, and the reason is the recording, not taste.** OBS records at
    30 fps, so half speed is frame-doubled mush; the camera's 160 fps never
    reaches the file, only detection. Real slow-mo has a prerequisite — OBS at
    60 fps — now written down where `SPEED` lives.
  - **A REPLAY badge is not decoration.** The live shot clock is burned into
    every replayed frame and keeps counting on the TV. `/replay` adds a scrim,
    a top-left wordmark and a hairline drain bar (top-left because a full-width
    band would clip the burned-in clock; the bar because the real question from
    twelve feet away is *when do I get my table back*). An opaque patch over
    the clock's rect was considered and rejected as brittle — it would break
    silently the day the overlay moves.
  - **Two OBS settings are load-bearing and both invert the default:**
    `restart_on_activate: false` (else the scene switch discards the seek) and
    `close_when_inactive: false` (the duration is read while the scene is
    dark). `scripts/setup_obs_replay.py` builds the scene and re-applies them,
    so a hand-edit in the OBS UI is repaired by re-running it rather than
    debugged at the table.
  - Order is unit-tested — load, read duration, seek, *then* switch — because
    seeking after the switch cuts the TV to frame 0 and then jumps. Every
    failure path leaves the program scene alone: OBS unreachable, no Replay
    scene, or a save that never lands all keep the TV live. 259 unit tests
    (+23); both the display-lag sign and the seek itself were mutation-checked.
  - Deck: Replay is one key in both directions (`REPLAY` → `CUEING…` →
    `BACK`/`TO LIVE`), still on the spine at 2,1.
  - **Found by smoke-testing against live OBS, after the first commit: OBS
    drops seeks silently.** `SetMediaInputCursor` issued in the ~70 ms after
    the media source first reports a duration — which is exactly when `show()`
    was issuing it — returns success and does nothing. That failure is
    invisible by construction: the replay plays from the top, and it reads as
    the lead being wrong rather than the seek being lost. The seek now proves
    itself (re-ask until the cursor lands; refuse the cut if it never does),
    which is the general lesson — an obs-websocket call that reports success is
    not evidence the thing happened.
  - **Keyframes every 1.90–1.97 s** (`ffprobe` on real replay files), so a seek
    always lands early and the *effective* lead is 1.5–3.5 s. The first
    tolerance for "did the seek land" was 2.0 s — a 0.03 s margin over the
    measured worst case, which would have started refusing good seeks the day
    the encoder preset changed. Now 4.0 s, anchored to the measurement.
  - Measured live across four presses: `show()` blocks the deck for
    **0.34–0.63 s**; saved files run 18.6–20.0 s against the 20 s buffer.

- **2026-08-02 (checkpoint 45 — deck layout v2: session modes, and the deck
  shrinks to what the mode can do):** fourteen of fifteen keys were spoken for
  with ~nine more queued, so the crowding was the presenting problem — but the
  fix that came out of designing it was mostly about *naming things correctly*.
  Eleven keys now, four spare, and three rules doing the work.
  - **Session mode is its own axis** (`clock/modes.py`, `POST /api/mode/{name}`,
    `GET /api/modes`). Steven's first instinct was to add "drill" as a ruleset —
    right that it shouldn't cost a key, wrong container: `rulesets.py` is a
    published-rules table whose docstring says *validated against published
    rules*, with a source per entry, and you can drill 8-ball patterns or 9-ball
    patterns. Mode is orthogonal to rulebook. Modes are **match** (a rack:
    break, boundaries, first-shot allowance, break gate), **run out** (fifteen
    balls thrown up, no rack — any-speed opener, base countdown on every shot),
    **no clock** (drills; vision journals but never clocks).
  - **"Drill" was the wrong word**, and the miss was instructive: it named a
    training activity when the difference is structural — *there is no rack*.
    Steven caught it by describing what he'd actually do ("throwing 15 balls up
    without racking/breaking and trying to run them out"), and once the mode was
    named for that, "New Rack" stopped grating on its own. An actual drill is
    what **no clock** is for.
  - This closes the BACKLOG item where the IDLE break gate ate honest
    ~1000 mm/s drill openers twice (checkpoint 33). Lowering the floor for
    everyone was always the wrong fix — placement lookalikes sit at ~1000 too.
    `run_out` has no floor because it has no break to gate. `MATCH_BREAK_FLOOR_MMS`
    moved to `modes.py` with its measurements; the gate's *input* semantics
    (what `speed_mms` means, why newborn fires are ignored) stayed in `hub.py`
    with the code that reads them.
  - **Reset and Force 30 were one key wearing two faces.** They differed by
    exactly one bit — both set the countdown to full, both rack-aware, both
    leaving extensions and shot count alone; one landed `IDLE`, the other
    `RUNNING`. Steven: *"I'm also unclear on the difference between those keys."*
    Merged into **Start N**, and the misnaming is now impossible: the face
    renders `current_base()`, so it says `START 60` inside the WNT first-shot
    window and `START 15` under Final 10. That is also the answer to *"we can't
    just have a Reset button because of the 60 second after break time"* — the
    button says which one it's about to give you. Parking the clock moved to
    No clock mode, where it's visible. `Snapshot` gained `base` to carry it, so
    the face is drawn from the same function the press calls and cannot drift.
  - **A key that can't be pressed goes dark, it doesn't dim** — Steven's call,
    and better than expected: dim stops being a state signal anywhere and
    survives only as typography (a cycle key's dim "next" line). One channel,
    one meaning. **Nothing reflows**: a removed key leaves a hole and every
    survivor keeps its cell, so position stays learnable. Match 11 keys → Run
    out 8 → No clock 4 → server down 1. Removal follows the *ruleset* axis too:
    `PUSH` is absent under either 8-ball rulebook, because push outs are 9-ball.
  - **Face grammar** — the `OFF`-button problem (checkpoint 43) was never the
    word, it was a *state* on a key read as a *command*. Three key types, three
    grammars: commands say the verb; cycles put the current value bright over
    the next value dim, so both are on the face and neither can be mistaken for
    the other; the one hold key states where it is and names the gesture out
    (`ON` / `HOLD 1s`, which moved from the face text to the dim sub-line).
  - **The spine**: Session, Mode, Replay, Flag work in every mode and hold the
    top-left 2×2, never moving. Position answers *does this key persist*, colour
    answers *how much time do I have* — two questions, two channels, which is
    why the spine isn't one colour. Group colour rides a **bottom stripe** and
    not the tile, because the tile is the press-outcome language: colour the
    tile and the green/red flash stops reading.
  - **Data graphics, no decorative ones** (`faces.Face`): extension pips (the
    pool reads *before* the press instead of after a red `NO EXT`), the live
    score numeral, the Start countdown, a session health dot. Four score keys
    became two on the deck's one hold rule — tap does the common thing, hold
    does the rare inverse.
  - **Flag keeps everything the 2026-08-01 split bought and moves the choice off
    the reflex press**: one key found by feel, then a reason page in the empty
    cells beside it. Labels, clip dirs and the journal's `flag_dump` line are
    unchanged, so `/triage-clips` needs nothing. An unanswered page files as
    `flag_other` after 4 s — what an unspecified flag always meant. Cost is the
    couple of seconds between presses, off a 20 s ring; no new API surface,
    which is why this beat dump-then-rename.
  - **Replay key** (`obsws.save_replay_buffer`) writes the buffer to disk.
    Playback on the TV — scene, stinger, slow-mo — is still phase 3.
  - **On the TV, `no_clock` hides the digits** and shows `NO CLOCK`. A parked
    clock renders a static `30`, which is indistinguishable from the stalled
    clock a missed settle produces — the exact failure Steven is trained to
    distrust. The control page gained a Mode selector, since the deck would
    otherwise be the only way out of `no_clock`.
  - Tests: 234 unit (+39). The deck suite is now written against *visibility*
    and *face grammar* rather than fixed cell contents, so it grades the rules
    instead of restating the table.

- **2026-08-02 (checkpoint 44 — the corpus battery gets two tiers):** the
  battery is the accuracy gate, and at ~5 minutes it was also the reason
  event-logic work happened in batches instead of in a loop. Three changes,
  each anchored to a measurement rather than a hunch.
  - **`ClipReader` reads with `os.pread`, not `np.memmap`.** Mapped reads of
    the exFAT corpus drive fault in at 4 KB granularity with no useful
    readahead: **207 MB/s against 958 MB/s** for one whole-frame `pread`
    (11.1 vs 2.4 ms per 1920×1200 frame), measured on cold clips with the two
    methods alternated across six clips so neither warmed the other's page
    cache. That was ~65% of the per-frame cost. Battery, 51 scenarios, `-n 4`:
    **7:48 → 5:05**. It also retires the 2026-08-01 note that "four workers hit
    the T7's sequential-read ceiling" — that ceiling was our own mapping, and
    a cold battery is now CPU-bound.
  - **The reference model is built once, not once per scenario.**
    `ReferenceModel` bundles the three static arrays `detect_balls` consumes
    (median gray, the shadow-rejection Bayer frame, the dilated cloth mask),
    ~0.30 s to build and previously paid 51 times over. **5:05 → 4:51.**
  - **`VisionPipeline.process()` splits into `detect()` + `advance()`**, both
    still pure. Per frame: raw read 2.4 ms, `detect_balls` 5.5 ms, churn
    0.4 ms — against **0.08 ms** for the tracker and event engine, which is
    where nearly all accuracy work happens. `pytest --detect-cache` stores the
    seam between the halves, so ~21 MB of cached detections stand in for
    ~134 GB of frames: **full suite 21 s, corpus alone 12 s.**
  - **Correctness rests entirely on the cache key**, so it is built from
    detection's *inputs*, not from a file list someone must remember to
    extend: `detect.py`'s bytes, `DetectConfig`'s values, a content hash of
    the three reference arrays (which covers the reference clip's pixels, the
    sampling, `build_reference`, `cloth_mask` and the dilation at once), clip
    identity, and the OpenCV/NumPy versions. Clip identity includes
    `frames.raw`'s size and mtime because metadata does not pin pixels —
    `medium_shot/meta.json` carries `"label": "soft_shot"` and every flag dump
    shares `"label": "flagged"` at the same frame count. Deliberately *not*
    keyed: `events.py` and `track.py` (the entire point), `pipeline.py`, and
    the calibration (detection runs on distorted pixels and never sees it, so
    a post-bump recalibration must not throw the cache away). Frame indices
    are stored and validated on load, because churn couples each frame to its
    predecessor *in the replayed sequence*.
  - **Cold stays the gate.** The key cannot cover environment drift below
    those library versions, and everything above the seam goes unexercised on
    a cached run. `--detect-cache` is the iteration loop for
    `events.py`/`track.py`; checkpoint batteries run cold. It buys nothing for
    a `DetectConfig` sweep — every grid point is a new key.
  - **`dump_state()` now refuses a cache-fed pipeline.** Churn differences
    against the previous frame's gray, which a cache-served replay never
    computes, so such a snapshot would resume with `prev_gray=None` and
    silently mis-compute churn on its first frame — a wrong answer, not an
    error. Consequence worth knowing during triage: every ring dump recorded
    before today is refused by *warm* replay anyway, since `pipeline.py` is
    part of the vision fingerprint. Cold replay is unaffected.
  - Two DRY/hygiene fixes carried along. `detect_indices()` moved onto
    `ClipReader` — the adaptive-stride rule from checkpoint 33 was duplicated
    in the corpus harness and the warm-replay test, which is exactly the kind
    of duplication that produced the half-cadence bug. And `tests/unit/` and
    `tests/corpus/` became packages: `test_warm_replay.py` exists in both, and
    under pytest's prepend import mode that basename collision has broken
    *whole-suite* collection since checkpoint 36 — `pytest tests/corpus`
    worked, plain `pytest` errored.
  - **195 unit tests** (+7: exact round-trip including the `None`-vs-`0.0`
    churn distinction and an empty frame, a key that moves for every keyed
    input, loud failure on corrupt or mismatched entries) and **57 corpus
    tests** (+1: cached replay must not move a single event against a cold one
    on `spin_in_place`, chosen because its settle is gated on churn — the one
    cached field derived *across* frames, so a mis-packed column would move
    the settle rather than fail loudly). ruff + mypy --strict clean.

- **2026-08-02 (checkpoint 43 — the key survives a deploy, and opens the TV
  itself):** two gaps the first real one-click sessions exposed.
  - **Adoption.** Shipping the `START` label meant a `launchctl kickstart`,
    which orphaned Steven's *live* session: the restarted daemon had no
    handle, read its own work as `DEV`, and the key went dead mid-practice.
    Killing sessions on deploy was never an option, so the daemon now records
    what it owns in `~/.cueclock-session.json` and re-adopts it on startup —
    `PidProcess` steers by pid (signal 0 for liveness). The ownership rule is
    unchanged, just recovered from disk: a terminal `run` writes no pidfile
    and still reads `DEV`, asserted by a test. It also plugs a leak the same
    orphaning caused — a `caffeinate -d` left holding a display assertion
    after its session was stopped by hand.
  - **The projector.** "Save projectors on exit" only restores what was open
    at the last quit, so any session that ended without one came back to a
    blank TV — which is exactly the state the key is supposed to make
    impossible. `obsws.open_projector()` now opens the program projector on
    monitor 0 explicitly, alongside the Syphon rebind, in one retried
    post-launch step. Verified idempotent: a second call reuses the existing
    projector window rather than stacking another.
  - Verified live on Steven's running session: the deploy adopted it without
    disruption, rebound the feed, and put the program on the TV — deck log
    `adopted the running session (run pid 25510)` → `Syphon Client bound` →
    `program projector on monitor 0`. 195 unit tests, ruff + mypy --strict
    clean.

- **2026-08-02 (checkpoint 42 — the black table feed: Syphon identity moves
  with the launcher):** first real one-click standup worked — server up, clock
  and scoreboard on the TV — except the overhead table feed was black, and
  Steven's own terminal launches showed it perfectly. OBS's own property list
  named the culprit: the source was bound to `[python3] cueclock`
  (`itemEnabled: false` — gone), while the live server was `[python] cueclock`.
  A `uv run python …` session advertises as **python3**; the deck daemon
  spawns `sys.executable` (`.venv/bin/python`), which advertises as
  **python**. OBS stores uuid + name + app_name, so a source configured by one
  launcher renders black under the other — and the UUID changes every run
  regardless. `obsws.bind_syphon()` now asks OBS which cueclock servers it can
  actually see and repoints the source at the live one; the session retries it
  on the poll until OBS's WebSocket answers (~10 s after launch), then stops.
  Asking OBS rather than guessing an identity is the point — it stays correct
  whoever started the session. Verified end to end: fresh cycle, source
  rebound with no human step, `GetSourceScreenshot` mean luma 128 (table
  visible, not black), stop clean in 0.7 s. 192 unit tests, ruff + mypy
  --strict clean.

- **2026-08-02 (checkpoint 41 — the quit dialog, actually fixed):** checkpoint
  40's fix did not work, and the way it failed is the lesson. Two bugs, both
  in the checking rather than the doing. (1) **A single status check raced the
  buffer.** `--startreplaybuffer` arms asynchronously, so a stop a few seconds
  after launch saw `outputActive: false`, skipped the stop, and the buffer
  started anyway — dialog. `stop_all_outputs()` now loops until OBS reports
  every output (replay buffer, record, stream, virtual cam) stopped, and
  `active_outputs()` returns `None` for *unknown* so a failed request can
  never be mistaken for "nothing is running". (2) **The dialog detector was a
  false positive.** It ran `pgrep` immediately after the quit, but a graceful
  OBS quit takes 0.3–3 s, so every clean quit looked stuck and the deck
  clicked a dialog that wasn't there — which is exactly why the first fix
  looked like it was working when it wasn't. The check moved to the STOPPING
  tick, 5 s after the stop begins, and clicks at most once. Isolating the two
  (launch → inspect outputs → stop → quit, no session involved) is what
  separated them; the session-level test could only ever say "dialog again".
  Verified: five full cycles across the arming race window (3/6/8/12/20 s of
  uptime), stop returning in 0.7 s, OBS exiting clean, backstop never firing.
  190 unit tests, ruff + mypy --strict clean.

- **2026-08-02 (checkpoint 40 — the Session key's stop path, first attempt):** Steven held the key and got a modal OBS dialog on the
  TV: *"OBS is still currently active. All streams/recordings will be shut
  down."* It fires whenever OBS quits with an output running — the replay
  buffer, enabled the same morning — and there is no keyboard at the table to
  click it. The deck log shows both consequences: `osascript ... to quit`
  blocked for its full 20 s timeout, and because the stop clock starts before
  that call, SIGINT's entire 20 s grace was gone before the signal was sent,
  so `run` got a pointless SIGTERM escalation. Two fixes, both ordering:
  **`run` is signalled first** (it owns the camera; nothing OBS does may delay
  it), and **the replay buffer is stopped over obs-websocket before the
  quit**, so no output is active and no dialog appears. New `deck/obsws.py` is
  a minimal obs-websocket v5 client — one request per connection (open,
  authenticate with the v5 sha256 challenge, send, read, close), every call
  best-effort so a closed OBS or a disabled WebSocket server can never raise
  into the deck loop; verified against the live server before wiring. There is
  no OBS setting that suppresses that dialog — stopping the output is the
  only fix. 188 unit tests (the stop test now asserts the full call order),
  ruff + mypy --strict clean.

- **2026-08-02 (checkpoint 40 — the corpus battery: 7:48 → 12 s):** accuracy
  work was rate-limited by the scoreboard. 51 pinned scenarios, ~58,000
  replayed frames, ~134 GB off the T7 per run, and every flagged clip made it
  permanently worse. Two independent fixes, measured end to end.
  - **`ClipReader` reads with `os.pread`, not `np.memmap`.** Mapped reads of
    the exFAT corpus drive fault in at 4 KB granularity with no readahead:
    measured on six cold clips with the methods alternated (so neither warms
    the other's page cache), **207 MB/s vs 958 MB/s** — 11.10 vs 2.40 ms per
    1920×1200 frame, ~65% of the old per-frame cost. Battery `-n 4`:
    **7:48 → 5:05**, with user CPU identical to 0.5% and system time falling
    442 s → 127 s. That split is the proof it was page-fault overhead and
    nothing else moved. The 2026-08-01 note below claiming four workers "hit
    the T7's sequential-read ceiling" was wrong: the ceiling was ours.
  - **Detection cache (`capture/detect_cache.py`, `pytest --detect-cache`).**
    `VisionPipeline.process()` split into `detect()` (frame-bound, 5.9 ms/frame,
    stable) and `advance()` (state-bound, **0.08 ms/frame**, the half under
    active tuning); both stay pure, and the two halves share no state, so a
    whole clip's detections can be computed or loaded up front. Cached, the
    battery skips both the T7 read and the detector: **18 MB of detections
    stand in for 134 GB of frames.** Full suite `-n 4`: **21 s**, corpus alone
    **12 s**.
  - **The key is built from detection's *inputs*, not a list of source files.**
    New `ReferenceModel` bundles the three arrays `detect_balls` consumes and
    content-hashes them, so the reference clip's pixels, the 15-frame sampling,
    `build_reference`, `cloth_mask` and the dilation are all covered at once —
    four holes a file-list key would have had. Plus `detect.py`'s bytes,
    `DetectConfig`, clip identity including `frames.raw` size+mtime (metadata
    alone does not pin pixels: `medium_shot/meta.json` says
    `"label": "soft_shot"`), and the cv2/numpy versions. Deliberately absent:
    `events.py`/`track.py` (the entire point), `pipeline.py` (would
    self-invalidate on cache-glue edits), and the calibration — detection runs
    on distorted pixels, so a post-bump recalibration must not throw it away.
    Frame indices are stored and validated, never re-derived: churn couples
    each frame to the previous one *in the replayed sequence*.
  - **Efficacy is unchanged, and that is tested.** Cold remains the default
    and the gate; `--detect-cache` is opt-in. `test_detect_cache_replay_matches_cold`
    replays `spin_in_place` — chosen because its settle is gated on churn, the
    one cached field derived *across* frames — and requires cold, miss and hit
    to produce identical event lists. Seven unit tests pin exact round-tripping
    (including the None-vs-0.0 churn distinction, empty frames, and detection
    order, which the greedy tracker depends on), key completeness as a
    collision test, and loud failure on corrupt or mismatched entries.
    float64 throughout: the corpus turns on margins like a 12.04 mm tug
    against a 12 mm floor. `dump_state()` now refuses a cache-fed pipeline —
    `prev_gray` is never computed, so such a snapshot would silently
    mis-compute churn on the first frame after a warm resume.
  - **Also fixed: `uv run pytest` has been erroring at collection since
    checkpoint 36** — `tests/unit/test_warm_replay.py` and
    `tests/corpus/test_warm_replay.py` share a basename with no package
    markers, so pytest's prepend import mode collided them. That is the exact
    command CI runs, so CI has been red. Added `__init__.py` to `tests/unit/`
    and `tests/corpus/`; bare `pytest` now collects and passes 244 tests.
  - Reference/calibration construction is now `@cache`d per worker (was rebuilt
    per test at ~0.30 s): −16% user CPU on the cold run, and it would have been
    the floor of the cached tier otherwise.
  - Sequencing note for anyone repeating this: the first measurement of the
    memmap cost came from a subagent that benchmarked *while a battery was
    running* and reported 35 MB/s — 6× off, and it inverted the expected
    payoff. Contended micro-benchmarks are worse than none. Re-measured quiet.

- **2026-08-02 (checkpoint 39 — the Session key: one press owns the whole
  session):** Stream Deck phase 2, the top-left key that makes the rig
  keyboard-free (`hardware.md` §C4, BACKLOG since 2026-07-31).
  - **Tap while `OFF`** spawns the canonical `run` (README) plus a
    `caffeinate -d` assertion, shows `STARTING…`, and launches OBS on the
    first poll where the server answers — server first, always, because a
    browser source that loads against a dead server parks on a silent error
    page and never retries (§B3, the checkbox that heals it is the same
    finding). **Hold 1 s while `ON`** quits OBS by AppleScript, SIGINTs `run`,
    and shows `STOPPING…` until the child actually exits, then releases the
    assertion. A tap while `ON` does nothing on purpose: a stray press that
    killed the camera mid-rack would be the worst key on the deck.
  - **`sys.executable`, not `uv run`** — the daemon must never block on uv's
    lock while another cueclock command holds it (the contention that has bit
    live sessions all week). Output appends to `~/Library/Logs/cueclock-run.log`.
  - **Ownership is the whole design.** The manager only ever signals children
    it spawned itself, so a terminal `run` reads `DEV` and is left alone —
    and a session the daemon spawned **survives the daemon's own exit**, so
    the `launchctl kickstart` ritual after a layout merge can never kill a
    live practice session. The trade, accepted: the survivor reads `DEV`
    afterwards and has to be stopped from a terminal.
  - **Failure paths all land somewhere honest**: no server within 60 s ⇒
    SIGINT + `OFF` (with the log path printed); `run` crashing on its own ⇒
    `OFF` and the assertion released; a wedged `run` ⇒ one escalation to
    SIGTERM at 20 s, never SIGKILL, and never `kill` for OBS (a killed OBS
    reopens in safe mode without the projector — unfixable headless).
  - **Shape:** `deck/session.py` is a pure state machine over injected
    `spawn`/`call`/`now`, advanced by the daemon's existing 2 s poll rather
    than a thread, so the deck keeps rendering every other key while a
    session comes up — and the entire start/stop sequence is unit-tested with
    fakes (10 new tests; 187 total, ruff + mypy --strict clean). The Session
    key is also the one key that stays lit when the server is down: dimming
    the key that starts the server would hide the way back up.
  - **Deferred with a reason:** the spec's "verify projector + replay buffer
    via obs-websocket" step. obs-websocket is disabled on this machine
    (`server_enabled: false`), so there is nothing to verify against; it
    lands with phase 3's Replay key, which needs the same client. Enabling
    the WebSocket server, the replay buffer, "Save projectors on exit", and
    removing OBS from Login Items stay one-time manual steps in the OBS UI.

- **2026-08-02 (checkpoint 38 — the missed ball-in-hand stroke: witnessed
  dwell, chatter-revised stamps, feasibility confirm):** first reason-coded
  flag of the post-36 era (`FLAG MISSED`, 02:51 UTC) and the first warm
  dump — `analyze --warm` reproduced the live session to the millisecond on
  its first production use. The miss: a real 1396 mm/s stroke on the
  post-scratch replacement cue while the whiteness vote sat parked on
  another white-ish ball (it recovered 2 s post-impulse, at the vote's
  motion-freeze release); settle-counts-the-shot patched the count 11 s
  later, so the clock ran through the stroke. The own-rest path — built for
  exactly this (checkpoint 32) — stayed shut because sustained address
  churn (raw 300–3000 mm/s on a ball wobbling ≤ ~10 mm) kept refreshing the
  ball's translate stamp: the checkpoint-35 residual, live four hours after
  it was written down. Closing it honestly took three rules (all existing
  constants; measured anchors cited in `design.md` §4.2 and the code):
  - **Chatter-revised stamps:** translate stamps land immediately — a ramp
    or bump still breaks the strike dwell on its first supra-gate frame —
    but an excursion that closes within `moving_window_s` with net travel
    under `moving_enter_mm` failed the table's own motion-evidence bar:
    chatter, and its stamps are revised away. (A first "persist before
    stamping" design corrupted the carom guard's mover evidence and let the
    OBJECT ball of the missed stroke mis-fire off its 10 s rest — caught by
    the kiss guard once stamps flowed honestly again.)
  - **Witnessed dwell:** with the stamp still broken at an impulse, the
    ball's own history may vouch for its rest — every observed position
    through the pre-strike window inside `moving_enter_mm` of the rest spot
    and **no unobserved run longer than `moving_window_s`**. That last
    clause is what finally separated `tip_bump`'s 3.5 s event — re-measured
    tonight at 203 mm of travel emerging at ~600 mm/s, kinematically a soft
    shot, not the 47 mm nudge checkpoint 10 described — from a real strike:
    a push rides the ball out of sight (666 ms unobserved; its emergence
    frame is stored AT the rest spot by coast smoothing, so the gate trusts
    observation timing, never reported positions), a strike's stick passes
    through contact (worst gap 77 ms, chatter flicker included).
  - **Feasibility confirm:** a candidate that can no longer reach the 60 mm
    confirm floor in its remaining window (displacement + raw × time left)
    dies immediately — no more lingering at rest where a stick-merge
    centroid jump can fake the distance.
  Corpus: `address_chatter_miss_0801` promoted (1, 0, 2, 2) plus an
  impulse-window pin (fires at 10.14 s cold, red pre-fix live and cold);
  `tip_bump` stays green at 0 shots. Warm-snapshot note: this changes the
  vision fingerprint, so tonight's `state.pkl` is version-locked — by
  design; the clip pins cold, which is stronger. Unit guards: address
  chatter must not close the own-rest window (red pre-fix), an occluded
  push must not open it, a short bump's candidate dies at rest not expiry.
  169 unit tests; 56 corpus scenarios green; ruff + mypy --strict clean.

- **2026-08-01 (checkpoint 37 — flag dumps stage locally; `/triage-clips`
  skill):** the flag-dump starvation BACKLOG item, decided from data and
  closed — and the off-table half of the Flag keys finally built.
  - **The measurement.** Steven reported the OBS wall-TV display stuttering
    and, at 22:55, cutting out entirely. It was not OBS and not the M2:
    across that whole 7:28 recording OBS output 13,447 frames with **5
    lagged (0.0%)**, and through every freeze the Syphon overhead pane kept
    updating and the clock browser source kept ticking. Only the Camo
    side-angle source stalled — 9 freezes ≥ 0.2 s, worst run 0.80 s. Per-pane
    frame differencing on the recording, correlated against the session
    journal's `detect_heartbeat`, put the worst cluster on the flag press at
    22:51:34: **`hz` 75 → 45.3**, `frame_age` spiking, 28 frozen Camo frames
    in one 5 s interval and blips for 20 s after. The dump was 3.6 GB
    (`frames.raw` 3,598,848,000 B) written to `tests/clips` — the T7, a
    **USB** SSD sharing its bus with the phone feeding Camo. Flagging a
    failure was degrading the session being flagged, and visibly stuttering
    the TV. (Checkpoint 36's heartbeat is what made this readable at all —
    exactly the "decide throttle-vs-accept from that data" the item asked
    for. Neither: move the write.)
  - **`run --dump-dir`** (default `~/cueclock-dumps`): dumps stage on the
    internal SSD. Off the USB bus, and outside iCloud's scope — in-repo
    staging was never an option since the Desktop is synced (the same sync
    that stamps `UF_HIDDEN` and forced `.venv.nosync`, checkpoint 13). The
    root is created at startup, not at flag time, so a bad path fails at
    launch rather than at the table with evidence expiring; the writer
    carries `record`'s 5 GiB headroom guard and names the drain remedy in
    its error. `/api/dump`'s stale "(is the T7 mounted?)" hint is gone — the
    T7 is no longer in that path.
  - **`/triage-clips` skill** (`.claude/skills/triage-clips/`, BACKLOG deck
    phase 4; flow spec `hardware.md` §C5): inventory staging → **ground
    truth from Steven first** (sparse stills mislead; a wrong pin is worse
    than no pin) → route on the reason code (`flag_other` checks the
    presentation side before any vision replay — that mistake cost two
    triage passes on 2026-08-01) → correlate the session journal, reading
    `detect_heartbeat` `hz` before believing any detection bug → **warm then
    cold** `analyze`, with a matrix for what the pair means (warm-fails/
    cold-passes = accumulated-state divergence, the class cold replay cannot
    show; both-pass = starvation or presentation, not a vision bug) →
    promote / keep / delete → drain. Promotion covers scenario naming
    (`<scenario>_MMDD`, behavior not reason code), the move to the T7, the
    `EXPECTED` entry with its ground-truth comment, when counts alone need a
    bespoke timing test, `pytest -n 4`, and a CHANGELOG entry.
  - Docs in the same commit: `docs/cli.md` (`--dump-dir`, ground rules,
    triage recipe), `hardware.md` §C5. 167 unit tests; ruff + mypy --strict
    clean. **Not** updated here: the BACKLOG item for deck phase 4 and the
    starvation item both want checking off, but `BACKLOG.md` had concurrent
    edits in the working tree and was left alone.

- **2026-08-01 (checkpoint 36 — warm replay: ring dumps carry live state;
  journal heartbeat + frame timestamps):** two triage gaps closed, both paid
  for the hard way this week. (1) Cold replay starts the pure core from
  empty state, so live failures caused by *accumulated* state —
  whiteness-vote position, roster membership, motion/episode latches — never
  reproduce offline (checkpoint 32's shot-1 miss and 9 s-late settle,
  checkpoint 33's white-shaft-sweep fires, all "cold-invisible"). (2) The
  journal carried no unambiguous frame-capture stamp and nothing measured
  detect-loop lag, so the flag-dump starvation (detection at ~38 Hz for
  ~10 s, `drill_shot_0801`) left no journal trace.
  - **State snapshots in `run`:** a rolling deque of the vision core's
    pickled state at ~1 Hz spanning the ring
    (`VisionPipeline.dump_state` — whole-object pickle of tracker + event
    engine + prev-gray frame, deliberately schema-free so engine state
    added later rides along automatically). Measured on real mid-shot
    state: **2.33 MB, 0.9 ms mean / 2.4 ms max per snapshot** — negligible
    against the 12.8 ms detect budget at 1 Hz (~58 MB resident for the
    default 20 s ring's retention). A flag dump writes the oldest snapshot
    inside the ring window as `state.pkl` + `state.json` (schema version +
    a fingerprint of the state-bearing vision sources, so a stale pickle
    fails loudly instead of rehydrating onto changed classes and mis-running
    silently; `snapshot_t`/`snapshot_ns` locate it in both timelines).
  - **`analyze --warm`:** restores the snapshot, skips the frames it
    already contains, and replays the rest at session-relative timestamps
    (matching the dump's `events.jsonl`) — reproducing exactly what the
    live session computed. No snapshot → cold replay plus a printed note;
    fingerprint/schema mismatch → loud refusal with the recovery options.
    Cold replay stays the default: corpus grading wants current-code-from-
    empty-state semantics.
  - **Determinism acceptance (the point of the feature):** running the core
    with a snapshot taken midway, then resuming from that snapshot, must
    equal the uninterrupted run event-for-event. Pinned three ways:
    engine-only over a scripted two-shot scenario at every 10th cut point;
    tracker+engine over scripted observations (occlusion coasting,
    whiteness vote) at every 20th; full VisionPipeline over `medium_shot`
    split at the midpoint (corpus, skips without clips). Plus an
    end-to-end CLI check: a synthesized ring dump warm-replayed to the
    byte-identical event stream of its simulated live session.
  - **Journal upgrades (strictly additive — old readers unaffected):**
    vision-event records add `frame_t`, the confirming frame's capture
    timestamp, named unambiguously against the record's emit-time `mono`
    stamp (`t` keeps the same value for existing tools); a
    `detect_heartbeat` record every ~5 s carries `hz` (effective detect
    rate), `frame_age_s` (staleness of the newest processed frame), and
    its `frame_t` (the camera→journal clock anchor, making per-event
    capture→emit lag computable). Emitted from the Hub tick loop, which
    keeps beating while the camera thread is starved — the starvation
    BACKLOG item is now measurable from any session journal. `flag_dump`
    records gain `warm: true/false`.
  - Docs in the same commit: `docs/cli.md` (`--warm`, run/dump notes),
    `design.md` §2.1 (dump + journal contract, appended). 161 unit tests
    (4 new); corpus battery green; ruff + mypy --strict clean.

- **2026-08-01 (checkpoint 35 — settle-dwell chatter guard: the SETTLE STALL
  root-caused):** the 21:50 UTC flag (one dump, 20 s ring). Steven's shot
  fired correctly (live and cold agree: 1507 mm/s, rest-anchored) and every
  ball was at rest ~3 s later — then no settle for 11.6 s live, and the cold
  replay is worse: **no settle at all** in 10 s of quiet. The session's last
  two racks clustered shot→settle at 12.0–16.3 s (10 of the last ~16 shots)
  vs the 3–8 s norm.
  - **Mechanism — one corrupt blob's chatter wiped the settle dwell every
    few tenths of a second:** the cue stick lay across a parked orange
    ball; the thresholded diff kept only a marginal half-ball crescent
    (area p50 676 px² vs ~1300+ nominal) whose centroid flips between two
    modes 7–10 mm apart (raw deviation p95 10.6 mm; every other parked
    ball ≤ 0.6 mm). Each flip is a 13–64 ms excursion past the 5 mm creep
    gate, and a single supra-gate frame reset `_all_still_since` outright —
    the §4.2 "hysteresis so it can't chatter" was one-sided, since the
    original settle implementation. Settle became a wait for a lucky
    excursion-free 0.5 s window: the measured chatter cadence (~0.2–0.3 s)
    makes that window rare (the clip: never). The same lottery is the long
    tail in EVERY session's latencies (17.9/18.4 s at 17:37, 17.3/50.0/
    47.6/19.1 s at 19:22, 17.0/16.9 s at 20:40) and checkpoint 32's
    "unexplained 9 s late settle / state divergence" — explained: not
    state divergence, just different dice. Not a 32/33 regression; tonight's
    drill posture (stick parked over a marginal ball) made it frequent and,
    for the first time, cold-reproducible.
  - **Fix — symmetric hysteresis, no new constant** (`vision/events.py`,
    design prose `design.md` §4.2): an un-still verdict must persist for a
    full `moving_window_s` (0.2 s, the existing displacement-evidence
    window) before it erases dwell credit; BallsSettled still fires only on
    a still-verdict frame with full credit, so genuine movers block settle
    from their first frame exactly as before (hanger_pot's young-roller
    protection untouched) and zero-ball frames keep the hard reset.
    Anchors: corrupt excursions ≤ 64 ms (40+ runs measured on the clip),
    genuine motion holds the verdict continuously ≥ 371 ms corpus-wide
    (hanger_pot 371/794/1434 ms, slow_creep one 3469 ms run whose end +
    dwell IS its settle time) — the window sits 3.1× above the worst
    corrupt run, 1.9× under the shortest genuine one. The clip's settle:
    none → 10.23 s (0.5 s after its creep-gate rest at 9.7; shot→settle
    3.3 s, back in the norm).
  - **Residual, BACKLOG'd:** `_last_translate` still refreshes on corrupt
    excursions (a chattering ball keeps its spin-watch armed, defers
    pickup-discard judgment, and cannot open own-rest strikes) — no live
    failure observed; the persistence rule extends there if one appears.
  - **Corpus:** `settle_stall_0801` promoted (ring dump renamed from
    `flagged`), pinned (1, 0, 2, 2) — the second settle is the fix (red
    before it: 0 settles), the 1.29 s one closes a cold-start warmup
    episode (founding-window stick fragment; live was long settled) — plus
    `test_settle_stall_settle_is_prompt` pinning the settle into
    rest + two dwells (9.7–10.7 s), so a chatter-lottery regression reads
    red even with counts intact. 46 scenarios, 53 corpus tests; 158 unit
    tests (new:
    `test_observation_chatter_does_not_stall_settle`, red on pre-guard
    code; `test_pocket_creep_blocks_settle` is the sustained-motion
    control); ruff + mypy clean.

- **2026-08-01 (checkpoint 34 — reason-coded flag keys: triage starts from
  *why*):** tonight an OBS-side failure (the camera source died on the wall
  TV) was flagged and then chased as a vision bug through two triage passes —
  the dump label was always `flagged`, so the clip carried no hint that the
  clock and events had been fine and only the presentation was broken. The
  single Flag key is now three reason-coded keys on the right half of row 2:
  **FLAG PHANTOM** (2,3 — an event was counted that shouldn't have been,
  label `flag_phantom`), **FLAG MISSED** (2,4 — a real shot/settle wasn't
  detected, or came late, `flag_missed`), **FLAG OTHER** (2,5 — display
  problems, anything else, `flag_other`). Each presses the same
  `POST /api/dump?label=<x>` contract (unchanged since 2026-07-29 — no server
  edits); the label names the clip dir (`tests/clips/<label>_N`) and rides
  the journal's `flag_dump` line, and `/triage-clips` (deck phase 4) can
  route on it: phantom/missed → vision replay, other → OBS logs first.
  A bare `POST /api/dump` still means `flagged` (pinned by a server test).
  Placement decisions: (2,5) — the old Flag cell — stays a flag key so a
  pre-split reflex press still saves the ring instead of no-opping while the
  20 s of evidence expires; OTHER inherits it because "unspecified" is
  exactly what the old key meant. To make the run contiguous, Force 30 moved
  to (2,2) (beside Reset: the clock-recovery pair — row 2 now reads recovery,
  then evidence), New Rack to (3,3) (mid-score-row: the whole between-racks
  ritual on one row), and the planned Replay key to (1,2). Faces are words,
  not glyphs, per the deck's rendering style: `FLAG` over
  `PHANTOM`/`MISSED`/`OTHER` — the family reads as one group and the second
  line is the reason, matching the clip name triage will see. `hardware.md`
  §C2/§C5, `README.md`, and `BACKLOG.md` updated in step. The running deck
  daemon renders the old layout until `cueclock deck` is relaunched.


- **2026-08-01 (checkpoint 33 — episode closure: a fire is evidence; eight
  evening dumps triaged):** two drill sessions on the checkpoint-32 build
  (18:30–18:55 and 19:22–19:28 UTC) produced eight flag dumps. Triage
  lesson repeated the hard way: the first-pass replays ran `analyze` at its
  fixed `--stride 2`, which halves the cadence on ring dumps (they already
  store detect-stride frames) and produced *different events than the live
  session* — every conclusion drawn at 39 Hz was wrong and had to be
  redone at 78 Hz (`analyze` now adapts its default stride; `docs/cli.md`
  updated). Frame verification also overturned two working hypotheses:
  both "hand-bowl" candidates were real stick shots.
  - **The dominant failure was missing settles, not phantom shots.** Three
    independent holes, all closed with structural rules and no new
    thresholds (`design.md` §4.2 checkpoint-33 bullet):
    (1) a birth-path strike whose only mover is the fired track never
    latched table motion — the clock froze in MOTION 33 s live
    (`lone_roller_0801`); a ShotTaken now latches motion and admits its
    ball to the roster. (2) The pickup discard unlatched live shots when
    the struck ball's original track died mid-streak
    (`pair_strikes_0801`, 14 s freeze) and when zero-ball scatter frames
    made its guards vacuous (`break_0801b`); a fired episode is never a
    pickup and an empty frame is never judged one. (3) Birth-evidence
    strike paths opened mid-episode once the mover clock went stale in
    scatter churn — a scatter rebirth at 906 mm/s fired as a "strike"
    2.2 s into a live break; newborn/own-rest opens now require an
    unlatched table.
  - **Hand-grab phantom through the cue path** (`cue_grab_0801`): a gloved
    hand grabbed the resting flag-holder — snatch = impulse, drag =
    coherent travel, so every rest-anchored gate passed, and the 209.6 mm
    on-table drag beat the 200 mm pickup ceiling (now 400, re-anchored) to
    mint a phantom settle too. Merged, impulse-fast, non-roster tracks
    (the hand blob, measured 618–1921 mm/s at 116 mm from the rest) now
    stamp the carom guard — cue path only: the same rule on the birth
    paths vetoed `lone_roller_0801`'s real strike (the stroke's own merged
    blob stamps 66–72 mm from the birth spot), which is why stamps carry
    ids and a hand flag.
  - **IDLE gate ate a real break** (`break_underread_0801`): the white
    streaked too fast to be *observed* and the fire reported 705 mm/s on a
    frame-verified rack explosion. `ShotTaken.speed_mms` now also carries
    the confirm's own kinematics (displacement ÷ confirm time) — still a
    lower bound on true peak, but journal speeds read higher than the old
    estimator on fast shots; re-measure before citing pre-33 margins. The
    cold replay never confirms that break at all (candidate follows the
    flag; the streak dies first) — pinned honest (shots unasserted),
    BACKLOG'd with the live-validation plan.
  - **Six more dumps arrived mid-triage (flagged_9–14, 19:33–19:55 UTC,
    same live session)** — all instances of the classes above, plus one
    new measured discriminator. Two hard bowled returns during rack setup
    read 3926 and 3489 mm/s — above the IDLE break floor — and each
    STARTED the rack as "shot 1" mid-setup. Every real break in today's
    corpus fires rest-anchored (`newborn=False`: break_0801, break_0801b,
    break_underread_0801, cluster_shot's session break) while a bowled
    ball is always a newborn track (it materializes leaving a hand), so
    **the Hub's IDLE gate now also ignores newborn fires**
    (`test_idle_break_gate_rejects_newborn_fires`; residual: a break from
    a fully occluded address would be eaten — Restart recovers).
    flagged_13's two mid-MOTION re-fires die under the latch guard; the
    quiet-table bowls that count mid-rack shots (flagged_10/11/13's
    first) remain the acoustic residual.
  - **Residuals, BACKLOG'd:** quiet-table bowls/placements fire the
    newborn path mid-rack (acoustic class — a stick strike clicks, a hand
    is silent; apparent-size/height as an idea-bank lever); bare-hand
    snatch beats all three carried gates (`pickup_snatch_0801`, pinned
    known-wrong); white-shaft sweep births whiteness-eligible phantom
    tracks (fired twice live, cold-invisible); flag-dump writes halve the
    detect cadence for ~10 s (`drill_shot_0801`'s ring spans 41 s); drill
    openers under the IDLE floor still need the Start key.
    `cluster_shot_0801` pins a session flag whose journal, frames, and
    replay all read *correct* — ground-truth question for Steven.
  - **Corpus:** eight scenarios promoted (`cue_grab_0801`,
    `break_underread_0801`, `lone_roller_0801`, `break_0801b`,
    `pair_strikes_0801`, `cluster_shot_0801`, `pickup_snatch_0801`,
    `drill_shot_0801`) — 45 scenarios; 6 new unit guards (fire-latch
    settle, fired-episode discard, scatter-gap discard, hand-grab guard,
    hand-stamps-vs-birth-paths, kinematic speed); ruff + mypy clean.

- **2026-08-01 (checkpoint 32 — own-rest strike path for a parked whiteness
  vote):** first flag of the match-day guest session (16:12 UTC, one flag in
  five shots on the dump journal). Steven's report: one missed shot. The
  ring window actually holds **two live strike misses** — the first was
  silently patched by settle-counts-the-shot 11 s later (clock ran through
  the stroke, then a 9 s-late settle reset it), the second (the flagged one)
  produced no live events at all.
  - **Root cause — the whiteness vote parked on the wrong still ball:**
    shot 1 rolled the cue into a cluster where it rested against the blue;
    the merge corrupts its color sample, the vote wandered to the yellow
    mid-table (t=6.5 in the dump), and stayed there. Shot 2 struck that
    rested cue: telemetry shows a 9.5 s own-rest, two occluded frames at
    contact, emergence at 619 mm/s with smooth decay — and the vote
    returning only 0.6 s post-impact. Both strike paths structurally blind:
    rest memory anchored to the parked flag-holder 578 mm from the impulse
    (handoff path punts to newborn), and the struck track was 11 s old
    (newborn path correctly rejects). Cold replay reproduces the shot-2
    miss exactly; shot 1 it catches (live vote state predates the ring).
  - **Fix — third identity-carrying path (`_maybe_open_rested`):** when the
    flag-holder shows no impulse, a roster ball stepping to impulse speed
    from a long own-rest opens a candidate anchored on its own rest spot.
    Reuses only measured gates (roster admission, `pre_strike_dwell_s` on
    the ball's own `_last_translate` — read before the impulse frame
    updates it, so a hand ramp that trips the creep window early can never
    show a dwell — carom guard, seed floor/ceiling, and the newborn
    carried-ball confirm gates). **No new thresholds.** Design prose:
    `design.md` §4.2.
  - **Corpus:** `parked_vote_strike_0801` added, pinned (2, 0, 2, 2), the
    first clip referenced against `empty_table_0801` (the §E2 refresh
    earlier that day). Post-fix replay: ShotTaken 0.69 + 12.54, settles
    3.14 + 17.97, no scratch. Unit-guarded three ways (fire from own rest,
    merged hand-carry control, hand-ramp control) on a Sim extended with a
    parkable cue vote. 37 scenarios; ruff + mypy clean.

- **2026-08-01 (checkpoint 31 — Scratch no longer disarms the newborn strike
  path):** the first post-30 session (13:59–14:07 UTC, checkpoints 29+30
  live) ran two full racks cleanly — 22 shots, every strike/settle correct —
  until one flag: a shot out of the jaws of a down-table pocket wasn't
  recognized (Steven's report; journal confirms a settle with no
  `shot_taken` bumping `rack_shots` 9→10 via the fallback at t=430.7).
  - **Root cause — a stuck disarm, not a detection gap:** the previous
    shot's cue stopped hanging in the top-left jaws, minting the known
    phantom Scratch (checkpoint 30). Frame-verified: the hanging ball sits
    **untracked over the pocket mouth** while the whiteness vote parks on a
    still ball mid-table. The checkpoint-26 arming rule had Scratch disarm
    the newborn strike path (fetch-carry guard), and re-arm needs a
    cue-flagged ball at rest that translated since the disarm — with the
    real cue trackless in the jaws, the disarm could never clear. The jaw
    strike (newborn born at 1294 mm/s, clean/observed/unmerged — the cold
    ring replay fires it) was categorically dropped live.
  - **Fix — remove the Scratch disarm; keep the fire-path disarm:** the
    scratch disarm predates all three carried-ball confirm gates and is
    strictly redundant with them — `scratch_replace`'s real fetch carry
    fails each independently (obs ≥ 0.9 and raw-step ≤ 2 were measured on
    that very carry at checkpoint 26; merged ≤ 0.5 added in 29), while a
    jaw ball rolling out cleanly passes all three. The categorical disarm
    was suppressing exactly the discrimination the gates provide. All
    scratch/carry corpus scenarios re-verified green (`scratch_replace`,
    `scratch_shot_0801`, `ball_in_hand_pickup_0801`, `pickup_newborn_0801`,
    `ball_in_hand_place/nudge`). Accepted residual, BACKLOG'd: a bowled
    post-scratch return would now read as a strike — no observed corpus
    case, and the exposure always existed outside the post-scratch window.
  - **Corpus:** `jaw_strike_0801` added, pinned (1, 0, 1, 1) — the pin
    guards jaw-emergence detection; the disarm regression is unit-guarded
    (`test_shot_from_pocket_jaw_after_scratch_fires`, red on checkpoint-30
    code) with a carry-back control, since a cold replay never sees the
    pre-clip scratch. 36 scenarios, 42 corpus tests; 146 unit tests;
    ruff + mypy clean.

- **2026-08-01 (checkpoint 30 — young solid movers block settle; flag-2
  record corrected):** Steven reviewed checkpoint 29's read of the third
  flag clip and corrected it: "this isn't a hand roll — I shot the 4 ball."
  He's right — the frames show him down on the shot from the top-left
  corner, cue stick on the white ball, potting the 4 that hung at the
  bottom-center pocket; the cue followed and stopped hanging in the jaw.
  The `ShotTaken` was real and correctly detected; checkpoint 29's
  "hand-rolled placement" bullet is **withdrawn** (clip renamed
  `hand_roll_place_0801` → `hanger_pot_0801`, design/backlog corrected).
  The clip's real failures, both journal-visible:
  - **Premature settle, fixed:** the fast cue's terminal track churn
    (coast-overshoot death → short rebirth → fresh id at the jaw) left the
    still-rolling ball represented only by tracks too young to judge, and
    settle fired 1.3 s after the strike while the cue visibly rolled
    another 37 mm — the clock reset mid-flight (the flag). Real
    displacement needs no measurement window: a **solidly observed** track
    (≥ the flicker-filter fraction; junk measures 7–43 % observed) now
    blocks stillness from its first frames, while creep and churn judgment
    still require a full judgeable window. The clip's settle moves
    12.85 → 14.56 s, after the ball truly stops; no other pin shifted.
  - **Phantom Scratch, documented open:** the cue stopping hanging in the
    jaw is indistinguishable from a pot at track-death time (last observed
    position inside the pocket zone, table moving), so Scratch fired while
    the cue sat on the table. Journal-only in v1 → no clock damage;
    BACKLOG'd with the idea-bank levers, revisit only if scratch ever
    drives the clock.
  - **Corpus:** 35 scenarios, 41 corpus tests; 144 unit tests (new:
    track-rebirth-mid-roll settle deferral); ruff + mypy clean.

- **2026-08-01 (checkpoint 29 — stroke-tug re-open, carried-newborn gate):**
  the first post-28 drill session (12:31–12:40 UTC, restarted on the
  checkpoint-28 build) produced three flags in four minutes of
  retrieve-and-replace drill play; all three root-caused from the journal +
  ring replays, two fixed, one pinned as an honest gap.
  - **Axis-kill re-open credit** (`vision/events.py`, from `stroke_tug_strike_0801`
    = flag 1): a real shot was missed live — journal shows a settle with no
    `shot_taken` and `rack_shots` silently bumping 3→4 (the settle-counts-
    the-shot fallback absorbing the miss). Traced frame by frame: the final
    stroke tugged the addressed cue 12.04 mm (just past the 12 mm axis-seed
    floor) ~0.1 s before impact and opened a candidate with a garbage axis;
    the real impulse (raw 3293 mm/s) failed that candidate's axis check as
    designed, but the step gate's still-stamp was 0.01 s too stale for the
    documented re-open — the same shape as `missed_strike_0731`, returning
    through a bigger tug. An axis kill on a rest-anchored candidate now
    re-arms the step gate for one `strike_step_max_s` window; all other open
    gates still apply. The clip's replay goes from 0 shots to exactly 1
    (fires 0.04 s after the kill).
  - **Carried-newborn merged gate** (`detect→track→events` plumbing, from
    `pickup_newborn_0801` = flag 3): picking the resting cue up births a new
    track from the hand+ball blob — the old track's merge-hold eats the blob
    until it slides beyond the 100 mm association gate, and the remainder is
    a newborn at speed on a quiet table. This carry was smooth and fully
    observed, beating both checkpoint-26 carried-ball gates, and fired a
    phantom `ShotTaken` 0.7 s after a settle (clock then stuck in MOTION 17 s
    — no settle can follow a pickup). Measured discriminator with 4×/2×
    margins: the carry confirmed on **8/8 merged observations** vs **0 %**
    on every real strike in the corpus (worst outlier 11 %, non-newborn;
    the 7361 mm/s break also 0 %). `BallState` now carries per-frame
    merged-ness; a newborn candidate whose observed confirm rows are > 50 %
    merged is a carry. Unit-tests the no-cue newborn path for the first time
    (control + carried pair).
  - **Hand-rolled placement — honest gap, pinned** (`hand_roll_place_0801` =
    flag 2): Steven bowled the cue back to his drill spot from the top-left
    corner; the launch is kinematically a strike (11 s rest, clean impulse,
    coherent free roll, nothing merged after release) and the reach came in
    over the masked rail, so there is no approach evidence either. The roll
    died at the bottom-center pocket mouth and also minted a `Scratch`.
    Position data cannot separate a hand-bowled ball from a struck one:
    joined the sub-impulse class under the acoustic corroborator (a stick
    strike clicks, a hand roll is silent — BACKLOG, top of accuracy queue).
    Pinned known-wrong (1 shot, 1 scratch) so any change surfaces.
  - **Corpus:** `stroke_tug_strike_0801`, `hand_roll_place_0801`,
    `pickup_newborn_0801` added — 35 scenarios, 41 corpus tests; 143 unit
    tests; ruff + mypy clean. Remaining residual: planted-glove slide
    (needs the `glove_address` recording).

- **2026-08-01 (checkpoint 28 — merge flag, pickup discard, IDLE break gate):**
  the first journal-instrumented session (checkpoint 27 ran live 11:14–11:19
  UTC) produced two flags, and the journal did its job: both root causes were
  identified from `events.jsonl` + the ring before writing any code.
  - **Detector merge flag + tracker hold** (`vision/detect.py`, `track.py`,
    `pipeline.py`): the stick merging into an addressed ball's blob yanks the
    shared centroid 31–47 mm — past the 30 mm motion latch (phantom settle ≈
    phantom reset mid-address) *and* it wrecks the rest/still state, so the
    real strike moments later was **also missed live** (frame-verified on the
    second flag: phantom reset at t≈279, the real shot at t≈281 invisible to
    both live and replay — one root cause behind "clock randomly resets to 30
    and then doesn't see the shot I take a second later"). `Detection.merged`
    reports blobs too big to be one clean ball (measured: clean still balls
    ≤ 1471 px² / ≤ 47 px extent everywhere, merged ≥ 2071 px² / ≥ 102 px;
    flag at 1800 / 70); the tracker holds a **confirmed, still** track in
    place through merged observations (a still ball cannot streak — §2.3
    lesson 2 untouched for movers). Kills the phantom settle AND recovers the
    blinded strike: `address_yank_strike_0801` pins 1 shot; `merge_yank_0731`
    tightens to exactly 1 settle; `triage_0801b/c` phantoms gone.
  - **Pickup discard** (`vision/events.py`): `ball_in_hand_pickup_0801`'s
    settle closed. Measured: a picked-up ball's track dies far from any
    pocket after a 41–78 mm drag; a real shot always leaves a live mover or
    a pocket-zone death after real travel (`shot_debug` cue: 1699 mm). When
    every ball that latched the episode died off-pocket under 200 mm,
    `table_moving` unlatches with no settle. Pot deaths keep settling.
  - **IDLE break gate** (`server/hub.py`): the first flag's phantom
    `ShotTaken` fired mid-racking (11:14:49, 19 s before the real break,
    which was then swallowed in MOTION). Evidence frames predated the 12 s
    ring (press came 12.7 s late), so no vision-side guard is anchorable —
    instead `ShotTaken` now carries `ball_id`/`speed_mms`/`newborn`, and in
    IDLE the Hub ignores impulses under 3000 mm/s (measured breaks 5690–6425
    vs. every ordinary shot 892–2695). Feather break → IDLE → Restart;
    drills → Start key. Ring default 12 s → 20 s (~3.6 GB) so a late press
    keeps its evidence; flag presses are journaled (`flag_dump`).
  - **Gate ate a real break, same morning — fixed before commit:** Steven's
    12:06 UTC break was journaled `vision_shot_taken_ignored_idle speed=2072`
    while the ring shows the cue flying 5300–5500 mm/s. On a handoff strike
    the candidate advances on the dying original track's remnant, so the
    open-frame raw underreads ~2.6×. `ShotTaken.speed_mms` is now the
    fastest *observed* ball anywhere in the candidate window (the dwell gate
    guarantees the table was quiet, so fast motion in that window is the
    shot): the same break reads 7361. `break_0801` (first TV-era break clip)
    pins it, including a corpus test asserting the reported speed clears
    `IDLE_BREAK_FLOOR_MMS`. Re-anchored: breaks 5845–7361, ordinary shots
    ≤ 2868, floor stays 3000.
  - **Honest regression, accepted + documented:** `soft_touch_0731`'s old
    settle was *phantom-latched by the stick yank* (the touched ball peaks
    at 25–28 mm per 0.2 s window, under the latch; `shaft_adjust` proves no
    slower latch is honest at 59 mm/0.5 s while judgeable). Touch shots now
    produce no events; the acoustic corroborator (Part D mic) is promoted to
    the top of the accuracy queue (BACKLOG).
  - **Corpus:** `rack_place_0801`, `address_yank_strike_0801`, `break_0801`
    added, five pins tightened, plus the break-vs-gate speed test — 32
    scenarios, 38 corpus tests green; 140 unit tests; ruff + mypy clean.
    Remaining residual: planted-glove slide (needs the `glove_address`
    recording).

- **2026-08-01 (checkpoint 27 — no-cue strikes + the session journal):**
  checkpoint 26 went live mid-session and Steven's next flags (half a rack,
  two dumps) caught its remaining gap within minutes.
  - **No-cue strike path** (`vision/events.py`): both live misses shared one
    mechanism — *no ball held the cue flag at strike time*. The address
    occlusion kills the cue's track; the tracker's regrab grace (1.0 s, built
    for scratch detection) and whiteness floor keep the flag empty; a fast
    newborn's blur-corrupted, frozen color sample cannot win the vote back;
    and the dead track coasts as a *flagged phantom* ~0.15 s after impact —
    longer than the entire 0.1 s newborn window (gate-traced frame by frame
    on `scratch_shot_0801`). Fix: when no observed ball is cue-flagged, the
    newborn scan runs over all observed balls and the birth conjunction
    carries the identity; candidates then follow the track id. Founding
    tracks excluded; candidate expiry centralized. Verified: both misses now
    detect (`no_cue_strike_0801`, `scratch_shot_0801` — the latter's scratch
    was frame-verified real and stays detected), the ball-in-hand carry
    (`ball_in_hand_pickup_0801`, Steven's own ground truth) does not fire,
    `scratch_replace`'s fetch carry stays contained by the scratch disarm.
  - **Session journal** (`server/journal.py`, Hub, `cueclock run`): a flag
    dump only captured frames — replaying it shows what *current* code does,
    not what the live session did, and ground truth lived in Steven's memory
    ("i don't even remember what happened the first one"). The Hub now
    journals every operator command, vision event (with the pipeline
    timestamp), and clock state transition to `logs/session-*.jsonl`, and
    every ring dump writes the recent tail as `events.jsonl` beside the
    frames. A flagged clip now records what the engine did while the frames
    were captured.
  - **Corpus:** five more graded scenarios (`no_cue_strike_0801`,
    `scratch_shot_0801`, `ball_in_hand_pickup_0801`, `triage_0801b/c`) — 29
    scenarios, 34 corpus tests green; 135 unit tests; ruff + mypy clean.
  - Known residuals unchanged from checkpoint 26 (stick-merge yank phantom
    settles, pickup settles) — the detector merge flag remains the next
    accuracy lever (BACKLOG).

- **2026-08-01 (checkpoint 26 — accuracy: occluded-address strikes, phantom
  resets, self-healing shot windows):** the 2026-07-31 evening match-mode
  session surfaced three failure classes, diagnosed from six Flag-key dumps
  plus the OBS recording (`~/Movies/2026-07-31 23-18-28.mov`, 2:08–2:25 and
  5:28–5:47), all fixed and corpus-pinned this checkpoint.
  - **Missed strikes — occluded address** (`vision/events.py`): the address
    hides the cue ball long enough that its track dies; the whiteness vote
    parks on another bright ball; the strike then surfaces as a new track
    born at impulse speed (1572 mm/s, 964 mm from the wrong rest anchor) and
    every rest-anchored gate rightly rejects it — that session's break and
    post-push shot both died this way, and each miss also left the rack shot
    count short, so the WNT 60 s window *replayed* on the following shot
    (Steven's original report). Fix: the **newborn-cue strike path** — birth
    spot as anchor, table-quiet dwell + kiss guard at birth, carried-ball
    confirm gates (≥ 90 % observed, adjacent-frame raw ratio ≤ 2 — both
    measured against `scratch_replace`'s 2220 mm/s fetch carry), armed once
    per rest cycle (Scratch disarms; re-arm needs a cue at rest that
    *translated since the disarm*, or the post-scratch vote fallback would
    re-arm it). Details + measurements: `design.md` §4.2.
  - **Phantom mid-address clock resets** (`vision/events.py`): bridge-glove /
    watch tracks reach 79–94 % observed — past the checkpoint-23 flicker
    filter — and a 30 mm gear drift latched `table_moving`, whose settle
    reset a RUNNING clock (three video-verified resets in one session). Fix:
    the **ball roster** — only tracks alive through a quiet dwell may latch;
    gear born mid-play moving cannot join; union during quiet (occlusion must
    not evict), wholesale prune at settle; carom stamps inherit the filter.
    Residuals + provokers recorded in `design.md` §4.2 and BACKLOG (stick-
    merge centroid yank `merge_yank_0731`, planted-glove slide, hand pickup
    `triage_0801c`).
  - **Clock semantics** (`clock/state.py`, `design.md` §2.1): settle in IDLE
    is now a no-op (New Rack → racking settles must not start the countdown
    — the first live complaint of the evening), and a settle with no
    ShotTaken since the last settle **counts the shot** — missed strikes and
    sub-impulse touch shots (`soft_touch_0731`: real shot, cue peaks at
    225 mm/s raw, optically indistinguishable from a nudge) no longer replay
    the 60 s window. Cue-rest slide guard: a whiteness-vote jump > handoff
    radius restarts rest dwell instead of teleporting it with ancient credit.
  - **Corpus:** six live captures promoted as graded scenarios —
    `occluded_address_0731` (1 shot exactly, was 0), `rack_gather_0731`,
    `soft_touch_0731` (shots unasserted by design), `soft_shot_0731`,
    `glove_address_0731` (max_settles 0 — the phantom pin), `merge_yank_0731`
    (max 2 tolerates the known yank settle); EXPECTED gains an optional
    max_settles field. `triage_0801a/b/c` staged for the next iteration.
    Suites: 135 unit + 29 corpus green; ruff + mypy --strict clean.
  voice "extension"):** built for the first real match (2026-08-01, WNT
  9-ball), in four parallel worktree agents + integration. All rulebook
  numbers verified against the official WNT Event Rules PDF §5 this session.
  - **Rack context in the clock** (`clock/state.py`): `first_shot_seconds`
    per ruleset (WNT 60 — "the first shot after the Break-Off or after a Push
    Out"; both 8-ball rulesets keep 30), a per-rack shot counter (the break
    counts from IDLE), and per-**rack** extensions — settles no longer refill
    the pool; `new_rack()` does. `wnt_9ball` pool is 2 (one per player per
    rack, self-policed — no turn tracking; backlog). `reset()` redefined as
    timer-normalize only (a foul must not refill extensions); `restart()` =
    fresh rack-aware countdown, RUNNING immediately; `push()` re-arms the
    60 s window and handles pass-backs (press again). New API:
    `POST /api/{rack,restart,push}` — design.md §2.1 updated.
  - **Score + vision kill switch on the Hub**: `MatchScore`
    (`server/score.py`, persisted to gitignored `config/score.json` so a
    crash can't lose the match; in-memory under tests), `/api/score/*`
    endpoints, `/api/vision/{on,off}` (when off, vision events can't touch
    the clock — the match-day fallback is deck/voice/manual). WS + `/api/state`
    now share one composed payload with `vision` and `score` keys.
  - **Scoreboard overlay** (`/score`): the v1 prototype's broadcast
    lower-third ported (dark-blue gradient bar, silver border), live over WS,
    RACE TO n center segment, hidden until data. OBS recipe: hardware.md §B3.
    `control.html` gains New Rack / Push / Force 30 / vision toggle / score
    panel. `shotclock.html`: WNT-style beeps every second from 5 down
    (replaces the single 5 s chime).
  - **Stream Deck row filled out** (`deck/layout.py` + hardware.md §C2):
    Push (1,4), New Rack (2,3), Force 30 (2,4), score keys (3,1/3,2 P1,
    3,4/3,5 P2). Extension face/flash now ruleset-aware (+15/+30, derived
    from `RULESETS` so it can't drift). Music keys displaced to BACKLOG.
  - **Voice "extension" built** (`src/cueclock/audio/`, hardware.md Part D):
    the v1 Vosk spotter ported — grammar-constrained, finals only, conf
    ≥ 0.9, 2 s cooldown; pure core unit-tested with a fake recognizer;
    `cueclock run` wires it to `hub.extension()`; `cueclock listen` is the
    bring-up/soak tool. vosk pinned 0.3.44 (newest macOS wheel). **Found in
    bring-up: the Mac mini has no built-in microphone** — a wired USB mic is
    required hardware; `--mic-device` has no default by design.
  - Smoke-tested end to end over HTTP (WNT switch → 60/pool-2, push/restart/
    rack, score CRUD + persistence round-trip, vision toggle, all pages).
    132 unit tests (98 → 132), ruff + mypy --strict clean.

- **2026-07-31 (checkpoint 24 — full-screen clock view + docs consolidation):**
  - **Broadcast-style full-screen shot clock** (`shotclock.html?view=full`):
    bare viewport-sized number, no box/icon, transparent background — for a
    dedicated OBS scene readable from anywhere in the room (Ultimate Pool TV
    look). The number is browser-rendered text, so a 1920×1080 browser source
    is pixel-crisp with no raster scaling. `?audio=off` mutes a duplicate
    source's cues (OBS keeps non-visible browser sources running — two
    sounding scenes would chime twice). OBS scene recipe: `hardware.md` §B3.
  - **Docs consolidated to a GitHub-standard layout** (10 markdown docs +
    2 PDFs → 5 files): root `README.md` (rewritten — rig-live status, launch
    commands, OBS sources), `CHANGELOG.md` (this file, from `progress.md`),
    `BACKLOG.md` (from `todo.md` + `accuracy-ideas.md` + scattered backlog
    sections); `docs/design.md` (from `cv-app-rewrite.md`, § numbers
    preserved verbatim because code cites them, + the corpus recording
    checklist as new §9) and `docs/hardware.md` (camera + video path +
    Stream Deck living doc + mic plan + TV/recalibration, merged from five
    docs). Every code/docstring reference updated; originals archived
    untouched in `~/Documents/cueclock-docs-archive/`; the PDFs left the
    repo (archive only). CLAUDE.md rewritten to match (its "camera in
    transit" status was a week stale).

- **2026-07-31 (checkpoint 23 — carom guard v2 + flicker filter + axis-seed
  floor: the accuracy blocker falls):** the guard-redesign session checkpoint 22
  called for. Three stacked defects fixed in `vision/events.py`, each constant
  anchored to a measurement from the Flag-key captures (no blind tuning):
  - **Carom guard v2**: rejection now requires another ball *genuinely moving*
    (`moving_enter_mm` over `moving_window_s`, observed positions, judgeable
    tracks) within 0.4 s of the impulse AND within 200 mm kiss range of the
    cue's rest spot — replacing the v1 "table settled ≤ 0.4 s ago" test that
    player presence starved. Physics anchors: a bringer fast enough to impart
    the 250 mm/s impulse floor covers ~100 mm inside the window and its last
    observed report lands ≤ ~120 mm from the rest spot; measured stroke-gear
    movers (grip hand / stick fragments, 72–100% observed so no flicker filter
    catches them) sit at 271 mm (medium_shot) and 340 mm (power_shot). Both
    conditions proved necessary — the time-only variant re-broke those two
    scenarios on the first re-grade.
  - **Flicker filter**: `_judgeable` now also requires ≥ 70% observed fraction
    over the creep window. Junk tracks measured 7–43% observed vs 90–100% for
    every real ball (per-track autopsy of missed_strike_0731 +
    live_divergence_0731a). Junk can no longer latch `table_moving` (the
    id-10 track that flipped it at 1.31 s and died the same instant) nor
    block `_is_still` (the 11.8 s live settle stall; settle on missed_strike
    now fires at 7.71 s vs 8.24 s). A hand wave over a settled table can no
    longer mint a BallsSettled — which `clock/state.py` turns into a free
    30 s reset.
  - **Axis-seed floor** (`strike_dir_seed_min_mm = 12`): only visible once the
    guard passed — the candidate opened on the impulse's first frame with a
    2 mm noise/merge-yank direction vector pointing *backwards*, the axis
    check then killed the true strike as incoherent, and the step gate
    expired 8 ms before a re-open could land. Candidates now wait for an
    honest 12 mm displacement off the rest spot (above the 8.5 mm max
    measured single-window yank excursion, ~2 impulse-floor steps).
  - **Corpus**: the three Flag captures promoted to graded scenarios (1 shot,
    0 scratch, ≥ 1 settle each — table ground truth), and the harness now
    derives replay stride from each clip's stored fps: ring dumps hold the
    78 fps detect-stride stream, so the fixed stride-2 had been replaying
    them at *half* the live cadence. 22/22 scenarios green; unit suite grew
    to 21 event-engine tests (carom kiss, stroke-gear reach, flicker
    starvation, hand-sweep settle-mint pins).
  - For the record: checkpoint-20's "rejected solely by the guard" was true
    but incomplete — the old gate probe never printed the dwell/norm/confirm
    internals, so the axis-seed defect underneath surfaced only after the
    guard verdict flipped. The fuller probe (probe_gates2, session
    scratchpad) prints every sub-condition of the conjunction.
  - Awaiting live validation: play normally, Flag anything wrong.

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
