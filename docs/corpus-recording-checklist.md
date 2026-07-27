# Test-corpus recording checklist

One session at the table (~30–40 min). These clips become the ground truth every
detection algorithm is graded against (`cv-app-rewrite.md` §2.3) — the false-strike
provokers matter as much as the real shots.

## Pre-flight

- [ ] Table light on (it's on/off only — on IS the canonical lighting state)
- [ ] **Live view closed** — only one program can hold the camera
- [ ] Full ball set, cue, chalk at the table
- [ ] ChArUco board printed, if you have it (used only by the last clip — skip
      that clip otherwise)
- [ ] ~55 GB free on disk (each 8–10 s clip is ~3–3.6 GB; the CLI refuses to
      start if space is short — if it complains, delete a less important clip
      or re-record shorter)

## Per-clip flow (same every time)

1. Set the table up for the scenario.
2. Run (swap label + seconds from the table below):

   ```
   PYTHONPATH=src uv run python -m cueclock.cli record --label soft_shot --seconds 8
   ```

3. Start the action **~2 s after hitting Enter**; let the tail of the clip
   capture the balls settling.
4. When it prints `done:`, check **~156 fps** and **no WARNING** on that line.
5. Re-runs with the same label auto-suffix (`soft_shot_2`) — delete rejects
   from `tests/clips/` whenever.

Ctrl-C stops a recording early and the clip stays valid.

## Scenarios (record in this order)

References first:

- [x] `empty_table` · 4 s — **no balls at all**, nobody near the table
      (the segmentation reference frame — do not skip)
- [x] `static_spread` · 6 s — balls spread mid-table, nobody near
      (detector noise floor)

Real shots:

- [x] `soft_shot` · 8 s — gentle lag-speed shot, cue ball rolls a short distance
- [x] `medium_shot` · 8 s — ordinary pot attempt
- [x] `power_shot` · 10 s — hard shot, multiple cushions
- [x] `break_shot` · 12 s — full rack, real break

False-strike provokers (the old app's failure modes — act them out honestly):

- [x] `ball_in_hand_place` · 8 s — pick the cue ball up, place it by hand, step away
- [x] `ball_in_hand_nudge` · 8 s — the full ball-in-hand sequence: pick the
      cue ball up, place it by hand, then fine-tune it a few inches **with the
      shaft** (the legal way — tip contact is a foul), cue away, step back.
      Dangerous for the detector because placement satisfies the stationary
      dwell and the shaft push then adds an impulse + short coherent
      trajectory — only the impulse's low speed separates it from a shot
- [x] `practice_strokes` · 8 s — address the ball, several practice strokes over
      it, never touching
- [x] `shaft_adjust` · 8 s — accidentally graze the cue ball with the shaft
      while settling into aim, so it rolls slightly. Yes, this is a foul in
      play — the clip exists because it happens by accident, and the clock
      must not count the graze as the shot (foul-calling stays human; v1
      detects shots, not fouls). Distinct from the deliberate multi-inch
      reposition above.
- [x] `tip_bump` · 8 s — the likelier accident: bump the cue ball with the
      **tip** while feathering/aiming, so it rolls an inch or two. Also a foul,
      recorded for the same reason — but this one is **the hardest case in the
      whole corpus**: dwell + small impulse along the cue line is nearly the
      signature of a real soft shot (`cv-app-rewrite.md` §4.2 calls slow tip
      nudge vs. soft shot the one genuinely ambiguous pair; the backlog audio
      click corroborator exists for exactly this). If vision alone can't split
      it from `soft_shot`, that's expected — this clip is what proves whether
      the mic feature is needed.
- [x] `chalk_near` · 8 s — chalk the cue directly above the table surface
- [x] `rack_balls` · 12 s — gather balls from the ball return and rack them
      (hands + many balls over the table at once; this table's honest stand-in
      for the drop-pocket "reach in and retrieve" provoker)

Scratch + settle behaviors:

- [x] `scratch_replace` · 12 s — scratch, fetch the cue ball from the ball
      return, carry it back over the table, place it ready to play (the full
      real sequence on this table)
- [x] `walk_around_settle` · 10 s — shoot, then walk around the table leaning
      over it while balls are still settling
- [x] `slow_creep` · 10 s — a shot that ends with a ball barely creeping to a
      stop (bonus: dying right at a pocket edge)
- [x] `spin_in_place` · 8 s — leave the cue ball stationary but still visibly
      spinning: hit a stun shot (cue ball stops dead on contact) with heavy
      side spin, so it sits there spinning like a top as the spin dies.
      Ground truth per the rules: the shot is NOT over until the spin stops —
      WPA def. 8.2, "a shot ends when all balls in play have stopped moving
      and spinning" — so settle must not fire while the ball visibly spins,
      even though its position is static (see §4.2 rules caveat).
      (Bonus second take: a hard draw shot — cue below center, backspin — so
      the cue ball hangs spinning for a beat at contact, then snaps backward;
      tests that the brief pause doesn't fire settle early.)

Calibration insurance (skip if the ChArUco board isn't printed — then intrinsics
get calibrated at final install and apply to the corpus as long as focus is
unchanged; homography comes from rail corners either way, `cv-app-rewrite.md` §4.3):

- [ ] `charuco_sweep` · 10 s — hold the board over the table and sweep it
      slowly through the center and all four edges/corners, tilting it up to
      ~45° as you go (edge coverage matters most — that's where the lens
      distortion is worst). Banks lens-distortion data at today's focus so the
      corpus stays gradable in table mm even if the final cable-track install
      shifts focus.

## After

- [ ] Spot-check one clip plays back: ask Claude, or
      `PYTHONPATH=src uv run python -c "from pathlib import Path; from cueclock.capture.clip import ClipReader; r = ClipReader(Path('tests/clips/break_shot')); print(len(r), 'frames')"`
- [ ] Bonus (2 min, no camera needed): see a clip in OBS — add a **Syphon Client**
      source, then `PYTHONPATH=src uv run python -m cueclock.cli replay --clip
      tests/clips/break_shot` and pick server "cueclock" in the source's properties
- [ ] Tell Claude the corpus is in — next build steps (calibration tooling,
      detection core) run against these clips and are camera-free
