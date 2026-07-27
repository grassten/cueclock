"""Display-branch frame processing: debayer -> undistort -> BGRA.

Deliberately mechanical: color grading lives in OBS (Color Correction /
Apply LUT filters) where it is adjustable on sliders at the actual TV —
see docs/video-output-pipeline.md §4.
"""

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import cast

import cv2
import numpy as np
import numpy.typing as npt

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


class DisplayPipeline:
    """(1200, 1920) BayerRG8 in -> (1200, 1920, 4) BGRA out."""

    def __init__(self, undistort: UndistortParams | None = None) -> None:
        self._maps = build_undistort_maps(undistort) if undistort else None

    def to_bgra(self, bayer: npt.NDArray[np.uint8]) -> npt.NDArray[np.uint8]:
        # GenICam "BayerRG8" needs OpenCV's *BG* constant (opposite-corner
        # naming conventions — docs/basler-camera-setup.md §5). _EA = edge-aware:
        # visibly cleaner color edges than bilinear, ~0.22 ms, fine at display rate.
        bgr = cv2.cvtColor(bayer, cv2.COLOR_BayerBG2BGR_EA)
        if self._maps is not None:
            bgr = cv2.remap(bgr, self._maps[0], self._maps[1], cv2.INTER_LINEAR)
        return cast(npt.NDArray[np.uint8], cv2.cvtColor(bgr, cv2.COLOR_BGR2BGRA))
