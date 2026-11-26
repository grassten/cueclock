"""Game chapters (#81): the maths on marks and shots, then the archive step
against a fake YouTube."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

from cueclock.youtube import chapters
from cueclock.youtube.api import SignedOutError, YouTubeError
from cueclock.youtube.chapters import Mark, build

A, B = "Sam Okafor", "Jordan Lee"
HOUR = 3600.0


def _g(game: int, s1: int, s2: int) -> str:
    """A game's label as the video shows it: the score is set with an en dash."""
    return f"Game {game} ({s1}\u2013{s2})"


def _m(t: float, s1: int, s2: int, **more: Any) -> Mark:
    return Mark(t, more.pop("p1", A), more.pop("p2", B), s1, s2, **more)


def test_one_chapter_a_game_with_the_score_going_in() -> None:
    marks = [_m(0, 0, 0), _m(600, 1, 0), _m(1200, 1, 1), _m(1800, 2, 1)]
    shots = [95.0, 300.0, 680.0, 900.0, 1290.0, 1900.0]
    assert build(marks, shots, HOUR) == [
        (0.0, _g(1, 0, 0)),  # YouTube's first line is always 0:00
        (670.0, _g(2, 1, 0)),  # 10 s before the game's first shot
        (1280.0, _g(3, 1, 1)),
        (1890.0, _g(4, 2, 1)),
    ]


def test_a_game_with_no_shot_found_starts_at_the_score_press() -> None:
    marks = [_m(0, 0, 0), _m(600, 1, 0), _m(1200, 1, 1), _m(1800, 2, 1)]
    shots = [95.0, 1290.0, 1900.0]  # nothing detected in game 2
    assert build(marks, shots, HOUR)[1] == (600.0, _g(2, 1, 0))


def test_a_shot_just_after_the_press_never_puts_the_chapter_before_it() -> None:
    marks = [_m(0, 0, 0), _m(600, 1, 0), _m(1200, 1, 1), _m(1800, 2, 1)]
    assert build(marks, [604.0, 1300.0, 1900.0], HOUR)[1] == (600.0, _g(2, 1, 0))


def test_fewer_than_three_games_writes_none() -> None:
    assert build([_m(0, 0, 0), _m(600, 1, 0)], [50.0, 700.0], HOUR) == []
    assert build([_m(0, 0, 0)], [50.0], HOUR) == []
    assert build([], [], HOUR) == []


def test_practice_gets_no_chapters_even_with_score_presses() -> None:
    marks = [
        Mark(0, "PLAYER 1", "PLAYER 2", 0, 0),
        Mark(600, "PLAYER 1", "PLAYER 2", 1, 0),
        Mark(1200, "PLAYER 1", "PLAYER 2", 1, 1),
        Mark(1800, "PLAYER 1", "PLAYER 2", 2, 1),
    ]
    assert build(marks, [50.0, 700.0, 1300.0, 1900.0], HOUR) == []


def test_a_point_taken_back_removes_the_game_end_before_it() -> None:
    # 620: the wrong player's key; 625: taken back; 630: the right one.
    marks = [
        _m(0, 0, 0),
        _m(620, 1, 0),
        _m(625, 0, 0),
        _m(630, 0, 1),
        _m(1200, 1, 1),
        _m(1800, 2, 1),
    ]
    shots = [95.0, 700.0, 1290.0, 1900.0]
    assert build(marks, shots, HOUR) == [
        (0.0, _g(1, 0, 0)),
        (690.0, _g(2, 0, 1)),
        (1280.0, _g(3, 1, 1)),
        (1890.0, _g(4, 2, 1)),
    ]


def test_a_point_moved_to_the_other_player_relabels_the_game_in_play() -> None:
    # Seen as one change by the desk's once-a-second look: 1-0 became 0-1.
    marks = [_m(0, 0, 0), _m(620, 1, 0), _m(623, 0, 1), _m(1200, 1, 1), _m(1800, 2, 1)]
    labels = [label for _, label in build(marks, [95.0, 700.0, 1290.0, 1900.0], HOUR)]
    assert labels == [_g(1, 0, 0), _g(2, 0, 1), _g(3, 1, 1), _g(4, 2, 1)]


def test_the_winning_point_opens_no_next_game() -> None:
    marks = [_m(0, 0, 0), _m(600, 1, 0), _m(1200, 2, 0), _m(1800, 3, 0, over=True)]
    shots = [95.0, 700.0, 1290.0, 2000.0]  # 2000: knocking balls about afterwards
    assert [label for _, label in build(marks, shots, HOUR)] == [
        _g(1, 0, 0),
        _g(2, 1, 0),
        _g(3, 2, 0),
    ]


def test_a_last_game_nobody_shot_in_is_dropped() -> None:
    marks = [_m(0, 0, 0), _m(600, 1, 0), _m(1200, 1, 1), _m(1800, 2, 1), _m(2400, 2, 2)]
    shots = [95.0, 700.0, 1290.0, 1900.0]  # the stream ended on the last press
    built = build(marks, shots, 2460.0)
    assert built[-1] == (1890.0, _g(4, 2, 1))


