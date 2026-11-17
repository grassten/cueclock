# Hardware & rig reference

Everything physical and everything between the sensor and the TV: camera
bring-up and locked settings (Part A), the video path into OBS (Part B), the
Stream Deck control surface (Part C — a living doc: keep it in sync with
`deck/layout.py`), the microphone plan (Part D), the TV + post-move
recalibration recipes (Part E), and the side-angle camera replacing the
iPhone (Part F — ordered 2026-11-11, not yet live). References use the form
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
| No camera found | Cable not fully seated; try the other USB-A port; never a hub. `ioreg -p IOUSB -w0` lists the bus — if no Basler is on it, no software can help |
| Session key flashes `STARTING…` then returns to `START` | Pre-2026-09-01 behaviour, now impossible: the key holds `NO CAMERA` / `START FAILED` instead. On an old build, read `~/Library/Logs/cueclock-run.log` |
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
  ├─ detection path (every frame, 156 Hz since checkpoint 106 — distorted pixels)
  └─ display branch @ ~60 fps (own thread — DisplayWorker):
       debayer (COLOR_BayerBG2BGR_EA) → RGBA, written straight into a
         shared-memory Metal texture (no upload)
       → rectify as a Metal compute pass (the same remap maps, on the GPU)
       → Syphon publish (same command buffer)
  ⇒ OBS "Syphon Client" source → scenes → Fullscreen Projector on the TV
