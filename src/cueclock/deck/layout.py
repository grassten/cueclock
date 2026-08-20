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
are on the face and neither can be mistaken for the other; a key with a hold on
it says so on its second line, because a gesture nothing names is a feature only
its author knows about (Session states where it is, Replay names the span a hold
would reach — the score keys are the exception, and only because their inverse
is self-evident and their result is already on the wall). The second line has
one other legitimate job, which Replay has always done and #43's scene key now
does too: **naming where a press goes** when the key's own word cannot. `BACK`
alone does not say back to what. What the second line may never be is context —
a fact about the world with no press behind it — because that is the promise
the whole grammar rests on.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import Enum

from cueclock.clock import MODES, RULESETS
from cueclock.deck.faces import (
    BG_ARMED,
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
    MATCH = STRIPE_MATCH  # between racks, no pressure — score, new rack, ruleset
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
# Rulesets with a countdown — derived, so a new untimed ruleset (freestyle)
# darkens the clock keys without another edit here.
TIMED = frozenset(name for name, ruleset in RULESETS.items() if ruleset.timed)


@dataclass(frozen=True)
class GameVerb:
    """One key on the active game's row, exactly as the server describes it.

    A mirror of `server.games.Verb` on this side of the HTTP boundary, so the
    deck package stays free of the server import and this module stays pure.
    `enabled=False` is a **dark** cell, not a dim one (layout rule A).
    """

    name: str
    text: str
    sub: str | None = None
    enabled: bool = True
    armed: bool = False


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
    replaying: bool = False  # a replay is on the TV — the Replay key says BACK
    session_face: str = "START"
    session_sub: str | None = None
    session_dot: Rgb | None = None
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
    # Minigames (#46). The whole games row is *dynamic* — the picker while no
    # game is chosen, the active game's own verbs after — so unlike every other
    # key it is described by the server rather than declared below. Outside
    # games mode all three fields are empty and row 3 is exactly what it was.
    games_active: str | None = None
    games_picker: tuple[tuple[str, str], ...] = ()  # (game name, key face)
    games_verbs: tuple[GameVerb, ...] = ()


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
class ReplayShow:
    """Save the OBS replay buffer and cut to it on the TV; press again for live.

    One key for both directions because there is only ever one thing to do with
    it: while live it starts a replay, while a replay is on it ends one. The
    face says which (`deck/replay.py` owns the sequence).

    Tap plays the last shot, hold plays the whole rack — the same tap/hold rule
    the score keys follow, and for the same reason: the common press has to stay
    instant. Putting the rare span behind a hold rather than behind a chooser
    page (Flag's pattern) is what keeps "watch that shot" at one press with
    nothing to read. Cost is that a tap now fires on *release*; a hold that
    matures fires while the key is still down, so the answer never waits for a
    finger to come off. Held length matches `ScoreAdjust`, deliberately: one
    hold dwell to learn for the whole deck.
    """

    hold_s: float = 0.6


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
class SceneToggle:
    """Swap the TV between the play scene and the bracket.

    Reads its state from the deck rather than from OBS: the daemon remembers
    the scene it left, exactly as the Replay key does, so the face never costs
    an obs-websocket round trip on a 2 s poll.
    """


@dataclass(frozen=True)
class GamePress:
    """A press on the games row (#46) — the picker's pick, or a game's verb.

    Its own action rather than a plain `ApiPost` because the row it sits on is
    *rewritten* by the press: picking a game swaps the picker for that game's
    verbs, and a verb can arm or darken its neighbours. So the daemon re-polls
    and repaints immediately instead of leaving the wrong row under a finger
    for up to one poll interval.
    """

    path: str
    ok_face: str = "OK"


Action = (
    ApiPost
    | Cycle
    | SessionToggle
    | ScoreAdjust
    | FlagStart
    | FlagReason
    | ReplayShow
    | BracketConfirm
    | SceneToggle
    | GamePress
)


# ---------- cycle label tables ----------

MODE_FACES: dict[str, str] = {
    "match": "MATCH",
    "run_out": "RUN OUT",
    "no_clock": "NO CLOCK",
    "games": "GAMES",
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
    text: str = ""  # static face text, where the face isn't state-derived
    # The remaining two are for keys built at runtime rather than declared
    # below — the games row (#46), whose faces come from the server. A
    # declared key derives its face from state instead, in `face()`.
    sub: str | None = None  # second line: where a press goes, or a gesture
    bg: Rgb | None = None  # armed background, as the bracket confirm uses
    meta: Mapping[str, str] = field(default_factory=dict)

    @property
    def index(self) -> int:
        """Hardware key number: 0-based, row-major from the top-left."""
        return (self.row - 1) * COLS + (self.col - 1)

    def visible(self, state: DeckState) -> bool:
        """Is this key pressable right now? If not, its cell goes dark."""
        if not state.up:
            return self.offline
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
        if isinstance(action, SceneToggle):
            # Same shape as the Replay key's two faces, and for the same
            # reason: one key, two directions, and the face says which.
            if state.on_bracket:
                return Face(text="BACK", sub="TO PLAY", stripe=stripe)
            return Face(text="BRACKET", sub="TO TV", stripe=stripe)
        if self.name == "start":
            # The countdown a press hands you, straight off the clock's own
            # current_base(): 60 s inside the WNT first-shot window, 15 under
            # Final 10, 30 otherwise. A plain "Reset" can't say which.
            return Face(text="START", big=str(state.base), stripe=stripe)
        if self.name == "replay":
            if state.replaying:
                # "BACK" alone would not say where, and "LIVE" alone is a
                # *state* on a key read as a command — the exact confusion
                # rule 3 exists to prevent.
                return Face(text="BACK", sub="TO LIVE", stripe=stripe)
            # Live: the verb, then the gesture that gets the other span. A hold
            # is invisible on a key that doesn't say it has one (rule 3).
            return Face(text=self.text, sub="HOLD RACK", stripe=stripe)
        if self.name == "extension":
            left = max(0, state.extensions_allowed - state.extensions_used)
            pips = (left, state.extensions_allowed) if state.extensions_allowed else None
            return Face(text="EXT", pips=pips, stripe=stripe)
        face = Face(text=self.text, sub=self.sub, stripe=stripe)
        return face if self.bg is None else replace(face, bg=self.bg)


LAYOUT: tuple[Key, ...] = (
    # --- the spine: top-left 2x2, present in every mode, never moves ---
    Key(1, 1, "session", Group.SYSTEM, SessionToggle(), offline=True),
    Key(1, 2, "mode", Group.SYSTEM, Cycle("mode")),
    Key(2, 1, "replay", Group.SYSTEM, ReplayShow(), text="REPLAY"),
    # Flag's reason page unfolds rightward into the empty cells beside it.
    Key(2, 2, "flag", Group.EVIDENCE, FlagStart(), text="FLAG"),
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
    # (2,3) sits inside the block Flag's reason page borrows while it is open,
    # which is fine and needs no new mechanism: the overlay already wins in
    # `_visible_key`, and a flag press is seconds long.
    Key(2, 3, "scene", Group.SYSTEM, SceneToggle(), needs_bracket=True),
)

# ---------- the games row (#46) ----------
#
# Row 3 — the row that is the mode's own block in every other mode — becomes
# the game picker in games mode, and the selected game's verbs replace the
# picker on the same row. Two levels, maximum: picker → game.
#
# All of it wears the **MATCH stripe**: the stripe channel answers "how much
# time do I have", and every games press is a between-shots press. No new
# colour, nothing new to learn.
#
# The row is dynamic — described by the server, not declared above — because
# what is on it depends on which games are registered and which one is active.
# Everything that is declared above and lives on row 3 (`ruleset`, `new_rack`,
# the score keys, the bracket confirm) is gated to CLOCKED or RACKED, so in
# games mode those cells are already dark and this overlay collides with
# nothing.
GAMES_MODE = "games"
GAMES_ROW = 3
# What the first picker card's key tag reads on the TV: 1-based, row-major.
# Mirrored by `server.games.GAMES_ROW_FIRST_KEY`; a unit test pins them.
GAMES_FIRST_KEY = (GAMES_ROW - 1) * COLS + 1


def games_keys(state: DeckState) -> tuple[Key, ...]:
    """The games row for this state — empty outside games mode.

    Disabled verbs are simply absent: a key that cannot be pressed goes dark,
    and a dark cell is exactly what the daemon paints where no key lives.
    """
    if state.mode != GAMES_MODE:
        return ()
    if state.games_active is None:
        entries = tuple(GameVerb(name=name, text=text) for name, text in state.games_picker)
        path = "/api/games/select/{}"
    else:
        entries = state.games_verbs
        path = "/api/games/press/{}"
    return tuple(
        Key(
            GAMES_ROW,
            col,
            f"game_{verb.name}",
            Group.MATCH,
            GamePress(path.format(verb.name), ok_face=verb.text),
            text=verb.text,
            sub=verb.sub,
            bg=BG_ARMED if verb.armed else None,
        )
        for col, verb in enumerate(entries[:COLS], start=1)
        if verb.enabled
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
REPLAY_PENDING_FACE = "CUEING…"  # while the buffer is written and seeked
# A held press says what it heard, not just that it is working: the hold fires
# under the finger, and this face is the only confirmation the gesture landed
# before the TV changes a second or two later.
RACK_PENDING_FACE = "RACK…"
# A confirm does four things across two processes (result, next match, rack,
# scene) and takes as long as the slowest — so the key says it is working.
CONFIRM_PENDING_FACE = "SAVING…"
