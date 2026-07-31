# To-do — punch list (updated 2026-07-30)

> **Status 2026-07-30:** rig is final — TV hung, camera USB cable run, calibration
> re-done after the cable (2026-07-29 23:05 refits from `empty_table_0729`, 0.76 px,
> corpus 18/18), OBS feed + TV setup done. The overnight "never discover it at the
> table again" layer (startup reference drift check, 12 s ring buffer +
> `POST /api/dump`, TV-era corpus scenarios) is committed as checkpoint 18.
>
> **Launch command** (fresh reference; LAN bind so the phone control page works):
> `uv run python -m cueclock.cli run --rectify --reference tests/clips/empty_table_0729 --host 0.0.0.0`
> After any future camera bump: fresh empty-table clip → `cueclock calibrate` →
> `cueclock fit-undistort` → point `--reference` at the new clip. The startup
> drift check warns loudly if the reference has gone stale.

**Open bug — needs your capture (the "clock didn't stop on my shot" one):** it's a
real bug, and the reset-on-settle that follows is the designed safety net doing its
job. Two shots in the 07-29 log (t≈42 s and t≈81 s) produced no ShotTaken: in both,
the cue's track was lost at impact for >1 s (`no cue` mid-flight) and reappeared far
away, so the strike detector never saw the launch impulse on a cue-flagged track.
Strike logic stays untouched until there's evidence — that's how the old app
plateaued. Instead: when a shot fails to freeze the clock, press the dump key (or
`curl -X POST 'http://localhost:8000/api/dump?label=missed_shot'`) within ~12 s.
One captured clip and it can be traced + fixed the same way as the settle bug.
(Also pinned: detect cadence matters — at stride 4+ the corpus behavior shifts, so
keep `run` at the default stride 2.)

Working list. Long-term status still lives in `progress.md`; this file is the
short-term punch list — delete items as they land.

## 1. Stream Deck build (deck arrived + plugged in; plan of record: `stream-deck.md`)

Approach decided 2026-07-31: **no Elgato app** — an in-repo `cueclock deck` daemon
drives the MK.2 over USB (buttons are versioned code; never install the Elgato app,
it would claim the device). Coding sessions, in order:

- [x] **Phase 1 — daemon + API keys** (Reset, Extension +15 s, Ruleset cycle,
      Flag/dump) — **done 2026-07-31, checkpoint 19**. Agent `com.cueclock.deck`
      installed + running; no TCC grant was needed. Keys light up (undimmed)
      whenever `run` is up.
- [ ] **Phase 2 — session toggle** (the headless on/off switch): daemon owns
      `run` + OBS lifecycles; one-time OBS settings (websocket server, replay
      buffer, "Save projectors on exit", drop OBS from Login Items).
- [ ] **Phase 3 — Instant replay + music keys** (obs-websocket + osascript).
- [ ] **Phase 4 — `/triage-clips` skill** (replay flagged dumps through the
      vision core; the off-table half of the Flag key).
- [ ] Bring-up checks (at the table, listed in `stream-deck.md`): deck-press
      wakes sleeping mini? · `pmset -g assertions` clean after session stop ·
      OBS projector/replay-buffer restore · dump-label collisions + no-T7 face.
- [ ] Placement: desk stand first; break-end wall mount later if it earns it.

## 2. Detect-loop load test (add telemetry, then one instrumented session)

Goal: settle whether live effective detect cadence ever drops below the 78 Hz
the corpus certifies. Offline grading is a metronome; live is "every 2nd frame
*we got to*" — `GrabStrategy_LatestImageOnly` makes falling behind invisible
(frames are silently skipped, no error). Checkpoint 14 measured 1.38× realtime
*average*; spikes during shots were never measured. This test also feeds the
missed-ShotTaken investigation (open bug above): if cadence dips exactly during
shots, it goes back on the suspect list.

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
   side-angle), TV on, default flags: the launch command above plus `--verbose`.
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

## 3. ChArUco intrinsics calibration (scheduled when wanted — not urgent)

ELI5: the wide-angle lens bends the image like a peephole (straight rails bow
outward, worst at the edges). Showing the camera a printed checkerboard-with-markers
board (ChArUco) at 20–30 poses lets software solve exactly how the lens bends light,
so pixels convert to true table-millimeters everywhere.

**Not strictly necessary**: the rails-fit shortcut already gives 0.76 px residual and
a green corpus. This is the gold-standard upgrade for mm accuracy out at the pockets —
insurance and polish, not a blocker.

The old blocker (rig not final) is cleared as of 2026-07-30 — camera is in final
position with focus locked. Remaining blocker: a Claude session to generate the
board PDF and build the intrinsics-fit tooling (doesn't exist yet). Steven's only
action now: none.
