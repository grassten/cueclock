# Basler Camera Setup — Bench Bring-Up Guide

**Scope:** assemble the camera + lens, connect it to the Mac mini, get live video on this monitor, and lock in the production camera settings. Mounting above the table is out of scope (you're handling that), but the final focus/white-balance steps below must be redone once the camera is at its final height under the table light.

**Machine:** the Mac mini M2 (Mac14,3, 24 GB, macOS 26.5) — the same machine that runs OBS and drives the TV. It has two USB-A ports (5 Gb/s), so no USB-C adapter is needed.

---

## 1. What's arriving (sanity-check on unboxing)

| Item | Exact P/N to confirm | Red flags |
|---|---|---|
| Camera | **a2A1920-160ucBAS** (label on the camera body) | `um` = mono, `gc` = GigE — wrong model |
| Lens | **Kowa LM4NCL** (3.5 mm f/1.4, C-mount) | LM5NCL = 4.5 mm, too narrow |
| Tripod plate | Basler **2200000314** (Tripod Mount ace 2) | 2000029679 = original-ace plate, wrong holes |
| USB cable | Basler 5 m passive USB 3.0, **locking Micro-B → Type-A** | Non-locking or USB 2.0 cable |

The camera is bus-powered (~3.6 W) — no power supply to look for.

## 2. Assembly

1. **Tripod plate → camera.** The 2200000314 plate screws onto a **side face** of the camera body with the small screws included with the plate. Its single ¼-20 tapped hole has a thread axis **perpendicular to the lens axis** — that's what the ball head's 90° tilt corrects when you mount it later. For bench testing you don't need the plate at all; the camera can sit on the desk or in a small clamp.
2. **Lens → camera.** Remove the camera's dust cap and the lens' rear cap, and screw the LM4NCL clockwise into the C-mount until snug (hand-tight only). The ace 2 has a standard C-mount flange — there is no back-focus adjustment on the camera; the lens' own focus ring covers it.
3. **Leave the lens' focus and iris locking screws loose** for now — you'll set focus and aperture during bring-up and lock them only after the camera is mounted at final height.

## 3. Install software

1. **pylon Camera Software Suite for macOS** — **turned out to be unnecessary (2026-07-27 bring-up):** the pypylon 26.7 wheel enumerated and streamed the camera with no system pylon install present, so the wheel bundles enough of the runtime on macOS arm64. Kept for reference in case a future pypylon version regresses: download the current release (26.x line) from [baslerweb.com → Downloads → Software](https://www.baslerweb.com/en/downloads/software/); it runs natively on Apple Silicon. If Gatekeeper blocks the `.dmg`, allow it under System Settings → Privacy & Security.

   ⚠️ **Important macOS caveat:** per Basler's ["Running pylon on Apple Silicon"](https://docs.baslerweb.com/knowledge/running-pylon-on-apple-silicon) (updated July 2026), the pylon **GUI tools — pylon Viewer included — are not available on Apple Silicon**. The SDK/runtime is native arm64, but the "open pylon Viewer → live image in minutes" flow from the build sheet does not exist on this Mac. Basler's suggested workaround is installing old pylon 6.2 (x86_64) under Rosetta 2, but that risks conflicting with the current arm64 framework install. **Recommendation: skip the Viewer entirely and use the pypylon live-view script below** — we need pypylon anyway, and it proves the exact stack production will use.
2. **pypylon** — current version 26.7 (July 2026) ships native `macosx_14_0_arm64` wheels on PyPI (`cp39-abi3`, Python 3.9–3.14). The wheel does **not** bundle the pylon runtime — the pylon suite from step 1 must be installed first. Install pypylon + opencv-python + numpy into whatever environment the new project uses (see the rewrite doc; for a quick one-off: `uvx --with pypylon --with opencv-python python live_view.py` or a throwaway venv).
3. **No camera permission needed.** A USB3 Vision camera is not an AVFoundation device — macOS TCC camera permission does not apply, and no driver/kext is involved. If Terminal/Python can enumerate USB, it can talk to the camera.

## 4. Connect

- Locking Micro-B end into the camera; tighten both thumbscrews.
- Type-A end **directly into a USB-A port on the Mac mini** — never through a hub. The USB-A ports are 5 Gb/s, which fits the full-rate budget (1920 × 1200 × 160 fps × 8-bit ≈ 369 MB/s vs ~450 MB/s usable).
- The camera's LED should light when connected.

## 5. See the feed on this monitor — live-view script

Save as `live_view.py` and run it. This is the day-1 "does everything work" proof: it opens the camera, streams at max rate, debayers for display, and prints the achieved frame rate.

```python
"""Basler a2A1920-160ucBAS live view + frame-rate check."""
import time

import cv2
from pypylon import pylon

cam = pylon.InstantCamera(pylon.TlFactory.GetInstance().CreateFirstDevice())
cam.Open()
print(f"Device: {cam.GetDeviceInfo().GetModelName()}")

# Production settings (also saved to a UserSet in §6)
cam.PixelFormat.Value = "BayerRG8"          # 8-bit Bayer — required to fit 160 fps in USB3
cam.ExposureAuto.Value = "Off"
cam.ExposureTime.Value = 4000.0             # µs; max ~6000 at 160 fps
cam.GainAuto.Value = "Off"
cam.Gain.Value = 0.0
cam.AcquisitionFrameRateEnable.Value = True
cam.AcquisitionFrameRate.Value = 160.0
print(f"Resulting frame rate: {cam.ResultingFrameRate.Value:.1f} fps")

cam.MaxNumBuffer.Value = 64
cam.StartGrabbing(pylon.GrabStrategy_LatestImageOnly)

frames, t0 = 0, time.monotonic()
while cam.IsGrabbing():
    grab = cam.RetrieveResult(5000, pylon.TimeoutHandling_ThrowException)
    if grab.GrabSucceeded():
        bayer = grab.Array                              # 1200x1920 uint8
        bgr = cv2.cvtColor(bayer, cv2.COLOR_BayerBG2BGR)   # see color note below
        frames += 1
        if frames % 32 == 0:
            fps = frames / (time.monotonic() - t0)
            cv2.putText(bgr, f"{fps:5.1f} fps", (20, 60),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.5, (0, 255, 0), 3)
        cv2.imshow("basler", bgr)
        if cv2.waitKey(1) == 27:                        # Esc
            break
    grab.Release()

cam.StopGrabbing()
cam.Close()
cv2.destroyAllWindows()
```

Notes:

- The camera's 8-bit Bayer format is **`BayerRG8`** (RGGB pattern — [Basler pixel-format docs](https://docs.baslerweb.com/pixel-format)).
- **Color sanity check — RESOLVED 2026-07-27:** OpenCV's Bayer constant naming doesn't follow GenICam's — the two conventions name the pattern from opposite corners. Verified empirically against the blue cloth: GenICam **`BayerRG8` decodes with OpenCV `COLOR_BayerBG2BGR`** (the doc's original `COLOR_BayerRG2BGR` guess swapped R↔B: white walls rendered blue, the blue cushion rendered yellow). This is settled; use `COLOR_BayerBG2BGR` everywhere.
- **Expect grey-ish color before §6:** until white balance is set (gains ship at 1.0) and with linear sensor output shown ungamma'd on an sRGB display, the blue cloth looks desaturated grey — that's normal, not a fault. The live-view script applies a display-only gamma lift.
- `cv2.imshow` at 160 fps will likely display slower than the grab rate — that's fine; the printed fps measures the *grab* loop. If display drags it down, show every 4th frame.
- The window appears on whatever display the script runs from — that's your "see it on this monitor" verification.

## 6. Lock in production settings

Once the image looks right (do the final pass at final mounting height, under the table light, at match brightness — define that as the canonical lighting state):

1. **Aperture:** f/4–5.6 (the Kowa is sharpest there; the table light has lumens to spare). **The LM4NCL iris ring is unmarked** — just O (open, f/1.4) to C (closed), so set it by brightness ratio: with exposure temporarily lowered so nothing clips, note the cloth brightness at O, then close the iris until the reading drops to **1/8–1/16 of that** (3–4 stops = f/4–5.6). The live-view script's `[`/`]` exposure hotkeys and `cloth` readout are built for this. Compensate with `ExposureTime` (up to ~6000 µs at 160 fps; global shutter keeps it clean). Then tighten the iris locking screw.
2. **Focus:** on the cloth at final height. Tighten the focus locking screw.
3. **White balance:** lay a **neutral white/grey target** (plain printer paper works) on the cloth so it fills the auto-function ROI, run `BalanceWhiteAuto = "Once"`, then confirm it reads `"Off"`. Do **not** balance on the bare cloth — the algorithm neutralizes what it sees, which would turn the blue cloth grey. Judge the result on the *debayered* view.
4. **No auto-anything:** `ExposureAuto = Off`, `GainAuto = Off`, `BalanceWhiteAuto = Off` after the once-shot. ⚠️ **Factory defaults are not neutral:** the camera ships with `BalanceWhiteAuto = "Continuous"` (grey-world auto-WB re-deciding colors every frame — it actively neutralizes the blue cloth) and `BslLightSourcePreset = "Daylight6500K"`. Set **both** to `"Off"` before doing any manual color work, or your ratio writes get silently overwritten.
5. **Persist:** save to a user set and make it the power-on default so settings survive power cycles:

   ```python
   cam.UserSetSelector.Value = "UserSet1"
   cam.UserSetSave.Execute()
   cam.UserSetDefault.Value = "UserSet1"
   ```

### Locked-in production state (2026-07-27, saved to UserSet1 = power-on default)

| Setting | Value | Note |
|---|---|---|
| PixelFormat | BayerRG8 | decode with `cv2.COLOR_BayerBG2BGR` (§5) |
| ExposureTime | 6000 µs | at the ceiling; light budget is tight (below) |
| Gain | 12.0 | ⚠️ applies as ≈×2, not ×4 — see quirk below |
| AcquisitionFrameRate | 160 → 156.2 actual | capped by default `DeviceLinkThroughputLimit` (360 MB/s); fine |
| BalanceWhiteAuto / BslLightSourcePreset | Off / Off | factory defaults were Continuous / Daylight6500K |
| BalanceRatio R/G/B | 2.005 / 1.0 / 1.893 | measured against white paper on the cloth |
| Iris | ~1 stop down from wide open | **not** the textbook f/4–5.6 — the perimeter LED light meters ~8× below the doc's assumption (cloth ≈130 at 6000 µs/gain 0 wide open), so we trade one stop of optics + ×2 gain instead. Table plane ⇒ DOF is a non-issue. |

**Firmware quirk (measured):** `Gain` and `BalanceRatio` both apply at ~half strength in
log domain — gain "12 dB" yields ×2 signal; a balance ratio of 2.005 applies ≈×1.42
(√). Never trust the units; set by measurement (the WB iteration loop converges in one
correction step).

## 7. Verification checklist

- [ ] `ResultingFrameRate` reports ≈160 with your exposure settings.
- [ ] Live view runs 60+ seconds with the measured grab fps ≈160 and no timeout/skipped-image errors.
- [ ] Blue cloth is blue (Bayer constant verified).
- [ ] Image is stable frame-to-frame: exposure/WB locked (wave a hand — brightness of the rest of the scene must not pump).
- [ ] Settings persist across unplug/replug (UserSet default verified).
- [ ] CPU headroom is sane while grabbing + debayering (Activity Monitor; the grab itself should be a few % on the M2).

## 8. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `CreateFirstDevice` throws / no camera found | pylon suite not installed (wheel needs it); cable not fully seated; try the other USB-A port; never a hub |
| Frame rate far below 160 | Exposure too long (>6.25 ms period); `AcquisitionFrameRateEnable` not set; camera negotiated USB 2.0 — check `DeviceLinkSpeed`, reseat/replace cable |
| Frame rate caps at ~140–155 with short exposure and correct settings | **Host-controller bandwidth ceiling.** Bayer8 @ 160 fps needs 369 MB/s; Basler's [measured USB 3.0 host-controller ceilings](https://assets-ctf.baslerweb.com/dg51pdwahxgw/4N0SupZ9UpIdxerGU2bXoL/3feef7a06f126ab852eaf3f69997aedf/AW00126010000_USB3.0_Host_Controllers_Max_Bandwidth.pdf) came in **under 350 MiB/s on most tested controllers** — this camera runs at the ragged edge of classic USB 3.0. The M2 Mac mini's controller is untested; if it can't sustain 160, acceptable fallbacks: run 140–150 fps (still 5× the iPhone), or crop the ROI `Height` ~10% (table doesn't fill the frame vertically), which restores full rate. Try both USB-A ports and the USB-C/TB ports (different controller lanes) |
| Sporadic dropped/incomplete frames | Raise `MaxNumBuffer`; close other USB-heavy apps; verify nothing else shares the bus; keep the Mac awake (disable USB sleep during matches) |
| Image black | Lens cap (it happens); iris fully closed; exposure microseconds vs milliseconds mix-up |
| Colors wrong | Debayer constant — see §5 color sanity check |
| Installer blocked | System Settings → Privacy & Security → allow |

## Sources

- [Basler: Running pylon on Apple Silicon](https://docs.baslerweb.com/knowledge/running-pylon-on-apple-silicon) — arm64-native SDK; GUI tools unavailable on Apple Silicon (updated 2026-07-14)
- [pypylon on PyPI](https://pypi.org/project/pypylon/) — 26.7 (2026-07-23), macOS ≥ 14 arm64 wheels, requires separate pylon install
- [pypylon GitHub](https://github.com/basler/pypylon) — install notes, `PYLON_FRAMEWORK_LOCATION` for non-default installs
- [a2A1920-160ucBAS product docs](https://docs.baslerweb.com/a2a1920-160ucbas) — 164 fps max at defaults, Sony IMX392LQR-C
- [Basler pixel-format docs](https://docs.baslerweb.com/pixel-format) — BayerRG8/10/12 on this model
- `research/shot-clock-camera-build-sheet.pdf` + `research/pool-shot-clock-project-spec.pdf` — hardware decisions, geometry, day-1 checklist this doc extends
