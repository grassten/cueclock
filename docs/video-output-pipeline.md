# Video Output — Basler → Python → OBS → TV

**Scope:** how the raw Basler frames become a good-looking broadcast feed on the wall TV. Everything is local to the Mac mini M2 (HDMI straight to the TV) — no network transport anywhere in this pipeline.

---

## 1. The constraint that shapes everything

OBS cannot use the Basler directly, for two independent reasons:

1. The camera is a **USB3 Vision device**, not a UVC webcam — it only speaks through the pylon SDK. It never appears in OBS's video-capture device list.
2. Even if it could, one process gets exclusive use, and the Python CV app must own the camera for detection.

So the CV app owns the camera and **republishes processed frames locally** for OBS to consume. This is a GPU texture handoff, not streaming.

## 2. Pipeline

```
CV app grab thread (BayerRG8 @ 160 fps)
  └─ display branch @ 30–60 fps (every ~3rd–5th frame):
       debayer (COLOR_BayerBG2BGR_EA, 0.22 ms)   ← *BG* constant, resolved naming mismatch
       → cv2.remap undistort (precomputed map, ~2 ms)
       → optional grade in Python (later; start with OBS filters — §4)
       → Syphon publish (Metal texture)
  ⇒ OBS "Syphon Client" source
       → scenes (overhead / overhead + iPhone side cam / replay)
       → shot-clock browser source overlay (transparent, localhost server)
       → Fullscreen Projector on the TV's HDMI display
```

**Status 2026-07-27: implemented and verified headless** (server + external client
round-trip; OBS-side visual check pending). Code: `src/cueclock/display/`
(pipeline + Syphon wrapper), CLI `replay` and `fit-undistort`. Camera-free dev
harness — replays a recorded clip to OBS:

```
PYTHONPATH=src uv run python -m cueclock.cli replay --clip tests/clips/break_shot
PYTHONPATH=src uv run python -m cueclock.cli replay --test-pattern   # labeled RGB bars
PYTHONPATH=src uv run python -m cueclock.cli live                    # camera feed (exclusive)
```

`live` is the interim way to see the table on the TV before `cueclock run`
exists; it holds the camera exclusively, so stop it before `record`.