```

- **The display branch runs on its own thread** (`DisplayWorker`,
  `display/syphon_out.py`) since 2026-08-07: the grab loop only hands over a
  copy of the Bayer frame (~0.3 ms). Published inline, the 60 fps branch cost
  the detect loop a third of its cadence (77 → 51 Hz). 30 fps was cheap
  enough to hide the same cost; 60 was not.

- **The rectify runs on the GPU** (checkpoint 117, #53). On the CPU,
  `cv2.remap` looked cheap by wall time (~1.7–2 ms on eight threads) but cost
  ~12.5 ms of CPU per frame, and the whole feed ~17 ms of CPU, about one
  full core at 60 fps. Its memory traffic, next to OBS compositing a 4K60
  canvas, slowed `find_blobs` on the performance cores from 9.7 to 17.9 ms and
  held detection near 134 Hz. The Metal pass samples exactly the coordinates
  `cv2.remap` does (`pipeline.remap_coordinates`) and matches it to within
  1/255; the feed then costs ~3.4 ms of CPU. If the shader fails to compile
  or the device refuses the buffer-backed texture, `run` prints one line and
  falls back to `cv2.remap`. `replay` and `live_view` still use the CPU path;
  with `--no-undistort` there is nothing to warp and the feed is just a debayer.
  `run` prints `display: rectify on the GPU` when the fast path is up.
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
- Video Capture Device → **Insta360 Link 2C Pro** (since 2026-11-12, Part F)
  — the side angle, 1920×1080 NV12 at **60000240/1000000** fps, with an Apply
  LUT filter ("Side colour", `assets/obs/cueclock-sidecam.cube`) that corrects
  the camera's colour (Part F, 2026-11-15). That odd frame
  rate is deliberate: the camera reports its 60 fps mode as 60.00024 fps, so
  OBS's plain "60" fails the plugin's exact range match and it silently falls
  back to **50** (log: `Frame rate is not supported: 60 FPS … falling back …
  50 FPS`), then saves 50 over the setting. Check the log for that line after
  touching the source. Before it, **Camo Camera** — the iPhone side angle over USB,
  owned by OBS directly (Camo Studio on the mini + the Camo app on the phone,
  manual focus/exposure/WB locks; 1080p60 in Camo Studio since 2026-08-07 —
  the 60 fps ceiling, Camo's 4K formats are 30 fps). It replaced the wireless **iPhone
  Continuity Camera** source on 2026-08-01: Continuity looked washed out with
  blotching (iPhone auto tone-mapping/AF, no manual controls) and it died
  mid-session once without reconnecting, which cost two triage passes chasing
  a vision bug that wasn't one (CHANGELOG 2026-08-01).
- Browser source `http://localhost:8000/shotclock?view=bar` — **the bar**: one
  transparent **2668×2160** source at (0, 0) carrying the clock *and* the
  scoreboard, drawn as a plinth (160 px in the page's 1334-wide reference
  geometry; the page scales itself to the source width — `fitBar()` in
  `shotclock.html` — so text rasterizes at the source's native resolution)
  along the bottom of the side-angle column with the clock in its right-hand
  cell (issue #23, 2026-08-03; resized for the 4K canvas 2026-08-07). Enable
  **"Control audio via OBS"** *and* set its Audio Monitoring to **"Monitor Only
  (mute output)"** — this is the source that sounds the cues on the TV, while
  what gets *recorded* is the room, from Mic/Aux. Every other pairing of those
  two switches fails somewhere (Part D). 2668 is the seam where the overhead strip
  begins, so nothing it draws can reach the table.
  Clock and score come from one page because the drain runs the full width of
  the bar, crossing from the score area into the clock cell, and no overlay can
  span two OBS sources. It replaced two sources that were both defects: a
  193×193 corner clock sitting *on* the live side-angle video (so it was burned
  into every replay — §C4a), and a `/score` bar centred on the canvas whose
  right third lay across the overhead strip's bottom-left pocket.
- Browser source `http://localhost:8000/replay` — the REPLAY badge: scrim,
  wordmark, countdown, drain bar, closing fade to black. Lives only in the
  Replay scene; the deck rewrites its URL on every press with `?s=<seconds>`
  (that replay's own length). It exists because the buffer records program
  output, so the live shot clock is burned into every replayed frame and keeps
  counting on the TV; saying REPLAY over it is what keeps that from reading as
  a real countdown — same reasoning as the `no_clock` label (§C2a). It also
  used to *cover* the burned-in clock with `?mask=` rectangles; that was
  removed 2026-08-03 (§C4a) and the countdown is visible again.
**Scenes** (scene collection `Untitled`; names below are what the rig
actually runs — the original plan called them *Table* / *Clock*):
- **Stream Detail** (default, the live table scene): Camo side angle + Syphon
  overhead + the bar. Names and race-to are set from `/control`; scores from
  the Stream Deck score keys. The bar is hidden until the server answers, so a
  dead source shows no chrome at all rather than an empty plinth over the
  table.
- **Shot Clock** (full-screen shot clock, readable from anywhere): a browser
  source `http://localhost:8000/shotclock?view=full&audio=off` at its drawn
  size (2668×1500 at (0, 312), filling the side-angle column beside the
  overhead strip — the view's type is vh/vw-sized, so a native-size source is
  all it takes to render sharp), the bar alongside if a score is wanted (add
  it with `&audio=off` too). It renders just a huge bare number — broadcast style —
  transparent background over the scene's black; add a color/shape source
  behind it only if a panel color is ever wanted. `audio=off` because the bar
  already sounds the cues (OBS keeps non-visible browser sources running, so
  two sounding sources would chime twice). The number is browser-rendered
  text: sharpness comes free at any source size.
- **Bracket** (tournament nights only, issue #43): one browser source,
  `http://localhost:8000/bracket`, filling the canvas. **Never build this scene
  by hand** — `uv run python scripts/setup_obs_bracket.py` creates it and
  re-applies the settings. The page is authored at 1920×1080 and scales itself
  to the source (`fitStage()`), the same trick the bar uses, so text rasterizes
  at the wall's own resolution instead of being upscaled by OBS. One setting is
  the opposite of the Replay badge's: `restart_when_active: **true**` — this
  page has no timeline to preserve and no pre-cut load, so reloading at the cut
  is the cheapest way to heal a source that first loaded while the server was
  down. The deck's scene key (2,3) switches to it and back, and a confirmed
  result flips to it for 30 seconds and then returns by itself, whether the
  result came from the confirm key, a concession or a phone (§C2, the confirm
  key); both only exist while a bracket is loaded.
  With none loaded the page says so rather than drawing an empty grid.
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
  Nothing is cropped: the buffer records the *output* resolution (1080p60 —
  §B3), so a replay filling the 4K canvas is a plain 2× upscale of the whole
  composed frame — same on-wall size, same recorded detail the pre-4K rig
  had. The overhead inside it is still a ~590×1080 portrait strip; blowing
  that strip up alone to fill 16:9 would be a further 3.3× upscale of a
  downscale, so the composed frame plays whole and carries both angles. A full-screen overhead replay is available any time by
  making the overhead the main *live* view — the buffer only ever holds what
  was on the TV.

**Settings:**
- On every cueclock browser source, tick **"Refresh browser when scene
  becomes active"**: if a source first loads while the server is down, OBS's
  embedded browser parks on a silent error page and never retries (the WS
  reconnect only helps *after* a successful page load) — the checkbox heals
  it on the next scene switch. Same reason the start order is server first,
  then OBS (enforced automatically once the §C4 session toggle is built).
- Canvas **3840×2160 @ 60**, output (scaled) resolution **1920×1080** —
  since 2026-08-07. The TV is a 4K panel driven at native 3840×2160: a
  1080p canvas got upscaled 2× by the projector and every browser-source
  pixel became a soft 2×2 blob (the pixelated scoreboard). The canvas is
  what the Fullscreen Projector renders, so the wall now gets native 4K;
  the *output* resolution feeds the encoders, and it stays 1080p because
  encoding the 4K60 canvas continuously (replay buffer) measurably starved
  the detect loop (~72 → ~50 Hz). Replays therefore record 1080p60 and
  upscale on playback — the same picture recordings had before. The crop
  geometry is the 2026-08-02 judgement (1080p crop, not 1200p letterbox),
  doubled. Keep color range consistent end-to-end (Full/Full or
  Limited/Limited — a mismatch shows as crushed blacks on the blue cloth).
- **The overhead's grade is one Apply LUT filter** on the Syphon source:
  `assets/obs/cueclock-overhead.cube` at amount **1.0** (since 2026-11-16;
  generated by `scripts/make_obs_lut.py`). It is the tone grade
  (`cueclock-tone.cube`) at the 0.459 amount it was dialed in at, followed by a
  hue-to-hue curve and a saturation gain for the balls.
  - **Why.** The camera has no colour matrix, so channel overlap pulls the warm
    balls together and washes them out. Through the tone grade alone the red 3
    read orange (15°), the maroon 7 brown-orange (10°), the yellow 1
    orange-yellow (38°), all at saturation ~0.65. Once the side camera was
    corrected (Part F) the two views disagreed.
  - **Measured through OBS after the change:** red 3 at 1°, maroon 7 at 355°,
    orange 5 at 28°, yellow 1 at 48°, warm balls at saturation 0.9–1.0, green 6
    and purple 4 unchanged in hue with saturation up by about a quarter. The cloth is
    untouched: (95,161,210) at 205°.
  - **Limits.** The curve stretches 15°–38° of camera hue across 0°–48°, so the
    orange 5's shaded edge leans red. Near-neutrals (saturation under 0.15) are
    not corrected, only halved in tint, which takes the cream off the cue ball.
  - The `-ccm` LUT is the older alternative. It fixes the same hues with a
    matrix but amplifies the cloth's chroma noise ~3×.
  - Judge the grade on the actual TV, not the Mac monitor.
- Output: right-click program → Fullscreen Projector on the TV display.
  General → **"Limit one fullscreen projector per screen" ON**
  (`CloseExistingProjectors`) and **"Save projectors on exit" OFF**
  (`SaveProjectors`) — both are the opposite of the OBS default, and both are
  load-bearing (settled 2026-08-03, §C4).
- Replay Buffer enabled (Settings → Output) — Stream Deck phase 2/3 owns the
  lifecycle, and `scripts/setup_obs_replay.py` owns its **length**: 600 s, so a
  held Replay key has a whole rack to play — even a long one (§C4a). Don't set
  it by hand.

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
(`com.cueclock.deck`). Ten keys on a casual night, five spare (Flag left in
checkpoint 114) — layout v2 itself went to eleven keys, down from
fourteen used and one spare, by consolidating the pairs that differed by one
bit and splitting session *mode* out as its own axis. Phase 3 landed
2026-08-02: Replay now cuts the saved buffer to the TV, seeked to the shot
(§C5). The bracket (#43, 2026-08-13) adds two keys that exist **only while a
tournament is loaded** — the deck is otherwise unchanged, which is the same
governing constraint the pages follow.

### C0. The three rules of layout v2

Everything below follows from these; they are also the module docstring of
`deck/layout.py`.

1. **A key that can't be pressed goes dark, it doesn't dim.** Visibility is
   declared per key against the session mode and the ruleset, so what is lit is
   always exactly what is pressable. **Nothing reflows** when a key leaves — it
   leaves a hole and every survivor keeps its cell, so position stays learnable
   even though the set changes. The deck goes 10 keys in Match → 7 in Run out →
   3 in No clock → 2 with the session off (Session and RECAL). Dimming survives only as typography
   (the small second line), never as state.
2. **The spine sits top-left and never moves.** Session, Mode and Replay work
   in every mode, so they hold the 2×2 corner (its fourth cell, 2,2, has been
   empty since the Flag key was removed in checkpoint 114, #55). Position therefore answers
   *does this key persist*, and colour answers *how much time do I have* — two
   questions, two channels, which is why the spine isn't one colour.
3. **Face grammar.** The `OFF`-button confusion was never the word, it was a
   *state* shown on a key read as a *command*. So: **commands** say the verb and
   nothing else (`START 30`, `PUSH`, `RECAL`); **cycles** put the current value
   bright over the next value dim, so both are on the face and neither can be
   mistaken for the other; a key carrying a **hold** names it on the second line
   (`ON` / `HOLD 1s`, `REPLAY` / `HOLD RACK`) — a gesture nothing states is a
   feature only its author knows about. The score keys are the exception, and
   only because their inverse is self-evident and lands on the wall scoreboard.
   The second line has one other legitimate job, which Replay has always done
   and #43's scene key now does too: **naming where a press goes** when the
   key's own word cannot (`BACK` / `TO LIVE`, `BACK` / `TO PLAY`) — `BACK`
   alone does not say back to what. What it may never be is *context*: a fact
   about the world with no press behind it. That is why the unarmed Confirm key
   puts its match and side on two main-tier lines instead.

Colour groups (bottom stripe, `faces.STRIPE_*`): **amber** = on the clock
(seconds matter) · **blue** = the match (no pressure) · **grey** = system. Red and green are reserved for the press-outcome flash, which
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
- **A start is preflighted, and a failed one leaves nothing behind**
  (2026-09-01). Before spawning anything the key asks pylon whether a camera is
  attached (0.6 s in a subprocess, so the always-on daemon never holds the
  pylon runtime), because "no device" is how a power cycle fails and the old
  key answered it by flashing `STARTING…` and dropping silently back to
  `START` — the reason only ever reaching `~/Library/Logs/cueclock-run.log`,
  behind the TV. And if a start dies *after* OBS was launched, OBS is quit with
  it: an orphaned OBS outlived the TV's HDMI renegotiation that day and every
  projector it opened afterwards came up with no renderer behind it
  (`obs_display_init: Failed to create swap chain`) — a grey wall that only a
  manual OBS restart cleared.
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

row 2   REPLAY       (·)          BRACKET*     (·)          RECAL†
        ▁▁grey▁▁                  ▁▁grey▁▁                  ▁▁grey▁▁

row 3   RULESET      NEW RACK     P1  3        P2  2        MATCH 4*
        ▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁blue▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁
```

`*` = only while a **bracket** is loaded (#43); dark on every other night, so
the casual deck is exactly the deck it has always been. `†` = only while the
**session is off** (#49): RECAL needs the camera `run` holds, so it is lit
exactly when every other key but Session is dark, and the two never meet.

| Pos | Key | Action | Face | Present in |
|---|---|---|---|---|
| 1,1 | **Session** | Tap while idle: spawn `run` + a `caffeinate -d` assertion, launch OBS as soon as the server answers, rebind its Syphon source. **Hold 1 s** while `ON`: `POST /api/match/clear` (end of day, 2026-08-13 — score back to PLAYER 1 / PLAYER 2, a loaded bracket archived-and-cleared, so tomorrow never wears yesterday's match), then SIGINT `run`, stop OBS's outputs, quit OBS, release the assertion. Stays lit with the server down — it is what starts the server | `START` / `STARTING…` / `ON`+`HOLD 1s` / `STOPPING…` / `DEV`; `HOLD…` while held. **A start that fails stays on the key, red and named** — `NO CAMERA`+`CHECK USB` (nothing on the USB bus) or `START FAILED`+`SEE LOG`, both cleared by pressing again, which retries. Refused (flashes `RECAL…`) while a recalibration has the camera. **Dot** = server answering: green, or **amber when the server's health line warns** (#52): the reference drift at launch crossed the warn level (> 8), or detection is below 78 Hz, the cadence every event constant was tuned at. The numbers are on the control page (bottom of the Play tab) | **always**, incl. server down |
| 1,2 | **Mode** | Cycle session mode via `POST /api/mode/{next}` — match → run out → no clock → match | current over next: `MATCH`/`RUN OUT` | always |
| 1,3 | **Start N** | `POST /api/restart` — the clock is wrong and you're about to shoot: fresh countdown, running immediately. **The face states the countdown it will hand you**, off the clock's own `current_base()` — 60 inside the WNT first-shot window, 15 under Final 10, 30 otherwise. Pressed inside a rack with no shot counted yet, it also **counts the break** vision missed, so the 60 doesn't come round again on the next shot (2026-08-05) | `START` over `30`/`60`/`15`, flashes `GO` / `ERROR` | match, run out; timed rulesets |
| 1,4 | **Push** | `POST /api/push` — push out called: clock restarts at the 60 s first-shot time and the window re-arms for a pass-back | `PUSH`, flashes `PUSH` / `ERROR` | match **and** 9-ball only |
| 1,5 | **Extension** | `POST /api/extension`. Top-right corner so a player under the clock finds it without hunting (2026-08-01). **Pips = extensions left** — but only while the pool is enforced (`rulesets.ENFORCE_EXTENSION_LIMIT`, **off since 2026-08-13**: the verbal extension can mishear table-side talk, and against an unlimited pool a false grant costs nothing; the snapshot reports the pool as 0, so the face is a bare `EXT` and every press over a live countdown is granted). Flip the flag back on and the pips return | `EXT` (+ pips while enforced), flashes `OK` / `NO EXT` | match, run out; timed rulesets |
| 2,1 | **Replay** | Save the buffer and cut to it on the TV (§C4a). **Tap** starts `1.5 s` before the last shot; **hold 0.6 s** starts at the last break and plays that rack — including after a New Rack press, until the new rack is broken. Press again to go back to live; it returns by itself when the file runs out | `REPLAY`+`HOLD RACK` → `CUEING…` / `RACK…` → `BACK`+`TO LIVE`; flashes `ERROR` and stays live if OBS can't | always |
| 2,3 | **Scene** | Swap the TV between the play scene and the bracket (#43). The deck remembers the scene it left rather than naming one, exactly as Replay does, so the play scene can be renamed in OBS without touching code | `BRACKET`+`TO TV` ⇄ `BACK`+`TO PLAY` | **bracket loaded** |
| 2,5 | **RECAL** | **Hold 2 s** (longer than Session's 1 s, so neither is mistaken for the other): run `cueclock recalibrate`, which records a fresh empty-table reference, refits undistort + calibration, and swaps the three files in only if every check passes (§E2). Takes ~10–20 s, polled, so the deck keeps drawing; a Session press meanwhile is refused. The result **stays on the key** until the next hold or the next session start | `RECAL`+`HOLD 2s` → `HOLD…` → `RECAL…` → `OK 0.8PX`/`DRIFT 2` on green, or the refusal on red (`BALLS`/`ON CLOTH`, `BAD`/`FIT 2.4px`, `CAMERA`/`BUSY`, `NO`/`TABLE`), each over `HOLD 2s` | **session off** only (server down, not mid-start/stop) |
| 3,1 | **Ruleset** | Cycle rulebooks via `POST /api/ruleset/{next}` — 8-ball → Final 10 → 9-ball → **Freestyle** (2026-08-13: match play with no shot clock; the amber clock keys go dark with the countdown, the wall shows `FREE STYLE` where the digits were, and vision journals without ever moving a clock) | current over next: `8-BALL`/`FINAL 10`/`9-BALL`/`FREE STYLE` | match, run out |
| 3,2 | **New Rack** | `POST /api/rack` — you're not shooting, you're building the next rack: clock parks, extensions refill, shot count zeroes. One key for both clocked modes, because racking and throwing fifteen balls back up are the same act to the clock | `NEW RACK`, flashes `RACK` / `ERROR` | match, run out |
| 3,3 | **P1** | **Tap** `POST /api/score/p1/increment`, **hold 0.6 s** decrement. Face shows the live score | `P1` over the score, flashes `+1` / `-1` / `ERROR` | match |
| 3,4 | **P2** | as P1 | `P2` over the score | match |
| 3,5 | **Confirm** | Commit the armed bracket result (#43). One press, four effects: the result is written and both players routed, the next ready match loads at 0–0 with its own targets, `POST /api/rack` fires (a new match is a new rack), and the TV flips to the bracket so the update is on the wall while the next pair racks — **and comes back on its own after `bracket.DWELL_S` (30 s), or the instant the clock leaves IDLE.** The bracket scene carries no shot clock and no table, so a flip out that waits on somebody remembering the scene key is a match played with nothing on the wall. **The confirm key is not the only thing that flips it**: the deck watches the draw's count of decided matches, so a match conceded or a score reported from a phone puts the bracket up for the same 30 s — to the room it is the same event. (It watches that count and not `revision`, which also moves for loads, edits and race changes.) Unarmed it is a label naming the loaded match; pressed unarmed it flashes `NOT YET` rather than swallowing the press | `MATCH 4`/`A SIDE` → `PRIYA`/`WINS 5-2`+`PRESS TO CONFIRM` on blue → `SAVING…`, flashes `SAVED`/`ERROR` | **bracket loaded**, match |

Before #43 this cell held **I shot** (`POST /api/groundtruth`, #19) from
2026-08-02 until 2026-08-05, then stood empty. The key was correct in every
respect except the one that mattered: it asked the player to do something on
every shot, and it was pressed in 2 sessions out of 29. The denominator it
existed to provide was already in the journal — `ShotClock.balls_settled` bumps
`rack_shots` exactly when vision missed the strike — so `cueclock report`
derives the miss rate with nothing asked of the table (`server/report.py`), and
the endpoint 404s on purpose per the `/score` rule. **What that removal
established still holds**: the deck never asks the player to classify anything.
Confirm is the opposite kind of key — no deadline, present only while a
tournament is, and what it commits is already on the wall before the press.

**Why arming rather than auto-advance** (#43 §7): plus/minus is the easiest
control on the deck to fat-finger, and advancing on the winning point would
turn one stray press into a bracket somebody has to unwind mid-party. Arming
costs one deliberate press and makes that failure a shrug — hold the winner's
key to drop them a point and the arm clears itself, so there is no undo to
find. The arm is *derived* from the score against each player's own race
target, not stored, which is why taking the point back is all it takes.

Press outcomes flash as background colour (green ok / red error) for 1.2 s —
words only, no decorative glyphs. The only graphics are *data*: extension pips,
the live score numeral, the Start countdown, the session health dot. The wall-TV
scoreboard is the real feedback for the score keys; the tile flash is an ack.
Deliberately absent (Steven, 2026-07-31): clock start/pause.

**What consolidated (2026-08-02).** Four score keys → two (tap adds, hold takes
back — the same rule the whole deck follows: tap does the common thing, hold
does the rare inverse). **Reset and
Force 30 → one key**: they differed by exactly one bit — both set the countdown
to full, both rack-aware, both leaving extensions and shot count alone; one
landed `IDLE`, the other `RUNNING`. Parking the clock moved to No clock mode,
where it's visible. *Force 30* was misnamed anyway: it gives 60 s inside the
first-shot window, which is exactly what `START 60` now says out loud.

**The ground-truth key (#19, 2026-08-02).** The one key that produces a
*denominator*. Everything else on the accuracy board was ranked by argument,
because "the clock missed a shot" was only ever observed when Steven happened
to notice and hit Flag (removed in checkpoint 114); pressing one key on every shot turns detection accuracy
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

**Flag is gone (checkpoint 114, #55).** It saved the last 20 s of raw frames
as a clip named for a reason picked at the table (PHANTOM / MISSED / OTHER).
It went unpressed from 2026-08-25 on: a player watching the rack does not
notice a clock fault in time, and picking a reason is a classification made
at the table. Holding the frames cost ~7.2 GB of RAM per session at 156 Hz.
Accuracy evidence now comes from the session journals (`cueclock report`,
`session-stats`) and from deliberate `record` clips.

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
produces evidence. On the TV
the shot-clock overlay **hides the digits entirely** and shows `NO CLOCK`: a
parked clock rendering a static `30` is indistinguishable from the stalled clock
a missed settle produces, which is the exact failure Steven is trained to
distrust.

### C3. Architecture

```
Stream Deck MK.2 ── USB HID ── cueclock deck (launchd agent, always on)
                                   ├─ HTTP → localhost:8000/api/*   (clock, score, ruleset)
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
(`obsws.open_projector`, monitor 0 — the mini drives one display), and **start
the OBS session recording** (`obsws.start_record`, §C7). Opening the
projector explicitly beats "Save projectors on exit", which only restores what
was open at the last quit: a session that ended without one came back to a
blank TV, and the key's promise is that the last session's state doesn't
matter.

So the deck is the **only** thing that opens a projector, and OBS's two
projector settings are set to enforce that (§B3). Both were wrong until
2026-08-03, and together they were why pressing Session appeared to "open the
presenter mode twice": **"Save projectors on exit"** restored one at OBS
launch, then `open_projector` opened a second ~10 s later. Worse,
`OpenVideoMixProjector` **stacks** — it does not reuse an open window, which
the 08-02 note here claimed it did. Counting OBS's own `saved_projectors` at
quit: three calls, three overlapping fullscreen windows on monitor 0; on one
display they are indistinguishable from one, which is how the wrong conclusion
survived. Turning **"Limit one fullscreen projector per screen"** on collapses
any repeat to a single window (verified: 3 opens → 1), which also covers the
re-open a daemon restart triggers when it re-adopts a live session. Turning
**"Save projectors on exit"** off leaves exactly one open per session, at the
cost of the TV showing the OBS UI for the ~10 s until the WebSocket answers —
flip it back on if that ever matters more; the per-screen limit makes it safe.
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
   record, stream, virtual cam). The session recording is stopped on its own
   just before, because `StopRecord` is the one request that names the file
   (§C7). Quitting OBS with *any* output active raises
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

**The empty-table reference** the toggle launches with is whatever
`config/reference.json` names: `run_command()` passes no `--reference`, and
`run` reads the pointer at launch. A RECAL hold (§C2, §E2) rewrites it, so the
next start uses the new reference without restarting the daemon.
`deck --reference` still pins one if needed.

**The run log survives a crash.** `run` is launched with `python -u`. Its
stdout is a log file, which Python otherwise block-buffers, and the
2026-11-15 session lost every line of it. `run` aborted at shutdown inside
Syphon's `stop()` (libdispatch: "Premature release of a locked queue"),
before its summary was written. The abort itself is fixed in
`display/syphon_out.py`: the server now stops only after the GPU has
finished posting the last frame.

**OBS one-time settings — all done 2026-08-02:** WebSocket server enabled
(Tools → WebSocket Server Settings; credentials copied to gitignored
`config/obs_websocket.json`), replay buffer enabled (20 s then; 600 s since
2026-08-05, and the setup script sets it — §C4a), "Save projectors
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

**Where playback starts.** The buffer holds the last `RecRBTime` seconds of
program output, so the interesting part is always at the *end* — playing from
the start is minutes of someone chalking. The vision core already timestamps
every ShotTaken to ~20 ms of contact, the server reports how long ago that was
(`GET /api/replay_anchors`), and the file ends at the save, which makes the cue
point arithmetic:

```
cursor = duration − anchor_age + display_lag − lead      (20 − 4 + 0.1 − 1.5 = 14.5 s)
```

**Two spans, one key** (2026-08-05, checkpoint 65). A **tap** anchors on the
last shot; a **hold** (0.6 s, the score keys' dwell) anchors on the rack — the
last break there has been — and plays from there to now. Same file, same seek, same badge: a span is just which of the two ages
`/api/replay_anchors` returns gets fed to the same arithmetic (`replay.Span`,
whose value *is* the field name). The one behavioural difference is the ceiling:
`MAX_PLAY_S` caps a tap at 20 s so a stale anchor can't strand the TV on the
whole buffer, and a hold lifts that to the file's length, because "play the
rack" is exactly the request to lift it.

Tap/hold rather than a chooser page because the common press
has to stay one press with nothing to read; the cost is that a tap now fires on
**release**, while a mature hold fires under the finger and says `RACK…` so the
gesture is acknowledged before the TV changes.

**The rack anchor is the break, and only ever the break** (amended 2026-08-05,
checkpoint 67). New Rack **arms** the next shot to become it rather than stamping
itself, so a rack replay opens on the break instead of on a minute of building a
triangle — *and* keeps pointing at the rack you just played until the new one is
actually broken. That second half is the common press: you run out, you rack up,
then you hold Replay to see how you did it. Taking the press as the anchor made
that press show the empty table you had just cleared (live failure below).
`no_clock` has no New Rack key, so out there the anchor is the session's first shot
until something racks — and a null anchor (or one older than the buffer) plays as
much of the buffer as there is, the same fallback a stale shot anchor already had.

> **Live failure, 2026-08-05.** `setup: POST /api/rack` and then, 2.0 s later,
> `replay: rack 2.0s ago, 598.5s file, cue 595.1s, 3.4s on air` — a correct hold,
> a correct 10-minute buffer, a seek that landed, and 3.4 s of an already-run-out
> table on the wall. The rack Steven wanted had broken ~266 s earlier and was
> sitting comfortably inside the file. Nothing about the failure looked like a
> wrong anchor from the deck's side, which is why the log line prints the age.

**The buffer has to be long enough to hold a rack, and 20 s never was.** This is
the one part of the feature that is an OBS setting rather than code:
`RecRBTime`, now **600 s** (`replay.BUFFER_TARGET_S`), written into the active
profile by `scripts/setup_obs_replay.py --buffer-seconds N` so the length the
deck assumes and the length OBS keeps can't drift. **OBS reads it when the
buffer starts**, so a change lands at the next Session start (or a manual
`Stop`/`StartReplayBuffer`), not immediately.

Ten rather than five minutes because the buffer is sized for the *worst* rack,
not the average one (Steven, 2026-08-05: *just in case there's a safety battle
and a long rack*). A rack that outruns the buffer doesn't fail — the cue point
clamps to the file's start — it silently begins mid-rack, which is the failure
here that looks most like the feature working. A safety battle is precisely the
rack worth replaying.

The cost is linear, and it is disk more than RAM. Measured 2026-08-05 (`ffprobe`
on real saves: 12.2 MB / 18.4 s and 43 MB / 77.6 s ⇒ **0.55–0.66 MB/s**), ten
minutes is ~400 MB held in OBS — nothing on 24 GB — and **~400 MB written to
`~/Movies` on every press**, up from 12 MB. Twenty presses is 8 GB and nothing
prunes that directory. It is also why `SAVE_TIMEOUT_S` went 3 → 8 s.

OBS caps the buffer by memory *and* by time, honouring whichever binds first and
saying nothing about it — so the setup script now sets `RecRBSize` from the time
target (×1.5 headroom for a busier scene: 594 MB ≈ 900 s) rather than leaving it
where it was. A memory cap that was fine for the old length is exactly how a
ten-minute buffer silently becomes a seven-minute one, and that reads as a bug
in the seek, not as a setting.

**Measured live, 2026-08-05** (both spans driven against the real OBS with the
server up but no camera, so the shot anchor was deliberately null; taken while
the buffer was still set to 300 s, and it had been running 68 s): a 68.2 s
buffer — itself the proof the setting took, since the old 20 s one capped the
file at 20 — gave `rack 37.2s ago → cue 29.6s, 38.6s on air` (68.2 − 37.2 + 0.1
− 1.5 ✓) and `no anchor → cue 48.2s, 20.0s on air` (the `MAX_PLAY_S` fallback).
Both seeks landed on a keyframe just before the target, both cut to **Replay**
and both returned to **Stream Detail**. `show()` blocked **1.2–1.4 s** — up from
the 0.34–0.63 s of checkpoint 49, which is the bigger file being written and
opened, and why the pending face matters more than it did.

The deck loop's own half of this — a tap firing on release, a hold firing under
the finger, a mature hold *not* also firing a tap on the lift — is covered by
`tests/unit/test_deck_loop.py`, which drives `_loop` with a fake device. That is
the one deck path that had never been testable without a finger, and the gesture
that made it worth building: a Replay key that silently plays the wrong span is
indistinguishable from one that works.

**A rack replay runs in real time**, and stays that way until someone verifies
that OBS honours `speed_percent` on a hardware-decoded media source. Fast
forward is honest at 30 fps (unlike slow-mo, which needs frames that were never
recorded), so 2× is one per-press setting away — but if OBS ignores it, `play_s`
is half the real airtime and the deck cuts back to live mid-rack. Until then the
bound is the Replay key itself, which says `BACK` for the whole time it runs.
The badge stops counting `LIVE IN n` above 20 s for the same reason: over a
five-minute replay that number is one nobody can use.

Constants live at the top of `deck/replay.py`: `LEAD_S` 1.5 (runway before the
shot), `DISPLAY_LAG_S` 0.1 (camera→Syphon→OBS, §B3's ~100 ms budget — the
detector's stamp is when *vision* saw the shot, the recording shows it later),
`MAX_PLAY_S` 20 and `MIN_PLAY_S` 2 (a stale anchor can't strand the TV on the
whole buffer; an instant press still shows something), `SPEED` 1.0,
`BUFFER_TARGET_S` 600 (what the setup script writes into OBS).

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

Removing it restored the checkpoint-46 problem for one checkpoint: a replay
showed a countdown that wasn't real, under a badge that said REPLAY.

**The fix was upstream, and it was not a mask** (landed 2026-08-03, checkpoint
61, issue #23). The buffer records program output, so a clean replay requires a
clean *program*; stock OBS 32.1.1 has no per-scene or per-source recording
(checked — the bundled plugin set only; a "record a different scene" path needs
Exeldro's `obs-source-record`, plus hotkey-triggered saves and file discovery,
because obs-websocket cannot save a filter-owned buffer). So the clock stopped
being drawn over live video at all: it is now the right-hand cell of the bar
along the bottom of the side-angle column (§B3). A replayed countdown is still
in the recorded pixels, but it is on a plinth rather than over the table, and
the REPLAY badge sits above it — the same relationship a broadcast has with its
own lower third. Whether that still wants covering is now a judgement on the
wall, not a code question.

The alternative — taking the clock out of OBS entirely and floating it over the
projector — was not needed, and would have cost the chime routing (§B3,
"Control audio via OBS").

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

### C5. Corpus-clip flow (removed in checkpoint 114)

Until 2026-11-15 a deck Flag key saved the last 20 s of raw frames from RAM
into `~/cueclock-dumps`, and the `/triage-clips` skill replayed each clip and
promoted it to the corpus. One measured lesson from it still holds: a
multi-GB write to the T7 saturated the USB bus it shares with every peer and
starved the live session (detect 75 → 45 Hz, the side-angle source frozen
~1 s in OBS, 2026-08-01). Keep heavy USB writes off the T7 while `run` is
live. New corpus clips come from deliberate `cueclock record` takes; why the
Flag path went is in §C2.

### C6. Bring-up checks (at the table, still open)

- [x] Does a deck keypress wake the sleeping mini? Settled 2026-08-02 without
      depending on it: Start issues `caffeinate -u -t 2` itself, so the display
      is woken explicitly rather than as a side effect of the keypress. Display
      sleep stays enabled (an M2 mini idles ~5 W); Amphetamine holds system
      sleep off, and screen lock is off so nothing covers the projector.
- [ ] `pmset -g assertions` clean after session stop.
- [ ] OBS relaunch restores projector + replay buffer, twice in a row.

### C7. Session recording + after-session housekeeping (#57, built 2026-11-16)

**OBS records every deck session.** Once OBS's WebSocket answers, the Session
key's setup retries `StartRecord` with the other prep steps (§C4); on stop it
sends `StopRecord` before the blanket output stop and appends one line to
`sessions.nosync/recordings.jsonl`: `{"path", "start_wall", "stop_wall"}`
(ISO UTC, the journal's own format). A daemon restart mid-session re-adopts the
running recording and back-dates its start by OBS's `outputDuration`, so
offsets into the file stay right. A stop OBS doesn't confirm leaves the file
**unlisted** — and an unlisted file is one housekeeping never cuts or deletes.
A terminal `run` (`DEV`) is not recorded: the deck only records sessions it owns.

What gets recorded is the profile's Simple-mode recording (read 2026-11-16 from
`basic/profiles/Untitled/basic.ini`): `~/Movies`, `hybrid_mov`, Apple H.264
(`RecQuality=HQ`), OBS's default `%CCYY-%MM-%DD %hh-%mm-%ss` file name, the
program output at 1920×1080/60 — both angles with the overlay burned in.
The replay buffer (600 s) shares that encoder in Simple mode, so recording
should add a disk write, not a second encode. **Unmeasured**: the recording's
cost to OBS frame stats and detection `hz` — read both off the first recorded
session (journal heartbeats; OBS log's "frames missed/skipped") before trusting it.
At the replay saves' measured ~6.6 Mbps it is ~3 GB an hour.

**The side-angle mic is in the mix** (checked 2026-11-16, read-only, OBS
closed): the scene collection's global `Mic/Aux` input is the
`Insta360 Link 2C Pro` CoreAudio device, unmuted, volume 1.0, on every audio
tracks, and Simple mode records track 1. The newest Replay-key save
(2026-11-14, after the Insta360 switchover) carries a stereo AAC track
peaking at −12.9 dB (mean −59.5 dB) — real room sound, not digital silence.
The voice listener opens the same mic through CoreAudio, which shares input
devices, so the two don't compete. If it ever drops out of the recording:
OBS → Settings → Audio → Global Audio Devices → **Mic/Auxiliary Audio** =
`Insta360 Link 2C Pro`, and check its meter moves in the Audio Mixer.

**`cueclock session maintain`** (`archive.py`, `cli.md`) does the rest after
the session: journal copy, parity check, moments, footage cuts into each
session directory, then moves the directory to `/Volumes/T7/cueclock_sessions/`
and prunes footage to a 10 GB budget (the listed recordings plus the Replay
key's saves — §C4a notes nothing pruned those before). It refuses to do
anything while a session is live, because a multi-GB T7 copy mid-session is the
USB stall in §C5.

It runs as its own launchd agent, `com.cueclock.maintain`
(`scripts/com.cueclock.maintain.plist.template`): every 900 s plus at load,
`ProcessType Background` + `LowPriorityIO`, logging to
`~/Library/Logs/cueclock-maintain.log`. It reads only the repo (pinned Keep
Downloaded), `logs/`, `sessions.nosync/` and `~/Movies` — nothing iCloud can
evict out from under a launchd job (see CLAUDE.md on EDEADLK). Install once:

```sh
sed -e "s|__REPO__|$HOME/Desktop/cueclock|g" -e "s|__HOME__|$HOME|g" \
    scripts/com.cueclock.maintain.plist.template \
    > ~/Library/LaunchAgents/com.cueclock.maintain.plist
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.cueclock.maintain.plist
```

---

## Part D — Audio / microphone (voice "extension" built 2026-07-31)

1. **v1 — voice "extension"** (built 2026-07-31, the only shipped use): Vosk
   small-EN model, grammar `["extension", "[unk]"]`, finals only, per-word
   confidence ≥ 0.9, 2 s cooldown, 16 kHz mono int16 — the old app's proven
   listener ported as-is (two latency changes since, below) (`design.md` §2.1 audio row). Code:
   `src/cueclock/audio/` — `spotter.py` is the pure accept/reject gate
   (recognizer injected, unit-tested with a scripted fake), `listener.py`
   owns the mic stream and thread. `cueclock run` starts it automatically
   when the model directory exists (one skip line otherwise); an accepted
   hit calls the Hub's extension exactly like the deck key. Flags:
   `--no-mic`, `--mic-device NAME`, `--mic-model PATH`. Model:
   `scripts/fetch_vosk_model.sh` fetches `vosk-model-small-en-us-0.15`
   (~40 MB) into gitignored `models/` — idempotent.
   **Latency** (checkpoint 120, #58): the final arrives ~0.44 s after the
   word ends, down from ~0.94 s. `scripts/fetch_vosk_model.sh` sets the
   model's endpoint rules 2–4 to 0.2 s of trailing silence (shipped
   0.5/0.75/1.0; 0.15 s gained nothing), and the mic is read in 100 ms
   chunks instead of v1's 250 ms. Measured on a synthesized "extension"
   with `say`. Re-run the script after any model re-download.
   Since 2026-11-16 (checkpoint 116, #58) `run` journals **every** final
   as a `voice_final` record (`text`, `conf`, `accepted`, `reason`, and from
   checkpoint 121 `peak_dbfs`), rejected and wordless ones too, so a missed "extension" leaves its confidence behind. The 0.9
   floor is v1's and gets re-anchored to those records (#58). A separate
   `listen` started while `run` is live heard nothing on 2026-11-16, so
   measure from the session journal rather than a second listener.
2. **Rejected 2026-08-02 — acoustic strike-click corroborator.** A broadband
   transient detector coincident with the cue-ball velocity impulse was the
   long-standing designed answer for slow tip-nudge vs. soft shot. Steven is
   not committing to a mic for shot detection, so it is not backlog — it is
   off the table. The cases it covered (issues #5, #8, #11) now stand on
   vision levers or, for #5, on no known path at all.
3. **v1 — spoken "Extension called"** (built 2026-08-03). A ref says it out
   loud, so every *granted* extension announces itself whoever called it —
   deck key, `/api/extension` or voice. It rides the clock's existing one-shot
   `cue` field: `hub.extension()` broadcasts `cue="extension"`, or
   `"extension_denied"` when the rules refuse, and `shotclock.html` plays it
   beside `cueTen`/`cueExpired`. Refusals sound too (a falling tone pair):
   silence there is indistinguishable from a mic that never heard the shout,
   which is the one thing it must not be confusable with.

   **How it reaches the TV** (settled 2026-08-03; this section previously
   described the opposite and the rig was set up that way). The bar source has
   **"Control audio via OBS" on** (`reroute_audio: true`) and its monitoring at
   **"Monitor Only (mute output)"** — page audio goes into the OBS mixer, and
   from there only down the monitoring path to the HDMI output. Settings →
   Audio → Monitoring Device stays `Default` = HDMI.

   ⚠️ The two switches are easy to get backwards, and each wrong pairing fails
   in a way you notice at a different time:

   - reroute **off** (as it shipped, despite the docs): CEF plays straight to
     the macOS default output. The TV beeps, but OBS never sees the audio, so
     the recording carries a **silent** AAC track — `mean_volume -91.0 dB`.
     Nobody finds out until playback.
   - reroute **on**, monitoring **off**: the mix reaches the recording and
     nothing else, so **the TV goes silent** — a shot clock that stops beeping,
     which you find out about immediately and at the worst moment.
   - reroute **on**, monitoring **and output**: both, which sounds right and is
     what put every chime in the recording *twice* — see the Mic/Aux note below
     for why the room's copy is the one to keep.

   Monitoring adds a little buffering to the speaker path that the direct CEF
   route didn't have. It eats into one margin worth naming: the mute window
   below is anchored to the *broadcast*, not to the sound leaving the TV, so
   its 2.5 s now covers the 0.98 s phrase plus broadcast→monitor→speaker
   rather than broadcast→speaker. Still comfortable, no longer untouched.

   ⚠️ Both `/shotclock` sources run at once (OBS keeps hidden browser sources
   alive), so one needs `&audio=off` or every cue fires twice — tolerable on
   a blip, obvious on a spoken phrase. The full-screen source
   (`?view=full&audio=off`) is the one that gives way; verified 2026-08-03.

   **The asset:** `src/cueclock/server/static/voice/extension_called.wav`,
   served at `/audio/extension_called.wav` (16-bit PCM WAV — no codec question
   in CEF) and **committed**. It is Kokoro-82M output — voice `af_bella`,
   "Extension called!", speed 1.10, 0.98 s — and Kokoro's weights and voices
   are Apache-2.0, so the audio may ship in a public repo. Regenerate or change
   the phrase with `uv run scripts/make_voice_assets.py`: a PEP 723 script that
   pulls its own dependencies, downloads the ~340 MB model into gitignored
   `models/kokoro/`, and needs `brew install espeak-ng` for phonemization. The
   overlay's chime fallback survives as a safety net for a deleted file, not as
   an expected state.

   **Why not macOS `say`** (the first cut, replaced the same day): its stock
   compact voices are robotic, the Premium ones (Ava, Zoe — free downloads)
   read breathy or droning on a two-word call, and `say` is macOS-only, which a
   public repo would inherit. Its output also could not be committed — Apple's
   voice data — which would have made "clone the repo, hear a chime" the normal
   experience. Kokoro fixes all three at the cost of a one-time local render.
4. **The mic must not hear the announcement.** The phrase contains the
   keyword, the TV speakers and the microphone share a room, so an un-muted
   listener calls the next extension itself. `Hub.on_extension_granted` fires
   after a granted broadcast and `cueclock run` points it at
   `ExtensionListener.mute()`, which feeds the recognizer silence for
   `ANNOUNCE_MUTE_SECONDS` (2.5 s, anchored to the 0.98 s clip plus
   broadcast→speaker lag). Driven from the *hub*, not from the listener's own
   fire, because a deck-granted extension announces without the listener
   having fired at all. The spotter's 2 s cooldown is **not** a substitute:
   Vosk finalizes at end-of-utterance, so the announcement's own final can
   land after the cooldown expires.
5. Nothing else. OBS owns the side-angle *camera*; its **audio** is a
   separate device that cueclock may open (§ below) — CoreAudio inputs are
   shareable, unlike the deck's exclusive HID claim.

**⚠️ Siri voices cannot be used — do not re-try this** (established
2026-08-03). They are not selectable with `say -v` (it silently falls back to
the default voice — five "Voice N" renders came out byte-identical to
Samantha), the "set them as System Voice and call bare `say`" trick from older
macOS write-ups does nothing on macOS 26 (13 samples across five System Voice
changes: all byte-identical), and `AVSpeechSynthesisVoice.speechVoices()` does
not list them at all. Apple reserves them for Siri and Spoken Content. Only
the Premium/Enhanced voices are reachable, and only after a manual download in
System Settings → Accessibility → Spoken Content → Manage Voices — the stock
compact voices sound robotic.

**The Mac mini has no built-in microphone** (verified 2026-07-31:
`system_profiler SPAudioDataType` lists only the built-in *speakers*, HDMI
out, and virtual devices — Apple's spec sheet agrees; mic arrays are an
iMac/MacBook feature), so the input is always something plugged in or
virtual.

**The rig's mic is the side-angle camera's own** since 2026-11-12:
`--mic-device` default `Insta360 Link 2C Pro`, the Link 2C Pro's built-in mic
(Part F). ⚠️ A camera that has never met **Insta360 Link Controller** streams
digital silence (OBS take: `-91.0 dB`) with no error anywhere; after the app has
run once, the mic carries real audio with the app closed (measured 2026-11-12:
`-62 dB` mean, `-50 dB` peak, Link Controller quit). If the voice call goes dead
after a camera swap or factory reset, that is the first thing to try.

*Until 2026-11-12 the mic was the iPhone's own, via Camo* (settled 2026-08-03,
`--mic-device` default `Camo Microphone`); the rest of this block is that
history, kept because the silent-but-healthy lesson is general. The phone is already on the USB
cable for the side-angle camera and Camo Studio already owns it, so Camo's
audio driver (`/Library/Audio/Plug-Ins/HAL/ReincubateCamoAudio.driver`) is the
input that costs nothing extra. 48 kHz stereo natively; the listener opens it
at 16 kHz mono and CoreAudio converts.

⚠️ **Camo Studio's audio toggle must be ON, and its being off is invisible
from this side.** `Camo Microphone` loads and opens with no error whether or
not Camo is feeding it, and with audio off it streams **digital silence** —
measured over a 4 s capture with speech playing in the room: peak 0 / rms 0.0
/ zero non-zero samples, identical at 48 kHz stereo, 48 kHz mono and 16 kHz
mono, while macOS's Continuity iPhone input recorded peak 1694 / rms 257 from
the same sound in the same seconds. That is a Camo setting
(`audioSettings.enabled` in `com.reincubate.macos.cam`), **not** a property of
the device, and it is what an earlier revision of this section wrongly recorded
as "Camo can't send audio". Turn it on in Camo Studio (View → Audio Settings);
patching the preference file does not work — Camo rewrites it from the peer
connection at launch, and its window exposes no accessibility tree, so this is
a click, not something a script can do.

A silent-but-healthy mic looks exactly like "nobody said the word", so if the
voice call ever goes dead, **measure the input levels first** — before
suspecting the spotter, the model, or the wiring:

```bash
uv run python -c "
import sounddevice as sd, numpy as np
d = sd.rec(int(4*16000), samplerate=16000, channels=1, dtype='int16', device='Insta360 Link 2C Pro')
sd.wait(); a = np.asarray(d)
print('peak', abs(a).max(), 'rms', round(float((a.astype(float)**2).mean()**0.5), 1))
"   # speak during the 4 s. peak 0 = the device isn't feeding audio, not a quiet room
```

**OBS's Mic/Aux source is what puts the *room* in a recording** — cue strikes,
the break, the player talking. It is `coreaudio_input_capture`, pinned to
`AppleUSBAudioEngine:Insta360:Insta360 Link 2C Pro:1100000:3` (until
2026-11-12, `CamoAudioDevice_UID`) rather than the `"default"` it shipped as.
That UID embeds the USB location (`1100000`), so **moving the camera to another
port breaks it** — re-pick the device in OBS. The camera happens to be the
system default input today, but a headset or Jump Desktop's
virtual mic would silently become the recording's audio, which is the same
never-trust-the-default rule the listener follows above.

**Unmuted, monitoring `NONE`** (settled 2026-08-03). Those are two different
switches and only one of them is dangerous:

- *Monitoring* is the hazard. On, the mic feeds the TV speakers that the mic is
  listening to — a feedback loop. It must stay `NONE`; nothing about recording
  needs it, because monitoring is the *speaker* path, not the capture path.
- *Muted* was not protecting anything, and it cost the room. Muted, OBS records
  the overlay's synthesized cues and nothing else — the shot you just played is
  silent. This section previously said it "must stay that way", conflating the
  two switches.

**The bar source is "Monitor Only (mute output)" for the same reason** — one
sound, one copy. With it at "Monitor and Output" a chime landed in the
recording **twice**: once clean from the browser and once as the room's echo of
the TV a beat later. Monitor-only keeps the speaker path (the TV still beeps —
that is what monitoring *is*) while dropping the browser's copy from the mix,
so the microphone's version is the only one recorded and it is in sync with the
balls that made the rest of the sound.

Verified 2026-08-03 by A/B, because the two settings are easy to get backwards:
mute the mic and fire a cue, and the take is `-91.0 dB` — digital silence,
proving the browser reaches the TV and nothing else. Unmute and fire the same
cue, and a ~20 dB transient appears exactly on it (`-14.7 dB` against a −33 to
−46 dB room floor), proving the TV really is still sounding it.

⚠️ **Not macOS's Continuity `<owner>'s iPhone Microphone`**, which is the same
physical microphone reached the other way and was the default for one day
(2026-08-03; it recognized speech across the room 3/3 at confidence 1.00, so
this is not about quality). Opening it makes macOS claim the phone as a
Continuity device while Camo is streaming the side angle off the same phone
over the same cable, and the side-angle feed is what pays for the collision.
It is also a Continuity *session* rather than a plain device — it can end on
its own if the phone is taken away, and it goes deaf silently when it does.
Keep it as the diagnostic that tells a dead Camo toggle from a dead room, not
as the input.

⚠️ **A wired USB mic is still the fallback** if the phone mic proves too
far-field in play: it sits at the side-angle mount, aimed at the table rather
than at the player. Any cheap wired one works; avoid Bluetooth (latency +
input flakiness). Swapping is a one-line change to `DEFAULT_DEVICE` in
`audio/listener.py`.

Design facts: keyword spotting is mic-agnostic (no calibration in the camera
sense); the input device is pinned by CoreAudio *name* — `--mic-device`
(exact match, then case-insensitive substring) — never "system default": on
no match the listener prints the available inputs and runs without the mic,
so a Bluetooth headset or the Jump Desktop virtual mic can't silently become
the listener. The flag is *defaulted* rather than required because the deck's
session key spawns a fixed argv (`deck/session.py` `run_command`) with no room
for flags — a required flag made every deck-started session silently deaf,
which is how sessions normally start. macOS asks for
microphone permission (TCC) the first time the process opens the mic —
approve the prompt for the terminal app once, or the stream delivers
silence.

Bring-up (at the table, before trusting it live):

- [ ] `uv run python -m cueclock.cli listen --devices` — confirm `Insta360
      Link 2C Pro` is listed;
      any other mic gets named with `--mic-device`.
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

**The host in `tv.json` is a hint, not an address.** DHCP moved the TV from
`192.168.4.27` to `.29` across a power cycle (2026-09-01), and from a closed
port that is indistinguishable from standby — so `ensure_on` fired nineteen
magic packets at a TV that was already awake and never confirmed, and the held
stop's `power_off` timed out with the wall left lit. Neither said why. The MAC
does not move, so `tv.resolve()` now asks the ARP cache who owns it (the same
three candidates `wol_targets` covers — the TV answers the remote protocol on
one NIC and appears in ARP on the other) and repairs the address in place. It
is a repair, not discovery: a TV in standby is off the network entirely and has
no ARP entry, which is fine, because the magic packet is a broadcast and needs
no address. A DHCP reservation on the router would remove the drift at source
and is still worth doing.

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
4. Clear the cloth, stop the session, step clear, and **hold RECAL 2 s** on
   the deck (or `uv run python -m cueclock.cli recalibrate`). It records a
   3 s reference and refits undistort + calibration, refusing without
   changing anything if a ball is on the cloth or either fit residual is
   over 2 px. On success it swaps `display_undistort.json`,
   `table_calibration.json` and the `reference.json` pointer together (the
   previous set goes to `config/backup/`), keeps the reference in
   `~/cueclock-reference/` and copies it to `tests/clips/` when the T7 is
   mounted. The key shows the residual and the new drift reading
   (`docs/cli.md` has the details). Built 2026-11-15 (#49), replacing three
   commands and a code edit.
5. Start the session. It reads the new pointer; the launch log should say
   `reference check ok` with drift near 2 (warn > 8 vs. same-mount noise ~2),
   and the control page's health line shows the same reading.
6. Re-set the OBS crop; re-judge the LUT/grade on the actual TV.
7. ChArUco intrinsics (`design.md` §4.3) only after a *final* install — void
   the moment the camera moves or refocuses.

---

## Part F — Side-angle camera: Insta360 Link 2C Pro (ordered 2026-11-11, not yet installed)

**Status: live since 2026-11-12 as the side angle (§B3) and the mic (Part D);
Camo is retired.** §F3 results so far are in F5; the §F2 apply step is
not built yet. Work
item: [#47](https://github.com/grassten/cueclock/issues/47). Nothing in the code
knows about this camera yet; §F2's apply step is a design, not a build.

### F1. Why this camera (decision record, 2026-11-11)

**What it has to do.** It has to be permanent, so no phone, no Camo Studio, and
no audio toggle that streams digital silence when it's off (Part D). It has to
match today's side angle at 1080p60 with manual exposure, white balance and
focus. Its built-in mic has to be good enough for the voice extension, so a
lapel mic is not wanted. And every setting has to be **set by code at session
start**, not clicked into a vendor app.

**Geometry.** The camera goes on the **short-end wall, ~5 ft (1.52 m) from the
near rail**, looking down the table's length.
- **Table size.** It's a 7-ft table: 1930 × 965 mm playing surface
  (`vision/calibration.py`), roughly 2.15 × 1.18 m outside. The outside figure is
  a bar-box estimate, so measure it.
- **Width.** From the end the table is only ~34° wide.
- **Length.** Near rail to far rail spans ~15–23° of the picture top to bottom,
  for a mount 5–8 ft high.
- **Against the Link's lens.** It covers 71.4° horizontally and ~44° vertically
  (83.9° diagonally), so the whole table and a far-end shooter fit with width to
  spare.
- **Zoom.** The 4K sensor allows a ~1.5× crop that still keeps more than 1080p of
  real detail.
- **The long-side case** (not chosen, kept for the record). From a long-side
  wall, a 71° lens clips a shooter at the end rail when mounted below ~7 ft;
  only the MX Brio's 82° (its 90° diagonal mode) covers them.

**Compared** (vendor specs and reviews, none tested on this rig):

| | Link 2C Pro | Logitech MX Brio | OBSBOT Meet 2 |
|---|---|---|---|
| Sensor | **1/1.3"**, dual native ISO 100–3200 | 1/2.8" (IMX415) | 1/2" |
| Horizontal FOV | 71.4° | 82° / 70° / 58° (digital crop) | 67.2° |
| Mic noise suppression | **can be turned off** ("Original" mode, kept after restart) | **can't be changed**: an undocumented block that `brioctl` deliberately doesn't write | off / weak / strong, auto gain, Near/Far setting |
| Rated pickup | ~3 m (Focus mode: 1 m) | 1.2–1.5 m | not published |
| Mic reviews | about as good as a MacBook mic; standard suppression clips soft syllables | "aggressively average" | weakest: "hollow", "a bit quiet" |
| Control path | **official** Insta360 Link SDK, macOS (§F2) | community `brioctl` (MIT, 1 star, one firmware tested) | official OBSBOT SDK; its remote-control interface (OSC) needs OBSBOT Center running |

**Why the others lost:**
- **MX Brio.** Its always-on noise suppression is the problem. Mic/Aux is what
  puts cue strikes in a recording or stream (Part D), and suppression is built to
  remove exactly that kind of click. Its wider lens only mattered for a long-side
  mount.
- **OBSBOT Meet 2.** It has the narrowest lens and the weakest mic reviews. Also,
  per [qaid/obsbot-control](https://github.com/qaid/obsbot-control), "the OBSBOT
  mic only passes audio while a video stream is open". That is Part D's silent
  but healthy mic trap again: a listener that starts before OBS hears nothing and
  reports no error.
- **Link 2 Pro.** It's the same sensor and mics on a 2-axis motorized gimbal. A
  fixed mount gains nothing from aim that can drift, park itself face-down for
  privacy, or be re-aimed by AI tracking. It was also out of stock.
- **GoPro.** Webcam mode is 1080p30 and needs a helper utility running on the
  Mac, the same kind of dependency as Camo.

⚠️ **The plain "Link 2C" (no "Pro") is a different camera.** It has the older 1/2"
sensor and different audio modes, and listings often show the two side by side.

**Mic plan.** Run the camera in **"Original"** mode, with noise suppression off,
and do all audio processing in software we control:
- **Vosk** opens the CoreAudio device directly, so it gets the raw signal.
  Keyword spotting copes with room noise.
- **OBS** puts its own **Noise Gate** filter (open/close threshold, attack, hold,
  release) and/or **Noise Suppression** filter on Mic/Aux if they're wanted.
  These affect only what's recorded or streamed, never the listener.
- **Avoid Focus mode**, which is rated for 1 m.
- **The weak spot is the far rail**, ~3.7–4.2 m from the camera and past the
  ~3 m rating. A shooter there is facing the camera, which helps. The iPhone mic
  sits in the same spot today, so the §F3 A/B is a fair comparison.

**Not yet verified.** Vendor sources disagree or say nothing on these, so §F3
settles them:
- **1080p60 without a crop.** The spec lists 1080p60. Whether that uses the full
  sensor width is not stated.
- **Turning noise suppression off.** Insta360's Link 2C Pro noise-cancellation
  page says to pick "Original" to disable it, and the *same page* also says
  "disabling noise cancellation is not supported".
- **HDR and manual exposure.** A reviewer reports that turning HDR on disables
  manual exposure. That's fine, because manual exposure is what we want.
- **Persistence.** The audio mode is documented as kept after a restart. Nothing
  says whether image settings are.
- **The SDK.** It publishes no license, and nothing says whether its own settings
  need an active video stream. The community crate
  [sglre6355/insta360-link](https://github.com/sglre6355/insta360-link) says they
  do: "Vendor settings require an active video stream."

### F2. Control path, and the session-start apply step (built 2026-11-12)

**Standard UVC controls** need no vendor code. Exposure, white balance, focus,
zoom and gain are reachable from macOS over IOKit while OBS is streaming.

**Vendor features** go through the **official Insta360 Link SDK**:
[Insta360Develop/Link-SDK](https://github.com/Insta360Develop/Link-SDK),
folder `UVCCamera_mac`, C++. The guide is
[`docs/en/sdk/link/uvc/guide.md`](https://github.com/Insta360Develop/Insta360-Developer_Docs/blob/main/docs/en/sdk/link/uvc/guide.md)
in Insta360-Developer_Docs. It lists the Link 2C Pro.
- **Calls that matter here:**
  - `EnableAutoExposure`, `SetShutterSpeed` (one value from `GetShutterSpeedList`),
    `SetISOValue` (100–3200), `SetExposureCompensation` (±3)
  - `SetWhitebalanceTemperatureMode` / `Value`
  - `EnableAutoFocus` / `SetFocusAbsolute`
  - `SetZoomAbsolute`, and `SetHostPTZ` (crop-based framing on fixed-lens models)
  - `EnableExtendFuncWork(HDR, …)`
  - `SetVideoMode(Normal)`, with tracking and gestures off
  - `EnableAudioNoiseReduction(bool)` and `SetAudioDirectionMode`
    (Standard / Wide / Focus / Music)
  - `UVCCameraDeviceWatcher`, a macOS-only hot-plug watcher
- **It is not vendored.** There's no published license, so Steven downloads it,
  and it lives outside the repo or under a gitignored path.
- **Fallback:**
  [lucasoskorep/open-360-link](https://github.com/lucasoskorep/open-360-link)
  (Rust, MIT/Apache-2.0, macOS over IOKit, audio modes included). It's reverse
  engineered and doesn't name the 2C Pro.

**As built (checkpoint 108).** Insta360's SDK is a C++ class API, which Python
can't call, so the bridge is a small helper of our own,
`tools/insta360/link_settings.cc`: `link-settings get` prints the camera's
settings as JSON, and `link-settings set key=value …` writes them in a fixed
order (modes before the values they gate) and prints the read-back.
`scripts/fetch_insta360_sdk.sh` downloads the SDK (pinned to commit `2dd2c78`,
each file checked against its SHA-256) into gitignored `vendor.nosync/` and
builds the helper there. `deck/sidecam.py` runs the helper, and
`cueclock sidecam show|save|apply` is the CLI (`docs/cli.md`).
- **Measured working while OBS streams** (2026-11-12): `noise_reduction` turned
  off with OBS capturing, and a fresh read confirmed it. The same goes for drift
  repair: `brightness` and `noise_reduction` were knocked off and put back.
- **The SDK logs to stdout** ahead of the JSON, so the parser takes the last `{`
  line.
- **No "Original" in the SDK.** The mic's noise suppression is a plain on/off
  (`EnableAudioNoiseReduction`), and that is what `noise_reduction: false`
  means. The pickup pattern (`SetAudioDirectionMode`: Standard/Wide/Focus/Music)
  has no getter, so it can't be read back and isn't enforced. Link Controller
  set it to 3 (Music) on 2026-11-12.
- **Auto exposure under the standard UVC call reads null.** The helper uses the
  extended controller's version.
- **Not built:** the hot-plug re-apply (item 3 below). A USB blip mid-session
  leaves the camera on whatever it stored until the next session start.

**The apply step.** A fourth retried item in `SessionManager._prepare_once()`
(`deck/session.py`), alongside the TV, the table feed and the projector:
1. **Run after OBS has the camera open.** `_prepare_once` only runs once OBS is
   up. Vendor settings may need an active stream, and starting a stream can reset
   settings on some webcams. Retry on the poll until OBS's capture is live.
2. **Set, then read back.** A mismatch is a failure. That's what catches firmware
   quietly ignoring "Original".
3. **Re-apply on reconnect** through the SDK's hot-plug watcher. A USB blip
   brings the camera back on its stored settings.
4. **Warn, never block.** The side angle doesn't feed detection, so this failing
   must not cost the shot clock. Log it in the same way ("setup incomplete — side
   camera"). It is never a red key like `NO CAMERA`.
5. **Keep the values in `config/`** (the `config.nosync` symlink; see CLAUDE.md on
   iCloud), with the exact values §F3 settles on. As built: `config/sidecam.json`,
   written by `cueclock sidecam save` from whatever the camera is doing. It leaves
   out values an auto mode owns (shutter and ISO under auto exposure, Kelvin under
   auto white balance, focus under autofocus), because enforcing those would
   fight the camera.

### F3. Arrival checks (do these inside the return window)

1. **Identify it.**
   - Confirm it's the Link 2C Pro. `system_profiler SPUSBDataType` should show
     USB ID `2e1a:4c05`, per sglre6355's table.
   - Note the firmware version. Update the firmware once through Insta360 Link
     Controller before testing anything else.
2. **USB.** Use a USB-C/Thunderbolt port. Never share the Basler's bus or a hub
   (§A1; the USB saturation, §C5). If the cable can't reach the mount, use a
   powered extension.
3. **1080p60 at full width.** Set OBS Video Capture Device to 1920×1080 at 60 fps,
   then compare the field of view against 30 fps and 4K30. Any crop shows up as a
   narrower picture.
4. **Framing.** Check the whole table and a far-end shooter are in frame. Pick the
   zoom/crop and the physical aim. Record the values.
5. **Exposure.**
   - HDR off, manual shutter ≥ 1/250 s at 60 fps (freezes ball motion).
   - Step the shutter speed until there's no LED flicker banding, and set
     anti-flicker to 60 Hz.
   - Set ISO, white balance in Kelvin, and manual focus on the table centre.
6. **"Original" really turns suppression off.** Record a cue strike plus room tone
   in OBS through Mic/Aux in Standard and then in Original. Original should keep
   the click and the room hiss.
7. **Mic A/B against Camo.**
   - Get its CoreAudio name with
     `uv run python -m cueclock.cli listen --devices`.
   - Run the Part D peak/rms snippet on both devices.
   - Call "extension" from the near rail **and the far rail**, on the new device
     (`run --mic-device NAME`).
8. **Cue strikes in an OBS recording.** They're the thing suppression would have
   removed.
9. **Persistence (for information only).** Unplug, reboot, and note what survives.
   The apply step sets everything anyway, so this only shapes how much
   reconnecting matters.
10. **Coexistence.** Find out whether Link Controller has to be closed for OBS (or
    the SDK) to own the camera. The goal is that no vendor app runs during a
    session.
11. **Detection rate during a session.** Check `run`'s detect Hz with the new
    camera streaming. A second USB video device must not cost the 156 Hz loop
    anything.

### F4. Switchover (after §F3 passes)

- **OBS.** In *Stream Detail*, replace the **Camo Camera** source. Point Mic/Aux
  at the new device's UID, not `"default"` (Part D explains why). Add Noise Gate /
  Noise Suppression filters only if a recording shows they're needed.
- **`audio/listener.py`.** Change `DEFAULT_DEVICE` to the new CoreAudio name, and
  rewrite the comment block above it (it's all about Camo).
- **The apply step.** Build it as §F2 describes, with tests in the existing
  `SessionManager` fake style.
- **Docs, in the same commit:**
  - §B3 sources and Part D's "the rig's mic" section
  - this Part's status line
  - README (lines ~106–107, `Camo Microphone`)
  - `docs/cli.md` (`--mic-device` default)
  - CLAUDE.md "Hardware / environment facts", the Camo bullet
- **Keep the phone and Camo Studio** as a fallback for a few sessions, then take
  Camo Studio out of the login items.
- **A CHANGELOG checkpoint entry** with `Closes #47`.

### F5. Arrival results (2026-11-12)

- **Identity:** USB `2e1a:4c05`, AVFoundation id `0x11000002e1a4c05`, camera
  firmware `v0.2.5.8_build1` (Link Controller 2.2.5 offers `0.3.5.8_build3`).
- **USB:** on a Thunderbolt/USB-C controller of its own (locationID
  `0x01100000`). The rear USB-A pair is one ASMedia controller and the Basler is
  on it, so the Stream Deck moved there (HID, negligible) to free the USB-C
  port. The camera enumerates as USB 2 high-speed.
- **Formats:** 1280×720, 1280×960, 1920×1080 and 1920×1440 at up to 60 fps;
  3840×2160 at up to 30 fps; NV12 (`420v`) and UYVY each. 60 fps is really
  60.00024 fps (see §B3 for why that matters to OBS).
- **1080p60 is full width:** OBS frames at 1080p60 and 4K30 match.
- **Mic:** silent until Link Controller had run once, then live with the app
  closed (Part D). Vosk at the rails and "Original" mode are still open.
- **Coexistence:** OBS captured the camera with Link Controller both running and
  quit. Link Controller installs its own virtual camera system extension; OBS
  doesn't use it.
- **Link Controller must be closed during play** (measured 2026-11-12). Each of
  its two open windows mid-session lined up to the minute with detection
  dropping from ~125 Hz to ~80 Hz (min 26). OBS lost 3.6% of its frames to
  rendering lag that session, against 0.0% in every earlier one. Its log shows
  a live preview and an "AI recording subsystem" starting on open. That is why
  the §F2 apply step exists: set the camera up in the app between sessions,
  quit it, and run `cueclock sidecam save`.
- **Baseline saved 2026-11-12** (`config/sidecam.json`): auto exposure at
  **-0.9 EV**, auto white balance, manual focus 85, brightness 45, HDR off, anti-flicker
  auto, mic noise suppression **off**. Under auto exposure in the evening light
  the camera chose a 1/60 s shutter at ISO 7184, which means motion blur and
  grain. A manual shutter (≥ 1/250 s, §F3.5) is the next thing to set.
- **Tuned 2026-11-15** (`config/sidecam.json`; staged balls; room lights on;
  overhead running for reference). Each value came from stills grabbed through
  OBS (`SaveSourceScreenshot`) while one setting was swept. #47 item 1.
  - **Focus 94**, not 85. Laplacian sharpness over the table peaked there at both
    ends (far 538 / mid 360 / near 572, against 149 / 93 / 188 at 85). Ball
    numbers are readable now.
  - **Manual exposure: 1/240 s at ISO 1440**, anti-flicker **60 Hz** (2). That is
    4× less motion blur than auto's 1/60. Four frames showed no LED banding, the
    same brightness as 1/120 at ISO 720, and no visible extra grain. Anti-flicker
    Auto (3) refuses 1/240; 60 Hz takes it.
  - **Manual white balance 6800 K.** That is the setting where the wall reads
    neutral (166/169/172). Manual Kelvin runs about 1500 K above what auto
    reports for the same picture. Locking it stops the colour shifting when
    someone walks into frame.
  - **Saturation 40.** At 50 or more, the cloth's blue channel clips at 255
    (86% of cloth pixels at 55, 1.7% at 40), whatever the ISO. That flattens the
    cloth and bends its hue. The OBS LUT below puts the saturation back without
    clipping.
  - EV -1.1, contrast **58** (deeper rails), sharpness **60** (80 puts halos
    around the balls), brightness 45.
- **The side camera's colour lives in OBS**: an Apply LUT filter named **Side
  colour** on Video Capture Device, at amount 1.0, running
  `assets/obs/cueclock-sidecam.cube` (generated by `scripts/make_obs_lut.py`).
  - **Why a LUT.** The camera skews saturated colours but keeps neutrals
    neutral. Raw, the red 3 reads magenta (333°), purple reads blue (226°), the
    cloth reads teal (193°, against 205° on the overhead), and yellow reads
    green-yellow. The hue *order* is intact, so the LUT is a hue-to-hue curve
    plus a saturation gain, anchored on each ball.
  - **What it replaced.** The filter before it was "Match overhead": a colour
    correction at hue −16° and saturation −0.3. It turned the cloth the right
    blue, but one rotation moves every colour, and it rendered the red 3 and 11
    orange (16°).
  - **Measured through OBS after the change:** red 3 at 358°, red 11 at 353°,
    orange 13 at 23°, yellow 9 at 50°, purple 4 at 255°, cloth (105,183,243)
    against the overhead's (97,166,217).
  - **Limits.** Purple stops at about 255°: shadowed cushion cloth sits 15°
    below it and would turn violet. The **green 6 stays teal**: the camera
    records it at the cloth's own hue (193° for both), so no filter can separate
    them.
  - **If the camera settings change, re-measure the anchors.** Camera saturation
    moves hues too: the orange 13 reads 355° at saturation 30 and 7° at 40.
- **Two helper fixes came out of that tuning** (`tools/insta360/link_settings.cc`).
  Before them, no manual-exposure setting could be enforced. (1) Auto exposure is
  switched through the extension API. The standard UVC call returns success and
  changes nothing. (2) `set` pauses 150 ms after each write, and anti-flicker
  is written before shutter. Firmware v0.2.5.8 silently drops a write that
  arrives while it is still taking the previous one, so a full `sidecam apply`
  lost its shutter and ISO. With the pause it applies 17/17 in about 3.2 s, and
  the deck's session-start `ensure()` restored a deliberately drifted camera on
  all three tries. `apply` can briefly read `video_mode` back as 255 while the
  mode switches. It reads 0 a second later.
- **Settings survive a stream close.** With OBS quit, `sidecam` read every saved
  value back. Not yet tested across an unplug or a reboot.
