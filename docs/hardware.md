# Hardware & rig reference

Everything physical and everything between the sensor and the TV: camera
bring-up and locked settings (Part A), the video path into OBS (Part B), the
Stream Deck control surface (Part C — a living doc: keep it in sync with
`deck/layout.py`), the microphone plan (Part D), and the TV + post-move
recalibration recipes (Part E). References use the form
"`hardware.md` camera §5" — part name + section number.

Machine: Mac mini M2 (Mac14,3, 24 GB, macOS 26.x) — dev and production, runs
OBS, drives the TV over HDMI directly. Full original bring-up docs (including
superseded planning and the hardware build-sheet PDFs) are archived outside the
repo in `~/Documents/cueclock-docs-archive/`.

---

## Part A — Camera (Basler ace 2 a2A1920-160ucBAS)

### A1. The kit

| Item | Exact P/N | Notes |
|---|---|---|
| Camera | **a2A1920-160ucBAS** | 1920×1200 @ 160 fps, global shutter, color, USB3 Vision |
| Lens | **Kowa LM4NCL** (3.5 mm f/1.4, C-mount) | wide enough to cover the table from the ceiling |
| Tripod plate | Basler 2200000314 | ¼-20 thread axis perpendicular to the lens axis |
| USB cable | 5 m passive USB 3.0, locking Micro-B → Type-A | straight into a Mac mini USB-A port, never a hub |

Bus-powered (~3.6 W). Assembly: plate on a side face; lens hand-tight into the
C-mount; focus/iris locking screws stay loose until final mounting height.

### A2. Software install

