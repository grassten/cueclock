"""What a stream is called: its title, description and thumbnail text (#81).

Pure. Everything is derived from what the control page already holds: the
score object every broadcast carries (`MatchScore.as_dict`), the clock's
settings, and the loaded bracket's name.

A stream is one of three things, and they differ only in these words:

==========  ==========================================================
match       both players named: `Sam Okafor (612) vs Jordan Lee (587) | 8-Ball Race to 7`
tournament  a bracket is loaded: `Thanksgiving Open | 8-Ball Tournament`
practice    anything else: `Practice | Nov 25, 2026`
==========  ==========================================================

Players come first, each with their Fargo rating when one is set, then one
divider: a phone list cuts a title at about 60 characters, and the names are
what a viewer looks for. A typed title replaces any of the three, on the
video and on the thumbnail.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

TITLE_MAX = 100  # YouTube's limit, and it refuses `<` and `>`
# The names a score starts on (`MatchScore`'s defaults; a test holds the two
# together) and the bracket's empty slot: none of them is a player.
_UNSET = frozenset({"", "PLAYER 1", "PLAYER 2", "—"})
_GAMES = {"8ball": "8-Ball", "9ball": "9-Ball", "10ball": "10-Ball"}  # `other` has no word


@dataclass(frozen=True)
class Naming:
    """One stream's words, as they stand right now."""

    kind: str  # "match" | "tournament" | "practice" | "typed"
    title: str
    description: str
    # The thumbnail (`thumbnail.py`): a small line over the large text. A
    # match's large text is its two surnames; anything else is one string.
    thumb_top: str | None
    thumb_big: str | tuple[str, str]
    date: str


def clean_title(text: str) -> str:
    """A title YouTube will take: one line, no angle brackets, 100 characters."""
    flat = " ".join(text.replace("<", "").replace(">", "").split())
    return flat[:TITLE_MAX].rstrip()


def date_text(day: date) -> str:
    """`Nov 25, 2026`."""
    return f"{day.strftime('%b')} {day.day}, {day.year}"


def player_set(name: object) -> bool:
    """Has this slot been given a player? `PLAYER 1` and `PLAYER 2` have not."""
    return isinstance(name, str) and name.strip().upper() not in _UNSET


def players(score: Mapping[str, Any]) -> tuple[str, str] | None:
    """Both names when both are set: a match. Otherwise None: practice."""
    p1, p2 = score.get("p1", {}).get("name"), score.get("p2", {}).get("name")
    if player_set(p1) and player_set(p2):
        return str(p1).strip(), str(p2).strip()
    return None


def surname(name: str) -> str:
    """The last word of the name as typed."""
    words = name.split()
    return words[-1] if words else name


def game_word(settings: Mapping[str, str]) -> str | None:
    return _GAMES.get(settings.get("game", ""))


def race(score: Mapping[str, Any]) -> tuple[int, int] | None:
    """What each player races to, or None when no race is set.

    Per-player targets win over the single race, as they do on the wall: a
    handicapped match has no single race to state.
    """
    t1, t2 = score.get("p1", {}).get("target"), score.get("p2", {}).get("target")
    if isinstance(t1, int) and isinstance(t2, int):
        return t1, t2
    race_to = score.get("race_to")
    return (race_to, race_to) if isinstance(race_to, int) else None


def _race_words(score: Mapping[str, Any]) -> str | None:
    """`Race to 7`, or `Race 7/4`: a dash after two names reads as a final score."""
    targets = race(score)
    if targets is None:
        return None
    return (
        f"Race to {targets[0]}" if targets[0] == targets[1] else f"Race {targets[0]}/{targets[1]}"
    )


def _rated(name: str, fargo: object) -> str:
    return f"{name} ({fargo})" if isinstance(fargo, int) else name


def _clock_sentence(settings: Mapping[str, str]) -> str:
    if settings.get("clock", "on") != "on":
        return ""
    return f" {settings.get('shot', '30')}-second shot clock."


def naming(
    score: Mapping[str, Any],
    settings: Mapping[str, str],
    tournament: str | None,
    today: date,
    typed: str | None = None,
) -> Naming:
    """The stream's words. ``typed`` is the title typed on the phone, if any.

    A typed title changes the title and the thumbnail's large text and nothing
    else: the description still says who is playing.
    """
    day = date_text(today)
    game = game_word(settings)
    named = players(score)
    if tournament:
        kind = "tournament"
        what = f"{game} Tournament" if game else "Tournament"
        title = f"{tournament} | {what}"
        event = f"{game} tournament." if game else "Tournament."
        description = f"{tournament}\n{event}{_clock_sentence(settings)}\nStreamed {day}."
        top: str | None = what
        big: str | tuple[str, str] = tournament
    elif named is not None:
        kind = "match"
        racing = _race_words(score)
        versus = (
            f"{_rated(named[0], score['p1'].get('fargo'))} vs "
            f"{_rated(named[1], score['p2'].get('fargo'))}"
        )
        tail = " ".join(part for part in (game, racing) if part)
        title = f"{versus} | {tail}" if tail else versus
        # `8-Ball, race to 7.` / `8-Ball.` / `Race to 7.`
        facts = ", ".join(part for part in (game, racing.lower() if racing else None) if part)
        second = f"{facts[0].upper() + facts[1:]}." if facts else ""
        description = "\n".join(
            line
            for line in (versus, (second + _clock_sentence(settings)).strip(), f"Streamed {day}.")
            if line
        )
        top = " · ".join(part for part in (game, racing) if part) or None
        big = (surname(named[0]), surname(named[1]))
    else:
        kind = "practice"
        title = f"Practice | {day}"
        description = f"Streamed {day}."
        top, big = None, "Practice"
    if typed is not None and clean_title(typed):
        kind, title = "typed", typed
        top, big = None, clean_title(typed)
    return Naming(kind, clean_title(title), description, top, big, day)
