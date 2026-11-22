"""Declarative key layout — the single source of truth for what is on the deck.

The button map in docs/hardware.md Part C mirrors this table; change both in the
same commit.

Three rules shape it (2026-08-02, layout v2):

**A key that can't be pressed goes dark, it doesn't dim.** Visibility is
declared per key against the session mode and the ruleset, so what is lit is
always exactly what is pressable. Nothing reflows when a key leaves: every
survivor keeps its cell, so position stays learnable even though the set
changes. Dimming survives only as typography (`Face.sub`), never as state.

**The spine sits top-left and never moves.** Session and Mode work in every
mode, so they hold the top of the 2x2 corner; its bottom row is empty, (2,2)
since the Flag key was removed in checkpoint 114 and (2,1) since the Replay key
was removed in checkpoint 144. Everything to the right of them is
mode-dependent. Position therefore answers "does this key persist" while colour
answers "how much time do I have" — two questions, two channels.

**Face grammar** — the `OFF`-button confusion was never the word, it was a
*state* on a key read as a *command*. So: command keys say the verb and nothing
else; cycle keys put the current value bright over the next value dim, so both
are on the face and neither can be mistaken for the other; a key with a hold on
it says so on its second line, because a gesture nothing names is a feature only
its author knows about (Session states where it is — the score keys are the
exception, and only because their inverse is self-evident and their result is
already on the wall). The second line has one other legitimate job, which
#43's scene key does: **naming where a press goes** when the key's own word
cannot. `BACK` alone does not say back to what. What the second line may never
be is context — a fact about the world with no press behind it — because that
is the promise the whole grammar rests on.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import Enum

from cueclock.clock import MODES, RULESETS
from cueclock.deck.faces import (
    BG,
    BG_ARMED,
    STRIPE_CLOCK,
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
    MATCH = STRIPE_MATCH  # between racks, no pressure — score, new rack, ruleset
    SYSTEM = STRIPE_SYSTEM  # session, mode, scene, recal

    @property
    def stripe(self) -> Rgb:
        stripe: Rgb = self.value
        return stripe


# Modes the clock actually runs in — the keys that shape a countdown are absent
# outside them. Named from the modes table so a rename fails loudly here.
CLOCKED = frozenset({"match", "run_out"})
RACKED = frozenset({"match"})
NINE_BALL = frozenset({"wnt_9ball"})
# Rulesets with a countdown — derived, so a new untimed ruleset (freestyle)
# darkens the clock keys without another edit here.
TIMED = frozenset(name for name, ruleset in RULESETS.items() if ruleset.timed)


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
    # Red for a start that failed. The face text already names it, but a key
    # that only changes its *words* is a key you have to be looking at — the
    # tile colour is what carries across the room.
    session_bg: Rgb = BG
    # The bracket (#43). `bracket_loaded` gates both new keys — with no bracket
    # their cells are dark and the deck is exactly the deck it was. `armed` is
    # the server's own derivation of "somebody has reached their race", so the
    # key lights on the same fact the phone and the wall are showing.
    bracket_loaded: bool = False
    bracket_match_id: int | None = None
    bracket_match: str | None = None  # "MATCH 4"
    bracket_side: str | None = None  # "A SIDE"
    bracket_armed: tuple[str, int, int] | None = None  # (name, their score, other)
    on_bracket: bool = False  # the bracket scene is on the TV, not the table
    # The RECAL key (#49) needs the camera, so it exists only while no session
    # holds it: server down *and* the session machine at rest (OFF or a failed
    # start, not STARTING/STOPPING). Its face is the recalibration's own
    # (`deck/recal.py`), passed in like `session_face`.
    session_idle: bool = False
    recal_face: Face = field(default_factory=Face)


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
class BracketConfirm:
    """Commit the armed bracket result — one press, four effects (#43).

    **Reaching the target arms the result, it never fires it.** Plus/minus is
    the easiest control on the deck to fat-finger, and auto-advancing on the
    winning point would turn one stray press into a bracket somebody has to
    unwind mid-party. Arming costs one deliberate press and makes that failure
    a shrug: hold the winner's key to drop them a point and the arm clears
    itself, so there is no undo to find.

    Confirming writes the result and routes both players, loads the next ready
    match at 0-0 with its own targets, resets the rack (a new match is a new
    rack), and flips the TV to the bracket so the update is on the wall while
    the next pair racks. The first three are one server call; the scene flip is
    this side's, because obs-websocket lives out here (`hardware.md` §C3).
    """


@dataclass(frozen=True)
class Recalibrate:
    """Record a fresh empty-table reference and refit, on a deliberate hold (#49).

    A hold, because it overwrites the calibration, and a longer one than
    Session's, so neither can be mistaken for the other. Only lit while the
    session is off: `run` and `record` cannot both hold the camera. The work
    is `cueclock recalibrate`, run as a child process; the face shows it
    running and then the result, which stays until the next hold.
    """

    hold_s: float = 2.0


@dataclass(frozen=True)
class SceneToggle:
    """Swap the TV between the play scene and the bracket.

    Reads its state from the deck rather than from OBS: the daemon remembers
    the scene it left, so the face never costs an obs-websocket round trip on a
    2 s poll.
    """


Action = ApiPost | Cycle | SessionToggle | ScoreAdjust | BracketConfirm | SceneToggle | Recalibrate


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
    "freestyle": "FREE\nSTYLE",
}

_CYCLES: dict[str, tuple[Mapping[str, str], tuple[str, ...], str, str]] = {
    # kind -> (label table, cycle order, listing endpoint, POST prefix)
    "mode": (MODE_FACES, tuple(MODES), "/api/modes", "/api/mode/"),
    "ruleset": (RULESET_FACES, tuple(RULESETS), "/api/rulesets", "/api/ruleset/"),
}


def first_name(name: str) -> str:
    """What fits on a 72 px key: the first name, or the whole one if it is one
    word. A full "Priya Shah" would be auto-shrunk to unreadable by `_fit`, and
    the key already names the match, so there is no ambiguity to resolve.

    Eight characters, because `faces._MAIN_SIZES` bottoms out at 10 px and a
    ninth would not fit the tile at that size — `_fit` falls through to the
    smallest rather than failing, so an over-long name is *clipped*, which is
    the one outcome worse than a truncated one.
    """
    return (name.split()[0] if name.split() else name).upper()[:8]


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
    needs_bracket: bool = False  # dark unless a bracket is loaded (#43)
    offline: bool = False  # survives a server-down deck (Session only)
    # The inverse: lit only while the server is down and the session is at
    # rest, dark the moment one is running (RECAL only).
    offline_only: bool = False
    text: str = ""  # static face text, where the face isn't state-derived
    meta: Mapping[str, str] = field(default_factory=dict)

    @property
    def index(self) -> int:
        """Hardware key number: 0-based, row-major from the top-left."""
        return (self.row - 1) * COLS + (self.col - 1)

    def visible(self, state: DeckState) -> bool:
        """Is this key pressable right now? If not, its cell goes dark."""
        if not state.up:
            return self.offline or (self.offline_only and state.session_idle)
        if self.offline_only:
            return False
        if self.needs_bracket and not state.bracket_loaded:
            return False
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
                bg=state.session_bg,
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
        if isinstance(action, BracketConfirm):
            if state.bracket_armed is None:
                # Nothing to confirm: the key just names the loaded match, so
                # the deck always says which one the score keys are keeping.
                # Both lines are main-tier rather than text-over-sub, because a
                # sub line promises "one gesture away" and this face has no
                # gesture behind it at all.
                label = state.bracket_match or "—"
                return Face(
                    text=f"{label}\n{state.bracket_side}" if state.bracket_side else label,
                    stripe=stripe,
                )
            name, score, other = state.bracket_armed
            return Face(
                text=f"{first_name(name)}\nWINS {score}-{other}",
                # One word, not the mockup's "PRESS TO CONFIRM": on one line
                # that is wider than the tile at every size `_fit` will try (and
                # `_fit` clips rather than fails), and on two the four-line
                # stack overflows the tile and eats the winner's name. The verb
                # alone is what rule 3 asks for anyway.
                sub="CONFIRM",
                bg=BG_ARMED,
                stripe=stripe,
            )
        if isinstance(action, Recalibrate):
            return replace(state.recal_face, stripe=stripe)
        if isinstance(action, SceneToggle):
            # One key, two directions, and the face says which. "BACK" alone
            # would not say where, so the second line names it (rule 3).
            if state.on_bracket:
                return Face(text="BACK", sub="TO PLAY", stripe=stripe)
            return Face(text="BRACKET", sub="TO TV", stripe=stripe)
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
    # --- the clock: top-right, absent once the clock is off ---
    # Start replaced Reset + Force 30, which differed by exactly one bit: both
    # set the countdown to full and both are rack-aware; one landed IDLE, the
    # other RUNNING. The stopped variant moved to `no_clock`, where it's
    # visible. "Force 30" was misnamed too — it gives 60 in the WNT window.
    Key(
        1,
        3,
        "start",
        Group.CLOCK,
        ApiPost("/api/restart", "GO", "ERROR"),
        modes=CLOCKED,
        rulesets=TIMED,
    ),
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
    # It never goes dark inside a clocked mode with a clock to extend (under
    # freestyle there is no countdown, so it leaves with the other clock
    # keys); while the extension limit is unenforced the pool reads 0 and the
    # face shows no pips.
    Key(
        1,
        5,
        "extension",
        Group.CLOCK,
        ApiPost("/api/extension", "OK", "NO EXT"),
        modes=CLOCKED,
        rulesets=TIMED,
    ),
    # --- the match: bottom row, blue block ---
    Key(3, 1, "ruleset", Group.MATCH, Cycle("ruleset"), modes=CLOCKED),
    # One key for both clocked modes: racking a fresh rack and throwing
    # fifteen balls back up are the same act to the clock. Clock parks,
    # extensions refill, shot count zeroes.
    Key(
        3,
        2,
        "new_rack",
        Group.MATCH,
        ApiPost("/api/rack", "RACK", "ERROR"),
        modes=CLOCKED,
        text="NEW\nRACK",
    ),
    Key(3, 3, "p1", Group.MATCH, ScoreAdjust("p1"), modes=RACKED),
    Key(3, 4, "p2", Group.MATCH, ScoreAdjust("p2"), modes=RACKED),
    # (3,5) took the bracket's confirm key on 2026-08-13 (#43). It had been
    # free since 2026-08-05, when the "I shot" ground-truth key (#19) was
    # removed — a key the player must remember on every shot is a key that
    # stops being pressed, and it was pressed in two sessions out of
    # twenty-nine; the denominator it existed to provide was already in the
    # journal, so `cueclock report` now derives the miss rate with nothing
    # asked of the table (`server/report.py`). Because the cell was empty, no
    # existing key changes meaning: with no bracket loaded it stays dark.
    Key(3, 5, "confirm", Group.MATCH, BracketConfirm(), modes=RACKED, needs_bracket=True),
    # RECAL (#49) takes (2,5). It is on the far side of the deck from
    # Session, so the key that starts a night is never beside the one that
    # rewrites the calibration.
    Key(2, 5, "recal", Group.SYSTEM, Recalibrate(), offline_only=True),
    Key(2, 3, "scene", Group.SYSTEM, SceneToggle(), needs_bracket=True),
)

# A confirm does four things across two processes (result, next match, rack,
# scene) and takes as long as the slowest — so the key says it is working.
CONFIRM_PENDING_FACE = "SAVING…"
