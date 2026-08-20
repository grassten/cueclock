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


def test_pipeline_bayer_to_rgba_shape() -> None:
    bayer = np.random.default_rng(3).integers(0, 256, size=(120, 192), dtype=np.uint8)
    out = DisplayPipeline().to_rgba(bayer)
    assert out.shape == (120, 192, 4)
    assert out.dtype == np.uint8
    assert (out[:, :, 3] == 255).all()


def _rectified(out_width: int = 400, margin_mm: float = 90.0) -> DisplayPipeline:
    """A rectified pipeline over a distortion-free 7 ft table.

    Distortion-free so the remap is an exact scale-and-shift: every mark's
    landing pixel is then arithmetic, which is what makes the assertions below
    about geometry rather than about OpenCV's interpolation.
    """
    import cv2

    from cueclock.vision.calibration import TableCalibration

    undistort = UndistortParams(
        fx=1000.0,
        fy=1000.0,
        cx=960.0,
        cy=600.0,
        dist=(0.0, 0.0, 0.0, 0.0, 0.0),
        width=1920,
        height=1200,
        fit_residual_px=0.0,
    )
    src = np.array([[200, 150], [1700, 150], [1700, 1050], [200, 1050]], dtype=np.float32)
    dst = np.array([[0, 0], [1930, 0], [1930, 965], [0, 965]], dtype=np.float32)
    h = cv2.getPerspectiveTransform(src, dst)
    calibration = TableCalibration(
        homography=tuple(tuple(float(v) for v in row) for row in h),
        length_mm=1930.0,
        width_mm=965.0,
        line_residual_px=0.0,
        undistort=undistort,
    )
    return DisplayPipeline.rectified(calibration, out_width=out_width, margin_mm=margin_mm)


def test_only_a_rectified_pipeline_draws_marks() -> None:
    """A plain undistort has no mm→px mapping, so there is nowhere honest to draw."""
    assert DisplayPipeline().draws_marks is False
    assert _rectified().draws_marks is True


def _lit(frame: npt.NDArray[np.uint8], x: int, y: int, slack: int = 2) -> bool:
    """Was anything drawn within `slack` px of (x, y)?

    A slack window rather than an exact pixel: the mapping rounds and the
    strokes are antialiased, and the claim under test is *where a mark lands in
    millimetres*, not which side of a half-pixel OpenCV rounded to.
    """
    return bool(frame[y - slack : y + slack + 1, x - slack : x + slack + 1].any())


def test_marks_land_where_the_millimetres_say() -> None:
    from cueclock.display.marks import Circle, Rect

    pipeline = _rectified(out_width=1920)
    scale, margin = 1920.0 / (1930.0 + 180.0), 90.0

    def px(x_mm: float, y_mm: float) -> tuple[int, int]:
        return round((x_mm + margin) * scale), round((y_mm + margin) * scale)

    frame = np.zeros((pipeline.out_size[1], pipeline.out_size[0], 3), dtype=np.uint8)
    # One diamond cell in the corner: 1930/8 x 965/4.
    pipeline.draw_marks(frame, (Rect(x_mm=0.0, y_mm=0.0, w_mm=241.25, h_mm=241.25),))
    assert _lit(frame, *px(120.0, 120.0))  # the wash tinted the cloth inside
    assert not _lit(frame, *px(1500.0, 600.0))  # a mark is local — nothing else moved

    frame[:] = 0
    pipeline.draw_marks(frame, (Circle(x_mm=965.0, y_mm=482.5),))
    cx, cy = px(965.0, 482.5)
    assert not _lit(frame, cx, cy)  # a ghost is a ring, not a disc
    from cueclock.display.marks import GHOST_RADIUS_MM

    edge = round(GHOST_RADIUS_MM * scale)
    assert _lit(frame, cx + edge, cy)
    brightest = frame[cy - edge - 4 : cy + edge + 5, cx - edge - 4 : cx + edge + 5]
    channels = brightest.reshape(-1, 3).max(axis=0)
    assert channels.min() > 200 and channels.max() - channels.min() < 8  # white, per role


def test_the_grid_spans_the_playing_area_and_nothing_more() -> None:
    """Derived entirely from the calibration — no invented geometry (#46)."""
    from cueclock.display.marks import Grid

    pipeline = _rectified(out_width=1920)
    scale, margin = 1920.0 / (1930.0 + 180.0), 90.0
    frame = np.zeros((pipeline.out_size[1], pipeline.out_size[0], 3), dtype=np.uint8)
    pipeline.draw_marks(frame, (Grid(cols=8, rows=4),))
    # Sampled inside a cell's height, not on the row boundary at y = 482.5 —
    # that one is itself a grid line and would pass whatever the columns did.
    mid_y = round((120.0 + margin) * scale)
    for i in range(9):  # every cell boundary of the 8x4 grid carries a line
        assert _lit(frame, round((1930.0 * i / 8 + margin) * scale), mid_y)
    # Between two boundaries, nothing: the grid is lines, not a wash.
    assert not _lit(frame, round((1930.0 / 16 + margin) * scale), mid_y)
    # And the surround outside the playing area is untouched — the grid IS the table.
    assert not _lit(frame, round(margin * scale / 2), round(margin * scale / 2))


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
    plain = DisplayPipeline().to_rgba(bayer)
    undistorted = DisplayPipeline(params).to_rgba(bayer)
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
