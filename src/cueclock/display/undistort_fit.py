"""Fit display-grade lens distortion from the table's own straight lines.

The cloth/cushion boundary is four straight lines in reality and a crisp
blue-vs-black edge in the image, so it doubles as a calibration target: fit
Brown radial coefficients (k1, k2) that make the four undistorted boundaries
straight. On the 2026-07-27 mount this lands ~0.9 px mean residual — plenty
for the TV picture. It is NOT detection-grade: ChArUco calibration
(design.md §4.3) replaces it after the final camera install.

Focal-length note: fx is fixed at the lens's nominal value (3.5 mm / 3.45 µm
pixels ≈ 1014 px) and only k1/k2 are fitted — for pure radial undistortion a
focal error just rescales the coefficients, so nominal is fine here.
"""

from dataclasses import dataclass
from typing import cast

import cv2
import numpy as np
import numpy.typing as npt

from cueclock.display.pipeline import UndistortParams

NOMINAL_FX = 1014.0  # 3.5 mm Kowa / 3.45 um pixel pitch

_SIDES = ("top", "bottom", "left", "right")


@dataclass(frozen=True)
class BoundaryPoints:
    """Sampled cloth-boundary points per side, in distorted pixel coords."""

    sides: dict[str, npt.NDArray[np.float64]]

    @property
    def total(self) -> int:
        return sum(len(p) for p in self.sides.values())


def cloth_mask(bgr: npt.NDArray[np.uint8]) -> npt.NDArray[np.uint8]:
    """Binary mask of the largest blue-dominant blob (the cloth)."""
    b = bgr[:, :, 0].astype(np.int16)
    g = bgr[:, :, 1].astype(np.int16)
    r = bgr[:, :, 2].astype(np.int16)
    raw = (((b - r) > 25) & ((b - g) > 10)).astype(np.uint8) * 255
    closed = cv2.morphologyEx(raw, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(closed)
    if n < 2:
        raise ValueError("no blue cloth region found in frame")
    biggest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    return cast(npt.NDArray[np.uint8], (labels == biggest).astype(np.uint8))


def sample_boundary(cloth: npt.NDArray[np.uint8]) -> BoundaryPoints:
    """First/last cloth pixel per column (top/bottom) and row (left/right).

    Skips the ends of each side (corner pockets) and the center of the long
    sides (middle pockets); remaining pocket lips and balls touching a
    cushion are trimmed as outliers later.
    """
    xs = np.where(cloth.any(axis=0))[0]
    ys = np.where(cloth.any(axis=1))[0]
    x0, bw = int(xs[0]), int(xs[-1] - xs[0] + 1)
    y0, bh = int(ys[0]), int(ys[-1] - ys[0] + 1)

    sides: dict[str, npt.NDArray[np.float64]] = {}
    mid_x = x0 + bw / 2
    for name in ("top", "bottom"):
        pts = []
        for x in range(x0 + int(0.12 * bw), x0 + int(0.88 * bw)):
            if abs(x - mid_x) < 0.09 * bw:  # middle-pocket cut
                continue
            col = np.where(cloth[:, x])[0]
            if len(col):
                pts.append((float(x), float(col.min() if name == "top" else col.max())))
        sides[name] = np.array(pts, dtype=np.float64)
    for name in ("left", "right"):
        pts = []
        for y in range(y0 + int(0.15 * bh), y0 + int(0.85 * bh)):
            row = np.where(cloth[y, :])[0]
            if len(row):
                pts.append((float(row.min() if name == "left" else row.max()), float(y)))
        sides[name] = np.array(pts, dtype=np.float64)

    for name in _SIDES:
        if len(sides[name]) < 50:
            raise ValueError(f"too few boundary points on {name} side ({len(sides[name])})")
    return BoundaryPoints(sides)


def trim_outliers(boundary: BoundaryPoints) -> BoundaryPoints:
    """Drop pocket lips / touching balls: iterate a coarse parabola fit per
    side (the distorted edge is smooth and near-quadratic) and reject points
    far off it."""
    trimmed: dict[str, npt.NDArray[np.float64]] = {}
    for name, pts in boundary.sides.items():
        horizontal = name in ("top", "bottom")
        t = pts[:, 0] if horizontal else pts[:, 1]
        v = pts[:, 1] if horizontal else pts[:, 0]
        for _ in range(3):
            coef = np.polyfit(t, v, 2)
            resid = np.abs(np.polyval(coef, t) - v)
            keep = resid < max(3.0, 2.5 * float(resid.std()))
            t, v = t[keep], v[keep]
        trimmed[name] = np.column_stack([t, v] if horizontal else [v, t])
    return BoundaryPoints(trimmed)


def _straightness(
    boundary: BoundaryPoints, k: npt.NDArray[np.float64], k1: float, k2: float
) -> float:
    """Sum over sides of squared perpendicular residuals to a total-least-
    squares line, after undistorting the points (normalized image units)."""
    dist = np.array([k1, k2, 0.0, 0.0, 0.0])
    total = 0.0
    for pts in boundary.sides.values():
        und = cv2.undistortPoints(pts.reshape(-1, 1, 2), k, dist).reshape(-1, 2)
        centered = und - und.mean(axis=0)
        total += float(np.linalg.eigvalsh(centered.T @ centered)[0])
    return total


def fit_distortion(
    boundary: BoundaryPoints,
    width: int,
    height: int,
    fx: float = NOMINAL_FX,
) -> UndistortParams:
    """Grid-search (k1, k2) that straightens the four boundary lines."""
    k = np.array([[fx, 0.0, width / 2], [0.0, fx, height / 2], [0.0, 0.0, 1.0]])
    best = (0.0, 0.0, _straightness(boundary, k, 0.0, 0.0))
    for k1 in np.linspace(-0.5, 0.1, 31):
        for k2 in np.linspace(-0.3, 0.3, 31):
            v = _straightness(boundary, k, float(k1), float(k2))
            if v < best[2]:
                best = (float(k1), float(k2), v)
    k1c, k2c, _ = best
    for k1 in np.linspace(k1c - 0.03, k1c + 0.03, 25):
        for k2 in np.linspace(k2c - 0.03, k2c + 0.03, 25):
            v = _straightness(boundary, k, float(k1), float(k2))
            if v < best[2]:
                best = (float(k1), float(k2), v)

    k1_fit, k2_fit, obj = best
    residual_px = float(np.sqrt(obj / boundary.total) * fx)
    return UndistortParams(
        fx=fx,
        fy=fx,
        cx=width / 2,
        cy=height / 2,
        dist=(k1_fit, k2_fit, 0.0, 0.0, 0.0),
        width=width,
        height=height,
        fit_residual_px=residual_px,
    )


def fit_from_frame(bgr: npt.NDArray[np.uint8]) -> UndistortParams:
    """Full pipeline: frame -> cloth boundary -> fitted UndistortParams."""
    height, width = bgr.shape[:2]
    boundary = trim_outliers(sample_boundary(cloth_mask(bgr)))
    return fit_distortion(boundary, width, height)
