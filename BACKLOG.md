# Backlog

Everything not done, in one place: the short-term punch list up top, queued
features in the middle, and the exploratory accuracy idea bank at the bottom.
Delete items as they land (history lives in `CHANGELOG.md`).

## Punch list (short-term, in rough order)

- [ ] **Live-validate the carom-guard redesign** (checkpoint 23): play normal
      sessions, Flag anything wrong. The three failure modes it fixed (missed
      strikes with hands in frame, settle stalls, phantom shots) are corpus-
      pinned; live play is the remaining judge.
- [ ] **Match-day bring-up (before the 2026-08-01 match)**: add the OBS
      scoreboard source (`hardware.md` §B3); dry-run one rack of WNT 9-ball
      end to end (New Rack → break → 60 → 30 → score keys → Push).
      Extensions are deck-key only until a mic exists.
- [ ] **Mic bring-up (parked — no mic yet)**: buy a wired USB mic (the Mac
      mini has **no built-in mic**, `hardware.md` Part D), then TCC grant,
      `listen --devices`, 10× "extension" at playing distance, and a
      false-positive soak with balls clacking and music. Also the
      prerequisite for the acoustic click corroborator (top of the
      accuracy queue).
- [ ] **Live-validate match mode** during the first real match: rack flow,
      Push, Force 30, per-rack extensions (deck keys; voice waits on the
      mic) — Flag anything wrong.
- [ ] **Live-validate checkpoints 26–32** (occluded-address strikes, roster
      latch rule, settle-counts-the-shot, merge flag, pickup discard, IDLE
      break gate, axis-kill re-open credit, carried-newborn merged gate,
      young-solid-mover settle block, scratch-disarm removal, own-rest
      strike path): every 2026-07-31/08-01 live failure is corpus-pinned or
      journal-pinned; live play judges the fixes. Every flag dump now
      carries `events.jsonl` with per-shot ball/speed/path detail. The
      14:00 UTC session validated 26–30 across two racks (11 + 11 shots,
      one flag); the 16:12 match-day flag became checkpoint 32
      (`parked_vote_strike_0801`). Unexplained residual from that dump:
      the live settle after the first shot came 9 s late (cold replay
      settles in 3.1 s) — state divergence the ring can't reproduce; watch
      for late settles live.
- [ ] **Sub-impulse touch shots are now honestly invisible** (checkpoint 28
      finding): `soft_touch_0731`'s settle used to ride a stick-yank phantom
      latch; with the merge flag it produces no events — a touch shot leaves
      the clock counting (Reset recovers). No displacement latch can fix it
      (`shaft_adjust` measures 59 mm/0.5 s while judgeable, worse than the
      touch's 43): this is the acoustic click corroborator's job (idea bank
      §5, `hardware.md` Part D mic), promoted from "if live play leaks
      nudges" to the top of the accuracy queue. (Checkpoint 29 briefly
      claimed hand-rolled placements as a second acoustic case — withdrawn
      in checkpoint 30: that clip was a real pot, misread.)
- [ ] **Phantom Scratch when the cue stops hanging in a pocket jaw**
      (`hanger_pot_0801`, checkpoint 30): a follow-to-the-jaw stop is
      indistinguishable from a pot at track-death time, so Scratch fires;
      journal-only in v1 — fix only if scratch ever drives the clock (idea
      bank §4 has the candidate levers). It found one indirect way to drive
      it — disarming the newborn strike path and eating the next jaw shot
      (`jaw_strike_0801`) — closed in checkpoint 31 by removing the scratch
      disarm; the Scratch event itself remains phantom and journal-only.
- [ ] **Post-scratch bowled return would read as a strike** (checkpoint 31
      residual, accepted): with the scratch disarm gone, a cue ball bowled
      back onto the cloth after a real scratch is kinematically a strike
      (clean, fast, unmerged) and would fire. No observed case in the
      corpus (checkpoint 29's "hand-rolled placement" was a misread,
      withdrawn in 30), and the same exposure always existed outside the
      post-scratch window. Flag it if it ever happens live; the acoustic
      corroborator (click vs silence) is the structural fix.
