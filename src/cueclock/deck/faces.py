"""Key-face rendering: 72x72 tiles. Pillow only — no hardware imports.

A face is data (`Face`) and `render` turns it into pixels. Beyond the text it
carries four devices, each of which exists because it says something a word
can't say as fast:

- **stripe** — a colour bar along the bottom naming the key's group. It is a
  bar rather than a tile colour because the *tile* is the press-outcome
  language (green ok / red error, `flash_ok`/`flash_err`): colour the whole
  tile and the flash stops reading.
- **sub** — a smaller, dimmed second line. On a cycle key it is the value one
  tap away, so state and action are both on the face and neither can be
  mistaken for the other. This is the only surviving use of dimming; since
  2026-08-02 unavailable keys go dark instead, so dim is typography, not state.
- **big** — an oversized numeral under the text, for `START 30`, whose whole
  point is that the key states the countdown it is about to hand you.
- **pips** — one dot per extension left, so the pool is readable before the
  press instead of after a red `NO EXT`.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

SIZE = 72  # MK.2 key resolution (square)
_MARGIN = 8
_STRIPE_H = 7
_PIP_R = 3
_PIP_GAP = 4
_DOT_R = 4
_FONT_CANDIDATES = (
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
)

Rgb = tuple[int, int, int]

BG: Rgb = (0, 0, 0)
BG_OK: Rgb = (18, 92, 44)
BG_ERR: Rgb = (140, 30, 30)
# The one standing exception to "the tile colour is the press-outcome language"
# (issue #43): an armed bracket result is a state that has to be findable from
# across the room, on a key whose whole job is that it is *waiting* for a press.
# Blue is not in the flash vocabulary, so it can't be read as an outcome — and
# the face still says the verb ("PRESS TO CONFIRM"), so rule 3 holds.
BG_ARMED: Rgb = (24, 74, 118)
FG: Rgb = (235, 235, 235)
FG_DIM: Rgb = (90, 90, 90)

# Group stripes. Chosen away from BG_OK/BG_ERR so a stripe can never be read as
# an outcome flash, and far enough apart to tell at a glance on a lit tile.
STRIPE_CLOCK: Rgb = (224, 160, 32)  # amber — seconds matter
STRIPE_MATCH: Rgb = (61, 134, 216)  # blue — between racks, no pressure
STRIPE_SYSTEM: Rgb = (102, 114, 126)  # grey — session, mode, scene, recal

DOT_OK: Rgb = (63, 191, 106)
DOT_WARN: Rgb = (224, 160, 32)

_Font = ImageFont.FreeTypeFont | ImageFont.ImageFont


@dataclass(frozen=True)
class Face:
    """Everything drawn on one key. Pure data — `render` does the pixels."""

    text: str = ""  # main lines; '\n' splits
    sub: str | None = None  # dimmed second-tier line (cycle keys: the next value)
    big: str | None = None  # oversized numeral under the text (START 30)
    stripe: Rgb | None = None  # group bar along the bottom
    pips: tuple[int, int] | None = None  # (lit, total) extension dots
    dot: Rgb | None = None  # small status dot, top-right (session health)
    fg: Rgb = FG
    bg: Rgb = BG


@lru_cache(maxsize=16)
def _font(px: int) -> _Font:
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, px)
        except OSError:
            continue
    return ImageFont.load_default(px)  # CI fallback; production Macs have Helvetica


def _fit(
    draw: ImageDraw.ImageDraw,
    text: str,
    sizes: tuple[int, ...],
    max_w: int,
    max_h: int,
) -> tuple[_Font, list[str], list[float], int]:
    """Largest listed size where the widest line and the stack both fit.

    Splits on '\\n' here rather than at the call sites: Pillow's `textlength`
    raises on a string containing a newline, and *every* face field can carry
    one — `FINAL\\n10` once reached a cycle key's `sub` as the next ruleset. Doing it
    once is what keeps that from being a per-field trap.

    Falls through to the smallest size rather than failing: a clipped face is
    still a face, and a key that renders nothing is worse than a tight one.
    """
    lines = text.split("\n")
    font, widths, line_h = _font(sizes[-1]), [0.0], sizes[-1] + 4
    for px in sizes:
        font = _font(px)
        widths = [draw.textlength(line, font=font) for line in lines]
        line_h = px + 4
        if max(widths) <= max_w and line_h * len(lines) <= max_h:
            break
    return font, lines, widths, line_h


_MAIN_SIZES = (22, 18, 15, 12, 10)
_BIG_SIZES = (30, 26, 22, 18, 14)
_SUB_SIZES = (12, 11, 10, 9)


def render(face: Face) -> Image.Image:
    """Draw a face. Pure: data in, image out."""
    img = Image.new("RGB", (SIZE, SIZE), face.bg)
    draw = ImageDraw.Draw(img)
    max_w = SIZE - _MARGIN

    # Reserve from the bottom up, so the text block centres in what's left.
    reserved_bottom = _STRIPE_H if face.stripe else 0
    if face.pips:
        reserved_bottom += 2 * _PIP_R + 6

    blocks: list[tuple[_Font, list[str], list[float], int, Rgb]] = []
    if face.text:
        font, lines, widths, line_h = _fit(draw, face.text, _MAIN_SIZES, max_w, SIZE - _MARGIN)
        blocks.append((font, lines, widths, line_h, face.fg))
    if face.big is not None:
        font, lines, widths, line_h = _fit(draw, face.big, _BIG_SIZES, max_w, SIZE)
        blocks.append((font, lines, widths, line_h, face.fg))
    if face.sub is not None:
        font, lines, widths, line_h = _fit(draw, face.sub, _SUB_SIZES, max_w, SIZE)
        blocks.append((font, lines, widths, line_h, FG_DIM))

    total_h = sum(line_h * len(lines) for _, lines, _, line_h, _ in blocks)
    y = (SIZE - reserved_bottom - total_h) / 2
    for font, lines, widths, line_h, colour in blocks:
        for line, width in zip(lines, widths, strict=True):
            draw.text(((SIZE - width) / 2, y), line, font=font, fill=colour)
            y += line_h

    if face.pips:
        lit, total = face.pips
        span = total * (2 * _PIP_R) + max(0, total - 1) * _PIP_GAP
        x = (SIZE - span) / 2
        cy = SIZE - reserved_bottom + _PIP_R + 1
        for i in range(total):
            colour = face.stripe or face.fg if i < lit else FG_DIM
            draw.ellipse((x, cy - _PIP_R, x + 2 * _PIP_R, cy + _PIP_R), fill=colour)
            x += 2 * _PIP_R + _PIP_GAP

    if face.dot is not None:
        right, top = SIZE - _MARGIN, _MARGIN - 2
        draw.ellipse((right - 2 * _DOT_R, top, right, top + 2 * _DOT_R), fill=face.dot)

    if face.stripe is not None:
        draw.rectangle((0, SIZE - _STRIPE_H, SIZE, SIZE), fill=face.stripe)

    return img


def blank() -> Image.Image:
    """A dark key — what an unused cell, or one this mode can't use, looks like."""
    return render(Face())


def flash_ok(text: str) -> Image.Image:
    """Press succeeded. Whole tile, no stripe — the flash owns the tile colour."""
    return render(Face(text, bg=BG_OK))


def flash_err(text: str) -> Image.Image:
    return render(Face(text, bg=BG_ERR))
