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

from cueclock.display.marks import (
    GRID_ALPHA,
    GRID_STROKE_MM,
    ROLE_COLORS,
    STROKE_MM,
    ZONE_FILL_ALPHA,
    Bracket,
    Circle,
    Grid,
    Line,
    Mark,
    Rect,
)

if TYPE_CHECKING:
    from cueclock.vision.calibration import TableCalibration

DEFAULT_UNDISTORT_PATH = Path("config/display_undistort.json")

# Dash geometry for every dashed mark, in table millimetres so a dash looks the
# same whatever the rectified output width is. Half a ball per dash reads as a
# dashed ring at the wall's distance without closing up into a solid one.
DASH_MM = 26.0
GAP_MM = 18.0


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
        # The mm→display-px mapping, present only on a rectified pipeline —
        # (output px per table mm, the surround in mm). Every on-cloth mark
        # goes through it, which is the whole reason the games layer draws
        # here rather than in a browser source over the Syphon feed (#46).
        self._mm: tuple[float, float] | None = None
        # What the games layer wants on the cloth right now. Rebound from the
        # server's asyncio loop and read on the display thread — a plain
        # attribute holding an immutable tuple, the same contract `Hub.note_frame`
        # uses in the other direction. Empty in every other mode, which is why
        # a match night pays nothing for this.
        self.marks: tuple[Mark, ...] = ()
        self._grid_cache: (
            tuple[
                tuple[Grid, tuple[int, int]],
                tuple[npt.NDArray[np.intp], npt.NDArray[np.intp]],
            ]
            | None
        ) = None

    @classmethod
    def rectified(
        cls, calibration: "TableCalibration", out_width: int = 1920, margin_mm: float = 90.0
    ) -> "DisplayPipeline":
        pipeline = cls(None)
        map1, map2, width, height = build_rectified_maps(calibration, out_width, margin_mm)
        pipeline._maps = (map1, map2)
        pipeline.out_size = (width, height)
        span_x = calibration.length_mm + 2 * margin_mm
        pipeline._mm = (out_width / span_x, margin_mm)
        return pipeline

    @property
    def draws_marks(self) -> bool:
        """Can this pipeline put marks on the cloth? (Only a rectified one.)

        A plain undistort has no mm→px mapping — the table sits wherever the
        mount put it in the frame — so there is nowhere honest to draw a zone.
        `cueclock run --rectify` is the production launch (README), and the
        games layer says so rather than drawing something approximately right.
        """
        return self._mm is not None

    def to_rgba(self, bayer: npt.NDArray[np.uint8]) -> npt.NDArray[np.uint8]:
        # GenICam "BayerRG8" needs OpenCV's *BG* constant (opposite-corner
        # naming conventions — docs/hardware.md §A3). _EA = edge-aware:
        # visibly cleaner color edges than bilinear, ~0.22 ms, fine at display rate.
        bgr = cv2.cvtColor(bayer, cv2.COLOR_BayerBG2BGR_EA)
        if self._maps is not None:
            bgr = cv2.remap(bgr, self._maps[0], self._maps[1], cv2.INTER_LINEAR)
        if self.marks:
            self.draw_marks(cast(npt.NDArray[np.uint8], bgr), self.marks)
        return cast(npt.NDArray[np.uint8], cv2.cvtColor(bgr, cv2.COLOR_BGR2RGBA))

    # ---------- on-cloth marks (#46) ----------

    def draw_marks(self, bgr: npt.NDArray[np.uint8], marks: tuple[Mark, ...]) -> None:
        """Rasterize `marks` into a rectified BGR frame, in place.

        No-op without the mapping — see :attr:`draws_marks`. Public so a
        screenshot test can render a known set of marks over a blank frame and
        assert where they landed, which is the only way this gets checked
        without the camera.
        """
        if self._mm is None:
            return
        scale, margin = self._mm
        stroke = max(1, round(STROKE_MM * scale))
        for mark in marks:
            color = _bgr(mark.role if not isinstance(mark, Grid) else "grid")
            if isinstance(mark, Grid):
                self._draw_grid(bgr, mark, scale, margin)
            elif isinstance(mark, Rect):
                self._draw_rect(bgr, mark, color, scale, margin, stroke)
            elif isinstance(mark, Circle):
                self._draw_circle(bgr, mark, color, scale, margin, stroke)
            elif isinstance(mark, Line):
                self._segment(
                    bgr,
                    mark.x1_mm,
                    mark.y1_mm,
                    mark.x2_mm,
                    mark.y2_mm,
                    color,
                    scale,
                    margin,
                    stroke,
                    mark.dashed,
                )
            elif isinstance(mark, Bracket):
                self._draw_bracket(bgr, mark, color, scale, margin, stroke)

    def _px(self, x_mm: float, y_mm: float, scale: float, margin: float) -> tuple[int, int]:
        """Table mm → rectified output pixel — the inverse of `build_rectified_maps`."""
        return round((x_mm + margin) * scale - 0.5), round((y_mm + margin) * scale - 0.5)

    def _segment(
        self,
        bgr: npt.NDArray[np.uint8],
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        color: tuple[int, int, int],
        scale: float,
        margin: float,
        stroke: int,
        dashed: bool,
    ) -> None:
        """One straight run in table mm, solid or dashed."""
        if not dashed:
            cv2.line(
                bgr,
                self._px(x1, y1, scale, margin),
                self._px(x2, y2, scale, margin),
                color,
                stroke,
                cv2.LINE_AA,
            )
            return
        length = float(np.hypot(x2 - x1, y2 - y1))
        if length <= 0:
            return
        step = DASH_MM + GAP_MM
        pos = 0.0
        while pos < length:
            end = min(pos + DASH_MM, length)
            f0, f1 = pos / length, end / length
            cv2.line(
                bgr,
                self._px(x1 + (x2 - x1) * f0, y1 + (y2 - y1) * f0, scale, margin),
                self._px(x1 + (x2 - x1) * f1, y1 + (y2 - y1) * f1, scale, margin),
                color,
                stroke,
                cv2.LINE_AA,
            )
            pos += step

    def _draw_rect(
        self,
        bgr: npt.NDArray[np.uint8],
        rect: Rect,
        color: tuple[int, int, int],
        scale: float,
        margin: float,
        stroke: int,
    ) -> None:
        x0, y0 = rect.x_mm, rect.y_mm
        x1, y1 = rect.x_mm + rect.w_mm, rect.y_mm + rect.h_mm
        if rect.fill:
            # A wash rather than a solid fill: the zone is aimed at through the
            # whole shot, so it has to read as a region while leaving the cloth
            # (and any ball standing on it) readable underneath.
            p0, p1 = self._px(x0, y0, scale, margin), self._px(x1, y1, scale, margin)
            patch = bgr[max(0, p0[1]) : max(0, p1[1]), max(0, p0[0]) : max(0, p1[0])]
            if patch.size:
                wash = np.full(patch.shape, color, dtype=np.uint8)
                cv2.addWeighted(wash, ZONE_FILL_ALPHA, patch, 1.0 - ZONE_FILL_ALPHA, 0.0, patch)
        for a, b in (
            ((x0, y0), (x1, y0)),
            ((x1, y0), (x1, y1)),
            ((x1, y1), (x0, y1)),
            ((x0, y1), (x0, y0)),
        ):
            self._segment(bgr, a[0], a[1], b[0], b[1], color, scale, margin, stroke, rect.dashed)

    def _draw_circle(
        self,
        bgr: npt.NDArray[np.uint8],
        circle: Circle,
        color: tuple[int, int, int],
        scale: float,
        margin: float,
        stroke: int,
    ) -> None:
        center = self._px(circle.x_mm, circle.y_mm, scale, margin)
        radius = max(1, round(circle.r_mm * scale))
        if not circle.dashed:
            cv2.circle(bgr, center, radius, color, stroke, cv2.LINE_AA)
            return
        # Dashes measured along the circumference, so a ghost ring and a miss
        # line wear the same dash however big the ring is.
        span = 360.0 * DASH_MM / (2.0 * np.pi * circle.r_mm)
        gap = 360.0 * GAP_MM / (2.0 * np.pi * circle.r_mm)
        angle = 0.0
        while angle < 360.0:
            cv2.ellipse(
                bgr,
                center,
                (radius, radius),
                0.0,
                angle,
                min(angle + span, 360.0),
                color,
                stroke,
                cv2.LINE_AA,
            )
            angle += span + gap

    def _draw_bracket(
        self,
        bgr: npt.NDArray[np.uint8],
        bracket: Bracket,
        color: tuple[int, int, int],
        scale: float,
        margin: float,
        stroke: int,
    ) -> None:
        top = bracket.y_mm - bracket.half_height_mm
        bottom = bracket.y_mm + bracket.half_height_mm
        for a, b in (
            ((bracket.x0_mm, top), (bracket.x1_mm, top)),
            ((bracket.x0_mm, bottom), (bracket.x1_mm, bottom)),
            ((bracket.x1_mm, top), (bracket.x1_mm, bottom)),
        ):
            self._segment(bgr, a[0], a[1], b[0], b[1], color, scale, margin, stroke, False)

    def _draw_grid(
        self, bgr: npt.NDArray[np.uint8], grid: Grid, scale: float, margin: float
    ) -> None:
        """The diamond grid across the rectangle the mark carries.

        Thinner and far fainter than everything else on purpose: it is the
        vocabulary a zone is quoted in, not a thing being aimed at, and at full
        stroke it would compete with the zone it exists to locate.

        Blended through a cached pixel index rather than an `addWeighted` over
        the whole frame: the grid never moves, so its pixels are found once and
        every later frame only touches the ~1% of the image the lines cover.
        This runs on the display thread at 60 fps beside a ~10 ms publish, and
        a full-frame blend per grid would be a third of that budget for
        thirteen lines.
        """
        index = self._grid_index(grid, scale, margin)
        color = np.array(_bgr("grid"), dtype=np.float32)
        patch = bgr[index].astype(np.float32)
        bgr[index] = (patch * (1.0 - GRID_ALPHA) + color * GRID_ALPHA).astype(np.uint8)

    def _grid_index(
        self, grid: Grid, scale: float, margin: float
    ) -> tuple[npt.NDArray[np.intp], npt.NDArray[np.intp]]:
        key = (grid, self.out_size)
        cached = self._grid_cache
        if cached is not None and cached[0] == key:
            return cached[1]
        out_w, out_h = self.out_size
        canvas = np.zeros((out_h, out_w), dtype=np.uint8)
        stroke = max(1, round(GRID_STROKE_MM * scale))
        for i in range(grid.cols + 1):
            x = grid.x0_mm + (grid.x1_mm - grid.x0_mm) * i / grid.cols
            cv2.line(
                canvas,
                self._px(x, grid.y0_mm, scale, margin),
                self._px(x, grid.y1_mm, scale, margin),
                255,
                stroke,
            )
        for j in range(grid.rows + 1):
            y = grid.y0_mm + (grid.y1_mm - grid.y0_mm) * j / grid.rows
            cv2.line(
                canvas,
                self._px(grid.x0_mm, y, scale, margin),
                self._px(grid.x1_mm, y, scale, margin),
                255,
                stroke,
            )
        index = cast("tuple[npt.NDArray[np.intp], npt.NDArray[np.intp]]", np.nonzero(canvas))
        self._grid_cache = (key, index)
        return index


def _bgr(role: str) -> tuple[int, int, int]:
    """A mark role's colour, in the BGR order the frame is in at draw time."""
    r, g, b = ROLE_COLORS.get(role, ROLE_COLORS["cue"])
    return (b, g, r)