- [ ] **Silent hand motion still fires the birth paths** (checkpoint 33
      residuals, both frame-verified 2026-08-01 evening, both acoustic-
      corroborator cases — a stick strike clicks, a hand is silent):
      (1) the **bare-hand snatch** — `pickup_snatch_0801`, pinned
      known-wrong: a snatch-and-carry beats all three carried-ball gates
      (observed, smooth, unmerged — bare fingers from above never oversize
      the blob) and fires a phantom newborn shot; an idea-bank lever short
      of the mic is **apparent ball size as a height cue** (a carried ball
      nears the overhead camera: chest height ≈ 1.9× area — would also
      merge-flag it); (2) the **white-shaft sweep** — the cue's light
      shaft laid over the cloth during a walk-around births a clean,
      coherent, whiteness-eligible track at ~1 m/s and fired twice live
      (lone_roller's t=4064.9 fire; drill_shot's t=302.4 fire, which
      counted a rack shot). Cold replays never reproduce either (live
      vote/roster state); unit-simulate if a gate is ever attempted.
- [ ] **Flag-dump writes starve the detect loop** (checkpoint 33 finding):
      `drill_shot_0801`'s ring spans 41 s for 1562 frames — detection ran
      at ~38 Hz while the previous dump was writing 3.6 GB to the T7, and
      the live stick-sweep phantom fired inside the degraded window.
      Candidate fixes: throttle the dump writer, lower ring seconds, or
      accept (flags cluster at failures anyway). Measure first: timestamp
      gaps in any dump recorded within ~15 s of a prior flag press.
- [ ] **IDLE break gate vs real breaks/openers** (checkpoint 33): the gate
      ate a real, frame-verified break that read 705 mm/s
      (`break_underread_0801` — the white streaked unobservably fast; the
      kinematic speed bound fixes the live-shaped underread when the
      candidate confirms, but a cold replay never confirms that break at
      all: the candidate follows the flag and the streak dies first). It
      also ate honest ~1000 mm/s drill openers twice (flagged_7 session,
      Restart pressed both times). Live-validate the kinematic bound on
      the next real break; if drill openers keep annoying, the answer is
      a deck-key workflow ("Start begins a drill rack") or a rack-context
      discriminator — not a lower floor (placement lookalikes sit at
      ~1000 too, and hard bowled returns at 3489-3926 — the newborn IDLE
      gate now catches those). Also live-validate the newborn IDLE gate's
      residual: a break struck from a fully occluded address would fire
      newborn and be eaten.
- [ ] **Quiet-table bowls/placements fire mid-rack** (checkpoint 33, the
      dominant remaining phantom class — flagged_10/11/13's first fires
      and both flagged_9/14 bowls counted rack shots live): a ball bowled
      or firmly placed on a settled table is kinematically a clean strike
      (newborn at speed, observed, smooth, unmerged after release) and no
      positional gate separates it — the arm blob at release is
      indistinguishable from the stroke blob at impact (guarding births
      on it vetoes real occluded strikes: lone_roller replay evidence).
      In IDLE the Hub's newborn gate now absorbs them; RUNNING-state
      bowls still count phantom shots. Acoustic corroborator class (top
      of the accuracy queue); idea-bank levers: apparent-size/height cue
      for carried balls, release-point arm-blob escort analysis.
- [ ] **Record `glove_address`** (~10 s: address with the bridge glove
      planted, slide the bridge hand deliberately, never shoot): measures
      the planted-glove-slide residual the roster rule cannot cover, and
      whether admission needs an appearance gate.
- [ ] **Stream Deck phase 2 — session toggle**: daemon owns `run` + OBS
      lifecycles; one-time OBS settings. Spec: `hardware.md` §C4.
- [ ] **Stream Deck phase 3 — Replay key** (obs-websocket).
- [ ] **Stream Deck phase 4 — `/triage-clips` skill** (the off-table half of
      the Flag key; flow in `hardware.md` §C5).
- [ ] **Deck bring-up checks** at the table: `hardware.md` §C6 (HID wake,
      power assertions, OBS restore).
- [ ] Deck placement: desk stand first; break-end wall mount later if it
      earns it.

## Queued features (build when asked)