Notes:
- **Quality debayer belongs here, not in the detection path**: edge-aware (`_EA`) at 0.22 ms/frame is effectively free at display rate and visibly better than bilinear on color edges; VNG is both slower than the frame budget allows and *not* better in OpenCV's implementation — don't use it.
- **Full-frame undistortion runs only here** (detection uses `undistortPoints` on coordinates instead — see `cv-app-rewrite.md` §4).
- **Undistort coefficients come from the table's own rails** (`cueclock fit-undistort`): the cloth/cushion boundary is straight in reality, so fitting Brown k1/k2 to straighten it needs no printed target — 0.86 px residual on the 2026-07-27 mount, `config/display_undistort.json` (gitignored, refit after every camera move). Display-grade only; ChArUco intrinsics (§4.3 of the rewrite doc) still happen after the final install for detection.
- **Square pixels are forced in the remap** (`alpha=0`'s optimal matrix is anisotropic — it would render balls ~14% elliptical). Cost: black pincushion bands top/bottom in the carpet region, which the OBS table crop removes anyway.
- 30 fps display is fine to start (TV content is a mostly-static table); 60 if the M2 shrugs, which measurements suggest it will (~2.5 ms/frame total on the display branch).
- **Crop to the table in OBS** (source Transform → Crop), not in Python: it's a taste call made at the TV, and the framing changes when the camera remounts after the cable-track/TV install.

## 3. The republish route: Syphon (recommended), pyvirtualcam (fallback)

**Syphon** is macOS's purpose-built GPU frame-sharing framework (the Mac equivalent of Spout), and OBS ships a built-in **Syphon Client** source on macOS.

- Python side: [`syphon-python`](https://github.com/cansik/syphon-python) (cansik) wraps Syphon via PyObjC with a **Metal** backend (macOS 11+): create `SyphonMetalServer`, `copy_image_to_mtl_texture(frame, texture)`, `publish_frame_texture(texture)`. Zero-copy on the GPU side; the only real cost is the numpy→texture upload of a 1920×1200 BGRA frame, trivial at 60 fps.
- **Integration facts learned 2026-07-27 (syphon-python 0.1.1**, wrapped in `src/cueclock/display/syphon_out.py`):
  - **The server process must pump its NSRunLoop** while publishing — servers answer directory-retrieve broadcasts on their runloop. Publish without pumping and the stream is invisible to every client, OBS included. `SyphonOutput.publish()` pumps, doubling as the frame-pacing sleep.
  - Python-side *clients* of an icon-less (headless Python) server crash on a library bug (`KeyError: SyphonServerDescriptionIconKey` in `SyphonServerDirectory.servers`); enumerate the ObjC directory directly with `.get()` as a workaround. OBS's native client is unaffected.
  - Texture helpers live in `syphon.utils.raw` (not `utils.raster` as older docs said); numpy frames go in **RGBA byte order** — confirmed eyes-on in OBS 2026-07-27 after BGRA rendered the blue cloth tan (the Metal texture's BGRA8 format tag does not dictate how the raw byte copy is interpreted downstream). `replay --test-pattern` shows labeled RGB bars whenever this needs re-proving.
  - **Publisher restarts self-heal (fix landed 2026-07-27).** The announce distributed notification is one-shot at server creation; if OBS's long-lived server directory misses it, the new server is invisible until the next system-wide announce-request round — this was the "OBS randomly stopped seeing my Syphon client" flakiness. OBS re-binds any server whose app name + server name match on *every* announce it hears (verified in obs-studio `mac-syphon` source), so `SyphonOutput.publish()` now re-broadcasts the announce every 2 s (framework's own `broadcastServerAnnounce`, verified against a live directory watcher). Net effect: Ctrl-C `live`/`replay` and restart it — the OBS source reattaches within ~2 s, no re-pick in source properties. Also verified: Ctrl-C/SIGTERM posts a clean retire; a `kill -9`'d publisher leaves a stale directory entry that gets pruned ~6 s after the next Syphon process starts. If the source ever stays black: check the publisher actually started (concurrent `uv run` invocations can block silently on uv's lock — one cueclock command at a time), then reopen source properties and re-pick.
- **Version caveat (the one thing to check before updating OBS):** Syphon Client worked through OBS **31.x**, then **broke in OBS 32.0.x** (blank/transparent source — [obs-studio #12684](https://github.com/obsproject/obs-studio/issues/12684), fix in [PR #12688](https://github.com/obsproject/obs-studio/pull/12688)). Install OBS 31.x, or verify the fix has shipped in whatever 32.x point release is current before upgrading. Pin the OBS version once the rig works; it's production equipment now.

**Fallback — pyvirtualcam** ([letmaik/pyvirtualcam](https://github.com/letmaik/pyvirtualcam), 0.15.0, Jan 2026): publishes to the **OBS Virtual Camera** device (requires OBS 30+ on modern macOS; one-time ritual of starting/stopping the virtual camera in OBS; macOS may require enabling the camera extension under Login Items & Extensions). Known fragility: pyvirtualcam has historically lagged OBS's macOS camera-extension changes ([#111](https://github.com/letmaik/pyvirtualcam/issues/111)), and OBS provides a single camera instance — you can't also use OBS's own virtual camera output for anything else. Works, but it's the brittle path; use only if Syphon misbehaves.

**Rejected for this setup:** NDI/DistroAV and RTSP/MJPEG servers — network-transport machinery with encode/decode latency and CPU cost, solving a problem (crossing machines) this one-machine setup doesn't have.

## 4. Dialing in the look

Target (from the project spec): "clean broadcast overhead" — televised-snooker character, not iPhone computational-photography punch. The locked camera does most of it (no exposure pumping, no WB drift, global shutter); the rest is a one-time grade.

**Where to grade — recommendation: OBS first, bake into Python later.**
- **Phase 1 (dialing in):** apply OBS filters on the Syphon source — **Color Correction** (gamma, contrast, saturation) and/or **Apply LUT** with a 3D LUT (.cube). Everything is live-adjustable on sliders while you look at the actual TV — the right feedback loop for taste decisions. GPU-side, negligible cost.
- **Starter LUTs exist** (`scripts/make_obs_lut.py` → `assets/obs/`): `cueclock-tone.cube` is the exact live-view grade that was dialed in at the table (luminance tone map + saturation, hue-preserving); `cueclock-tone-ccm.cube` adds the measured ball-set CCM — richer separation but ~3× chroma-noise amplification, which is why it was rolled back from the live view. Load either in Apply LUT and use the **amount slider** to temper at the TV; if the CCM one shimmers on the cloth, drop to the tone-only file (denoise-then-CCM in Python is the eventual fix, backlog).
- **Phase 2 (optional):** once the look is settled, bake it into the Python display branch as a LUT applied via `cv2.LUT`/numpy (or keep it in OBS forever — there's no correctness reason to move it; moving it only matters if you want the graded look in recordings made outside OBS).
- Define the **canonical lighting state** (table light setting, room lights) and always shoot matches in it — the grade and the white balance are only valid for that state.
- Judge color only after the debayer choice is final (EA), since debayer quality affects edge color fringing that a grade can't fix.

**OBS color settings gotchas** (worth setting deliberately rather than by default):
- Syphon delivers BGRA (full-range RGB). In OBS Settings → Advanced, the canvas color format default (NV12) forces an RGB→YUV conversion of the whole canvas; for an HDMI-to-TV rig, setting color format **P010/NV12 vs BGRA** mainly matters if banding appears in the cloth gradient — if it does, try BGRA canvas. Keep color range consistent end-to-end (Full in OBS + Full/PC range on the TV input, or Limited both sides) — a mismatch shows as crushed blacks or washed grays on the blue cloth.
- Canvas: **1920×1080**. The camera delivers 1200-tall, but the TV is 16:9; letting OBS scale/crop the Syphon source into a 1080p canvas beats a 1200p canvas the TV would letterbox anyway. The table spans ~75% of frame width, so there's vertical margin to crop without losing the rails. (Alternative if you want every pixel: 1920×1200 canvas, TV letterboxes. Taste call, decide at the TV.)

## 5. OBS scene & output setup

- **Sources:** Syphon Client (overhead), iPhone Continuity Camera (side angle — OBS can own it since the Python app doesn't touch it), Browser source `http://localhost:8000/shotclock` (transparent 200×200 overlay; enable **"Control audio via OBS"** so the chimes reach the TV speakers over HDMI).
- **Scenes** (per the project spec): full overhead / overhead + side-angle split / replay. Stream Deck's OBS plugin switches scenes; the same Stream Deck keeps hitting the shot-clock HTTP API directly for start/pause/reset/extension.
- **Output to TV:** right-click the program → **Fullscreen Projector** on the TV display. System Settings: prevent display sleep while OBS runs; add OBS to Login Items with the projector restored (OBS remembers projector state via "Save projectors on exit" in General settings).
- **TV settings:** Game Mode ON for the Mac's HDMI input, motion smoothing OFF (both from the spec — matters for latency and for not "enhancing" the feed).
- **Replays:** OBS's own Replay Buffer can capture the *display-rate* feed for casual instant replays now. The real 160 fps slow-mo replay (5.3× at 30 fps playback) comes later from the CV app's raw ring buffer — that's an app feature, not an OBS one (`cv-app-rewrite.md` §7 backlog).

## 6. Latency expectations

Rough budget for the live TV picture: camera exposure+readout+transfer ~10 ms → display-branch processing ~3 ms → Syphon handoff ≤1 frame → OBS compositing ~1–2 frames at 60 Hz → TV in Game Mode ~10–20 ms. Total: **on the order of 100 ms, well under the "few hundred ms is fine" bar** the spec sets for stats/replay use. The detection path is unaffected by any of this — it never leaves the Python process.

## 7. Open questions

- ~~OBS version~~ — **resolved 2026-07-27: this Mac runs OBS 32.1.1, which includes the Syphon fix** (PR #12688 merged Oct 2025 into the 32.0 milestone). No downgrade needed; still pin the version once the rig is dialed in.
- ~~Python→OBS route works?~~ — **resolved 2026-07-27: Syphon route implemented and verified headless** (publish → directory discovery → external client receives frames). Remaining: eyes-on check in OBS itself (`replay --test-pattern`, then a clip), then filters/crop/scenes at the TV.
- 1080p-crop vs 1200p-letterbox canvas — decide looking at the actual TV.
- Whether the side-angle iPhone stays wireless Continuity Camera (occasional flakiness, ~200 ms extra latency) or becomes wired — worth revisiting only if it annoys during real sessions.

## Sources

- [syphon-python](https://github.com/cansik/syphon-python) — Metal Syphon server from Python, macOS 11+
- [OBS Syphon regression in 32.0.x #12684](https://github.com/obsproject/obs-studio/issues/12684) / [fix PR #12688](https://github.com/obsproject/obs-studio/pull/12688)
- [pyvirtualcam](https://github.com/letmaik/pyvirtualcam) — 0.15.0; macOS = OBS Virtual Camera backend; OBS 30+ required; [detection issue #111](https://github.com/letmaik/pyvirtualcam/issues/111)
- Debayer/remap timings: local M2 measurements in `cv-app-rewrite.md` §6 research appendix
- `research/pool-shot-clock-project-spec.pdf` §5 (pipeline, Syphon/exclusive-camera note, scenes, Game Mode, latency bar)
