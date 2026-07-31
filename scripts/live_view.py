"""Basler a2A1920-160ucBAS live view + frame-rate + focus assist.

Day-1 bring-up tool (docs/hardware.md §A3): opens the camera with
production settings, streams at max rate, debayers for display, and overlays
grab fps, image mean brightness, and a center-ROI sharpness score with peak
hold — turn the focus ring to maximize SHARP toward its PEAK.

Keys:  Esc quits · r resets the sharpness peak (after changing the iris)
       [ halves exposure · ] doubles exposure (clamped to 50-6000 µs)
       , / . saturation down/up · ; / ' contrast down/up

Run:  uv run python scripts/live_view.py
"""

import time

import cv2
import numpy as np
from pypylon import pylon

# Display-only tone mapping, deliberately minimal: this is a diagnostic tool,
# not the OBS display path. Black/white point + gamma + contrast S-curve on
# LUMINANCE with all channels scaled by one factor (per-channel curves bend
# hue), then a saturation blend. Detection and recording consume the linear
# data untouched. Accurate color rendering (CCM etc.) belongs to the OBS/GPU
# path — the measured matrix is banked in CHANGELOG.md (2026-07-27).
BLACK, WHITE, GAMMA = 0.04, 0.94, 2.2
DISPLAY_SIZE = (
    960,
    600,
)  # half res: keeps the float tone-map fast; a viewing tool, not the OBS path


def build_lut(contrast: float) -> np.ndarray:
    x = np.linspace(0.0, 1.0, 256)
    x = np.clip((x - BLACK) / (WHITE - BLACK), 0.0, 1.0)
    y = x ** (1.0 / GAMMA)
    y = np.clip(0.5 + (y - 0.5) * contrast, 0.0, 1.0)
    return (255.0 * y).astype(np.uint8)


cam = pylon.InstantCamera(pylon.TlFactory.GetInstance().CreateFirstDevice())
cam.Open()
info = cam.GetDeviceInfo()
print(f"Device: {info.GetModelName()} (serial {info.GetSerialNumber()})")
print(f"Link speed: {cam.DeviceLinkSpeed.Value / 1e9:.1f} Gb/s")

# Production settings (locked in via a UserSet in §6 once mounted)
cam.PixelFormat.Value = "BayerRG8"  # 8-bit Bayer — required to fit 160 fps in USB3
cam.ExposureAuto.Value = "Off"
cam.ExposureTime.Value = 6000.0  # µs; ceiling ~6000 before it costs frame rate
cam.GainAuto.Value = "Off"
cam.Gain.Value = 12.0  # ≈x2 on this camera (its gain scale applies at half strength)
cam.AcquisitionFrameRateEnable.Value = True
cam.AcquisitionFrameRate.Value = 160.0
print(f"ResultingFrameRate: {cam.ResultingFrameRate.Value:.1f} fps")

cam.MaxNumBuffer.Value = 64
cam.StartGrabbing(pylon.GrabStrategy_LatestImageOnly)

frames, t0 = 0, time.monotonic()
cam_frames = 0  # includes frames the viewer skipped: true camera delivery
fps, cam_fps = 0.0, 0.0
peak = 0.0
sat, contrast = 1.20, 1.12
lut = build_lut(contrast)
while cam.IsGrabbing():
    grab = cam.RetrieveResult(5000, pylon.TimeoutHandling_ThrowException)
    if grab.GrabSucceeded():
        bayer = grab.Array  # 1200x1920 uint8
        # GenICam "BayerRG8" (RGGB) needs OpenCV's *BG* constant — the two
        # conventions name the pattern from opposite corners. Verified against
        # the blue cloth 2026-07-27 (docs/hardware.md §A3).
        bgr = cv2.cvtColor(bayer, cv2.COLOR_BayerBG2BGR)
        bgr = cv2.resize(bgr, DISPLAY_SIZE, interpolation=cv2.INTER_AREA)
        y = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        y_mapped = cv2.LUT(y, lut)
        scale = y_mapped.astype(np.float32) / np.maximum(y.astype(np.float32), 1.0)
        # Cap the scale so no channel clips: clipping a saturated color both
        # pales it and bends its hue — better to render it a touch dimmer.
        scale = np.minimum(scale, 255.0 / np.maximum(bgr.max(axis=2).astype(np.float32), 1.0))
        bgr = (bgr.astype(np.float32) * scale[:, :, None]).astype(np.uint8)
        gray = cv2.cvtColor(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
        bgr = cv2.addWeighted(bgr, sat, gray, 1.0 - sat, 0)
        frames += 1
        try:
            cam_frames += 1 + grab.GetNumberOfSkippedImages()
        except Exception:
            cam_frames += 1
        if frames % 32 == 0:
            elapsed = time.monotonic() - t0
            fps = frames / elapsed
            cam_fps = cam_frames / elapsed

        # Sharpness: Laplacian variance over the center third of one green
        # plane (plain intensity, no Bayer mosaic to pollute the gradient).
        green = bayer[0::2, 1::2]
        gh, gw = green.shape
        roi = green[gh // 3 : 2 * gh // 3, gw // 3 : 2 * gw // 3]
        sharp = cv2.Laplacian(roi, cv2.CV_64F).var()
        peak = max(peak, sharp)

        for i, text in enumerate(
            [
                f"cam {cam_fps:5.1f} fps / view {fps:4.1f}",
                f"SHARP {sharp:6.1f}",
                f"PEAK  {peak:6.1f}",
                f"cloth {roi.mean():5.1f}",  # brightness of the center ROI, not the whole frame
                f"exp {cam.ExposureTime.Value:5.0f}us",
                f"sat {sat:.2f} con {contrast:.2f}",
            ]
        ):
            cv2.putText(
                bgr,
                text,
                (12, 44 + 46 * i),
                cv2.FONT_HERSHEY_SIMPLEX,
                1.2,
                (0, 255, 0),
                3,
            )
        # Outline the region SHARP/cloth measure (center third of the frame)
        dw, dh = DISPLAY_SIZE
        cv2.rectangle(bgr, (dw // 3, dh // 3), (2 * dw // 3, 2 * dh // 3), (0, 255, 0), 1)
        cv2.putText(
            bgr,
            "SHARP/cloth ROI",
            (dw // 3, dh // 3 - 8),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (0, 255, 0),
            1,
        )
        cv2.imshow("basler", bgr)
        key = cv2.waitKey(1)
        if key == 27:  # Esc
            break
        if key == ord("r"):
            peak = 0.0
        elif key == ord("["):
            cam.ExposureTime.Value = max(50.0, cam.ExposureTime.Value / 2)
        elif key == ord("]"):
            cam.ExposureTime.Value = min(6000.0, cam.ExposureTime.Value * 2)
        elif key == ord(","):
            sat = max(0.8, sat - 0.05)
        elif key == ord("."):
            sat = min(2.5, sat + 0.05)
        elif key == ord(";"):
            contrast = max(1.0, contrast - 0.04)
            lut = build_lut(contrast)
        elif key == ord("'"):
            contrast = min(1.6, contrast + 0.04)
            lut = build_lut(contrast)
    grab.Release()

cam.StopGrabbing()
cam.Close()
cv2.destroyAllWindows()
print(f"Final grab rate: {frames / (time.monotonic() - t0):.1f} fps over {frames} frames")
