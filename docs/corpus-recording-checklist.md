# Test-corpus recording checklist

One session at the table (~30–40 min). These clips become the ground truth every
detection algorithm is graded against (`cv-app-rewrite.md` §2.3) — the false-strike
provokers matter as much as the real shots.

## Pre-flight

- [ ] Table light on at normal match brightness (the canonical lighting state)
- [ ] **Live view closed** — only one program can hold the camera
- [ ] Full ball set, cue, chalk at the table
- [ ] ~50 GB free on disk (each 8–10 s clip is ~3–3.6 GB; the CLI refuses to
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

- [ ] `empty_table` · 4 s — **no balls at all**, nobody near the table
      (the segmentation reference frame — do not skip)
- [ ] `static_spread` · 6 s — balls spread mid-table, nobody near
      (detector noise floor)

Real shots:

- [ ] `soft_shot` · 8 s — gentle lag-speed shot, cue ball rolls a short distance
- [ ] `medium_shot` · 8 s — ordinary pot attempt
- [ ] `power_shot` · 10 s — hard shot, multiple cushions
- [ ] `break_shot` · 12 s — full rack, real break

False-strike provokers (the old app's failure modes — act them out honestly):

- [ ] `ball_in_hand_place` · 8 s — pick the cue ball up, place it by hand, step away
- [ ] `ball_in_hand_nudge` · 8 s — adjust the placed cue ball **with the cue tip**
- [ ] `practice_strokes` · 8 s — address the ball, several practice strokes over
      it, never touching
- [ ] `shaft_adjust` · 8 s — roll/touch the cue ball slightly with the shaft
      while settling into aim
- [ ] `chalk_near` · 8 s — chalk the cue directly above the table surface

Scratch + settle behaviors:

- [ ] `scratch_retrieve` · 10 s — pot the cue ball, reach in and retrieve it by
      hand over the table
- [ ] `scratch_replace` · 10 s — scratch, then place the ball back ready to play
- [ ] `walk_around_settle` · 10 s — shoot, then walk around the table leaning
      over it while balls are still settling
- [ ] `slow_creep` · 10 s — a shot that ends with a ball barely creeping to a
      stop (bonus: dying right at a pocket edge)
- [ ] `draw_spin` · 8 s — draw shot that leaves the cue ball spinning in place

## After

- [ ] Spot-check one clip plays back: ask Claude, or
      `PYTHONPATH=src uv run python -c "from pathlib import Path; from cueclock.capture.clip import ClipReader; r = ClipReader(Path('tests/clips/break_shot')); print(len(r), 'frames')"`
- [ ] Tell Claude the corpus is in — next build steps (calibration tooling,
      detection core) run against these clips and are camera-free
