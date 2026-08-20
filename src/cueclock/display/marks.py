"""On-cloth markings, in table millimetres — the games layer's drawing language.

Everything a minigame puts **on the cloth** (the diamond grid, ghost rings,
target zones, the miss line, the lag bracket) is drawn server-side into the
display frame rather than by a browser source aligned on top of the Syphon
feed (#46). The display pipeline already owns both the table image and the
mm→display-px mapping, so orientation and scale are correct for free and OBS
stays dumb; a browser overlay would need an exported mapping that has to
survive every recalibration — a standing alignment liability.

This module is the *language*, deliberately separate from the drawing: it is
pure geometry with no OpenCV import, so the games layer (`server/games.py`)
can compose marks, and the unit tests can assert them, without either one
dragging in cv2. `display/pipeline.py` is the only thing that rasterizes them.

**The palette is a role code, not decoration** (mockups v1 §7): white is the
cue ball's markings, slate dashed is an object ball's, and the one alarm red
is the miss line and nothing else. Accent blue never touches the cloth —
on blue cloth it is invisible — so accent and alarm live in the HUD bar
instead. One visual per role on every surface: TV, capture, phone.
"""

from __future__ import annotations

from dataclasses import dataclass

Rgb = tuple[int, int, int]

# The three on-cloth roles. Named here so the HUD page, the phone editor and
# the rasterizer all quote the same three constants.
CUE_WHITE: Rgb = (255, 255, 255)
OBJECT_SLATE: Rgb = (159, 179, 196)
MISS_ALARM: Rgb = (255, 77, 77)
GRID_WHITE: Rgb = (255, 255, 255)

ROLE_COLORS: dict[str, Rgb] = {
    "cue": CUE_WHITE,
    "object": OBJECT_SLATE,
    "miss": MISS_ALARM,
    "grid": GRID_WHITE,
}

# Stroke weights in millimetres rather than pixels, because the rectified
# display frame's px/mm depends on `--rectify`'s output width and the TV
# upscales it again. A ball is ~50 mm across, so 5 mm of line is a tenth of a
# ball — the mockup's 4 px on a 1920-wide authored frame, in units that keep
# meaning it after any resize.
STROKE_MM = 5.0
GRID_STROKE_MM = 2.5
GHOST_RADIUS_MM = 30.0  # a hair outside the ball, so the ball sits *in* the ring
# The alpha of a zone's interior wash. The zone is a target being aimed at
# through the whole shot, so it has to read as a region rather than an
# outline — but it is painted over live cloth the player is also reading.
ZONE_FILL_ALPHA = 0.10
# The grid is the vocabulary a zone is quoted in, not a thing being aimed at,
# so it sits at the edge of visible: enough to sight a cell boundary against,
# never enough to compete with the zone it locates or with the balls.
GRID_ALPHA = 0.20


@dataclass(frozen=True)
class Rect:
    """An axis-aligned block of cloth — a target zone."""

    x_mm: float
    y_mm: float
    w_mm: float
    h_mm: float
    role: str = "cue"
    dashed: bool = False
    fill: bool = True


@dataclass(frozen=True)
class Circle:
    """A ring around a spot — a ghost (where a ball starts) or a snapshot ring."""

    x_mm: float
    y_mm: float
    r_mm: float = GHOST_RADIUS_MM
    role: str = "cue"
    dashed: bool = True


@dataclass(frozen=True)
class Line:
    """A segment — the miss line from a resting ball to the zone it missed."""

    x1_mm: float
    y1_mm: float
    x2_mm: float
    y2_mm: float
    role: str = "miss"
    dashed: bool = True


@dataclass(frozen=True)
class Bracket:
    """The lag measurement bracket: a ⊐ off one rail, spanning `y` at `x`.

    Its own shape rather than three `Line`s because it is one measurement and
    reads as one thing — and lag has no grid and no zones, so this is the only
    thing on the cloth in that game.
    """

    x0_mm: float
    x1_mm: float
    y_mm: float
    half_height_mm: float = 80.0
    role: str = "cue"


@dataclass(frozen=True)
class Grid:
    """The diamond grid over the playing surface — `cols` x `rows` cells.

    Carries no coordinates: it always spans the whole calibrated playing area,
    which is the only thing that makes it the *diamond* grid rather than an
    invented one (#46 — derived entirely from the homography, no new geometry
    constants).
    """

    cols: int
    rows: int


Mark = Rect | Circle | Line | Bracket | Grid
