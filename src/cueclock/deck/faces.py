"""Key-face rendering: 72x72 text tiles. Pillow only — no hardware imports."""

from __future__ import annotations

from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

SIZE = 72  # MK.2 key resolution (square)
_MARGIN = 8
_FONT_CANDIDATES = (
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
)

Rgb = tuple[int, int, int]

BG: Rgb = (0, 0, 0)
BG_OK: Rgb = (18, 92, 44)
BG_ERR: Rgb = (140, 30, 30)
FG: Rgb = (235, 235, 235)
FG_DIM: Rgb = (90, 90, 90)

_Font = ImageFont.FreeTypeFont | ImageFont.ImageFont


@lru_cache(maxsize=8)
def _font(px: int) -> _Font:
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, px)
        except OSError:
            continue
    return ImageFont.load_default()  # CI fallback; production Macs have Helvetica


def _fit(draw: ImageDraw.ImageDraw, lines: list[str]) -> tuple[_Font, list[float], int]:
    """Largest size where the widest line and the stack both fit (may clip at 10 px)."""
    font, widths, line_h = _font(10), [0.0], 14
    for px in (22, 18, 15, 12, 10):
        font = _font(px)
        widths = [draw.textlength(line, font=font) for line in lines]
        line_h = px + 4
        if max(widths) <= SIZE - _MARGIN and line_h * len(lines) <= SIZE - _MARGIN:
            break
    return font, widths, line_h


def render(text: str, *, fg: Rgb = FG, bg: Rgb = BG) -> Image.Image:
    """Centered multi-line text tile ('\\n' splits lines). Pure: text in, image out."""
    img = Image.new("RGB", (SIZE, SIZE), bg)
    if not text:
        return img
    draw = ImageDraw.Draw(img)
    lines = text.split("\n")
    font, widths, line_h = _fit(draw, lines)
    y = (SIZE - line_h * len(lines)) / 2
    for line, width in zip(lines, widths, strict=True):
        draw.text(((SIZE - width) / 2, y), line, font=font, fill=fg)
        y += line_h
    return img


def dimmed(text: str) -> Image.Image:
    """Disabled look — server down or key inactive."""
    return render(text, fg=FG_DIM)


def flash_ok(text: str) -> Image.Image:
    return render(text, bg=BG_OK)


def flash_err(text: str) -> Image.Image:
    return render(text, bg=BG_ERR)
