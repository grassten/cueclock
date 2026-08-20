"""Minigames platform (#46): the registry, the active game, and the games.

A minigame is a **registered event consumer** — the same registry pattern
``RULESETS`` uses, plain code in this repo, no plugin loading and no
config-file game definitions. The vision core is untouched by construction:
games sit at the Hub seam the clock and ``last_settle_movers`` already use, so
the corpus stays green without anything in ``vision/`` moving (docs/design.md
§14).

The contract, frozen to exactly what the two launch games need (**rule of
two** — nothing speculative):

- **A game receives** the event stream (:meth:`Game.on_shot`,
  :meth:`Game.on_settle`, :meth:`Game.on_scratch`) plus the resting layout and
  the cue crown, and a :meth:`Game.tick` for anything that has to move between
  events (the placement countdown).
- **A game provides** its deck verbs (:meth:`Game.verbs`), the HUD bar state
  for the TV (:meth:`Game.hud`), the marks it wants on the cloth
  (:meth:`Game.marks`), and namespaced journal lines through :class:`GameCtx`.
- **One active game at a time**, owned by the Hub. Entering games mode shows
  the picker; leaving it ends the game. There is no parking, and nothing is
  lost by that: a game's record lives in the journal.

Roles, and the one place this file departs from the mockups: the bar's
per-ball lockups are labelled ``CUE`` / ``OBJECT`` / ``BALL n``, not the
mockup's ``RED`` / ``BLACK``. Colour names would need 16-ball identity, which
#46 puts out of scope on every surface; the three identity tiers it *does*
specify (crown, elimination, lineage) can name a role but never a colour.

Pure module: no clocks, no threads, no HTTP. The Hub calls in; wall time
arrives as arguments.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from cueclock.display.marks import Bracket, Mark

# ---------------------------------------------------------------------------
# Table geometry
# ---------------------------------------------------------------------------
#
# Duplicated numbers, deliberately, with `tests/unit/test_games.py` as the
# thing that keeps them honest: importing `vision.calibration` (or
# `vision.pipeline`) would drag cv2 and numpy into the bare server, which the
# Hub goes out of its way to avoid (see its TYPE_CHECKING block). A unit test
# imports both sides and asserts they agree, so drift fails a test rather than
# silently mis-judging a zone.
TABLE_LENGTH_MM = 1930.0  # == vision.calibration.TABLE_LENGTH_MM
TABLE_WIDTH_MM = 965.0  # == vision.calibration.TABLE_WIDTH_MM
BALL_RADIUS_MM = 28.575  # == vision.pipeline.BALL_RADIUS_MM (2 1/4", WPA spec)

# The diamond grid. The standard diamond layout divides the playing surface
# into 8 x 4 squares, and every coordinate here is derived from the calibrated
# playing area — there is no invented geometry constant, which is the whole
# reason zones are grid cells rather than free circles (#46).
GRID_COLS = 8
GRID_ROWS = 4

# ---------------------------------------------------------------------------
# The two constants #46 held back for build time, both anchored on measurements
# already in the repo (the no-blind-thresholds rule), anchor stated here.
# ---------------------------------------------------------------------------
#
# Ghost placement tolerance: how close a ball must be to its saved spot before
# the drill will arm itself. Anchored on the tracker's stationary noise as the
# event layer already prices it (`vision/events.py` EventConfig):
#
#   settle_creep_mm = 5.0    the residual wobble a *settled* ball is allowed
#   moving_enter_mm = 30.0   the net displacement that means a ball MOVED
#
# Both ends are hard. Below the creep figure a hand-placed ball could never
# reliably read "on the ghost" — its own resting noise would keep flipping the
# verdict — and at or above the motion figure a ball the event layer would
# score as *moved from* the ghost would still read as standing on it. 15 mm is
# 3x the creep and half the motion threshold: comfortably outside resting
# noise, comfortably inside a displacement anything else in the system would
# call movement. In felt terms it is a little over half a ball radius
# (28.6 mm), which is what "on the ring" looks like on the wall.
PLACEMENT_TOLERANCE_MM = 15.0

# Cell edge margin: the chalk-line dispute — a ball resting exactly on a cell
# boundary. Same anchor, used at its lower end: `settle_creep_mm = 5.0` is the
# measured wobble of a ball that has come to rest, so a centre within 5 mm of a
# boundary genuinely cannot be assigned to one side of it. The margin is
# applied **outward** — a ball within it of the zone counts as in — so the
# unresolvable case fails toward giving the shot to the player. Failing the
# other way would mean losing a shot you made to a measurement nobody can take.
CELL_EDGE_MARGIN_MM = 5.0

# ---------------------------------------------------------------------------
# Journal kinds — namespaced, additive as ever (`server/journal.py`).
# ---------------------------------------------------------------------------
GAME_STARTED = "game_started"
GAME_ENDED = "game_ended"
DRILL_ARMED = "drill_armed"
DRILL_ATTEMPT = "drill_attempt"
LAG_ATTEMPT = "lag_attempt"


# ---------------------------------------------------------------------------
# Evidence in, faces out
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Ball:
    """One ball as the games layer sees it: a position and the crown.

    ``is_cue`` is the tracker's whiteness crown, which is tier 1 of #46's
    identity ladder and the reason cue verdicts are immune to mid-shot label
    swaps: the EMA freezes while a ball flies and re-converges within ~0.3 s at
    rest, so "which resting ball is the cue" is re-derived visually at the
    settle rather than inherited from whatever the tracks did mid-collision.
    """

    x_mm: float
    y_mm: float
    is_cue: bool = False


@dataclass(frozen=True)
class Verb:
    """One key on the active game's deck row.

    ``enabled=False`` is a **dark** cell, not a dim one — layout v2 rule A: what
    is lit is exactly what is pressable. ``sub`` is the face's second line and
    obeys the same grammar as every other key: it names where a press goes or
    what gesture is on the key, never context.
    """

    name: str
    text: str
    sub: str | None = None
    enabled: bool = True
    armed: bool = False  # the deck's armed background (SAVE, once it can save)

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "text": self.text,
            "sub": self.sub,
            "enabled": self.enabled,
            "armed": self.armed,
        }


@dataclass(frozen=True)
class Stat:
    """One centre lockup in the HUD bar: label over value, optional caption."""

    label: str
    value: str
    unit: str | None = None
    sub: str | None = None
    ok: bool = False  # collapsed to a quiet tick — inside tolerance, in zone

    def as_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "value": self.value,
            "unit": self.unit,
            "sub": self.sub,
            "ok": self.ok,
        }


@dataclass(frozen=True)
class Answer:
    """The bar's right-hand answer cell — where the clock lives during a match.

    ``fill`` escalates **by area**, the rule every overlay already obeys:
    ``"pass"`` floods the cell accent, ``"fail"`` floods it alarm. Games mode
    has no clock, so a FAIL cannot collide with an expiry alarm.
    """

    word: str
    unit: str | None = None
    eyebrow: str | None = None
    fill: str | None = None  # None | "pass" | "fail"
    small: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "word": self.word,
            "unit": self.unit,
            "eyebrow": self.eyebrow,
            "fill": self.fill,
            "small": self.small,
        }


@dataclass(frozen=True)
class Hud:
    """The whole games bar: who/context left, stat lockups centre, answer right.

    One shape for every game, because the bar is the live scene's plinth
    wearing games content and the wall keeps one voice (mockups v1 §2).
    """

    name: str
    context: str | None = None
    stats: tuple[Stat, ...] = ()
    answer: Answer | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "context": self.context,
            "stats": [s.as_dict() for s in self.stats],
            "answer": self.answer.as_dict() if self.answer is not None else None,
        }


@dataclass
class GameCtx:
    """What a game may do to the world: journal a line, and read the roster.

    Deliberately tiny. A game cannot touch the clock, the score, OBS or the
    disk — everything it wants to persist goes through the journal, which is
    what makes abandoning a game lossless and its record readable by
    ``cueclock report`` without the game being involved.
    """

    log: Callable[..., None]
    players: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# The game interface
# ---------------------------------------------------------------------------


class Game:
    """Base class: a registered event consumer with a deck row and a bar.

    Subclasses override what they use. Everything here is a no-op or an empty
    answer, so a game only writes the halves it actually has — the lag trainer
    has no verbs at all beyond BACK, and says so by not overriding
    :meth:`verbs`.
    """

    name = ""  # registry key, and the `game` field on every journal line
    title = ""  # the deck key face and the menu card heading
    description = ""  # the menu card's sentence

    def __init__(self, ctx: GameCtx) -> None:
        self.ctx = ctx

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        """The game has been picked. The table is whatever it is."""

    def stop(self) -> None:
        """Games mode left, or another game picked. Nothing to save."""

    # -- the event stream -------------------------------------------------
    def on_shot(self, t: float, speed_mms: float | None) -> None:
        """A ShotTaken landed."""

    def on_settle(self, t: float, balls: Sequence[Ball]) -> None:
        """BallsSettled landed; ``balls`` is the resting layout with the crown."""

    def on_scratch(self, t: float) -> None:
        """A confirmed Scratch verdict."""

    def tick(self, balls: Sequence[Ball], moving: bool) -> None:
        """The 10 Hz broadcast tick: the current layout and the motion bit.

        This is where anything that has to move between events happens — the
        placement countdown and the auto-arm. ``moving`` is the vision layer's
        identity-free table-motion bit (§13.3), which is what "and settled"
        means for arming: no roster machinery, so nothing it could be lying
        about.
        """

    # -- the surfaces -----------------------------------------------------
    def verbs(self) -> tuple[Verb, ...]:
        """The keys that go on the deck's games row while this game owns it."""
        return ()

    def press(self, verb: str) -> bool:
        """Handle one verb press. False = this game has no such verb."""
        return False

    def hud(self) -> Hud:
        """The bar."""
        return Hud(name=self.title)

    def marks(self) -> tuple[Mark, ...]:
        """What this game wants drawn on the cloth."""
        return ()

    def card(self, stats: GameStats) -> dict[str, object]:
        """The menu card's numbers, from journal-derived session/history stats."""
        return {"stats": [], "last_played": stats.last_played}


