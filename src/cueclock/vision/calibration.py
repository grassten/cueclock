"""Table calibration: distorted pixel coordinates → table millimeters.

Detection needs positions and velocities in physical units (§4.2 thresholds
are mm/s). This module fits the mapping from the table's own cloth boundary
— the same machinery the display undistort fit uses (`display.undistort_fit`)
— so the recorded corpus can be graded now, with no printed target:

    cloth boundary points → undistort → total-least-squares line per side
    → corner intersections → homography onto the playing-area rectangle

This is corpus-grade: the cloth outline (not the cushion nose line) stands in
for the playing area, so absolute scale is approximate at the few-percent
level — harmless against §4.2's generous threshold margins. This fit is what
the live rig runs on: the ChArUco calibration once planned to replace it was
never built (design.md §4.3, amendment of 2026-11-17).

Coordinates: x runs along the table's long axis (0..length_mm), y across
(0..width_mm), origin at the cloth corner that appears top-left in the image.
Corner pockets sit at the rectangle corners, middle pockets at x = length/2 —
exactly the geometry `EventConfig.pocket_centers()` assumes.
"""

import json
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import numpy.typing as npt

from cueclock.display.pipeline import UndistortParams
from cueclock.display.undistort_fit import (
    BoundaryPoints,
    cloth_mask,
    sample_boundary,
    trim_outliers,
)

DEFAULT_CALIBRATION_PATH = Path("config/table_calibration.json")

# 7 ft table playing area, 76 x 38 in (matches EventConfig defaults).
TABLE_LENGTH_MM = 1930.0
TABLE_WIDTH_MM = 965.0

_CORNER_ORDER = ("top-left", "top-right", "bottom-right", "bottom-left")


