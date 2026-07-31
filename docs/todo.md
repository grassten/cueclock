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

**Accuracy blocker — FIXED IN CODE 2026-07-31 (checkpoint 23), awaiting live
validation.** The "clock didn't stop on my shot" / "clock stays paused with my
hands in frame" failures were three stacked defects in `vision/events.py`, all
pinned by the Flag-key captures:

1. **Carom guard starved by player presence** (the checkpoint-20/22 root
   cause). Redesigned: an impulse is rejected only when another ball was
   *genuinely moving* within 0.4 s of it AND within 200 mm kiss range of the
   cue's rest spot. Per-ball, windowed, self-clearing — no persistent state
   for hands to starve. (Both conditions are needed: time-only re-broke
   medium/power_shot — the grip hand reads as a genuine mover during the
   stroke, but from 271–340 mm away; a real kissing ball arrives ≤ ~120 mm.)
2. **Junk tracks drove the table state.** Hand/stick fragments flicker
   through the detector gates (7–43% observed vs 90–100% for every real
   ball); tracks under 70% observed no longer judge table motion or
   stillness. Settle can't stall while you lean over the table, and a hand
   wave can't mint a BallsSettled (= a free 30 s clock reset).
3. **Strike axis seeded from noise** (masked by #1 until the guard passed):
   the candidate opened on the impulse's first frame with a 2 mm noise
   direction vector pointing backwards, then killed the true strike as
   "incoherent". Candidates now wait for 12 mm of honest displacement.

Corpus: all 22 scenarios green, including the three promoted Flag captures
(`missed_strike_0731`, `live_divergence_0731a/b`: exactly one ShotTaken + one
BallsSettled each, matching table ground truth), with the harness now replaying
each clip at live cadence (ring dumps store 78 fps — a fixed stride-2 had been
grading them at half speed). **Next live session: play normally and keep
flagging** — any remaining miss/phantom dump now gets a full per-gate verdict
from the scratchpad probes.

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

## 2. Refresh the stale reference (~10 min at the table) — NEW, from the telemetry session

The startup check fired at launch: **boundary drift 8.1** vs the 07-29 reference
(same-mount noise ~2, warn > 8) — lighting or a small camera shift since
`empty_table_0729`. Detection still ran, but segmentation margins are degraded
until this is refreshed, and it muddies accuracy debugging. The standing recipe:

1. Clear the table, T7 mounted: `uv run python -m cueclock.cli record --label empty_table_MMDD --seconds 3`
2. `uv run python -m cueclock.cli fit-undistort tests/clips/empty_table_MMDD`
3. `uv run python -m cueclock.cli calibrate tests/clips/empty_table_MMDD`
4. Relaunch `run` with `--reference tests/clips/empty_table_MMDD` (and update
   the launch command in this file's header + the future session-start macro).

*(Detect-loop load test: **CLOSED 2026-07-31, checkpoint 22** — cadence
exonerated at full rate with detect avg 4.0–4.5 ms vs the 12.8 ms budget; three
isolated max-spikes to 21–47 ms never sagged the rate below 144 for more than
one interval. Verdict + remedies archive in git history; the live-only misses
are the carom guard, see the open-bug block above.)*

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
