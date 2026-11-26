"""What a stream is called (#81): the title table from the issue, line by line."""

from __future__ import annotations

from datetime import date

import pytest

from cueclock.clock import Settings
from cueclock.server.score import MatchScore
from cueclock.youtube import title
from cueclock.youtube.title import naming

DAY = date(2026, 11, 25)
EIGHT = Settings().as_dict()


def _score(
    p1: str = "Sam Okafor",
    p2: str = "Jordan Lee",
    *,
    fargo: tuple[int | None, int | None] = (612, 587),
    race_to: int | None = 7,
    targets: tuple[int | None, int | None] = (None, None),
) -> dict[str, object]:
    score = MatchScore(
        p1_name=p1,
        p2_name=p2,
        p1_fargo=fargo[0],
        p2_fargo=fargo[1],
        p1_target=targets[0],
        p2_target=targets[1],
        race_to=race_to,
    )
    return score.as_dict()


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (_score(), "Sam Okafor (612) vs Jordan Lee (587) | 8-Ball Race to 7"),
        (
            _score(fargo=(612, 487), race_to=None, targets=(7, 4)),
            "Sam Okafor (612) vs Jordan Lee (487) | 8-Ball Race 7/4",
        ),
        (_score(race_to=None), "Sam Okafor (612) vs Jordan Lee (587) | 8-Ball"),
        (_score(fargo=(612, None)), "Sam Okafor (612) vs Jordan Lee | 8-Ball Race to 7"),
        # Equal per-player targets are one race, as on the wall.
        (
            _score(race_to=None, targets=(5, 5)),
            "Sam Okafor (612) vs Jordan Lee (587) | 8-Ball Race to 5",
        ),
    ],
)
def test_a_match_is_titled_players_first(score: dict[str, object], expected: str) -> None:
    assert naming(score, EIGHT, None, DAY).title == expected


def test_the_game_word_follows_the_setting_and_other_leaves_it_out() -> None:
    nine = Settings(game="9ball").as_dict()
    other = Settings(game="other").as_dict()
    assert naming(_score(), nine, None, DAY).title.endswith("| 9-Ball Race to 7")
    assert naming(_score(), other, None, DAY).title.endswith("Jordan Lee (587) | Race to 7")
    # Nothing after the names means no divider either.
    assert naming(_score(race_to=None), other, None, DAY).title == (
        "Sam Okafor (612) vs Jordan Lee (587)"
    )


def test_no_players_set_is_practice() -> None:
    words = naming(MatchScore().as_dict(), EIGHT, None, DAY)
    assert (words.kind, words.title) == ("practice", "Practice | Nov 25, 2026")
    assert words.description == "Streamed Nov 25, 2026."  # the date line alone
    assert (words.thumb_top, words.thumb_big) == (None, "Practice")
    # One name is not a match.
    assert naming(_score(p2="PLAYER 2"), EIGHT, None, DAY).kind == "practice"
    assert naming(_score(p1="  "), EIGHT, None, DAY).kind == "practice"


def test_the_unset_names_are_the_scores_own_defaults() -> None:
    fresh = MatchScore()
    assert not title.player_set(fresh.p1_name) and not title.player_set(fresh.p2_name)
    assert not title.player_set("player 1") and not title.player_set("—")
    assert title.player_set("Sam")


def test_a_tournament_is_titled_with_the_event_not_the_match_on_the_table() -> None:
    words = naming(_score(), EIGHT, "Thanksgiving Open", DAY)
    assert (words.kind, words.title) == ("tournament", "Thanksgiving Open | 8-Ball Tournament")
    assert (words.thumb_top, words.thumb_big) == ("8-Ball Tournament", "Thanksgiving Open")
    assert words.description == (
        "Thanksgiving Open\n8-Ball tournament. 30-second shot clock.\nStreamed Nov 25, 2026."
    )
    other = naming(_score(), Settings(game="other").as_dict(), "Thanksgiving Open", DAY)
    assert other.title == "Thanksgiving Open | Tournament"


def test_the_description_is_written_from_the_match() -> None:
    assert naming(_score(), EIGHT, None, DAY).description == (
        "Sam Okafor (612) vs Jordan Lee (587)\n"
        "8-Ball, race to 7. 30-second shot clock.\n"
        "Streamed Nov 25, 2026."
    )
    quick = Settings(shot="15", clock="off").as_dict()
    handicapped = naming(_score(race_to=None, targets=(7, 4)), quick, None, DAY)
    assert handicapped.description == (
        "Sam Okafor (612) vs Jordan Lee (587)\n8-Ball, race 7/4.\nStreamed Nov 25, 2026."
    )
    bare = naming(_score(race_to=None), Settings(game="other", clock="off").as_dict(), None, DAY)
    assert bare.description == "Sam Okafor (612) vs Jordan Lee (587)\nStreamed Nov 25, 2026."


def test_the_thumbnail_carries_the_surnames_and_the_game_and_race() -> None:
    words = naming(_score(p1="Mary Jo van der Berg"), EIGHT, None, DAY)
    assert words.thumb_big == ("Berg", "Lee")  # the last word of the name as typed
    assert words.thumb_top == "8-Ball · Race to 7"
    assert words.date == "Nov 25, 2026"


def test_a_typed_title_replaces_the_title_and_the_thumbnails_text_only() -> None:
    words = naming(_score(), EIGHT, None, DAY, "Friday night money game, race to 9")
    assert (words.kind, words.title) == ("typed", "Friday night money game, race to 9")
    assert (words.thumb_top, words.thumb_big) == (None, "Friday night money game, race to 9")
    assert words.description.startswith("Sam Okafor (612) vs Jordan Lee (587)\n")
    # It replaces a practice title the same way.
    assert naming(MatchScore().as_dict(), EIGHT, None, DAY, "Drills").title == "Drills"
    # Nothing typed, or only spaces, is no typed title.
    assert naming(_score(), EIGHT, None, DAY, "   ").kind == "match"


def test_a_title_is_cut_to_what_youtube_takes() -> None:
    assert title.clean_title("  a <b>  c\nd  ") == "a b c d"
    assert len(title.clean_title("x" * 150)) == title.TITLE_MAX
    long = naming(_score(p1="A" * 60, p2="B" * 60), EIGHT, None, DAY)
    assert len(long.title) == 100


def test_the_date_reads_like_the_mockups() -> None:
    assert title.date_text(date(2026, 1, 5)) == "Jan 5, 2026"
