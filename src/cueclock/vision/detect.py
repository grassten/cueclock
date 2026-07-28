"""Ball detection: raw Bayer frame vs. empty-table reference → candidate balls.

Implements the §4.1 detection path: Bayer→gray, absolute difference against a
captured empty-table reference (classical segmentation — the researched
choice for a fixed camera with locked exposure; adaptive subtractors absorb
stopped balls), threshold, morphology, connected components, then geometry
gates sized for ~41-45 px balls.

Two §2.3 lessons are load-bearing here:

- Lesson 2: never shape-filter for circularity — fast balls streak into
  elongated blobs. The gates accept anything ball-sized through
  ~3 diameters of motion streak; only clearly-not-a-ball geometry (cue
  stick: thin; hands/arms: huge) is rejected.
- Lesson 5: blob identity confusion is the tracker's job, not the
  detector's. The detector reports every ball-like blob plus a whiteness
  score (for cue-ball ID, resolved globally downstream) and never guesses.

Pure module: arrays in, detections out. No I/O, no clocks, no threads.
"""

from dataclasses import dataclass
from typing import cast

import cv2
import numpy as np
import numpy.typing as npt


@dataclass(frozen=True)
class Detection:
    """One ball-like blob in one frame, in distorted pixel coordinates."""

    cx: float
    cy: float
    area: float
    bbox: tuple[int, int, int, int]  # x, y, w, h
    whiteness: float  # 0..1; high = bright and unsaturated (cue-ball-like)


@dataclass(frozen=True)
class DetectConfig:
    """Geometry gates sized for the 2026-07-27 mount (ball ≈ 41-45 px)."""

    diff_threshold: int = 25
    min_area_px: float = 600.0  # ~half a nominal ball disc (partial occlusion)
    max_area_px: float = 6000.0  # ~4x nominal: streak allowance, arms rejected
    min_extent_px: int = 25  # cue stick (~12 px wide) fails this
    max_extent_px: int = 170  # ~3 ball diameters of motion streak passes
    patch_radius_px: int = 10  # central-disk radius for the whiteness sample


def bayer_to_gray(bayer: npt.NDArray[np.uint8]) -> npt.NDArray[np.uint8]:
    """Detection-path luminance (0.12 ms on the M2 — §4 frame budget)."""
    return cast(npt.NDArray[np.uint8], cv2.cvtColor(bayer, cv2.COLOR_BayerBG2GRAY))


def build_reference(frames: list[npt.NDArray[np.uint8]]) -> npt.NDArray[np.uint8]:
    """Per-pixel median gray of empty-table frames (robust to transients)."""
    if not frames:
        raise ValueError("no frames to build a reference from")
    stack = np.stack([bayer_to_gray(f) for f in frames])
    return cast(npt.NDArray[np.uint8], np.median(stack, axis=0).astype(np.uint8))


def _patch_rgb_means(
    bayer: npt.NDArray[np.uint8], cx: float, cy: float, radius: int
) -> tuple[float, float, float]:
    """Mean R, G, B over the Bayer sites of a square patch around (cx, cy).

    BayerRG8 (GenICam): R at (even row, even col), B at (odd row, odd col),
    G at the other two sites — absolute image parity, hence the global
    coordinates below.
    """
    h, w = bayer.shape
    x0, x1 = max(0, int(cx) - radius), min(w, int(cx) + radius + 1)
    y0, y1 = max(0, int(cy) - radius), min(h, int(cy) + radius + 1)
    # shift the window start to even global parity so slicing stays aligned
    y0 += y0 % 2
    x0 += x0 % 2
    patch = bayer[y0:y1, x0:x1].astype(np.float64)
    if patch.shape[0] < 2 or patch.shape[1] < 2:
        return 0.0, 0.0, 0.0
    r = float(patch[0::2, 0::2].mean())
    b = float(patch[1::2, 1::2].mean())
    g = float((patch[0::2, 1::2].mean() + patch[1::2, 0::2].mean()) / 2.0)
    return r, g, b


def _whiteness(r: float, g: float, b: float) -> float:
    """High for bright, unsaturated patches — the cue ball's signature."""
    high = max(r, g, b)
    if high <= 0.0:
        return 0.0
    saturation = (high - min(r, g, b)) / high
    return (1.0 - saturation) * (high / 255.0)


def detect_balls(
    bayer: npt.NDArray[np.uint8],
    reference_gray: npt.NDArray[np.uint8],
    config: DetectConfig | None = None,
    table_mask: npt.NDArray[np.uint8] | None = None,
) -> list[Detection]:
    """Find ball-like blobs in one frame. See module docstring for the gates.

    ``table_mask`` (optional, uint8 0/1) restricts detection to the cloth —
    bodies leaning over a rail then only contribute their on-cloth part.
    """
    cfg = config or DetectConfig()
    gray = bayer_to_gray(bayer)
    diff = cv2.absdiff(gray, reference_gray)
    _, binary = cv2.threshold(diff, cfg.diff_threshold, 255, cv2.THRESH_BINARY)
    if table_mask is not None:
        binary = cv2.bitwise_and(binary, binary, mask=table_mask)
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))

    n, _labels, stats, centroids = cv2.connectedComponentsWithStats(binary)
    detections: list[Detection] = []
    for i in range(1, n):
        area = float(stats[i, cv2.CC_STAT_AREA])
        if not (cfg.min_area_px <= area <= cfg.max_area_px):
            continue
        bw = int(stats[i, cv2.CC_STAT_WIDTH])
        bh = int(stats[i, cv2.CC_STAT_HEIGHT])
        if min(bw, bh) < cfg.min_extent_px or max(bw, bh) > cfg.max_extent_px:
            continue
        cx, cy = float(centroids[i, 0]), float(centroids[i, 1])
        r, g, b = _patch_rgb_means(bayer, cx, cy, cfg.patch_radius_px)
        detections.append(
            Detection(
                cx=cx,
                cy=cy,
                area=area,
                bbox=(
                    int(stats[i, cv2.CC_STAT_LEFT]),
                    int(stats[i, cv2.CC_STAT_TOP]),
                    bw,
                    bh,
                ),
                whiteness=_whiteness(r, g, b),
            )
        )
    return detections
