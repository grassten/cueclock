"""Table calibration tests: synthetic frames only, no camera, no clips."""

from pathlib import Path

import numpy as np
import numpy.typing as npt
import pytest

from cueclock.display.pipeline import UndistortParams
from cueclock.vision.calibration import TableCalibration, fit_from_frame


def _identity_undistort(width: int, height: int, fx: float = 1000.0) -> UndistortParams:
    return UndistortParams(
        fx=fx,
        fy=fx,
        cx=width / 2,
        cy=height / 2,
        dist=(0.0, 0.0, 0.0, 0.0, 0.0),
        width=width,
        height=height,
        fit_residual_px=0.0,
    )


def _cloth_frame(
    width: int, height: int, x0: int, y0: int, x1: int, y1: int
) -> npt.NDArray[np.uint8]:
    """Dark frame with a blue-dominant cloth rectangle spanning [x0,x1)x[y0,y1)."""
    img = np.zeros((height, width, 3), dtype=np.uint8)
    img[:, :] = (40, 40, 40)
    img[y0:y1, x0:x1] = (200, 80, 30)
    return img


def test_fit_maps_cloth_rectangle_to_table_mm() -> None:
    img = _cloth_frame(960, 600, x0=100, y0=80, x1=860, y1=520)
    cal = fit_from_frame(img, _identity_undistort(960, 600))

    # cloth corners land on the table rectangle corners
    corners_px = np.array([[100, 80], [859, 80], [859, 519], [100, 519]], dtype=np.float64)
    expected = np.array([[0, 0], [1930, 0], [1930, 965], [0, 965]], dtype=np.float64)
    got = cal.to_table_mm(corners_px)
    assert np.abs(got - expected).max() < 6.0  # mm; one boundary px ≈ 2.5 mm here

    # the cloth center lands at the table center
    center = cal.to_table_mm(np.array([[479.5, 299.5]]))[0]
    assert center == pytest.approx((965.0, 482.5), abs=6.0)

    assert cal.line_residual_px < 1.0


def _distort(
    pts: npt.NDArray[np.float64], fx: float, cx: float, cy: float, k1: float, k2: float
) -> npt.NDArray[np.float64]:
    # Brown forward model, same as test_display's — duplicated because test
    # modules are not importable from each other.
    xn = (pts[:, 0] - cx) / fx
    yn = (pts[:, 1] - cy) / fx
    r2 = xn**2 + yn**2
    factor = 1.0 + k1 * r2 + k2 * r2**2
    return np.column_stack([fx * xn * factor + cx, fx * yn * factor + cy])


def test_to_table_mm_inverts_distortion() -> None:
    """Round trip: table mm → undistorted px → distorted px → to_table_mm."""
    fx, cx, cy = 1014.0, 960.0, 600.0
    k1, k2 = -0.30, 0.09
    undistort = UndistortParams(
        fx=fx,
        fy=fx,
        cx=cx,
        cy=cy,
        dist=(k1, k2, 0.0, 0.0, 0.0),
        width=1920,
        height=1200,
        fit_residual_px=0.0,
    )
    # hand-built homography: cloth spans (200,150)-(1700,1050) in undistorted px
    import cv2

    src = np.array([[200, 150], [1700, 150], [1700, 1050], [200, 1050]], dtype=np.float32)
    dst = np.array([[0, 0], [1930, 0], [1930, 965], [0, 965]], dtype=np.float32)
    h = cv2.getPerspectiveTransform(src, dst)
    cal = TableCalibration(
        homography=tuple(tuple(float(v) for v in row) for row in h),
        length_mm=1930.0,
        width_mm=965.0,
        line_residual_px=0.0,
        undistort=undistort,
    )

    mm_grid = np.array([[0, 0], [965, 482.5], [1930, 965], [400, 700], [1600, 100]], np.float64)
    h_inv = np.linalg.inv(np.array(h, dtype=np.float64))
    ones = np.hstack([mm_grid, np.ones((len(mm_grid), 1))])
    und_px = (ones @ h_inv.T)[:, :2] / (ones @ h_inv.T)[:, 2:]
    distorted_px = _distort(und_px, fx, cx, cy, k1, k2)

    recovered = cal.to_table_mm(distorted_px)
    assert np.abs(recovered - mm_grid).max() < 0.5  # mm


def test_calibration_round_trip(tmp_path: Path) -> None:
    img = _cloth_frame(960, 600, x0=100, y0=80, x1=860, y1=520)
    cal = fit_from_frame(img, _identity_undistort(960, 600))
    cal.save(tmp_path / "cal.json")
    loaded = TableCalibration.load(tmp_path / "cal.json")
    assert loaded == cal
    pts = np.array([[480.0, 300.0], [200.0, 200.0]])
    np.testing.assert_allclose(loaded.to_table_mm(pts), cal.to_table_mm(pts))