def test_a_stream_begun_mid_match_numbers_its_first_chapter_from_the_score() -> None:
    marks = [_m(0, 3, 2), _m(600, 4, 2), _m(1200, 4, 3)]
    assert build(marks, [50.0, 700.0, 1300.0], HOUR) == [
        (0.0, _g(6, 3, 2)),
        (690.0, _g(7, 4, 2)),
        (1290.0, _g(8, 4, 3)),
    ]


def test_each_match_of_several_names_its_players_once() -> None:
    marks = [
        _m(0, 0, 0),
        _m(600, 1, 0),
        _m(1200, 2, 0, over=True),
        _m(1300, 0, 0, p1="Ana Ruiz", p2="Bo Chen"),  # new players, scores reset
        _m(1900, 0, 1, p1="Ana Ruiz", p2="Bo Chen"),
        _m(2500, 0, 0),  # the first pair again...
    ]
    shots = [95.0, 700.0, 1400.0, 2000.0, 2600.0]
    assert [label for _, label in build(marks, shots, HOUR)] == [
        "Okafor vs Lee · Game 1 (0\u20130)",
        _g(2, 1, 0),
        "Ruiz vs Chen · Game 1 (0\u20130)",
        _g(2, 0, 1),
        "Okafor vs Lee · Game 1 (0\u20130)",
    ]


def test_a_score_reset_with_the_same_players_is_their_next_match() -> None:
    marks = [_m(0, 0, 0), _m(600, 1, 0), _m(1200, 2, 0, over=True), _m(1300, 0, 0), _m(1900, 1, 0)]
    shots = [95.0, 700.0, 1400.0, 2000.0]
    assert [label for _, label in build(marks, shots, HOUR)] == [
        "Okafor vs Lee · Game 1 (0\u20130)",
        _g(2, 1, 0),
        "Okafor vs Lee · Game 1 (0\u20130)",
        _g(2, 1, 0),
    ]


def test_a_bracket_gets_one_chapter_a_match_not_a_game() -> None:
    def match(t: float, number: int, p1: str, p2: str, s1: int = 0, s2: int = 0) -> Mark:
        return Mark(t, p1, p2, s1, s2, tournament="Thanksgiving Open", match=number)

    marks = [
        match(0, 1, A, B),
        match(300, 1, A, B, 1, 0),
        match(600, 1, A, B, 2, 0),
        match(900, 2, "Ana Ruiz", "Bo Chen"),
        match(1500, 2, "Ana Ruiz", "Bo Chen", 0, 1),
        match(2100, 7, A, "Bo Chen"),
        match(2700, 8, "Ana Ruiz", B),  # loaded, never played: the stream ended
    ]
    shots = [60.0, 400.0, 1000.0, 1600.0, 2200.0]
    assert build(marks, shots, 2760.0) == [
        (0.0, "Match 1 · Okafor vs Lee"),
        (990.0, "Match 2 · Ruiz vs Chen"),
        (2190.0, "Match 7 · Okafor vs Chen"),
    ]


def test_a_chapter_shorter_than_ten_seconds_gives_its_time_to_the_next() -> None:
    # Two presses six seconds apart and no shot until the game after them.
    marks = [_m(0, 0, 0), _m(600, 1, 0), _m(606, 1, 1), _m(1200, 2, 1), _m(1800, 2, 2)]
    built = build(marks, [50.0, 1300.0, 1900.0], HOUR)
    assert built[1] == (600, _g(3, 1, 1))
    assert _g(2, 1, 0) not in [label for _, label in built]
    assert all(b[0] - a[0] >= chapters.MIN_LENGTH_S for a, b in pairwise(built))


def test_stamps_read_as_youtube_expects() -> None:
    assert [chapters.stamp(t) for t in (0, 59.9, 754, 3723)] == ["0:00", "0:59", "12:34", "1:02:03"]
    assert chapters.render([(0.0, _g(1, 0, 0)), (670.0, _g(2, 1, 0))]) == (
        "0:00 Game 1 (0\u20130)\n11:10 Game 2 (1\u20130)"
    )


# ---------- the archive pass's step ----------

T0 = datetime(2026, 11, 26, 1, 0, tzinfo=UTC)


