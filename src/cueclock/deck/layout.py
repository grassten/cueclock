"""Declarative key layout — the single source of truth for what is on the deck.

The button map in docs/hardware.md Part C mirrors this table; change both in the
same commit.

Three rules shape it (2026-08-02, layout v2):

**A key that can't be pressed goes dark, it doesn't dim.** Visibility is
declared per key against the session mode and the ruleset, so what is lit is
always exactly what is pressable. Nothing reflows when a key leaves: every
survivor keeps its cell, so position stays learnable even though the set
changes. Dimming survives only as typography (`Face.sub`), never as state.

**The spine sits top-left and never moves.** Session, Mode, Replay and Flag work
in every mode, so they hold the 2x2 corner; everything to the right of them is
mode-dependent. Position therefore answers "does this key persist" while colour
answers "how much time do I have" — two questions, two channels.

**Face grammar** — the `OFF`-button confusion was never the word, it was a
*state* on a key read as a *command*. So: command keys say the verb and nothing
else; cycle keys put the current value bright over the next value dim, so both
are on the face and neither can be mistaken for the other; the one hold key
states where it is and names the gesture out.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum

from cueclock.clock import MODES, RULESETS
from cueclock.deck.faces import (
    STRIPE_CLOCK,
    STRIPE_EVIDENCE,
    STRIPE_MATCH,
    STRIPE_SYSTEM,
    Face,
    Rgb,
)

ROWS = 3
COLS = 5  # MK.2: 15 keys, row-major from the top-left


class Group(Enum):
    """What a key's stripe colour says about how much time you have."""

    CLOCK = STRIPE_CLOCK  # seconds matter — extension, push, start
    MATCH = STRIPE_MATCH  # between racks, no pressure — score, set up, ruleset
    EVIDENCE = STRIPE_EVIDENCE  # flag and its reasons
    SYSTEM = STRIPE_SYSTEM  # session, mode, replay

    @property
    def stripe(self) -> Rgb:
        stripe: Rgb = self.value
        return stripe


# Modes the clock actually runs in — the keys that shape a countdown are absent
# outside them. Named from the modes table so a rename fails loudly here.
CLOCKED = frozenset({"match", "run_out"})
RACKED = frozenset({"match"})
NINE_BALL = frozenset({"wnt_9ball"})


@dataclass(frozen=True)
class DeckState:
    """Everything the faces need, polled from `GET /api/state` in one request.

    Session fields are plain data rather than a `SessionState`, so this module
    stays free of the process-management import.
    """

    up: bool = False
    mode: str | None = None
    ruleset: str | None = None
    base: int = 30  # what a fresh countdown gives you now — the START N face
    extensions_used: int = 0
    extensions_allowed: int = 0
    p1: int = 0
    p2: int = 0
    session_face: str = "START"
    session_sub: str | None = None
    session_dot: Rgb | None = None


# ---------- actions ----------


@dataclass(frozen=True)
class ApiPost:
    """POST a server path on press; the face flashes the outcome."""

    path: str
    ok_face: str
    err_face: str
    timeout: float = 3.0
    pending_face: str | None = None  # shown while a slow call is in flight


@dataclass(frozen=True)
class Cycle:
    """Advance a context axis; the face shows current over next."""

    kind: str  # "mode" | "ruleset"


@dataclass(frozen=True)
class SessionToggle:
    """Start the whole session on press; stop it on a deliberate hold.

    The one key that must work while the server is down — it is what starts the
    server. Held (not tapped) to stop, because a stray press mid-rack that
    killed the camera would be the worst key on the deck. State, faces, and the
    process lifecycle live in `session.py`.
    """

    hold_s: float = 1.0


@dataclass(frozen=True)
class ScoreAdjust:
    """Tap adds a point, hold takes one back.

    Four score keys became two on the same rule the rest of the deck follows:
    tap does the common thing, hold does the rare inverse. A mis-tap is visible
    on the wall scoreboard, which is the real feedback for these keys anyway.
    """

    player: str  # "p1" | "p2"
    hold_s: float = 0.6

    def path(self, *, decrement: bool) -> str:
        return f"/api/score/{self.player}/{'decrement' if decrement else 'increment'}"


