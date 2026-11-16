"""Display-branch frame processing: debayer -> undistort -> BGRA.

Deliberately mechanical: color grading lives in OBS (Color Correction /
Apply LUT filters) where it is adjustable on sliders at the actual TV —
see docs/hardware.md §B3.
"""

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

import cv2
import numpy as np
import numpy.typing as npt

if TYPE_CHECKING:
    from cueclock.vision.calibration import TableCalibration

DEFAULT_UNDISTORT_PATH = Path("config/display_undistort.json")


@dataclass(frozen=True)
class UndistortParams:
    """Pinhole intrinsics + Brown radial distortion, fitted or calibrated.

    v1 source is the rail-line fit (`cueclock fit-undistort`): display-grade,
    good enough for straight rails on the TV. Detection-grade ChArUco
    calibration replaces this after the final camera install.
    """

    fx: float
    fy: float
    cx: float
    cy: float
    dist: tuple[float, float, float, float, float]
    width: int
    height: int
    fit_residual_px: float

    @classmethod
    def load(cls, path: Path) -> "UndistortParams":
        raw = json.loads(path.read_text())
        raw["dist"] = tuple(raw["dist"])
        return cls(**raw)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2) + "\n")

    @property
    def camera_matrix(self) -> npt.NDArray[np.float64]:
        return np.array(
            [[self.fx, 0.0, self.cx], [0.0, self.fy, self.cy], [0.0, 0.0, 1.0]],
            dtype=np.float64,
        )


def build_undistort_maps(
    params: UndistortParams,
) -> tuple[npt.NDArray[np.int16], npt.NDArray[np.uint16]]:
    """Precompute cv2.remap maps for the display undistort.

    alpha=0 finds the zoom that fills the frame with valid pixels, but its
    fx/fy differ (anisotropic) which would render balls elliptical on the TV.
    Force square pixels with the smaller focal — the table stays fully
    visible and the leftover invalid corners render black in the carpet
    region, which OBS crops away anyway.
    """
    size = (params.width, params.height)
    dist = np.array(params.dist, dtype=np.float64)
    new_k, _roi = cv2.getOptimalNewCameraMatrix(params.camera_matrix, dist, size, alpha=0)
    f_square = min(float(new_k[0, 0]), float(new_k[1, 1]))
    new_k[0, 0] = new_k[1, 1] = f_square
    map1, map2 = cv2.initUndistortRectifyMap(
        params.camera_matrix, dist, None, new_k, size, cv2.CV_16SC2
    )
    return cast(npt.NDArray[np.int16], map1), cast(npt.NDArray[np.uint16], map2)


def build_rectified_maps(
    calibration: "TableCalibration",
    out_width: int = 1920,
    margin_mm: float = 90.0,
) -> tuple[npt.NDArray[np.int16], npt.NDArray[np.uint16], int, int]:
    """Display maps that render the table axis-aligned and rectangular.

    Composes the perspective homography (px→mm, from `cueclock calibrate`)
    with the lens undistortion into one remap: every output pixel is a table
    coordinate, so mount roll/tilt/off-center vanish from the TV picture
    entirely. `margin_mm` of surround keeps the pocket openings and some rail
    in frame. Output height follows from the table's mm aspect ratio.
    Runtime cost is identical to the plain undistort (one cv2.remap).
    """
    u = calibration.undistort
    span_x = calibration.length_mm + 2 * margin_mm
    span_y = calibration.width_mm + 2 * margin_mm
    out_height = round(out_width * span_y / span_x)
    scale = out_width / span_x  # output px per table mm

    us, vs = np.meshgrid(np.arange(out_width), np.arange(out_height))
    mm_x = (us + 0.5) / scale - margin_mm
    mm_y = (vs + 0.5) / scale - margin_mm
    h_inv = np.linalg.inv(calibration.homography_matrix)
    denom = h_inv[2, 0] * mm_x + h_inv[2, 1] * mm_y + h_inv[2, 2]
    und_x = (h_inv[0, 0] * mm_x + h_inv[0, 1] * mm_y + h_inv[0, 2]) / denom
    und_y = (h_inv[1, 0] * mm_x + h_inv[1, 1] * mm_y + h_inv[1, 2]) / denom

    # forward Brown model: undistorted pixel -> distorted source pixel
    k1, k2, p1, p2, k3 = u.dist
    xn = (und_x - u.cx) / u.fx
    yn = (und_y - u.cy) / u.fy
    r2 = xn * xn + yn * yn
    radial = 1.0 + k1 * r2 + k2 * r2 * r2 + k3 * r2 * r2 * r2
    xd = xn * radial + 2.0 * p1 * xn * yn + p2 * (r2 + 2.0 * xn * xn)
    yd = yn * radial + p1 * (r2 + 2.0 * yn * yn) + 2.0 * p2 * xn * yn
    map_x = (u.fx * xd + u.cx).astype(np.float32)
    map_y = (u.fy * yd + u.cy).astype(np.float32)
    map1, map2 = cv2.convertMaps(map_x, map_y, cv2.CV_16SC2)
    return (
        cast(npt.NDArray[np.int16], map1),
        cast(npt.NDArray[np.uint16], map2),
        out_width,
        out_height,
    )


