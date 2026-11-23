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
| `state.py` `ClockState` | `IDLE → RUNNING → MOTION (shot in progress) → RUNNING (reset to full) → GRACE (2 s after 0) → EXPIRED (flash) → auto-reset for next player`. Extension allowed in RUNNING/GRACE only; adds `extension_seconds`; re-arms urgency cues; respects `extensions_per_frame`. Edge-triggered audio cues `ten`/`five`/`expired`, fired exactly once per countdown. Amended 2026-08-01 (checkpoint 26): **a settle in IDLE is a no-op** — IDLE means the operator said hold (New Rack, pause), and racking produces genuine settles that must not start the countdown; the untimed break still enters via ShotTaken (IDLE→MOTION). And **a settle with no ShotTaken since the previous settle counts the shot anyway** — balls moved and came to rest, so a missed strike (or a sub-impulse touch shot) no longer leaves the rack shot count short and replays the WNT 60 s window. Trade-offs accepted: a missed *break* now leaves the clock IDLE (visible; Start N recovers), and a phantom settle inside the first-shot window burns it (Push recovers). Amended 2026-08-01 (checkpoint 28): **in IDLE, a ShotTaken below a break-speed floor is ignored** (3000 mm/s; Hub-side until checkpoint 45, now a field of the `match` mode — `clock/modes.py`). `ShotTaken.speed_mms` is the fastest *observed* ball in the strike-candidate window — the candidate target's own raw underreads on handoff strikes (live lesson, same morning: the gate ate a real break whose target remnant read 2072 mm/s while the reborn cue flew 5300+; `break_0801` pins the fix). Measured with these semantics: breaks 5845–7361 mm/s vs. every ordinary shot ≤ 2868; racking/placement lookalikes below 1000. The one live rack-gather phantom started the clock 19 s early and swallowed the real break; out of a fresh rack the only legal shot *is* a break. Trade-offs: a feather-soft break stays IDLE (Start N, same as a missed break). Events without a speed (manual/synthetic) pass. Amended 2026-08-02 (checkpoint 45): the floor is a **`match`** rule — practice that starts without a break belongs in `run_out` mode, which has no floor because it has no break to gate. Amended 2026-08-05 (checkpoint 69): "Start N recovers" now recovers the *rack*, not just the timer — pressed in a rack-scoped mode with no shot yet counted, `restart()` **counts the break** (`_rack_shots = 1`). The two trade-offs above both end with a missed break and a press, and neither incremented the shot count, so the rack ran one short and the 60 s window replayed on the shot after the press (live, twice: 2026-08-05 19:59 and 2026-08-06 01:53). The press is the operator saying the break is behind us — out of a fresh rack there is no other shot it could mean, and the countdown it hands out is unchanged (`current_base()` still reads 60 at one shot). Amended 2026-08-10 (checkpoint 83, #33): **the shot became a settle-time decision.** A RUNNING countdown freezes provisionally at the engine's motion-episode latch (~0.25 s after the stroke; the hardened roster-mover latch — the raw E9 motion bit still cannot freeze RUNNING), a RUNNING ShotTaken defers its count to the settle, and the settle adjudicates the whole episode with hub-side evidence policy: exactly one shot per episode, fired or compensated (lateness within the episode is unbounded), unless the evidence is positively placement-shaped — rearrangement `moved==0, involved==1`, no surviving episode mover ≥ `moving_enter_mm`, no scratch candidate anchored inside the episode — in which case fire and freeze revert together: nothing counted, remaining restored exactly. The engine's pickup discard reverts the same way. GRACE and IDLE fires stay immediate; every ambiguity fails open to counting (the checkpoint 26 compensation is the default verdict, now merely refusable on measured placement evidence). Amended 2026-11-17 (checkpoint 134, #68): **the buzzer sounds at zero.** RUNNING→GRACE emits the one-shot cue `buzzer` (it outranks an urgency cue crossed in the same tick), and the soft `expired` chime 2 s later is gone, because time is up the instant the wall reads 0 and a player down on a shot must hear it then. GRACE is now only the window for a stroke made *before* zero to be confirmed after it, `grace_seconds` = 1 s in every ruleset (`rulesets.GRACE_SECONDS`): the strike confirm lands a median 0.02 s after the motion latch (p99 0.22 s, max 1.18 s over 872 journaled strikes), and the latch is ~0.25 s after the stroke, so 1 s is about twice the p99. And **nothing holds the clock on zero**: the E9 expiry hold (2026-08-06, `CHANGELOG.md`), which re-armed GRACE while the motion bit was set, is removed and `tick` no longer takes `motion`. Measured over all 50 zeros in the journals: the hold saved none (no zero ever ended in a settle) and held the clock on zero for 3.7–40.6 s eleven times, twice then accepting a shot made about 20 s after zero. Since checkpoint 83 the motion latch freezes the countdown before zero on a missed strike, which is the case the hold was built for. A shot that flies dark through zero (no latch, no strike: 12 of 1060 counted settles since checkpoint 83) still gets its fresh countdown when it settles in EXPIRED or in the restarted countdown; the cost is the buzzer sounding through it. Auto-restart 2 s after expiry is unchanged (Steven's call). |
| `Ruleset` config | `ultimate_pool_8ball` (30 s base / 1×30 s extension / 2 s grace / 2 s post-expiry), `ultimate_pool_8ball_final10` (15 s variant), `ultimate_9ball`. Values carry over verbatim. Amended 2026-11-17 (checkpoint 134, #68): grace is 1 s in every ruleset, the measured strike-confirm window after the buzzer at zero (see `state.py` above). Amended 2026-11-22 (checkpoint 145, #74): **rulesets are gone**; the clock runs on six independent settings (`clock/settings.py`: game, shot, after_break, extension, push, clock), see §22. The old names survive only as `LEGACY_RULESETS`, for replaying logs that recorded them. |
| HTTP API | `POST /api/{start,pause,reset,extension}`, `POST /api/ruleset/<name>`, `GET /api/{state,rulesets}` — this is the **Stream Deck contract**; keep the URLs stable so the Stream Deck buttons keep working. Added 2026-07-29, removed 2026-11-15 (checkpoint 114, #55): `POST /api/dump?label=<x>`, which saved `cueclock run`'s in-RAM ring of raw frames as a labeled corpus clip; see §4.4. Added 2026-07-31 (WNT match mode): `POST /api/rack` (per-rack reset: shot count + extension pool, IDLE at the first-shot time), `POST /api/restart` (fresh countdown, RUNNING immediately — missed-auto-reset recovery), `POST /api/push` (push out called: re-enter the WNT §5 60 s first-shot window; press again on a pass-back), `GET /api/score`, `POST /api/score/{p1\|p2}/{increment\|decrement}` (decrement floors at 0; unknown player/op → 404), `POST /api/score/reset` (0–0, names/race kept), `POST /api/score/config` (partial JSON body `{p1_name, p2_name, race_to}`; `race_to: null` clears), `POST /api/vision/{on,off}` (kill switch: while off, vision events don't move the clock; operator commands still work). Added 2026-08-01 (warm replay): each ring dump also carries `state.pkl` + `state.json` — the oldest of `run`'s ~1 Hz rolling vision-core state snapshots that falls inside the ring window (whole-object pickle of tracker + event engine, deliberately schema-free so new engine state rides along; measured 2.33 MB / ~1 ms per snapshot on real mid-shot state). `analyze --warm` restores it, skips the frames it already contains, and replays the rest at session-relative timestamps — reproducing exactly what the live session computed, accumulated state included (whiteness-vote position, roster, episode latches: the divergence class a cold replay can never show). `state.json` stamps a schema version + a fingerprint of the state-bearing vision sources so a stale pickle fails loudly, never silently. Session-journal contract, same date (strictly additive — readers must ignore unknown kinds/fields): vision-event records add `frame_t`, the confirming frame's *capture* timestamp (camera clock, session-relative; unambiguous vs. the record's emit-time `mono` stamp — `t` keeps the same value for old readers), and a `detect_heartbeat` record every ~5 s carries `hz` (effective detect rate), `frame_age_s` (staleness of the newest processed frame), and its `frame_t` (the camera→journal clock anchor). The heartbeat is emitted from the Hub tick loop, which keeps beating while the camera thread is starved — so dump-write starvation of the detect loop is measurable from the journal alone. `flag_dump` records gain `warm` (whether the dump carries a snapshot). Added 2026-08-02 (session modes): `POST /api/mode/<name>` and `GET /api/modes` — modes are `match` / `run_out` / `no_clock` (`clock/modes.py`), an axis **orthogonal to the ruleset**: a ruleset is a rulebook, a mode is what you're doing at the table. Only `match` is rack-scoped (first-shot allowance) and only `match` applies the IDLE break-gate floor; `no_clock` parks the countdown *and* stops vision driving it in one command, journaled as one `cmd_mode` intent, with events still recorded as `vision_shot_taken_no_clock` / `vision_balls_settled_no_clock` so drills stay evidence. Mode is independent of the `/api/vision/{on,off}` kill switch — neither gate being open reopens the other. Added 2026-08-02 (instant replay): `GET /api/last_shot` → `{"age_s": <float>|null}`, seconds since the last ShotTaken — the anchor the Stream Deck's Replay key seeks the OBS buffer to (`hardware.md` §C4a). Its own endpoint rather than a field of `/api/state` because the deck polls state every 2 s and this answer is only worth having at the instant of the press. Deliberately an *age*, not a timestamp: the deck is a separate process whose `monotonic()` shares no epoch with the server's. The anchor is stamped in `Hub.shot_taken` **before every gate** — kill switch, mode, IDLE break floor — because those decide whether a shot moves the clock while replay asks what is on the video; that is what makes the seek work in `no_clock`, where drills happen. Accepted cost: a phantom shot anchors a phantom replay. Also added: the `/replay` page (REPLAY badge for the OBS Replay scene — scrim, wordmark, drain bar; `?s=<seconds>` sets the drain rate). Amended 2026-08-02 (live validation of checkpoint 46): the badge also takes `?mask=<x,y,w,h;…>`, opaque rectangles in **percent of the canvas** covering wherever the live scene drew the shot clock — the buffer records program output, so that countdown is in the recorded pixels and can only be covered, never switched off. The deck measures the rects from OBS itself at each press (enabled browser items whose URL contains `/shotclock`), which is what keeps a patch from drifting off the thing it hides. Percent rather than pixels because OBS may record a rescaled output of the canvas the transforms were read in. The page also fades to black over its last 0.45 s and counts down `LIVE IN n` to the same instant: a replay is a pixel-exact rewind of the TV, so the cross-fade back to live dissolves two near-identical pictures and reads as nothing happening. `?s` is the *page's* timeline, which is `play_s` plus the badge's pre-cut head start (`hardware.md` §C4a), not the airtime. Added 2026-08-02 (ground truth, #19): `POST /api/groundtruth` records that a shot was actually taken — journal kind `ground_truth_press`, carrying `t`/`frame_t` on the *capture* clock (extrapolated from the newest processed frame, so alignment never crosses the emit-time/capture-time boundary; `null` when no vision is feeding the hub, which the reader treats as unanchored rather than guessing). **It never touches the clock** — an observation, not a command — and it is deliberately **not a mode**: one unconditional key, always live, no toggle, because the player must never have to classify a failure at the table. Missed / phantom / late are derived offline by `cueclock report`, which diffs presses against emitted `ShotTaken` events (including the gated `vision_shot_taken_ignored_idle` / `_no_clock` / `_killed` kinds — "vision saw it, the clock declined it" is a different bug from a miss). Amended 2026-08-03 (checkpoint 60): **`?mask=` is withdrawn** — the `/replay` page takes `?s=` only. The rectangles were correct and the burned-in countdown really is in the recorded pixels, but an opaque patch over live footage looked worse on the wall than the stale clock it hid, and the strategy has no good tail (from the full-screen `Shot Clock` scene the same rule masks ~70% of the canvas). The countdown is visible in replays again; the fix is to stop burning it over live video in the first place, which is a live-scene layout question, not an API one (`hardware.md` §C4a). Amended 2026-08-03 (checkpoint 61, #23): that layout landed, and with it **`GET /score` is withdrawn** — the scoreboard is drawn by `GET /shotclock?view=bar`, which carries clock and score together. No `/api/*` URL changed, so the Stream Deck contract is untouched; the merged page needs nothing the `/ws` payload didn't already carry. `/score` returns 404 rather than redirecting, deliberately: a stale OBS browser source pointing at it must fail visibly, not keep looking like it works. Amended 2026-08-05 (checkpoint 65, replay spans): **`GET /api/last_shot` is replaced by `GET /api/replay_anchors`** → `{"shot_age_s": <float>|null, "rack_age_s": <float>|null}`. The Replay key now has two spans — a tap plays the last shot, a hold plays the rack — which is the same cue-point arithmetic against a second anchor, so the endpoint returns both ages in one round trip measured against one instant (the save that ends the file). Renamed rather than grown a field, because an endpoint named for one anchor that returns two is a name that lies; the old URL 404s deliberately, so a deck daemon left on the old build fails visibly instead of quietly cueing every replay to the top of the buffer (same rule as `/score`). `rack_age_s` is stamped in `Hub.new_rack` (the New Rack press) and superseded by the next `shot_taken` (the break), so a rack replay opens on the break rather than on a minute of racking; both stamps sit ahead of the same gates the shot anchor does, so a `no_clock` drill anchors on its first shot. Null means "no rack yet", which the deck reads as *play as much of the buffer as there is* — the fallback a stale shot anchor already had. Amended 2026-08-05 (accuracy instrument): **`POST /api/groundtruth` is withdrawn** and 404s, per the same rule as `/score` and `/api/last_shot` — a deck left on the old build must fail visibly. The key it served (#19, "I shot") was pressed in 2 sessions out of 29: one unconditional key was the right design and still the wrong mechanism, because a key the player must remember on every shot is a key that stops being pressed. The denominator it existed to provide was already in the journal. `ShotClock.balls_settled` increments `rack_shots` **only** when no ShotTaken arrived since the previous settle (the missed-strike compensation added 2026-07-31 for correctness), and every record carries that count — so a `vision_balls_settled` record whose `rack_shots` exceeds the preceding record's *is* a self-reported missed strike, and settles recorded in `IDLE` are racking motion rather than shots. `cueclock report` derives the miss rate from that, over any journal, with nothing asked of the table; `run` prints it on clean shutdown and appends a `session_summary` record (strictly additive, as ever). Measured over every session to 2026-08-05: **764 shots, 25 missed, 3.27%** — flat across nine days in which `vision/events.py` grew 257 → 1842 lines, which is the evidence that the clip-by-clip loop had stopped converging. Derived and never accumulated, so the answer survives a `kill -9` and a better reader retroactively improves every session on disk. Amended 2026-08-05 (checkpoint 67): **`rack_age_s` is the break and nothing else.** `Hub.new_rack` now only *arms* the next `shot_taken` to become the anchor; it no longer stamps itself, and it no longer retires the previous break. The stamp-the-press half was wrong in the press it exists for — you run out, hit New Rack, and hold Replay to watch the rack you just played, at which moment "this rack" was a two-second-old empty table (live: `rack 2.0s ago, 598.5s file, cue 595.1s, 3.4s on air`, against a rack that broke ~266 s earlier and sat well inside the buffer). Between racking and breaking there is by definition nothing on the table to watch, so the honest anchor there is the rack that just ended; the new break releases it. Null now means "no break yet this session" — same deck fallback, one fewer meaning. Added 2026-08-06 (FargoRate, #27): `GET /api/fargo/search?q=<name>` — a server-side proxy of dashboard.fargorate.com's `indexsearch` (the dashboard sends no CORS headers, so the control page cannot ask it directly), normalized to `{name, location, rating, established}` rows; `established` is robustness ≥ 200, FargoRate's own provisional threshold, so a 10-game rating is never displayed as an established one. Upstream failure is a 502 the page renders as "Fargo unreachable" — a third-party outage can never touch the clock or the score. `/api/score/config` gains `p1_fargo` / `p2_fargo` (int sets, `null` clears) and the score payload carries `fargo` per player (`null` = no rating shown); changing a name without a rating in the same update clears the old rating, because a rating belongs to a name and a stale one under a new player is worse than none. Added 2026-08-07 (two-stage scratch verdict, #26, checkpoint 77): journal kinds `vision_scratch_candidate` (death spot, pocket, the dying crown's whiteness + staleness) and `vision_scratch_refuted` (witness id/position/reading) beside the kept `vision_scratch`, which is now the *adjudicated* verdict — strictly additive as ever, and the presence of candidate records is what tells a reader the journal's scratch count is a measurement rather than the pre-#26 ~6x upper bound (see §4.2). Added 2026-08-13 (double-elimination bracket, #43): `GET /bracket` (the bracket overlay page, its own OBS scene), `GET /api/bracket` (the full draw, or `{bracket: null}` in casual play), `POST /api/bracket` (create: `{name, players[], a_race, b_race, handicap, seeding}`), `DELETE /api/bracket` (clear → casual), `POST /api/bracket/archive` + `GET /api/bracket/archives` + `POST /api/bracket/archives/{name}` (write a finished draw beside the session journals, list them, reopen one), `PATCH /api/bracket/match/{id}` (redraw a Round-1 matchup, or override its two race targets), `POST /api/bracket/match/{id}/load` (put that match's two players and their own targets into `MatchScore` at 0-0), `POST /api/bracket/match/{id}/result` (confirm and advance; the body may carry explicit `scores`, and without one the loaded match's live scores *are* the result), and `DELETE /api/bracket/match/{id}/result` (un-play a match so the one feeding it can be corrected). **The governing constraint**: the bracket is optional state layered on top of the score already kept. `MatchScore` stays the single source of truth for what is on the TV — the bracket hands it two players and two race targets and takes a result back, and never keeps a score of its own, so the bracket page and the score bar cannot disagree in front of everyone. With none loaded every page and every deck key behaves exactly as it did before, which is what makes the casual night and the tournament night the same software. `/api/score/config` gains `p1_target` / `p2_target` (int sets, `null` clears) and the score payload carries `target` per player, because handicapping makes the two differ — a 563 against a 240 races 7 to 2, so one centre `race_to` would be a lie about at least one player; a target follows the same rule the Fargo rating already does, cleared by a rename that doesn't carry a new one. Confirming a result is deliberately **one action with several effects** (result written and both players routed, next ready match loaded at 0-0, `new_rack()` fired — a new match is a new rack), and the OBS scene flip is the deck's half, because obs-websocket lives out there (`hardware.md` §C3). Reporting a match that is *not* the loaded one is a correction rather than a new match: it touches the bracket and nothing else, so fixing a score from a phone can never park a clock somebody is playing under. A correction is refused by name (409, `Match 5 has already been played — clear its result first`) when anything downstream has been played, so a bad score is walked back one deliberate step at a time and no real result is ever silently erased. New journal kinds, strictly additive as ever: `bracket_created`, `bracket_cleared`, `bracket_archived`, `bracket_reopened`, `bracket_match_loaded`, `bracket_match_edited`, `bracket_result`, `bracket_result_cleared`. Amended 2026-08-13 (live iteration, §10.2): added `PATCH /api/bracket` (`{a_race, b_race}` — change the base races on a draw already underway; only unplayed matches move, a contested result keeps the race it was played under) and `POST|DELETE /api/bracket/player/{index}/withdraw` (concede every unplayed match of somebody who has gone home, down both sides of the draw, and put them back). A withdrawal invents no score and is **derived, never stored** — the same machinery byes already use — so it survives a restart as the player's state and reinstating is one press rather than an unwind; matches they already played are untouched in both directions. `DELETE /api/bracket` now answers `{ok, archived}` and writes anything contested beside the journals on its way out, because clearing is how you get back to casual and leaving the save behind a separate button made the destructive path the cheap one. `POST /api/bracket/match/{id}/load` parks the outgoing match's live score on that match and restores the incoming one's rather than zeroing — `MatchScore` is a single slot, so switching used to *be* the act of destroying what was in it — and refuses a walkover by name. `DELETE /api/bracket/match/{id}/result` keeps the score it cleared, so a correction behind a played match does not cost re-keying it. The match payload's `status` gains `paused` (started, then something else was loaded) and `forfeit`; players carry `withdrawn`. New journal kinds: `bracket_withdrawal`, `bracket_races_changed`; `bracket_cleared` gains `archived`. Amended 2026-08-13 (tournament eve, §10.3): added `POST|DELETE /api/bracket/match/{id}/concede` (`{slot: 0|1}` — one player gives up one match and keeps the rest of their draw, undone by the DELETE rather than by clearing a result, since a conceded match never was one) and `GET /api/lan` → `{url, qr}`, the scoring page's address on the LAN and an inline SVG QR of it, so nobody has to read an IP out loud to a room; `url` is `null` on a machine with no LAN address, and the host is discovered server-side because a page OBS loaded over loopback cannot tell a phone where to go. The match payload gains `conceded` (the slot that gave it up, or null) — a concession and a forfeit-by-withdrawal are both `status: "forfeit"` but are undone by different levers, so the page has to be able to tell them apart. New journal kind: `bracket_conceded`. Amended 2026-08-13 (UX pass): added `POST /api/match/clear` — the end-of-day reset the deck's Session **hold** fires on the way down: a loaded bracket is archived-and-cleared (same path as `DELETE /api/bracket`, so an unfinished tournament reopens from the archives) and `MatchScore` goes back to defaults (PLAYER 1 / PLAYER 2, 0-0, no race, no ratings, no targets), answering `{ok, archived}`; journal kind `cmd_match_clear`. Deliberately fired by the deliberate shutdown gesture and **not** by server exit, because mid-session restarts (deploying a fix into a live match) must keep the score — that is the whole reason `MatchScore` persists. The snapshot gains `timed` (`false` only under an untimed ruleset — overlays drop their countdown surfaces on it) and `GET /api/rulesets` gains `details` (per-ruleset `base_seconds` / `first_shot_seconds` / `extension_seconds` / `timed`, so the control page captions the pick without hardcoding the rulebook). New ruleset `freestyle` (`clock/rulesets.py`, `timed: false`): **match play with no shot clock** — distinct from `no_clock` *mode*, which is the drill surface (no racks, rep counters, no score keys); freestyle keeps everything match mode has except the countdown. Operator start/restart/push/extension are no-ops in the clock itself, and the hub gates vision events exactly as it does for `no_clock` (same `_no_clock` journal kinds; the payload's `ruleset` says which axis parked it). Extensions: `ENFORCE_EXTENSION_LIMIT` (`clock/rulesets.py`) is **off** — every extension over a live countdown is granted (a misheard verbal extension against an unlimited pool costs nothing, where a limited one burned a real extension mid-rack), and `extensions_allowed` reports 0 so no surface draws a pool that cannot run out; per-ruleset allowances stay in the data for the day the flag flips back. Added 2026-11-17 (checkpoint 127, #54): `POST /api/ball_strip/{on,off}`, the switch for the bar's ball strip (a test graphic, §13), journaled as `cmd_ball_strip` and logged as a hub input like every other command. It starts on with every `run`. Journal contract, strictly additive: a `ball_list` record per ball list written (`frame_t`, `reason` settle/rest, `arm`, `balls` as space-separated labels in strip order, `tracker` = the tracker's present tracks at a settle), stamped with clock state and `rack_shots` so session-stats can judge it per rack. Amended 2026-11-17 (checkpoint 134, #68): the snapshot's `cue` loses `expired` and gains `buzzer`, sent the instant the countdown reaches zero. A Session-key sound check (the buzzer played once at session start) was built and dropped in the same checkpoint: Steven did not want it. Amended 2026-11-22 (checkpoint 144, #75): **instant replay is removed**, and with it **`GET /api/replay_anchors` and the `/replay` page are withdrawn** and 404, per the `/score` rule. The idle stop (#60) was the endpoint's other reader and needed only the shot age, so it moves to **`GET /api/shot_age`** → `{"shot_age_s": <float>|null}`, stamped before every gate as before. The rack anchor (`rack_age_s`, the break) had no other reader and is gone. Amended 2026-11-22 (checkpoint 145, #74): **`POST /api/ruleset/<name>` and `GET /api/rulesets` are withdrawn** and 404, per the `/score` rule. In their place `POST /api/setting/{name}/{value}` (404 for an unknown setting or a value it does not offer) and `GET /api/settings` → `{values: {name: value}, options: {name: [values]}}`. The deck and the control page were their only clients and change in the same commit. Journal: `cmd_setting` (`setting`, `value`) per change, `settings_unreadable` / `settings_unsaved` when `config/settings.json` cannot be read or written; `cmd_ruleset` is written only by a replay of an old log. The session log's header carries `hub.settings` instead of `hub.ruleset`; a header with a ruleset is read through the legacy table. Added 2026-11-22 (checkpoint 146, #76): `POST /api/voice/{on,off}`, the Voice switch: off, a spoken "extension" is journaled as `voice_ignored` and never reaches the clock, while the deck key and `/api/extension` still grant. Journal kinds `cmd_voice` (`enabled`) and `voice_ignored` (`call`, `reason`); a heard call is now the logged input `voice_extension` rather than `extension`, so a session replay re-decides it under the same switch. On with every `run`. |
| WebSocket `/ws` | Broadcast `{state, time, display, ruleset, extensions_used, cue}` at 10 Hz + snapshot on connect and on every event. Added 2026-07-31: the broadcast is the composed Hub payload — the clock snapshot plus `vision` (bool, kill-switch state) and `score` (`{p1: {name, score}, p2: {name, score}, race_to}`); `GET /api/state` returns the identical shape. Added 2026-08-02: `mode` (the active session mode) and `base` (the countdown a fresh start would hand you *now* — rack- and mode-aware, straight off `current_base()`). `base` exists so the deck's `START N` key can state the time it is about to give you without recomputing the rule: the face is drawn from the same function the press calls, so it cannot drift. Added 2026-08-07 (checkpoint 80, #30): `solo` — the bar's solo panel, strictly additive. `null` in `match` (the scoreboard is the panel there); in `run_out` `{shots, last_s, avg_s}` where `shots` is the clock's own rack counter (so it inherits the missed-strike settle compensation) and `last_s`/`avg_s` are the wall-clock pace from each fresh countdown's start to the ShotTaken that ends it, `null` until the first *timed* shot — the opening shot of a run-out leaves IDLE with no countdown over it, so it counts in `shots` but never in the pace; in `no_clock` `{reps, elapsed_s}` — ShotTaken fires seen while the mode is on (approximate by design: they inherit detection accuracy, and a settle is deliberately *not* compensation here because drill placement motion settles constantly) and seconds since the mode was entered. Scope: the panel rescopes on mode switch and New Rack; pause/reset only close the open pace timer. Wall time deliberately includes scratch holds and extensions — the number answers "how long did the shot take", not "how much clock was burned". Added 2026-08-13 (#43): `bracket` — `null` in casual play, else a **compact** summary `{revision, state, name, champion, match, armed}`, where `match` carries the loaded match's id, title, side, phone header and winner/loser routing, and `armed` is `{slot, name, score, other}` once somebody has reached their own race target. Compact deliberately: the full draw is a few kilobytes and changes a handful of times an evening, so it rides `GET /api/bracket` and the pages refetch when `revision` moves, rather than being re-serialized ten times a second for every connected page all night. `armed` is *derived* from the live score against each target and never stored — which is what makes taking a point back clear the arm by itself, with no undo to find. Added 2026-11-15 (#52): `health` — `null` on a bare server, else `{drift, drift_stale, hz, hz_slow, balls, warn}`: the launch-time reference drift reading and whether it crossed `BOUNDARY_DRIFT_WARN`, the detect rate from the latest heartbeat and whether it is under `hub.DETECT_HZ_WARN` (78 Hz, the cadence every event constant was tuned at), the tracker's ball count, and `warn` = either flag. The control page prints it as a line under the Play tab and the deck turns the Session dot amber on `warn`. It exists because three months of stale-reference warnings went unread in a launchd log behind the TV. Journal contract, same date and strictly additive: a `reference_drift` record once per launch (`drift`, `stale`), and `detect_heartbeat` gains `queue_ms` / `blobs_ms` / `loop_ms`, the mean ms per frame spent waiting for a detect worker, inside `find_blobs`, and in the loop thread's churn + advance + dispatch, so the journal can say where frames go. Added 2026-11-17 (checkpoint 127, #54): `ball_strip` — `{shown, balls}`, the switch and the labels (`cue`, `1`–`15`, `?`) of the latest written ball list that had no arm over the table, `null` until `run` writes one (§13). Amended 2026-11-22 (checkpoint 145, #74): the snapshot's `ruleset` is replaced by `settings`, the six values by name; `timed` and `base` stay. Added 2026-11-22 (checkpoint 146, #76): `voice`, the Voice switch (bool, true with every `run`). |
| `shotclock.html` | OBS browser source, transparent 200×200; Web Audio chimes (10 s: E5 sine; 5 s: A5 triangle; expired: G4→D5 cadence); visual states: motion dims, grace red border, expired flashing. Requires "Control audio via OBS" for stream audio. Amended 2026-08-03 (checkpoint 61, #23): one page, three views, selected by `?view=`. **`?view=bar`** is the live scene's layout and the one that sounds the cues — a 160 px plinth along the bottom of the side-angle column carrying **the scoreboard as well as the clock**, so `score.html` and `/score` are gone. That merge is forced, not a preference: the drain runs the full width of the bar, crossing from the score area into the clock cell, and no overlay can span two OBS sources. Palette is `/replay`'s (`#0B0E13` ink, `#2E86C8` accent spent only on the drain, `#9FB3C4` slate, a single `#FF4D4D` alarm); escalation is by **area** — ink (urgent) → edge (GRACE) → field (EXPIRED) — because GRACE and EXPIRED both read "0" and three near-identical reds cannot be told apart at twelve feet. Geometry, states and rationale: `hardware.md` §B3. Amended 2026-08-13 (#43): a handicapped match has no single race, so **one fact goes in the centre and two facts go under the players they belong to** — differing targets are drawn as `TO 5` / `TO 4` beneath each player's own score, on their own side of the tally rule, and the centre line stands down while they are up. The first build put both in the centre as `3 OF 5 · 2 OF 4`, which said each score twice (they are already 48 px above it) and made four numbers in a row that had to be read left to right to work out who was playing to what. Equal targets collapse back to `RACE TO n` in the centre, so a bracket with handicapping off does not read as something new and one fact never looks like two; casual play with only `race_to` is untouched. Amended 2026-11-17 (checkpoint 134, #68): the expired cadence is replaced by **the buzzer** on `cue: "buzzer"`, sent at zero: two sawtooths at 196/199 Hz through a 2.4 kHz low-pass, held flat for 1.1 s at gain 0.35 (~14 dB RMS above a blip, peaking just under clipping), so it is the opposite of the 5..1 blips in pitch, timbre and length. Loudness is judged at the table. |
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
6. **The genuinely hard ambiguity: slow ball-in-hand nudges with the cue tip** are visually near-identical to a soft legal shot in pixel-motion terms. The new architecture has three real answers unavailable to the old one: pre-strike stillness dwell on a *reliable* per-ball track, velocity-impulse shape (a strike is a step from rest to ≥0.2 m/s in 1–2 frames; a placement is continuous low-speed drag), and optionally an acoustic strike-click coincidence (see §4.4; disqualified for good 2026-11-18, §8).
7. **Detection cadence ≠ event cadence.** The clock only needs events; detection can run at half rate while capture runs at full rate for replay.

**Test-corpus shopping list** (record with the Basler in week 1 — these are the old failure modes as labeled clips): soft/medium/power shots, break shots, ball-in-hand placement by hand, ball-in-hand nudge with cue tip, practice strokes over the ball, shaft-adjustment of cue ball, hand retrieving ball after scratch, walking around the table mid-settle, chalking near the table, scratch + replace, balls settling with a slow-creep finish, ball spinning in place after a draw shot.

## 3. Requirements for the rewrite

**Functional:**
- Emit `shot_taken`, `balls_settled`, `scratch` events driving the shot-clock state machine (semantics of §2.1).
- Voice "extension" keyword → extension (§2.1 audio row).
- HTTP/WS surface per §2.1 (Stream Deck compatibility).
- OBS overlay + control panel equivalents.
- Publish the display feed to OBS (see `hardware.md` (video output part)).
- Later (backlog): break-speed measurement, ball-position stream, replay export, match stats.

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
 ├─► detection path (every 2nd frame ≈ 80 Hz):
 │     Bayer→gray (0.12 ms) + color sampling from raw Bayer at ball patches
 │     → |frame − empty-table reference| → threshold → morphology
 │     → connectedComponentsWithStats + area/circularity gates (ball ≈ 41–45 px ⇒ tight area window)
 │     → tracker: Hungarian assignment + per-ball Kalman (coast through occlusion)
 │     → cue-ball ID: white-fraction + hue stats on central disk, resolved globally
 │     → undistortPoints + homography → positions/velocities in table mm
 │     → event logic (§4.4) → clock state machine
 └─► display path (30–60 Hz): debayer (EA) → full-frame remap (Metal, checkpoint 117) → Syphon → OBS
```

Key architectural choices, each the researched consensus for this setting:

- **Classical segmentation, not a neural detector.** Fixed camera + locked exposure + known background is the textbook case where background differencing against a captured empty-table reference beats everything (POOL-AID and the >99%-detection snooker literature use exactly this). Stationary balls never fade (unlike MOG2/KNN adaptive subtractors, which are the wrong tool — they absorb stopped balls). HoughCircles is fragile and unnecessary when radius is known. A CoreML nano-detector (3–10 ms on ANE) stays in reserve as a low-cadence auditor if ever needed — not in v1 (YAGNI).
- **Full frame rate (amended 2026-10-17, checkpoint 106).** Detection ran at stride 2 (78 Hz) from build step 5 to checkpoint 105, because one thread could not hold 156 Hz. It now runs on every frame: the frame-bound half (`VisionPipeline.find_blobs`) is stateless and runs on 3 worker threads (OpenCV drops the GIL; ~420 frames/s sustained on the M2 against the camera's 156), while churn, tracker and events stay on the loop thread in frame order. Every frame-count constant was measured at 78 Hz, so `events.TUNED_HZ` names that cadence and `EventConfig.at_cadence` / `TrackConfig.at_cadence` convert counts and per-frame gains to the same span of time — a conversion, not a retune. The two impulse-grade signals keep their tuned *step*: raw speed spans the newest observation ≥ 0.75 tuned intervals old, and churn differences against the gray one tuned interval back. A one-frame step at 156 Hz divides the same centroid jitter by half the time — still balls' raw-speed p99 measured 581 → 1156 mm/s on `static_spread` — which would have crossed every impulse threshold. What 156 Hz buys is everything else: half the inter-frame travel for association (a 3 m/s ball steps 19 mm instead of 38 on a 57 mm ball) and twice the evidence per time window. Halving to 39 Hz on the same corpus cost 7–9 of 54 graded strokes and under-read break speed (1731 vs ≥2500 mm/s) — the dose-response that motivated the change.
- **Track all balls, minimally.** At 160 fps even a 13 m/s break moves a ball ~1.4 diameters/frame; at 80 Hz detection it's ~2.8. Nearest-neighbor/Hungarian association with a generous gate is nearly unambiguous; Kalman filters earn their keep only for coasting through hand occlusions and for smoothed velocity — not for association. Norfair is the off-the-shelf fit (centroid input, custom distance, ReID hook for color re-identification after occlusion) if we don't hand-roll; pedestrian MOT stacks (ByteTrack/OC-SORT/BoT-SORT) are dead weight here.
- **Cue ball identification** by white-fraction + hue statistics on a central disk of each ball patch, resolved **globally** (Hungarian against ball identities) rather than per-patch — POOL-AID's 72% → 98.6% lesson. For the shot clock only cue-vs-rest matters; full 16-ball identity is backlog.
- **Undistort points, not pixels.** Detection runs on distorted frames; only detected coordinates go through `undistortPoints` + homography (~200× cheaper than full-frame remap, and the detector sees unresampled pixels). Full remap runs only on the display path. Straight-line logic (if we ever detect the cue stick again) must account for curvature in distorted space — undistort line endpoints too.
- **The rejects are evidence too — export them (2026-08-11, #41).** The geometry gates above are already a *classifier*: an arm fails the area ceiling, a cue stick fails the minimum extent, anything reaching in from off-table fails the mask-border rule. Throwing those verdicts away left the rest of the system unable to see a hand at all — which is why four separate passes (checkpoint 50, checkpoint 66, #38) found the `merged` flag reading ~0 on hand events. A hand that *engulfs* a ball produces one over-size blob that is discarded outright, so there is no detection left to mark; the arm was never absent from the image, only from the exported evidence. `detect_frame` therefore returns a second list, `Intrusion`, describing the hand-scale rejects. Strictly additive — which blobs become balls is untouched, and §2.3 lesson 2 stands: balls are never shape-filtered, because fast balls streak. The one shape number it carries is the radius of the largest inscribed disc, which is where a hand and a cue actually differ: a hand carrying a ball measures 23–62 px against 8–20 px for a bridge hand with the shaft running through its fingers. What consumes it is settle-time *policy*, in the hub — see §4.2's ball-in-hand note.

  **Nothing consumes it today (2026-08-29, checkpoint 104).** The one consumer, #41's `_hand_delivered`, was deleted with the verdict it fed. The channel stays because it is honest evidence cheaply produced, and because the export itself was never what failed — but note what was measured before reaching for it again: suppressing a ball-shaped blob because it sits *inside* an intrusion box fires on **3.09%** of detections in `occluded_address_0731`, a legitimate address, against **0.62%** on `flag_phantom`, where a white tank top really did mint a track at 1220 mm/s. Used that way it would trade a phantom for blindness at the address, which is the #1 upstream cause of misses. An intrusion says a person is reaching in; it does not say which blob is not a ball.

  **And so it is no longer computed (2026-11-15, checkpoint 109, #50).** `DetectConfig.export_intrusions` defaults to off: describing a reject costs a distance transform per large blob, which reached 27% of `find_blobs` on `static_spread` against a stale reference. Setting it back on restores the list exactly (checked frame by frame against the pre-109 detector, 17 of 17 intrusions on `ball_in_hand_carry_0811`). The same checkpoint crops segmentation to the cloth mask's bounding box padded by 16 px (morphology reaches 6), with coordinates offset back. Same blobs, same order, same areas; centroids agree to 1e-13 px. The two changes take `detect_frame` from 3.2 to 2.3 ms on the corpus clips.

- **The shadow gate exempts ball-sized discs (2026-11-15, checkpoint 110, #51).** The colour gate ("0.65–0.95× the reference brightness, chroma within 0.15") was anchored on the 2026-07-27 corpus, and on this blue cloth it is also the signature of the solid blue 2, the 10's stripe and the green 6, which it discarded on every frame. It is not removable: deleting it outright cost 8 corpus tests, because it also rejects forearm and hand shadows beside a reaching player, diagonal cue sticks (their bounding box passes the extent gate) and touching pairs. The exemption is shape: fill the blob's holes (a blue ball's white number disc is within threshold of the cloth, so its raw blob is a ring), then take the IoU against a disc of the filled area at the filled centroid. A blob at IoU ≥ 0.84 and 0.65–1.15× the predicted ball area is a ball. Measured over every colour reject in the corpus (9621 blobs) and checked on contact sheets: hand and forearm shadows reach 0.76, rail and pocket corners 0.82, and above 0.83 every blob was a ball. It recovers 68% of the rejects. Two variants failed first and are recorded so they aren't retried: IoU against the *predicted* disc (non-balls reach 0.74–0.83, and a rolling blue 2 at 0.8× predicted area flickered across the cut and minted a newborn-strike phantom), and IoU without hole filling (the same 2 read 0.67–0.78). Ruled out by the audit, still: "brighter than the reference ⇒ ball", and its variant tried here, highlight contrast within the blob (still balls read 10–20 levels, the light's reflection is a thin dim outline). `_as_intrusion` keeps the plain colour gate: intrusions are hand-scale, so a ball-size exemption has nothing to say there, and the channel is off.

- **Colour rescues what gray cannot see, and touching balls split (2026-11-15, checkpoint 113, #56).** Detection differences gray, and on this cloth the solid red 3 has the cloth's luminance (centre |diff| 21 against the 25 threshold) while moving R by +131 and B by −77. It was invisible on nearly every corpus clip, not just the 16-ball one. Two additions, both measured over the whole corpus (71 clips, every 8th frame) and checked on contact sheets:
  - *Chroma rescue.* A second channel: the frame's colour change orthogonal to the cloth's own colour direction (`ChromaReference`: two fixed RGB axes perpendicular to the cloth's median colour, applied to the Bayer quads at half resolution, 3×3 quad blur). A shadow scales the cloth's RGB along that direction, so it differences to ~0 there; the blue 2 reads ~6, like a shadow, and stays the shadow gate's business. Threshold 25 is 2× the maximum read on all four empty references against themselves. It is **rescue-only**: a colour blob becomes a ball where gray segmented nothing of a whole ball's size (no gray blob ≥ 0.65× the predicted ball area under it, accepted or rejected), through the same geometry gates and border rule, holding a disc of ≥ 0.5 predicted radii. Every guard is there because the survey found its case: the gray verdict on a hand, glove logo, cluster or stick-and-cue blob must stand; diagonal cue sticks fattened by the blur measure 0.22–0.30 radii against ≥ 0.78 for every ball (one blurred stripe in flight at 0.57). Where gray saw *part* of the ball, gray's detection stays — except when it sits ≥ 0.6 radii off the colour blob's centre and the colour blob is a whole ball-sized disc: that is the red 3 seen in gray as only the crescent of its own shadow, which hovers at the area floor, and deferring to it made a still ball's track jump 25 mm and latch motion (`scratch_replace`). The offsets split cleanly (7 558 centred partial views at 0–12 px, 27 crescents at 14–25 px). Replacing centred partial views too was tried and failed: where gray's area hovers at the cut, the centroid alternates between the two views, 3–4 px each way (`live_divergence_0731a`), and three clip-start settles moved for no physical reason.
  - *Touching-pair split.* A merged gray blob is read together with the colour blobs touching it (and touching no other gray blob), holes filled. Its distance transform peaks at ~1.0 ball radius at each ball's centre with a ~0.65 saddle at the contact; a streak is a capsule with a flat ridge and no saddle, which is why §2.3 lesson 2 still holds. Cores at ≥ 0.75 radii, pixels to the nearest core, and the split stands only if every part passes the shadow exemption's disc test (IoU ≥ 0.84, 0.65–1.35× predicted area: gray-and-colour parts are larger than gray blobs, p99 1.28, nothing 1.35–1.7). Half of all merged blobs in the corpus (3 199 of 6 442) were touching balls, and every sampled split was real, including blurred pairs from break and power clips. The split runs **before** the shadow gate, because the gate rejected touching pairs whose centroid sample lands in their contact shadow (checkpoint 110 recorded it): a white cue touching the green 6 in `cluster_shot_0801` was invisible for 6 s and read as a scratch. 774 such blobs in the survey now split (75 distinct, all real pairs). A blob that fails stays what it was.
  - Net: corpus detections +13% with no new pair of detections closer than a ball radius; `all_balls_still_1115` and `static_spread` read 16 on every frame (were 11 and 14). Cost: `detect_frame` 2.2–2.6 → 3.9–5.2 ms on the corpus clips, on three detect workers. The rail half of #56 needed no code: it was the stale reference (#49), and RECAL's `empty_table_1115` recovers the 14 and the 11.

### 4.2 Event logic (the actual product)

- **`shot_taken` — a conjunction, not a single signal** (patent US10226685 + US8992315 + launch-monitor pattern):
  1. cue ball stationary ≥ dwell (the old `pre_strike_stillness_ms`, now on a reliable track),
  2. velocity impulse: from below the settle floor to **≥ ~0.2–0.3 m/s within 1–2 detection frames** (softest deliberate shot ≈ 0.45 m/s — Dr. Dave; noise floor ≈ 0.007–0.022 m/s — §4.3), and
  3. displacement consistent over the next few frames (coherent trajectory — the confirm-window idea, now trivial with tracks).
  Ball-in-hand placement fails (1); practice strokes fail (2); hand-waves fail (2)+(3).
- **`balls_settled` — per-ball velocities with hysteresis**, not global pixel motion: every tracked ball's filtered speed **< 0.03–0.05 m/s** (RoboCup SSL autoref uses 0.05–0.2 m/s for officiating-grade "stationary") **and** max any-ball displacement over the last ~500 ms < ~2–3 mm (catches pocket-edge creep), sustained for a dwell; re-enter "moving" only above ~0.1–0.15 m/s (Schmitt-trigger hysteresis so it can't chatter). Per-ball velocity is immune to the failure modes that plagued global frame-diff: hands in frame, player shadows, a ball spinning in place. **Rules caveat (verified 2026-07-27, Steven's catch):** spin-in-place immunity overshoots — a shot officially ends only when all balls have "stopped moving **and spinning**" ([WPA def. 8.2](https://www.poolbilliards.co/rules/wpa-rules-of-play/definitions/); International Rules 6k's "come to rest" implies the same), so `balls_settled` must additionally wait for visible spin to stop. **Implemented 2026-07-28 as the appearance-churn gate:** mean |Δgray| in a 16 px disk inside each stationary ball, EMA-smoothed by the tracker, blocks stillness above 2.0 gray levels. Measured on `spin_in_place` at the 78 Hz detect cadence: still-ball noise floor 1.2–1.5 (max 1.66), spinning cue 2.6–6.5 decaying to the floor exactly at visible spin stop (t≈7.0 s in that clip); the gate moves settle from 6.48 s (mid-spin) to after the stop, graded by a corpus timing test. Hands/shadow immunity is unaffected — the gate never looks outside the ball, and unobserved (coasting) balls are exempt.
- **`scratch`** — the cue-ball track terminates (not re-associated, not coasting) with last position near a pocket zone; latch during motion exactly as the old system learned. Pocket zones come from the homography, so this is finally geometric rather than "ball missing anywhere." **"Last position" means last *observed* position (corpus lesson, 2026-07-28):** a dying track's final coordinates are up to a coast-budget of constant-velocity extrapolation, and an occluded passing cue's phantom can coast into the zone and die there (`spin_in_place`: last real observation 359 mm from the pocket, coasted death 89 mm). A real sink needs no such generosity — its final observations come from the pocket jaw itself (`scratch_replace`: last observation 110 mm from the pocket center).
- **Optional corroborator (was backlog; disqualified for good 2026-11-18, §8):** a broadband transient ("click") detector on the existing audio stream, coincident within ~100 ms of the velocity impulse — patent-precedented (US10226685), nearly free since an audio thread already exists, and it cleanly separates the one genuinely ambiguous case (slow tip nudge vs. soft shot: no click vs. click). Fresh motivation 2026-07-31: `soft_touch_0731` is a real live shot whose cue peaks at **225 mm/s raw** — below any impulse floor that still rejects nudges, so it is undetectable optically by design; the clock's settle-counts-the-shot rule (§2.1 semantics, checkpoint 26) absorbs it, but only audio can name it a shot.
- **Occluded-address strike — the newborn-cue path (added 2026-08-01, checkpoint 26, from `occluded_address_0731`):** when the player's body/stick hides the addressed cue ball long enough, its track dies, the whiteness vote parks on some other bright ball, and the real strike surfaces as a **brand-new track born already at impulse speed** (measured: born at 1572 mm/s, 964 mm from the wrong flag-holder's rest — every rest-anchored gate correctly rejected it; that live session's break and post-push shot were both lost this way). A ball cannot teleport into existence at speed, so a fresh cue-vote-winning track born ≥ impulse floor opens a candidate anchored at its **birth spot**, provided the table had no genuine mover for a pre-strike dwell before the birth and none within kiss range of the birth spot. Confirm adds two measured carried-ball gates (`scratch_replace`'s fetch carry, born 2220 mm/s, is the false-positive twin): ≥ 90 % of confirm-window frames observed (a carried ball coasts through finger occlusions; a struck cue on open cloth is observed every frame) and adjacent-frame raw-speed ratio ≤ 2 (carry: 575 → 4140 mm/s between frames; struck cue: ±5 %). The path is **armed** once per rest cycle: disarmed when it fires (no machine-gun re-opens off the same strike), re-armed by a cue-flagged ball at rest that has *translated since the disarm* — the whiteness-vote fallback (a still ball that never moved) must not re-arm it. (Scratch also disarmed the path from checkpoint 26 through 30; checkpoint 31 removed that — see the `jaw_strike_0801` bullet in §4.2 — and the fetch-carry false positive it guarded is contained by the carried-ball confirm gates instead.) **No-cue variant (2026-08-01, from `no_cue_strike_0801` + `scratch_shot_0801`):** the tracker can lose the cue outright — the address occlusion kills its track, the regrab grace and whiteness floor keep the flag empty, a fast newborn's blur-corrupted frozen color sample can't win the vote back, and a dead cue's track **coasts as a flagged phantom for ~0.15 s after impact**, longer than the whole newborn window. So when no *observed* ball holds the cue flag, the scan runs over every observed ball and the birth conjunction alone carries the identity (on a quiet table, only the struck ball can materialize at impulse speed); the candidate then follows that track by id, not the flag. Founding tracks are excluded (first sight at engine start is not a birth), and candidate expiry is centralized so a stuck cue-follow candidate can't block the scan.
- **Ball roster — who may latch table motion (added 2026-08-01, checkpoint 26):** address gear (bridge-glove knuckles, watch, stick fragments) reaches 79–94 % observed — past the flicker filter — and a 30 mm gear drift used to latch `table_moving` and mint a phantom settle ≈ a phantom clock reset mid-address (three in one live session, video-verified). Structural discriminator measured across every clip: real latches come from tracks alive since the last quiet period; gear tracks are born mid-play already moving. Only **roster** balls may latch: membership founded at engine start, then admitted by union whenever the table has been continuously still for a settle dwell (union, not replacement — a roster ball occluded by the addressing player must not fall off mid-aim), pruned wholesale at each settle. Racked/placed balls join at the next quiet dwell; a gear fragment sweeping in cannot join mid-sweep because its own motion breaks the quiet streak. Carom-guard mover stamps inherit the roster filter, so gear can no longer starve strikes either. Amended 2026-08-11 (checkpoint 88, #40, from `latch_blind_shot_0811`): roster membership is *negative* evidence, and the one class that can never earn it is **the cue reborn at the impact** — admission happens only during sustained quiet, so a track born mid-motion is permanently excluded. On the flagged clip the struck cue crossed the table at 2048 mm/s as a two-frame-old track and the engine latched **nothing at all** — no episode, no provisional freeze, no settle, 0 events in 20.74 s — while the countdown ran through the stroke; the strike path's own escape hatch (a fire certifies the track and latches directly) never ran either, because the crown was parked on the cue's dead identity. A non-roster track may now latch on **positive** evidence instead (`EventEngine._struck_mover`): certified struck-ball speed (two consecutive *observed* raw reports at `parked_fast_confirm_mms`) **and** a mostly-unmerged observed history (`newborn_merged_frac_max`), on top of the ordinary solidly-observed / `moving_enter_mm` bar. No new constant: both gates already existed for the same question in the strike path. Measured corpus-wide over every non-roster track clearing the ordinary bar — real strokes best consecutive-observed raw pair 1210–3803 vs. hand junk ≤ 1122, and the hand events that *do* reach struck-ball speed are carries, which read 0.67–1.00 merged against 0.00–0.17 for every admitted stroke. The new latchers deliberately do **not** stamp `_recent_movers` or `_last_mover_t`: a reborn cue must stay invisible to the carom guard and to the birth path's pre-strike-dwell test, or the admission would refuse the strikes it exists to catch. Known residuals, each with a recorded provoker: a planted glove that later *slides* ≥ 30 mm without lifting (needs `glove_address`-style recording to measure); the stick-merge yank and the hand pickup were residuals here until checkpoint 28 closed both (merge flag and pickup discard, below).
- **Cue-rest slide guard (added 2026-08-01):** the rest memory keeps its original timestamp while following the flagged cue's coordinates; when the whiteness vote jumps to a different still ball, that used to teleport the rest spot with ancient dwell credit intact. A still-stamp landing > handoff radius from the current rest now restarts the rest (fresh dwell) — a genuinely creeping cue moves ~0.5 mm between stamps and never trips it.
- **Merged-blob flag — a still ball cannot streak (added 2026-08-01, checkpoint 28, from `address_yank_strike_0801` / `merge_yank_0731`):** the stick or hand merging into an addressed ball's blob yanks the shared centroid 31–47 mm — past the 30 mm motion latch (phantom settle ≈ phantom clock reset mid-address) *and* enough to wreck the rest/still state the strike conjunction reads, so the **real shot two seconds later was missed live too** (video-verified: one root cause behind both "the clock randomly reset while I was down on the shot" and "then it didn't see the shot I took"). Fix is layered per §2.3 lesson 5: the detector reports (never gates) blobs that are too big to be one clean ball — measured clean still balls ≤ 1471 px² / ≤ 47 px extent across every frame vs. ≥ 2071 px² / ≥ 102 px merged, flag at 1800 px² / 70 px — and the tracker treats a merged observation on a **confirmed, still** track as presence evidence only: position/velocity/whiteness/churn untouched, `observed=False`, the observation consumed so the yanked centroid can't found a junk track. Moving tracks keep full streak tolerance (lesson 2 stands). Contamination small enough to slip under the flag cannot yank a centroid past the latch — the yank scales with the contaminating area. Bonus: the whiteness sample no longer ingests stick pixels, protecting the cue flag through addresses.
- **Pickup discard — motion episodes that leave the table (added 2026-08-01, checkpoint 28, from `ball_in_hand_pickup_0801`):** a hand pickup latches table motion honestly (a roster ball genuinely drags), then the fingers wrap it and its track dies — and the quiet that follows minted a settle ≈ a clock reset mid-visit. Measured discriminators: every picked-up ball's track dies **far from any pocket** after a **41–78 mm** drag, while a real shot always ends with a live mover still on the table (`shot_debug`'s object ball) or a pocket-zone death after real travel (its potted cue: 1699 mm). When every ball that latched the current episode has died off-pocket under 200 mm of travel (2.6× the measured drag max), the episode is a pickup: `table_moving` unlatches with **no settle**. Pot deaths (inside pocket zones) and any surviving mover keep the settle real. Two guards protect the no-cue strike, whose original track dies in the address handoff (mirroring a pickup death) while its newborn continuation is not roster and never joins the mover set: any reported ball's real translation within the last settle dwell defers judgment (`_last_translate` covers coasted frames, so a brief occlusion can't open a one-frame window), and a **sticky per-episode marker** — any *judgeable* non-mover ball translating while latched (junk can't pass the flicker filter; hand fragments also die before the 0.4 s judgeability age) — marks the episode a shot permanently, so the discard can't outrace the settle once the flying ball finally stops.
- **Sub-impulse touch shots — an honest hole, not a tuning target (measured 2026-08-01):** `soft_touch_0731`'s settle used to pass only because the stick yank phantom-latched motion; the touched ball itself peaks at 25–28 mm per 0.2 s window, under the latch, and no honest slower latch exists — `shaft_adjust` reaches **59 mm per 0.5 s window while judgeable**, worse than the touch shot's 43. With the merge flag the lucky settle is gone: a sub-impulse touch shot currently produces *no events* (clock keeps counting; Start N recovers). This is §2.3 lesson 6's boundary made precise; the acoustic click corroborator (backlog bullet above) is the designed answer for the whole class, not more vision tuning. (Superseded: see the 2026-08-02 foul reversal and the 2026-11-18 disqualification of audio, both in §8.)
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
- **Removed 2026-11-15 (checkpoint 114, #55).** The ring grew to 20 s for the deck's Flag key (≈ 7.2 GB at 156 Hz once detection went full-rate, checkpoint 106), with ~1 Hz pickled vision-core snapshots so a dump could replay warm (`analyze --warm`). Flag went unpressed from 2026-08-25 on: a player watching the rack doesn't notice a clock fault inside 20 s, and choosing PHANTOM/MISSED/OTHER was a classification made at the table. Ring, snapshots, warm replay, `/api/dump` and the key all went together. `run` now holds no frames beyond the detect queue (`DETECT_INFLIGHT`). Instant replay was OBS's replay buffer until it too was removed (checkpoint 144, `hardware.md` §C4a). Accuracy evidence comes from the session journals; new corpus clips are deliberate `record` takes.

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
│       ├── capture/             # pypylon grab loop, camera config/UserSet,
│       │                        #   clip container, offline detection cache
│       ├── calib/               # ChArUco intrinsics, homography, image↔table transforms
│       ├── vision/              # pure detection core: segmentation, tracker, events
│       ├── clock/               # state machine + settings (port of state.py semantics)
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
- **KISS enforcement:** v1 scope = clock + events + overlay + API. No replay export, no break speed, no stats, no 16-ball identity until the clock is trustworthy. Amended 2026-11-17 (checkpoint 127, #54): 16-ball identity is in, as a **separate output** read off a still table — the ball list and its test strip (§13). It never feeds the tracker or the engine; #63 and #64 are what let it touch the clock.

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
7. Backlog by demand: replay export, break speed, acoustic click corroborator, stats, 16-ball identity. (16-ball identity built 2026-11-17 as the ball list, §13. The acoustic click corroborator was disqualified for good 2026-11-18, §8.)

## 8. Decisions (resolved with Steven, 2026-07-27)

- **Name:** `cueclock` — repo, src package, and CLI.
- **License:** MIT. Patent note: MIT (like Apache-2.0) cannot shield the project from *third-party* patents, and this space has active ones (US10226685 automated shot clock, US8992315 tip-contact detection — see §6). Practical read: enforcement against a personal, free, non-commercial project is essentially unheard of, and 20+ similar open-source billiards-vision projects sit on public GitHub untroubled. **Start the repo private** (Steven's stated comfort); flipping public later is one click, and nothing in the scaffold changes either way.
- **Acoustic click corroborator:** backlog. Ship per-ball tracking first; build it only if the corpus shows slow nudges still leaking through. (Superseded: rejected 2026-08-02, disqualified for good 2026-11-18, below.)
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

- **2026-08-29 — REVERSED (checkpoint 104). Moving the cue ball by hand resets
  the clock again; the 2026-08-02 "eat the rare wrong reset" decision above
  stands unnarrowed.** The premise the entry above rests on — *"a real stroke
  always leaves at least one ball resting where no hand is"* — is false, and
  live play falsified it. When a shot **pots** the object ball, that ball
  vanishes and never arrives, so the cue is the *only* arrival; if the player
  is still leaning over where it stopped, every arrival is hand-covered. The
  conjunction that was supposed to keep the direction fail-safe has nothing
  left to conjoin. That is not a corner case, it is the shape of a pot:
  **9 of the 10 fires this predicate ever produced read `arrivals == 1`**, and
  `flag_missed_2` (2026-08-23) is one of them with ground truth — a real shot,
  no hand contact at all, the countdown run into EXPIRED and restarted by hand.

  Every discriminator available was measured before reversing, and the two
  populations are identical in all of them: arrivals (1/1 both), hand-coverage
  (by construction), and episode mover travel — the labelled 2026-08-11
  carries read **30, 140, 159 and 238 mm** against that real pot's **252 mm**.
  Two independent removals (#38, and now this) have reached the same wall from
  opposite directions. **The next attempt must bring a new measurement, not a
  third arrangement of this one** — the acoustic strike-click of §4.4 is the
  standing candidate, and it is the reason this stays open rather than closed.
  (Superseded 2026-11-18, below: audio is disqualified for detection. The new
  measurement came from the camera instead, in #67.)

  Steven's 2026-08-11 requirement is not withdrawn, it is unmet: ball-in-hand
  still *should* not reset the clock. Refusing a real shot is simply the worse
  error — the countdown then runs through the stroke, the failure #33 exists to
  kill — so with no separating evidence the system fails toward counting the
  shot. The `ball_in_hand_*` corpus clips stay graded (on the stroke they must
  still count) as the labelled ground truth for whoever tries next.

- **2026-11-18 — Audio is disqualified for detection, permanently.** Steven:
  "OK let's permanently disqualify audio for detection on this evidence
  then". This ends the acoustic strike-click line: the 2026-07-27 "backlog"
  entry above, the optional corroborator of §4.2, the backlog item of §7 and
  the 2026-08-29 entry's "standing candidate". It was rejected once on
  2026-08-02 (`hardware.md` Part D) and reopened for #67 on 2026-11-18,
  after the quiet-room measurement below looked promising.

  The evidence (issue #67; the side camera's microphone as OBS records it).
  In the quiet practice room all 41 strokes of two sessions had a sharp onset
  at the strike, 37 of them within 30 ms. With real songs mixed into those
  recordings the strokes still heard fell to 80 % with the music very quiet,
  45 % at soft background level and 16 % at ordinary listening level, where
  4 % of the slower third were heard. The soft strokes go first, and the open
  cases turn on soft strokes (a tip bump, a soft shot that touches nothing).
  It is a mixing test: no recording has music in it, the volume labels are
  estimates, and talking was not tested.

  The clock is built for real matches, "where there would be talking and
  music" (Steven, the same day). There the reading would stand down for most
  of a match and come back in the gaps: one rule in practice and another in
  a match, and that is the kind he turns down ("unpredictable is worse than
  not supported", §15).

  What it leaves. Detection is the camera alone. The microphone stays for
  the spoken "extension" (`hardware.md` Part D item 1), which is a command,
  not detection. The 2026-08-11 requirement is still unmet outside a scratch
  (§15 covers the scratch): ball in hand should not reset the clock. #67
  holds a camera-only measurement for it (a cue ball moved by hand is never
  seen rolling), researched 2026-11-18 and parked: nothing there is built or
  approved.

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
- New failure captures are deliberate `record` takes. The Stream Deck Flag
  keys that saved live ring dumps were removed on 2026-11-15 (§4.4).

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

## 11. The minigames platform (issue #46) — withdrawn 2026-08-21

**This section is a tombstone. Do not reuse the number 11.**

Checkpoints 97–102 built a minigames platform on top of the hub: a game
registry with a Stream Deck picker row, a lag trainer, a shot/drill core that
judged where balls came to rest against diamond-cell targets, a shot catalog
with a phone-side editor, a `/games` browser source, and a marks layer that drew
target zones onto the cloth inside the rectified feed. All of it was reverted
wholesale in checkpoint 103 — Steven played it and did not want it. The
contract that used to live here went with the code.

Nothing in it touched `vision/`, so nothing in §4 or the corpus depends on it.
The CHANGELOG entries for 97–102 stay where they are; the git history is at
`revert/46-minigames`'s parent if any of it is ever wanted back. The heading
survives only because CHANGELOG entries 97–102 cite `§11.x` subsection numbers,
and this file's rule is that § numbers are never renumbered or recycled — a
future section appends as §12.

## 12. The session log and replay (issue #57, 2026-11-16)

Every `cueclock run` writes `sessions.nosync/<stamp>/` (the journal's stamp),
archived to `/Volumes/T7/cueclock_sessions/<stamp>/` after the session. It
replaces Flag's raw ring (checkpoint 114) with evidence that needs no button:

| File | What | Measured |
|---|---|---|
| `header.json` | git sha + dirty, cadence, stride, churn lag, `DetectConfig`, reference path + digest, the calibration's JSON, the hub's starting ruleset / mode / score / bracket, and a `started_mono`/`started_wall` pair that maps the two clocks | — |
| `detections/NNNNNN.npz` | per detect frame: camera `frame_i` (BlockID, so gaps are camera drops), `t`, `mono` (when the frame's stateful half ran, which is the hub's clock), balls + churn + intrusions in `detect_cache`'s array layout (`pack_frames`), and every rejected blob (`detect.Reject`: bbox, area, reason bits). ~10 s chunks, `savez_compressed`, each landing by rename | **189 B/frame, ~106 MB/hour** at 156 Hz; 17 µs/frame of writer time (6 corpus clips, 1800 frames) |
| `inputs.jsonl` | every non-vision hub input, `{"mono","wall","method","kwargs"}` | a few KB |
| `stills/` | raw Bayer PNGs (lossless), each with its churn-lag predecessor: at every ShotTaken its own frame and the one 0.5 s earlier (the table before the stroke; for a break, the racked pack, #59), at every BallsSettled, at every input, and (`rest`, #65) at every ball list written at rest | **1.62 MB, 40 ms per PNG** at compression 1; ~1.5 GB/hour, ~2.3 with rest stills |

**Rejected blobs** are the one detector change (`DetectConfig.export_rejects`,
off by default so corpus output is untouched, on in `run`). The gates already
compute everything a `Reject` carries, so it costs nothing measurable:
+0.03 ms/frame mean, +0.005 ms paired median, against a 4.8 ms `find_blobs`
(1800 corpus frames, interleaved on/off). 3.9 rejects/frame on average, 14 at
p99. It is deliberately not `Intrusion`, whose distance transform checkpoint
109 turned off for cost.

**PNG level** 1, from the 2026-11-16 measurement on three corpus frames:
level 0 is 11 ms but 2.31 MB; 1, 2, 3 and 6 all land at 1.61-1.62 MB for 40,
44, 45 and 66 ms. Bayer noise does not compress past the first level.

**Stills per hour** come from the 2026-11-16 journal (10.1 minutes: 22 strikes,
23 settles, 11 commands): 4 PNGs a strike, 2 a settle, 2 a command, 156 PNGs,
253 MB, so ~1.5 GB/hour. That makes stills, not detections, the session log's
bulk. If the T7 ever minds, crop them to the cloth's ROI (51% of the frame,
`table_roi_of`) before anything else.

**Off the detect loop.** The loop thread appends one `LoggedFrame` per frame
to a list and, every 10 s of camera time, hands the list to a writer thread
through a bounded queue (32 items: chunks and stills). Packing, compression
and PNG encoding all run on that thread, and zlib releases the GIL. When the
queue is full the item is dropped and counted, never waited for, and each drop
journals `session_log_drop`; the exit line `session_log` carries the totals.
The stills ring holds references (`grab.Array` is already a copy, which is why
it can outlive `Release` on the detect workers): ~1 s, ~360 MB. Inputs are
written synchronously on the asyncio loop, as the journal is: they are rare,
and replay parity needs every one. The live Hz check against the
`detect_heartbeat` baseline (p50 155.7) happens on the first session.

**One input seam.** `hub.hub_input` decorates every Hub coroutine an outside
caller reaches (API, deck, voice, ruleset / mode / score / bracket), and logs
only the outermost call (a ContextVar, since each API request is its own
task), so a bracket result's own `new_rack` is not logged twice. Score
mutations moved from `app.py` onto the hub for this. Vision calls are not
inputs: replay re-derives them, through the same `bridge.hub_calls` `run`
uses.

**Virtual time.** The hub reads its clock only through `now`/`wall` (and the
journal likewise), and the tick loop's body is the synchronous `Hub.tick()`.
`session replay` sets the clock to each frame's logged `mono`, each input's
`mono`, and a 0.1 s tick grid. It cannot reproduce the tick loop's phase or
the few milliseconds a vision event takes to cross to the asyncio loop, so
`verify` matches decision records by kind, state, rack_shots and verdict
fields, and `remaining` within two ticks (0.2 s).

**The self-check.** Under the code that ran live (`same_code`), replay must
reproduce every decision. A divergence there means the log is missing an
input, and it is reported rather than explained away. Under newer code a
divergence is what the change did, which is the point of keeping the logs.

**Limits.** A detector change makes old logs stale for detection questions
(they stay valid for tracker, events and hub work). Replay always runs from
the top (`--from`/`--to` filter the output) because tracker and clock state
need the history. A bracket input that needs an archived file replay does not
have journals `replay_input_failed` instead of failing.

## 13. The ball list (issue #54, 2026-11-17)

Which balls are on a still table, which one is the cue, and which number each
carries — read from one overhead frame, kept apart from the tracker.
`vision/ball_list.py`; measurements and the prototype behind every constant
are in issue #54.

**A separate output.** `read_ball_list(bayer, ReferenceModel) -> BallList` is
pure, like the vision core. Nothing it says reaches the tracker or the event
engine, so shot detection cannot regress through it and no golden moved when
it landed. Two of the detector's helpers became public for it
(`chroma_binary`, `ring_whiteness`); `detect_frame`'s output is unchanged.

**The fit.** The detector's own foreground (gray difference OR chroma), then
known-radius circle voting along each colour edge's gradient, inside every
component of 600 px or more — the blob outline is ignored, which is what
splits racks and balls merged with a stick. A circle is accepted when strong
(vote ≥ 2.5, outline coverage ≥ 0.5) or enclosed by four strong ones (the
black 8 inside a 15-ball rack). The radius is `ReferenceModel.radius_px` read
at the fitted centre. Two fits of the visible edge are used, each where it was
measured: the first (`7.82 + 0.492 r`) sizes the search, fill and colour disc;
the refit (`8.13 + 0.497 r`) sizes the cue ring and rim wrap.

**Labels.** Cue: rim whiteness ≥ 0.65 on a ring 0.70–0.97 of the edge radius
(corpus: the cue's lowest reading 0.72, a pole-up stripe's highest 0.57).
Otherwise the nearest of eight Lab colour centroids over the inner disc, and
a stripe when the disc is ≥ 20 % white or white wraps 10 of 36 sectors of a
rim ring 0.70–0.83 of the edge radius (16 sectors of a ring out to 0.95
until #79, below).
A ball with under 10 coloured pixels gets no number (`?`): a stripe resting
white cap up shows no colour, and calling it anything doubles a number.
`arm` is the detector's intrusion test on the same frame; a list with an arm
can show a ball is present, never that one is absent.

**When it is read.** In `cueclock run`, at every `BallsSettled` and about
once a second (`list_reads.REST_S`) while the engine reports no episode and no
motion and (#65) no detection churns over `spin_churn_max` — balls racked, lifted or put down by hand often raise no settle. The
read runs on the detect pool (corpus settles: p50 18 ms, p95 28 ms), and the
loop consumes results in the order they were asked for, so whether a list
counts as changed depends only on frames, never on which worker finished
first.

**Where it goes.** A settle's list is always written; a rest read only when it
differs from the last one written (`same_table`: same `arm`, every ball with a
same-label ball within a quarter radius both ways). Written means
`balls.jsonl` in the session log, keyed by frame (§12), plus a `ball_list`
journal line. Only written lists reach the hub, which keeps the latest one
with no arm for the strip — so a session and its replay see the same lists.
Since #63 (§14) a settle's list also has a say in the scratch hold, and
since #64 (§15) in ball in hand after a scratch.

**The strip.** A row of chips above the bar's left end in
`/shotclock?view=bar`: the cue, 1–15 in order, then a grey `?` per ball with
no number. Each number is on a list at most once (#65); there is no memory
across lists and no smoothing, so the strip's mistakes are visible. It is a test graphic, to be
refined once sessions show how often it is wrong; a switch on the control
page's Setup tab hides it.

**Judging it.** `session-stats` adds the list's own rows (two balls with one
number and a number returning within a rack are always errors); `cueclock
balls` re-reads a session's stills, or a clip, offline. The lists and stills
are debugging logs. Amended 2026-11-17 (checkpoint 130): the review page's
view of them was removed at Steven's request. He asks for timestamps when he
wants to see one, and reads them off the recording.

**Known limits.** A stripe showing only white gets no number until it rolls.
A solid 5 could read 13 (its orange rim passed the rim-wrap floor) until
checkpoint 152 (#79, below). Labels were
measured almost entirely on older mounts read with the 1115 colour
references. A cue in a pocket jaw reads 0.75–0.84 against the 0.65 floor. The
arm test misses some hands, and a hand holding the cue on the cloth can read
"no cue, no arm". A ball under a hand is unknown until the next list.

**Amended 2026-11-17 (checkpoint 131, #65): fewer wrong lists.** Measured
against ground truth: the 33 settle stills of `20261117-201731` (checked ball
by ball by eye), the 13 of `20261117-001351`, `all_balls_still_1115`, and the
162 corpus rests scored with no human (duplicates, unmoved balls changing
number, 15-ball frames reading exactly 1–15), all through `read_ball_list`
itself. Three changes, each in for a measured reason:

- *Each number once per list* (Steven's decision 3). `_label` read each ball
  alone; now every (ball, number) pair has a cost in typical margins —
  colour distance over the corpus's median colour margin (19.6 Lab), plus or
  minus half the stripe evidence (white fraction and rim wrap past their
  thresholds, over their median distances from them, 0.175 and 11 sectors) —
  and pairs are taken cheapest first. A ball with no rival keeps exactly the
  number it reads alone; the loser of a clash takes its next best, and the
  record keeps what it read as `own`. It fixed the one wrong settle of the
  live session (the solid 1 with the lamp's reflection, which read 0.21 white
  beside the real 9 at 0.57) and changed two corpus labels, both a hand read
  as a 14, where the real 14 now keeps its number. Still no memory across
  lists.
- *Rest reads wait for a quiet cloth.* The extra 10 at 604.5 s was Steven's
  bridge hand: for 0.2 s it came loose from the rail in the segmentation and
  passed as one ball-shaped blob, and a rest read landed inside it. Its churn
  read 3.9–14.5; no ball at any other rest read or settle of the session read
  over 2.3, under the engine's own still-ball ceiling (`spin_churn_max`, 2.5).
  A rest read now waits for every detection to be at or under it.
- *Rest stills* (Steven's decision 1): every list written at rest keeps its
  frame and churn-lag predecessor as a `rest` still (§12), so a wrong one can
  be re-read with `cueclock balls`. They live under the session log's
  existing rules: the shared 100 GB budget, pinning, the pinned budget, the
  writer's bounded queue.

The missing 8 at 710.6 s has a cause but no fix yet. It was under Steven's
arm and body as he shot (recording 11:46.5), inside a 57,000 px blob reaching
in from the rail that the hand test passed over (a blob that size can only be
dropped by the shadow test at its thickest point), so the list said no arm.
Calling every hand-sized blob from the rail an arm was measured and rejected:
over the session's strip it removes 11 s of wrong lists and adds 62, because
the player's shadow at the rail also qualifies and a stale list then stays up
(9 s with no cue after a scratch; 53 s of just `cue` from racking to the
break). The rest stills will hold the next one's pixels.

Not changed, with the reason: a coverage floor against the hands (all three
read 0.569, but a real 10 reads 0.597 and a wrist 0.653); colour fit (hands
16–17 Lab from the nearest centroid, real balls up to 30); the 5 read as 13
(#54's stricter rim test has no instance in either truth set to be confirmed
on); a stripe white cap up (0 of 259 live readings). Scores, before → after:
truth stills 32/33 → 33/33 lists exact; game sequence and all-balls
unchanged; corpus rests 2 → 0 duplicates, 3/615 unmoved flips and 8/8 full
tables unchanged. Measurements and scripts: issue #65 and
`~/cueclock-research/65-labels/`.

**A standing rack is not an arm (2026-11-18, checkpoint 135).** The hand
test asks whether a solid blob too fat to be a cue stick is on the cloth,
and a tight rack is one. So every list read with a rack standing came back
`arm`, the strip (which shows only arm-free lists) never showed a rack, and
before each break it sat on whatever it had read mid-racking: 8 of 15 balls
for 75 s and 10 of 15 for 35 s in session 20261119-020755. A list now also
says when every hand-sized blob on the frame is accounted for by the balls
fitted in it: `pack`. The share of a blob's area covered by its fitted
balls' discs, over all 218 hand-sized blobs in the stills of four sessions:
a rack standing alone 1.02–1.10 (19 blobs), a rack with hands or the
triangle on it 0.50–0.62, any other arm or hand at most 0.43; `PACK_COVER`
is 0.8, the middle of the empty band. The strip shows a list that is
arm-free or a pack.

`arm` itself is unchanged, and so is everything the clock concludes from a
list. The first build of this simply stopped calling a rack an arm, and the
corpus refused it: in `break_0801b` a hand is on the cue ball beside a
standing rack, the hand test misses the bare forearm (too thin for it), and
the only thing that had kept that list from saying "no cue, no arm, so a
scratch" was the rack's own flag. The hand test misses real arms; where a
rack stands, its flag was covering for that. Known and left: fingers on the
rail can still be read as one or two unnumbered balls for a second, which a
pack list now shows on the strip (one such list in that session).

**Amended 2026-11-23 (checkpoint 152, #79): the rim ring stops short of the
ball's edge.** Session `20261123-180144`, eight racks of 9-ball, read 20
numbers back within a rack. Read off the stills, 16 were the solid 5 called
13 (15 at one spot, where it lay untouched through six shots and read 5 on
some lists and 13 on others, and 1 at another), and 4 were Steven's bridge
hand read as a ball on a list with no arm.

The 5's inner disc was 3–6 % white, so the rim ring decided it. The ring ran
0.70–0.95 of the edge radius, and at the edge an orange or yellow ball blends
with the blue cloth into pixels (about B 128, G 128, R 168) that pass the
ring's floor of 110. An orange solid holds 10 such pixels inside 0.85 of the
edge and 28 outside it; a stripe holds white evenly at every radius. Against
a threshold of 16 the 5 at rest read 10–12 sectors when its whole-pixel
centre fell on column 1483 and 13–16 on column 1484. Over that session's
solids, orange read a median of 11 sectors (highest 16), yellow 8 (16), every
other colour 0–4 (12).

`RIM_WRAP_RING` is now 0.70–0.83 and `RIM_WRAP_STRIPE` 10. Measured on every
written list of the six sessions logged by then, on the balls this test
decides (inner white under 0.20), by the ring's outer bound:

| Outer bound | Highest solid (1,335 arm-free readings, 275 resting spots) | Lowest stripe (161 readings, 31 spots) |
|---|---|---|
| 0.95 | 17 | 12 |
| 0.84 | 11 | 12 |
| **0.83** | **8** | **12** |
| 0.82 | 8 | 11 |

0.83 has the widest gap and 10 is its middle. The 162 corpus rests, older
mounts that were not used to choose, read 9 and 11 on the new ring (15 and
16 on the old one): right on both, by one sector each way. Raising the floor
instead (#54's stricter test, floor 140 and 12 sectors) read 13 of the 161
stripes as solids.

`WRAP_SPREAD` stays 11 although the same median on the new ring is 8: at 8 a
hand read as a 14 takes the number from the real 14 on `power_shot` at
4.33 s, one of the two clashes checkpoint 131 settled.

Before → after, all through `read_ball_list`:

- #65's sets (1,837 readings: the truth stills, the game sequence,
  `all_balls_still_1115`, and 1,482 at the 162 corpus rests): every score
  unchanged, and one label differs, a solid 5 on `break_outrun_0805` that
  read 13.
- Numbers back within a rack: 20 → 4 in that session (the four hands),
  1 → 0 in `20261123-004951`, the other sessions unchanged (16).
- A ball that has not moved changing between a solid and its stripe on
  consecutive lists, all sessions: 45 → 25. The 20 gone are all the 5. Of
  the 25 left, 23 are the inner-white test on a list with an arm (below) and
  2 are a real 15 that lost its number to another circle for one list.
- The clock: the session replayed through the hub with the new lists, and
  again with the 15 lists that only the changing 5 had caused to be written
  left out. 0 of 464 decisions differ. The numbers never reach the clock's
  rules (§14, §15), but they decide which rest lists are written.

What it costs, from every ball whose number changes over about 1,000 logged
lists (31 spots, each looked at): better, the 5 at 3 spots and 6 real
stripes showing one white cap that had read as solids; worse, 3 solids with
a hand on them while racking, which now read as their stripes (lists with an
arm); and where a hand or glove is read as a ball, which real ball loses its
number shifts (the real 8 named 4 on 3 lists, a real 10 named 2 on 1). The
stripes that need this test are thin evidence, 31 resting spots and 8
readings of an orange one, so the first 8-ball session is the check.

Left alone: the hand read as a ball (checkpoint 131 measured two ways to
drop it and rejected both); and a 5 or a 1 lying number up in the rack,
whose inner white reads 0.198–0.216 against 0.20, so it changes number while
racking on lists with an arm. Six of the session's stills are in the corpus
(`stills_20261123-180144`). Measurements and scripts: issue #79 and
`~/cueclock-research/79-solid-stripe/`.

## 14. The ball list's say in the scratch hold (issue #63, 2026-11-17)

Steven: the 5 s post-scratch hold "almost never works". When today's logic
notices a scratch the hold is nearly always granted (16 of the 18 confirmed
scratches with a countdown live, over 50 sessions); what nobody could see is
how many scratches it never notices. A `ScratchCandidate` needs the tracked
cue lost *while moving* inside a pocket zone. The ball list (§13) asks a
second, independent question at every settle — was the cue on the table
before the shot, and is it gone now? — and both answers now decide the hold.
The policy is `server/scratch.py` (pure) plus the hub; detection, the tracker
and the event engine are unchanged, and no golden moved.

**Terms.** `now` is the list read at the settle. `rest` is the newest list
read at rest with no arm over the table, before the motion this settle ends
(its frame time is earlier than the episode's first frame). The cue was on
the table before the shot when `rest` holds exactly one cue, or a later
at-rest list with an arm over the table holds one. An object ball moved when
one on either list has none within 3 px on the other (9 unchanged corpus
settles shift at most 1.4 px; the smallest real move to catch is 8.6 px).

**The list calls a scratch** at a settle that counted a shot when (1) the
cue was on the table before it, (2) `now` holds no cue and no arm, (3) the
tracker holds no cue-grade ball (the adjudicator's witness test without its
birth gate, `EventEngine.cue_witnessed`, exported as
`VisionPipeline.last_settle_cue_seen`), and (4) it was not lifted by hand: an
object ball moved, or something travelled 400 mm or more (the engine's
`pickup_travel_max_mm`; scratches 474–1711 mm, hand moves 18–288 mm), or a
candidate is open for this shot, or (since checkpoint 140, §21) the cue ball
was seen rolling. (1)–(3) without (4) journals `lifted`: #64's
case, handled as before. **The list clears**: when it sees the cue, no hold
is granted for this shot and a running one is released, and a candidate that
arrives later for the same shot finds it cleared. **A refutation by today's
logic still stands** when the list holds no cue: the cue counts as present
if either one sees it. An arm over the table, a settle that counted no shot,
or no list: today's logic decides alone.

**A shot's scratch question** (`hub._ShotScratch`) lives for one motion
episode: from the latch rising (or a strike firing with no episode open) to
the next one. Either call makes the hold *owed*; it is granted the moment the
countdown is RUNNING — at the candidate, the settle, the settle's list, or a
reverted freeze — and stays owed through a confirmed verdict (on 2026-08-13
the verdict came 0.14 s into a freeze that was then reverted, and the
countdown resumed with no hold). One hold per shot. The next episode retires
the question with a `scratch_call` journal line: who called it (`candidate`,
`list`, `both`), whether it was held, and if not, why.

**Wiring.** The settle's hub call is not held for its list (my call on the
brief, which asked to hold every hub call up to 0.15 s): the settle runs
today's logic unchanged, and the list, 20–75 ms later, calls, clears or
leaves it. So no settle reacts later than before, and "no list" is today's
behaviour by construction. The cost is that a candidate the list clears
holds the countdown for those 20–75 ms, and a list-called hold starts that
much after the settle. (20–75 ms is the fit alone. Live, from the settle to
its list reaching the hub, the first two logged sessions measured a median
of 74 ms and up to 174 ms on a full table: §16.) A list that arrives after the next motion began is
journaled `no_list` and judges nothing. Lists reach the hub from the run
loop's own thread, posted behind the frame calls already dispatched;
`balls.jsonl` records that frame (`after`), and `session replay` feeds each
list back right behind it, so replay reproduces live's order exactly. The
corpus clock harness now dispatches through `bridge.hub_calls` (one copy of
the translation, not two), reads lists on the frames `cueclock run` would
(`list_reads.rest_due`), and caches them beside the detections
(`BallListCache`, keyed on `ball_list.py`, `detect.py` and the reference).

**The corpus, settle by settle**: the list calls exactly the five recorded
scratches (`kick_scratch_0811`, `pot_scratch_nohold_0811`,
`scratch_shot_0801`, `scratch_replace`, `shot_debug`), journals the hand lift
in `ball_in_hand_carry_b_0811` as `lifted`, and calls nothing at the other 59
of the corpus's 65 settles (71 clips). Three holds now start at the settle
instead of 0.63–0.64 s later when the candidate arrived; no other clock
outcome changed.

**Known limits.** A scratch with the clock idle gets no hold (all 7 journaled
no-clock scratches). An arm over the table at the settle leaves today's logic
alone (the `session-stats` row counts it). A cue that rolls under 400 mm into
a pocket touching nothing read as lifted until §21, which calls it a scratch
because it was seen rolling (none in the corpus); a cue slid
400 mm or more by hand and then lifted reads as a scratch. A hand holding the
cue on the cloth can read "no cue, no arm" (`ball_in_hand_nudge`), so after a
real shot that is a scratch call unless the tracker still holds the cue.

**Judging it.** `session-stats` rows: scratches by who called them (the
list-only row is the scratches the tracker never noticed), holds granted with
the reason for each one not, candidates cleared by the list, refutations
while the list held no cue, and settles with no list or with an arm. The
review page shows a `list_scratch` moment for every scratch the list called
alone and a `list_cleared` moment for every candidate it cleared.

## 15. Ball in hand after a scratch keeps the clock running (issue #64, 2026-11-17)

Steven, from real play: "if I pick up the cue ball with my hand and it leaves
the table, how do we keep the clock running, differentiate this from a shot
and differentiate it from a scratch". Until now every settle of a hand move
could count as a shot and restart the countdown: `scratch_replace` gave a
fresh 30 s with no hold when the cue went back down after its scratch.

**Ball in hand is a state, not a per-settle guess.** It begins when a shot
whose scratch stands (called by a candidate or the list, §14, and neither
refuted nor cleared) has counted its settle, and the next motion starts.
While it lasts, a settle that moved **only the cue** is placing: nothing
counted, a frozen countdown resumes at the remaining it froze with, a running
one and the scratch hold keep going. Placed, picked up, placed somewhere
else, nudged: the cue itself is never looked at. The first settle that moves
an object ball (or travels 400 mm or more, the engine's hand bound; since
§21 only when the cue ball was also seen rolling, so a carry stays placing
however far it went) counts as the shot and ends it; so do New Rack and a
refutation of the scratch.

"Only the cue" is #63's `object_moved` (3 px, measured there) between the
settle's list and the newest arm-free list read since the scratch (since
§21 asked one way, `object_missing`: a circle new on the settle's list is
not asked about). An arm
over the table at the settle is allowed: a hand on the cue hides the cue, not
the object balls. An arm hiding an object ball reads as moved and the settle
counts, which is the safe side.

**Why not judge each settle alone** (the first build of this issue, never
pushed): it needed the camera to see the table with the cue off it and no
hand in view between moves, and placing a ball by hand defeats exactly that
— the settle fires while the fingers are still on the ball. Steven: a rule
that works half the time is worse than none ("unpredictable is worse than
not supported"). The state needs only the object balls to stay put while he
places, which they do.

**The settle waits for its list during ball in hand.** §14 lets a settle
decide at once and its list follow 20–75 ms later; placing cannot work that
way without counting and then undoing (a fresh countdown flashing on the
wall, the count, pace and rack un-happening). So during ball in hand a settle
with an arm-free list since the scratch and travel under 400 mm holds its
clock decision until its list arrives. The clock meanwhile stays as it was.
A candidate, a refutation or any command reaching the hub first, or
`SETTLE_LIST_WAIT_S` (0.3 s since checkpoint 135; it was 0.15 s, which a
quarter of the lists read on a full table missed, §16) decides it as before.
The next motion or strike did too until §18.1: it now waits behind the
settle. The decision is made as of the
settle's own time, so a countdown it starts runs from the settle. Outside
ball in hand no settle waits. The clock refuses the suppression when a GRACE
or IDLE fire already committed a shot this cycle; the settle then counts
(`hand: refused`).

**Open play is unchanged** (Steven, 2026-11-17: "Last row is behavioral, the
rule is you wait for everything to settle before picking up the cue ball").
A cue lifted or moved by hand without a scratch before it counts as before.
Ball in hand after other fouls is not covered: nothing on the table says a
foul happened. (§21 changes this paragraph: since checkpoint 140 a cue ball
never seen rolling counts nothing in open play either.)

**The corpus**: `scratch_replace` 10.00 s is placing (no count, the hold from
8.45 s unreleased); every other clip's clock marks are identical to
checkpoint 132 (`HAND_ROWS` in `test_corpus_events.py`). No corpus clip holds
place → lift → place; the unit tests carry it. Session 20261117-201731
replayed: its one scratch was followed by a settle that moved only the cue
(it restarted the countdown live), now placing; the shot 9 s later counted.

**Journal.** `ball_in_hand` when it begins (with who called the scratch),
`ball_in_hand_ended` with `reason` (`shot`, `new_rack`, `refuted`) and how
many settles were `placed`. Every judged settle's `scratch_list` line carries
`row` (`ball_in_hand`, `shot`, `scratch`, `lifted`, `cue_moved`, `still`,
`arm`, `no_rest`, `other`) and, on `ball_in_hand`, `hand`: `applied`,
`refused`, `late` (decided before its list came) or `not_held` (clock idle
or off). An applied one journals `shot_provisional_reverted` (a freeze open)
or `compensation_suppressed` (none) with reason `ball_in_hand`, and
`rejected` on the settle record; a held settle's record says what decided it
(`held`).

**Known limits.** It starts only from a scratch the hub recognised (§14); a
missed scratch leaves placing counting as before (until §21, which keeps a
hand move off the clock without one). An arm or a hand-blob
phantom changing the object balls at the settle counts the placing as a shot
(restart). A ball-in-hand shot that touches nothing and travels under 400 mm
is placing (not counted). With no arm-free list since the scratch (an arm in
every read), placing counts as before.

**Judging it.** `session-stats` rows: ball in hand periods (and how each
ended), placings kept off the clock (with refused / late), and settles where
only the cue moved in open play. The review page shows a `placing` moment
for every one kept off the clock and an `in_hand_shot` moment where ball in
hand ended on a counted settle — Steven's shot, or a placing the list missed —
and a `cue_only` moment for every counted open-play settle that moved only
the cue, so the open-play case can be labelled hand or stick at the desk.
What would send this back: a placing that was a real shot (the worst
outcome), or an `in_hand_shot` that was still placing.

## 16. A freeze on a table where nothing moved is not a shot (2026-11-18)

The first session played for #66 (20261119-020755, 46 shots) held one
restart with no shot behind it. At 176 s Steven got down over the bottom
rail with 20.5 s left; his sleeve came to rest beside the ball standing at
the side pocket. The ball's blob merged into the arm's and was thrown out as
hand-scale, its last observation sat 30 mm off (enough to latch motion and
freeze the countdown), the track died, and the settle reported one ball
vanished beside a pocket. To the tracker that is a pot. The settle counted,
and the clock showed a fresh 30 s. `report` has always filed these as a
missed strike that healed itself: 48 of the 947 counted settles in the
journals since #33 had no strike, and in 17 of those no ball was seen to move
at all.

**The ball list can tell the two apart, and the tracker cannot.** The list
read at that settle (with his arm in the frame) found the cue and all twelve
object balls within 3 px of where the last arm-free list had them: the
journal already said `row: still`, 174 ms after the count.

**The rule.** A settle waits for its list, exactly as a placing does in §15,
when the countdown is frozen on a provisional (MOTION), the tracker's
rearrangement shows no ball displaced (`moved == 0`: balls it lost or found
are not evidence either way), it is not the pipeline's backstop settle, and
an arm-free list from before the motion exists. If the list then reads the
table unchanged (`scratch.nothing_moved`: the cue within `OBJECT_MOVED_PX` of
its spot, every object ball matched both ways) the freeze is released with
the time it froze at and nothing is counted. Anything else decides the
settle as before: a ball gone or moved on the list, a cue that moved, an arm
hiding a ball, a hand read as a ball, two cues, no list in
`SETTLE_LIST_WAIT_S`, or a command, candidate or refutation arriving first
(§18.1; §18.2 widens which settles wait; **§19 replaces the question the
list is asked: since checkpoint 138 only the cue ball has to be on its
spot**, so an arm hiding a ball, a hand read as a ball and two cues before
the motion no longer count the settle).

It is #37's refusal ("the table is provably unchanged") with a witness that
knows the balls apart, and it fails the same way: every doubt counts the
shot. #37 itself is untouched and still decides at once when the tracker's
own layout is unchanged.

**The wait is now 0.3 s** (it was 0.15 s). 0.15 s was twice the slowest fit
in #63's brief, which left out the wait for a detect worker. Hub time from
settle to list over the 81 settles of the two logged sessions: median 74 ms,
90th percentile 128 ms, 71–174 ms with 13 or more balls listed (median 142).
Four lists took longer than 0.15 s, all with nine or more balls listed, the
176 s one among them. That matters most for §15: a break scratch leaves ball
in hand on a full table, where two of the eight lists measured came later
than 0.15 s, and a placing whose list is late is decided without it and
restarts the countdown (`late`). 0.3 s is 1.7 times the slowest list of a
healthy session (the one slower list, 537 ms, came in a stretch of session
20261117-201731 where the detect rate had fallen to 68–89 Hz). Waiting costs
no clock time: the decision is made as of the settle's own instant.

**Evidence.** No real shot on record reads unchanged. The corpus has three
settles whose list reads `still`: two are the phantoms #37 already refuses
(`parked_vote_strike_0801`, `phantom_still_down_0811`) and the third is the
settle that opens `settle_stall_0801` before its one stroke. Session
20261117-201731 has none in 33 settles. Replayed through this rule,
20261119-020755 differs from its live journal in one decision: the 176 s
settle is refused, the countdown resumes at 20.44 s, the stroke three
seconds later fires at 17.45 s instead of a restarted 26.95 s, and rack 1
counts 20 shots instead of 21. Every stroke of that session then has its
own strike: the "missed strike" was never a stroke.

**Journal.** The settle record carries `held` and `rejected:
nothing_moved`; a `shot_provisional_reverted` line carries the same reason;
the `scratch_list` line reads `row: still`, `counted: false`.

**Known limits.** A settle the tracker saw a ball move in is not held (a
sleeve can also drag a merged blob without killing the track; the first one
on record came three days later, and §18.2 holds it when no strike fired). A
list later than the wait counts as before. Both leave a
`scratch_list` line with `row: still` and `counted: true`, which is its own
review moment (`no_change`) and its own `session-stats` row ("Restarted with
nothing moved", counted against clock accuracy), so the next one is seen
rather than filed as a healed miss.

## 17. A strike that leaves the rack standing is not the break (2026-11-18)

In the same session the clock started 19 s before the first break. Steven
set the cue ball down for the break and it rolled about 100 mm out of his
hand in view of the camera; the tracker read a strike at 6,520 mm/s; and out
of IDLE any strike over the 2,500 mm/s floor is the break
(`clock/modes.py`). He pressed New Rack. One more start in the 49 racks
journaled since mid-August reads the same way (2026-08-25: a strike just
over the floor, nothing rearranged, New Rack 15 s later).

The corpus already held one, filed as its opposite. `break_0801b` was
flagged on 2026-08-01 and pinned as "a clean 7104 mm/s break, exactly one
shot". Its frames show a nine-ball rack standing untouched for all 20.5 s
while he picks the cue ball up at 14.6 s, carries it round the rail and sets
it down at 18.5 s. The 7,104 mm/s was his hand taking the ball, he pressed
Flag 13 s after the clock had started on it, and for three months the
battery required the false start to count. Read the frames before pinning a
flag.

**The rule.** The strike still takes the clock out of IDLE at once, as it
must for a real break. Its settle then waits for its ball list (the wait of
§16) when the newest list read before the strike holds a rack: at least
nine numbered balls, the smallest rack in the rulesets, with or without an
arm in that frame. If every numbered ball of that list has a ball within
`OBJECT_MOVED_PX` on the settle's list (`scratch.rack_stands`), the rack did
not move and the strike was not the break: `ShotClock.break_reverted` puts
the clock back to IDLE with nothing counted, and the real break anchors the
rack when it comes. The comparison runs one way and ignores the cue, so the
cue ball put down somewhere else, a hand on it, or a hand read as a ball
changes nothing; a rack ball hidden or moved by 3 px or more, no rack on
the list before, or a list later than the wait, and the strike stands as
the break exactly as before.

**Why speed and timing cannot do this.** Both false starts read break
speed. A real break's settle has come as early as 1.4 s after its strike
(#59) and a false start's as late as 2.6 s (`break_0801b`). The tracker has
nothing to offer at these settles: a standing rack is one rejected blob, so
its baseline is empty (`baseline 0`, `involved 0`, the same reading a real
break gives when every pack track dies at impact).

**Evidence.** Every one of the six real break clips still counts once; the
corpus gate (`FALSE_STARTS`, `test_corpus_events.py`) requires `break_0801b`
to end IDLE with nothing counted and requires that no other clip ever has a
count taken back. Session 20261119-020755 replayed: the 58 s strike is
taken back 0.56 s later, the clock never starts, and the real break 19 s
later counts as the break with its 60 s window. With §16 that is the whole
difference from the live journal, two decisions.

**Journal.** `shot_provisional_reverted` with reason `rack_standing` (the
same record every other not-a-shot verdict writes, so `report` and the
review page need nothing new), and `rejected: rack_standing` on the settle
record, logged with the clock already back in IDLE.

**Known limits.** It needs the rack on a written list before the strike; a
strike in the first second after the hands leave a rack that no list has
read yet stands as today. A false start whose settle never comes was
confirmed by the frozen-clock watchdog until §18.3. A break that leaves all nine or more rack balls
within 3 px of where they stood would be taken back; that is a cue ball
that never reached the rack.

## 18. What the first session with scratches sent back (issue #69, 2026-11-21)

Session 20261121-153046 (27 minutes, 5 racks, checkpoint 135's code) was the
first with scratches in it. Steven judged its review moments at the desk;
five were wrong, and four of those come down to three mechanisms in the hub,
each reproducible from the session log. `detect.py`, `events.py` and
`track.py` are untouched.

### 18.1 A held settle is decided by its list, not by what the pipeline says next

§15 let "the next motion or strike" decide a settle that was waiting for its
list. During placing the hand is still moving when the settle fires. At
400.66 s a placing's settle was held; the motion latch rose 32 ms later and
decided it as a shot; the list came 47 ms after the settle and read the
table exactly as the last arm-free list had it. Ball in hand ended there
(`placed: 0`), so the next adjustment 1.4 s later restarted the countdown
again as open play. Of the ten settles held since checkpoint 133, nine were
decided by their list and this one by motion; none timed out.

**The rule.** While a settle is held, the pipeline's calls (motion started,
strike, discard, another settle, the confirmed scratch verdict) queue behind
it instead of deciding it, each stamped with the hub time it arrived at.
When the list comes, or `SETTLE_LIST_WAIT_S` after the settle, the settle is
decided and the queue is applied in arrival order, each call as of its own
arrival (`Hub._vision`). A queued settle that is held in its turn stops the
queue until its own list comes. What does not wait: the ball list itself; a
scratch candidate or refutation, which are evidence about the cue in the
held settle's own shot and decide it as before (§15: with a candidate open
it is no pick-up); and every operator command, because a press is
authoritative: it decides the held settle as before, the queue is applied,
then the command runs.

**Cost.** Only when a settle is held, a freeze right behind it can show up
to 0.3 s late on the wall. No clock time is lost: a queued freeze stops the
countdown as of when the motion arrived.

**Rejected:** "pre-empted during ball in hand, so do not count". A real shot
whose settle is followed within 50 ms by new motion would go uncounted, the
countdown would not reset, and it could buzz.

### 18.2 §16 also holds a freeze with no strike in which one ball moved

At 712.5 s he stood over a shot, the tracker displaced one ball 38 mm, no
strike fired, and the settle counted at once because §16 held only
`moved == 0`. The list 74 ms later read every ball in place (`row: still`,
`counted: true`): 25.4 s became 30 during his pre-shot routine.

A distance cannot separate it: real shots in the same journals moved one
ball 42, 44 and 49 mm. So the hold widens instead: a frozen settle also
waits when **no strike fired in its episode and the tracker moved at most
one ball**. The list decides exactly as in §16, refusing only a table read
unchanged. Over the three sessions with list rows (160 settles) that shape
occurred twice, and none of the 112 settles the list read as a shot reads
unchanged. A settle with a strike in it, or with two or more balls moved,
is decided at once as before.

### 18.3 The watchdog does not confirm a break over a standing rack

§17 lives in the settle's decision. At 453.9 s the cue ball set down for the
break read as a strike out of IDLE with a full rack on the list before it,
no settle came, and after 12 s the E9 watchdog synthesized one. A
synthesized settle carries no evidence and counts; here that confirmed the
false break and started a countdown on a racked table (he pressed New Rack
again to clear it).

**The rule.** When the watchdog fires on a strike that left IDLE as the
break, and the newest list before the strike holds a rack (`holds_rack`),
it takes the break back the way §17 does (`break_reverted`: IDLE, nothing
counted, the rack armed again) instead of synthesizing a settle. A break is
never 12 s quiet: its balls roll, and the longest roll-out on record is
8.62 s. Mid-rack the watchdog is unchanged. Four settles have ever been
synthesized; this is the only one at the break.

### Evidence, journal, limits

**Replayed** through this code, session 20261121-153046 differs from its
live journal in three decisions and nothing else (compared by frame): the
400.66 s and 402.06 s settles are placing (`hand: applied`; the countdown
keeps running from 24.3 s and ball in hand lasts until New Rack); the
453.9 s false start is taken back at the watchdog and the tracker's settle a
second later lands in IDLE; the 712.5 s settle is refused (`nothing_moved`),
the countdown resumes at 25.4 s and the stroke 5 s later fires at 20.5 s.
`review check`: the four verdicts behind these go from *still wrong* to
*changed*. The other sessions with a detection log replay unchanged.

**Journal.** Nothing new to read. A held settle's `held` is now `list`,
`timeout`, `input`, `candidate` or `refuted`, never `motion`, `shot`,
`settle` or `discard`; the calls that waited are journaled when they are
applied, right behind the settle's `scratch_list` line. The watchdog's
revert is `shot_provisional_reverted` with `reason: rack_standing` and
`by: watchdog`.

**Known limits.** A list later than 0.3 s still loses to the timeout, and
what queued is applied then. A real break over a rack list whose settle is
lost for 12 s with the motion bit low would be taken back; none is on
record, and E8's dark tier still counts a break that never fired. A cue
ball rolled or pushed by hand with no scratch before it is still open play
(#67, parked; §21 leaves it counting).

## 19. The cue ball on its spot means no shot was played (issue #73, 2026-11-21)

In session 20261121-153046 the countdown restarted twice with no shot
played: at 699.4 s (8.75 s became 30) and at 1565.2 s (23.66 s became 30,
five seconds after his one extension). Steven found both in the recording;
neither had raised a review moment, and checkpoint 136 replayed them exactly
as they ran live.

**Mechanism.** His gloved bridge hand resting on the cloth was read as a
ball by both readers. The ball list labelled it `cue` with no arm flagged
(white 0.61-0.64 against 0.97-0.99 for the real cue, coverage 0.667); the
tracker tracked it (699 s: `appeared 1`, a mover of 460 mm) or lost the
balls under the stick and arm (1565 s: `vanished 3`). Motion latched, the
countdown froze, no strike fired, and the settle was held for its list
exactly as §16 intends. The list then could not say "nothing moved", for
two reasons, either one enough: `RestLists.before` wanted exactly one cue on
the list before the motion and found two (`before: no_cue`); and
`nothing_moved` wanted every object ball matched both ways, while the
stick's shaft hid the 2 (1565 s) and the hand read as a `10` (699 s). Every
doubt counts the shot (§16), so both counted. In both, the real cue read at
the same pixel before and after: 0.0 px.

**The rule.** Every shot moves the cue ball. So a held settle is refused as
`nothing_moved` when its list holds exactly one cue and some cue on the
newest arm-free list before the motion stands within `OBJECT_MOVED_PX`
(3 px, about 5 mm) of it (`scratch.cue_px`, `scratch.nothing_moved`). The
object balls are no longer asked about, and a second "cue" on the list
before no longer voids the comparison: the nearest one is taken. Which
settles are held is unchanged (§16, §18.2: a frozen provisional in which the
tracker displaced no ball, or one ball with no strike), and so is
everything else that decides one.

**Measured** over every settle in the four sessions with ball lists
(20261117-201731, 20261119-020755, 20261119-022522, 20261121-153046) whose
list holds one cue and whose list before holds at least one, 114 settles:

| Cue moved | Settles | What they were |
|---|---|---|
| 0-1 px | 8 | none a shot: 4 already refused, the two §16 and §18.2 fixed (176.2 s, 712.5 s), and these two |
| 27.5 px or more | 106 | all 101 counted settles the list read as a shot among them |

Nothing lies between 1 px and 27.5 px.

**What it risks.** A real shot, held because no strike fired, whose cue
ball comes to rest within 3 px of where it started: it would not be counted
and the countdown would not reset. None is on record. It also means a ball
moved by hand on a frozen provisional, with the cue untouched, resumes the
countdown instead of restarting it; that was never a shot either.

**Not done, noted.** The list could also decline to call the glove a cue
(white 0.61-0.64 against 0.92 or more for every real cue on 257 arm-free
lists). That is two samples, unchecked against the corpus, and this rule
does not need it. `session-stats` counts such lists so the misread stays
visible. (Done the same day as §20: the list keeps one cue. No floor was
needed, and none would have worked.)

**Replayed** through this rule, session 20261121-153046 journals
`shot_provisional_reverted` with `reason: nothing_moved` at 699.402 s and
1565.199 s; the countdown resumes at 8.74 s and 23.66 s, and the strokes
3.0 s and 4.6 s later fire at 5.66 s and 18.96 s instead of a restarted
26.92 s and 25.3 s. The rack counts one shot fewer each time. One other
line differs: the settle at 1582.6 s, refused by #37 before and after,
changes `row` from `shot` to `still` (a hand read as a `14`; the cue had
not moved). The six other sessions with a detection log replay record for
record.

**Journal.** The `scratch_list` line carries `cue_px`: the distance from the
settle's cue to the nearest cue on the list before the motion, null when
either is missing. `row: still` now means the cue ball is on its spot,
whatever else the list read, so a refused settle reads `rejected:
nothing_moved` and `row: still` as before.

**Seeing the next one.** Three things in the journal's readers, so a count
with no shot behind it is not filed as a soft shot again:

- A review moment `no_strike` for every shot counted with no strike behind
  it (`report`'s "counted at settle" list): 100 in the 56 journals with
  shots, 1.8 a session. In the sessions with ball lists there were 6 and
  none was a shot. It merges into whatever else that settle raised and
  never gives the card its id, so verdicts given before it existed stay
  attached.
- `session-stats`: "Counted with the cue ball on its spot" (was "Restarted
  with nothing moved"): a counted settle with `cue_px` of 3 or less, or
  `row: still`. Always an error, counted against Detection. The `no_change`
  review moment is the same settles.
- `session-stats`: "Lists with two cue balls", arm-free lists holding more
  than one `cue`. Always an error: 2 in 257 across the four sessions, both
  these events.

**Known limits.** A settle that is not held (a strike fired, or two or more
balls displaced) is still decided at once; with the cue on its spot it
counts, and shows as `no_change` and in the stats row. A list later than
0.3 s still loses to the timeout. A journal written before checkpoint 138
has no `cue_px`, so the stats row there sees only the stricter old
`row: still`.

## 20. The cue ball once per list (2026-11-21)

Steven, on reading that §19 left the ball list calling a glove a cue:
"There should never be two cue balls detected." A table has one.

**What the second cue is.** 21 of the 549 lists in the four sessions with
`balls.jsonl` hold two. The issue counted the 2 with no arm flagged; the
other 19 are in session 20261119-020755, on lists with an arm in frame.
Read off their stills, none is a ball:

| Second "cue" | Lists | Rim whiteness | Inner white | Outline coverage |
|---|---|---|---|---|
| The white logo on a black sleeve | 19 | 0.66-0.89 | 0.86-0.93 | 0.74-0.93 |
| A gloved hand on the cloth | 2 | 0.71-0.75 | 0.61-0.64 | 0.67 |
| The cue ball beside them | 21 | 0.905-1.00 | 0.968-0.992 | 1.0 |

**Why not a higher floor.** The cue ball held in a hand, which the list
must keep reading as the cue (§14, §15), reads no better than the logo: rim
0.65-0.97, inner white 0.85-1.00, coverage 0.57-0.89 over the 10 such lists
in the same sessions. Any floor that drops the logo drops those.

**The rule** (`ball_list._one_cue`). When more than one circle passes the
cue test, the one with the whitest inner disc is the cue (rim whiteness
breaks a tie) and the others are left off the list. In all 21 the cue ball
is the whiter by 0.046 or more; the rim reading orders them the same way
but by as little as 0.012. The others are dropped, not read as object
balls: none was a ball, and the highest rim reading of any object ball in
the corpus is 0.57, under the cue floor. A list is still a pure function of
its frame.

**What it does not do.** One circle that is not a ball and passes the cue
test alone (the logo with the cue ball out of sight) is still listed as the
cue; none is on record. The same glove read as a numbered ball (`10` at
699.4 s) is untouched. The rule in §19 does not depend on any of this and
stays as it is: logged sessions replay from their `balls.jsonl`, two cues
and all.

**Evidence.** Each of the 494 logged lists that still has its frame was
read again from that frame: 472 identical, the 21 two-cue lists now hold one
cue at the cue ball's pixel with every other label unchanged, and one
differs for #65's reason (a list logged before that fix). Four of the stills
are in the corpus (`stills_two_cues`, two of each kind) with the cue ball's
position as truth.

**Journal.** `BallList.cues_dropped` counts the circles left off; it is in
the `balls.jsonl` record and on the hub's `ball_list` line when not zero.
`session-stats` reads it as "Lists with a second cue ball" (two `cue` labels
in journals before this): the row §19 added as "Lists with two cue balls",
now counted on every list, an arm in frame or not.

## 21. A cue ball moved by hand counts no shot (issue #67, 2026-11-21)

Steven: "If I'm moving the cue ball by hand it's because there was a foul
lol. So yes if I'm moving the cue ball by hand the clock should keep
running, that's the whole idea". Every rule set he plays gives ball in hand
anywhere after a foul, and the countdown runs through the placement in all
of them. Since §15 the clock knew that after a scratch it recognised. After
any other foul, or a scratch it missed, a hand move of the cue ball could
restart the countdown.

**The evidence: was the cue ball seen rolling** (`vision/roll.py`). A ball
that moved by itself rolls across the cloth in the open. A ball in a hand
never does: from overhead hand and ball are one blob. At every settle
`VisionPipeline.last_settle_cue_roll_s` reports how long a clean cue-white
detection (merged with nothing, this frame's own whiteness 0.70 or more)
stood more than 6 mm from every rest spot, over a window from 1 s before
the motion start (the frame before the latch's last run, or the first
strike, whichever is earlier; never before the previous settle) to the
settle. Rest spots: every such detection in the 50 ms at the window's start
and in the 50 ms up to the settle, and any spot such detections stood on
for 0.1 s or more between the window's start and the motion start. None
when no motion started since the previous settle (the pipeline's backstop
settle), and none counts as rolling. Positions, not speeds, and each frame's
whiteness, not the track's average (both alternatives were measured and
fail: the issue's research record). It is computed from detections alone,
so `session replay` and the corpus harness recompute it, and `bridge`
passes it to `Hub.balls_settled`. Every `vision_balls_settled` record
carries it as `cue_roll_s`.

| Settles | n | Seen rolling |
|---|---|---|
| Live, no shot behind them (four logged sessions) | 17 | 0.006 s at most |
| Live shots | 152 | 0.28 s at least |
| Corpus, the cue ball moved by hand | 6 | 0.013 s at most |
| Corpus, `tip_bump` (a 48 mm stick bump), the lowest stroke | | 0.19 s |

Under `ROLL_MIN_S` (0.1 s, `server/scratch.py`) the cue ball was not seen
rolling. The floor 0.70 holds where 0.60 does not (a glove passes and a
settle with no shot reads 0.134 s); 6 mm holds at 3 and 10; spots by
occupancy over the whole window lose `tip_bump`.

**Three places use it.**

- **Open play** (`scratch.moved_by_hand`, refusal `by_hand`). A settle with
  the clock not IDLE, not a #37 phantom, not the backstop, whose cue ball
  was not seen rolling, waits for its list (§15's wait, §18.1's queue) when
  an arm-free list from before the motion exists. After the earlier
  refusals (`ball_in_hand`, `rack_standing`, `nothing_moved`), it is
  refused when no object ball of that list is missing from the settle's:
  nothing counted, a frozen countdown resumes at what it froze with, a
  running one keeps going. No 5 s hold: the hold is for fetching the ball
  from a pocket. Placed spot to spot, lifted off, put down: all the same.
- **Ball in hand after a scratch** (§15). Placing is no object ball missing
  and travel under 400 mm **or** the cue ball not seen rolling: a carry
  stays placing however far it went (two live placings read 372 and
  374 mm). A cue-only settle that travelled 400 mm or more and was seen
  rolling still counts and ends ball in hand.
- **The scratch call** (§14, guard 4). Seen rolling is one more way the cue
  ball was not lifted: a cue ball that rolls into a pocket touching nothing
  under 400 mm is a scratch, where it read `lifted` before.

**The object question, one way** (`scratch.object_missing`, for the two
placing tests): some object ball on the list before has no ball within
`OBJECT_MOVED_PX` on the settle's list. Circles new on the settle's list are
not asked about, because a hand, a glove or a sleeve read as a ball is
always new. `rack_stands` (§17) already compares this way; `judge` keeps
the two-way `object_moved` for the scratch call.

**Which way it fails.** A real shot is refused only if its cue ball was
never in the open for 0.1 s and no object ball moved 3 px: the lowest
stroke on record reads 0.19 s. Every doubt counts the shot: no evidence, no
list before, no list in time, an object ball missing (a hand hiding one
too), a candidate or a command first.

**The corpus.** The four open-play hand settles stop counting
(`ball_in_hand_carry_b_0811` 13.35 s, `ball_in_hand_carry_0811` 15.43 s,
`ball_in_hand_triple_0811` 14.74 s and 16.04 s, row `by_hand`), so each
carry clip counts once, on its stroke (`CARRY_CLIPS`, asserted again); the
three stick settles that touched nothing count (`ball_in_hand_triple_0811`
12.46 s, `pickup_newborn_0801` 8.60 s, `tip_bump` 3.87 s). Every other
clock mark, row, list verdict and hold of the 69 clips is unchanged.

**Logged sessions.** The four with play replay decision for decision as
under checkpoint 139: every one of the 13 in-game settles with no shot
behind them was already refused by an earlier rule. The evidence reads
under 0.1 s at 16 settles and 0.28 s or more at 154; the 171st,
20261121-153046 at 886.74 s (racking), is the pipeline's backstop and
carries none, so it counts as before (the research had read it 0.000 s and
expected it to stop counting; since checkpoint 148 it is refused, §23). `review check` is line for line as at
checkpoint 139.

**Journal.** `cue_roll_s` on every `vision_balls_settled` record (null with
no motion to measure). A refused settle: `rejected: by_hand`, a
`shot_provisional_reverted` or `compensation_suppressed` line with that
reason, and its `scratch_list` line `row: by_hand` with `hand` (`applied`,
`refused`, `late`, `not_held`) as §15 journals it. `cue_moved` keeps its
meaning, a counted open-play settle that moved only the cue, which is now a
cue ball seen rolling. The review page shows every one kept off the clock
as a `placing` moment, worded without a scratch, and a counted `by_hand`
(late or refused) as a `cue_only` moment; `session-stats` splits "Placings
kept off the clock" into after a scratch and in open play.

**Known limits.**

- A cue ball rolled or pushed so that it travels on its own, by hand or
  with the shaft, was seen rolling and counts. During ball in hand after a
  scratch only one of 400 mm or more does. No in-game example is on record;
  the issue keeps two candidate readings for when one is.
- A hand or arm hiding an object ball when the settle's list is read makes
  that ball missing, and the settle counts.
- A misread on the list before counts the same way (seen once: the cue ball
  in a hand read as a `15` with no arm flagged, 20261121-153046 at 266.5 s,
  which #37 caught).
- No arm-free list before the motion, or a list later than 0.3 s: the rule
  does not apply.
- Ball in hand does not begin at a hand move: a short roll after the first
  placement in open play counts. No such sequence is on record.
- All of the evidence is Steven alone at the table. How another player
  handles the cue ball is not on record.

**What would send this back:** a hand move kept off the clock that was a
real shot (a `placing` moment marked wrong), or a hand move in a game that
restarted the countdown (a `cue_only` moment marked as a hand whose
`cue_roll_s` is under 0.1 s).


## 22. The clock's settings instead of rulesets (issue #74, 2026-11-22)

**What changed.** Until checkpoint 145 the clock ran on a *ruleset*: a named
bundle of a shot time, a time after the break, an extension length, whether
push outs are called, and whether there is a clock at all. Each of those is now
its own setting, plus the game being played, which no ruleset carried:

| Setting | Values | Replaces |
|---|---|---|
| `game` | 8ball 9ball 10ball other | new: which ball ends the rack (#72) |
| `shot` | 30 15 | `base_seconds` |
| `after_break` | same 60 | `first_shot_seconds` |
| `extension` | 15 30 | `extension_seconds` |
| `push` | off on | the PUSH key showing only under `wnt_9ball` |
| `clock` | on off | `Ruleset.timed` (`freestyle`) |

**Why.** Steven, 2026-11-22: *"what varies between them? Time per shot and
time after break? And push? What if we just had buttons on the deck for those
… Then there's no ambiguity and we don't have to worry about adding new
rulesets ever."* The measurements behind it (`~/cueclock-research/72-rack-end/`,
all 57 journals with play): the name never said the game (two 15-ball racks were
played under `wnt_9ball` for its 60 s after the break), `wnt_9ball` carried 67%
of all settles, and every `run` started on `ultimate_pool_8ball`, the one used
least, so the ruleset key was pressed in 36 of 57 sessions (91 presses).

**Decisions.**

- *Values are strings, and the lists are data* (`OPTIONS`). One URL shape and
  one JSON file cover all six; a 45 s clock is one more entry, never a type.
- *`after_break` is `same` or a number.* Ultimate Pool's last ten minutes is
  then one change (SHOT to 15), and #45's automatic switch becomes "SHOT drops
  to 15".
- *A change takes effect on the next countdown* (`ShotClock.change_settings`).
  A ruleset switch zeroed the rack and parked the clock, which mid-match would
  hand out the 60 s after-break window again and leave the clock waiting for a
  Start. A running shot now keeps its time and the rack its count; an IDLE
  clock shows the new number at once. Turning the clock on or off still parks
  it, as choosing `freestyle` did.
- *Remembered between sessions* in `config/settings.json`, written on every
  change, read at start, as the score is. Unreadable (an iCloud EDEADLK under
  launchd, CLAUDE.md) means the defaults and a journal line, never a failed
  start.
- *GAME is the only source for the game.* Nothing reads it off the table and
  nothing checks it against the table (Steven, during #72: "If we forget to
  switch the game we can simply switch and press start clock").
- *No presets.* The old names are not kept behind the API, unlike the issue's
  first draft: the deck and the control page were the only clients. They
  survive as `LEGACY_RULESETS` for one reader, `cueclock session replay`, so a
  log that recorded `set_ruleset` replays to the clock it produced live
  (`Hub.set_ruleset`, unreachable from any URL, keeps that input's name and its
  rack-zeroing behaviour). None names a game, so applying one keeps GAME.
- *Grace, post-expiry and the unenforced extension pool* are the same for every
  setting (`GRACE_SECONDS`, `POST_EXPIRY_SECONDS`, `EXTENSIONS_PER_RACK`).

**The deck** gets its first second page, the SETUP menu (`hardware.md` §C2b):
SETUP takes the ruleset key's cell and reads DONE while the menu is open,
SESSION and EXT keep their cells, and the menu closes itself 10 s after its
last press. Keys are looked up on the page that is showing, so an empty menu
cell does nothing, and a page change drops any hold in progress.

**Verified.** Every archived session with inputs (six) replays to the same
decision records under this code as under checkpoint 144: 726 records, no
differences, including the three sessions that logged ruleset switches.
Steven confirmed the open choices the same day: next-countdown semantics,
SHOT 30 / 15, a 10 s menu, DONE in SETUP's cell.

**What would send this back:** a settings change mid-rack that moved the
running countdown or the rack's shot count, or a session that did not start on
the settings the last one ended with.

## 23. On a live clock, a settle with no motion behind it counts no shot (issue #78, 2026-11-22)

In session 20261123-004951 at 39.03 s the countdown went from 25.0 s back to
30 and the rack from one shot to two, with no shot played. Steven's note on
the review card: "Getting down on a shot, none taken".

**Mechanism.** The settle was the pipeline's backstop
(`SETTLE_BACKSTOP_MIN_INVOLVED`, `vision/pipeline.py`): pixels moved, the
table sat quiet for the settle dwell, and the quiet layout differed from the
last one by five balls or more, with no strike and no motion latch in
between. The difference was five balls gone, none moved, none new. His arm
and body hid them: the lists read `arm: true`, the 1, 2, 7 and 14 left the
list, and the next settle read `appeared: 4`. The cue ball read (867, 758) px
before and after. The hub decided a backstop settle at once ("break-scale by
construction"), and with a countdown running a settle with no strike is a
missed shot: count one, start a fresh countdown.

The backstop was built for a break whose every track dies (`flag_missed_4`,
2026-08-06), and its floor of five was set against placing (one ball) and
racking (one to three). A body hiding five balls was not considered.

**The rule.** A backstop settle that arrives with the clock not IDLE and no
motion episode behind it (no latch and no strike since the settle before) is
refused as `backstop`: nothing counted, the countdown and a scratch hold left
as they are. It goes through the same branch as the list's refusals
(`Hub._decide_settle`), so the journal reads `vision_balls_settled` with
`rejected: backstop` after a `compensation_suppressed` with that reason. With
the clock IDLE nothing changes: break adjudication (E8,
`Hub._adjudicate_break`) decides as before. With an episode open the settle counts as
before: the engine never discards a fired episode, so this cannot happen
today, and refusing it could leave a clock frozen.

**Measured.** The 112 journals (2,520 shots) hold ten backstop settles.

| Clock when it arrived | n | What it was | What it did |
|---|---|---|---|
| Live | 4 | In play, 2: balls hidden (this one; 2026-09-18 12:59:43, six of seven balls gone 1.9 s after a real settle). Racking, 2: 2026-11-21 10:45:33 (five gone), 2026-08-13 13:53:11 (eight new, four by hand) | All four counted a shot and restarted the countdown |
| Idle, clock on | 4 | One real break (2026-08-11 14:47:42: one moved, seven new). Three racking (six gone each) | The break counted; racking did nothing |
| Clock off | 2 | Not examined | Nothing |

None of the four on a live clock was a shot. The one right call was a break
with the clock idle, which the rule leaves alone. Since checkpoint 88 the
engine settles a dark break itself (`test_break_dark_total_settles_at_the_rest`):
the 51 journals from 2026-08-12 on hold 9 adjudicated breaks, none through the
backstop, and 6 backstop settles, none a shot or a break. And the backstop was
never the net for a missed ordinary shot, since it ignores anything under
five balls. All four cases are solo practice; a match adds people leaning
over the table, which is the same shape.

**Each step of the real sequence.** Getting down on a shot with five or more
balls hidden: nothing now. Standing back up without shooting (five or more
balls back in view, the 2026-08-13 shape): nothing. Someone else leaning over
the table: nothing. Placing with ball in hand: one ball, under the floor, as
before. Racking on a live clock: nothing (it counted twice on record). The
break with the clock idle: as before. A break on a clock left live from the
last rack, with every track dead: no longer counted by this path; none on
record.

**Rejected.** Refusing only "balls gone, none moved, none new" covers three of
the four, leaves standing back up, and a sleeve tracked as a ball during the
cover turns one hidden ball into `moved: 1`. Holding the settle for its list
and refusing a cue ball on its spot (§19) fails when the stance hides the cue
ball. A higher floor separates nothing: the hidden-ball cases read 5, 5 and 6,
racking 6 to 8, the real break 8. Deleting the backstop was not proposed: with
the clock idle it has one catch and no false start on record.

**The list such a settle is compared with.** `SettleFacts.motion_t` was the
last episode's motion start whatever the settle, so a settle with no episode
was compared with the list from before the *previous* shot: its row read
`cue_px: 213.7`, `row: shot` for a cue ball that had not moved, and
`session-stats` (which takes "counted with the cue ball on its spot" off
Detection) read 100.00% for the session. A settle with no episode behind it
now carries its own time, so its list is compared with the newest arm-free
one before the settle: `cue_px: 0.0`, `row: still`. Of the 149 settles with a
list row, two had no episode, both backstop settles, both read `shot`, both
with the cue ball 0.0 px from the newest arm-free list before them.

**Logged sessions.** All eight replay decision for decision as under
checkpoint 147 (822 decision records) except at the two backstop settles on
a live clock: 20261123-004951 at 39.03 s (refused, row `still`, and the next
strike freezes at 21.6 s instead of 26.7) and 20261121-153046 at 886.74 s
(racking: refused, row `still`, and the next strike freezes at 5.5 s instead
of 18.9). `review check`: `no_strike-1837149548` goes from *still wrong* to
*changed*; `cue_only-1718069707` (judged right, "mid racking it's fine") now
reads REGRESSED on that 5.5 s, which is racking on a live clock and not a
regression; every other row is as before.

**Journal.** `rejected: backstop` on the settle, `compensation_suppressed`
with `reason: backstop`, and `report`'s "settles refused" names it. No review
card is raised: two of the four on record were racking (in-game only), and a
refusal of a real shot would show as a countdown that ran through it.

**What would send this back:** a `rejected: backstop` settle in a journal
with a shot behind it, which Steven would see as a countdown that did not
restart after a shot.
