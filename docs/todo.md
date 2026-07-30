# To-do — post-TV-mount (written 2026-07-29, for 2026-07-30)

> **Morning status (overnight 07-29→30, second pass):** working clock confirmed;
> overnight I built the "never discover it at the table again" layer. All tests
> green (77 unit, 20 corpus, mypy, ruff). **Uncommitted** — review + commit, then
> launch as before (the run command below §3). What landed overnight:
>
> 1. **Startup reference check** — `run` now measures boundary drift between the
>    live scene and the reference clip on the first frame and prints either
>    `reference check ok` or a loud WARNING with the fix recipe. Catches the
>    camera-moved / stale-reference class (measured: noise ~1.9, the TV-install
>    shift ~35, warn at >8).
> 2. **Ring buffer + `POST /api/dump?label=x`** — `run` keeps the last ~12 s of
>    raw frames in RAM; the endpoint saves them as a labeled corpus clip on the
>    T7. This is Stream Deck build item 1 — wire a key to
>    `curl -X POST 'http://localhost:8000/api/dump?label=flagged'` and every miss
>    becomes replayable evidence without stopping play.
> 3. **`shot_debug` + `empty_table_0729` are corpus scenarios now** (scoreboard
>    supports per-scenario references) — the TV-era conditions are permanently
>    graded. Scratch verified real (measle cue ball visibly sinks at 8.5 s).
>
> **Confirmed bug, needs your capture (the "clock didn't stop on my shot" one):**
> your read was right — it's a bug, and the reset-on-settle that follows is the
> designed safety net doing its job. Two shots in last night's log (t≈42 s and
> t≈81 s) produced no ShotTaken: in both, the cue's track was lost at impact for
> >1 s (`no cue` mid-flight) and reappeared far away, so the strike detector never
> saw the launch impulse on a cue-flagged track. I did NOT change strike logic
> blind — that's how the old app plateaued. Instead: next session, when a shot
> fails to freeze the clock, press the dump key (or curl above) within ~12 s.
> One captured clip and I can trace + fix it the same way as the settle bug.
> (Also pinned: detect cadence matters — at stride 4+ the corpus behavior shifts,
> so keep `run` at the default stride 2.)

Working list for the days after the TV went up. Dependency-ordered: rig-disturbing
work first, then camera, then everything downstream. Long-term status still lives in
`progress.md`; this file is the short-term punch list — delete items as they land.

## 0. Gate question: is the cable track done?

`progress.md` flags two rig-disturbing installs: wall TV (done 2026-07-29) and the
cable track. If the track is still coming, do it **before** any camera work below —
every calibration is void the moment the rig gets bumped. (Taking the TV down to
re-level it does *not* void anything; the camera hangs over the table, not on that
wall.)

## 1. Level the overhead camera (~20 min, Claude walks through it)

- [ ] Coarse: loosen mount, bubble level on the flat back of the camera body, level
      both axes, snug screws.
- [ ] Fine: record a 2–3 s empty-table clip → Claude fits the cloth boundary lines
      and computes keystone. Iterate adjust → record → measure until flat.
      Only tilt is worth fixing physically; roll and off-center are handled in
      software. **Baseline re-measured 2026-07-29 after the TV install** (camera
      had shifted ~20 mm; tilt improved on its own): keystone right/left +2.20%,
      top/bottom +0.44% (was +2.88% / +1.25%), roll −0.33°. Remaining tilt is
      about ¾°— worth one bubble-level pass, but not urgent.
- [ ] Then immediately: re-peak focus (live-view SHARP metric), lock lens screws,
      re-verify WB against white paper, re-save UserSet1. Focus must be locked
      before anything downstream.

## 2. Re-run the two quick recalibrations (~10 min, existing tooling)

Moving the camera stales two gitignored config files. Fresh empty-table clip, then:

