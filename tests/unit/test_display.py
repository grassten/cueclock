"""Display pipeline + rail-fit tests (no camera, no Syphon)."""

from pathlib import Path

import numpy as np
import numpy.typing as npt
import pytest

from cueclock.display.pipeline import DisplayPipeline, UndistortParams, build_undistort_maps
from cueclock.display.undistort_fit import (
    BoundaryPoints,
    cloth_mask,
    fit_distortion,
    sample_boundary,
)


def test_pipeline_bayer_to_bgra_shape() -> None:
    bayer = np.random.default_rng(3).integers(0, 256, size=(120, 192), dtype=np.uint8)
    out = DisplayPipeline().to_bgra(bayer)
    assert out.shape == (120, 192, 4)
    assert out.dtype == np.uint8
    assert (out[:, :, 3] == 255).all()


def test_undistort_params_round_trip(tmp_path: Path) -> None:
    params = UndistortParams(
        fx=1014.0,
        fy=1014.0,
        cx=960.0,
        cy=600.0,
        dist=(-0.3, 0.09, 0.0, 0.0, 0.0),
        width=1920,
        height=1200,
        fit_residual_px=0.86,
    )
    params.save(tmp_path / "u.json")
    loaded = UndistortParams.load(tmp_path / "u.json")
    assert loaded == params


def test_zero_distortion_is_identity() -> None:
    params = UndistortParams(
        fx=200.0,
        fy=200.0,
        cx=96.0,
        cy=60.0,
        dist=(0.0, 0.0, 0.0, 0.0, 0.0),
        width=192,
        height=120,
        fit_residual_px=0.0,
    )
    bayer = np.random.default_rng(5).integers(0, 256, size=(120, 192), dtype=np.uint8)
    plain = DisplayPipeline().to_bgra(bayer)
    undistorted = DisplayPipeline(params).to_bgra(bayer)
    np.testing.assert_array_equal(plain, undistorted)


def test_square_pixels_in_maps() -> None:
    params = UndistortParams(
        fx=1014.0,
        fy=1014.0,
        cx=960.0,
        cy=600.0,
        dist=(-0.3, 0.09, 0.0, 0.0, 0.0),
        width=1920,
        height=1200,
        fit_residual_px=0.86,
    )
    map1, _ = build_undistort_maps(params)
    # equal output spans at the image center must cover (near-)equal source
    # distances horizontally and vertically: square pixels, round balls.
    # (span of 100 px swamps the CV_16SC2 integer quantization of map1)
    cy, cx = 600, 960
    dx = np.linalg.norm(map1[cy, cx + 100].astype(float) - map1[cy, cx - 100].astype(float))
    dy = np.linalg.norm(map1[cy + 100, cx].astype(float) - map1[cy - 100, cx].astype(float))
    assert dx == pytest.approx(dy, rel=0.02)


def _distort(
    pts: npt.NDArray[np.float64], fx: float, cx: float, cy: float, k1: float, k2: float
) -> npt.NDArray[np.float64]:
    xn = (pts[:, 0] - cx) / fx
    yn = (pts[:, 1] - cy) / fx
    r2 = xn**2 + yn**2
    factor = 1.0 + k1 * r2 + k2 * r2**2
    return np.column_stack([fx * xn * factor + cx, fx * yn * factor + cy])


def test_fit_recovers_known_distortion() -> None:
    fx, cx, cy = 1014.0, 960.0, 600.0
    k1_true, k2_true = -0.30, 0.09
    sides = {}
    for name, (x_range, y_range) in {
        "top": (np.arange(240.0, 1680.0, 18.0), 160.0),
        "bottom": (np.arange(240.0, 1680.0, 18.0), 1040.0),
        "left": (300.0, np.arange(260.0, 940.0, 9.0)),
        "right": (1620.0, np.arange(260.0, 940.0, 9.0)),
    }.items():
        if name in ("top", "bottom"):
            straight = np.column_stack([x_range, np.full_like(x_range, y_range)])
        else:
            straight = np.column_stack([np.full_like(y_range, x_range), y_range])
        sides[name] = _distort(straight, fx, cx, cy, k1_true, k2_true)

    params = fit_distortion(BoundaryPoints(sides), width=1920, height=1200, fx=fx)
    assert params.dist[0] == pytest.approx(k1_true, abs=0.03)
    assert params.dist[1] == pytest.approx(k2_true, abs=0.06)
    assert params.fit_residual_px < 0.5


def test_cloth_mask_and_boundary() -> None:
    img = np.zeros((300, 480, 3), dtype=np.uint8)
    img[:, :] = (40, 40, 40)
    img[40:260, 60:420] = (200, 80, 30)  # blue-dominant rectangle
    mask = cloth_mask(img)
    assert mask.sum() == 220 * 360
    boundary = sample_boundary(mask)
    assert set(boundary.sides) == {"top", "bottom", "left", "right"}
    assert (boundary.sides["top"][:, 1] == 40).all()
    assert (boundary.sides["bottom"][:, 1] == 259).all()
    assert (boundary.sides["left"][:, 0] == 60).all()
    assert (boundary.sides["right"][:, 0] == 419).all()


def test_cloth_mask_rejects_frame_without_cloth() -> None:
    grey = np.full((100, 100, 3), 90, dtype=np.uint8)
    with pytest.raises(ValueError, match="cloth"):
        cloth_mask(grey)
