# Minigames (#46) — implementation handoff

Design agreed 2026-08-20, no amendments. This doc is for the agent building it:
what to read, what the mockups fixed beyond the issue text, where the code
lands, and how the work gets verified. It adds to `CLAUDE.md` — everything
there (uv, corpus symlink, worktree recipe, contention rules) still applies.

## Read in this order

1. **Issue #46** — the spec: platform contract, diamond-grid zones, per-role
   pass criteria, the three role-identity tiers, capture flow, build order,
   out-of-scope list. Nothing here overrides it except the one deviation below.
2. **`docs/mockups/games-v1.html`** — the agreed visuals for every surface
   (open it in a browser). Where it and the issue text differ, the mockup wins;
   there is exactly one such spot (PLAY/NEXT, below).
3. **`docs/design.md`** §2.1 (API contract) and §13 (hub-policy strategy) —
   games are Hub-side event consumers; the vision core is untouched by
   construction. While building, give the platform contract a design.md home
   the way the bracket got §10 — appended, never renumbered.

## What the mockups fixed beyond the issue text

- **Games scene** = the Syphon overhead feed full-bleed (its 1920×1080 frame is
  an exact 2× fit on the 4K canvas) + **one** full-canvas `/games` browser
  source. No side panel, no side-angle camera anywhere in the scene.
- **The `/games` source has two postures**: opaque menu (cards, deck-order,
  each tagged with its key) while the picker is up; transparent-except-the-bar
  once a game owns the table.
- **Layer split, load-bearing for the build**: everything **on the cloth**
  (grid, ghosts, snapshot rings, zones, miss line, lag bracket) is drawn
  **server-side in the display pipeline**; everything **in the bottom bar** is
  the `/games` page. The bar reuses the live bar's plinth grammar — name +
  context left, stat lockups center, 290 px answer cell right; PASS fills the
  cell accent, FAIL fills it alarm (escalation by area, as everywhere).
- **On-cloth palette**: white = cue's markings, slate dashed = object ball's,
  alarm red = miss line only. Accent blue never touches the cloth (blue on
  blue cloth is invisible). One visual per role on every surface — TV, capture,
  phone.
- **Deck**: picker on **row 3** (one game per key from (3,1); the spine and
  dark-cell rules of layout v2 unchanged); the selected game's verbs replace
  the picker on the same row; **capture is a row-3 transient overlay** (the
  Flag reason-page mechanism). All games keys wear the MATCH blue stripe — no
  new stripe colour.
- **PLAY and NEXT are one key with two faces** (deviation from the issue's key
  list; the Replay-key precedent — the face says which press this is). RETRY
  is its own key, dark until a shot has been served: re-serving the *same*
  shot and serving *another* must never be one mispress apart.
- **ZONE is a cycle key** (current size over next: 1×1 → 2×1 → 1×2 → 2×2) and
  sizes the **most recently placed** target. SAVE stays dark until the cue's
  target exists, then arms with the auto-name on its second line ("SHOT 15").
  CANCEL discards the capture.
- **Ghosts drop at arm; the zone stays lit through the shot.** Per-ball
  offsets in the bar collapse to a quiet ✓ inside tolerance; arming is
  automatic when all balls are ✓ and settled.
- **Lag**: no arming — every strike→settle is an attempt; bad attempts are
  free because only the best matters. No grid on the cloth, just the
  measurement bracket off the head rail. LAST center, BEST in the answer cell.
- **Phone**: a third tab — Play · **Drills** · Setup. The list carries name /
  ball count / last played / make % (journal-derived). The editor renames,
  re-sizes and re-places zones on an 8×4 grid picker, serves a specific shot,
  deletes. It **never** edits ball or ghost positions — the table is the only
  positional input device.

## Where the code lands

| Piece | Home |
|---|---|
| `games` mode (fourth on the mode axis) | `clock/modes.py`; face in `deck/layout.py` `MODE_FACES` |
| Game registry + active game + WS state | `server/hub.py` — same seam the clock and `last_settle_movers` consume; registry follows the `RULESETS` pattern |
| `/games` page + game/drill API verbs | `server/app.py` + `server/static/games.html`, keeping the §2.1 contract style (deck talks HTTP) |
| Drills tab | `server/static/control.html` |
| Picker / verbs / capture overlay keys | `deck/layout.py` — and `docs/hardware.md` Part C **in the same commit** |
| On-cloth rendering (grid, ghosts, zones, miss line, lag bracket) | `display/pipeline.py` — it already owns the table image and the mm→display mapping, so orientation is correct for free |
| Games scene in OBS | a setup script (`scripts/setup_obs_bracket.py` is the precedent — scenes are never built by hand); Session-key automation learns the scene once |
| Journal kinds | `server/journal.py`: `game_started {game, players}`, `drill_armed`, `drill_attempt {shot_id, pass, misses: [{role, miss_mm}]}`, `lag_attempt {distance_mm}`; `server/report.py` derives per-shot make % |
| Shot persistence | one JSON per shot, definitions only (the bracket persistence pattern); zones as typed objects (`{type: "cells", ...}`) |

## Constants — assert, don't tune

Per the no-blind-thresholds rule, both new constants are asserted from
measurements already in the repo, anchor stated in code:

- **Ghost placement tolerance** (how close to a saved spot arms) — anchored to
  the tracker's stationary noise: smoothed-speed floor 7–22 mm/s raw,
  `settle_creep_mm = 5.0`, `moving_enter_mm = 30.0` in `events.py`.
- **Cell edge margin** (a ball resting on a cell boundary) — same anchors.

## Build order and rollout

Phases from the issue: **1** platform + lag → **2** drill core → **3** catalog
+ safeties + phone tab. Each phase ships **live in one commit** — no shadow
rollouts, no assessment sessions; Steven vibes-checks at the table. Never ask
him to classify a failure while playing — a wrong verdict in live use is a
Flag-and-triage item.

## Verification

- **Unit tests + ruff + mypy --strict gate this work. No cold corpus battery**
  — nothing in `vision/` changes. If you find yourself editing `detect.py` /
  `track.py` / `events.py`, stop: that's out of scope by design.
- Overlay pages are verified by **real render** (scratchpad WKWebView
  screenshot or OBS `GetSourceScreenshot`), including the never-painted state:
  the page must be correct in a source that has never painted — no entrance
  animations, nothing that depends on having been visible.
- After a deck layout merge: `launchctl kickstart -k gui/501/com.cueclock.deck`
  and verify the pid changed. Never install the Elgato app.
- No batteries or heavy jobs while a live session runs; if detection seems
  blind, check heartbeat `hz` first.

## Process obligations

- CHANGELOG checkpoint entry in every checkpoint commit; `Closes #46` fires
  only from a commit reaching `origin/main` — otherwise `gh issue close`.
- `docs/hardware.md` Part C ↔ `deck/layout.py` and `docs/cli.md` ↔ `cli.py`
  stay in lockstep, same commit.
- README's OBS sources list gains the Games scene.