# ---------------------------------------------------------------------------
# Lag — the contract's minimal proof
# ---------------------------------------------------------------------------


class LagGame(Game):
    """Down the table and back: how close to the head rail did it stop?

    **No arming, deliberately.** Every strike→settle in the game is an attempt,
    because only the best one matters — which makes a bad attempt free, and
    makes a retrieval push you did not mean a number you ignore rather than a
    state you have to clear. The readout says which attempt was the best one,
    so nothing is lost in the noise.

    The measurement is the gap between the ball's near edge and the head rail
    (x = 0 in table coordinates), not its centre: that is the gap a player
    would measure with a piece of chalk, and a ball frozen on the cushion
    should read ~0 rather than one ball radius.

    Which ball: the crown (identity tier 1). A lag is played with the cue ball,
    and the crown is the same evidence scratch adjudication trusts. With no
    crown at rest — the cue is a stripe-lookalike away from every white ball —
    a table holding exactly one ball is unambiguous anyway, so that falls back
    by elimination. Anything else is not a lag and scores nothing.
    """

    name = "lag"
    title = "LAG"
    description = (
        "Down the table and back. Every settle scores itself — distance off "
        "the head rail in millimeters, best of the session on the wall."
    )

    def __init__(self, ctx: GameCtx) -> None:
        super().__init__(ctx)
        self.attempts = 0
        self.last_mm: float | None = None
        self.best_mm: float | None = None
        self._mark: Bracket | None = None

    def on_settle(self, t: float, balls: Sequence[Ball]) -> None:
        ball = _lag_ball(balls)
        if ball is None:
            return
        distance = max(0.0, ball.x_mm - BALL_RADIUS_MM)
        self.attempts += 1
        self.last_mm = distance
        if self.best_mm is None or distance < self.best_mm:
            self.best_mm = distance
        self._mark = Bracket(x0_mm=0.0, x1_mm=ball.x_mm - BALL_RADIUS_MM, y_mm=ball.y_mm)
        self.ctx.log(
            LAG_ATTEMPT,
            game=self.name,
            distance_mm=round(distance),
            attempt=self.attempts,
            best_mm=round(self.best_mm),
        )

    def hud(self) -> Hud:
        stats: tuple[Stat, ...] = ()
        if self.last_mm is not None:
            stats = (Stat(label="LAST", value=str(round(self.last_mm)), unit="MM"),)
        answer = (
            Answer(word=str(round(self.best_mm)), unit="MM", eyebrow="BEST")
            if self.best_mm is not None
            else Answer(word="—", eyebrow="BEST", small=True)
        )
        return Hud(
            name="Lag",
            context=f"Attempt {self.attempts + 1}" if self.attempts else "Roll one down",
            stats=stats,
            answer=answer,
        )

    def marks(self) -> tuple[Mark, ...]:
        # No grid: lag has no zones, so the cloth carries the measurement and
        # nothing else (mockups v1 §4).
        return () if self._mark is None else (self._mark,)

    def card(self, stats: GameStats) -> dict[str, object]:
        best = stats.numbers.get("best_mm")
        return {
            "stats": [
                # No unit on an em-dash: "—MM" is a unit attached to nothing,
                # and it reads as a broken number rather than as no number.
                {"label": "BEST EVER", "value": "—"}
                if best is None
                else {"label": "BEST EVER", "value": str(round(best)), "unit": "MM"},
                {"label": "ATTEMPTS", "value": str(round(stats.numbers.get("attempts", 0)))},
            ],
            "last_played": stats.last_played,
        }


