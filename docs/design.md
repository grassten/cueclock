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
| `state.py` `ClockState` | `IDLE → RUNNING → MOTION (shot in progress) → RUNNING (reset to full) → GRACE (2 s after 0) → EXPIRED (flash) → auto-reset for next player`. Extension allowed in RUNNING/GRACE only; adds `extension_seconds`; re-arms urgency cues; respects `extensions_per_frame`. Edge-triggered audio cues `ten`/`five`/`expired`, fired exactly once per countdown. Amended 2026-08-01 (checkpoint 26): **a settle in IDLE is a no-op** — IDLE means the operator said hold (New Rack, pause), and racking produces genuine settles that must not start the countdown; the untimed break still enters via ShotTaken (IDLE→MOTION). And **a settle with no ShotTaken since the previous settle counts the shot anyway** — balls moved and came to rest, so a missed strike (or a sub-impulse touch shot) no longer leaves the rack shot count short and replays the WNT 60 s window. Trade-offs accepted: a missed *break* now leaves the clock IDLE (visible; Start N recovers), and a phantom settle inside the first-shot window burns it (Push recovers). Amended 2026-08-01 (checkpoint 28): **in IDLE, a ShotTaken below a break-speed floor is ignored** (3000 mm/s; Hub-side until checkpoint 45, now a field of the `match` mode — `clock/modes.py`). `ShotTaken.speed_mms` is the fastest *observed* ball in the strike-candidate window — the candidate target's own raw underreads on handoff strikes (live lesson, same morning: the gate ate a real break whose target remnant read 2072 mm/s while the reborn cue flew 5300+; `break_0801` pins the fix). Measured with these semantics: breaks 5845–7361 mm/s vs. every ordinary shot ≤ 2868; racking/placement lookalikes below 1000. The one live rack-gather phantom started the clock 19 s early and swallowed the real break; out of a fresh rack the only legal shot *is* a break. Trade-offs: a feather-soft break stays IDLE (Start N, same as a missed break). Events without a speed (manual/synthetic) pass. Amended 2026-08-02 (checkpoint 45): the floor is a **`match`** rule — practice that starts without a break belongs in `run_out` mode, which has no floor because it has no break to gate. Amended 2026-08-05 (checkpoint 69): "Start N recovers" now recovers the *rack*, not just the timer — pressed in a rack-scoped mode with no shot yet counted, `restart()` **counts the break** (`_rack_shots = 1`). The two trade-offs above both end with a missed break and a press, and neither incremented the shot count, so the rack ran one short and the 60 s window replayed on the shot after the press (live, twice: 2026-08-05 19:59 and 2026-08-06 01:53). The press is the operator saying the break is behind us — out of a fresh rack there is no other shot it could mean, and the countdown it hands out is unchanged (`current_base()` still reads 60 at one shot). Amended 2026-08-10 (checkpoint 83, #33): **the shot became a settle-time decision.** A RUNNING countdown freezes provisionally at the engine's motion-episode latch (~0.25 s after the stroke; the hardened roster-mover latch — the raw E9 motion bit still cannot freeze RUNNING), a RUNNING ShotTaken defers its count to the settle, and the settle adjudicates the whole episode with hub-side evidence policy: exactly one shot per episode, fired or compensated (lateness within the episode is unbounded), unless the evidence is positively placement-shaped — rearrangement `moved==0, involved==1`, no surviving episode mover ≥ `moving_enter_mm`, no scratch candidate anchored inside the episode — in which case fire and freeze revert together: nothing counted, remaining restored exactly. The engine's pickup discard reverts the same way. GRACE and IDLE fires stay immediate; every ambiguity fails open to counting (the checkpoint 26 compensation is the default verdict, now merely refusable on measured placement evidence). |
| `Ruleset` config | `ultimate_pool_8ball` (30 s base / 1×30 s extension / 2 s grace / 2 s post-expiry), `ultimate_pool_8ball_final10` (15 s variant), `ultimate_9ball`. Values carry over verbatim. |
| HTTP API | `POST /api/{start,pause,reset,extension}`, `POST /api/ruleset/<name>`, `GET /api/{state,rulesets}` — this is the **Stream Deck contract**; keep the URLs stable so the Stream Deck buttons keep working. Added 2026-07-29: `POST /api/dump?label=<x>` saves `cueclock run`'s in-RAM ring (last ~12 s of raw frames) as a labeled corpus clip — the "that was wrong / interesting" button. Added 2026-07-31 (WNT match mode): `POST /api/rack` (per-rack reset: shot count + extension pool, IDLE at the first-shot time), `POST /api/restart` (fresh countdown, RUNNING immediately — missed-auto-reset recovery), `POST /api/push` (push out called: re-enter the WNT §5 60 s first-shot window; press again on a pass-back), `GET /api/score`, `POST /api/score/{p1\|p2}/{increment\|decrement}` (decrement floors at 0; unknown player/op → 404), `POST /api/score/reset` (0–0, names/race kept), `POST /api/score/config` (partial JSON body `{p1_name, p2_name, race_to}`; `race_to: null` clears), `POST /api/vision/{on,off}` (kill switch: while off, vision events don't move the clock; operator commands still work). Added 2026-08-01 (warm replay): each ring dump also carries `state.pkl` + `state.json` — the oldest of `run`'s ~1 Hz rolling vision-core state snapshots that falls inside the ring window (whole-object pickle of tracker + event engine, deliberately schema-free so new engine state rides along; measured 2.33 MB / ~1 ms per snapshot on real mid-shot state). `analyze --warm` restores it, skips the frames it already contains, and replays the rest at session-relative timestamps — reproducing exactly what the live session computed, accumulated state included (whiteness-vote position, roster, episode latches: the divergence class a cold replay can never show). `state.json` stamps a schema version + a fingerprint of the state-bearing vision sources so a stale pickle fails loudly, never silently. Session-journal contract, same date (strictly additive — readers must ignore unknown kinds/fields): vision-event records add `frame_t`, the confirming frame's *capture* timestamp (camera clock, session-relative; unambiguous vs. the record's emit-time `mono` stamp — `t` keeps the same value for old readers), and a `detect_heartbeat` record every ~5 s carries `hz` (effective detect rate), `frame_age_s` (staleness of the newest processed frame), and its `frame_t` (the camera→journal clock anchor). The heartbeat is emitted from the Hub tick loop, which keeps beating while the camera thread is starved — so dump-write starvation of the detect loop is measurable from the journal alone. `flag_dump` records gain `warm` (whether the dump carries a snapshot). Added 2026-08-02 (session modes): `POST /api/mode/<name>` and `GET /api/modes` — modes are `match` / `run_out` / `no_clock` (`clock/modes.py`), an axis **orthogonal to the ruleset**: a ruleset is a rulebook, a mode is what you're doing at the table. Only `match` is rack-scoped (first-shot allowance) and only `match` applies the IDLE break-gate floor; `no_clock` parks the countdown *and* stops vision driving it in one command, journaled as one `cmd_mode` intent, with events still recorded as `vision_shot_taken_no_clock` / `vision_balls_settled_no_clock` so drills stay evidence. Mode is independent of the `/api/vision/{on,off}` kill switch — neither gate being open reopens the other. Added 2026-08-02 (instant replay): `GET /api/last_shot` → `{"age_s": <float>|null}`, seconds since the last ShotTaken — the anchor the Stream Deck's Replay key seeks the OBS buffer to (`hardware.md` §C4a). Its own endpoint rather than a field of `/api/state` because the deck polls state every 2 s and this answer is only worth having at the instant of the press. Deliberately an *age*, not a timestamp: the deck is a separate process whose `monotonic()` shares no epoch with the server's. The anchor is stamped in `Hub.shot_taken` **before every gate** — kill switch, mode, IDLE break floor — because those decide whether a shot moves the clock while replay asks what is on the video; that is what makes the seek work in `no_clock`, where drills happen. Accepted cost: a phantom shot anchors a phantom replay. Also added: the `/replay` page (REPLAY badge for the OBS Replay scene — scrim, wordmark, drain bar; `?s=<seconds>` sets the drain rate). Amended 2026-08-02 (live validation of checkpoint 46): the badge also takes `?mask=<x,y,w,h;…>`, opaque rectangles in **percent of the canvas** covering wherever the live scene drew the shot clock — the buffer records program output, so that countdown is in the recorded pixels and can only be covered, never switched off. The deck measures the rects from OBS itself at each press (enabled browser items whose URL contains `/shotclock`), which is what keeps a patch from drifting off the thing it hides. Percent rather than pixels because OBS may record a rescaled output of the canvas the transforms were read in. The page also fades to black over its last 0.45 s and counts down `LIVE IN n` to the same instant: a replay is a pixel-exact rewind of the TV, so the cross-fade back to live dissolves two near-identical pictures and reads as nothing happening. `?s` is the *page's* timeline, which is `play_s` plus the badge's pre-cut head start (`hardware.md` §C4a), not the airtime. Added 2026-08-02 (ground truth, #19): `POST /api/groundtruth` records that a shot was actually taken — journal kind `ground_truth_press`, carrying `t`/`frame_t` on the *capture* clock (extrapolated from the newest processed frame, so alignment never crosses the emit-time/capture-time boundary; `null` when no vision is feeding the hub, which the reader treats as unanchored rather than guessing). **It never touches the clock** — an observation, not a command — and it is deliberately **not a mode**: one unconditional key, always live, no toggle, because the player must never have to classify a failure at the table. Missed / phantom / late are derived offline by `cueclock report`, which diffs presses against emitted `ShotTaken` events (including the gated `vision_shot_taken_ignored_idle` / `_no_clock` / `_killed` kinds — "vision saw it, the clock declined it" is a different bug from a miss). Amended 2026-08-03 (checkpoint 60): **`?mask=` is withdrawn** — the `/replay` page takes `?s=` only. The rectangles were correct and the burned-in countdown really is in the recorded pixels, but an opaque patch over live footage looked worse on the wall than the stale clock it hid, and the strategy has no good tail (from the full-screen `Shot Clock` scene the same rule masks ~70% of the canvas). The countdown is visible in replays again; the fix is to stop burning it over live video in the first place, which is a live-scene layout question, not an API one (`hardware.md` §C4a). Amended 2026-08-03 (checkpoint 61, #23): that layout landed, and with it **`GET /score` is withdrawn** — the scoreboard is drawn by `GET /shotclock?view=bar`, which carries clock and score together. No `/api/*` URL changed, so the Stream Deck contract is untouched; the merged page needs nothing the `/ws` payload didn't already carry. `/score` returns 404 rather than redirecting, deliberately: a stale OBS browser source pointing at it must fail visibly, not keep looking like it works. Amended 2026-08-05 (checkpoint 65, replay spans): **`GET /api/last_shot` is replaced by `GET /api/replay_anchors`** → `{"shot_age_s": <float>|null, "rack_age_s": <float>|null}`. The Replay key now has two spans — a tap plays the last shot, a hold plays the rack — which is the same cue-point arithmetic against a second anchor, so the endpoint returns both ages in one round trip measured against one instant (the save that ends the file). Renamed rather than grown a field, because an endpoint named for one anchor that returns two is a name that lies; the old URL 404s deliberately, so a deck daemon left on the old build fails visibly instead of quietly cueing every replay to the top of the buffer (same rule as `/score`). `rack_age_s` is stamped in `Hub.new_rack` (the New Rack press) and superseded by the next `shot_taken` (the break), so a rack replay opens on the break rather than on a minute of racking; both stamps sit ahead of the same gates the shot anchor does, so a `no_clock` drill anchors on its first shot. Null means "no rack yet", which the deck reads as *play as much of the buffer as there is* — the fallback a stale shot anchor already had. Amended 2026-08-05 (accuracy instrument): **`POST /api/groundtruth` is withdrawn** and 404s, per the same rule as `/score` and `/api/last_shot` — a deck left on the old build must fail visibly. The key it served (#19, "I shot") was pressed in 2 sessions out of 29: one unconditional key was the right design and still the wrong mechanism, because a key the player must remember on every shot is a key that stops being pressed. The denominator it existed to provide was already in the journal. `ShotClock.balls_settled` increments `rack_shots` **only** when no ShotTaken arrived since the previous settle (the missed-strike compensation added 2026-07-31 for correctness), and every record carries that count — so a `vision_balls_settled` record whose `rack_shots` exceeds the preceding record's *is* a self-reported missed strike, and settles recorded in `IDLE` are racking motion rather than shots. `cueclock report` derives the miss rate from that, over any journal, with nothing asked of the table; `run` prints it on clean shutdown and appends a `session_summary` record (strictly additive, as ever). Measured over every session to 2026-08-05: **764 shots, 25 missed, 3.27%** — flat across nine days in which `vision/events.py` grew 257 → 1842 lines, which is the evidence that the clip-by-clip loop had stopped converging. Derived and never accumulated, so the answer survives a `kill -9` and a better reader retroactively improves every session on disk. Amended 2026-08-05 (checkpoint 67): **`rack_age_s` is the break and nothing else.** `Hub.new_rack` now only *arms* the next `shot_taken` to become the anchor; it no longer stamps itself, and it no longer retires the previous break. The stamp-the-press half was wrong in the press it exists for — you run out, hit New Rack, and hold Replay to watch the rack you just played, at which moment "this rack" was a two-second-old empty table (live: `rack 2.0s ago, 598.5s file, cue 595.1s, 3.4s on air`, against a rack that broke ~266 s earlier and sat well inside the buffer). Between racking and breaking there is by definition nothing on the table to watch, so the honest anchor there is the rack that just ended; the new break releases it. Null now means "no break yet this session" — same deck fallback, one fewer meaning. Added 2026-08-06 (FargoRate, #27): `GET /api/fargo/search?q=<name>` — a server-side proxy of dashboard.fargorate.com's `indexsearch` (the dashboard sends no CORS headers, so the control page cannot ask it directly), normalized to `{name, location, rating, established}` rows; `established` is robustness ≥ 200, FargoRate's own provisional threshold, so a 10-game rating is never displayed as an established one. Upstream failure is a 502 the page renders as "Fargo unreachable" — a third-party outage can never touch the clock or the score. `/api/score/config` gains `p1_fargo` / `p2_fargo` (int sets, `null` clears) and the score payload carries `fargo` per player (`null` = no rating shown); changing a name without a rating in the same update clears the old rating, because a rating belongs to a name and a stale one under a new player is worse than none. Added 2026-08-07 (two-stage scratch verdict, #26, checkpoint 77): journal kinds `vision_scratch_candidate` (death spot, pocket, the dying crown's whiteness + staleness) and `vision_scratch_refuted` (witness id/position/reading) beside the kept `vision_scratch`, which is now the *adjudicated* verdict — strictly additive as ever, and the presence of candidate records is what tells a reader the journal's scratch count is a measurement rather than the pre-#26 ~6x upper bound (see §4.2). Added 2026-08-13 (double-elimination bracket, #43): `GET /bracket` (the bracket overlay page, its own OBS scene), `GET /api/bracket` (the full draw, or `{bracket: null}` in casual play), `POST /api/bracket` (create: `{name, players[], a_race, b_race, handicap, seeding}`), `DELETE /api/bracket` (clear → casual), `POST /api/bracket/archive` + `GET /api/bracket/archives` + `POST /api/bracket/archives/{name}` (write a finished draw beside the session journals, list them, reopen one), `PATCH /api/bracket/match/{id}` (redraw a Round-1 matchup, or override its two race targets), `POST /api/bracket/match/{id}/load` (put that match's two players and their own targets into `MatchScore` at 0-0), `POST /api/bracket/match/{id}/result` (confirm and advance; the body may carry explicit `scores`, and without one the loaded match's live scores *are* the result), and `DELETE /api/bracket/match/{id}/result` (un-play a match so the one feeding it can be corrected). **The governing constraint**: the bracket is optional state layered on top of the score already kept. `MatchScore` stays the single source of truth for what is on the TV — the bracket hands it two players and two race targets and takes a result back, and never keeps a score of its own, so the bracket page and the score bar cannot disagree in front of everyone. With none loaded every page and every deck key behaves exactly as it did before, which is what makes the casual night and the tournament night the same software. `/api/score/config` gains `p1_target` / `p2_target` (int sets, `null` clears) and the score payload carries `target` per player, because handicapping makes the two differ — a 563 against a 240 races 7 to 2, so one centre `race_to` would be a lie about at least one player; a target follows the same rule the Fargo rating already does, cleared by a rename that doesn't carry a new one. Confirming a result is deliberately **one action with several effects** (result written and both players routed, next ready match loaded at 0-0, `new_rack()` fired — a new match is a new rack), and the OBS scene flip is the deck's half, because obs-websocket lives out there (`hardware.md` §C3). Reporting a match that is *not* the loaded one is a correction rather than a new match: it touches the bracket and nothing else, so fixing a score from a phone can never park a clock somebody is playing under. A correction is refused by name (409, `Match 5 has already been played — clear its result first`) when anything downstream has been played, so a bad score is walked back one deliberate step at a time and no real result is ever silently erased. New journal kinds, strictly additive as ever: `bracket_created`, `bracket_cleared`, `bracket_archived`, `bracket_reopened`, `bracket_match_loaded`, `bracket_match_edited`, `bracket_result`, `bracket_result_cleared`. Amended 2026-08-13 (live iteration, §10.2): added `PATCH /api/bracket` (`{a_race, b_race}` — change the base races on a draw already underway; only unplayed matches move, a contested result keeps the race it was played under) and `POST|DELETE /api/bracket/player/{index}/withdraw` (concede every unplayed match of somebody who has gone home, down both sides of the draw, and put them back). A withdrawal invents no score and is **derived, never stored** — the same machinery byes already use — so it survives a restart as the player's state and reinstating is one press rather than an unwind; matches they already played are untouched in both directions. `DELETE /api/bracket` now answers `{ok, archived}` and writes anything contested beside the journals on its way out, because clearing is how you get back to casual and leaving the save behind a separate button made the destructive path the cheap one. `POST /api/bracket/match/{id}/load` parks the outgoing match's live score on that match and restores the incoming one's rather than zeroing — `MatchScore` is a single slot, so switching used to *be* the act of destroying what was in it — and refuses a walkover by name. `DELETE /api/bracket/match/{id}/result` keeps the score it cleared, so a correction behind a played match does not cost re-keying it. The match payload's `status` gains `paused` (started, then something else was loaded) and `forfeit`; players carry `withdrawn`. New journal kinds: `bracket_withdrawal`, `bracket_races_changed`; `bracket_cleared` gains `archived`. Amended 2026-08-13 (tournament eve, §10.3): added `POST|DELETE /api/bracket/match/{id}/concede` (`{slot: 0|1}` — one player gives up one match and keeps the rest of their draw, undone by the DELETE rather than by clearing a result, since a conceded match never was one) and `GET /api/lan` → `{url, qr}`, the scoring page's address on the LAN and an inline SVG QR of it, so nobody has to read an IP out loud to a room; `url` is `null` on a machine with no LAN address, and the host is discovered server-side because a page OBS loaded over loopback cannot tell a phone where to go. The match payload gains `conceded` (the slot that gave it up, or null) — a concession and a forfeit-by-withdrawal are both `status: "forfeit"` but are undone by different levers, so the page has to be able to tell them apart. New journal kind: `bracket_conceded`. Amended 2026-08-13 (UX pass): added `POST /api/match/clear` — the end-of-day reset the deck's Session **hold** fires on the way down: a loaded bracket is archived-and-cleared (same path as `DELETE /api/bracket`, so an unfinished tournament reopens from the archives) and `MatchScore` goes back to defaults (PLAYER 1 / PLAYER 2, 0-0, no race, no ratings, no targets), answering `{ok, archived}`; journal kind `cmd_match_clear`. Deliberately fired by the deliberate shutdown gesture and **not** by server exit, because mid-session restarts (deploying a fix into a live match) must keep the score — that is the whole reason `MatchScore` persists. The snapshot gains `timed` (`false` only under an untimed ruleset — overlays drop their countdown surfaces on it) and `GET /api/rulesets` gains `details` (per-ruleset `base_seconds` / `first_shot_seconds` / `extension_seconds` / `timed`, so the control page captions the pick without hardcoding the rulebook). New ruleset `freestyle` (`clock/rulesets.py`, `timed: false`): **match play with no shot clock** — distinct from `no_clock` *mode*, which is the drill surface (no racks, rep counters, no score keys); freestyle keeps everything match mode has except the countdown. Operator start/restart/push/extension are no-ops in the clock itself, and the hub gates vision events exactly as it does for `no_clock` (same `_no_clock` journal kinds; the payload's `ruleset` says which axis parked it). Extensions: `ENFORCE_EXTENSION_LIMIT` (`clock/rulesets.py`) is **off** — every extension over a live countdown is granted (a misheard verbal extension against an unlimited pool costs nothing, where a limited one burned a real extension mid-rack), and `extensions_allowed` reports 0 so no surface draws a pool that cannot run out; per-ruleset allowances stay in the data for the day the flag flips back. |
| WebSocket `/ws` | Broadcast `{state, time, display, ruleset, extensions_used, cue}` at 10 Hz + snapshot on connect and on every event. Added 2026-07-31: the broadcast is the composed Hub payload — the clock snapshot plus `vision` (bool, kill-switch state) and `score` (`{p1: {name, score}, p2: {name, score}, race_to}`); `GET /api/state` returns the identical shape. Added 2026-08-02: `mode` (the active session mode) and `base` (the countdown a fresh start would hand you *now* — rack- and mode-aware, straight off `current_base()`). `base` exists so the deck's `START N` key can state the time it is about to give you without recomputing the rule: the face is drawn from the same function the press calls, so it cannot drift. Added 2026-08-07 (checkpoint 80, #30): `solo` — the bar's solo panel, strictly additive. `null` in `match` (the scoreboard is the panel there); in `run_out` `{shots, last_s, avg_s}` where `shots` is the clock's own rack counter (so it inherits the missed-strike settle compensation) and `last_s`/`avg_s` are the wall-clock pace from each fresh countdown's start to the ShotTaken that ends it, `null` until the first *timed* shot — the opening shot of a run-out leaves IDLE with no countdown over it, so it counts in `shots` but never in the pace; in `no_clock` `{reps, elapsed_s}` — ShotTaken fires seen while the mode is on (approximate by design: they inherit detection accuracy, and a settle is deliberately *not* compensation here because drill placement motion settles constantly) and seconds since the mode was entered. Scope: the panel rescopes on mode switch and New Rack; pause/reset only close the open pace timer. Wall time deliberately includes scratch holds and extensions — the number answers "how long did the shot take", not "how much clock was burned". Added 2026-08-13 (#43): `bracket` — `null` in casual play, else a **compact** summary `{revision, state, name, champion, match, armed}`, where `match` carries the loaded match's id, title, side, phone header and winner/loser routing, and `armed` is `{slot, name, score, other}` once somebody has reached their own race target. Compact deliberately: the full draw is a few kilobytes and changes a handful of times an evening, so it rides `GET /api/bracket` and the pages refetch when `revision` moves, rather than being re-serialized ten times a second for every connected page all night. `armed` is *derived* from the live score against each target and never stored — which is what makes taking a point back clear the arm by itself, with no undo to find. |
| `shotclock.html` | OBS browser source, transparent 200×200; Web Audio chimes (10 s: E5 sine; 5 s: A5 triangle; expired: G4→D5 cadence); visual states: motion dims, grace red border, expired flashing. Requires "Control audio via OBS" for stream audio. Amended 2026-08-03 (checkpoint 61, #23): one page, three views, selected by `?view=`. **`?view=bar`** is the live scene's layout and the one that sounds the cues — a 160 px plinth along the bottom of the side-angle column carrying **the scoreboard as well as the clock**, so `score.html` and `/score` are gone. That merge is forced, not a preference: the drain runs the full width of the bar, crossing from the score area into the clock cell, and no overlay can span two OBS sources. Palette is `/replay`'s (`#0B0E13` ink, `#2E86C8` accent spent only on the drain, `#9FB3C4` slate, a single `#FF4D4D` alarm); escalation is by **area** — ink (urgent) → edge (GRACE) → field (EXPIRED) — because GRACE and EXPIRED both read "0" and three near-identical reds cannot be told apart at twelve feet. Geometry, states and rationale: `hardware.md` §B3. Amended 2026-08-13 (#43): a handicapped match has no single race, so **one fact goes in the centre and two facts go under the players they belong to** — differing targets are drawn as `TO 5` / `TO 4` beneath each player's own score, on their own side of the tally rule, and the centre line stands down while they are up. The first build put both in the centre as `3 OF 5 · 2 OF 4`, which said each score twice (they are already 48 px above it) and made four numbers in a row that had to be read left to right to work out who was playing to what. Equal targets collapse back to `RACE TO n` in the centre, so a bracket with handicapping off does not read as something new and one fact never looks like two; casual play with only `race_to` is untouched. |
| `control.html` | Buttons over the same API + live state display. Amended 2026-08-03 (checkpoint 61, #23): restyled into **Match / Clock / Session** sections, Match first, in the bar's palette — the phone surface and the wall read as one product. No new capability and no new endpoints; the names and race-to fields were already here and already posted to `/api/score/config`. Race-to became a stepper (on a phone a number input is tiny arrows or a whole numeric keyboard, for a value that is almost always 5/7/9) and destructive controls became outlined rather than filled. The Clock block keeps the deck's exact vocabulary (§C2) — plain `POST /api/start` lost its button, since `pause()` drops to IDLE and a later `start()` restarts from full base, which is what Start N already does. Amended 2026-08-06 (#27): entering a player name searches FargoRate through the server proxy and offers the matches in a pick-list under that input — name and location on the left (the only columns that separate fifty John Smiths), rating on the right with provisional ones marked `prov`, plus a skip row for unlisted/unestablished players. Attaching is always a human tap, never automatic, even on a single result. The rating shows as a badge on the player label here and rides the name — one step quieter — on the bar scoreboard. Each name field also carries an explicit **Fargo** button (added same day, live feedback): the `change` event alone is undiscoverable and never fires for a name that is already right — Steven's own persisted name retyped identically produced no search while a fresh name beside it did. Amended 2026-08-13 (#43): **the same page in both modes**. With no bracket loaded it is exactly what it was. With one loaded the Score section *gains* a match header (`Match 4 · A Side · Race 5-4`), an `of N` under each score, a line saying where the winner and the loser go next, a Report button enabled only once somebody has reached their race, and a Switch match list showing Live / Ready / Waiting — while the plus/minus keys themselves do not move, because they are what a thumb finds without looking. A third tab (**Bracket**) builds the draw — players by FargoRate search or a typed estimate on the same 0-800 scale, per-side races, handicap mode, seeding mode — and afterwards holds the state lifecycle (archive, clear, reopen) and the whole match list, where a played result can be cleared. Served on the LAN so players score from their own phones; it is family on a home network, so no auth. Amended 2026-08-13 (UX pass): **two tabs — Play and Setup** (the merged tab shipped as "Match" for an hour and was renamed back the same day — Setup is the name players already knew). Setup and Bracket merged: with a handicapped tournament live, Setup's single "Race to" had nothing true to say (it showed `Race to –`) and its name fields could fight the draw that set them, so the single match and the tournament became two sides of one segmented control that can never both claim the page. The segment follows the bracket (built one → Tournament) and a tap wins until the bracket changes; with a bracket loaded the single-match editors yield to a read-only card naming the tournament, the loaded match and its races. The **Mode** select left the page — in real use it is always `match` when a phone is out, and the deck's Mode key still cycles it for drills — as did the **Vision** kill-switch box (never used from a phone) and the join QR (whoever is reading the page already has it open; the wall's bracket scene keeps the code). Both APIs stay. The Session header became a **Ruleset** section: friendly labels over the canonical names and a caption of the rulebook's numbers fed by `GET /api/rulesets`' new `details`. The Play tab drops the extension counter while the pool is unlimited, and under freestyle replaces the countdown with a `FREESTYLE` pill and keeps only New Rack of the clock keys. |
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
- **The rejects are evidence too — export them (2026-08-11, #41).** The geometry gates above are already a *classifier*: an arm fails the area ceiling, a cue stick fails the minimum extent, anything reaching in from off-table fails the mask-border rule. Throwing those verdicts away left the rest of the system unable to see a hand at all — which is why four separate passes (checkpoint 50, checkpoint 66, #38) found the `merged` flag reading ~0 on hand events. A hand that *engulfs* a ball produces one over-size blob that is discarded outright, so there is no detection left to mark; the arm was never absent from the image, only from the exported evidence. `detect_frame` therefore returns a second list, `Intrusion`, describing the hand-scale rejects. Strictly additive — which blobs become balls is untouched, and §2.3 lesson 2 stands: balls are never shape-filtered, because fast balls streak. The one shape number it carries is the radius of the largest inscribed disc, which is where a hand and a cue actually differ: a hand carrying a ball measures 23–62 px against 8–20 px for a bridge hand with the shaft running through its fingers. What consumes it is settle-time *policy*, in the hub — see §4.2's ball-in-hand note.

### 4.2 Event logic (the actual product)

- **`shot_taken` — a conjunction, not a single signal** (patent US10226685 + US8992315 + launch-monitor pattern):
  1. cue ball stationary ≥ dwell (the old `pre_strike_stillness_ms`, now on a reliable track),
  2. velocity impulse: from below the settle floor to **≥ ~0.2–0.3 m/s within 1–2 detection frames** (softest deliberate shot ≈ 0.45 m/s — Dr. Dave; noise floor ≈ 0.007–0.022 m/s — §4.3), and
  3. displacement consistent over the next few frames (coherent trajectory — the confirm-window idea, now trivial with tracks).
  Ball-in-hand placement fails (1); practice strokes fail (2); hand-waves fail (2)+(3).
- **`balls_settled` — per-ball velocities with hysteresis**, not global pixel motion: every tracked ball's filtered speed **< 0.03–0.05 m/s** (RoboCup SSL autoref uses 0.05–0.2 m/s for officiating-grade "stationary") **and** max any-ball displacement over the last ~500 ms < ~2–3 mm (catches pocket-edge creep), sustained for a dwell; re-enter "moving" only above ~0.1–0.15 m/s (Schmitt-trigger hysteresis so it can't chatter). Per-ball velocity is immune to the failure modes that plagued global frame-diff: hands in frame, player shadows, a ball spinning in place. **Rules caveat (verified 2026-07-27, Steven's catch):** spin-in-place immunity overshoots — a shot officially ends only when all balls have "stopped moving **and spinning**" ([WPA def. 8.2](https://www.poolbilliards.co/rules/wpa-rules-of-play/definitions/); International Rules 6k's "come to rest" implies the same), so `balls_settled` must additionally wait for visible spin to stop. **Implemented 2026-07-28 as the appearance-churn gate:** mean |Δgray| in a 16 px disk inside each stationary ball, EMA-smoothed by the tracker, blocks stillness above 2.0 gray levels. Measured on `spin_in_place` at the 78 Hz detect cadence: still-ball noise floor 1.2–1.5 (max 1.66), spinning cue 2.6–6.5 decaying to the floor exactly at visible spin stop (t≈7.0 s in that clip); the gate moves settle from 6.48 s (mid-spin) to after the stop, graded by a corpus timing test. Hands/shadow immunity is unaffected — the gate never looks outside the ball, and unobserved (coasting) balls are exempt.
- **`scratch`** — the cue-ball track terminates (not re-associated, not coasting) with last position near a pocket zone; latch during motion exactly as the old system learned. Pocket zones come from the homography, so this is finally geometric rather than "ball missing anywhere." **"Last position" means last *observed* position (corpus lesson, 2026-07-28):** a dying track's final coordinates are up to a coast-budget of constant-velocity extrapolation, and an occluded passing cue's phantom can coast into the zone and die there (`spin_in_place`: last real observation 359 mm from the pocket, coasted death 89 mm). A real sink needs no such generosity — its final observations come from the pocket jaw itself (`scratch_replace`: last observation 110 mm from the pocket center).
- **Optional corroborator (backlog):** a broadband transient ("click") detector on the existing audio stream, coincident within ~100 ms of the velocity impulse — patent-precedented (US10226685), nearly free since an audio thread already exists, and it cleanly separates the one genuinely ambiguous case (slow tip nudge vs. soft shot: no click vs. click). Fresh motivation 2026-07-31: `soft_touch_0731` is a real live shot whose cue peaks at **225 mm/s raw** — below any impulse floor that still rejects nudges, so it is undetectable optically by design; the clock's settle-counts-the-shot rule (§2.1 semantics, checkpoint 26) absorbs it, but only audio can name it a shot.
- **Occluded-address strike — the newborn-cue path (added 2026-08-01, checkpoint 26, from `occluded_address_0731`):** when the player's body/stick hides the addressed cue ball long enough, its track dies, the whiteness vote parks on some other bright ball, and the real strike surfaces as a **brand-new track born already at impulse speed** (measured: born at 1572 mm/s, 964 mm from the wrong flag-holder's rest — every rest-anchored gate correctly rejected it; that live session's break and post-push shot were both lost this way). A ball cannot teleport into existence at speed, so a fresh cue-vote-winning track born ≥ impulse floor opens a candidate anchored at its **birth spot**, provided the table had no genuine mover for a pre-strike dwell before the birth and none within kiss range of the birth spot. Confirm adds two measured carried-ball gates (`scratch_replace`'s fetch carry, born 2220 mm/s, is the false-positive twin): ≥ 90 % of confirm-window frames observed (a carried ball coasts through finger occlusions; a struck cue on open cloth is observed every frame) and adjacent-frame raw-speed ratio ≤ 2 (carry: 575 → 4140 mm/s between frames; struck cue: ±5 %). The path is **armed** once per rest cycle: disarmed when it fires (no machine-gun re-opens off the same strike), re-armed by a cue-flagged ball at rest that has *translated since the disarm* — the whiteness-vote fallback (a still ball that never moved) must not re-arm it. (Scratch also disarmed the path from checkpoint 26 through 30; checkpoint 31 removed that — see the `jaw_strike_0801` bullet in §4.2 — and the fetch-carry false positive it guarded is contained by the carried-ball confirm gates instead.) **No-cue variant (2026-08-01, from `no_cue_strike_0801` + `scratch_shot_0801`):** the tracker can lose the cue outright — the address occlusion kills its track, the regrab grace and whiteness floor keep the flag empty, a fast newborn's blur-corrupted frozen color sample can't win the vote back, and a dead cue's track **coasts as a flagged phantom for ~0.15 s after impact**, longer than the whole newborn window. So when no *observed* ball holds the cue flag, the scan runs over every observed ball and the birth conjunction alone carries the identity (on a quiet table, only the struck ball can materialize at impulse speed); the candidate then follows that track by id, not the flag. Founding tracks are excluded (first sight at engine start is not a birth), and candidate expiry is centralized so a stuck cue-follow candidate can't block the scan.
- **Ball roster — who may latch table motion (added 2026-08-01, checkpoint 26):** address gear (bridge-glove knuckles, watch, stick fragments) reaches 79–94 % observed — past the flicker filter — and a 30 mm gear drift used to latch `table_moving` and mint a phantom settle ≈ a phantom clock reset mid-address (three in one live session, video-verified). Structural discriminator measured across every clip: real latches come from tracks alive since the last quiet period; gear tracks are born mid-play already moving. Only **roster** balls may latch: membership founded at engine start, then admitted by union whenever the table has been continuously still for a settle dwell (union, not replacement — a roster ball occluded by the addressing player must not fall off mid-aim), pruned wholesale at each settle. Racked/placed balls join at the next quiet dwell; a gear fragment sweeping in cannot join mid-sweep because its own motion breaks the quiet streak. Carom-guard mover stamps inherit the roster filter, so gear can no longer starve strikes either. Amended 2026-08-11 (checkpoint 88, #40, from `latch_blind_shot_0811`): roster membership is *negative* evidence, and the one class that can never earn it is **the cue reborn at the impact** — admission happens only during sustained quiet, so a track born mid-motion is permanently excluded. On the flagged clip the struck cue crossed the table at 2048 mm/s as a two-frame-old track and the engine latched **nothing at all** — no episode, no provisional freeze, no settle, 0 events in 20.74 s — while the countdown ran through the stroke; the strike path's own escape hatch (a fire certifies the track and latches directly) never ran either, because the crown was parked on the cue's dead identity. A non-roster track may now latch on **positive** evidence instead (`EventEngine._struck_mover`): certified struck-ball speed (two consecutive *observed* raw reports at `parked_fast_confirm_mms`) **and** a mostly-unmerged observed history (`newborn_merged_frac_max`), on top of the ordinary solidly-observed / `moving_enter_mm` bar. No new constant: both gates already existed for the same question in the strike path. Measured corpus-wide over every non-roster track clearing the ordinary bar — real strokes best consecutive-observed raw pair 1210–3803 vs. hand junk ≤ 1122, and the hand events that *do* reach struck-ball speed are carries, which read 0.67–1.00 merged against 0.00–0.17 for every admitted stroke. The new latchers deliberately do **not** stamp `_recent_movers` or `_last_mover_t`: a reborn cue must stay invisible to the carom guard and to the birth path's pre-strike-dwell test, or the admission would refuse the strikes it exists to catch. Known residuals, each with a recorded provoker: a planted glove that later *slides* ≥ 30 mm without lifting (needs `glove_address`-style recording to measure); the stick-merge yank and the hand pickup were residuals here until checkpoint 28 closed both (merge flag and pickup discard, below).
- **Cue-rest slide guard (added 2026-08-01):** the rest memory keeps its original timestamp while following the flagged cue's coordinates; when the whiteness vote jumps to a different still ball, that used to teleport the rest spot with ancient dwell credit intact. A still-stamp landing > handoff radius from the current rest now restarts the rest (fresh dwell) — a genuinely creeping cue moves ~0.5 mm between stamps and never trips it.
- **Merged-blob flag — a still ball cannot streak (added 2026-08-01, checkpoint 28, from `address_yank_strike_0801` / `merge_yank_0731`):** the stick or hand merging into an addressed ball's blob yanks the shared centroid 31–47 mm — past the 30 mm motion latch (phantom settle ≈ phantom clock reset mid-address) *and* enough to wreck the rest/still state the strike conjunction reads, so the **real shot two seconds later was missed live too** (video-verified: one root cause behind both "the clock randomly reset while I was down on the shot" and "then it didn't see the shot I took"). Fix is layered per §2.3 lesson 5: the detector reports (never gates) blobs that are too big to be one clean ball — measured clean still balls ≤ 1471 px² / ≤ 47 px extent across every frame vs. ≥ 2071 px² / ≥ 102 px merged, flag at 1800 px² / 70 px — and the tracker treats a merged observation on a **confirmed, still** track as presence evidence only: position/velocity/whiteness/churn untouched, `observed=False`, the observation consumed so the yanked centroid can't found a junk track. Moving tracks keep full streak tolerance (lesson 2 stands). Contamination small enough to slip under the flag cannot yank a centroid past the latch — the yank scales with the contaminating area. Bonus: the whiteness sample no longer ingests stick pixels, protecting the cue flag through addresses.
- **Pickup discard — motion episodes that leave the table (added 2026-08-01, checkpoint 28, from `ball_in_hand_pickup_0801`):** a hand pickup latches table motion honestly (a roster ball genuinely drags), then the fingers wrap it and its track dies — and the quiet that follows minted a settle ≈ a clock reset mid-visit. Measured discriminators: every picked-up ball's track dies **far from any pocket** after a **41–78 mm** drag, while a real shot always ends with a live mover still on the table (`shot_debug`'s object ball) or a pocket-zone death after real travel (its potted cue: 1699 mm). When every ball that latched the current episode has died off-pocket under 200 mm of travel (2.6× the measured drag max), the episode is a pickup: `table_moving` unlatches with **no settle**. Pot deaths (inside pocket zones) and any surviving mover keep the settle real. Two guards protect the no-cue strike, whose original track dies in the address handoff (mirroring a pickup death) while its newborn continuation is not roster and never joins the mover set: any reported ball's real translation within the last settle dwell defers judgment (`_last_translate` covers coasted frames, so a brief occlusion can't open a one-frame window), and a **sticky per-episode marker** — any *judgeable* non-mover ball translating while latched (junk can't pass the flicker filter; hand fragments also die before the 0.4 s judgeability age) — marks the episode a shot permanently, so the discard can't outrace the settle once the flying ball finally stops.
- **Sub-impulse touch shots — an honest hole, not a tuning target (measured 2026-08-01):** `soft_touch_0731`'s settle used to pass only because the stick yank phantom-latched motion; the touched ball itself peaks at 25–28 mm per 0.2 s window, under the latch, and no honest slower latch exists — `shaft_adjust` reaches **59 mm per 0.5 s window while judgeable**, worse than the touch shot's 43. With the merge flag the lucky settle is gone: a sub-impulse touch shot currently produces *no events* (clock keeps counting; Start N recovers). This is §2.3 lesson 6's boundary made precise; the acoustic click corroborator (backlog bullet above) is the designed answer for the whole class, not more vision tuning.
- **Axis-kill re-open credit (added 2026-08-01, checkpoint 29, from `stroke_tug_strike_0801`):** the final stroke can tug the addressed cue's centroid just past the 12 mm axis-seed floor (measured 12.04 mm) ~0.1 s before impact, opening a candidate whose axis points wherever the tug did. The axis check kills that candidate as designed — the documented intent has always been that "the real impulse can still re-open from the same rest" — but the step gate reads the *still-stamp*, which the tug burned: on the live miss it was 0.01 s too stale at re-open time, the fall-through newborn path rejects an ancient track, and the shot vanished (clock ran through the stroke, then silently reset at settle — the second recurrence of the `missed_strike_0731` shape). An axis kill on a rest-anchored candidate is itself evidence an impulse is in progress, so it now re-arms the step gate for one `strike_step_max_s` window; every other open gate (impulse floor, dwell, carom guard, seed floor) still applies at the re-open. Shaft pushes are unaffected — they die on the 60 mm confirm displacement, never on the axis check, so they earn no credit.
- **Carried-newborn merged gate (added 2026-08-01, checkpoint 29, from `pickup_newborn_0801`):** picking the resting cue up births a new track from the hand+ball blob — the old track's merge-hold consumes the blob until it slides beyond the association gate, and the remainder is a fresh id born at speed on a quiet table that sails through the newborn conjunction. This carry was smooth and fully observed, beating both original carried-ball gates (obs fraction, speed-step ratio). The discriminator that survives: **every one of its confirm observations was a merged blob** (8/8 — a ball in a hand is inseparable from the hand), while every real strike in the corpus confirms on **0 % merged** observations (occluded_address, scratch_shot, the 7361 mm/s break; one 11 % outlier on the non-newborn `no_cue_strike_0801`). The tracker now reports per-frame merged-ness on `BallState`, and a newborn candidate whose observed confirm rows are **> 50 % merged** is a carry, not a strike — 4× above the worst real strike, 2× below the measured carry.
- **Young solid movers block settle (added 2026-08-01, checkpoint 30, from `hanger_pot_0801` — supersedes the same-day "hand-rolled placement" misreading):** the clip is a real full-table pot of a hanging ball, cue following to the jaw (Steven's ground truth; the strike was always detected correctly). A fast shot's **terminal track churn** — coast-overshoot death near the pocket, a short-lived rebirth, then a fresh id at the jaw — left the still-rolling cue represented only by tracks *too young to judge*, and settle fired 1.3 s after the strike while the ball visibly rolled another 37 mm (clock reset mid-flight). The age gate exists because a short window can't measure creep or churn, but real displacement needs no window: a **solidly observed** track (≥ the flicker-filter fraction — junk measures 7–43 % observed, real balls 90–100 %) now blocks stillness on displacement from its first frames, while creep and churn judgment still require a full window. The clip's settle moves 12.85 → 14.56 s, after the ball truly stops. Known limitation left open in the same clip: the cue *stopping hanging in the jaw* is indistinguishable from a pot at track-death time, so a phantom **Scratch** fired (journal-only in v1; the counter-evidence — a ball reappearing at the death spot — arrives ~1 s too late to veto it). **Resolved 2026-08-02 (#6, checkpoint 55) — see the deferred-scratch bullet below.**

- **The IDLE gate reads speed, not birth (2026-08-02, checkpoint 57, closing #10):** a second IDLE gate added at checkpoint 33 ignored every *newborn* `ShotTaken` regardless of speed, on the premise that real breaks fire rest-anchored while hand-rolled balls fire newborn. Its two anchor clips turned out to be **9-ball breaks that fired newborn** — it was measured on examples of the thing it was meant to admit — and the residual its own note conceded ("a break from a fully occluded address is eaten") was that evidence, not a hypothetical. The instrumentation run reported both halves honestly: its **positive control did not pass** (nine rack-setup scenarios yield two `ShotTaken`, both the genuine shots in their clips, both rest-anchored — racking and placement are now event-silent, so silence above the floor could prove nothing), and what decided it was positive evidence instead: **seven real strikes in the corpus fire newborn** — the occluded-address and own-rest paths are newborn *by design* — against zero rack-setup motions that do. Removed. The **speed floor is untouched and is not in question**: it is anchored on racking/placement lookalikes below 1000 mm/s against breaks at 5845–7361, and it is what fixed the live rack-gather phantom that started the clock 19 s early.

- **Birth-path candidates must observe before they confirm (added 2026-08-02, checkpoint 56, closing #8):** the three carried-ball gates are all *fractions over the confirm window*, and a fast confirm can close that window in three observations — at which point they are a lottery, not a measurement. `pickup_snatch_0801`'s bare-hand snatch fired with `merged_frac` exactly **0.00** over 0.070 s while reading 71% merged across the whole carry. Nothing was wrong with the gates: `newborn_raw_step_ratio_max` catches this carry outright (ratio 1.01, 1.00, 1.07, then **2.24** at age 0.096 s) and the candidate simply confirmed one frame before the evidence arrived. `newborn_confirm_min_s` (0.10 s) makes it wait. The cost falls only on the birth path — the occluded-address class, where a tenth of a second is nothing against a 30 s clock — and rest-anchored strikes, which is nearly all of them, are untouched. **Generalisable lesson: a gate expressed as a fraction needs a floor on how many samples it may be computed from, or a short window silently disables it.**
  **Measured and rejected — do not re-derive:** the apparent-size *height cue* (the camera looks straight down, so a carried ball images larger). Real newborn strikes read 0.82–1.22 against balls resting on the cloth and the carry reads 2.45 — a clean 2× split — but **at the instant the phantom confirms the ball reads 1.14**, mid-band: it has only just been touched and has not risen yet, passing 2.0 about 25 ms later. A gate cannot use evidence that does not exist yet. It also misjudges `no_cue_strike_0801`, a *real* strike at 2.62 where the cue is merged with the stick through impact. Area *growth* during the window is strictly worse: a ball accelerating out of a strike blurs and streaks, so `break_0801b` grows 1.58× against the snatch's 1.27×, and growth resets every time the carried ball is reborn as a fresh track.

- **Scratch is a deferred verdict (added 2026-08-02, checkpoint 55, closing #6):** the counter-evidence above was never missing, only late, and Scratch drives no clock state in v1 — so it is simply waited for. Track death inside a pocket zone now *latches* a scratch; it is published `scratch_confirm_s` (1.0 s) later, and **cancelled outright if any observed ball is within `scratch_reappear_mm` (60 mm) of the cue's last observed position**. Measured on `hanger_pot_0801`: a reborn track appears 0.24 s after the loss, 33 mm away, converging to 12 mm — while on all three genuine scratches (`scratch_shot_0801`, `shot_debug`, `scratch_replace`) *nothing at all* is observed within 200 mm for the next 3 s. Two constraints learned building it: cancellation must be **positional only** (the whiteness vote parks on another white-ish ball within a few frames of the cue dropping — treating that as the cue returning suppresses every real scratch), and 60 mm has to stay well under the 94 mm at which an unrelated still ball sat during that same episode. **Consequence:** the emitted `t` is the confirmation, ~1 s after the ball actually vanished, and a Scratch can now land *after* the shot's `BallsSettled`. Subtract the delay when correlating against footage.
- **Scratch is a two-stage verdict (added 2026-08-07, checkpoint 77, closing #26):** the deferred verdict above acquits hangers, but a potted *object ball* mis-crowned as the cue passes every gate honestly — it died near a pocket while moving, and it genuinely never reappears. Live journals confessed a ~6x over-count from exactly this shape (29 in one session, 68 across all 15, vs. ~5 real per Steven's ground truth), and timing mining proved journal-only retro-adjudication impossible (the two populations' shot-to-death and next-shot gaps overlap completely). The fact the reappear watch never checks is the one that matters: **after a real scratch there is no cue on the table until the player replaces it; after a false one the real cue is sitting right there.** Scratch is log-only, so the verdict can wait for the cheapest reliable moment. The watch's confirm now emits a `ScratchCandidate` (death spot, pocket, the dying crown's whiteness EMA + its staleness — every live verdict journals its trigger evidence), and the verdict resolves by whiteness census: **refute on positive evidence only** — one roster ball, judgeable, still + observed, whiteness EMA ≥ `scratch_witness_whiteness` (0.72), born before the dead track's last observation → `ScratchRefuted` (witness id/position/reading journaled for spot-audits); **confirm on complete census** — table quiet, every live roster ball observed, none qualifying → `Scratch`; **horizon fallback** — census never completes by `scratch_verdict_horizon_s` (30 s) → confirm, keeping the pre-#26 upper-bound bias (absence of evidence must not acquit: the corpus shows the real cue absent from the census for a 41 s occluded address in `drill_shot_0801`). Anchors, all measured 2026-08-07: the floor sits mid-corridor between the corpus-wide sustained never-crowned rest-EMA ceiling 0.613 (a white-face-up stripe, 595 samples; pool p99 0.542 over 495 tracks) and the real cue's converged rest band ≥ ~0.83 (`scratch_floor_sweep.py`, cache-served full-corpus sweep of the exact quantity the gate reads); the horizon covers the live tail — 68/68 candidates saw a settle within 19.4 s of the trigger (p90 5.5 s). The **birth gate is mandatory**: `scratch_replace`'s hand-placed replacement reads 0.880 within ~1 s of landing, and on that clip's real pixels it is present at the census, birth-gated, and the Scratch stands. The witness gates (roster + judgeable) structurally exclude the sweep's one outlier, a single-frame flash track reading 1.000. Known leak, priced: a real cue at rest in deep top-rail shade reads 0.53 (`cluster_rest_shot_0806`) and cannot refute — that false candidate confirms, which is exactly the pre-#26 behavior, never a new failure mode; the same asymmetry holds everywhere (a failed refutation costs status quo, a false refutation would cost a real scratch — hence every gate leans toward not refuting). Consequences: `Scratch.t` is now the census completion (typically the settle) rather than latch+1 s — `shot_debug` moves 9.54 → 11.92 s, `scratch_shot_0801` is unchanged to the millisecond (its table was already quiet); all three corpus scratches confirm in-clip; `BallState` carries the tracker's whiteness EMA + staleness (`whiteness`/`color_age_s`); the tracker's identity defect (E7) remains the upstream cause and this adjudication works regardless of why the crown was wrong. Journal contract, strictly additive: `vision_scratch_candidate` / `vision_scratch_refuted` beside the kept `vision_scratch` verdict; `report` prints candidates/refuted and marks journals without candidate records as pre-#26 upper bounds. Unblocks #25 (post-scratch grace): refutation normally lands at the first settle, early enough for the clock; #25 can key on "candidate un-refuted at its settle", wrong only in the shaded-cue leak where the failure is unearned grace — the cheap direction. **Landed 2026-08-07 (checkpoint 78, closing #25):** exactly that keying — `ShotClock.hold` freezes a RUNNING countdown (5 s, the issue's spec — a felt number to revisit from the wall, not a measurement); the hub grants it once per candidate at the first opportunity (candidate-after-settle: immediately, scratch_shot_0801's order; candidate-during-MOTION: at the settle, shot_debug's order), `ScratchRefuted` releases it early (typically same-frame, so a refuted phantom never visibly touches the wall), the confirmed `Scratch` closes the candidate without touching the clock, and every countdown-starting path clears the hold so it cannot leak into the next shot. Gated exactly like the events that move the clock (kill switch, `no_clock`); journaled as `scratch_hold` / `scratch_hold_released`.
- **Scratch no longer disarms the newborn path (2026-08-01, checkpoint 31, from `jaw_strike_0801`):** the phantom Scratch found a way to drive the clock after all — not directly, but through the checkpoint-26 arming rule. A cue that stops hanging in the jaws sits **untracked over the pocket mouth** (frame-verified: no track on the ball at address; the whiteness vote parked on a still ball mid-table), so after its phantom Scratch disarmed the newborn path, no cue-flagged track could ever satisfy the translate-then-rest re-arm — the disarm stuck for good, and the strike out of the jaws (a newborn born at 1294 mm/s, clean and fully observed) was categorically dropped; the settle-counts-the-shot fallback absorbed the miss live. The scratch disarm predates every carried-ball confirm gate and is strictly redundant with them: `scratch_replace`'s real fetch carry fails all three independently (observed fraction ≥ 0.9, raw-step ratio ≤ 2 — both measured on that carry at checkpoint 26 — and merged fraction ≤ 0.5 from checkpoint 29), while a jaw ball rolling out cleanly passes them, which is exactly the discrimination the categorical disarm suppressed. Removed the Scratch disarm; the fire-path disarm (one shot per rest cycle) and its re-arm rule are unchanged. Corpus-verified against every scratch/carry scenario; the disarm regression itself is unit-guarded (`test_shot_from_pocket_jaw_after_scratch_fires` — cold corpus replay never sees the pre-clip scratch, so the pin alone can't catch it). Residual accepted: a ball *hand-rolled* back onto the table post-scratch would now read as a strike — kinematically it is one, no observed case exists in the corpus (checkpoint 29's "hand-rolled placement" was a misread, withdrawn in 30), and the exposure already existed everywhere outside the post-scratch window.
- **Own-rest (wrong-flag) strike path (added 2026-08-01, checkpoint 32, from `parked_vote_strike_0801`):** the whiteness vote can park on the wrong *still* ball for many seconds — the real cue lost the vote resting merged against a cluster (its color sample corrupts against the touching balls) and won it back only 0.6 s after being struck. Both existing strike paths are then structurally blind: the rest memory tracks the parked flag-holder (the measured impulse landed 578 mm from it, so the handoff path punts), and the struck ball's track is seconds old (11 s in the clip — not a birth, so the newborn path correctly rejects). The identity-carrying scan now covers this third configuration: when the flag-holder itself shows no impulse (parked still, absent, or unobserved), any **roster** ball that steps to impulse speed from a long rest of its own opens a candidate anchored on its **own rest spot**. The conjunction reuses only measured gates — roster membership (admitted at a settle: a real ball, not junk), a full `pre_strike_dwell_s` since the ball's own last translation (a hand push ramps and trips the creep window early, so it can never present a long dwell *at* impulse speed — the scan reads `_last_translate` before the impulse frame updates it), the carom guard at the rest spot, the direction-seed floor/ceiling, and the newborn confirm gates (observation fraction, raw-speed smoothness, merged fraction — the measured hand discriminators). No new thresholds. The same configuration explains the clip's *other* live miss (shot 1, which a cold replay catches — the live session's vote state diverged before the ring window) and the 9 s late live settle that followed it; live validation judges that half.
- **Phantom-flag handoff strike path (added 2026-08-05, checkpoint 64, from `phantom_flag_strike_0805`):** the addressed cue's own track can freeze at its rest spot and **keep** the whiteness vote while the ball is struck and gone, so all three existing paths are blind at once. Three tracker mechanisms compose to produce it: a still track matched by a merged blob is *held* in place (checkpoint 28) and, unmatched, coasts for `still_coast_s` (3 s) on the premise that an addressed ball cannot move unseen; the struck ball re-emerges past the 100 mm association gate and so founds a **new** track; and a frozen track's color is never resampled, so its stale whiteness outranks the live one and the flag never transfers. Measured: the cue was last detected at its rest (1529, 872) at 10.030 s, went undetected **entirely** for 0.173 s while travelling 171 mm, and re-emerged at 10.228 s as a merged blob 193 mm out at 2281 mm/s — the flag stayed on the phantom until its 3 s coast expired at 13.04 s. Cold replay produced **zero events over the whole 20 s**; live the clock ran down through the stroke (30 → 10.95) and Steven took an extension to avoid a false violation. At the impulse frame every gate of the flag-borne handoff already passed (dwell 9.72 s, still-stamp gap 0.013 s, quiet table, no mover within `carom_reach_mm` of the rest, 193 mm inside `strike_handoff_radius_mm`) — the strike was missed **only** because the flag was on the wrong track. So the cue's remembered rest becomes the scan's **third identity anchor**, beside birth conjunction and a roster ball's own rest, sharing the flag-borne path's open logic verbatim (`_open_from_rest`). Scoping, all from existing constants: the birth-path impulse floor (`newborn_impulse_mms` — the rest vouches for the *place*, nothing vouches for the track; at the flag-borne 110 the scan handed the slot to stick fragments and cost `soft_shot` its real shot), the latched-episode guard, the step gate, and an **ordering test with no constant at all** — the track must have been first seen *after* the flag-holder was last observed, so a stick fragment, a reaching hand or an established ball all pre-date the cue going dark and are refused. Why it is routed here rather than through the birth path: a ball struck out of an address is merged with the stick that struck it (**100 %** merged confirm observations on the real strike), which `newborn_merged_frac_max` correctly reads as a carry — chain of custody from the cue's own rest is what makes the merge admissible, exactly as it is for an ordinary streaking shot. The merged gate being unusable leaves the observation fraction carrying the whole carried-ball judgement, so it must be a measurement and not three lucky frames: measured against the hand class at increasing confirm age, 0.10 s reads a perfect 1.00 on two of three carries and 0.15 s still lets `pickup_newborn_0801`'s 0.92 clear, while at **0.20 s** the real strikes read 1.00/1.00 against 0.64/0.69/0.22 — the first age at which the existing `newborn_confirm_obs_min` (0.9) separates the populations, hence `handoff_confirm_min_s`. A second, threshold-free gate closes the hole the first cut had: the candidate ball must be **leaving** the anchor, its distance from the rest growing by at least `strike_min_travel_mm` between open and confirm (193 → 221 mm on the real strike). The axis check tests direction only, and the player's own stick/hand blob — born the instant the cue goes dark, sweeping along the stroke line, ending inside the handoff radius — holds a near-perfect axis dot while its distance to the anchor *shrinks*; it cannot be vetoed by the carom guard the way other movers are, because there it *is* the candidate and the guard skips a candidate's own stamps. It fired a phantom on the hand until the departure rule went in. Side effect: `scratch_shot_0801`'s strike now comes through this path rather than the birth path, reporting **2008 mm/s against the birth path's 1518** (the `break_underread_0801` failure mode on a clip that was already passing) for 0.09 s of added latency; counts unchanged, golden moved.
- **A fire is evidence too — episode closure and quiet-table guards (added 2026-08-01, checkpoint 33, from the eight-dump evening drill triage):** four structural rules, no new thresholds, each anchored to a frame-verified failure:
  1. **A ShotTaken latches table motion and admits its ball to the roster.** A birth-path strike whose only mover is the fired track itself never latched — the struck ball streaks, so it only becomes judgeable after it stops, when displacement no longer trips the mover criterion — and the settle never came: `lone_roller_0801` froze the live clock in MOTION for 33 s and absorbed the next real shot. If the fire was itself a phantom, stillness returns within a dwell and the settle resolves it — a reset, never a stuck clock.
  2. **A fired episode is never a pickup, and an empty frame is never judged one.** The struck ball's original track dying mid-streak (continuation untracked for a beat) satisfied "every latcher died off-pocket under drag-scale travel" and the discard silently unlatched live shots (`pair_strikes_0801` lost its first settle; the clock froze 14 s); mid-scatter zero-ball frames made every guard vacuously true and unlatched a live break (`break_0801b`).
  3. **A latched, unsettled episode is not a quiet table.** The birth-evidence paths (newborn + own-rest) refuse to open while `table_moving` is latched: mid-scatter the roster movers die in streak churn, the mover clock goes stale, and a scatter ball reborn at 906 mm/s read as an occluded-address strike 2.2 s into `break_0801b`'s live break. Every real birth-path strike in the corpus fires from a settled table.
  4. **A merged, impulse-fast, non-roster track is a hand.** `cue_grab_0801`: a gloved hand crossed at 618–1921 mm/s (merged on every observation, 116 mm from the cue's rest, two frames before the "impulse"), grabbed the resting flag-holder and fired a phantom rack shot through the rest-anchored path — a snatch is impulsive and a drag coherent, so dwell/step/axis/travel all pass. Hand blobs now stamp the carom guard (stamps carry the source id so a struck ball can't veto itself during a handoff), read by the **cue path only**: birth-evidence strikes happen exactly where the player is reaching — the stroke's own merged stick/arm blob stamped 66–72 mm from `lone_roller_0801`'s birth spot moments before impact, and guarding the birth paths with hand stamps vetoed that real strike in replay. Stick tips are too small to merge-flag; a resting touching pair is merged but not fast. **Superseded in part on 2026-08-02 (checkpoint 50) — the distance half of this guard was wrong on the cue path.** The stroke's own gear does not stay behind the bridge: `stroke_gear_veto_0802` and `stroke_gear_margin_0802` measured it stamping **138 mm and 152 mm** from the cue's *rest* spot (not the birth spot), well inside `carom_reach_mm`, and it vetoed both real strikes live — the clock ran down through the stroke. The 271 mm (`medium_shot`) / 340 mm (`power_shot`) figures above do not generalise. Distance cannot be repaired here: `cue_grab_0801` grabs from 116 mm, leaving 22 mm between accept and reject. The separator is **direction**, and it is physical — a stroke drives the cue *away* from the gear behind it (measured 148–162° between the departure and the direction to the stamp), a grab drags it *across toward* the hand (61°). A hand stamp is therefore exempt when that dot product is negative (a 90° boundary, 29° off the grab and 58° off the nearest stroke, no constant to tune), requiring a departure of at least `strike_dir_seed_min_mm` to have a trustworthy direction. Hand stamps only: a genuine carom also sends the cue away from its bringer, so ball movers keep the pure distance rule. Note the tempting alternative was measured and is false — the cue reads `merged=False` on all three clips, the grab included, so "a carried ball is merged every frame" does not discriminate a grab from a stroke at the rest-anchored path. **Also corrected on 2026-08-02 (checkpoint 51): the one-shot-per-rest-cycle disarm on the birth paths had no self-clearing path.** `_newborn_disarmed_at` was cleared only once the *flagged* cue's own translate stamp advanced past it, so with the whiteness vote parked on a ball that never moves again the latch is permanent and both birth paths stay dead for the session — `stroke_gear_margin_0802`'s snapshot carried it set 13 s after the table had settled, and the real strike 25 s later was rejected `newborn-disarmed` before any other gate ran. A `BallsSettled` is the rest cycle the disarm is scoped to and cannot be parked on the wrong ball, so the disarm now clears there too. Same lesson as the v1 carom guard: any latch the player's presence can hold shut must be windowed or episode-scoped, never keyed to one track's state. **The disarm was REMOVED entirely on 2026-08-11 (#37 Part B)** — see the removal note in `_maybe_open_newborn`: its only setter latched table motion in the same statement block, so the latch check directly below it was already an exact superset on two of the three scan paths, and since #33 a second fire inside one latched episode has no clock consequence at all. Corpus flat; the lesson above stands on the v1 carom guard and this one's two live failures.
  Same checkpoint, measured adjustments: `ShotTaken.speed_mms` is now the larger of the fastest observed ball in the candidate window and the confirm's own kinematics (displacement ÷ confirm time) — `break_underread_0801`'s white streaked too fast to be observed at all, read 705 mm/s on a frame-verified rack explosion, and the Hub's IDLE break gate ate the real break; the kinematic bound cannot miss travel the confirm already proved (it remains a lower bound on true peak speed, but journal speeds now read higher than the pre-33 estimator on fast shots — re-measure before citing old margins). The pickup travel ceiling moved 200 → 400 mm (`cue_grab_0801`'s snatch dragged 209.6 mm on-table before lift-off and beat the old ceiling, minting a phantom settle; 400 = 1.9× the new measured drag max, still 4.2× under the shortest measured real-shot travel).
- **Settle-dwell chatter guard — an un-still verdict must persist to erase dwell credit (added 2026-08-01, from `settle_stall_0801`):** since the original settle implementation, a single frame failing the per-ball creep gate wiped the entire accumulated settle dwell (`_all_still_since = None`) — the hysteresis §4.2 calls for ("sustained for a dwell", "so it can't chatter") was one-sided. A corrupt observation stream on ONE parked ball is enough to exploit that: in the flagged clip the cue stick lay across a parked orange ball, the thresholded diff kept only a marginal half-ball crescent (area p50 676 px² vs ~1300+ nominal), and its centroid flipped between two modes 7–10 mm apart (raw deviation p95 10.6 mm vs ≤ 0.6 mm for every other parked ball) — supra-gate excursions every ~0.2–0.3 s, each wiping the dwell, so a clean 0.5 s window essentially never occurred: the cold replay produced NO settle in 10 s of physical rest, and live settles became a lottery (13.7 s on the flagged shot; 12.0–16.3 s on 10 of that session's last shots; the same mechanism is the long tail in every session's shot→settle latencies — 17.9/18.4 s, 17.3/50.0/47.6 s, 17.0/16.9 s — and checkpoint 32's "unexplained 9 s late settle"). Measured discriminator: corrupt excursions persist ≤ 64 ms (40+ runs on the clip's parked phase), while genuine motion holds the un-still verdict *continuously* — ≥ 371 ms across the corpus (`hanger_pot_0801`: pre-shot adjustment 371 ms, strike 794 ms, reborn terminal roller 1434 ms unbroken; `slow_creep`: one 3469 ms run whose end + dwell is exactly the settle time). The rule: an un-still verdict only erases dwell credit once it has persisted for a full `moving_window_s` (the existing displacement-evidence window — 3.1× the worst corrupt run, 1.9× under the shortest genuine one; no new constant), and BallsSettled still fires only on a still-verdict frame with full credit — so a genuine mover blocks settle from its very first frame exactly as before (the hanger-pot young-roller protection is untouched), and the persistence window only decides whether credit survives a transient. Zero-ball frames keep the hard reset (a mid-scatter detection desert is not stillness evidence). Known residual, BACKLOG'd: `_last_translate` still refreshes on corrupt excursions (a chattering ball defers pickup judgment and cannot open own-rest strikes); no live failure observed. **The residual materialized the same night (2026-08-02 02:51 UTC, `address_chatter_miss_0801`, checkpoint 38):** address chatter kept a rested replacement cue's translate stamp current, so the own-rest strike path read ~0 dwell against a physical 13 s rest and a real 1396 mm/s stroke went unseen (live and cold; the whiteness vote was parked on another white-ish ball and recovered only 2 s post-impulse). Three rules close it, all from existing constants: (1) translate stamps land immediately but a creep excursion that CLOSES within `moving_window_s` with net travel under `moving_enter_mm` was chatter, and its stamps are revised away (a hand ramp or tip bump ends displaced or holds longer — its stamps stand); (2) when the stamp-dwell is still broken at an impulse, the ball's own history may vouch for the rest — every observed position through the pre-strike window inside `moving_enter_mm` of the rest spot AND no unobserved run longer than `moving_window_s` (a push rides the ball out of sight — 666 ms unobserved on `tip_bump`, whose emergence frame is stored AT the rest spot by coast smoothing, which is why the gate trusts observation timing, never reported positions; a strike's stick passes through contact — worst gap 77 ms with chatter flicker); (3) a strike candidate that can no longer reach `strike_confirm_displacement_mm` in its remaining window (displacement + raw × time left) dies immediately instead of lingering at rest where a stick-merge centroid jump can fake the distance. `tip_bump` re-measured while pinning: its 3.5 s event pushes the cue 203 mm at ~600 mm/s emerging from occlusion — kinematically a soft shot, separable only by the witnessed-dwell rule (or, someday, the acoustic corroborator).

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
│       ├── capture/             # pypylon grab loop, ring buffer, camera config/UserSet,
│       │                        #   clip container, offline detection cache
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
- **Purity rule (the one dogma worth keeping from the old repo):** the vision core takes `(frame, timestamp)` and returns events/state — no I/O, no clocks, no threads — so the clip harness stays deterministic. `VisionPipeline.process()` is split into `detect()` (frame-bound, expensive, stable) and `advance()` (state-bound, cheap, the part under active tuning); both stay pure. Anything that persists the seam between them — the corpus battery's detection cache — lives in `capture/`, never in `vision/`.
- **`ClipReader` reads with `os.pread`, never `np.memmap`** (2026-08-02). Mapped reads of the exFAT corpus drive fault in at 4 KB granularity with no readahead: 207 MB/s against 958 MB/s for whole-frame `pread`, which was ~65% of the battery's per-frame cost. Do not "simplify" it back to a mapping.
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

- **2026-08-02 — A shaft/tip bump is a foul, and a foul resets the clock.**
  Reverses the 2026-07-27 "no game-state awareness / v1 detects shots, not
  fouls" decision above, which is superseded on this point only (the wider
  rule — no rules engine in v1 — still holds).

  That decision was right that a legal ball-in-hand shaft nudge and a foul
  shaft bump are the same pixels. It stopped one step short of the *clock
  outcomes*:

  | Event | Correct clock outcome |
  |---|---|
  | Soft legal shot | **reset** |
  | Foul shaft/tip bump | **reset** — the visit ends |
  | Legal ball-in-hand nudge | keep running — still your visit |

  Two of the three reset, so the detector never needed to tell a soft shot
  from a foul: they do the same thing. Only legal ball-in-hand placement
  differs, and that is not spontaneous — it follows a *called* foul, which is
  already human input.

  **Ball-in-hand stays keyless (Steven's call): fire on both and eat the rare
  wrong reset.** In solo practice ball-in-hand is uncommon and a wrong reset
  is recoverable. Do not add a ball-in-hand key speculatively — if those
  phantom resets turn out to be annoying in real sessions, that annoyance is
  the evidence that would justify building one.

  **What it cost in code** (checkpoint 54): the two constants that made
  sub-impulse touch shots invisible were both anchored on this rule, not on
  any measurement of real shots — `strike_impulse_mms` 250 sat just above
  `soft_touch_0731`'s 225 mm/s peak, and `strike_confirm_displacement_mm` 60
  existed to reject `shaft_adjust`'s ~59 mm. **#5 was never mic-blocked; it
  was policy-blocked by a corpus expectation.**

  Two measurements from that work are worth carrying forward:

  1. **Speed does not separate a shot from a provoker.** `practice_strokes`'
     stick fragment peaks at 1576 mm/s — faster than every true event in the
     sub-impulse class (`tip_bump` 124, `soft_touch_0731` 225). The impulse
     floor only decides which candidates *open*; the confirm displacement is
     what rejects them.
  2. **60 mm was load-bearing for an undocumented second reason** — it forced
     the speed measurement to span enough real travel. `ShotTaken.speed_mms`
     is max(fastest observed, displacement / candidate lifetime), and an early
     confirm truncates both terms: dropping it outright made `break_0801`
     report 2068 mm/s instead of 7371, under `MATCH_BREAK_FLOOR_MMS`, which
     would hand the IDLE gate the exact live failure checkpoint 45 fixed.
     Hence the two-stage confirm — 60 mm at any time, or 22 mm once the window
     has run — which leaves every ordinary shot firing on the same frame at
     the same speed.

- **2026-08-11 — Moving the cue ball *by hand* does not reset the clock (#41).**
  Narrows the 2026-08-02 "eat the rare wrong reset" above; it does not reverse
  it. Steven, after the first session that measured the cost (five carries in
  80 shots, one of them three counted shots in 3.6 s for one real stroke):
  *"Adjusting the cue ball with a human hand shouldn't reset the clock… I need
  you to identify a way to make ball in hand NOT look like movement. In my
  opinion an arm / a hand does not look like a cue."*

  What separates them is **not** that a hand is near the ball — during a
  stroke the bridge hand and forearm are on the cloth beside it, and every
  presence-shaped axis #38 measured overlaps because of that. It is **where
  the ball ends up**: a struck ball leaves the hand within a few frames and
  comes to rest on open cloth, while a carried ball comes to rest inside where
  the hand was, because the hand is what took it there. So the settle asks,
  of every ball that arrived somewhere new, whether a hand-scale intrusion
  (§4.1) was covering that spot at the moment it arrived — and refuses the
  shot only when **all** of them were. A real stroke always leaves at least
  one ball resting where no hand is, which is what keeps the fail-safe
  direction from flipping.

  **What this does and does not settle.** It settles the *hand* case, which
  is the one Steven actually hits. It does not settle the shaft case: a legal
  ball-in-hand nudge with the cue tip and a foul shaft bump are still the same
  pixels and both still reset, exactly as the 2026-08-02 decision says. Both
  arrive without a hand covering the spot, so neither is refused here. The
  keyless call stands and no ball-in-hand key is added.

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
  settling into aim (a foul in play). **Since 2026-08-02 this must fire** — a
  foul ends the visit, so the clock resets exactly as for a shot. See the
  dated foul-reversal decision in §8; the parenthetical this line used to
  carry ("must not count as the shot — v1 detects shots, not fouls") is the
  rule that was reversed.
- `tip_bump` · 8 s — bump the cue ball with the **tip** while feathering, so it
  rolls an inch or two. Was **the hardest case in the corpus** while it had to
  be told apart from a soft shot — dwell + small impulse along the cue line is
  nearly a real soft shot's signature. The foul reversal dissolved the problem
  rather than solving it: both are fouls-or-shots that reset the clock, so
  **this must now fire too**, and no acoustic corroborator is needed to split
  the pair (that idea is rejected — `hardware.md` Part D)
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
- New failure captures come from the Stream Deck Flag keys (`POST /api/dump`;
  reason-coded labels since 2026-08-01) — see `hardware.md` (Stream Deck part)
  for the triage flow.

## 10. The bracket (issue #43, 2026-08-13)

Double elimination as **optional state layered on top of the score already
kept**. The API surface and the payload shape are in §2.1; this section records
the decisions behind the draw itself, because they are the ones that are
expensive to get wrong and invisible when they are.

The structure is a pure function of the field size (`server/bracket.py`), so a
saved bracket stores only the draw and the results — regenerating the rest is
what stops a persisted tournament and the generator from ever disagreeing.

**The cross-drop is load-bearing.** Where the A side's second round drops its
losers onto the B side, the incoming order is *reversed* against the B-side
matches waiting for them, so a player never meets the half of the draw they
just came from. Drop them straight down instead and the bracket still runs — it
quietly replays first-round pairings, which is exactly the kind of bug nobody
notices during a party. Pinned by a test that walks the reference graph and
proves the two branches feeding each first-drop match descend from disjoint
halves of Round 1 (further down the B side the field is small enough that
everyone can meet everyone, and no drop rule changes that).

**Match 2B−1 is a second final, never a "bracket reset"** — that is esports
vocabulary and does not belong here. It is played only if the B-side player
wins the final: the A-side finalist arrives undefeated, so losing there is
their first loss and the two play again for the title. It is drawn from the
moment the bracket is built, dashed to mark that it is conditional, rather than
materialised when it becomes necessary.

**Seeding is a mode, and the default is the opposite of standard.** Standard
pulls the top seeds apart (1v8, 4v5) so they meet last; **adjacent** pairs 1v2,
3v4, 5v6 so every early match is close. On the reference field the mean rating
gap is 50 adjacent against 187 standard, where three of four standard openers
are blowouts — for a family event that is the whole point. The cost, stated
where the mode is implemented and accepted deliberately: the two strongest
players meet in Round 1, so one of them spends the day on the B side and the A
final is likely weaker than the B final.

### 10.1 The four questions #43 left open (resolved with Steven, 2026-08-12)

1. **Byes go to the top seeds**, in both seeding modes. Under adjacent seeding
   that needs saying, because a bye rule and an adjacent draw can disagree about
   who sits out: seeds 1 and 2 are *paired* by the seeding rule but cannot both
   sit out of the same match. So byes are placed first, into the bracket
   positions the standard order reserves for the top seeds (which spreads them
   across the halves), and only the remaining players are paired adjacently. The
   leftover pairs are assigned strongest-first to the matches feeding the
   strongest byes, so Round 2 stays as close as the byes allow.
2. **The handicap chart is configurable** — off / mild / medium / hot — rather
   than one column chosen once. The model is FargoRate's own: game odds are
   `1/(1 + 2^(−diff/100))`, match odds over a race are the exact negative
   binomial, and a race is picked off a ladder that shortens the underdog's
   number and lengthens the favourite's in turn (5-5, 5-4, 6-4, 6-3, 7-3, 7-2).
   Each mode has a **target** — the underdog's chance of winning the match — and
   50% is a hard ceiling in all of them, per FargoRate's stated rule of the
   closest match-up to the race you asked for "without giving the lower skill
   player an advantage". The target is what stops the rule degenerating into
   "always take the biggest legal spot", which at a race to five hands a
   68-point gap the same 6-4 as a 100-point one. The ladder's end is capped near
   a third above the base race, which reproduces every published endpoint on
   record: their base-3 league ladder ends at 5-2, their Fair Match race-to-5
   column at 6-4 (and a 563 against a 240 at 7-2 — the example in the issue),
   and their race-to-11 hot column at 14-8. Both finals play the A-side length:
   the A-side finalist has not lost yet, and shortening the title match because
   the B side's races are shorter would be the wrong answer.
3. **Field ceiling 16.** The page computes its own geometry from the field
   (`sizeStage()`), so eight draws like the mockup and sixteen packs tighter —
   smaller boxes, narrower columns, a thinner connector channel — and the page
   never scrolls at either size. Sixteen is where the A side needs five columns
   plus both finals; beyond it the names would have to go.
4. **A correction cascades over unplayed matches and is refused behind a played
   one.** Re-deriving downstream slots is free while they are empty, so fixing a
   score just re-runs them. Behind a played match the edit is refused *by name*
   (`Match 5 has already been played — clear its result first`), and clearing
   that result is its own deliberate act — so a correction is walked back one
   match at a time and no real result is ever silently erased. A walkover is not
   a result and cannot be cleared: the bye is a fact about the draw.

### 10.2 State that outlives the wall (2026-08-13, live iteration)

The first pass made `MatchScore` the single source of truth for what is on the
TV, which is right and stays. But it is a *single slot*, and the corollary went
unnoticed: anything not currently loaded had nowhere to live, so the system's
answer to "I need to do something else for a minute" was to destroy what was
there. Four things followed from that, and all four are fixed here.

**A hand-set race target is an override, not a value.** `set_targets` used to
write straight into `Match.targets`, which every resolve recomputes from the
handicap chart — so an override held until the next result landed anywhere in
the draw and then silently reverted. Worse on the loaded match, where the old
number had already been pushed to `MatchScore`: the wall and the bracket then
disagreed, which is the exact failure the single-source rule exists to prevent.
Overrides are now stored beside the pairing they were chosen for and reapplied
after the chart, and they are dropped if different players end up in the box.

**An interrupted match is parked, not discarded.** Loading a match used to call
`reset_scores()` unconditionally, and the Switch match list offered the *live*
match as a tappable row — on a page passed round the room with no auth. One tap
set the wall to 0-0 with no confirmation. The outgoing score is now parked on
its own match (`Bracket.park`), restored on reload, and shown as a distinct
`paused` status; the live row is no longer tappable, because the only thing
tapping it could do is reload what is already up.

**Clearing a result keeps its score.** Clearing says "this is wrong", not "this
never happened", and the reason to clear Match 5 is usually to fix something
behind it — after which 5 must be replayed from a scoreline somebody would
otherwise re-key from memory, eight presses at a time, with the wall counting up
through every one. Scores are kept and tied to the pairing that made them, so an
upstream correction that changes who is in the box still throws them away.

**Clearing a bracket archives it first.** `DELETE /api/bracket` unlinked the
file while Save & archive sat behind a button nobody had to remember, which made
the destructive path the cheap one. Anything contested is written beside the
journals on the way out and the filename comes back in the response.

**Withdrawal is a bye that happens mid-tournament.** Kids, bedtime, a drive
home: there was no honest way to advance an opponent, only plussing them up to
their target and confirming — recording a match nobody played. A withdrawal
concedes every *unplayed* match of theirs down both sides of the draw, invents
no score, and is expressed in `_auto_advance` alongside byes so it needs no
separate routing. It is **derived and never stored** (`_forget_walkovers`
throws every walkover away and re-derives it each resolve, and `state_dict`
omits them), which buys two things: reinstating somebody who came back is one
press rather than an unwind, and it survives a restart as the player's state
rather than as a written-down result. Matches they had already played are
untouched in both directions — a win does not become a forfeit against them,
and a loss stays a loss on its real scoreline.

**The base races can change mid-tournament** (`PATCH /api/bracket`) — eight
players is fourteen matches on one table, family events run long, and the first
thing anyone asks for at 9pm is shorter races. Only unplayed matches move: a
contested result keeps the race it was played under, because a 5-3 means "first
to five" and rewriting that would make the record lie about a match that is
over. Hand-set overrides are dropped, having been chosen against the base being
replaced.

New journal kinds, strictly additive as ever: `bracket_withdrawal`,
`bracket_races_changed`. `bracket_cleared` gains `archived` (the path, or null).

### 10.3 The night before the tournament (2026-08-13)

Three of the items #43 deliberately parked for evidence from a real event,
built on the eve of one, plus the fourth settled as a decision rather than as
code.

**A concession is a withdrawal narrowed to one match.** Somebody gives up a
single match — had enough of the B side, wants to be at the table for the final
— and keeps everything else. Withdraw-then-reinstate was the standing answer
and is two presses, but it concedes every *other* unplayed match of theirs on
the way through, so the two are only equivalent when nothing else of theirs is
waiting, which on the B side is precisely when it is not. `Bracket.concede`
stores **who gave the match up**, never the win it produces, and expresses the
walkover in `_auto_advance` beside byes and withdrawals — one rule down the
chain, no second way for the draw to advance somebody. It is the **one walkover
written to disk**, because nothing else in the draw implies it (a bye follows
from the positions, a forfeit-by-withdrawal from `Player.withdrawn`); the
derived half is still derived, so it stays as reversible as the others. Two
rules fall out and are pinned by tests: **a withdrawal outranks a concession**
in the same match, because somebody who has gone home cannot be handed a win —
the match below would then wait on a player who is not in the room; and a
concession is invalidated by an upstream correction like any other entry made
about two specific people. It is undone by taking it back, not by clearing a
result, since it never was one, and both directions are refused behind a played
match.

**The champion plate was built and taken out again**, one day apart. A finished
tournament said `Champion · Name` in the corner of the bracket page, which is
the right weight for a fact and the wrong one for the thing the evening ends on
— so it became a plate over the whole page, the draw ghosted behind it. At the
table it did not show, and rather than debug something nobody had asked for
twice it went and the header line came back. The wall after a final is the draw
itself, with the final's winner row already reading as won. **The finding worth
keeping is the one from building it**: the first version carried a fade-and-rise
and rendered as *nothing at all*, because an OBS browser source in a scene that
is not on program paints nothing, so the animation never starts and
`animation-fill-mode: both` holds the element at its `from` keyframe — opacity
zero — for as long as that lasts. Same class of bug as the
`requestAnimationFrame` connector caught in checkpoint 92, and the same rule
follows: **these pages must be correct in a source that has never painted**,
because that is their state every time the scene is cut to.

**The LAN address goes on the wall** (`GET /api/lan`, `server/lan.py`). Scoring
from phones needs no install and no auth by design, which leaves one obstacle:
the tournament director reciting an IP address to eleven relatives one at a
time. The bracket page carries a QR of `/control` in its header while a
tournament is live, and the Bracket tab carries a larger one for handing a phone
to whoever is next to you. The page cannot derive this itself — OBS loads it
over loopback, so `location.host` is `127.0.0.1` — so the host is discovered
server-side (a connected UDP socket transmits nothing; it just asks the routing
table which interface would carry the traffic) while the port comes off the
request, being the same on every interface. `url: null` when the machine has no
LAN address, and the pages say so rather than showing a code for an address
nobody can reach.

**The confirm key's scene flip takes itself back down** (`bracket.DWELL_S`, 30 s,
with an early return the moment the clock leaves IDLE). As first built, the flip
*out* was automatic and the flip *back* waited on somebody pressing the scene
key — and the bracket scene carries no shot clock and no table, so the wall
could sit on a bracket through a whole live match, missing the one thing the rig
exists to put on that TV. The flip itself is right: the instant a result lands
is when the bracket has information everybody wants, and during racking there is
nothing to watch on the play scene. It is a **beat, not a mode**, so it is
bounded — thirty seconds reads as who advanced and who is up next, and is still
shorter than any rack, so the TV is back watching them rack before anybody
breaks. A showing that a *person* chose (the scene key) carries no deadline and
never expires: a timer must not second-guess a decision. A server that has
stopped answering reads as IDLE, so a dead poll can only leave the bracket up
for the rest of its dwell, never pull it down early.

**A result that nobody pressed a key for flips the wall too.** The scene flip
started out inside the confirm key, so a match conceded from a phone left the TV
on the play scene — the same event, told two different ways depending on which
surface reported it. The deck watches the draw's count of **decided matches**
instead, and does the same thing for any of them. That count rather than
`revision`, deliberately: revision also moves for loads, edits and race changes,
and a wall that flips every time somebody taps a stepper is a wall nobody can
play under. The first count a loop sees is never news, so a deck restarted
mid-event does not flip over results that were already in the draw.

**Late entry is a hard cutoff, and that is the decision.** The field freezes at
Build; somebody who arrives after it waits for the next bracket. The tempting
alternative is to drop a latecomer into an unplayed bye slot, but a bye is not a
parking space — it has already advanced somebody, so filling it un-resolves a
walkover, and once that player has played their next match the insert collides
head-on with the rule that refuses corrections behind a played match. The
window in which it is safe is the window that closes fastest at a real event,
and a feature that works at 6:05 and refuses at 6:35 is worse at a party than
one that never worked: the second is a rule, the first is a bug with a
schedule. Revisit with evidence if it actually bites.

## 11. The minigames platform (issue #46, 2026-08-20)

A **games** session mode, fourth on the mode axis, hosting on-table minigames.
Launched with two: a **shot catalog** (capture practice shots as layouts plus
target zones, replay them with guided setup and PASS/FAIL judging) and a **lag
trainer** (settle → distance from the head rail, best of the session). The
surfaces are agreed in `docs/mockups/games-v1.html`; where a shipped page and
that file come to differ, the shipped page is right.

**The contract is frozen to exactly what those two games need** — the rule of
two. A minigame is a registered event consumer, the same registry pattern
`RULESETS` uses: plain code in the repo, no plugin loading and no config-file
game definitions.

- **Receives** the event stream (`ShotTaken` / `BallsSettled` / `Scratch`), the
  resting layout with the cue crown, and a 10 Hz tick for anything that has to
  move between events (the placement countdown, the auto-arm).
- **Provides** its deck verbs, the HUD bar state for the TV, the marks it wants
  on the cloth, and namespaced journal kinds.
- **One active game at a time**, owned by the Hub. Entering games mode shows the
  picker; leaving it ends the game. **No parking** — and nothing is lost by
  that, because a game's record is in the journal, which is also what lets
  `cueclock report` derive make % without the games layer keeping a score of
  its own.

**The vision core is untouched by construction.** Games sit at the Hub seam the
clock and `last_settle_movers` already use, so the corpus stays green without
anything in `vision/` moving. Games are fed *after* the vision kill switch (a
vision the operator has disowned must not drive a game either) and *before* the
mode gate, which exists to keep events off the **clock**.

### 11.1 Where the drawing happens, and why it is split

**Everything on the cloth is drawn server-side into the display frame**;
everything in the HUD bar is the `/games` browser source. The display pipeline
already owns the table image and the mm→display mapping (`--rectify` composes
the homography into the remap), so orientation and scale are correct for free
and OBS stays dumb. The rejected alternative — a browser overlay aligned on top
of the Syphon feed — needs an exported mm→display-px mapping that must survive
every recalibration: a standing alignment liability, in exchange for nothing.

The cost is stated rather than hidden: **without `--rectify` there are no
on-cloth markings at all**, because a plain undistort has no mm→px mapping and
there is nowhere honest to draw a zone. `run` says so at startup; the bar still
works.

On-cloth palette is a role code, not decoration: **white** is the cue ball's
markings, **slate dashed** an object ball's, and the one **alarm red** is the
miss line. Accent blue never touches the cloth — on blue cloth it is invisible
— so accent and alarm live in the bar, where the answer cell escalates by area
exactly as every other overlay does. Games mode has no clock, so a FAIL can
never collide with an expiry alarm.

### 11.2 Zones are diamond-grid cells, and pass criteria are a list

Zones are rectangular blocks of **diamond-grid cells** — the standard layout's
8×4 division of the playing surface — derived entirely from the calibrated
homography, with no invented geometry constant. Circles were the first design
and lost on one argument: **a grid is aimable from the table.** Sighting
diamond-to-diamond is how players already carve the table up, so screen and
cloth share a vocabulary, a miss becomes legible ("one cell short") as well as
quantitative ("214 mm"), and capture becomes single-press — the cell containing
the placed ball is the default target. Each zone is a typed object
(`{"type": "cells", …}`) so a future zone type is an addition, not a migration.

A shot's pass criteria is a **list of (ball role → zone) requirements**, judged
with one point-in-rectangle primitive per entry, never a hardcoded cue rule.
The list schema goes in on day one even while only one entry is ever populated:
retrofitting a list under a hardcoded judge later is pointless churn. Every
shot requires the cue in its zone; a **safety** adds the object ball in its own.
Failure semantics then fall out — a scratch fails the cue's requirement, and an
object ball potted during a safety attempt has left its zone and fails its own.

**How each role is identified at settle — three tiers, weakest last:**

1. **The cue is judged against the whiteness crown at rest, never against track
   lineage.** The EMA freezes while a ball flies and re-converges within ~0.3 s
   at rest, so whatever the tracks did mid-collision, "which resting ball is the
   cue" is re-derived visually — the same machinery scratch adjudication trusts.
   Cue verdicts are therefore immune to mid-shot label swaps.
2. **One object ball — the common safety drill — is identified by elimination**:
   the resting ball not wearing the crown. No lineage, zero identity risk.
3. **Two or more object balls fall back to track lineage**, and the exposure is
   narrower than it looks: a cue↔object swap is always corrected by tier 1, so
   the only corruptible case is two *object* balls passing within merge distance
   mid-shot. Recorded so nobody is surprised; a swapped verdict in live use is a
   Flag-and-triage item like any other detection defect. **No 16-ball identity
   anywhere** — which is why the bar's per-ball lockups read `CUE` / `OBJECT` /
   `BALL n` rather than the mockup's `RED` / `BLACK`: a colour name is an
   identity claim this system deliberately cannot make.

### 11.3 The two constants, and their anchors

Per the no-blind-thresholds rule both are asserted from measurements already in
the repo (`EventConfig`, `vision/events.py`), with the anchor stated in code:

- **Ghost placement tolerance — 15 mm.** Bounded below by
  `settle_creep_mm = 5.0` (under it a resting ball's own wobble would flip the
  verdict) and above by `moving_enter_mm = 30.0` (at or over it, a ball the
  event layer would score as *moved from* the ghost would still read as
  standing on it). 3× the creep, half the motion threshold.
- **Cell edge margin — 5.0 mm**, straight off `settle_creep_mm`: a centre
  within a resting ball's own wobble of a boundary genuinely cannot be assigned
  to one side of it. Applied **outward**, so the unresolvable chalk-line case
  fails toward giving the shot to the player — losing a shot you made to a
  measurement nobody can take is the worse direction.

### 11.4 The deck and the wall

The picker is **the deck's row 3** — the row that is the mode's own block in
every other mode — one game per key, and the selected game's verbs replace the
picker on the same row. **Two levels maximum: picker → game.** No cursor
navigation (arrow keys were considered and dropped): the screen is the brochure
and the deck is the picker, so a choice is one press with nothing to read.
Capture is a row-3 transient overlay, the mechanism Flag's reason page already
uses. All games keys wear the **MATCH stripe** — the stripe channel answers
"how much time do I have", and every games press is a between-shots press.

**PLAY and NEXT are one key with two faces** (the Replay-key precedent — the
face says which press this is). RETRY is its own key, dark until a shot has been
served: re-serving the *same* shot and serving *another* must never be one
mispress apart.

OBS gets **one Games scene**: the overhead feed full-bleed plus one full-canvas
`/games` source, and nothing else. Adding a future game touches zero OBS
configuration. The scene follows the Mode key with no dwell and no scene key of
its own — games mode is a thing you are *in*, and the mode **is** the choice.
