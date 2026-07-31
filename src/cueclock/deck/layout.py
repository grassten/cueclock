"""Declarative key layout — the single source of truth for what is on the deck.

The button map in docs/hardware.md Part C mirrors this table; change both in the
same commit.
"""

from __future__ import annotations

from dataclasses import dataclass

ROWS = 3
COLS = 5  # MK.2: 15 keys, row-major from the top-left


@dataclass(frozen=True)
class ApiPost:
    """POST a server path on press; the face flashes the outcome."""

    path: str
    ok_face: str
    err_face: str
    timeout: float = 3.0  # raise for endpoints that work before replying (dump!)
    pending_face: str | None = None  # shown while a slow call is in flight


@dataclass(frozen=True)
class RulesetCycle:
    """Advance to the next ruleset; the face tracks the active one."""


Action = ApiPost | RulesetCycle


@dataclass(frozen=True)
class Key:
    row: int  # 1-based from the top
    col: int  # 1-based from the left
    name: str
    face: str  # static face text; RulesetCycle keys render the active ruleset instead
    action: Action

    @property
    def index(self) -> int:
        """Hardware key number: 0-based, row-major from the top-left."""
        return (self.row - 1) * COLS + (self.col - 1)


LAYOUT: tuple[Key, ...] = (
    Key(1, 3, "ruleset", "RULESET", RulesetCycle()),
    Key(2, 1, "reset", "RESET", ApiPost("/api/reset", "RESET", "ERROR")),
    Key(2, 2, "extension", "EXT\n+15", ApiPost("/api/extension", "+15 s", "NO EXT")),
    # Dump writes ~2.2 GB to the T7 before responding — measured well over the
    # 3 s default (2026-07-31: three "timed out" flashes for dumps that all
    # succeeded). Generous timeout + a pending face instead.
    Key(
        2,
        5,
        "flag",
        "FLAG",
        ApiPost("/api/dump?label=flagged", "SAVED", "NO T7", timeout=60.0, pending_face="SAVING…"),
    ),
)

RULESET_FACES: dict[str, str] = {
    "ultimate_pool_8ball": "8-BALL",
    "ultimate_pool_8ball_final10": "FINAL\n10",
    "wnt_9ball": "9-BALL",
}


def ruleset_face(name: str) -> str:
    """Face text for a ruleset; unknown names show raw (truncated) rather than hide."""
    return RULESET_FACES.get(name, name[:12].upper())


def next_ruleset(active: str, available: list[str]) -> str:
    """The ruleset after `active` in server order, wrapping; unknown active -> first."""
    if not available:
        return active
    try:
        i = available.index(active)
    except ValueError:
        return available[0]
    return available[(i + 1) % len(available)]