- **pypylon** (26.7+) ships native macOS arm64 wheels and — verified at
  bring-up 2026-07-27 — enumerated and streamed the camera **without** a
  system-wide pylon Suite install (the wheel bundles enough of the runtime).
  If a future pypylon regresses, the pylon Camera Software Suite from
  [baslerweb.com](https://www.baslerweb.com/en/downloads/software/) is the
  fallback (arm64-native).
- ⚠️ **No pylon Viewer on Apple Silicon** ([Basler docs](https://docs.baslerweb.com/knowledge/running-pylon-on-apple-silicon)):
  the GUI tools don't exist for arm64. All verification goes through
  `scripts/live_view.py` — which also proves the exact stack production uses.
- **No TCC camera permission needed** — a USB3 Vision camera is not an
  AVFoundation device; if Python can enumerate USB, it can talk to the camera.

### A3. Bring-up

`uv run python scripts/live_view.py` opens the camera at full rate, debayers
for display, prints the achieved grab fps, and has hotkeys for exposure and a
SHARP focus metric. Key facts, all verified 2026-07-27:

- The 8-bit Bayer format is GenICam **`BayerRG8`**; OpenCV names Bayer
  patterns from the opposite corner, so decode with **`cv2.COLOR_BayerBG2BGR`**
  (verified against the blue cloth — the "obvious" `COLOR_BayerRG2BGR`
  renders white walls blue and the blue cushion yellow). Settled; use BG
  everywhere.
- **156.25 fps, not 160**, with zero incomplete frames: the camera's default
  `DeviceLinkThroughputLimit` (360 MB/s) caps it, not the host controller.
  Fine for v1.
- Grey-ish color before white balance is set is normal (linear sensor output,
  gains at 1.0).

### A4. Locked production settings (UserSet1 = power-on default)

| Setting | Value | Note |
|---|---|---|
| PixelFormat | BayerRG8 | decode with `cv2.COLOR_BayerBG2BGR` (§A3) |
| ExposureTime | 6000 µs | at the ceiling for 156 fps; light budget is tight |
| Gain | 12.0 | ⚠️ applies as ≈×2, not ×4 — firmware quirk below |
| AcquisitionFrameRate | 160 → 156.2 actual | throughput-limit cap; fine |
| BalanceWhiteAuto / BslLightSourcePreset | Off / Off | ⚠️ factory defaults are Continuous / Daylight6500K — both actively fight manual color; set Off before any ratio writes |
| BalanceRatio R/G/B | 2.005 / 1.0 / 1.893 | measured against white paper laid on the cloth (never balance on the bare cloth — grey-world AWB neutralizes the blue) |
| Iris | ~1 stop down from wide open | the perimeter LED meters ~8× below the build sheet's assumption; one stop of optics + ×2 gain instead of f/4–5.6. Table plane ⇒ DOF is a non-issue |

**Firmware quirk (measured):** `Gain` and `BalanceRatio` both apply at ~half
strength in log domain — gain "12 dB" yields ×2 signal; a balance ratio of
2.005 applies ≈×1.42 (√). Never trust the units; set by measurement.

Persist with `UserSetSelector = "UserSet1"; UserSetSave.Execute();
UserSetDefault = "UserSet1"` so settings survive power cycles.

### A5. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| No camera found | Cable not fully seated; try the other USB-A port; never a hub |
| Frame rate far below 156 | Exposure > frame period; `AcquisitionFrameRateEnable` unset; USB 2.0 negotiation — check `DeviceLinkSpeed`, reseat cable |
| Sporadic dropped frames | Raise `MaxNumBuffer`; close USB-heavy apps; keep the Mac awake |
| Image black | Lens cap; iris closed; µs-vs-ms exposure mix-up |
| Colors wrong | Debayer constant — §A3 |

---

## Part B — Video output (camera → Python → Syphon → OBS → TV)

OBS cannot use the Basler directly (USB3 Vision, not UVC; and the CV app must
own the camera exclusively), so `cueclock run` republishes processed frames
locally via **Syphon** — a GPU texture handoff, not streaming.

### B1. Pipeline

```
grab thread (BayerRG8 @ 156 fps)
  ├─ detection path (every 2nd frame ≈ 78 Hz, distorted pixels)
  └─ display branch @ ~30 fps:
       debayer (COLOR_BayerBG2BGR_EA, 0.22 ms)
       → cv2.remap undistort (rails-fit map, ~2 ms)
       → Syphon publish (Metal texture)
  ⇒ OBS "Syphon Client" source → scenes → Fullscreen Projector on the TV
```

- **Quality debayer belongs here, not in detection**: edge-aware (`_EA`) is
  effectively free at display rate; VNG is slower *and* not better in OpenCV.
- **Full-frame undistortion runs only here** — detection undistorts
  coordinates, not pixels (`design.md` §4.1).
- Undistort coefficients come from the table's own rails
  (`cueclock fit-undistort`): the cloth/cushion boundary is straight in
  reality, so fitting Brown k1/k2 needs no printed target. Display-grade only;
  refit after every camera move (`config/display_undistort.json`, gitignored).
- **Square pixels are forced in the remap** (`alpha=0`'s optimal matrix is
  anisotropic — would render balls ~14% elliptical).
- Crop to the table in OBS (source Transform → Crop), not in Python.

### B2. Syphon integration facts (learned 2026-07-27, `display/syphon_out.py`)

- The publisher **must pump its NSRunLoop** while publishing or the stream is
  invisible to every client; `SyphonOutput.publish()` pumps, doubling as the
  frame-pacing sleep.
- Numpy frames go in **RGBA byte order** (BGRA rendered the blue cloth tan).
  `replay --test-pattern` shows labeled RGB bars whenever this needs re-proving.
- **Publisher restarts self-heal**: `publish()` re-broadcasts the Syphon
  announce every 2 s, so OBS reattaches within ~2 s of a Ctrl-C + relaunch.
  If a source stays black: check the publisher actually started (concurrent
  `uv run` invocations can block on uv's lock — one cueclock command at a
  time), then reopen source properties and re-pick.
- **OBS version caveat**: Syphon Client broke in OBS 32.0.x
  ([#12684](https://github.com/obsproject/obs-studio/issues/12684)); this Mac
  runs 32.1.1 which includes the fix. Pin the version — it's production
  equipment now.
- Fallback if Syphon ever misbehaves: pyvirtualcam → OBS Virtual Camera
  (brittle path, historically lags OBS's camera-extension changes).
  NDI/RTSP rejected — network machinery this one-machine setup doesn't need.

### B3. OBS setup

**Sources:**
- Syphon Client — the overhead table feed.
- Video Capture Device → **Camo Camera** — the iPhone side angle over USB,
  owned by OBS directly (Camo Studio on the mini + the Camo app on the phone,
  manual focus/exposure/WB locks). It replaced the wireless **iPhone
  Continuity Camera** source on 2026-08-01: Continuity looked washed out with
  blotching (iPhone auto tone-mapping/AF, no manual controls) and it died
  mid-session once without reconnecting, which cost two triage passes chasing
  a vision bug that wasn't one (CHANGELOG 2026-08-01).
- Browser source `http://localhost:8000/shotclock` — the corner clock overlay
  (transparent, ~200×200; enable **"Control audio via OBS"** so the chimes
  reach the TV speakers).
- Browser source `http://localhost:8000/score` — the match scoreboard
  (transparent 1920×1080; the bar sits bottom-center, broadcast lower-third
  style). No audio of its own. Names/race-to are set from `/control`; scores
  from the Stream Deck score keys. Hidden until the server answers, so a dead
  source shows nothing rather than a broken box.

**Scenes** (scene collection `Untitled`; names below are what the rig
actually runs — the original plan called them *Table* / *Clock*):
- **Stream Detail** (default, the live table scene): Camo side angle + Syphon
  overhead + scoreboard + corner clock overlay. The scoreboard's eye icon is
  the match/practice toggle — no separate *Match* scene.
- **Shot Clock** (full-screen shot clock, readable from anywhere): a browser
  source `http://localhost:8000/shotclock?view=full&audio=off` sized to the
  full canvas (1920×1080), scoreboard alongside. It renders just a huge bare
  number — broadcast style — transparent background over the scene's black;
  add a color/shape source behind it only if a panel color is ever wanted.
  `audio=off` because the corner overlay already sounds the cues (OBS keeps
  non-visible browser sources running, so two sounding sources would chime
  twice). The number is browser-rendered text: sharpness comes free at any
  source size.
- **Canvas**: standby card shown between sessions (still image, no cueclock
  sources).
- *Replay*: replay-buffer playback — not built yet (Stream Deck phase 3).

**Settings:**
- On every cueclock browser source, tick **"Refresh browser when scene
  becomes active"**: if a source first loads while the server is down, OBS's
  embedded browser parks on a silent error page and never retries (the WS
  reconnect only helps *after* a successful page load) — the checkbox heals
  it on the next scene switch. Same reason the start order is server first,
  then OBS (enforced automatically once the §C4 session toggle is built).
- Canvas 1920×1080 — **settled 2026-08-02** (judged on the TV): 1080p crop,
  not 1200p letterbox. Keep color range consistent end-to-end (Full/Full or
  Limited/Limited — a mismatch shows as crushed blacks on the blue cloth).
- Grade on the Syphon source with Color Correction or Apply LUT;
  starter LUTs in `assets/obs/` (`cueclock-tone.cube` is the dialed-in table
  grade; the `-ccm` variant is richer but amplifies chroma noise ~3×).
  Judge the grade on the actual TV, not the Mac monitor.
- Output: right-click program → Fullscreen Projector on the TV display;
  General → "Save projectors on exit".
- Replay Buffer enabled (Settings → Output, ~30 s) — Stream Deck phase 2/3
  owns the lifecycle.

Latency budget end-to-end (camera → TV in Game Mode): on the order of 100 ms —
well under the "few hundred ms is fine" bar; the detection path never leaves
the Python process and is unaffected.

---

## Part C — Stream Deck MK.2 (living doc: must match `deck/layout.py`)

The physical control surface for a deliberately headless system — the Mac mini
lives behind the TV with no keyboard in reach. **This part is the source of
truth for the deck's current state**: update the button map here in the same
commit as any `src/cueclock/deck/layout.py` change; the two must never
disagree.

**Status: phase 1 + match keys + reason-coded flags live (2026-08-01).**
`cueclock deck` daemon runs under launchd (`com.cueclock.deck`) with thirteen
API keys working; session toggle and replay are phases 2–3 (see `BACKLOG.md`).
The match-mode update (WNT 9-ball hosting) took row 3 for scoring, displacing
the planned music keys to `BACKLOG.md`. The flag split (2026-08-01) took the
right half of row 2, moving Force 30 to (2,2), New Rack to (3,3), and the
planned Replay key to (1,2).

### C1. Decision record

- **MK.2** (15 keys) chosen 2026-07-28 over Mini/+/XL: enough keys, detachable
  stand leaves a flat wall-mountable slab, no wasted dials.
- **No Elgato app — in-repo controller daemon** (2026-07-31). Requirements:
  fully headless, no manual GUI setup, Claude maintains the buttons, layout
  versioned in the repo. The official app fails all four. The daemon drives
  the hardware over USB HID via
  [python-elgato-streamdeck](https://github.com/abcminiuser/python-elgato-streamdeck);
  buttons are data in a Python module. **Never install the Elgato Stream Deck
  app on this Mac** — it would claim exclusive HID access and starve the daemon.
- **OBS control via obs-websocket** (built into OBS 28+); music via
  `osascript`. TV power stays on its own remote (the CanvasTV VIDAA network
  API is pairing-fussy).
- **Session toggle owns `run` AND OBS** (2026-07-31): the replay buffer keeps
  OBS continuously encoding, and active outputs hold macOS power assertions
  that block sleep — so OBS dies with the session and relaunches into
  presentation on start.

### C2. Button map — current state

Page 1 (only page). 5 columns × 3 rows; unlisted keys stay dark.

| Pos (row,col) | Key | Action | Face | Status |
|---|---|---|---|---|
| 1,1 | **Session** | Tap while `OFF`: spawn `run` + a `caffeinate -d` assertion, then launch OBS as soon as the server answers. **Hold 1 s** while `ON`: quit OBS (AppleScript), SIGINT `run`, release the assertion. The face is the state, and it stays lit with the server down — it is what starts the server | `OFF` / `STARTING…` / `ON` / `STOPPING…` / `DEV`; `HOLD…` while held | **live** |
| 1,2 | **Replay** | obs-websocket: save replay buffer → replay scene → back | `REPLAY` | planned (phase 3; displaced from 2,2 by the flag reorg) |
| 1,3 | **Ruleset** | Cycle rulesets via `POST /api/ruleset/{next}` | `8-BALL` / `FINAL 10` / `9-BALL` | **live** |
| 1,4 | **Push** | `POST /api/push` — push out called: clock restarts at the 60 s first-shot time and the window re-arms for a pass-back (9-ball only, so the face stays static) | `PUSH`, flashes `60 s` / `ERROR` | **live** |
| 1,5 | **Extension** | `POST /api/extension` — face/flash follow the active ruleset (Ultimate Pool 15 s; WNT 30 s, WNT Event Rules §5). Top-right corner so a player under the clock finds it without hunting (moved from 2,2 on 2026-08-01) | `EXT +15` / `EXT +30`, flashes `+15 s` / `+30 s` / `NO EXT` | **live** |
| 2,1 | **Reset** | `POST /api/reset` (fouls, bug situations; does not refill extensions) | `RESET` | **live** |
| 2,2 | **Force 30** | `POST /api/restart` — missed auto-reset recovery: clock restarts and runs immediately (rack-aware: 60 s in the first-shot window). Beside Reset: the clock-recovery pair (moved from 2,4 on 2026-08-01) | `FORCE 30`, flashes `GO` / `ERROR` | **live** |
| 2,3 | **Flag phantom** | `POST /api/dump?label=flag_phantom` — an event was counted that shouldn't have been | `FLAG PHANTOM` → `SAVING…`, flashes `SAVED` / `ERROR` | **live** |
| 2,4 | **Flag missed** | `POST /api/dump?label=flag_missed` — a real shot/settle wasn't detected, or came late | `FLAG MISSED` → `SAVING…`, flashes `SAVED` / `ERROR` | **live** |
| 2,5 | **Flag other** | `POST /api/dump?label=flag_other` — display problems, anything else. The old single-Flag cell on purpose: a pre-split reflex press still saves the ring | `FLAG OTHER` → `SAVING…`, flashes `SAVED` / `ERROR` | **live** |
| 3,1 | **P1 +** | `POST /api/score/p1/increment` | `P1 +`, flashes `+1` / `ERROR` | **live** |
| 3,2 | **P1 −** | `POST /api/score/p1/decrement` | `P1 -`, flashes `-1` / `ERROR` | **live** |
| 3,3 | **New Rack** | `POST /api/rack` — between racks: refills extensions, arms the first-shot window, clock idle. Mid-score-row so the whole between-racks ritual — score it, rack it — is one row (moved from 2,3 on 2026-08-01) | `NEW RACK`, flashes `RACKED` / `ERROR` | **live** |
| 3,4 | **P2 −** | `POST /api/score/p2/decrement` | `P2 -`, flashes `-1` / `ERROR` | **live** |
| 3,5 | **P2 +** | `POST /api/score/p2/increment` | `P2 +`, flashes `+1` / `ERROR` | **live** |

Press outcomes flash as background color (green ok / red error) for 1.2 s —
words only, no glyphs. Keys dim while the server is down; presses no-op. The
wall-TV scoreboard is the real press feedback for the score keys; the tile
flash is just an ack. Row 3 was reserved for the phase-3 music keys
(play/pause, vol ±) — displaced to `BACKLOG.md` by match scoring (2026-07-31).
Deliberately absent (Steven, 2026-07-31): clock start/pause — Reset covers the
bug cases, Force 30 the missed auto-reset.

**The flag split (2026-08-01):** one Flag key became three reason-coded keys
after an OBS-side failure (a dead camera source) masqueraded as a vision bug
through two triage passes — the dump label was always `flagged` and carried no
reason. All three keys dump the same ~20 s ring through the same endpoint;
only the label differs (`flag_phantom` / `flag_missed` / `flag_other`), and
the label names the clip dir (`<dump-dir>/<label>_N`, default
`~/cueclock-dumps` since checkpoint 37) and rides the journal's `flag_dump`
line. A bare `POST /api/dump` (no label) still means `flagged` — the endpoint
is unchanged (`design.md` §2.1, 2026-07-29). Row 2 now reads left to right as
recovery, then evidence.

### C3. Architecture

```
Stream Deck MK.2 ── USB HID ── cueclock deck (launchd agent, always on)
                                   ├─ HTTP → localhost:8000/api/*   (clock, score, dump, ruleset)
                                   ├─ obs-websocket → OBS           (projector, replay, quit — phase 2+)
                                   ├─ osascript                     (Music.app, volume — backlog)
                                   └─ subprocess                    (owns `cueclock run` — phase 2)
```

- `src/cueclock/deck/`: `layout.py` (declarative keys), `faces.py` (Pillow
  72×72 tiles), `client.py` (stdlib HTTP), `daemon.py` (the only hardware
  module — StreamDeck imports are function-local so tests import everywhere).
- Deps: `brew install hidapi`, then the `streamdeck` + `pillow` packages.
- Always-on: launchd LaunchAgent (`com.cueclock.deck`, KeepAlive), installer
  `scripts/install_deck_agent.sh` (idempotent — re-run after moving the repo).
  **No TCC grant needed** (verified 2026-07-31): vendor-usage HID isn't gated
  behind Input Monitoring.
- Resilience: USB unplug → reconnect loop; daemon crash → launchd restarts;
  server restart → faces recover on the next poll (2 s).

### C4. The session toggle (phase 2 — built 2026-08-02)

Code: `deck/session.py` (the state machine, pure over injected callables —
`spawn`, `call`, `now` — so the whole sequence is unit-tested without a
camera, OBS, or a deck) + the Session branch in `deck/daemon.py`. The machine
is advanced by the daemon's existing 2 s poll, not a background thread: the
deck keeps rendering and dispatching every other key while a session comes up.

**Start** (tap while `OFF`): spawn the canonical `run` command (README, built
by `session.run_command()`) and a `caffeinate -d` assertion → face reads
`STARTING…` → on the first poll where `/api/rulesets` answers, launch OBS
(`open -a OBS --args --startreplaybuffer --disable-shutdown-check`) and go
`ON` → then **rebind OBS's Syphon source** to this session's server
(`obsws.bind_syphon`, retried on the poll until OBS's WebSocket answers).
That rebind is not optional: every `run` publishes a **new Syphon UUID**, and
the server is advertised under whatever launched it — `[python] cueclock` from
the deck daemon's interpreter versus `[python3] cueclock` from a terminal
`uv run`. OBS stores uuid + name + app_name, so a source last configured by
one launcher renders **black** under the other. That is exactly what "the
overhead isn't making it into OBS" looked like on 2026-08-02: server healthy,
clock and scoreboard fine, table feed black, and perfect again as soon as the
session was started from a terminal. Server first, always: a browser source that loads while the server is
down parks on a silent error page (§B3). `run` is spawned with the venv's own
`sys.executable`, never `uv run` — the daemon must not block on uv's lock. Its
output appends to `~/Library/Logs/cueclock-run.log`. If no server answers
within 60 s the start is called failed: SIGINT, release, back to `OFF`.

**Stop** (hold 1 s while `ON`; the face reads `HOLD…` while held, and a short
tap does nothing), in this order — every step learned from live presses on
2026-08-02:

1. **SIGINT `run` first** (closes the pylon device — the camera stops
   streaming and cools). Nothing OBS does may delay the camera: a blocked quit
   once held the signal for 20 s and burned its whole grace window, forcing a
   pointless SIGTERM.
2. **Drive every OBS output to stopped over obs-websocket** (replay buffer,
   record, stream, virtual cam). Quitting OBS with *any* output active raises
   a modal *"OBS is still currently active"* dialog, and there is no keyboard
   at the table to click it. This **loops** rather than checking once, because
   `--startreplaybuffer` arms the buffer asynchronously — a stop pressed a few
   seconds after launch sees "not active", skips, and the buffer starts
   anyway. It also distinguishes *"nothing running"* from *"couldn't ask"*
   (OBS gone, WebSocket off): unknown is reported, never assumed quiet.
3. **Quit OBS gracefully** by AppleScript — never `kill`, which reopens it in
   safe mode without the projector, exactly what a headless rig can't fix.
4. **Backstop:** if OBS is still alive `OBS_QUIT_GRACE_S` (5 s) after the stop
   began, the dialog caught the quit anyway, so the deck clicks its OK button
   itself. The grace matters — a clean quit takes 0.3–3 s, so checking
   immediately reads *every* healthy quit as stuck.

Then `STOPPING…` until the child actually exits, at which point the assertion
is released. Measured after the fix: five cycles across the race window
(3/6/8/12/20 s of uptime), stop returning in 0.7 s, OBS exiting clean every
time with the backstop never firing. A wedged `run` gets one escalation
to SIGTERM after 20 s (`cli.py` maps it to KeyboardInterrupt, so it takes the
same shutdown path) — never SIGKILL.

**`DEV`**: the API answers but this daemon has no handle on it — a terminal
`run`, or a session that outlived a daemon restart. The key refuses to touch
it and flashes `DEV`. Ownership is deliberate: a session the daemon spawned
**survives** the daemon's own exit, so a `launchctl kickstart` after a layout
merge can never kill a live practice session. The trade is that the survivor
reads `DEV` afterwards — stop it from a terminal, or leave it up.

**The empty-table reference** the toggle launches with is
`deck --reference` (default `tests/clips/empty_table_0801`) — update it after
a camera bump alongside the README command (§E2).

**OBS one-time settings — all done 2026-08-02:** WebSocket server enabled
(Tools → WebSocket Server Settings; credentials copied to gitignored
`config/obs_websocket.json`), replay buffer enabled (20 s), "Save projectors
on exit" on, and OBS was never in Login Items. The client is
`deck/obsws.py` — one request per connection (open, authenticate, send, read,
close), every call best-effort so OBS being closed or the WebSocket server
being off can never raise into the deck loop. Phase 2 uses it only to stop the
replay buffer; the spec's "verify projector + replay buffer" check lands with
phase 3's Replay key, which builds on the same client.

### C5. Corpus-clip flow (Flag → triage)

1. **At the table:** anything wrong or interesting → press the **Flag** key
   that names why, within ~20 s: **PHANTOM** (an event was counted that
   shouldn't have been), **MISSED** (a real shot/settle wasn't detected, or
   came late), **OTHER** (display problems, anything else — when in doubt).
   The ring buffer lands as a clip named for the reason in `~/cueclock-dumps`
   — the internal SSD, **not** the T7. Dumping 3.6 GB over USB saturated the
   bus it shares with every peer and starved the very session being flagged:
   detect 75 → 45 Hz, and the Camo side-angle source frozen ~1 s in OBS,
   visible as a stutter on the wall TV (2026-08-01). (Window was 12 s: a
   measured live press came 12.7 s after the phantom it reacted to and the
   evidence frames were already gone — checkpoint 28. Reasons added after an
   OBS-side failure spent two triage passes being chased as a vision bug —
   2026-08-01.)
2. **Off the table:** Screen Sharing/SSH into the mini, open Claude Code, run
   **`/triage-clips`** (`.claude/skills/triage-clips/`): replay each new clip
   through the vision core, diagnose → fix → re-grade. Each clip ends as a
   graded corpus scenario, kept evidence, or deleted — and staging is drained
   either way, since it is neither backed up nor synced. The reason in the
   clip name sets the starting point: `flag_phantom`/`flag_missed` go
   straight to vision replay; `flag_other` checks the presentation side
   first (OBS logs, overlay, journal) before assuming a vision bug. The skill
   takes ground truth from Steven before naming or pinning anything.

### C6. Bring-up checks (at the table, still open)

- [ ] Does a deck keypress wake the sleeping mini? If not: `pmset` never-sleep
      (an M2 mini idles ~5 W); display sleep stays enabled regardless.
- [ ] `pmset -g assertions` clean after session stop.
- [ ] OBS relaunch restores projector + replay buffer, twice in a row.

---

## Part D — Audio / microphone (voice "extension" built 2026-07-31)

1. **v1 — voice "extension"** (built 2026-07-31, the only shipped use): Vosk
   small-EN model, grammar `["extension", "[unk]"]`, finals only, per-word
   confidence ≥ 0.9, 2 s cooldown, 16 kHz mono int16 — the old app's proven
   listener ported as-is (`design.md` §2.1 audio row). Code:
   `src/cueclock/audio/` — `spotter.py` is the pure accept/reject gate
   (recognizer injected, unit-tested with a scripted fake), `listener.py`
   owns the mic stream and thread. `cueclock run` starts it automatically
   when the model directory exists (one skip line otherwise); an accepted
   hit calls the Hub's extension exactly like the deck key. Flags:
   `--no-mic`, `--mic-device NAME`, `--mic-model PATH`. Model:
   `scripts/fetch_vosk_model.sh` fetches `vosk-model-small-en-us-0.15`
   (~40 MB) into gitignored `models/` — idempotent.
2. **Backlog — acoustic strike-click corroborator** (`design.md` §4.2): a
   broadband transient detector coincident within ~100 ms of the cue-ball
   velocity impulse; resolves slow tip-nudge vs. soft shot. Build **only if**
   live play shows nudges leaking through.
3. Nothing else. Overlay chimes come from the browser page via OBS; OBS owns
   the Camo side-angle camera and its audio — cueclock must never grab it.

**The Mac mini has no built-in microphone** (verified 2026-07-31:
`system_profiler SPAudioDataType` lists only the built-in *speakers*, HDMI
out, and virtual devices — Apple's spec sheet agrees; mic arrays are an
iMac/MacBook feature). A wired USB mic is required hardware for this
feature; until one is plugged in, `run` prints the no-mic line and
everything else works normally.

Design facts: keyword spotting is mic-agnostic (no calibration in the camera
sense); the input device is pinned by CoreAudio *name* — `--mic-device`
(exact match, then case-insensitive substring) — never "system default": on
no match the listener prints the available inputs and runs without the mic,
so a Bluetooth headset, the Jump Desktop virtual mic, or Camo's virtual audio
device can't silently become the listener. Once the USB mic is
chosen, bake its name in as the `--mic-device` default. Any cheap wired USB
mic works; avoid Bluetooth (latency + input flakiness). macOS asks for
microphone permission (TCC) the first time the process opens the mic —
approve the prompt for the terminal app once, or the stream delivers
silence.

Bring-up (at the table, before trusting it live):

- [ ] Plug in the USB mic, then `uv run python -m cueclock.cli listen
      --devices` — confirm its CoreAudio name and pass it as `--mic-device`.
- [ ] `uv run python -m cueclock.cli listen` — say "extension" 10× at playing
      distance, head down over a shot: every call prints `ACCEPTED`
      (conf ≥ 0.9).
- [ ] False-positive soak: leave `listen` running through a practice session
      with balls clacking and music playing — zero `ACCEPTED` lines.
- [ ] End-to-end: `uv run python -m cueclock.cli listen --post
      http://localhost:8000/api/extension` against a running clock, then the
      real thing — say "extension" during `cueclock run` and watch the
      overlay add time.

---

## Part E — TV & recalibration

### E1. The TV

**Hisense 55S7SG CanvasTV** (2026, 55"), chosen 2026-07-28 for the one
property that solves the room's stated #1 problem: a **true matte panel**
(Hi-Matte) that diffuses the perimeter-LED light frame's reflection instead of
mirroring it. An art TV is engineered to display a static image for hours
under ambient light — exactly the scoreboard workload. OLED was ruled out on
burn-in (hours-static scoreboard is the textbook workload); glossy
high-brightness panels (U8QG-class) were ruled out because tilting them
away from the LED frame points their reflection field at the lit table.

Install: ECHOGEAR tilting mount (VESA 400×200), screen center ~62–66" from
the floor, Mac mini + T7 + power strip behind the panel, cable raceway down to
the outlet. **TV settings: Game Mode ON for the Mac's HDMI input, motion
smoothing OFF.** TV power stays on the TV remote.

### E2. After any camera bump/move (~15 min)

The standing recipe — the camera moved when the TV and cable track went in,
and it will move again someday:

1. Level the camera (bubble level on the body) before locking screws;
   re-measure tilt from a fresh empty-table clip (boundary-line fit).
2. Re-peak focus via the live-view SHARP metric; re-lock the lens screws.
3. Re-verify WB against white paper (lighting unchanged ⇒ ratios hold);
   re-save UserSet1 if anything changed.
4. Record a fresh reference + refit, all through the symlinked clips dir:
   `uv run python -m cueclock.cli record --label empty_table_MMDD --seconds 3`
   → `fit-undistort tests/clips/empty_table_MMDD`
   → `calibrate tests/clips/empty_table_MMDD`.
5. Relaunch `run` with `--reference tests/clips/empty_table_MMDD` (and update
   the launch command in README). The startup drift check warns loudly if the
   reference has gone stale (warn > 8 vs. same-mount noise ~2).
6. Re-set the OBS crop; re-judge the LUT/grade on the actual TV.
7. ChArUco intrinsics (`design.md` §4.3) only after a *final* install — void
   the moment the camera moves or refocuses.