def _lag_ball(balls: Sequence[Ball]) -> Ball | None:
    """The ball a lag is measured on — crown first, then elimination."""
    crowned = [b for b in balls if b.is_cue]
    if len(crowned) == 1:
        return crowned[0]
    return balls[0] if len(balls) == 1 else None


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

GAMES: dict[str, type[Game]] = {game.name: game for game in (LagGame,)}


@dataclass(frozen=True)
class GameStats:
    """One game's journal-derived history, for its menu card.

    Free-form ``numbers`` rather than fixed fields: each game reads its own
    journal kinds and knows what they mean, and a new game's card must not
    need a schema change here (`server/report.py` derives them).
    """

    numbers: dict[str, float] = field(default_factory=dict)
    last_played: str | None = None


EMPTY_STATS = GameStats()


# ---------------------------------------------------------------------------
# The session: one active game, owned by the Hub
# ---------------------------------------------------------------------------


class GameSession:
    """Registry + active game + the WS state both the TV and the deck read.

    Lives on the Hub and is fed from two threads under the same contract the
    rest of the Hub uses: the camera thread only ever *rebinds* attributes
    (:meth:`note_balls`), and every journal write happens on the asyncio loop.
    """

    def __init__(
        self,
        log: Callable[..., None],
        stats: Callable[[], dict[str, GameStats]] | None = None,
    ) -> None:
        self._log = log
        self._stats = stats
        self.active: Game | None = None
        # The live layout, rebound by the camera thread. A tuple so a reader on
        # the asyncio loop can never see it half-written.
        self.balls: tuple[Ball, ...] = ()
        self.moving = False

    # -- lifecycle --------------------------------------------------------

    def select(self, name: str, players: list[str]) -> bool:
        """Pick a game. False if the registry has no such name."""
        factory = GAMES.get(name)
        if factory is None:
            return False
        self.back()
        game = factory(GameCtx(log=self._log, players=players))
        self.active = game
        self._log(GAME_STARTED, game=name, players=players)
        game.start()
        return True

    def back(self) -> None:
        """End the active game and show the picker. Nothing is parked."""
        if self.active is None:
            return
        game = self.active
        self.active = None
        game.stop()
        self._log(GAME_ENDED, game=game.name)

    # -- evidence in (camera thread: rebinding only) ----------------------

    def note_balls(self, balls: tuple[Ball, ...], moving: bool) -> None:
        self.balls = balls
        self.moving = moving

    # -- evidence in (asyncio loop) ---------------------------------------

    def on_shot(self, t: float, speed_mms: float | None) -> None:
        if self.active is not None:
            self.active.on_shot(t, speed_mms)

    def on_settle(self, t: float) -> None:
        """The resting layout is the newest camera-thread snapshot.

        A settle fires only after dwell-seconds of renewed quiet, so the most
        recent frame's layout *is* the settled layout — there is nothing to
        plumb through the event and nothing that could disagree with it.
        """
        if self.active is not None:
            self.active.on_settle(t, self.balls)

    def on_scratch(self, t: float) -> None:
        if self.active is not None:
            self.active.on_scratch(t)

    def tick(self) -> None:
        if self.active is not None:
            self.active.tick(self.balls, self.moving)

    # -- surfaces out ------------------------------------------------------

    def press(self, verb: str) -> bool:
        if verb == "back":
            self.back()
            return True
        return self.active is not None and self.active.press(verb)

    def marks(self) -> tuple[Mark, ...]:
        return () if self.active is None else self.active.marks()

    def payload(self) -> dict[str, object]:
        """The `games` object on every broadcast — `null` outside games mode.

        Carries both postures of the one `/games` source: with no active game
        it is the picker (the page's opaque menu, cards in deck order, each
        tagged with its key), and with one it is that game's bar and verbs.
        """
        if self.active is None:
            return {"active": None, "picker": self.picker(), "hud": None, "verbs": []}
        return {
            "active": self.active.name,
            "picker": [],
            "hud": self.active.hud().as_dict(),
            "verbs": [v.as_dict() for v in self.verbs()],
        }

    def verbs(self) -> tuple[Verb, ...]:
        """BACK plus the active game's own — BACK is the platform's, always first.

        Every games key wears the MATCH stripe out on the deck: the stripe
        channel answers "how much time do I have", and every games press is a
        between-shots press (mockups v1 §5).
        """
        if self.active is None:
            return ()
        return (Verb(name="back", text="BACK", sub="TO GAMES"), *self.active.verbs())

    def picker(self) -> list[dict[str, object]]:
        """The menu cards, in deck order, each naming the key that picks it."""
        stats = self._stats() if self._stats is not None else {}
        cards: list[dict[str, object]] = []
        for slot, (name, factory) in enumerate(GAMES.items()):
            game = factory(GameCtx(log=self._log))
            card = game.card(stats.get(name, EMPTY_STATS))
            cards.append(
                {
                    "name": name,
                    "title": factory.title,
                    "description": factory.description,
                    # 1-based hardware key number, which is what the deck's own
                    # documentation counts in and what the card tag reads.
                    "key": GAMES_ROW_FIRST_KEY + slot,
                    **card,
                }
            )
        return cards


# The picker occupies the deck's row 3 from (3,1) — the row that is the mode's
# own block in every other mode — so the first card's key is number 11 (1-based,
# row-major on a 5-wide MK.2). Mirrored by `deck/layout.py`; the two are checked
# against each other in `tests/unit/test_deck.py`.
GAMES_ROW_FIRST_KEY = 11
