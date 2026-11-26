"""The stream's thumbnail: 1280x720, drawn at go-live (#81). Pillow only.

One layout for every stream. Left, on the overlay's ink: a small top line, the
large text, and the date. Right: one fixed picture of the table
(`config/stream_still.jpg`, chosen once), behind a rule in the accent colour.

==========  ====================  ==============================
stream      small top line        large text
==========  ====================  ==============================
match       game and race         the two surnames, `vs` between
tournament  `8-Ball Tournament`   the event's name
practice    none                  `PRACTICE`
typed       none                  the typed title
==========  ====================  ==============================

The large text is the only part whose length is not known, so it wraps and
shrinks to fit: up to four lines, balanced so no line is left with one short
word, at the largest listed size that fits. Past the smallest size it is cut
with an ellipsis. Two surnames at 124 px are the largest case.
"""

from __future__ import annotations

import io
from functools import lru_cache
from itertools import combinations, pairwise

from PIL import Image, ImageDraw, ImageFont, ImageOps

from cueclock.youtube.title import Naming

SIZE = (1280, 720)
TEXT_W = 700  # the left panel; the picture has the rest
PAD_X, PAD_Y = 72, 64
INK = (11, 14, 19)  # the overlay's palette (`control.html`)
PANEL = (26, 31, 41)  # where the picture goes, when there is none
ACCENT = (46, 134, 200)
SLATE = (159, 179, 196)
TEXT = (237, 241, 245)
RULE_W = 4

TOP_PX, DATE_PX, VS_PX = 34, 30, 40
CAP = 0.72  # a capital's height over the font size, for San Francisco and its stand-ins
VS_GAP = 24  # between a surname and `vs`, capital edge to capital edge
# Largest first. 124 is the mockup's two surnames; 48 is still a headline at
# the size a phone shows a thumbnail (about a quarter of this).
BIG_SIZES = (124, 108, 96, 84, 72, 64, 56, 48)
MAX_LINES = 4
# One more line is taken only when it buys at least this much size: a title
# on three lines at 72 px reads better than the same title on four at 84.
LINE_GAIN = 1.2
JPEG_MAX = 2_000_000  # YouTube's limit for a thumbnail

_Font = ImageFont.FreeTypeFont | ImageFont.ImageFont
# San Francisco is what the control page and the mockups are set in. It is a
# variable font, so the weight is asked for; the others are fixed bold faces.
_FONTS: tuple[tuple[str, int], ...] = (
    ("/System/Library/Fonts/SFNS.ttf", 0),
    ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 0),
    ("/System/Library/Fonts/Helvetica.ttc", 1),
)


@lru_cache(maxsize=32)
def _font(px: int, weight: int = 800) -> _Font:
    for path, index in _FONTS:
        try:
            font = ImageFont.truetype(path, px, index=index)
        except OSError:
            continue
        try:
            axes = font.get_variation_axes()
        except OSError:
            return font  # a fixed face
        # Each axis at its default, except the weight asked for and the
        # optical size, which follows the text: the display cut for a headline.
        values = []
        for axis in axes:
            name = axis.get("name")
            low, high = float(axis["minimum"] or 0), float(axis["maximum"] or 0)
            if name in (b"Weight", "Weight"):
                values.append(float(weight))
            elif name in (b"Optical Size", "Optical Size"):
                values.append(min(high, max(low, float(px))))
            else:
                values.append(float(axis["default"] or 0))
        font.set_variation_by_axes(values)
        return font
    return ImageFont.load_default(px)  # CI; a Mac has all three


def _width(text: str, font: _Font, tracking: float = 0.0) -> float:
    return float(font.getlength(text)) + tracking * max(0, len(text) - 1)


def _draw(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    font: _Font,
    fill: tuple[int, int, int],
    tracking: float = 0.0,
    anchor: str = "la",
) -> None:
    """Text from its top-left (or, with ``anchor="ls"``, from its baseline);
    letter-spaced one glyph at a time when asked."""
    x, y = xy
    if not tracking:
        draw.text((x, y), text, font=font, fill=fill, anchor=anchor)
        return
    for char in text:
        draw.text((x, y), char, font=font, fill=fill, anchor=anchor)
        x += float(font.getlength(char)) + tracking


def _cut(text: str, font: _Font, max_w: float) -> str:
    """``text`` shortened to fit, ending in an ellipsis if anything was lost."""
    if _width(text, font) <= max_w:
        return text
    while text and _width(text + "…", font) > max_w:
        text = text[:-1]
    return text.rstrip() + "…"


def _balanced(words: list[str], lines: int, font: _Font) -> list[str]:
    """``words`` on exactly ``lines`` lines, the widest line as narrow as it can be."""
    best: tuple[float, list[str]] | None = None
    for cuts in combinations(range(1, len(words)), lines - 1):
        edges = (0, *cuts, len(words))
        split = [" ".join(words[a:b]) for a, b in pairwise(edges)]
        widest = max(_width(line, font) for line in split)
        if best is None or widest < best[0]:
            best = (widest, split)
    return best[1] if best else [" ".join(words)]