- [x] `cueclock calibrate` — refit rails homography (`config/table_calibration.json`)
      — **done 2026-07-29** from `tests/clips/empty_table_0729`, 0.76 px residual
      (better than July 27's 0.89); corpus re-graded 18/18 green afterward
- [x] `cueclock fit-undistort` — refit display undistort — **done 2026-07-29**,
      0.76 px, preview verified (rails straight)
- [ ] Re-set the OBS crop on the rectified feed
- [ ] **Relaunch `run` with the fresh reference** (the old default reference is the
      July-27 clip — stale references silently kill shot detection):
      `uv run python -m cueclock.cli run --rectify --reference tests/clips/empty_table_0729 --host 0.0.0.0`
      Redo the clip + both refits after any future camera bump (this is what "no
      shots registering" looks like).

## 3. OBS + TV eyes-on setup (unblocked by the mount)

- [ ] Browser source → `http://localhost:8000/shotclock` (200×200, "Control audio
      via OBS") — **start `cueclock run` before OBS**, or right-click the source →
      "Refresh cache of current page" after the server is up; a browser source that
      loaded while the server was down shows blank until manually refreshed.
- [ ] Apply LUT (`assets/obs/cueclock-tone.cube`) + crop, judged on the actual TV
      (current grade was tuned on the Mac monitor — provisional).
- [ ] Rough in scenes: table feed · replay · iPhone side-angle.
- [ ] Control page from the phone needs a LAN bind — the default is loopback-only
      (phone gets connection refused): run
      `uv run python -m cueclock.cli run --rectify --host 0.0.0.0`, then open
      `http://mac-mini.local:8000/control`. Bake `--host 0.0.0.0` into the
      Stream Deck session-start macro when it lands. OBS overlay on the mini
      keeps using `localhost` either way.

## 4. Stream Deck (arrives 2026-07-30) — day-one setup, zero new code

Full plan: `stream-deck-plan.md`.

- [ ] Install Stream Deck app + official Elgato OBS plugin.
- [ ] Page 1 buttons → live HTTP API (`http://localhost:8000/api/*` via
      HTTP-request plugin or curl shell actions): clock start/pause, reset,
      extension (+15 s).
- [ ] OBS scene keys + instant replay (replay buffer save-and-switch multi-action).
- [ ] Music keys (native media actions + `osascript` volume presets).
- [ ] Decide placement (plan: break-end wall, hip height; USB 2.0 extends to
      ~16 ft). Mount hardware can come later.
- [ ] NOT yet: ground-truth / "event wrong" keys — they need the ring-buffer build
      (`stream-deck-plan.md` build item 1), a coding session.

## 5. TV re-level (whenever — no dependencies)

- [ ] Take it down, level it, re-hang. Purely cosmetic; nothing in the system cares.

## 6a. Detect-loop load test (add telemetry, then one instrumented session)

Goal: settle whether live effective detect cadence ever drops below the 78 Hz
the corpus certifies. Offline grading is a metronome; live is "every 2nd frame
*we got to*" — `GrabStrategy_LatestImageOnly` makes falling behind invisible
(frames are silently skipped, no error). Checkpoint 14 measured 1.38× realtime
*average*; spikes during shots were never measured. This test also feeds the
missed-ShotTaken investigation (morning-status note above): if cadence dips
exactly during shots, it goes back on the suspect list.

**Part 1 — implement the telemetry (~15 min, camera-free):**

- File: `src/cueclock/cli.py`, function `_cmd_run`, inside the grab loop
  (find the `--verbose` status block — search `next_status`).
- Count per status interval: (a) frames retrieved (`grabs`), (b) detect cycles
  run, (c) `vision.process` wall time via `time.perf_counter()` — keep sum and
  max. Reset counters every interval.
- Extend the verbose line with e.g.
  `| 156 grabs/s | detect 8.9ms avg 12.1ms max` (grabs per second, process
  time avg/max over the interval).
- Key semantics: with LatestImageOnly, retrieve rate == processing rate, so
  `grabs/s` *below* the camera's 156.25 means the loop is skipping frames —
  the deficit is the skip count. There is no other way to see it.
- Verify camera-free: `uv run pytest tests/unit -q && uv run mypy src` (the
  loop itself needs the camera; the change is print-only, keep it that way).

**Part 2 — the at-table session (~10 min):**

1. Normal conditions: OBS running with the usual scene (Syphon + iPhone
   side-angle), TV on, default flags:
   `uv run python -m cueclock.cli run --rectify --reference tests/clips/empty_table_0729 --host 0.0.0.0 --verbose`
2. Read the status line in three phases: (a) idle table ~1 min, (b) 8–10 real
   shots incl. one break, (c) while walking around / waving over the table
   (worst-case blob load).
3. If any shot fails to freeze the clock during this session, immediately
   `curl -X POST 'http://localhost:8000/api/dump?label=missed_shot'` — that
   clip is gold for the strike bug regardless of what telemetry says.

**Part 3 — evaluating the numbers:**

- **Healthy:** grabs/s ≈ 155–157 in all three phases; detect avg ≲ 10 ms;
  occasional max spikes near 12.8 ms are fine *if grabs/s holds*. Verdict:
  cadence exonerated for the missed-strike bug; live matches what the corpus
  certifies; close this item.
- **Concerning:** grabs/s sagging (≲ 150) — especially if it correlates with
  shots/motion phases. Every 2 missing grabs/s ≈ 1 detect/s skipped; sustained
  sag means live runs at an effective stride the corpus never graded (stride 4
  is *known* to change event behavior — spurious settle on `shot_debug`).
- **Budget math for context:** stride-2 detect budget is 12.8 ms/cycle
  (2 frames at 156.25 fps). Detect avg ≥ ~12 ms ⇒ chronically at the edge.
- **If concerning, remedies in order (do NOT start with stride):** 1) drop
  display publish rate while clock state is MOTION (display shares the detect
  thread); 2) move display/Syphon to its own thread; 3) cheapen detection
  (ROI-limited segmentation around known tracks); 4) only as a last resort
  raise stride — that requires re-deriving the churn thresholds
  (`spin_churn_max` is calibrated at 78 Hz, see `EventConfig`) and re-grading
  the whole corpus at the new stride.
- Paste the phase-labeled status lines back to Claude either way — idle vs
  shots vs waving is the signal, single numbers out of context are not.

## 6b. ChArUco intrinsics calibration (NOT tomorrow — scheduled when rig is final)

ELI5: the wide-angle lens bends the image like a peephole (straight rails bow
outward, worst at the edges). Showing the camera a printed checkerboard-with-markers
board (ChArUco) at 20–30 poses lets software solve exactly how the lens bends light,
so pixels convert to true table-millimeters everywhere.

**Not strictly necessary**: the rails-fit shortcut already gives 0.89 px residual and
17/17 corpus. This is the gold-standard upgrade for mm accuracy out at the pockets —
insurance and polish, not a blocker.

Blocked until: (a) camera in final position + focus locked (void the instant either
changes — must follow §1 and any cable-track work), and (b) a Claude session to
generate the board PDF and build the intrinsics-fit tooling (doesn't exist yet).
Steven's only action now: none.
