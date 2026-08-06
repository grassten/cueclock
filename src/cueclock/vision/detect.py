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
    merged: bool = False  # blob too big to be one clean ball: either a motion
    # streak (legitimate) or the stick/hand/another ball merged into the blob
    # (centroid contaminated). The tracker disambiguates by whether the
    # matched track is moving — a still ball cannot streak.


@dataclass(frozen=True)
class DetectConfig:
    """Geometry gates sized for the 2026-07-27 mount (ball ≈ 41-45 px)."""

    diff_threshold: int = 25
    min_area_px: float = 600.0  # ~half a nominal ball disc (partial occlusion)
    max_area_px: float = 6000.0  # ~4x nominal: streak allowance, arms rejected
    min_extent_px: int = 25  # cue stick (~12 px wide) fails this
    max_extent_px: int = 170  # ~3 ball diameters of motion streak passes
    patch_radius_px: int = 10  # central-disk radius for the whiteness sample
    # Shadow rejection (measured on the 2026-07-27 corpus): a shadow darkens
    # the cloth multiplicatively, keeping its chroma direction — brightness
    # ratio ~0.7-0.9 vs. the reference with near-identical normalized RGB.
    # The black ball, the nearest neighbor in signature, measures ratio 0.61
    # / chroma-diff 0.17 — outside on both axes.
    shadow_ratio_lo: float = 0.65
    shadow_ratio_hi: float = 0.95
    shadow_chroma_max: float = 0.15
    # Appearance-churn sample (settle-at-spin-stop gate, §4.2 rules caveat):
    # disk radius safely inside the ~21 px ball radius, so centroid jitter
    # never lets cloth pixels into the sample.
    churn_radius_px: int = 16
    # Merged-blob flag (the phantom-settle / blinded-strike root cause,
    # checkpoint 28): a stick or hand merging into a still ball's blob yanks
    # the centroid tens of mm. Measured on address_yank_strike_0801 and
    # merge_yank_0731
    # (2026-08-01): clean single still balls read area <= 1471 px^2 with
    # bbox extent <= 47 px across every frame; stick-merged blobs read
    # >= 2071 px^2 and >= 102 px. Thresholds sit in the gap with margin on
    # the clean side; contamination small enough to slip under them cannot
    # yank a centroid past the 30 mm motion latch (the yank scales with the
    # contaminating area). Not a detection gate — merged blobs are still
    # reported (§2.3 lesson 2: fast balls streak); the flag only tells the
    # tracker the centroid is untrustworthy *if* the ball is known-still.
    merged_area_px: float = 1800.0
    merged_extent_px: int = 70


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


def _is_shadow(
    rgb: tuple[float, float, float], ref_rgb: tuple[float, float, float], cfg: DetectConfig
) -> bool:
    """Mildly darker than the reference with the same chroma direction."""
    cur = sum(rgb)
    ref = sum(ref_rgb)
    if cur <= 0.0 or ref <= 0.0:
        return False
    ratio = cur / ref
    if not (cfg.shadow_ratio_lo <= ratio <= cfg.shadow_ratio_hi):
        return False
    chroma_diff = sum(abs(c / cur - rc / ref) for c, rc in zip(rgb, ref_rgb, strict=True))
    return chroma_diff <= cfg.shadow_chroma_max


def ball_churn(
    gray: npt.NDArray[np.uint8],
    prev_gray: npt.NDArray[np.uint8],
    cx: float,
    cy: float,
    radius: int,
) -> float:
    """Mean |gray - prev_gray| inside a disk at (cx, cy) — appearance churn.

    A stationary-but-spinning ball keeps changing its own pixels (the marking
    rotates) while its centroid holds still; a truly still ball shows only
    sensor noise. Measured on the spin_in_place corpus clip at 78 Hz:
    still-ball floor 1.2-1.5 gray levels (max 1.66), spinning cue 2.6-6.5,
    reaching the floor exactly when the spin visibly stops.
    """
    h, w = gray.shape
    x, y = round(cx), round(cy)
    if x - radius < 0 or y - radius < 0 or x + radius >= w or y + radius >= h:
        return 0.0  # partially off-frame: no honest sample
    window = (slice(y - radius, y + radius + 1), slice(x - radius, x + radius + 1))
    diff = np.abs(gray[window].astype(np.float32) - prev_gray[window].astype(np.float32))
    ys, xs = np.ogrid[-radius : radius + 1, -radius : radius + 1]
    return float(diff[(xs * xs + ys * ys) <= radius * radius].mean())


def detect_balls(
    bayer: npt.NDArray[np.uint8],
    reference_gray: npt.NDArray[np.uint8],
    config: DetectConfig | None = None,
    table_mask: npt.NDArray[np.uint8] | None = None,
    reference_bayer: npt.NDArray[np.uint8] | None = None,
) -> list[Detection]:
    """Find ball-like blobs in one frame. See module docstring for the gates.

    ``table_mask`` (optional, uint8 0/1) restricts detection to the cloth.
    Any blob touching the mask's border is rejected outright: nothing that is
    genuinely on the table can cross the table boundary, so a border-touching
    blob is the on-table stub of something reaching in from outside (cue
    stick, hand, arm) that the mask truncated into a deceptively ball-sized
    piece — the §2.3 lesson-5 identity thieves.
    """
    cfg = config or DetectConfig()
    gray = bayer_to_gray(bayer)
    diff = cv2.absdiff(gray, reference_gray)
    _, binary = cv2.threshold(diff, cfg.diff_threshold, 255, cv2.THRESH_BINARY)
    border_ids: set[int] = set()
    if table_mask is not None:
        binary = cv2.bitwise_and(binary, binary, mask=table_mask)
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))

    n, labels, stats, centroids = cv2.connectedComponentsWithStats(binary)
    if table_mask is not None:
        ring = cv2.subtract(table_mask, cv2.erode(table_mask, np.ones((7, 7), np.uint8)))
        border_ids = set(np.unique(labels[ring.astype(bool)]).tolist())
    detections: list[Detection] = []
    for i in range(1, n):
        if i in border_ids:
            continue
        area = float(stats[i, cv2.CC_STAT_AREA])
        if not (cfg.min_area_px <= area <= cfg.max_area_px):
            continue
        bw = int(stats[i, cv2.CC_STAT_WIDTH])
        bh = int(stats[i, cv2.CC_STAT_HEIGHT])
        if min(bw, bh) < cfg.min_extent_px or max(bw, bh) > cfg.max_extent_px:
            continue
        cx, cy = float(centroids[i, 0]), float(centroids[i, 1])
        r, g, b = _patch_rgb_means(bayer, cx, cy, cfg.patch_radius_px)
        if reference_bayer is not None:
            ref_rgb = _patch_rgb_means(reference_bayer, cx, cy, cfg.patch_radius_px)
            if _is_shadow((r, g, b), ref_rgb, cfg):
                continue
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
                merged=area > cfg.merged_area_px or max(bw, bh) > cfg.merged_extent_px,
            )
        )
    return detections
