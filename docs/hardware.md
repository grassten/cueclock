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
  — the side angle, **1920×1440** NV12 at **60000240/1000000** fps (since
  2026-11-22, #77; 1920×1080 before). In the Stream Detail scene it sits at
  (0, 0), scale 1.3896 (2668 / 1920), bottom crop 116, which fills the
  2668×1840 column above the plinth exactly. It carries an Apply
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
  Since 2026-11-17 (checkpoint 127, #54) the same page also draws the **ball
  strip**: one chip per ball the overhead reads on a still table (cue, 1–15,
  then a grey `?` for a ball with no number), on its own ink pill just above
  the plinth's left end. It is a test graphic and shows the list as read,
  mistakes included; the control page's Setup tab has the switch (it starts
  on with every `run`). No source or scene changed for it.
  Clock and score come from one page because the drain runs the full width of
  the bar, crossing from the score area into the clock cell, and no overlay can
  span two OBS sources. It replaced two sources that were both defects: a
  193×193 corner clock sitting *on* the live side-angle video (so it was burned
  into every replay and recording), and a `/score` bar centred on the canvas whose
  right third lay across the overhead strip's bottom-left pocket.
- `/replay`, the REPLAY badge, went with the Replay key in checkpoint 144 (§C4a).
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
  load-bearing: `restart_when_active: **true**` — this page has no timeline to preserve and no pre-cut load, so reloading at the cut
  is the cheapest way to heal a source that first loaded while the server was
  down. The deck's scene key (2,3) switches to it and back, and a confirmed
  result flips to it for 30 seconds and then returns by itself, whether the
  result came from the confirm key, a concession or a phone (§C2, the confirm
  key); both only exist while a bracket is loaded.
  With none loaded the page says so rather than drawing an empty grid.
- **Canvas**: standby card shown between sessions (still image, no cueclock
  sources).
- **Replay**: removed with the Replay key in checkpoint 144 (§C4a). The scene
  and its two sources (**Replay Media**, **Replay Badge**) were deleted from
  the collection on 2026-11-22 (backup:
  `basic/scenes/Untitled.json.pre-replay-removal`).

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
  encoding the 4K60 canvas continuously (then the replay buffer, now the
  session recording) measurably starved the detect loop (~72 → ~50 Hz).
  Recordings are therefore 1080p60. The crop
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
- Replay Buffer: no longer started (checkpoint 144, §C4a). The deck launches
  OBS without `--startreplaybuffer`, so the profile's buffer setting is inert.

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
(`com.cueclock.deck`). Nine keys on a casual night, six spare (Flag left in
checkpoint 114, Replay in checkpoint 144) — layout v2 itself went to eleven keys, down from
fourteen used and one spare, by consolidating the pairs that differed by one
bit and splitting session *mode* out as its own axis. Phase 3 (instant
replay, 2026-08-02) was removed in checkpoint 144 (§C4a). The bracket (#43, 2026-08-13) adds two keys that exist **only while a
tournament is loaded** — the deck is otherwise unchanged, which is the same
governing constraint the pages follow.

### C0. The three rules of layout v2

Everything below follows from these; they are also the module docstring of
`deck/layout.py`.

1. **A key that can't be pressed goes dark, it doesn't dim.** Visibility is
   declared per key against the session mode and the clock's settings, so what is lit is
   always exactly what is pressable. **Nothing reflows** when a key leaves — it
   leaves a hole and every survivor keeps its cell, so position stays learnable
   even though the set changes. The deck goes 9 keys in Match → 6 in Run out →
   2 in No clock → 2 with the session off (Session and RECAL). Dimming survives only as typography
   (the small second line), never as state.
2. **The spine sits top-left and never moves.** Session and Mode work in
   every mode, so they hold the top of the 2×2 corner; its bottom row is
   empty — 2,2 since the Flag key was removed in checkpoint 114 (#55), 2,1
   since the Replay key was removed in checkpoint 144 (#75). Position therefore answers
   *does this key persist*, and colour answers *how much time do I have* — two
   questions, two channels, which is why the spine isn't one colour.
3. **Face grammar.** The `OFF`-button confusion was never the word, it was a
   *state* shown on a key read as a *command*. So: **commands** say the verb and
   nothing else (`START 30`, `PUSH`, `RECAL`); **cycles** put the current value
   bright over the next value dim, so both are on the face and neither can be
   mistaken for the other; a key carrying a **hold** names it on the second line
   (`ON` / `HOLD 1s`) — a gesture nothing states is a
   feature only its author knows about. The score keys are the exception, and
   only because their inverse is self-evident and lands on the wall scoreboard.
   The second line has one other legitimate job, which #43's scene key does:
   **naming where a press goes** when the key's own word cannot
   (`BACK` / `TO PLAY`) — `BACK`
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
- **Session toggle owns `run` AND OBS** (2026-07-31): the session recording
  (then the replay buffer) keeps OBS continuously encoding, and active outputs
  hold macOS power assertions
  that block sleep — so OBS dies with the session and relaunches into
  presentation on start.

### C2. Button map — current state

Two pages since checkpoint 145 (#74): the main page below, and the **SETUP
menu** (§C2b) that its SETUP key opens. 5 columns × 3 rows; unlisted cells stay
dark, as do listed keys outside the modes and settings named in **Present in**.

```
        col 1        col 2        col 3        col 4        col 5
row 1   SESSION      MODE         START 30     PUSH         EXT ••
        ▁▁grey▁▁     ▁▁grey▁▁     ▁▁▁▁▁▁▁▁▁▁▁▁amber▁▁▁▁▁▁▁▁▁▁▁▁▁

row 2   (·)          (·)          BRACKET*     (·)          RECAL†
        ▁▁grey▁▁                  ▁▁grey▁▁                  ▁▁grey▁▁

row 3   SETUP        NEW RACK     P1  3        P2  2        MATCH 4*
        ▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁blue▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁▁
```

`*` = only while a **bracket** is loaded (#43); dark on every other night, so
the casual deck is exactly the deck it has always been. `†` = only while the
**session is off** (#49): RECAL needs the camera `run` holds, so it is lit
exactly when every other key but Session is dark, and the two never meet.

| Pos | Key | Action | Face | Present in |
|---|---|---|---|---|
| 1,1 | **Session** | Tap while idle: spawn `run` + a `caffeinate -d` assertion, launch OBS as soon as the server answers, rebind its Syphon source. **Hold 1 s** while `ON`: `POST /api/match/clear` (end of day, 2026-08-13 — score back to PLAYER 1 / PLAYER 2, a loaded bracket archived-and-cleared, so tomorrow never wears yesterday's match), then SIGINT `run`, stop OBS's outputs, quit OBS, release the assertion. **Stops itself after 10 min with no shot** (same sequence, no warning; §C4). Stays lit with the server down — it is what starts the server | `START` / `STARTING…` / `ON`+`HOLD 1s` / `STOPPING…` / `DEV`; `HOLD…` while held. **A start that fails stays on the key, red and named** — `NO CAMERA`+`CHECK USB` (nothing on the USB bus) or `START FAILED`+`SEE LOG`, both cleared by pressing again, which retries. Refused (flashes `RECAL…`) while a recalibration has the camera. **A press counts only for the face it was made on** (checkpoint 135): the stop takes about 5 s on the deck's own loop, the key says `STOPPING…` from the moment the hold lands, and presses made while it works are dropped instead of being read later against `START` (on 2026-11-18 they started a second session). **Dot** = server answering: green, or **amber when the server's health line warns** (#52): the reference drift at launch crossed the warn level (> 8), or detection is below 78 Hz, the cadence every event constant was tuned at. The numbers are on the control page (bottom of the Play tab) | **always**, incl. server down |
| 1,2 | **Mode** | Cycle session mode via `POST /api/mode/{next}` — match → run out → no clock → match | current over next: `MATCH`/`RUN OUT` | always |
| 1,3 | **Start N** | `POST /api/restart` — the clock is wrong and you're about to shoot: fresh countdown, running immediately. **The face states the countdown it will hand you**, off the clock's own `current_base()` — the AFTER BREAK time inside the first-shot window (60 when set), the SHOT time otherwise (30 or 15). Pressed inside a rack with no shot counted yet, it also **counts the break** vision missed, so the 60 doesn't come round again on the next shot (2026-08-05) | `START` over `30`/`60`/`15`, flashes `GO` / `ERROR` | match, run out; CLOCK on |
| 1,4 | **Push** | `POST /api/push` — push out called: clock restarts at the AFTER BREAK time and the window re-arms for a pass-back | `PUSH`, flashes `PUSH` / `ERROR` | match **and** PUSH on |
| 1,5 | **Extension** | `POST /api/extension`. Top-right corner so a player under the clock finds it without hunting (2026-08-01). **Pips = extensions left** — but only while the pool is enforced (`settings.ENFORCE_EXTENSION_LIMIT`, **off since 2026-08-13**: the verbal extension can mishear table-side talk, and against an unlimited pool a false grant costs nothing; the snapshot reports the pool as 0, so the face is a bare `EXT` and every press over a live countdown is granted). Flip the flag back on and the pips return | `EXT` (+ pips while enforced), flashes `OK` / `NO EXT` | match, run out; CLOCK on |
| 2,3 | **Scene** | Swap the TV between the play scene and the bracket (#43). The deck remembers the scene it left rather than naming one, so the play scene can be renamed in OBS without touching code | `BRACKET`+`TO TV` ⇄ `BACK`+`TO PLAY` | **bracket loaded** |
| 2,5 | **RECAL** | **Hold 2 s** (longer than Session's 1 s, so neither is mistaken for the other): run `cueclock recalibrate`, which records a fresh empty-table reference, refits undistort + calibration, and swaps the three files in only if every check passes (§E2). Takes ~10–20 s, polled, so the deck keeps drawing; a Session press meanwhile is refused. The result **stays on the key** until the next hold or the next session start | `RECAL`+`HOLD 2s` → `HOLD…` → `RECAL…` → `OK 0.8PX`/`DRIFT 2` on green, or the refusal on red (`BALLS`/`ON CLOTH`, `BAD`/`FIT 2.4px`, `CAMERA`/`BUSY`, `NO`/`TABLE`), each over `HOLD 2s` | **session off** only (server down, not mid-start/stop) |
| 3,1 | **Setup** | Open the SETUP menu (§C2b); on the menu the same cell is **DONE** and closes it. Replaced the Ruleset key, which cycled 8-ball → Final 10 → 9-ball → Freestyle, in checkpoint 145 (#74) | `SETUP` ⇄ `DONE` | match, run out; on both pages |
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

A **mode** is orthogonal to the clock's settings: the settings say how long each
countdown is (`clock/settings.py`, sourced from published rules), a mode is what
you're doing at the table. `clock/modes.py` is the table.

| Mode | What it is | Opening shot | Clock |
|---|---|---|---|
| **match** | A rack: break, rack boundaries, the AFTER BREAK allowance | must reach 3000 mm/s (the IDLE break gate) | live, rack-aware |
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

### C2b. The SETUP menu (checkpoint 145, #74)

The clock's six settings (`clock/settings.py`), one key each, on the deck's
only second page. SETUP (3,1) opens it, the same cell reads **DONE** and
closes it, and it closes itself `daemon.MENU_IDLE_S` (10 s, a chosen value)
after the last press in it, so it is never left open over a game. SESSION and
EXT keep their cells and their function; every other main-page key is gone
while it is open, and an empty menu cell does nothing (keys are looked up on
the page that is showing). Opening or closing it drops any hold in progress.

```
        col 1        col 2        col 3        col 4        col 5
row 1   SESSION      GAME         SHOT         BREAK        EXT ••
row 2   (·)          EXT TIME     PUSH         CLOCK        (·)
row 3   DONE         (·)          (·)          (·)          (·)
```

Each setting key shows its name over its current value, the shape `START 30`
and the score keys have: on a page of six such keys the value alone would not
say which setting it is. A press moves it to the next value and applies it at
once (`POST /api/setting/{name}/{value}`); it flashes the new value.

| Key | Values (in cycle order) | What it decides |
|---|---|---|
| **GAME** | 8-BALL · 9-BALL · 10-BALL · OTHER | which ball ends the rack (read by #72) |
| **SHOT** | 30 · 15 | the countdown for each shot (15 = Ultimate Pool's last ten minutes) |
| **BREAK** | SAME · 60 | the countdown after the break or a push out (60 = WNT §5) |
| **EXT TIME** | 15 · 30 | seconds an extension adds |
| **PUSH** | OFF · ON | whether the main page shows the PUSH key |
| **CLOCK** | ON · OFF | OFF = match play with no shot clock (was `freestyle`): START and EXT go dark, the wall shows `FREE STYLE` |

A change takes effect on the next countdown: a running shot keeps its time and
the rack keeps its count, so SHOT → 15 mid-match does not hand anyone the 60 s
window again. Turning CLOCK on or off parks the clock. The settings are
remembered in `config/settings.json`, so a session starts on the last one's; an
unreadable file means the defaults (8-BALL, 30, SAME, 15, OFF, ON) and a
`settings_unreadable` journal line, never a failed start. The control page's
Setup tab has the same six as selects.

What the old rulesets were, as settings: 8-ball = the defaults; Final 10 =
SHOT 15; 9-ball (WNT) = BREAK 60, EXT TIME 30, PUSH ON; Freestyle = CLOCK OFF.

### C3. Architecture

```
Stream Deck MK.2 ── USB HID ── cueclock deck (launchd agent, always on)
                                   ├─ HTTP → localhost:8000/api/*   (clock, score, settings)
                                   ├─ obs-websocket → OBS           (projector, recording, scenes, quit)
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
face reads `STARTING…` → on the first poll where `/api/state` answers, launch OBS
(`open -a OBS --args --disable-shutdown-check`) and go
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
2. **Drive every OBS output to stopped over obs-websocket** (record, stream,
   virtual cam, and a replay buffer if one was started by hand). The session recording is stopped on its own
   just before, because `StopRecord` is the one request that names the file
   (§C7). Quitting OBS with *any* output active raises
   a modal *"OBS is still currently active"* dialog, and there is no keyboard
   at the table to click it. This **loops** rather than checking once, because
   an output can start asynchronously — on 2026-08-02 the replay buffer, then
   armed at launch, was seen "not active" by a stop pressed a few seconds after
   launch and started anyway. It also distinguishes *"nothing running"* from *"couldn't ask"*
   (OBS gone, WebSocket off): unknown is reported, never assumed quiet.
3. **Quit OBS gracefully** by AppleScript — never `kill`, which reopens it in
   safe mode without the projector, exactly what a headless rig can't fix.
4. **Backstop:** if OBS is still alive `OBS_QUIT_GRACE_S` (5 s) after the stop
   began, the dialog caught the quit anyway, so the deck clicks its OK button
   itself. The grace matters — a clean quit takes 0.3–3 s, so checking
   immediately reads *every* healthy quit as stuck.

5. **Tap the TV down to standby, last of all** — once `run` has exited and OBS
   is gone, so the wall keeps reading `STOPPING…` until the session really is
   over rather than going black mid-shutdown. One checked tap at a time, until
   the TV itself reports standby: a single tap only reaches art mode with the
   panel still lit, and a long press is unpredictable (§E1). Only an *explicit* stop does this
   — a `run` that crashed on its own leaves the TV on, because the wall is the
   one place that failure is visible from the table.

Then `STOPPING…` until the child actually exits, at which point the assertion
is released. Measured after the fix: five cycles across the race window
(3/6/8/12/20 s of uptime), stop returning in 0.7 s, OBS exiting clean every
time with the backstop never firing. A wedged `run` gets one escalation
to SIGTERM after 20 s (`cli.py` maps it to KeyboardInterrupt, so it takes the
same shutdown path) — never SIGKILL.

**A forgotten session stops itself** (#60, checkpoint 126): **10 min with no
shot** (`IDLE_STOP_S`) runs the same stop as the hold, steps 1–5, with no
warning. A session never played counts from its start, and one re-adopted
after a daemon restart counts from the restart. The deck asks
`GET /api/shot_age` for the last shot's age only once its own clock says
the limit may have passed (about once per 10 min of play), and only an answer
stops it, so a server that misses a poll can't end a live session. Every
ShotTaken counts, before every gate (phantoms, `no_clock` drills and the
vision kill switch included). DEV sessions are never touched. The deck log
(`~/Library/Logs/cueclock-deck.log`) says `session: no shot in 10 min —
stopping`. The Mac stays awake afterwards, deliberately: it is also the Plex
server. Releasing the session's `caffeinate -d` changes nothing there,
because Amphetamine and `displaysleep 0` each hold off system sleep on their
own (`pmset -g`, 2026-11-16). The auto-stop never sleeps the Mac.

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
`config/obs_websocket.json`), replay buffer enabled (no longer started since
checkpoint 144 — §C4a), "Save projectors
on exit" on, and OBS was never in Login Items. The client is
`deck/obsws.py` — one request per connection (open, authenticate, send, read,
close), every call best-effort so OBS being closed or the WebSocket server
being off can never raise into the deck loop. The Session key uses it to stop
the outputs, bind Syphon, open the projector and run the session recording;
the scene key uses it to switch to the bracket and back.

### C4a. Instant replay (removed in checkpoint 144)

From 2026-08-02 to 2026-11-22 the deck's Replay key (2,1) saved OBS's replay
buffer and cut it to the TV: a tap played the last shot, a hold played the
rack from its break. OBS launched with `--startreplaybuffer`, holding up to
600 s of program output, and the `/replay` page badged the Replay scene.
Steven removed it (#75): *"I don't really use it in its current form and we're
saving the full sessions so we don't need to save replays in this way."* It
had saved five times since August. The session recording (§C7) and the review
page are the footage now.

What went with it: `deck/replay.py`, the key and its faces,
`scripts/setup_obs_replay.py`, the `/replay` page, the rack anchor in the hub,
and `GET /api/replay_anchors`, which now 404s (the idle stop reads
`GET /api/shot_age`). The five old `Replay …` saves in `~/Movies` (1.8 GB)
were deleted the same day, and the footage budget (§C7) no longer looks for
them. Two lessons from it are
kept where they still apply: continuous 4K encoding starves detection, so the
output stays 1080p (§B3); and a stop has to loop, because an OBS output can
start after a stop has looked (§C4).

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
- [ ] OBS relaunch restores the projector, twice in a row.

### C7. Session recording + after-session housekeeping (#57, built 2026-11-16)

**OBS records every deck session.** Once OBS's WebSocket answers, the Session
key's setup retries `StartRecord` with the other prep steps (§C4); on stop it
sends `StopRecord` before the blanket output stop and appends one line to
`sessions.nosync/recordings.jsonl`: `{"path", "start_wall", "stop_wall"}`
(ISO UTC, the journal's own format). `start_wall` is when the file's first
frame was written, not when the deck asked for it: just before `StopRecord`
the deck reads `GetRecordStatus.outputDuration`, which is the file's length,
and back-dates the start by it. A freshly launched OBS acknowledges
`StartRecord` 1.2-1.8 s before it writes anything (six recordings,
2026-11-16 to 11-21), and every review clip and seek is an offset from this
instant. A daemon restart mid-session re-adopts the running recording the
same way. A stop OBS doesn't confirm leaves the file
**unlisted** — and an unlisted file is one housekeeping never cuts or deletes.
A terminal `run` (`DEV`) is not recorded: the deck only records sessions it owns.

What gets recorded is the profile's Simple-mode recording (read 2026-11-16 from
`basic/profiles/Untitled/basic.ini`): `~/Movies`, `hybrid_mov`, Apple H.264
(`RecQuality=HQ`), OBS's default `%CCYY-%MM-%DD %hh-%mm-%ss` file name, the
program output at 1920×1080/60 — both angles with the overlay burned in.
Since checkpoint 144 the recording is the only encode (the replay buffer that
shared its encoder is no longer started). **Unmeasured**: the recording's
cost to OBS frame stats and detection `hz` — read both off the first recorded
session (journal heartbeats; OBS log's "frames missed/skipped") before trusting it.
At the old replay saves' measured ~6.6 Mbps it is ~3 GB an hour.

**The side-angle mic is in the mix** (checked 2026-11-16, read-only, OBS
closed): the scene collection's global `Mic/Aux` input is the
`Insta360 Link 2C Pro` CoreAudio device, unmuted, volume 1.0, on every audio
tracks, and Simple mode records track 1. The last Replay-key save
(2026-11-14, after the Insta360 switchover) carries a stereo AAC track
peaking at −12.9 dB (mean −59.5 dB) — real room sound, not digital silence.
The voice listener opens the same mic through CoreAudio, which shares input
devices, so the two don't compete. If it ever drops out of the recording:
OBS → Settings → Audio → Global Audio Devices → **Mic/Auxiliary Audio** =
`Insta360 Link 2C Pro`, and check its meter moves in the Audio Mixer.

**`cueclock session maintain`** (`archive.py`, `cli.md`) does the rest after
the session: journal copy, parity check, moments, footage cuts into each
session directory, then moves the directory to `/Volumes/T7/cueclock_sessions/`
and prunes footage to a 10 GB budget (the listed recordings). It refuses to do
anything while a session is live, because a multi-GB T7 copy mid-session is the
USB stall in §C5.

**What the T7 holds.** Session logs + stills share a 100 GB budget, oldest
session first (`--archive-gb`); pinned sessions get 5 GB of their own. From
checkpoint 131 (#65) each ball list written at rest keeps its frame, so the
weight is **3.3 GB per hour of play**, measured on the first session that
wrote them (`20261119-020755`: 955 MB in 17.4 minutes, 459 MB of it the 142
rest lists' frames, 63 of those written while racking), and 100 GB is ~30
hours. The projection had been 2.4 GB and ~40 hours.
**With the T7 unplugged nothing is capped**: a session stays in
`sessions.nosync/` until a pass finds the drive (the internal disk had 40 GB
free on 2026-11-17, ~12 hours at that rate).

It runs as its own launchd agent, `com.cueclock.maintain`
(`scripts/com.cueclock.maintain.plist.template`): every 900 s plus at load,
`ProcessType Background` + `LowPriorityIO`, logging to
`~/Library/Logs/cueclock-maintain.log`. Background scheduling is slow on
purpose: the parity check took 384 s there on a 17-minute session that
replays in about a minute from a terminal. Each hook may therefore run for
10 minutes or the session's own length, whichever is longer
(`archive.hook_timeout`, checkpoint 135); at a fixed 10 minutes no session
past ~27 minutes could have been archived. It reads only the repo (pinned Keep
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
   floor stays (#58, closed 2026-11-21): words that sound like the keyword
   ("attention", "exception", "extended") score 1.00, the same as real
   calls, so the records hold no gap to set a floor in. A separate
   `listen` started while `run` is live heard nothing on 2026-11-16, so
   measure from the session journal rather than a second listener.
   **Gain** (checkpoint 135, 2026-11-18): the samples are amplified 40 dB
   on their way to the recognizer (`listener.MIC_GAIN_DB`), because "I still
   have to shout" was a level problem and not a recognition one. The
   camera's input volume is already at its maximum (CoreAudio scalar 1.0)
   and a voice from the table still peaks under −55 dBFS: in session
   20261119-020755 Steven's first "extension" peaked at −59.2 dBFS and went
   unheard, and the shouted repeat 2.4 s later at −56.2 dBFS was accepted.
   Replaying that recording through the same recognizer, the unheard call is
   accepted at confidence 1.00 from +6 dB of gain up, and with more gain it
   could have been quieter still: 3 dB quieter at +20 dB, 6 dB at +30, 7.5 dB
   at +40, 9 dB at +46 and no further (the room's noise is the limit from
   there). Across all six session recordings on disk (55 minutes of breaks,
   ball impacts and the TV's own cues) nothing but those two calls was
   accepted at any gain up to +46 dB; what rises is stray noises decoded as
   `[unk]` (0 finals at +20 dB, 5 at +40, 12 at +46), and an `[unk]` in the
   same final as a real call rejects it. The journal's `peak_dbfs` stays the
   mic's own level, before the gain.
   Two things the same test showed and gain does not change: **the word
   has to be said by itself** (at this gain a synthesized "I need an
   extension" is heard as `[unk] extension` and rejected), and
   **words that sound like it are it**: synthesized "attention",
   "intention", "expansion" and "tension" all come back as `extension` at
   confidence 1.00 once they are loud enough to be heard at all, because the
   grammar holds one word. That was always so; the gain moves how far away
   someone can stand and do it. Practice-room evidence only: no recording
   has talking or music in it.
   **The Voice switch** (checkpoint 146, #76): the control page's Setup tab
   turns spoken control off for a noisy room (`POST /api/voice/{on,off}`,
   `voice` in the payload). Off, a heard "extension" reaches
   `Hub.voice_extension`, is journaled as `voice_ignored` and goes no
   further; the deck's EXT key and `/api/extension` still grant. The mic
   keeps listening and `voice_final` records keep being written, so a call
   made while it was off is still in the journal. It is on with every
   `run`, like the Vision and Ball strip switches, and has no deck key.
   A spoken "push out" was looked at in the same issue and dropped (Push is
   two presses when the other player passes it back, and the pass-back has no word);
   the measurements are in #76.
2. **Disqualified for good 2026-11-18 — audio for detection.** A broadband
   transient detector coincident with the cue-ball velocity impulse (the
   acoustic strike-click corroborator) was the long-standing designed answer
   for slow tip-nudge vs. soft shot. Steven rejected it on 2026-08-02,
   doubting a click across tips, stroke power and room noise, and reopened
   it for #67 on 2026-11-18 after a quiet-room measurement looked promising.
   Room noise decided it.
   Through the side camera's own mic, in the quiet practice room, all 41
   strokes of two sessions had a sharp onset at the strike. With real songs
   mixed into the same recordings, 16 % of those strokes were still heard at
   an estimated ordinary listening volume, and 4 % of the slower third (a
   mixing test: no recording has music in it, and talking was not tested).
   The clock is built for real matches, "where there would be talking and
   music", so it would work in practice and stand down in a match. Steven:
   "OK let's permanently disqualify audio for detection on this evidence
   then". It is not backlog and not a candidate: detection is the camera
   alone (`design.md` §8, 2026-11-18; the measurements are in issue #67).
   The issues that once waited on it (#5, #8, #11) are all closed without
   it. Item 1 is a spoken command, not detection, and is unaffected.
3. **v1 — spoken "Extension called"** (built 2026-08-03). A ref says it out
   loud, so every *granted* extension announces itself whoever called it —
   deck key, `/api/extension` or voice. It rides the clock's existing one-shot
   `cue` field: `hub.extension()` broadcasts `cue="extension"`, or
   `"extension_denied"` when the rules refuse, and `shotclock.html` plays it
   beside `cueTen`/`cueBuzzer`. Refusals sound too (a falling tone pair):
   silence there is indistinguishable from a mic that never heard the shout,
   which is the one thing it must not be confusable with.

   **How it reaches the TV** (settled 2026-08-03; this section previously
   described the opposite and the rig was set up that way). The bar source has
   **"Control audio via OBS" on** (`reroute_audio: true`) and its monitoring at
   **"Monitor Only (mute output)"** — page audio goes into the OBS mixer, and
   from there only down the monitoring path to the HDMI output. Settings →
   Audio → Monitoring Device is **`HISENSE`, by name** (since 2026-11-17,
   #68). It was `Default`, which follows the macOS default output, and that
   was found on the Jump Desktop virtual device: every cue would go there and
   none to the TV. The mic input follows the same never-the-default rule
   (below). The setting lives in the profile's `basic.ini` (`[Audio]
   MonitoringDeviceId`, the TV's CoreAudio UID).

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

**Since 2026-11-22 the deck reaches the TV over a cable, not Wi-Fi (#62).**
Steven turned the TV's Wi-Fi off to keep it off the home network, and a patch
cable now runs from the Mac mini's Ethernet port (`en0`, unused because the
Mac runs on Wi-Fi) to the TV's. The two make a private link with no router and
no internet, on fixed addresses:

- **Mac** — System Settings → Network → Ethernet → Manual: `10.77.0.1`, mask
  `255.255.255.0`, **no router** (`networksetup -setmanual Ethernet 10.77.0.1
  255.255.255.0`). With no router the default route stays on Wi-Fi (`en1`), so
  the Mac's internet is untouched; `route -n get default` must still say `en1`.
- **TV** — Settings → Network → Ethernet → IP settings → Static: `10.77.0.2`,
  mask `255.255.255.0`, gateway blank. It reports "no internet", as intended.
- **`config/tv.json`** — host `10.77.0.2`, MAC `74:D0:82:76:4A:35`, the wired
  port's own MAC, read from `arp -an -i en0`. The TV still reports its Wi-Fi
  MAC (`64:6B:E7:FE:46:97`) over the protocol, so `pair_tv.py`'s printout is
  the wrong one to copy here.

Left on automatic, both ends fall back to self-assigned `169.254.x.x`
addresses, which work until the TV picks a new one. That is the drift below
again, so the addresses are fixed. The pairing certificate carried over from
Wi-Fi unchanged; no re-pair was needed.

**On the cable, standby keeps the network up, and that showed how the TV
really behaves (measured 2026-11-22).** On Wi-Fi a TV in standby could not be
asked anything. On the cable it can, and the remote protocol reports both the
power state and the foreground app:

| State | `is_on` | app |
|---|---|---|
| on | true | `com.vt.tv.tvcenter` |
| art mode | **true** | `com.vt.arttv.artmode` |
| standby (panel black) | false | `com.vt.arttv.artmode` or none |

- **POWER steps through the three: on → art mode → standby.** Each tap moves
  one step, and about one tap in five does nothing. The tap's effect is pushed
  back 1.0 to 1.2 s later.
- **The long press was never reliable.** It steps an unknown number of times:
  from on it reached standby 2 times in 3 and art mode once, and from art mode
  it stayed there once and went back on once. Art mode reports *on*, so a
  stop that left the wall in art mode looked like a successful one. That is
  probably what "the TV stays lit after a stop" was all along.
- **Settled in standby the TV ignores key presses** (ten taps on the deck's
  2 s poll did nothing), though one tap seconds into standby turned it back
  on. A magic packet to the wired MAC (at `10.77.0.255`) woke it in about a
  second, 6 times in 6. **Art mode ignores the magic packet.**
- `SLEEP` and `WAKEUP`, the Android keys that aren't toggles, do nothing in
  any state.

So `deck/tv.py` now works by the TV's report. **Off** taps POWER on one
connection, waits for the TV to push its new state, and taps again only while
the TV still isn't in standby (at most 4 taps; a dropped tap gets no push and
is simply sent again). **On** sends the magic packet unless the TV says it is
on; from art mode it first taps down to standby, because that is the only way
up. Live, 5 off/on cycles in a row: standby confirmed in 6.4 s every time and
on again 2.3 s later. Waking from art mode took 6 s, twice in 2.

What follows was written for the Wi-Fi link and stays as the record of it.

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
- **1080p60 is full width:** OBS frames at 1080p60 and 4K30 match. (Measured
  again 2026-11-22: 1080p60 is about 5% tighter than 4K30. The spacing between
  two fixed targets was 0.408 of the frame height against 0.388.)
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
    numbers are readable now. (Superseded 2026-11-22: the peak is at 90, below.)
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
- **Sharper mode, zoom and focus, 2026-11-22** (#77; stills through OBS, room
  lights on, balls staged at both ends, saved exposure). The balls are 1.32×
  bigger than before and the side column carries 1920 real camera pixels
  across, where it had about 1550.
  - **Why the old picture was soft.** OBS drew the 1080p frame at 1.72× and let
    370 px of its width run off the column, so the TV saw 1550 camera pixels
    stretched to 2668. Enlarging in OBS adds no detail. The camera's own zoom
    does, because it crops the 4K sensor before scaling down: at zoom 200 a
    target is exactly 2× wider and visibly crisper than the zoom-100 frame
    enlarged 2× in software.
  - **1920×1440 at 60 fps.** This mode keeps the full height and the middle 73%
    of the width, at 1.36× the pixel density of 1080p. It is a real 60 fps mode:
    about 57 distinct frames per second reached OBS, against 30 in 4K. The 4:3
    frame also fits the column (2668×1840) far better than 16:9 did.
  - **Zoom 120.** Zoom is anchored on the frame centre and moves in steps of
    10 (a request for 125 lands on 130). With the camera aimed where it is, 120
    is the tightest that keeps the far corner pockets; 130 clips them. Measured
    size against the old picture: 1.10× at zoom 100, 1.21× at 110, 1.32× at
    120, 1.43× at 130. Going past 120 means tilting the camera up first.
  - **Focus 90**, not 94. Laplacian sharpness (far / mid / near) at zoom 120:
    88 = 528 / 287 / 376, 89 = 624 / 308 / 366, **90 = 672 / 311 / 331**,
    91 = 660 / 275 / 266, 94 = 330 / 134 / 110. The 1080p mode peaks at the
    same place (90 = 846 / 478 / 424 against 550 / 289 / 254 at 94). The sweep
    read the same going down and coming back up. Why the peak sat at 94 on
    2026-11-15 is not known.
  - **UYVY was tried and dropped.** It scored no better than NV12 on colour
    edges and slightly worse on overall sharpness.
  - **What you lose.** The picture is the table and a little floor. A player
    shows only at the table or leaning over it.
  - **Not measured: what 1440 costs detection.** OBS handles 33% more pixels per
    frame from this source. Read the heartbeat `hz` of the first session
    against an earlier one.
  - **Changing the format over obs-websocket** takes `supported_format` (the
    value from `GetInputPropertiesListPropertyItems`) as well as `resolution`;
    `resolution` alone leaves the old format running. A zoom write sent in the
    same moment as a format switch was dropped once, so wait a few seconds.
    Capturing straight from the camera with ffmpeg hangs on a macOS
    camera-permission prompt; go through OBS.
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