@dataclass(frozen=True)
class FlagStart:
    """Open the reason page. The dump fires from whichever reason is chosen.

    One key pressed by feel beats reading three faces inside a 20 s ring — and
    the reason itself has no deadline, so it moves off the reflex press. The
    page auto-files as OTHER after `auto_s`, which is what an unspecified flag
    has always meant, so walking away still saves the clip. Cost is the couple
    of seconds between the two presses, off a 20 s ring (`--ring-seconds`).
    """

    auto_s: float = 4.0
    auto_reason: str = "flag_other"


@dataclass(frozen=True)
class FlagReason:
    """One reason on the transient page; dumps the ring under its own label.

    The label names the clip dir (`<dump-dir>/<label>_N`) and rides the
    journal's `flag_dump` line, so triage starts from *why* it was pressed
    (2026-08-01: an OBS-side failure spent two triage passes being chased as a
    vision bug because the label was always `flagged`).
    """

    label: str
    timeout: float = 60.0  # a dump replies only after writing ~3.6 GB


@dataclass(frozen=True)
class ReplaySave:
    """Save the OBS replay buffer to disk over obs-websocket."""


Action = ApiPost | Cycle | SessionToggle | ScoreAdjust | FlagStart | FlagReason | ReplaySave


# ---------- cycle label tables ----------

MODE_FACES: dict[str, str] = {
    "match": "MATCH",
    "run_out": "RUN OUT",
    "no_clock": "NO CLOCK",
}

RULESET_FACES: dict[str, str] = {
    "ultimate_pool_8ball": "8-BALL",
    "ultimate_pool_8ball_final10": "FINAL\n10",
    "wnt_9ball": "9-BALL",
}

_CYCLES: dict[str, tuple[Mapping[str, str], tuple[str, ...], str, str]] = {
    # kind -> (label table, cycle order, listing endpoint, POST prefix)
    "mode": (MODE_FACES, tuple(MODES), "/api/modes", "/api/mode/"),
    "ruleset": (RULESET_FACES, tuple(RULESETS), "/api/rulesets", "/api/ruleset/"),
}


def cycle_label(kind: str, name: str | None) -> str:
    """Face text for a value; unknown names show raw (truncated) rather than hide."""
    if name is None:
        return "—"
    table, _, _, _ = _CYCLES[kind]
    return table.get(name, name[:12].upper())


def cycle_next(kind: str, active: str | None, available: list[str] | None = None) -> str:
    """The value after `active` in server order, wrapping; unknown -> the first."""
    _, order, _, _ = _CYCLES[kind]
    names = available if available else list(order)
    if not names:
        return active or ""
    try:
        return names[(names.index(active or "") + 1) % len(names)]
    except ValueError:
        return names[0]


def cycle_listing(kind: str) -> str:
    return _CYCLES[kind][2]


def cycle_path(kind: str, name: str) -> str:
    return _CYCLES[kind][3] + name


# ---------- keys ----------


@dataclass(frozen=True)
class Key:
    row: int  # 1-based from the top
    col: int  # 1-based from the left
    name: str
    group: Group
    action: Action
    modes: frozenset[str] | None = None  # None = every mode
    rulesets: frozenset[str] | None = None  # None = every ruleset
    offline: bool = False  # survives a server-down deck (Session only)
    text: str = ""  # static face text, where the face isn't state-derived
    meta: Mapping[str, str] = field(default_factory=dict)

    @property
    def index(self) -> int:
        """Hardware key number: 0-based, row-major from the top-left."""
        return (self.row - 1) * COLS + (self.col - 1)

    def visible(self, state: DeckState) -> bool:
        """Is this key pressable right now? If not, its cell goes dark."""
        if not state.up:
            return self.offline
        if self.modes is not None and state.mode is not None and state.mode not in self.modes:
            return False
        return not (
            self.rulesets is not None
            and state.ruleset is not None
            and state.ruleset not in self.rulesets
        )

    def face(self, state: DeckState) -> Face:
        """What this key looks like under the current state."""
        stripe = self.group.stripe
        action = self.action
        if isinstance(action, SessionToggle):
            return Face(
                text=state.session_face,
                sub=state.session_sub,
                dot=state.session_dot,
                stripe=stripe,
            )
        if isinstance(action, Cycle):
            active = state.mode if action.kind == "mode" else state.ruleset
            return Face(
                text=cycle_label(action.kind, active),
                sub=cycle_label(action.kind, cycle_next(action.kind, active)),
                stripe=stripe,
            )
        if isinstance(action, ScoreAdjust):
            score = state.p1 if action.player == "p1" else state.p2
            return Face(text=action.player.upper(), big=str(score), stripe=stripe)
        if self.name == "start":
            # The countdown a press hands you, straight off the clock's own
            # current_base(): 60 s inside the WNT first-shot window, 15 under
            # Final 10, 30 otherwise. A plain "Reset" can't say which.
            return Face(text="START", big=str(state.base), stripe=stripe)
        if self.name == "extension":
            left = max(0, state.extensions_allowed - state.extensions_used)
            pips = (left, state.extensions_allowed) if state.extensions_allowed else None
            return Face(text="EXT", pips=pips, stripe=stripe)
        return Face(text=self.text, stripe=stripe)