- **Auto game-end detection (auto New Rack)**: in 9-ball the rack ends when
  the 9 ball leaves the table (8 ball in 8-ball) — detecting that requires
  ball *identity*, a standing v1 exclusion. The deck **New Rack** key is the
  decided interface; revisit only if 16-ball identity ever ships. (Ball-count
  proxies don't work: object balls leave the table all rack long.)
- **Per-player extension tracking**: WNT is one extension per player per rack;
  the clock has no turn tracking, so `wnt_9ball` allows a shared, self-policed
  pool of 2. Real enforcement needs turn state — manual input or inference —
  which is also what foul handling (below) waits on.
- **ChArUco intrinsics calibration** (`design.md` §4.3): gold-standard mm
  accuracy out at the pockets. Not urgent — the rails-fit shortcut gives
  0.76 px residual and a green corpus. Blocked on a session to generate the
  board PDF and build the intrinsics-fit tooling. ELI5: the wide lens bends
  the image like a peephole; showing it a printed marker board at 20–30 poses
  lets software solve the bending exactly.
- **v1 scope exclusions** (`design.md` §5, standing YAGNI list): replay export
  from the ring buffer (160 fps → 5.3× slow-mo), break-speed measurement,
  match stats, 16-ball identity, config hot-reload, auto-switch to final-10,
  match timer.
- **Replay polish** (after deck phase 3's plain-cut replay): branded stinger
  transition (alpha-channel animation), 50% slow-mo, a REPLAY banner — the
  banner also solves the burned-in clock overlay reading as live in replays.
- **Stream Deck extras**: ground-truth mode (see idea bank §8 — a "real shot"
  key + auto-dump on disagreement; the Pedal if marking shots must not take a
  hand off the cue), health face (grabs/s, clock state, drift warning).
- **TV power via VIDAA network API** — only if the remote ever annoys.
- **OBS taste calls left open**: 1080p-crop vs 1200p-letterbox canvas (decide
  at the TV); wired iPhone instead of Continuity Camera if flakiness/latency
  annoys during real sessions.
- **Foul/game-state handling**: v1 has none by decision (vision can't see
  whose visit it is — CHANGELOG 2026-07-27). If match-grade foul handling is
  ever wanted, the state comes from manual input (a "foul → ball in hand"
  button), not detection.

## Accuracy idea bank (exploratory)

From the 2026-07-27 research sweep + subsequent triage sessions. Nothing here
is committed; ideas graduate to the punch list when a live failure implicates
them. House rules: KISS/YAGNI, no neural detector in v1, thresholds in
physical units, every idea gradeable on recorded clips before it ships.
(Ported from `accuracy-ideas.md`; internal § numbers preserved.)

### §1. False Scratch from mid-flight occlusion near a pocket — RESOLVED

Fixed 2026-07-28: scratch now keys on the cue's last *observed* position, not
where the coasting extrapolation died (`design.md` §4.2). Unbuilt ideas that
remain relevant if pocket-adjacent bugs recur: exit prediction (coast the
velocity through the latch window — a passing ball's path *exits* the zone),
speed-at-loss gate (a sinking ball decelerates into the jaw), occluder
awareness (§2), and MOLT-style re-spawn suppression after a pot.

### §2. Detection-layer ideas

- **Report occluders, don't discard them.** The detector already finds the
  arm/cue blob, then throws it away. Reporting too-big blobs as "occluders"
  unlocks: scratch suppression under occlusion, smarter coast budgets, and a
  cleaner story for walk-around scenes (US8016665 precedent).
- **Streak blobs carry velocity**: a fast ball's elongated diff blob's major
  axis is a per-frame velocity vector; the streak center is the true position
  (BlurBall).
- **Sub-pixel centroids**: intensity-weighted centroids (or a paraboloid fit)
  typically halve centroid jitter — could win back the settle_creep margin
  (5 mm instead of the design's 3 mm exists because of measured jitter).
- **Per-pixel noise threshold**: `threshold = k·σ(x,y)` from the empty-table
  clip's free noise map — sensitivity in the quiet center without edge noise.
- **Chroma-aware differencing**: gray diff is blind where a ball's luminance
  matches the cloth; per-Bayer-plane diffs are nearly free. Audit per-color
  recall on `static_spread` first.
- **Known-radius circle extraction for merged blobs**: the stick-merges-ball
  blob is implicated in three separate workarounds. Ball radius is known
  (~21 px), making the split well-posed (distance-transform peaks + watershed,
  or RANSAC circle on boundary arcs). Highest structural leverage here.
- **Median (not mean) color patches**: specular-robust; widens the thin
  cue-vs-palest-yellow whiteness margin (0.85 vs 0.77).
- **Guarded reference maintenance**: never an adaptive subtractor (absorbs
  stopped balls — settled), but a guarded slow update of detection-free pixels
  keeps the reference honest over multi-hour sessions.
- **Double-threshold segmentation**: hysteresis segmentation stabilizes blob
  area at the min_area margin — only if track churn shows in live telemetry.

### §3. Tracking-layer ideas

- **Speed-proportional association gate**: at 8 m/s a ball moves ~102 mm/frame
  vs `gate_mm: 100`. Verify track continuity on `break_shot` (ID churn count);
  if it churns, gate = base + |v|·dt.
- **Full-rate detection while the table is moving**: 156 Hz halves the
  impulse-timing quantization the step gate leans on; drop back to half-rate
  on settle. Budget fits (detect avg 4.0–4.5 ms vs 12.8 ms).
- **Per-track chroma signature for re-ID**: an EMA chroma vector per track
  gives occlusion-recovery identity by color agreement, not just proximity.
  The strongest externally-evidenced idea on this list (POOL-AID's 72% →
  98.6% identification came from color histograms + global assignment).
- **Merged-blob multi-assignment**: let an oversized blob satisfy two
  predicted tracks — matters for racking and pocket-jaw scrums.
- **Cue-ball size as a free identity feature**: if the cue ball's diameter
  differs from the object balls' (common in UK sets), blob area is a second
  independent vote for the cue flag. Measure on `static_spread` first.

### §4. Event-logic ideas

- **Close-range stun leak (probable real miss)**: a legal stun from < 6 cm
  never covers the 60 mm confirm displacement. Record `close_stun` (§7A) —
  expected to FAIL today. Fix candidates: confirm-by-consequence (an object
  ball gains an impulse within ~50 ms), or scale the confirm displacement when
  an object ball sits inside the confirm radius.
- **Object-ball corroboration as a tie-breaker**: momentum transfer is visible
  in the tracks and unused. A weak optional "+1 confidence" — never a
  requirement (safeties into empty space are real shots).
- **Highlight-jitter settle test**: the specular highlight's sub-pixel
  position is pinned by lamp geometry — its jitter is an extremely sensitive
  "truly stopped" detector (Legg et al.). Settle-side only (degrades under
  motion blur).
- **Scratch zones per pocket geometry**: the 120 mm disc was tuned on one
  corner scratch; middle-pocket jaws differ. Grade scratches into all six
  pockets (§7A) before adding per-pocket-type radii.
- **"Ball off table"**: a track lost at speed near a rail but not a pocket is
  currently silent; it's ball-in-hand in play. Cheap to latch once a clip
  exists.
- **Cue-stick as a positive signal (YAGNI-flagged)**: gate strikes on "stick
  present and pointing at the cue ball". Costs line logic in distorted space,
  and every nudge provoker also has a stick present — only if hand-only false
  strikes appear.

### §5. Corroborating signals (non-vision)

- **Acoustic click coincidence** — the designed answer to `tip_bump` vs
  `soft_shot` (`hardware.md` Part D, patent US10226685 precedent). The corpus
  graded `tip_bump` correctly, so the trigger hasn't fired; if live play leaks
  nudges, this is the first lever, not more vision tuning.
- **The Stream Deck as a ground-truth instrument** — §8.

### §6. Survey: what other projects do

Condensed findings from the 2026-07-27 sweep (full source list in the archived
`accuracy-ideas.md`): every independent project converged on the same skeleton
this repo has (background diff over Hough — thrice-confirmed; blobs over
generic trackers — MOLT benchmarked KCF/TLD/MIL/CSRT/MOSSE all failing on
billiards; conjunction-style events). Transferable specifics: POOL-AID's
identity-by-color-histogram + global assignment (the biggest documented
accuracy jump anywhere, 72.2% → 98.64%); potted = presence-then-absence with
debounce, then STOP reacquiring at that spot (MOLT's z-counter); occluders
enter from the frame edge (validates the mask-border-stub rejection);
ball–ball contact lasts hundreds of µs (Mathavan) so the impulse — not the
touch — is the only observable, and Rea & Dahyot model contact as the birth of
the struck ball's motion; US10226685's global-motion flag re-armed only by
cue-ball motion; OpenPool's ~$10 IR break-beams per pocket as pot ground truth
for labeling.

### §7. Corpus expansion — clips worth recording

Same recording discipline as `design.md` §9. **A. Real-shot coverage gaps:**
`close_stun` (expected fail — sizes the §4 leak) · `carom_into_cue` (the carom
guard's dedicated clip — guard v2 is pinned by unit tests + the flag captures,
but a staged kiss clip is still the direct test) · `cushion_frozen_shot`
(border-stub rejection vs a real frozen ball) · `jaw_shot` (shot from inside
the pocket zone — must not scratch) · `pocket_pass_slow` (slow roll across a
pocket mouth — hardest no-scratch case) · `scratch_each_pocket` ·
`scratch_clean_center` (track ends abruptly inside the mouth) ·
`object_ball_pot` · `jaw_rattle_stay` (rattles and stays up — settle must
still fire) · `hanger_falls_late` (ball drops seconds after settle — no
scratch, no shot) · `multi_shot_run` (3–4 consecutive shots — first
event-*sequence* grading and cross-shot state-leak probe).
**B. Appearance provokers:** `glove_wear` · `white_sleeve` · `chalk_on_cloth`
(persistent ball-scale foreign blob — does it age into judgeability?) ·
`cue_ball_dirty` (whiteness margin under dirt) · `lighting_variants` ·
`shadow_walk` (strongest moving shadows the room can produce).
**C. Settle & timing:** `medium/power/break_shot_long` re-records with full
settle tails (mandatory the day settle drives the clock live — it does now) ·
`settle_chatter` (hysteresis probe: one settle cycle per nudge) ·
`spin_draw_pause` (draw-shot pause must not fire settle).
**D. Ops/drift:** `empty_table_end_of_session` (measures real reference drift,
sizes the §2 guarded-update idea) · `frame_drop_replay` (a replay-harness mode
injecting timestamp gaps — the observed 544 ms iCloud stall as a profile).

### §8. Grading methodology upgrades

- **The ground-truth button**: a deck key pressed at every real shot during
  shadow-mode sessions; the server aligns presses with emitted events and
  auto-dumps the ring on every disagreement. Every practice session becomes
  corpus growth with zero extra table time — the single highest-leverage
  accuracy investment on this list. (The Flag key + ring dump built 2026-07-29
  are its first half.)
- **Pocket ground truth for ~$10**: IR break-beams in the pockets as a
  *labeling* instrument (detection stays vision-only — settled).
- **Latency grading**: a frame-accurate contact label per shot clip upgrades
  count assertions to "ShotTaken within X ms of contact" (checkpoint 11
  measured 20 ms by hand — pin it before it drifts).
- **Golden event timelines**: persist each clip's full event list as a
  committed golden file; a tuning change that moves a settle by 300 ms
  currently passes silently.
- **Holdout discipline**: the original 17 clips have been tuned against
  repeatedly — they are a *training set*. Grade every §7 addition before
  tuning on it; the honest accuracy number comes from clips the thresholds
  never saw.
- **Threshold sensitivity sweeps**: re-grade the corpus across a grid around
  each config value; narrow passing plateau = overfit = fragile live.
- **Synthetic clip perturbation**: replay clips through gain/gamma shifts,
  noise, and frame drops — a corpus multiplier that costs no table time.
- **Detector-level metrics**: hand-label ~20 scattered frames once; per-frame
  recall/precision and centroid σ get tracked separately from event grading.
- **Live telemetry counters**: per-session track births/deaths, cue-flag
  switches, coast activations, candidates opened vs fired — the early-warning
  system for silent accuracy decay.

### §9. First-pass priority read

1. **Cheap and compounding now**: sub-pixel centroids, median color patches,
   golden timelines + latency labels.
2. **Before trusting the clock in matches**: `close_stun` + `multi_shot_run` +
   `scratch_each_pocket` recordings, long-tail settle re-records, ground-truth
   button + shadow mode.
3. **When a live failure implicates them**: merged-blob circle extraction,
   chroma re-ID, full-rate-while-moving.
4. **Hold until the corpus proves the need (standing decisions)**: acoustic
   click corroborator, cue-stick detection, CoreML auditor, any neural
   anything.
