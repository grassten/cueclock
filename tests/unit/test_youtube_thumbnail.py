"""The stream thumbnail (#81): its size, and how the large text is made to fit.

Nothing here compares pixels, and nothing depends on which font the machine
has: CI draws with Pillow's built-in face, a Mac with San Francisco, and the
rules hold for both. The one test that pins the mockup's exact result runs
only where San Francisco is.
"""

from __future__ import annotations

import io
from datetime import date
from pathlib import Path

import pytest
from PIL import Image

from cueclock.clock import Settings
from cueclock.server.score import MatchScore
from cueclock.youtube import thumbnail
from cueclock.youtube.title import Naming, naming

MAX_W = thumbnail.TEXT_W - 2 * thumbnail.PAD_X
MAX_H = 400.0
SAN_FRANCISCO = Path(thumbnail._FONTS[0][0]).exists()


def _words(big: str | tuple[str, str], top: str | None = None) -> Naming:
    return Naming("typed", "t", "d", top, big, "Nov 25, 2026")


def _widest(px: int, lines: list[str]) -> float:
    return max(thumbnail._width(line, thumbnail._font(px)) for line in lines)


@pytest.mark.parametrize(
    "text",
    ["Practice", "Thanksgiving Open", "Black Friday Tournament 8 Ball 2026", "A", "Sam vs Jo"],
)
def test_the_large_text_fits_whole_on_up_to_four_lines(text: str) -> None:
    px, lines = thumbnail.fit(text, MAX_W, MAX_H)
    assert 1 <= len(lines) <= thumbnail.MAX_LINES
    assert " ".join(lines) == text.upper()  # nothing lost, nothing reordered
    assert _widest(px, lines) <= MAX_W
    assert len(lines) * thumbnail._line_h(px) <= MAX_H
    assert px in thumbnail.BIG_SIZES


def test_a_longer_title_never_gets_larger_text() -> None:
    sizes = [
        thumbnail.fit(text, MAX_W, MAX_H)[0]
        for text in ("Practice", "Practice night", "Practice night at the pool room in November")
    ]
    assert sizes == sorted(sizes, reverse=True)


def test_text_that_cannot_fit_is_cut_with_an_ellipsis() -> None:
    text = " ".join(["supercalifragilisticexpialidocious"] * 8)
    px, lines = thumbnail.fit(text, MAX_W, MAX_H)
    assert px == thumbnail.BIG_SIZES[-1]
    assert len(lines) == thumbnail.MAX_LINES
    assert all(line.endswith("…") for line in lines)
    assert _widest(px, lines) <= MAX_W


@pytest.mark.skipif(not SAN_FRANCISCO, reason="pins the mockup, which is set in San Francisco")
def test_the_mockups_own_cases_come_out_as_drawn() -> None:
    # Board 10: three lines at 72 px, "a little over half" of two surnames' 124.
    assert thumbnail.fit("Black Friday Tournament 8 Ball 2026", MAX_W, MAX_H) == (
        72,
        ["BLACK FRIDAY", "TOURNAMENT", "8 BALL 2026"],
    )
    assert thumbnail.fit("Practice", MAX_W, MAX_H) == (108, ["PRACTICE"])  # board 9
    assert thumbnail._surnames("Okafor", "Lee", MAX_W) == (124, "OKAFOR", "LEE")  # board 8


def test_two_surnames_share_one_size_and_a_long_one_shrinks_both() -> None:
    px, first, second = thumbnail._surnames("Montgomery", "Lee", MAX_W)
    assert (first, second) == ("MONTGOMERY", "LEE")
    assert thumbnail._width(first, thumbnail._font(px)) <= MAX_W
    assert px <= thumbnail._surnames("Okafor", "Lee", MAX_W)[0]
    px, first, _ = thumbnail._surnames("W" * 40, "Lee", MAX_W)
    assert px == thumbnail.BIG_SIZES[-1] and first.endswith("…")


def test_every_kind_of_stream_renders_at_youtubes_size() -> None:
    day = date(2026, 11, 25)
    eight = Settings().as_dict()
    match = MatchScore(p1_name="Sam Okafor", p2_name="Jordan Lee", race_to=7).as_dict()
    for words in (
        naming(match, eight, None, day),
        naming(match, eight, "Thanksgiving Open", day),
        naming(MatchScore().as_dict(), eight, None, day),
        naming(match, eight, None, day, "x" * 100),
    ):
        image = thumbnail.render(words)
        assert image.size == (1280, 720)
        data = thumbnail.jpeg(image)
        assert len(data) <= thumbnail.JPEG_MAX
        assert Image.open(io.BytesIO(data)).format == "JPEG"


def test_the_picture_fills_the_right_of_the_rule_and_nothing_left_of_it() -> None:
    red = Image.new("RGB", (400, 300), (200, 0, 0))
    image = thumbnail.render(_words("Practice"), red)
    assert image.getpixel((thumbnail.TEXT_W + thumbnail.RULE_W + 5, 360)) == (200, 0, 0)
    assert image.getpixel((1275, 5)) == (200, 0, 0)
    assert image.getpixel((thumbnail.TEXT_W + 1, 360)) == thumbnail.ACCENT  # the rule
    assert image.getpixel((5, 5)) == thumbnail.INK
    # With no picture chosen the panel is drawn plain.
    assert thumbnail.render(_words("Practice")).getpixel((1000, 360)) == thumbnail.PANEL