def _at(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat(timespec="milliseconds")


def _session(tmp_path: Path, video_id: str | None = "vid1") -> Path:
    """A session directory whose journal holds one four-game stream."""
    session = tmp_path / "20261126-010000"
    session.mkdir(parents=True)
    records: list[dict[str, Any]] = [
        {"wall": _at(-30), "kind": "cmd_new_rack"},
        {"wall": _at(0), "kind": "stream_on_air", "video_id": video_id, "start_wall": _at(-2)},
    ]
    for t, s1, s2 in ((0, 0, 0), (600, 1, 0), (1200, 1, 1), (1800, 2, 1)):
        records.append(
            {"wall": _at(t), "kind": "stream_mark", "video_id": video_id, "p1": A, "p2": B}
            | {"s1": s1, "s2": s2, "over": False, "tournament": None, "match": None}
        )
    for t in (95, 680, 1290, 1900):
        records.append({"wall": _at(t), "kind": "vision_shot_taken"})
    records.append({"wall": _at(2400), "kind": "cmd_match_clear"})
    records.sort(key=lambda r: r["wall"])
    (session / "journal.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    return session


class FakeYouTube:
    def __init__(self, *, start: float | None = 3.0, fail: Exception | None = None) -> None:
        self.start, self.fail = start, fail
        self.described: list[tuple[str, str, str]] = []

    def broadcast(self, video_id: str) -> dict[str, Any] | None:
        if self.fail is not None:
            raise self.fail
        snippet: dict[str, Any] = {"title": "Sam vs Jo", "description": "Sam vs Jo\n8-Ball."}
        if self.start is not None:
            snippet["actualStartTime"] = _at(self.start).replace("+00:00", "Z")
            snippet["actualEndTime"] = _at(2400).replace("+00:00", "Z")
        return {"id": video_id, "snippet": snippet}

    def describe(self, video_id: str, title: str, description: str) -> None:
        self.described.append((video_id, title, description))


def test_the_step_appends_the_chapters_to_the_description_once(tmp_path: Path) -> None:
    session, yt, said = _session(tmp_path), FakeYouTube(), []
    assert chapters.write(session, said.append, youtube=yt)  # type: ignore[arg-type]
    assert yt.described == [
        (
            "vid1",
            "Sam vs Jo",
            # Timed from YouTube's own start (3 s in), not the deck's.
            "Sam vs Jo\n8-Ball.\n\n"
            f"0:00 {_g(1, 0, 0)}\n11:07 {_g(2, 1, 0)}\n21:17 {_g(3, 1, 1)}\n31:27 {_g(4, 2, 1)}",
        )
    ]
    done = json.loads((session / chapters.DONE_FILE).read_text())
    assert done["vid1"]["chapters"] == 4
    assert said == ["20261126-010000: video vid1: 4 chapters written"]
    # A later pass finds it done and asks YouTube nothing.
    assert chapters.write(session, said.append, youtube=FakeYouTube(fail=AssertionError()))  # type: ignore[arg-type]


def test_without_youtubes_start_time_the_decks_is_used(tmp_path: Path) -> None:
    session, yt = _session(tmp_path), FakeYouTube(start=None)
    assert chapters.write(session, lambda _line: None, youtube=yt)  # type: ignore[arg-type]
    assert "\n11:12 Game 2 (1\u20130)\n" in yt.described[0][2]  # from start_wall, 2 s before


def test_an_unreachable_youtube_is_tried_again_next_pass(tmp_path: Path) -> None:
    session, said = _session(tmp_path), []
    yt = FakeYouTube(fail=YouTubeError("Reading the stream: YouTube did not answer"))
    assert not chapters.write(session, said.append, youtube=yt)  # type: ignore[arg-type]
    assert not (session / chapters.DONE_FILE).exists()
    assert "not written yet" in said[0]


def test_a_refusal_or_a_sign_out_is_noted_and_never_holds_the_session(tmp_path: Path) -> None:
    for i, fail in enumerate(
        (YouTubeError("Updating the video: forbidden", status=403), SignedOutError("Not signed in"))
    ):
        session = _session(tmp_path / str(i))
        assert chapters.write(session, lambda _line: None, youtube=FakeYouTube(fail=fail))  # type: ignore[arg-type]
        done = json.loads((session / chapters.DONE_FILE).read_text())
        assert done["vid1"]["chapters"] == 0 and done["vid1"]["why"]


def test_a_session_with_no_stream_or_no_video_asks_youtube_nothing(tmp_path: Path) -> None:
    boom = FakeYouTube(fail=AssertionError("YouTube was asked"))
    unknown = _session(tmp_path / "a", video_id=None)  # started in OBS, not from the deck
    assert chapters.write(unknown, lambda _line: None, youtube=boom)  # type: ignore[arg-type]
    empty = tmp_path / "20261126-020000"
    empty.mkdir()
    assert chapters.write(empty, lambda _line: None, youtube=boom)  # type: ignore[arg-type]


def test_a_dry_run_only_says_what_it_would_do(tmp_path: Path) -> None:
    session, said = _session(tmp_path), []
    boom = FakeYouTube(fail=AssertionError("YouTube was asked"))
    assert chapters.write(session, said.append, youtube=boom, dry_run=True)  # type: ignore[arg-type]
    assert said == ["20261126-010000: would write chapters to video vid1"]
    assert not (session / chapters.DONE_FILE).exists()