def remap_coordinates(
    map1: npt.NDArray[np.int16], map2: npt.NDArray[np.uint16]
) -> npt.NDArray[np.float32]:
    """The exact source coordinates a CV_16SC2 remap samples, as float32 (h, w, 2).

    `cv2.remap` with INTER_LINEAR on fixed-point maps samples at the integer
    part in `map1` plus a 1/32-pixel fraction packed into `map2` (x in the low
    5 bits, y in the next 5). The GPU rectify (syphon_out.py) samples these
    same coordinates, so its picture matches the CPU path to within rounding.
    """
    frac_x = (map2 & 31).astype(np.float32) / 32.0
    frac_y = (map2 >> 5).astype(np.float32) / 32.0
    coords = np.empty((*map2.shape, 2), dtype=np.float32)
    coords[..., 0] = map1[..., 0].astype(np.float32) + frac_x
    coords[..., 1] = map1[..., 1].astype(np.float32) + frac_y
    return coords


class DisplayPipeline:
    """BayerRG8 in -> RGBA out for Syphon.

    RGBA, not BGRA: the bytes handed to syphon-python land in OBS in RGBA
    order — feeding BGRA rendered the blue cloth tan (confirmed eyes-on in
    OBS 2026-07-27).

    Plain construction undistorts in place (1920x1200 in and out); the
    `rectified` constructor instead composes undistortion with the table
    homography so the output frame IS the table, straight and centered.
    """

    def __init__(self, undistort: UndistortParams | None = None) -> None:
        self._maps = build_undistort_maps(undistort) if undistort else None
        self.out_size: tuple[int, int] = (1920, 1200)  # (width, height)

    @classmethod
    def rectified(
        cls, calibration: "TableCalibration", out_width: int = 1920, margin_mm: float = 90.0
    ) -> "DisplayPipeline":
        pipeline = cls(None)
        map1, map2, width, height = build_rectified_maps(calibration, out_width, margin_mm)
        pipeline._maps = (map1, map2)
        pipeline.out_size = (width, height)
        return pipeline

    @property
    def maps(self) -> tuple[npt.NDArray[np.int16], npt.NDArray[np.uint16]] | None:
        """The remap maps, or None when the feed is not undistorted at all."""
        return self._maps

    def debayer_rgba(self, bayer: npt.NDArray[np.uint8], out: npt.NDArray[np.uint8]) -> None:
        """Debayer into `out` (h, w, 4) without the remap; the GPU path warps after."""
        bgr = cv2.cvtColor(bayer, cv2.COLOR_BayerBG2BGR_EA)
        cv2.cvtColor(bgr, cv2.COLOR_BGR2RGBA, dst=out)

    def to_rgba(self, bayer: npt.NDArray[np.uint8]) -> npt.NDArray[np.uint8]:
        # GenICam "BayerRG8" needs OpenCV's *BG* constant (opposite-corner
        # naming conventions — docs/hardware.md §A3). _EA = edge-aware:
        # visibly cleaner color edges than bilinear, ~0.22 ms, fine at display rate.
        bgr = cv2.cvtColor(bayer, cv2.COLOR_BayerBG2BGR_EA)
        if self._maps is not None:
            bgr = cv2.remap(bgr, self._maps[0], self._maps[1], cv2.INTER_LINEAR)
        return cast(npt.NDArray[np.uint8], cv2.cvtColor(bgr, cv2.COLOR_BGR2RGBA))