LAYOUT: tuple[Key, ...] = (
    # --- the spine: top-left 2x2, present in every mode, never moves ---
    Key(1, 1, "session", Group.SYSTEM, SessionToggle(), offline=True),
    Key(1, 2, "mode", Group.SYSTEM, Cycle("mode")),
    Key(2, 1, "replay", Group.SYSTEM, ReplaySave(), text="REPLAY"),
    # Flag's reason page unfolds rightward into the empty cells beside it.
    Key(2, 2, "flag", Group.EVIDENCE, FlagStart(), text="FLAG"),
    # --- the clock: top-right, absent once the clock is off ---
    # Start replaced Reset + Force 30, which differed by exactly one bit: both
    # set the countdown to full and both are rack-aware; one landed IDLE, the
    # other RUNNING. The stopped variant moved to `no_clock`, where it's
    # visible. "Force 30" was misnamed too — it gives 60 in the WNT window.
    Key(1, 3, "start", Group.CLOCK, ApiPost("/api/restart", "GO", "ERROR"), modes=CLOCKED),
    # Push outs are a 9-ball rule and need a break to follow, so the key is
    # absent under 8-ball and outside a rack.
    Key(
        1,
        4,
        "push",
        Group.CLOCK,
        ApiPost("/api/push", "PUSH", "ERROR"),
        modes=RACKED,
        rulesets=NINE_BALL,
        text="PUSH",
    ),
    # Top-right corner: the one key a player under the clock hunts for — it
    # must be findable without reading faces (Steven, 2026-08-01 match day).
    # It never goes dark inside a clocked mode; a spent pool shows as empty
    # pips, because those are the only readout of how many are left.
    Key(
        1,
        5,
        "extension",
        Group.CLOCK,
        ApiPost("/api/extension", "OK", "NO EXT"),
        modes=CLOCKED,
    ),
    # --- the match: bottom row, blue block ---
    Key(3, 1, "ruleset", Group.MATCH, Cycle("ruleset"), modes=CLOCKED),
    # One word for both clocked modes: racking is setting up, and so is
    # throwing fifteen balls back up. Clock parks, extensions refill, shot
    # count zeroes.
    Key(
        3,
        2,
        "setup",
        Group.MATCH,
        ApiPost("/api/rack", "SET", "ERROR"),
        modes=CLOCKED,
        text="SET\nUP",
    ),
    Key(3, 3, "p1", Group.MATCH, ScoreAdjust("p1"), modes=RACKED),
    Key(3, 4, "p2", Group.MATCH, ScoreAdjust("p2"), modes=RACKED),
)

# The transient reason page: shown only while a Flag press is waiting for a
# reason, overlaid on the cells to Flag's right (which are otherwise dark).
REASON_KEYS: tuple[Key, ...] = (
    Key(2, 3, "flag_phantom", Group.EVIDENCE, FlagReason("flag_phantom"), text="PHANTOM"),
    Key(2, 4, "flag_missed", Group.EVIDENCE, FlagReason("flag_missed"), text="MISSED"),
    Key(2, 5, "flag_other", Group.EVIDENCE, FlagReason("flag_other"), text="OTHER"),
)

FLAG_PENDING_FACE = "WHY?"  # on the Flag key itself while the page is open
DUMP_PENDING_FACE = "SAVING…"
