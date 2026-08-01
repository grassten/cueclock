"""Declarative key layout — the single source of truth for what is on the deck.

The button map in docs/hardware.md Part C mirrors this table; change both in the
same commit.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from cueclock.clock.rulesets import DEFAULT_RULESET, RULESETS

ROWS = 3
COLS = 5  # MK.2: 15 keys, row-major from the top-left


def _for_ruleset(by_ruleset: Mapping[str, str] | None, active: str | None, default: str) -> str:
    """Pick the active ruleset's text, falling back to the static default."""
    if by_ruleset is None or active is None:
        return default
    return by_ruleset.get(active, default)


@dataclass(frozen=True)
class ApiPost:
    """POST a server path on press; the face flashes the outcome."""

    path: str
    ok_face: str
    err_face: str
    timeout: float = 3.0  # raise for endpoints that work before replying (dump!)
    pending_face: str | None = None  # shown while a slow call is in flight
    ok_face_by_ruleset: Mapping[str, str] | None = None  # per-ruleset ok flash (else ok_face)

    def ok_face_for(self, active: str | None) -> str:
        """Ok-flash text under the active ruleset (static when no mapping / unpolled)."""
        return _for_ruleset(self.ok_face_by_ruleset, active, self.ok_face)


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
    face_by_ruleset: Mapping[str, str] | None = None  # per-ruleset face (else `face`)

    @property
    def index(self) -> int:
        """Hardware key number: 0-based, row-major from the top-left."""
        return (self.row - 1) * COLS + (self.col - 1)

    def face_for(self, active: str | None) -> str:
        """Face text under the active ruleset (static when no mapping / unpolled)."""
        return _for_ruleset(self.face_by_ruleset, active, self.face)


# The Extension key is ruleset-aware: Ultimate Pool extensions are 15 s, WNT
# extensions are 30 s (WNT Event Rules §5). Face and flash derive from the
# rulesets table so a rulebook correction there lands on the deck automatically.
_EXT_FACE: dict[str, str] = {n: f"EXT\n+{r.extension_seconds}" for n, r in RULESETS.items()}
_EXT_FLASH: dict[str, str] = {n: f"+{r.extension_seconds} s" for n, r in RULESETS.items()}


def _flag(label: str) -> ApiPost:
    """Ring-dump action for one flag reason; the label names the clip dir.

    Dump replies only after writing ~3.6 GB to the T7 — measured well over the
    3 s default (2026-07-31: three "timed out" flashes for dumps that all
    succeeded). Generous timeout + a pending face instead.
    """
    return ApiPost(
        f"/api/dump?label={label}",
        "SAVED",
        "NO T7",
        timeout=60.0,
        pending_face="SAVING…",
    )


LAYOUT: tuple[Key, ...] = (
    Key(1, 3, "ruleset", "RULESET", RulesetCycle()),
    # Push-out call: clock restarts at the 60 s first-shot time (WNT §5) and the
    # window re-arms for a pass-back. Push outs exist only in 9-ball, so the
    # face and flash stay static — no ruleset-aware treatment.
    Key(1, 4, "push", "PUSH", ApiPost("/api/push", "60 s", "ERROR")),
    # Top-right corner: the one key a player under the clock hunts for — it
    # must be findable without reading faces (Steven, 2026-08-01 match day).
    Key(
        1,
        5,
        "extension",
        _EXT_FACE[DEFAULT_RULESET],
        ApiPost(
            "/api/extension",
            _EXT_FLASH[DEFAULT_RULESET],
            "NO EXT",
            ok_face_by_ruleset=_EXT_FLASH,
        ),
        face_by_ruleset=_EXT_FACE,
    ),
    # Row 2 reads left to right as recovery, then evidence: the two manual
    # clock-recovery keys (Reset, Force 30) on the left, the flag family on
    # the right (2026-08-01; Force 30 moved from 2,4 to make the run).
    Key(2, 1, "reset", "RESET", ApiPost("/api/reset", "RESET", "ERROR")),
    Key(2, 2, "restart", "FORCE\n30", ApiPost("/api/restart", "GO", "ERROR")),
    # Flag family (2026-08-01): the single Flag split into three reason-coded
    # keys after an OBS-side failure masqueraded as a vision bug through two
    # triage passes — the label was "flagged" and carried no reason. Each key
    # dumps the same ring through /api/dump; only the label differs, and the
    # label names the clip dir (tests/clips/<label>_N) and rides the journal's
    # flag_dump line, so triage starts from *why* it was pressed.
    #   PHANTOM — an event was counted that shouldn't have been.
    #   MISSED  — a real shot/settle wasn't detected, or came late.
    #   OTHER   — display problems, anything else.
    # (2,5) — the old Flag cell — stays a flag key on purpose: a pre-split
    # reflex press still saves the ring, and OTHER is exactly what the old
    # unspecified flag meant.
    Key(2, 3, "flag_phantom", "FLAG\nPHANTOM", _flag("flag_phantom")),
    Key(2, 4, "flag_missed", "FLAG\nMISSED", _flag("flag_missed")),
    Key(2, 5, "flag_other", "FLAG\nOTHER", _flag("flag_other")),
    # Score row — the wall-TV scoreboard is the real press feedback; the tile
    # flash is just an ack. (Displaced the planned music keys; see BACKLOG.md.)
    # New Rack sits mid-row (moved from 2,3 on 2026-08-01) so the whole
    # between-racks ritual — adjust the score, rack the next game — is one row.
    Key(3, 1, "p1_plus", "P1 +", ApiPost("/api/score/p1/increment", "+1", "ERROR")),
    Key(3, 2, "p1_minus", "P1 -", ApiPost("/api/score/p1/decrement", "-1", "ERROR")),
    Key(3, 3, "rack", "NEW\nRACK", ApiPost("/api/rack", "RACKED", "ERROR")),
    Key(3, 4, "p2_minus", "P2 -", ApiPost("/api/score/p2/decrement", "-1", "ERROR")),
    Key(3, 5, "p2_plus", "P2 +", ApiPost("/api/score/p2/increment", "+1", "ERROR")),
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
