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
- Browser source `http://localhost:8000/replay` — the REPLAY badge: scrim,
  wordmark, countdown, drain bar, closing fade to black. Lives only in the
  Replay scene; the deck rewrites its URL on every press with `?s=<seconds>`
  (that replay's own length). It exists because the buffer records program
  output, so the live shot clock is burned into every replayed frame and keeps
  counting on the TV; saying REPLAY over it is what keeps that from reading as
  a real countdown — same reasoning as the `no_clock` label (§C2a). It also
  used to *cover* the burned-in clock with `?mask=` rectangles; that was
  removed 2026-08-03 (§C4a) and the countdown is visible again.
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
- **Replay**: replay-buffer playback (Stream Deck phase 3, built 2026-08-02).
  Two sources, both filling the canvas: **Replay Media** (`ffmpeg_source`, the
  file the buffer just wrote, muted) under **Replay Badge** (browser source
  `http://localhost:8000/replay`). **Never build this scene by hand** —
  `uv run python scripts/setup_obs_replay.py` creates it and re-applies the
  settings, three of which are load-bearing and the opposite of the OBS default:
  `restart_on_activate: false` (the seek happens *before* the scene switch, so
  a restart would discard it and play the twenty seconds of chalking the seek
  exists to skip), `close_when_inactive: false` (the source must hold the
  file open while the scene is dark — that is when its duration is read), and
  on the badge `restart_when_active: false` (the deck loads that page itself,
  before the cut — a reload triggered *by* the cut shows the previous replay's
  last frame, which is a blacked-out one, for the half second it takes).
  Nothing is cropped: the overhead in a recording is a ~586×1080 portrait strip
  already downscaled to 57%, so filling a 16:9 canvas from it would be a 3.3×
  upscale of a downscale. Playing the composed frame 1:1 is pixel-native and
  carries both angles. A full-screen overhead replay is available any time by
  making the overhead the main *live* view — the buffer only ever holds what
  was on the TV.

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

**Status: layout v2 live (2026-08-02).** `cueclock deck` runs under launchd
(`com.cueclock.deck`). Eleven keys, four spare — down from fourteen used and
one spare, by consolidating the pairs that differed by one bit and splitting
session *mode* out as its own axis. Phase 3 landed 2026-08-02: Replay now cuts
the saved buffer to the TV, seeked to the shot (§C5).

### C0. The three rules of layout v2

Everything below follows from these; they are also the module docstring of
`deck/layout.py`.

1. **A key that can't be pressed goes dark, it doesn't dim.** Visibility is
   declared per key against the session mode and the ruleset, so what is lit is
   always exactly what is pressable. **Nothing reflows** when a key leaves — it
   leaves a hole and every survivor keeps its cell, so position stays learnable
   even though the set changes. The deck goes 11 keys in Match → 8 in Run out →
   4 in No clock → 1 with the server down. Dimming survives only as typography
   (the small second line), never as state.
2. **The spine sits top-left and never moves.** Session, Mode, Replay and Flag
   work in every mode, so they hold the 2×2 corner. Position therefore answers
   *does this key persist*, and colour answers *how much time do I have* — two
   questions, two channels, which is why the spine isn't one colour.
3. **Face grammar.** The `OFF`-button confusion was never the word, it was a
   *state* shown on a key read as a *command*. So: **commands** say the verb and
   nothing else (`START 30`, `PUSH`, `FLAG`); **cycles** put the current value
   bright over the next value dim, so both are on the face and neither can be
   mistaken for the other; the one **hold** key states where it is and names the
   gesture out (`ON` / `HOLD 1s`).

Colour groups (bottom stripe, `faces.STRIPE_*`): **amber** = on the clock
(seconds matter) · **blue** = the match (no pressure) · **purple** = evidence ·
**grey** = system. Red and green are reserved for the press-outcome flash, which
is why the group colour is a stripe and not the tile: colour the tile and the
flash stops reading.

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
  `osascript`. **TV power over the Android TV Remote protocol** (`deck/tv.py`,
  §E1) — the CanvasTV runs Google TV, not VIDAA as this line used to claim.
- **Session toggle owns `run` AND OBS** (2026-07-31): the replay buffer keeps
  OBS continuously encoding, and active outputs hold macOS power assertions
  that block sleep — so OBS dies with the session and relaunches into
  presentation on start.

### C2. Button map — current state

Page 1 (only page). 5 columns × 3 rows; unlisted cells stay dark, as do listed
keys outside the modes and rulesets named in **Present in**.

```
        col 1        col 2        col 3        col 4        col 5
row 1   SESSION      MODE         START 30     PUSH         EXT ••
        ▁▁grey▁▁     ▁▁grey▁▁     ▁▁▁▁▁▁▁▁▁▁▁▁amber▁▁▁▁▁▁▁▁▁▁▁▁▁

row 2   REPLAY       FLAG         (·)          (·)          (·)
        ▁▁grey▁▁     ▁purple▁      ← the reason page unfolds here →

row 3   RULESET      SET UP       P1  3        P2  2        I SHOT
        ▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁blue▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁     ▁purple▁
```

| Pos | Key | Action | Face | Present in |
|---|---|---|---|---|
| 1,1 | **Session** | Tap while idle: spawn `run` + a `caffeinate -d` assertion, launch OBS as soon as the server answers, rebind its Syphon source. **Hold 1 s** while `ON`: SIGINT `run`, stop OBS's outputs, quit OBS, release the assertion. Stays lit with the server down — it is what starts the server | `START` / `STARTING…` / `ON`+`HOLD 1s` / `STOPPING…` / `DEV`; `HOLD…` while held. Green dot = server answering | **always**, incl. server down |
| 1,2 | **Mode** | Cycle session mode via `POST /api/mode/{next}` — match → run out → no clock → match | current over next: `MATCH`/`RUN OUT` | always |
| 1,3 | **Start N** | `POST /api/restart` — the clock is wrong and you're about to shoot: fresh countdown, running immediately. **The face states the countdown it will hand you**, off the clock's own `current_base()` — 60 inside the WNT first-shot window, 15 under Final 10, 30 otherwise | `START` over `30`/`60`/`15`, flashes `GO` / `ERROR` | match, run out |
| 1,4 | **Push** | `POST /api/push` — push out called: clock restarts at the 60 s first-shot time and the window re-arms for a pass-back | `PUSH`, flashes `PUSH` / `ERROR` | match **and** 9-ball only |
| 1,5 | **Extension** | `POST /api/extension`. Top-right corner so a player under the clock finds it without hunting (2026-08-01). **Pips = extensions left**, so the pool reads before the press instead of after a red `NO EXT`. Never removed inside a clocked mode — a spent pool shows as empty pips, which are the only readout of how many remain | `EXT` + one pip per extension, flashes `OK` / `NO EXT` | match, run out |
| 2,1 | **Replay** | Save the buffer and cut to it on the TV, starting `1.5 s` before the last shot (§C4a). Press again to go back to live; it returns by itself when the file runs out | `REPLAY` → `CUEING…` → `BACK`+`TO LIVE`; flashes `ERROR` and stays live if OBS can't | always |
| 2,2 | **Flag** | Opens the reason page at 2,3–2,5. The dump fires from whichever reason is pressed | `FLAG` → `WHY?` | always |
| 2,3–2,5 | **Phantom / Missed / Other** | `POST /api/dump?label=flag_phantom` etc. Only visible while the page is open | `PHANTOM` / `MISSED` / `OTHER` → `SAVING…`, flashes `SAVED` / `ERROR` | reason page only |
| 3,1 | **Ruleset** | Cycle rulebooks via `POST /api/ruleset/{next}` | current over next: `8-BALL`/`FINAL 10` | match, run out |
| 3,2 | **Set up** | `POST /api/rack` — you're not shooting, you're setting up: clock parks, extensions refill, shot count zeroes. One word for both clocked modes, because racking and throwing fifteen balls back up are the same act to the clock | `SET UP`, flashes `SET` / `ERROR` | match, run out |
| 3,3 | **P1** | **Tap** `POST /api/score/p1/increment`, **hold 0.6 s** decrement. Face shows the live score | `P1` over the score, flashes `+1` / `-1` / `ERROR` | match |
| 3,4 | **P2** | as P1 | `P2` over the score | match |
| 3,5 | **I shot** | `POST /api/groundtruth` — ground truth (#19). Pressed **every shot**, with no judgement attached: it records that a shot happened and never touches the clock. The server derives missed / phantom / late offline by diffing presses against emitted `ShotTaken` events (`cueclock report`). Bottom-right corner so it can be found by feel, like Extension | `I SHOT`, flashes `✓` / `ERROR` | **always** — not a mode |

Press outcomes flash as background colour (green ok / red error) for 1.2 s —
words only, no decorative glyphs. The only graphics are *data*: extension pips,
the live score numeral, the Start countdown, the session health dot. The wall-TV
scoreboard is the real feedback for the score keys; the tile flash is an ack.
Deliberately absent (Steven, 2026-07-31): clock start/pause.

**What consolidated (2026-08-02).** Four score keys → two (tap adds, hold takes
back — the same rule the whole deck follows: tap does the common thing, hold
does the rare inverse). Three flag keys → one plus the reason page. **Reset and
Force 30 → one key**: they differed by exactly one bit — both set the countdown
to full, both rack-aware, both leaving extensions and shot count alone; one
landed `IDLE`, the other `RUNNING`. Parking the clock moved to No clock mode,
where it's visible. *Force 30* was misnamed anyway: it gives 60 s inside the
first-shot window, which is exactly what `START 60` now says out loud.

**The ground-truth key (#19, 2026-08-02).** The one key that produces a
*denominator*. Everything else on the accuracy board was ranked by argument,
because "the clock missed a shot" was only ever observed when Steven happened
to notice and hit Flag; pressing one key on every shot turns detection accuracy
into a number.

**Its governing rule: the player never classifies a failure.** One key, "I
shot", pressed every shot, always available, no judgement attached — that is
the only fact a human holds which the machine does not. Missed / phantom /
late / recovery are all diffs the server computes. An intermediate design had
Steven choosing between Start, Flag and this key depending on *how* the clock
had misbehaved; he rejected it, correctly — every branch existed because the
design was asking him to classify failures at the table, which is exactly the
work the key exists to remove. **If a future change adds a case the player must
recognise, the change is wrong.** Push the branch into the server.

It is therefore not a mode and never goes dark: once the only remaining cost is
the press itself, a toggle saves nothing and adds one more thing to remember.
Arming is inferred offline instead — presses are grouped into runs and only
those windows are judged, so a session he did not mark is ignored rather than
reported as all-phantom.

**Where Flag still fits:** problems that are *not* about shots — overlay wrong,
replay broken, OBS dead, scoreboard stuck. The ground-truth diff is
structurally blind to those, and the two never overlap, so there is still no
decision: *did you shoot? press the shot key. is something broken that isn't a
shot? press Flag.*

**The flag reason page.** One Flag key became three reason-coded keys on
2026-08-01, after an OBS-side failure (a dead camera source) masqueraded as a
vision bug through two triage passes because the dump label was always
`flagged`. Layout v2 keeps every bit of that — the label still names the clip
dir (`<dump-dir>/<label>_N`, default `~/cueclock-dumps`) and still rides the
journal's `flag_dump` line — but moves the *choice* off the reflex press: one
key found by feel beats reading three faces inside a 20 s ring, and the reason
itself has no deadline. An unanswered page files itself as `flag_other` after
4 s, which is what an unspecified flag has always meant, so walking away still
saves the clip. Cost is the couple of seconds between the two presses, off a
20 s ring (`run --ring-seconds`). A bare `POST /api/dump` still means `flagged`
— the endpoint is unchanged (`design.md` §2.1, 2026-07-29).

### C2a. Session modes

A **mode** is orthogonal to a ruleset: a ruleset is a *rulebook* (every entry in
`clock/rulesets.py` cites published rules), a mode is what you're doing at the
table. Both are cycle keys, both blue-adjacent on the deck, and `clock/modes.py`
is the table.

| Mode | What it is | Opening shot | Clock |
|---|---|---|---|
| **match** | A rack: break, rack boundaries, the ruleset's first-shot allowance | must reach 3000 mm/s (the IDLE break gate) | live, rack-aware |
| **run out** | Fifteen balls thrown up, no rack, run them | any speed | live, base countdown on every shot |
| **no clock** | Actual drills — repeat a shot as long as you like | — | off; vision still journals |

`run_out` exists because the break gate ate honest ~1000 mm/s drill openers
twice (`BACKLOG.md`, checkpoint 33) and lowering the floor for everyone was the
wrong fix — placement lookalikes sit at ~1000 too. There is no break to gate
without a rack, so the mode simply has no floor. It also drops the first-shot
allowance: the WNT 60 s exists for the shot *after* a break-off, and without one
there is nothing for it to attach to.

`no_clock` **parks the countdown as well as gating vision** — one command, one
journal line (`cmd_mode`). Gating vision alone would leave whatever was already
counting to run down and fire the 10 s / 5 s calls and EXPIRED mid-drill.
Detections keep being journaled (`vision_shot_taken_no_clock`), so a drill still
produces evidence and a Flag press during one still dumps a real ring. On the TV
the shot-clock overlay **hides the digits entirely** and shows `NO CLOCK`: a
parked clock rendering a static `30` is indistinguishable from the stalled clock
a missed settle produces, which is the exact failure Steven is trained to
distrust.

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

**Start** (tap while `OFF`): **wake the wall first**, because between sessions
both halves of it are asleep — `caffeinate -u -t 2` asserts user activity to
pull the Mac's display out of `displaysleep` (after 10 min idle the mini stops
driving HDMI, so the wall reads "no signal" however awake the TV is), and a
Wake-on-LAN magic packet starts the TV climbing out of standby (§E1). Both are
fire-and-forget: the assertion expires by itself and the TV confirms on a later
poll, so neither can hold up the camera. Then spawn the canonical `run` command
(README, built by `session.run_command()`) and a `caffeinate -d` assertion →
face reads `STARTING…` → on the first poll where `/api/rulesets` answers, launch OBS
(`open -a OBS --args --startreplaybuffer --disable-shutdown-check`) and go
`ON` → then set the rig up for this session, retried on the poll (20 attempts,
~40 s) because nothing is ready when first asked — the TV is still waking and
OBS's WebSocket answers ~10 s after launch: **confirm the TV is on**
(`tv.ensure_on`, which re-fires the magic packet each attempt so a TV that
missed the first one still comes up), **rebind the Syphon source**
(`obsws.bind_syphon`) and **open the program projector on the TV**
(`obsws.open_projector`, monitor 0 — the mini drives one display). Opening the
projector explicitly beats "Save projectors on exit", which only restores what
was open at the last quit: a session that ended without one came back to a
blank TV, and the key's promise is that the last session's state doesn't
matter. Verified idempotent — calling it with a projector already up reuses
that window rather than stacking another.
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

5. **Long-press POWER on the TV, last of all** — once `run` has exited and OBS
   is gone, so the wall keeps reading `STOPPING…` until the session really is
   over rather than going black mid-shutdown. Long, not short: a tap lands in
   art mode with the panel still lit (§E1). Only an *explicit* stop does this
   — a `run` that crashed on its own leaves the TV on, because the wall is the
   one place that failure is visible from the table.

Then `STOPPING…` until the child actually exits, at which point the assertion
is released. Measured after the fix: five cycles across the race window
(3/6/8/12/20 s of uptime), stop returning in 0.7 s, OBS exiting clean every
time with the backstop never firing. A wedged `run` gets one escalation
to SIGTERM after 20 s (`cli.py` maps it to KeyboardInterrupt, so it takes the
same shutdown path) — never SIGKILL.

**`DEV`**: the API answers but this daemon has no handle on it — a terminal
`run`. The key refuses to touch it and flashes `DEV`; it only ever signals
processes it started itself.

**Surviving a daemon restart.** A session the daemon spawned survives the
daemon's own exit — a `launchctl kickstart` after a layout merge must never
kill a live practice session. The daemon records what it owns in
`~/.cueclock-session.json` and **re-adopts** it on startup, so the restart is
invisible: the key comes back reading `ON` and can still stop it. Before
adoption existed the restarted daemon disowned its own session mid-practice
and the key went dead (2026-08-02). A terminal `run` writes no pidfile, so it
still reads `DEV` — the ownership rule is unchanged, only recovered from disk.

**The empty-table reference** the toggle launches with is
`deck --reference` (default `tests/clips/empty_table_0801`) — update it after
a camera bump alongside the README command (§E2).

**OBS one-time settings — all done 2026-08-02:** WebSocket server enabled
(Tools → WebSocket Server Settings; credentials copied to gitignored
`config/obs_websocket.json`), replay buffer enabled (20 s), "Save projectors
on exit" on, and OBS was never in Login Items. The client is
`deck/obsws.py` — one request per connection (open, authenticate, send, read,
close), every call best-effort so OBS being closed or the WebSocket server
being off can never raise into the deck loop. The Session key uses it to stop
the outputs, bind Syphon and open the projector; the **Replay** key drives
playback (§C4a).

### C4a. Instant replay (phase 3 — built 2026-08-02)

Code: `deck/replay.py` (cue-point arithmetic + the obs-websocket sequence) and
the `ReplayShow` branch in `deck/daemon.py`. The scene is built by
`scripts/setup_obs_replay.py`, never by hand (§B3).

**Where playback starts.** The buffer holds the last 20 s of program output, so
the interesting part is always at the *end* — playing from the start is twenty
seconds of someone chalking. The vision core already timestamps every ShotTaken
to ~20 ms of contact, the server reports how long ago that was
(`GET /api/last_shot`), and the file ends at the save, which makes the cue
point arithmetic:

```
cursor = duration − shot_age + display_lag − lead      (20 − 4 + 0.1 − 1.5 = 14.5 s)
```

Constants live at the top of `deck/replay.py`: `LEAD_S` 1.5 (runway before the
shot), `DISPLAY_LAG_S` 0.1 (camera→Syphon→OBS, §B3's ~100 ms budget — the
detector's stamp is when *vision* saw the shot, the recording shows it later),
`MAX_PLAY_S` 20 and `MIN_PLAY_S` 2 (a stale anchor can't strand the TV on the
whole buffer; an instant press still shows something), `SPEED` 1.0.

**Why 1.0 and not slow-mo.** OBS records at 30 fps, so half speed is
frame-doubled mush. Real slow-mo needs OBS set to 60 fps first; then `SPEED`
becomes 0.5 and the setup script's `speed_percent` follows it. The camera's 160
fps never reaches the recording — it only reaches detection.

**No cue-stick detection, deliberately.** The detector discards the stick/arm
blob by design (it is the oversized occluder), so
finding it would be new detection work with no ground truth, to answer a
question the shot timestamp already answers. It would also answer the wrong
question: the stick appears at *address*, which can be ten seconds of standing
still.

**The anchor is stamped before every gate** (`Hub.shot_taken`) — ahead of the
kill switch, the mode check and the IDLE break floor. Those decide whether a
shot moves the *clock*; replay asks what is on the *video*. So the seek still
works in `no_clock`, which is exactly where drills happen. Accepted cost: a
phantom shot anchors a phantom replay, which is a bug worth seeing anyway.

**Order is the feature.** Load the file → read its duration → seek → switch
scene. Seeking after the switch would cut the TV to frame 0 and then jump;
unit-tested as such (`tests/unit/test_replay.py`).

**The seek proves itself, because OBS drops seeks silently.** Measured against
OBS 32 on 2026-08-02: `SetMediaInputCursor` issued in the ~70 ms after the media
source first reports a duration is **accepted and ignored** — success code, no
movement. Unverified, that failure is invisible: the replay just plays from the
top and reads as the lead being wrong rather than the seek being lost. So the
seek is re-asked until the cursor proves it landed, and if it never does the cut
is refused (TV stays live) rather than showing a wrong replay.

**Effective lead is 1.5–3.5 s, not 1.5.** A media source seeks to a *keyframe*,
so it always lands early; the recordings carry keyframes every **1.90–1.97 s**
(`ffprobe` on real replay files, 2026-08-02). Lowering `LEAD_S` alone therefore
cannot tighten the framing past the keyframe grid — that needs a shorter
keyframe interval in OBS's encoder settings.

**The burned-in shot clock is visible again — the mask is gone** (removed
2026-08-03, checkpoint 60; added 2026-08-02, checkpoint 49). For one day the
badge painted the countdown out with opaque `?mask=` rectangles it measured
from OBS at each press. The mechanism was sound and never misfired: identity
was the *page*, so a renamed, moved or duplicated overlay stayed covered and a
hidden one was skipped. It was removed for how it looked, not for what it did.
On the rig the clock is a **193×193 browser source at (1141, 0)**, sitting on
the Camo side angle — so the mask was a 197×195 black rectangle punched into
live footage, which is the clock's own black square with its white border and
its digits deleted. That reads as damage (Steven, 2026-08-03: *the black square
over the clock in replay view looks bad*).

Removing it restores the checkpoint-46 problem: a replay shows a countdown that
isn't real, under a badge that says REPLAY. That is the accepted interim state.

**The fix is upstream, and it is not a mask.** The buffer records program
output, so a clean replay requires a clean *program*; stock OBS 32.1.1 has no
per-scene or per-source recording (checked — the bundled plugin set only; a
"record a different scene" path needs Exeldro's `obs-source-record`, plus
hotkey-triggered saves and file discovery, because obs-websocket cannot save a
filter-owned buffer). The cheap structural answer is to stop burning the clock
over live video: put a flat backdrop under it in `Stream Detail` (a top bar
holding clock + score, broadcast style) so a future mask would be black on
black, or take the clock out of OBS entirely and float it over the projector —
which also costs the chime routing (§B3, "Control audio via OBS"). Undecided;
`Stream Detail`'s corner clock is due a redesign either way.

One tail worth knowing, and the reason a mask is a bad strategy rather than
just an ugly one: pressing Replay from the **Shot Clock** scene used to mask
69.6% × 69.8% of the canvas, because that scene's clock *is* the canvas.

**The return to live is a fade to black, not a transition.** OBS's Fade is
already set and it is invisible here by construction: a replay is a pixel-exact
rewind of the TV, so a 300 ms cross-fade dissolves one picture of the table into
a nearly identical one and reads as "nothing happened" (Steven, 2026-08-02:
*hard to tell when we're back on the live view*). The badge instead fades the
whole frame to black over its last 0.45 s, so black separates the two pictures.
The `.secs` slot became a `LIVE IN n` countdown to the same moment — it says
*when*, never *that* we are live, because the page only ever sits over old
footage.

**The badge is loaded before the cut, and its timeline is longer than the
replay.** Two instants that look like one, both measured 2026-08-02: the page's
clock starts **0.03–0.05 s** after `SetInputSettings`, but its first frame
reaches OBS **0.567–0.570 s** later (three trials each, repeatable to 3 ms).
Cut instantly and the badge is invisible — clock unmasked — for half a second;
give it time to paint and it is already half a second into its own animation.
So the deck does both: it sets the URL and presses the source's own refresh
button early (behind the seek, whose round trips hide most of the wait), sleeps
out the remainder of `BADGE_PAINT_S`, *then* switches; and it hands the page
`play_s + BADGE_PAINT_S`, so the drain bar, the countdown and the fade all land
on the deck's actual return. The explicit refresh is why the source has
`restart_when_active` off — see §B3.

**Measured live, 2026-08-02** (four consecutive presses, camera not running):
`show()` took **0.34–0.63 s** end to end, which is how long the deck blocks;
saved files come out at **18.6–20.0 s** against the 20 s buffer setting. The
badge pre-warm adds up to `BADGE_PAINT_S` (0.7 s) minus whatever the seek
already spent, so a press now blocks on the order of a second.

Every failure leaves the program scene alone — unreachable OBS, missing Replay
scene, a save that never lands all mean the TV keeps showing live, which is
always the safe state for a key pressed mid-session.

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

- [x] Does a deck keypress wake the sleeping mini? Settled 2026-08-02 without
      depending on it: Start issues `caffeinate -u -t 2` itself, so the display
      is woken explicitly rather than as a side effect of the keypress. Display
      sleep stays enabled (an M2 mini idles ~5 W); Amphetamine holds system
      sleep off, and screen lock is off so nothing covers the projector.
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
2. **Rejected 2026-08-02 — acoustic strike-click corroborator.** A broadband
   transient detector coincident with the cue-ball velocity impulse was the
   long-standing designed answer for slow tip-nudge vs. soft shot. Steven is
   not committing to a mic for shot detection, so it is not backlog — it is
   off the table. The cases it covered (issues #5, #8, #11) now stand on
   vision levers or, for #5, on no known path at all.
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
smoothing OFF.**

#### TV power from the deck (built 2026-08-02, validated live the same day)

Steven's verdict after using it: *"power on and off buttons are working well."*
Nothing below changed as a result — it is recorded because the four measured
facts that follow are the kind that only look obvious afterwards.

The set runs **Google TV** — not VIDAA, which earlier notes here assumed — so
it speaks the **Android TV Remote** protocol: TLS + protobuf on 6466, pairing
handshake on 6467. Code is `deck/tv.py`; the Session key drives it (§C4).

Pairing is one-time: `uv run python scripts/pair_tv.py`. The TV shows a
**6-character hex** code (`B195EF`, `410A85` — hex, so `O` is a zero), and the
cert + key land in gitignored `config/tv_cert.pem` / `tv_key.pem` beside
`config/tv.json`, which holds the host and MAC. Losing them just means pairing
again.

Four things were measured on the real set, and none are guessable:

1. **A short POWER press only reaches art mode.** This is a CanvasTV: tapping
   power drops it into the artwork screen, which keeps the panel lit *and the
   whole network stack up*. Every early attempt to switch it off with short
   presses read as "the key did nothing", because the TV never left the
   network. **A long press (~2.5 s) is what reaches true standby.**
2. **POWER is a toggle**, so it is only ever sent when the TV's own reported
   state disagrees with the target — the same rule Home Assistant's
   `androidtv_remote` uses. Retrying it undoes it; a probe that sent SLEEP then
   POWER switched the TV off and straight back on.
3. **In standby the TV leaves the network entirely** — 6466 dies ~4 s after
   the long press — so nothing can be *told* to wake up. There is no
   Quick Start / Screenless service toggle on this model to keep it alive.
4. **Wake-on-LAN brings it back, screen included, in about a second.** The TV
   reports MAC `…:97` over the protocol while ARP shows `…:98` carrying
   traffic — two consecutively allocated NICs on one board — and which one
   listens in standby isn't observable, so the packet goes to `…:96/:97/:98`
   on ports 9 and 7, at both the subnet and global broadcast address.

The Mac keeps its HDMI display through all of this: across a full off/on cycle
the display list never emptied, briefly degrading to a generic
`spdisplays_display` during the wake handshake before returning to `HISENSE`.
So the mini never goes headless, OBS's projector window survives the night,
and **no EDID holder is needed**. The TV also returns to the Mac's HDMI input
by itself, so no input switching is required.

**Screen lock must stay off** (System Settings → Lock Screen → "Require
password after…" → **Never**). A locked Mac draws loginwindow *over* the OBS
projector, so the morning press would light the wall up on a password prompt.

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