def _tls_line(
    pts: npt.NDArray[np.float64],
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """Total-least-squares line through points: (centroid, unit direction)."""
    centroid = pts.mean(axis=0)
    _u, _s, vt = np.linalg.svd(pts - centroid)
    return centroid, vt[0]


def _intersect(
    p1: npt.NDArray[np.float64],
    d1: npt.NDArray[np.float64],
    p2: npt.NDArray[np.float64],
    d2: npt.NDArray[np.float64],
) -> npt.NDArray[np.float64]:
    """Intersection of two point+direction lines."""
    a = np.column_stack([d1, -d2])
    t, _s = np.linalg.solve(a, p2 - p1)
    result: npt.NDArray[np.float64] = p1 + t * d1
    return result


@dataclass(frozen=True)
class TableCalibration:
    """Distorted-px → table-mm transform (undistort points, then homography)."""

    homography: tuple[tuple[float, float, float], ...]  # 3x3, undistorted px → mm
    length_mm: float
    width_mm: float
    line_residual_px: float  # boundary-line straightness after undistortion
    undistort: UndistortParams

    @property
    def homography_matrix(self) -> npt.NDArray[np.float64]:
        return np.array(self.homography, dtype=np.float64)

    def to_table_mm(self, pts_px: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
        """Map Nx2 distorted pixel coordinates to Nx2 table millimeters."""
        pts = np.asarray(pts_px, dtype=np.float64).reshape(-1, 1, 2)
        k = self.undistort.camera_matrix
        undistorted = cv2.undistortPoints(pts, k, np.array(self.undistort.dist), P=k)
        mm = cv2.perspectiveTransform(undistorted, self.homography_matrix)
        return np.asarray(mm, dtype=np.float64).reshape(-1, 2)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        u = self.undistort
        payload = {
            "homography": [list(row) for row in self.homography],
            "length_mm": self.length_mm,
            "width_mm": self.width_mm,
            "line_residual_px": self.line_residual_px,
            "undistort": {
                "fx": u.fx,
                "fy": u.fy,
                "cx": u.cx,
                "cy": u.cy,
                "dist": list(u.dist),
                "width": u.width,
                "height": u.height,
                "fit_residual_px": u.fit_residual_px,
            },
        }
        path.write_text(json.dumps(payload, indent=2) + "\n")

    @classmethod
    def load(cls, path: Path) -> "TableCalibration":
        payload = json.loads(path.read_text())
        u = payload["undistort"]
        return cls(
            homography=tuple(tuple(row) for row in payload["homography"]),
            length_mm=payload["length_mm"],
            width_mm=payload["width_mm"],
            line_residual_px=payload["line_residual_px"],
            undistort=UndistortParams(
                fx=u["fx"],
                fy=u["fy"],
                cx=u["cx"],
                cy=u["cy"],
                dist=tuple(u["dist"]),
                width=u["width"],
                height=u["height"],
                fit_residual_px=u["fit_residual_px"],
            ),
        )


def table_mm_to_raw_px(
    calibration: TableCalibration, pts_mm: npt.NDArray[np.float64]
) -> npt.NDArray[np.float64]:
    """Inverse of `to_table_mm`: mm → undistorted px (H⁻¹) → raw distorted px.

    Applies the Brown radial terms only (k1/k2/k3); the v1 rail fits leave
    the tangential coefficients at zero.
    """
    h_inv = np.linalg.inv(calibration.homography_matrix)
    pts = np.asarray(pts_mm, dtype=np.float64)
    proj = (h_inv @ np.column_stack([pts, np.ones(len(pts))]).T).T
    und = proj[:, :2] / proj[:, 2:3]
    u = calibration.undistort
    k1, k2, k3 = u.dist[0], u.dist[1], u.dist[4]
    xn = (und[:, 0] - u.cx) / u.fx
    yn = (und[:, 1] - u.cy) / u.fy
    r2 = xn * xn + yn * yn
    s = 1.0 + k1 * r2 + k2 * r2 * r2 + k3 * r2 * r2 * r2
    return np.column_stack([xn * s * u.fx + u.cx, yn * s * u.fy + u.cy])


def boundary_drift(
    reference_gray: npt.NDArray[np.uint8],
    live_gray: npt.NDArray[np.uint8],
    calibration: TableCalibration,
) -> float:
    """Mean |Δgray| sampled across the cloth/rail boundary ring.

    A cheap "has the camera moved or the lighting changed since the
    reference was recorded?" signal: the cloth boundary is the
    highest-contrast position-locked structure in the scene, so a shift of
    even a few pixels sends this soaring, while balls (which live on the
    cloth, away from the sampled ring) barely graze it. Run at startup and
    warn — a stale reference silently kills detection with no other symptom
    (learned 2026-07-29, the TV-install camera shift).
    """
    h, w = reference_gray.shape
    length, width = calibration.length_mm, calibration.width_mm
    offsets = (-9.0, -6.0, -3.0, 0.0, 3.0, 6.0, 9.0)
    stations: list[tuple[float, float]] = []
    for frac in np.linspace(0.05, 0.95, 40):
        for off in offsets:
            stations.append((float(frac) * length, off))  # top edge
            stations.append((float(frac) * length, width - off))  # bottom
            stations.append((off, float(frac) * width))  # left
            stations.append((length - off, float(frac) * width))  # right
    pts = table_mm_to_raw_px(calibration, np.array(stations))
    xs = np.clip(np.round(pts[:, 0]).astype(np.intp), 0, w - 1)
    ys = np.clip(np.round(pts[:, 1]).astype(np.intp), 0, h - 1)
    diff = np.abs(reference_gray[ys, xs].astype(np.int16) - live_gray[ys, xs].astype(np.int16))
    return float(diff.mean())


# Empirical `boundary_drift` levels (measured 2026-07-29 on real clips):
# same mount ≈ 1.9 (with or without balls on the cloth), the 20 mm
# TV-install camera shift ≈ 35. Warn well above noise, far below any move.
BOUNDARY_DRIFT_WARN = 8.0


def table_corners_px(
    boundary: BoundaryPoints, undistort: UndistortParams
) -> tuple[npt.NDArray[np.float64], float]:
    """Cloth corners in undistorted pixel coords (TL, TR, BR, BL) + residual.

    The residual is the RMS perpendicular distance of undistorted boundary
    points to their fitted lines, in pixels — the health metric for the fit.
    """
    k = undistort.camera_matrix
    dist = np.array(undistort.dist)
    lines: dict[str, tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]] = {}
    sq_sum, n_pts = 0.0, 0
    for name, pts in boundary.sides.items():
        und = np.asarray(
            cv2.undistortPoints(pts.reshape(-1, 1, 2), k, dist, P=k), dtype=np.float64
        ).reshape(-1, 2)
        centroid, direction = _tls_line(und)
        lines[name] = (centroid, direction)
        offsets = und - centroid
        perp = offsets - np.outer(offsets @ direction, direction)
        sq_sum += float((perp**2).sum())
        n_pts += len(und)
    corners = np.array(
        [
            _intersect(*lines["top"], *lines["left"]),
            _intersect(*lines["top"], *lines["right"]),
            _intersect(*lines["bottom"], *lines["right"]),
            _intersect(*lines["bottom"], *lines["left"]),
        ]
    )
    return corners, float(np.sqrt(sq_sum / n_pts))


def fit_from_frame(
    bgr: npt.NDArray[np.uint8],
    undistort: UndistortParams,
    length_mm: float = TABLE_LENGTH_MM,
    width_mm: float = TABLE_WIDTH_MM,
) -> TableCalibration:
    """Fit the full px→mm calibration from one empty-table frame."""
    boundary = trim_outliers(sample_boundary(cloth_mask(bgr)))
    corners, residual = table_corners_px(boundary, undistort)
    dst = np.array(
        [[0.0, 0.0], [length_mm, 0.0], [length_mm, width_mm], [0.0, width_mm]],
        dtype=np.float32,
    )
    h = cv2.getPerspectiveTransform(corners.astype(np.float32), dst)
    return TableCalibration(
        homography=tuple((float(r[0]), float(r[1]), float(r[2])) for r in h),
        length_mm=length_mm,
        width_mm=width_mm,
        line_residual_px=residual,
        undistort=undistort,
    )