def fit(text: str, max_w: float, max_h: float) -> tuple[int, list[str]]:
    """The large text's size and lines: wrapped, shrunk, and cut as a last resort."""
    words = text.upper().split()[:24]  # a title is 100 characters; this bounds the search
    if not words:
        return BIG_SIZES[-1], []
    best: tuple[int, list[str]] | None = None
    for count in range(1, min(MAX_LINES, len(words)) + 1):
        for px in BIG_SIZES:
            font = _font(px)
            split = _balanced(words, count, font)
            fits = max(_width(line, font) for line in split) <= max_w
            if fits and count * _line_h(px) <= max_h:
                if best is None or px >= best[0] * LINE_GAIN:
                    best = (px, split)
                break
    if best is not None:
        return best
    # Nothing fits even at the smallest size: fill four lines and cut the rest.
    px = BIG_SIZES[-1]
    font = _font(px)
    lines: list[str] = []
    rest = words
    while rest and len(lines) < MAX_LINES:
        take = 1
        while take < len(rest) and _width(" ".join(rest[: take + 1]), font) <= max_w:
            take += 1
        lines.append(" ".join(rest[:take]))
        rest = rest[take:]
    if rest:
        lines[-1] += " " + " ".join(rest)
    return px, [_cut(line, font, max_w) for line in lines]


def _line_h(px: int) -> int:
    return px + 4


def _surnames(a: str, b: str, max_w: float) -> tuple[int, str, str]:
    """Two surnames at one size, each on its own line."""
    a, b = a.upper(), b.upper()
    for px in BIG_SIZES:
        font = _font(px)
        if max(_width(a, font), _width(b, font)) <= max_w:
            return px, a, b
    font = _font(BIG_SIZES[-1])
    return BIG_SIZES[-1], _cut(a, font, max_w), _cut(b, font, max_w)


def render(naming: Naming, still: Image.Image | None = None) -> Image.Image:
    """Draw the thumbnail. Pure: words and an optional picture in, image out."""
    img = Image.new("RGB", SIZE, INK)
    draw = ImageDraw.Draw(img)
    picture_w = SIZE[0] - TEXT_W
    if still is not None:
        img.paste(ImageOps.fit(still.convert("RGB"), (picture_w, SIZE[1])), (TEXT_W, 0))
    else:
        draw.rectangle((TEXT_W, 0, SIZE[0], SIZE[1]), fill=PANEL)
    draw.rectangle((TEXT_W, 0, TEXT_W + RULE_W - 1, SIZE[1]), fill=ACCENT)

    max_w = TEXT_W - 2 * PAD_X
    if naming.thumb_top:
        top_font = _font(TOP_PX)
        top = _cut(naming.thumb_top.upper(), top_font, max_w - 5 * len(naming.thumb_top))
        _draw(draw, (PAD_X, PAD_Y), top, top_font, ACCENT, tracking=5)
    date_y = SIZE[1] - PAD_Y - DATE_PX - 4
    _draw(draw, (PAD_X, date_y), naming.date.upper(), _font(DATE_PX, 700), SLATE, tracking=4)

    # The large text is centred in what the two small lines leave it.
    area_top = PAD_Y + TOP_PX + 28
    area_h = date_y - 28 - area_top
    if isinstance(naming.thumb_big, tuple):
        # Set from the baselines, so the gap above `vs` equals the gap below
        # it whatever size the names came out at.
        px, first, second = _surnames(*naming.thumb_big, max_w)
        cap, vs_cap = round(px * CAP), round(VS_PX * CAP)
        y = area_top + (area_h - (2 * cap + vs_cap + 2 * VS_GAP)) / 2 + cap
        _draw(draw, (PAD_X, y), first, _font(px), TEXT, anchor="ls")
        y += VS_GAP + vs_cap
        _draw(draw, (PAD_X, y), "VS", _font(VS_PX, 700), SLATE, tracking=4, anchor="ls")
        y += VS_GAP + cap
        _draw(draw, (PAD_X, y), second, _font(px), TEXT, anchor="ls")
    else:
        px, lines = fit(naming.thumb_big, max_w, area_h)
        y = area_top + (area_h - len(lines) * _line_h(px)) / 2
        for line in lines:
            _draw(draw, (PAD_X, y), line, _font(px), TEXT)
            y += _line_h(px)
    return img


def jpeg(image: Image.Image) -> bytes:
    """The image as a JPEG under YouTube's 2 MB (it is about 150 kB)."""
    for quality in (90, 80, 70, 55):
        out = io.BytesIO()
        image.save(out, "JPEG", quality=quality, optimize=True)
        if out.tell() <= JPEG_MAX:
            break
    return out.getvalue()
