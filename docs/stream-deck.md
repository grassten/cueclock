# Stream Deck — control surface (living doc)

The physical control surface for a deliberately headless system. The Mac mini lives
behind the TV; there is no keyboard, mouse, or monitor in reach of the table. Every
interaction the room needs — clock control, session on/off, corpus capture, replay,
music — funnels through buttons that work with chalk on your hands.

> **This doc is the source of truth for the deck's current state.** Update the
> button map and status lines here in the same commit as any layout or daemon
> change (same rule as `progress.md`). The declarative layout in
> `src/cueclock/deck/layout.py` and the table below must never disagree.

**Status: phase 1 live (2026-07-31).** The `cueclock deck` daemon runs under
launchd (`com.cueclock.deck`) with the four API keys working; the session
toggle, replay, and music keys are phases 2–3 (build order below). Hardware:
Stream Deck MK.2, 15 keys, arrived 2026-07-30.

## Decision record

- **MK.2** chosen 2026-07-28 over Mini/+/XL (full comparison in git history of
  `stream-deck-plan.md`): 15 keys is enough, detachable stand leaves a flat
  wall-mountable slab, no wasted dials.
- **No Elgato app — in-repo controller daemon** (decided 2026-07-31). Steven's
  requirements: fully headless, no manual GUI setup, Claude maintains the buttons,
  layout versioned in the repo. The official app fails all four (GUI-configured,
  undocumented profile format). Instead, a small always-on `cueclock deck` daemon
  drives the hardware directly over USB HID via the mature
  [python-elgato-streamdeck](https://github.com/abcminiuser/python-elgato-streamdeck)
  library. Buttons are data in a Python module; changing the deck is a code edit.
  **Consequence: never install the Elgato Stream Deck app on this Mac** — it would
  claim exclusive HID access to the device and starve the daemon.
- **OBS control via obs-websocket** (built into OBS 28+, enabled in Tools menu) —
  replaces the Elgato OBS plugin. Music via `osascript` — replaces native media keys.
- **Session toggle owns cueclock `run` AND OBS** (decided 2026-07-31). The replay
  buffer keeps OBS continuously encoding all session — real constant load, and
  active outputs are when OBS takes the macOS power assertions that block sleep.
  Relaunch-into-presentation is solved (see session toggle below), so OBS dies with
  the session. TV power stays on its own remote (CanvasTV's VIDAA network API is
  pairing-fussy; revisit only if the remote annoys). The Mac manages its own sleep,
  pending the HID-wake bring-up check below.

## Button map — current state

Page 1 (only page for now). 5 columns × 3 rows; unlisted keys stay dark.
Positions are provisional until the deck is mounted and used in anger — shuffling
keys is a one-line layout edit.

| Pos (row,col) | Key | Action | Face | Status |
|---|---|---|---|---|
| 1,1 | **Session** | Toggle: start/stop `run` + OBS (hold 1 s to stop — no accidental mid-rack shutdown) | `OFF` / `STARTING…` / `ON` / `DEV` | planned |
| 1,3 | **Ruleset** | Cycle `ultimate_pool_8ball` → `final10` → `wnt_9ball` via `POST /api/ruleset/{next}` | active ruleset (`8-BALL` / `FINAL 10` / `9-BALL`) | **live** |
| 1,5 | **Replay** | obs-websocket: save replay buffer → replay scene → dwell → back | `REPLAY` | planned |
| 2,1 | **Reset** | `POST /api/reset` (fouls, end of rack/match, bug situations) | `RESET` | **live** |
| 2,2 | **Extension** | `POST /api/extension` (+15 s; rule exists in every ruleset) | `EXT +15`, flashes `+15 s` / `NO EXT` | **live** |
| 2,5 | **Flag** | `POST /api/dump?label=flagged` — last ~12 s of frames → corpus clip on T7 | `FLAG`, flashes `SAVED` / `NO T7` | **live** |
| 3,1 | **Play/Pause** | `osascript`: Music.app playpause | `♪` | planned |
| 3,2 | **Vol −** | `osascript`: output volume −10, hold to repeat | `VOL −`, flashes new level | planned |
| 3,3 | **Vol +** | `osascript`: output volume +10, hold to repeat | `VOL +`, flashes new level | planned |

Press outcomes flash as background color (green ok / red error) for 1.2 s — words
only, no ✓/✗ glyphs, so faces never depend on symbol font coverage. All keys
render dim while the server is down, and presses no-op.

Deliberately absent (per Steven, 2026-07-31): clock start/pause — reset covers the
bug cases; add later only if a real need shows up.

## Architecture

```
Stream Deck MK.2 ── USB HID ── cueclock deck (launchd agent, always on)
                                   ├─ HTTP → localhost:8000/api/*   (clock, dump, ruleset)
                                   ├─ obs-websocket → OBS           (projector, replay, quit)
                                   ├─ osascript                     (Music.app, volume)
                                   └─ subprocess                    (owns `cueclock run` lifecycle)
```

- **Package:** `src/cueclock/deck/` — `layout.py` (declarative key definitions:
  position, face, action), `daemon.py` (HID loop, face rendering, dispatch),
  `actions.py` (api / obs / osascript executors), `session.py` (the session state
  machine + process management). CLI entry: `cueclock deck`.
- **Deps:** `brew install hidapi`, then `uv add streamdeck pillow obsws-python`.
  Key faces are Pillow-rendered text (72×72 JPEG — the lib handles the encoding).
- **Always-on:** launchd LaunchAgent (`com.cueclock.deck`, `KeepAlive`), plist
  template + installer in `scripts/install_deck_agent.sh` (idempotent — re-run
  after moving the repo). Idle cost is a blocked HID read — negligible. TCC:
  **none needed** (verified 2026-07-31) — the deck is a vendor-usage HID device,
  which macOS doesn't gate behind Input Monitoring.
- **State-driven faces:** poll `GET /api/state` + `GET /api/rulesets` at ~1 Hz
  while the session is on; API keys render a dimmed "disabled" face when the
  server is down (pressing them is a no-op, not an error).
- **Resilience:** USB unplug → reconnect loop; daemon crash → launchd restarts it;
  server restart → faces recover from the next poll. The daemon is I/O glue like
  the server — the vision core stays pure.
- **Tests:** layout validation, action dispatch, and the session state machine are
  pure and unit-tested with fakes; anything needing the physical deck skips when
  no device is present (same pattern as clip tests without the T7).

## The session toggle (flagship key)

The one interface the room lacked without a keyboard. End state: walk to the wall,
press once, play; hold when done, lights out.

**Start** (press while `OFF`):
1. Spawn `cueclock run --rectify --reference tests/clips/empty_table_0729 --host 0.0.0.0`
   (the canonical command — todo.md header).
2. Poll `GET /api/state` until it answers (face: `STARTING…`).
3. Launch OBS: `open -a OBS --args --startreplaybuffer --disable-shutdown-check`.
   Ordering matters — the server must be up first or the overlay browser source
   loads blank (known gotcha).
4. Verify presentation via obs-websocket: fullscreen projector open on the TV
   display (command `OpenVideoMixProjector` if the "Save projectors on exit"
   restore didn't do it), replay buffer active.
5. Hold a display-sleep assertion (`caffeinate -d`) for the life of the session.

**Stop** (hold 1 s while `ON`): quit OBS gracefully via websocket/AppleScript
(never `kill -9` — that's what triggers the safe-mode nag), SIGINT the `run`
process (closes the pylon device — the camera stops streaming and cools), release
the assertion. Display sleeps; TV off by remote; Mac sleeps on its own schedule.

**`DEV` state:** if the API answers but the daemon didn't spawn it (a terminal
session), the face shows `DEV` and the toggle refuses to touch it.

## OBS one-time settings (manual, ~5 min, done once at build time)

- Tools → WebSocket Server: enable, note the password → gitignored
  `config/obs_websocket.json`.
- Enable the replay buffer (Settings → Output; length ~30 s).
- General → "Save projectors on exit" (restores the fullscreen projector on the
  TV at launch).
- **Remove OBS from Login Items** if present — the session toggle owns its
  lifecycle now.

## Corpus-clip flow (Flag → triage)

The capture path for every detection bug, including the open missed-ShotTaken one.

1. **At the table:** anything wrong or interesting → press **Flag** within ~12 s.
   The ring buffer lands as a labeled clip under `tests/clips/` on the T7. One
   generic label is enough — triage sorts out what each clip actually shows.
2. **Later, off the table:** Screen Sharing (or SSH) into the mini, open Claude
   Code in this repo, run **`/triage-clips`** — a project skill (to be written,
   `.claude/skills/triage-clips/`) that: finds clip dirs not yet in the corpus
   scoreboard, replays each through the vision core, prints the event timeline
   vs. the clock's behavior, and drives the diagnose→fix→regrade workflow (the
   same shape as the settle-bug sessions). Each clip ends as: promoted to a
   graded corpus scenario, kept as evidence, or deleted.

## Build order

1. ✅ **Phase 1 — daemon + the four API keys** (Reset, Extension, Ruleset, Flag)
   — **DONE 2026-07-31** (checkpoint 19). Both verification items turned out to
   already exist: dump labels auto-suffix (`_unique_dir` in cli.py), and a
   missing T7 surfaces as a 503 the Flag key renders as red `NO T7`.
2. **Phase 2 — session toggle**: session state machine + process ownership +
   OBS launch/quit + the one-time OBS settings above.
3. **Phase 3 — Replay + music keys** (obs-websocket is already wired by then;
   music is three osascript one-liners).
4. **Phase 4 — `/triage-clips` skill.**

## Bring-up checks (empirical, at the table)

- [ ] Does a deck keypress wake the sleeping mini? (HID wake is device-dependent.)
      If not: `pmset` the system to never sleep — an M2 mini idles ~5 W, and the
      session button must always be live. Display sleep stays enabled regardless.
- [ ] `pmset -g assertions` clean after session stop (nothing left holding the
      machine awake — the reason OBS is in the toggle).
- [ ] OBS relaunch restores projector on the TV + replay buffer running, twice in
      a row (once after graceful quit, once after a simulated crash).
- [x] ~~Input Monitoring grant~~ — resolved 2026-07-31: no TCC prompt at all
      (vendor HID); the launchd agent runs without any grants.

## Backlog (build only when asked)

- **Ground-truth mode** (accuracy-ideas §8): a "real shot" key + auto-dump on
  disagreement — shadow-mode grading; every session grows the corpus. The Pedal
  (~$90, 3 foot switches) if marking shots must not take a hand off the cue.
- **Score/match page**: score keys need server endpoints + overlay element first.
- **Health face**: a key face showing grabs/s / clock state / drift warning.
- **Playlist keys**, now-playing face; tune the ±10 volume step to the amp.
- **Replay polish**: branded stinger transition into/out of the Replay scene
  (needs an alpha-channel animation asset), 50% slow-mo playback, and a REPLAY
  banner. Day-one replay is a plain cut to the Replay scene and auto-return.
  Note: the replay buffer records the program mix, so the shot-clock overlay is
  burned into replays — the banner (or covering that corner in the Replay scene)
  is what stops a replayed clock reading as live.
- **TV power via VIDAA network API** — only if the remote ever annoys.
- Deck **placement + mount**: plan is break-end wall at hip height (USB 2.0
  extends fine to ~16 ft; ships with stand + ~5 ft cable). Decide after a few
  sessions of desk-stand use; Command strips or a printed cradle when it moves.

## Music notes (carried from the original plan)

Mac mini pairs to the Bluetooth amp as a normal audio output; Music.app plays an
Apple Music playlist; deck keys drive it via `osascript` (`playpause`,
`set volume output volume N`, `play playlist "Pool Room"`). One caveat to test:
some BT amps need a re-pair nudge after power-off — if it bites, the permanent
fix is a cheap USB DAC → amp line-in, which also kills BT latency/dropout risk.
