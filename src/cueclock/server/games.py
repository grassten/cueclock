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

import json
import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from math import hypot
from pathlib import Path

from cueclock.display.marks import Bracket, Circle, Grid, Line, Mark, Rect

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
    """One ball as the games layer sees it: a position, the crown, a track id.

    ``is_cue`` is the tracker's whiteness crown, which is tier 1 of #46's
    identity ladder and the reason cue verdicts are immune to mid-shot label
    swaps: the EMA freezes while a ball flies and re-converges within ~0.3 s at
    rest, so "which resting ball is the cue" is re-derived visually at the
    settle rather than inherited from whatever the tracks did mid-collision.

    ``ball_id`` is the tracker's own track id, and is the *weakest* tier — read
    only when a layout holds two or more object balls with requirements of
    their own, where neither the crown nor elimination can separate them.
    """

    x_mm: float
    y_mm: float
    is_cue: bool = False
    ball_id: int = -1


# ---------------------------------------------------------------------------
# The diamond grid: cells, zones, and the point-in-rectangle judge
# ---------------------------------------------------------------------------

CELL_LENGTH_MM = TABLE_LENGTH_MM / GRID_COLS
CELL_WIDTH_MM = TABLE_WIDTH_MM / GRID_ROWS

# What the ZONE key cycles through, in order. One cell, then a cell either way,
# then the square — the sizes a player would actually ask for, and small enough
# that the block stays something you can sight off the diamonds.
ZONE_SIZES: tuple[tuple[int, int], ...] = ((1, 1), (2, 1), (1, 2), (2, 2))


def size_face(w: int, h: int) -> str:
    """A zone size as every surface draws it: a multiplication sign, not an ex.

    Written as an escape rather than the character so the linter's ambiguous-
    unicode check stays on for the rest of the file — the glyph is deliberate
    here and nowhere else.
    """
    return f"{w}\u00d7{h}"


def _as_cell(value: object, default: int = 0) -> int:
    """One grid index off untyped JSON (or an HTTP body)."""
    return int(value) if isinstance(value, int | float) else default


def cell_of(x_mm: float, y_mm: float) -> tuple[int, int]:
    """Which diamond cell a point falls in, clamped to the playing surface.

    Clamped rather than refused: a ball resting against a cushion can read a
    hair outside the calibrated rectangle (the calibration is corpus-grade —
    the cloth outline stands in for the playing area), and "off the table" is
    not an answer anybody wants from a ball that is visibly on it.
    """
    col = min(GRID_COLS - 1, max(0, int(x_mm // CELL_LENGTH_MM)))
    row = min(GRID_ROWS - 1, max(0, int(y_mm // CELL_WIDTH_MM)))
    return col, row


@dataclass(frozen=True)
class Zone:
    """A rectangular block of diamond-grid cells — a target.

    Serialized as a **typed** object (`{"type": "cells", …}`) so a future zone
    type (a circle around the spot, a rail lane) is an addition rather than a
    migration (#46).
    """

    col: int
    row: int
    w: int = 1
    h: int = 1

    @property
    def bounds_mm(self) -> tuple[float, float, float, float]:
        return (
            self.col * CELL_LENGTH_MM,
            self.row * CELL_WIDTH_MM,
            (self.col + self.w) * CELL_LENGTH_MM,
            (self.row + self.h) * CELL_WIDTH_MM,
        )

    def nearest_mm(self, x_mm: float, y_mm: float) -> tuple[float, float]:
        """The closest point of the block to `(x, y)` — where a miss line ends."""
        x0, y0, x1, y1 = self.bounds_mm
        return (min(max(x_mm, x0), x1), min(max(y_mm, y0), y1))

    def miss_mm(self, x_mm: float, y_mm: float) -> float:
        """How far outside the block the point is; 0.0 inside it.

        The **edge margin is applied here**, so it applies once and applies
        everywhere: a centre within `CELL_EDGE_MARGIN_MM` of the block reads as
        a zero miss. That is the chalk-line dispute failing toward the player,
        which is the direction it has to fail — see the constant.
        """
        nx, ny = self.nearest_mm(x_mm, y_mm)
        raw = hypot(x_mm - nx, y_mm - ny)
        return 0.0 if raw <= CELL_EDGE_MARGIN_MM else raw

    def contains(self, x_mm: float, y_mm: float) -> bool:
        return self.miss_mm(x_mm, y_mm) == 0.0

    def resized(self, w: int, h: int, anchor: tuple[int, int]) -> Zone:
        """The same target at a new size, still covering `anchor`.

        It grows **toward the middle of the table**, then clamps to the grid.
        Growing outward from a target near a rail would push half the block off
        the cloth for nothing; growing inward puts the extra cell where the cue
        ball actually has room to end up.
        """
        col, row = anchor
        if col >= GRID_COLS / 2:
            col = col - (w - 1)
        if row >= GRID_ROWS / 2:
            row = row - (h - 1)
        col = min(GRID_COLS - w, max(0, col))
        row = min(GRID_ROWS - h, max(0, row))
        return Zone(col=col, row=row, w=w, h=h)

    def as_dict(self) -> dict[str, object]:
        return {"type": "cells", "col": self.col, "row": self.row, "w": self.w, "h": self.h}

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> Zone:
        """Parse a stored zone. Unknown types raise rather than degrade.

        The typed envelope only earns its keep if reading one it does not
        understand is an error: silently treating a future circle zone as a
        cell block would judge the shot against a target nobody set.
        """
        if raw.get("type") != "cells":
            raise ValueError(f"unknown zone type: {raw.get('type')!r}")
        return cls(
            col=_as_cell(raw.get("col")),
            row=_as_cell(raw.get("row")),
            w=_as_cell(raw.get("w"), default=1),
            h=_as_cell(raw.get("h"), default=1),
        )


def cells_out(miss_mm: float) -> str:
    """A miss in the grid's own vocabulary, for the bar's caption.

    Magnitude only — deliberately **not** the mockup's "ONE CELL SHORT". Short
    and long are relative to the path the cue was meant to take, which nothing
    here knows; the direction is on the cloth instead, drawn exactly, as the
    miss line from the ball to the edge it missed. So the caption says how big
    and the picture says which way, and neither one guesses.
    """
    # Rounded, not floored: on a 2:1 table an 8x4 grid has square cells
    # (241.25 mm here), and a 214 mm miss is a cell in every way that matters
    # at the table. Flooring would call it "inside one cell", which reads as
    # close when it is a whole diamond.
    count = round(miss_mm / min(CELL_LENGTH_MM, CELL_WIDTH_MM))
    if count == 0:
        return "INSIDE ONE CELL"
    return "ONE CELL OUT" if count == 1 else f"{count} CELLS OUT"


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
    # Where a game may keep **definitions** on disk. Never a score: attempts
    # live in the journal, which is what makes abandoning a game lossless and
    # what lets `cueclock report` and the phone agree by construction.
    store_dir: Path | None = None
    # Per-shot (attempts, made, last played) derived from the journals. A
    # callable rather than a value so a game reads it when it needs it and a
    # server with no log directory simply has no history.
    history: Callable[[], dict[str, tuple[int, int, str | None]]] = dict


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

    @property
    def transient(self) -> bool:
        """Does this game own the whole deck row right now?

        True while a transient page is open — the capture flow, which borrows
        the row the way Flag's reason page borrows its cells. The platform's
        BACK key steps aside for it: leaving games mode halfway through
        capturing a shot is not a thing anybody means to do with one press.
        """
        return False

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        """The game has been picked. The table is whatever it is."""

    def stop(self) -> None:
        """Games mode left, or another game picked. Nothing to save."""

    def reload(self) -> None:
        """Definitions this game reads changed underneath it (the phone edited
        the catalog). Re-read them; keep whatever is on the table."""

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

    def press(self, verb: str, balls: Sequence[Ball]) -> bool:
        """Handle one verb press. False = this game has no such verb.

        ``balls`` is the layout at the instant of the press rather than the
        last tick's, because a capture snapshot is exactly "the table as it is
        now" and 100 ms of staleness is 100 ms of somebody's hand.
        """
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
# The shot catalog
# ---------------------------------------------------------------------------
#
# Capture by **placing, not configuring**. The table is a millimetre-accurate
# input device and nothing is drawn on a phone: set the shot up, snapshot it,
# then pick each ball up and put it where the shot should leave it. The system
# attributes each placement by **which ghost was vacated**, so there is nothing
# to tell it.
#
# Placement rather than demonstration is deliberate: these are shots being
# practised *because* they don't come off, so a demonstration would rarely land
# in its own target.

CUE_ROLE = "cue"


def role_label(role: str, object_count: int) -> str:
    """The face a role wears in the bar.

    `CUE` / `OBJECT` / `BALL n` — never a colour. A colour name is an identity
    claim, and #46 puts 16-ball identity out of scope on every surface; the
    three tiers it does specify can name a role but never a colour.
    """
    if role == CUE_ROLE:
        return "CUE"
    if object_count <= 1:
        return "OBJECT"
    return f"BALL {role.removeprefix('obj')}"


@dataclass
class Shot:
    """One saved practice shot: a layout, and a list of requirements.

    The criteria is a **list** of (role → zone) from day one even while most
    shots carry exactly one entry, because retrofitting a list under a
    hardcoded cue judge later is pointless churn (#46). Every shot requires the
    cue in its zone; a **safety** adds the object ball in its own, which is the
    full discipline of a safety — hiding the cue *and* leaving the object ball
    where intended.
    """

    id: str
    name: str
    layout: list[tuple[str, float, float]]  # (role, x_mm, y_mm) — the ghosts
    requirements: list[tuple[str, Zone]]
    attempts: int = 0
    made: int = 0

    @property
    def safety(self) -> bool:
        return any(role != CUE_ROLE for role, _ in self.requirements)

    @property
    def object_count(self) -> int:
        return sum(1 for role, _, _ in self.layout if role != CUE_ROLE)

    last_played: str | None = None

    @property
    def make_pct(self) -> int | None:
        return None if not self.attempts else round(100 * self.made / self.attempts)

    def state_dict(self) -> dict[str, object]:
        """What goes on disk: **definitions only**.

        Not `attempts`/`made`/`last_played` — those are derived from the
        journal (`report.shot_records`), which is what makes them survive a
        deleted file, agree with `cueclock report` by construction, and be
        recomputable over every session ever recorded.
        """
        return {
            "id": self.id,
            "name": self.name,
            "layout": [{"role": r, "x_mm": x, "y_mm": y} for r, x, y in self.layout],
            "requirements": [{"role": r, "zone": z.as_dict()} for r, z in self.requirements],
        }

    @classmethod
    def from_state(cls, data: Mapping[str, object]) -> Shot:
        layout = data.get("layout")
        requirements = data.get("requirements")
        return cls(
            id=str(data["id"]),
            name=str(data.get("name") or data["id"]),
            layout=[
                (str(e["role"]), float(e["x_mm"]), float(e["y_mm"]))
                for e in (layout if isinstance(layout, list) else [])
            ],
            requirements=[
                (str(e["role"]), Zone.from_dict(e["zone"]))
                for e in (requirements if isinstance(requirements, list) else [])
            ],
        )

    def as_dict(self) -> dict[str, object]:
        """The phone's view: the definition plus the journal's verdict on it."""
        return {
            **self.state_dict(),
            "safety": self.safety,
            "balls": len(self.layout),
            "attempts": self.attempts,
            "made": self.made,
            "make_pct": self.make_pct,
            "last_played": self.last_played,
        }

    def zones(self) -> tuple[Mark, ...]:
        return tuple(_zone_mark(zone, role) for role, zone in self.requirements)

    def ghosts(self) -> tuple[Mark, ...]:
        return tuple(
            Circle(x_mm=x, y_mm=y, role=CUE_ROLE if role == CUE_ROLE else "object")
            for role, x, y in self.layout
        )


class ShotStore:
    """The catalog on disk: **one JSON per shot, definitions only**.

    The bracket's persistence pattern, at the scale it deserves — a directory
    of small files, no index to maintain and nothing that can get out of step
    with itself. Deleting a shot is deleting a file; a torn one is skipped
    rather than taking the catalog down with it.

    With no directory (a bare in-memory server, or a test) it is a working
    catalog that simply forgets on restart, so nothing above it has to branch
    on whether persistence exists.
    """

    def __init__(self, directory: Path | None) -> None:
        self.directory = directory
        self._memory: dict[str, Shot] = {}

    def all(self) -> list[Shot]:
        """Every saved shot, oldest id first — which is capture order."""
        if self.directory is None:
            return sorted(self._memory.values(), key=lambda s: _shot_number(s.id))
        shots: list[Shot] = []
        for path in sorted(self.directory.glob("shot-*.json")):
            try:
                shots.append(Shot.from_state(json.loads(path.read_text())))
            except (OSError, ValueError, KeyError, TypeError):
                continue  # a torn or future-typed file must not hide the rest
        return sorted(shots, key=lambda s: _shot_number(s.id))

    def save(self, shot: Shot) -> None:
        if self.directory is None:
            self._memory[shot.id] = shot
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{shot.id}.json"
        # The temp-file rename `MatchScore` and the bracket use: never leave a
        # torn file where the loader looks.
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(shot.state_dict(), indent=2) + "\n")
        tmp.replace(path)

    def delete(self, shot_id: str) -> bool:
        if self.directory is None:
            return self._memory.pop(shot_id, None) is not None
        path = self.directory / f"{Path(shot_id).name}.json"
        if not path.is_file():
            return False
        path.unlink()
        return True

    def next_id(self) -> tuple[str, str]:
        """The next free `(id, auto-name)` — the number the SAVE key promises.

        The lowest unused number rather than a count, so deleting shot 3 and
        capturing again reuses 3 instead of leaving a hole and a name that
        counts something else.
        """
        used = {_shot_number(shot.id) for shot in self.all()}
        n = 1
        while n in used:
            n += 1
        return f"shot-{n}", f"Shot {n}"


def _shot_number(shot_id: str) -> int:
    tail = shot_id.removeprefix("shot-")
    return int(tail) if tail.isdigit() else 0


def _zone_mark(zone: Zone, role: str) -> Rect:
    x0, y0, x1, y1 = zone.bounds_mm
    return Rect(
        x_mm=x0,
        y_mm=y0,
        w_mm=x1 - x0,
        h_mm=y1 - y0,
        role=CUE_ROLE if role == CUE_ROLE else "object",
        dashed=role != CUE_ROLE,
    )


def _match_roles(
    layout: Sequence[tuple[str, float, float]], balls: Sequence[Ball]
) -> dict[str, tuple[Ball, float]]:
    """Greedy nearest-neighbour: which ball is standing on which ghost, and how far.

    The same globally-sorted greedy the tracker's own association uses, for the
    same reason: at these separations the assignment is unambiguous, and taking
    the shortest pair first resolves two adjacent ghosts correctly.
    """
    pairs = sorted(
        (hypot(ball.x_mm - x, ball.y_mm - y), role, index)
        for role, x, y in layout
        for index, ball in enumerate(balls)
    )
    taken_roles: set[str] = set()
    taken_balls: set[int] = set()
    matched: dict[str, tuple[Ball, float]] = {}
    for distance, role, index in pairs:
        if role in taken_roles or index in taken_balls:
            continue
        taken_roles.add(role)
        taken_balls.add(index)
        matched[role] = (balls[index], distance)
    return matched


class ShotsGame(Game):
    """Your saved shots, served back at you, and the table judging the leave.

    Four states and one transient page:

        idle      nothing served. PLAY serves; CAPTURE opens the page.
        serving   ghosts up, per-ball offsets counting down. **The shot arms
                  itself** when every ball is inside tolerance and the table is
                  still — no press, because the press would be one more thing
                  to remember while bent over a layout you are rebuilding.
        armed     the ghosts drop (the balls are standing on them, so from here
                  they would only be noise under the shot); the zone stays lit
                  the whole way, because it is the thing being aimed at.
        verdict   PASS/FAIL, per-requirement misses in millimetres, and the
                  miss line drawn on the cloth from ball to zone edge.

    RETRY re-shows the ghosts and the loop restarts; NEXT serves another.
    """

    name = "shots"
    title = "SHOTS"
    description = (
        "Your saved shots, served back at you. Place the balls on the ghosts, "
        "shoot, and the table judges where everything finishes."
    )

    IDLE = "idle"
    SERVING = "serving"
    ARMED = "armed"
    VERDICT = "verdict"
    CAPTURING = "capturing"

    def __init__(self, ctx: GameCtx) -> None:
        super().__init__(ctx)
        self.store = ShotStore(None if ctx.store_dir is None else ctx.store_dir / "shots")
        self.shots: list[Shot] = []
        self.state = self.IDLE
        self.served: Shot | None = None
        self.offsets: dict[str, float] = {}
        self.verdict: list[tuple[str, float | None]] = []  # (role, miss_mm | None)
        self.passed = False
        self._armed_ids: dict[str, int] = {}
        self._rest_mm: dict[str, tuple[float, float]] = {}  # where each role came to rest
        # capture state
        self._snapshot: list[tuple[str, float, float]] = []
        self._targets: dict[str, Zone] = {}
        self._anchors: dict[str, tuple[int, int]] = {}
        self._last_placed: str | None = None
        self._resume: str = self.IDLE  # what CANCEL puts back on the wall

    # -- lifecycle --------------------------------------------------------

    @property
    def transient(self) -> bool:
        return self.state == self.CAPTURING

    def start(self) -> None:
        self.reload()

    def stop(self) -> None:
        self._cancel_capture()

    def reload(self) -> None:
        """Re-read the catalog, and re-derive every shot's record from the journal.

        Called when the game starts and whenever the phone edits the catalog
        underneath it. Definitions come off disk; **attempts, makes and last
        played come from the journal**, so a shot renamed from the couch
        mid-session keeps its history and a shot whose file was deleted takes
        none of it with it.
        """
        history = self.ctx.history()
        served = None if self.served is None else self.served.id
        self.shots = self.store.all()
        for shot in self.shots:
            shot.attempts, shot.made, shot.last_played = history.get(shot.id, (0, 0, None))
        # Keep serving the same shot across an edit — its zones may have just
        # moved, which is exactly the thing you would want to try immediately.
        self.served = next((s for s in self.shots if s.id == served), None)
        if self.served is None and self.state in (self.SERVING, self.ARMED, self.VERDICT):
            self.state = self.IDLE

    # -- the deck row -----------------------------------------------------

    def verbs(self) -> tuple[Verb, ...]:
        if self.state == self.CAPTURING:
            w, h = self._zone_size()
            nxt = ZONE_SIZES[(ZONE_SIZES.index((w, h)) + 1) % len(ZONE_SIZES)]
            armed = CUE_ROLE in self._targets
            return (
                Verb(name="cancel", text="CANCEL"),
                # A cycle key: current value bright over next value dim, so
                # both are on the face and neither reads as the other.
                Verb(
                    name="zone",
                    text=size_face(w, h),
                    sub=size_face(*nxt),
                    enabled=self._last_placed is not None,
                ),
                Verb(name="_gap1", text="", enabled=False),
                Verb(name="_gap2", text="", enabled=False),
                # The second line names what a press writes — rule 3's other
                # legitimate job. Dark until the cue's target exists.
                Verb(
                    name="save",
                    text="SAVE",
                    sub=self._next_name().upper(),
                    enabled=armed,
                    armed=armed,
                ),
            )
        # PLAY and NEXT are ONE key with two faces (the Replay-key precedent):
        # with uniform-random serving they are the same act, and the face says
        # which press this is. RETRY is separate on purpose — re-serving the
        # SAME shot and serving ANOTHER must never be one mispress apart.
        return (
            Verb(name="capture", text="CAPTURE"),
            Verb(
                name="play",
                text="PLAY" if self.served is None else "NEXT",
                enabled=bool(self.shots),
            ),
            Verb(name="retry", text="RETRY", enabled=self.served is not None),
        )

    def press(self, verb: str, balls: Sequence[Ball]) -> bool:
        if verb == "capture":
            return self._begin_capture(balls)
        if verb == "cancel":
            self._cancel_capture()
            return True
        if verb == "zone":
            return self._cycle_zone()
        if verb == "save":
            return self._save()
        if verb == "play":
            return self._serve(self._pick())
        if verb == "retry":
            return self.served is not None and self._serve(self.served)
        return False

    # -- capture ----------------------------------------------------------

    def _begin_capture(self, balls: Sequence[Ball]) -> bool:
        """Snapshot the settled layout. The crown names the cue; the rest are
        object balls in a stable left-to-right order, so a role means the same
        thing every time this layout is read back."""
        if self.state == self.CAPTURING or not balls:
            return False
        cue = next((b for b in balls if b.is_cue), None)
        others = sorted(
            (b for b in balls if b is not cue), key=lambda b: (round(b.x_mm), round(b.y_mm))
        )
        snapshot: list[tuple[str, float, float]] = []
        if cue is not None:
            snapshot.append((CUE_ROLE, cue.x_mm, cue.y_mm))
        snapshot.extend((f"obj{i}", b.x_mm, b.y_mm) for i, b in enumerate(others, start=1))
        self._snapshot = snapshot
        self._targets = {}
        self._anchors = {}
        self._last_placed = None
        self._resume = self.state
        self.state = self.CAPTURING
        return True

    def _cancel_capture(self) -> None:
        """Discard the capture and put back whatever was on the wall before it."""
        self._snapshot = []
        self._targets = {}
        self._anchors = {}
        self._last_placed = None
        if self.state == self.CAPTURING:
            self.state = self._resume

    def _zone_size(self) -> tuple[int, int]:
        zone = self._targets.get(self._last_placed or "")
        return (1, 1) if zone is None else (zone.w, zone.h)

    def _cycle_zone(self) -> bool:
        """Size the **most recently placed** target — no selection to make."""
        role = self._last_placed
        if role is None or role not in self._targets:
            return False
        size = ZONE_SIZES[(ZONE_SIZES.index(self._zone_size()) + 1) % len(ZONE_SIZES)]
        self._targets[role] = self._targets[role].resized(*size, anchor=self._anchors[role])
        return True

    def _next_name(self) -> str:
        return self.store.next_id()[1]

    def _save(self) -> bool:
        if CUE_ROLE not in self._targets:
            return False
        # Requirements ride in layout order, so the cue's is always first and
        # the bar's lockups read left to right the way the roles were assigned.
        requirements = [
            (role, self._targets[role]) for role, _, _ in self._snapshot if role in self._targets
        ]
        shot_id, name = self.store.next_id()
        shot = Shot(
            id=shot_id,
            name=name,
            layout=list(self._snapshot),
            requirements=requirements,
        )
        self.store.save(shot)
        self.shots.append(shot)
        self._cancel_capture()
        self._serve(shot)  # saving loads it, so the loop starts without a press
        return True

    # -- serving and arming -----------------------------------------------

    def _pick(self) -> Shot | None:
        """Which shot PLAY serves: **uniform random**, deliberately.

        Weighting toward the shots you miss is the obvious next move and is out
        of scope until the catalog is populated enough for the weights to mean
        anything (#46) — the journal is already recording everything that
        weighting would need, so it costs nothing to wait.

        A single-shot catalog serves that shot; otherwise the one currently
        served is excluded, so PLAY twice never lands on the same shot twice
        and reads as a broken key.
        """
        pool = [s for s in self.shots if s is not self.served] or self.shots
        # Which practice shot comes next is not a secret.
        return random.choice(pool) if pool else None  # nosec B311

    def serve_id(self, shot_id: str) -> bool:
        """Serve one named shot — the phone's "Serve this shot"."""
        return self._serve(next((s for s in self.shots if s.id == shot_id), None))

    # -- the catalog (the phone's Drills tab) ------------------------------
    #
    # Rename, re-size, re-place a zone, delete. **Never** ball or ghost
    # positions: the table is the only positional input device, and a map you
    # can drag balls around on is a second, worse one that would disagree with
    # the millimetres the camera measured.

    def edit(
        self, shot_id: str, name: str | None, zones: Mapping[str, object] | None
    ) -> Shot | None:
        shot = next((s for s in self.shots if s.id == shot_id), None)
        if shot is None:
            return None
        if name is not None and name.strip():
            shot.name = name.strip()[:60]
        for role, raw in (zones or {}).items():
            if not isinstance(raw, Mapping):
                continue
            shot.requirements = [
                (r, Zone.from_dict(raw) if r == role else z) for r, z in shot.requirements
            ]
        self.store.save(shot)
        return shot

    def remove(self, shot_id: str) -> bool:
        if not self.store.delete(shot_id):
            return False
        self.reload()
        return True

    def _serve(self, shot: Shot | None) -> bool:
        if shot is None:
            return False
        self.served = shot
        self.state = self.SERVING
        self.offsets = {}
        self.verdict = []
        self.passed = False
        self._armed_ids = {}
        return True

    def tick(self, balls: Sequence[Ball], moving: bool) -> None:
        if self.state == self.CAPTURING:
            self._watch_placements(balls, moving)
        elif self.state == self.SERVING:
            self._watch_setup(balls, moving)

    def _watch_placements(self, balls: Sequence[Ball], moving: bool) -> None:
        """Attribute a placement by **which ghost was vacated**.

        A ball is on its ghost, or it is somewhere else. Balls that have
        already been placed are accounted for against the targets they set, so
        picking up a second ball for a safety leaves exactly one vacated ghost
        and exactly one unaccounted ball — and there is nothing to tell the
        system. Anything more ambiguous than that is left alone rather than
        guessed at.

        Re-placing the same ball in a **different** cell re-anchors its target
        at 1x1; putting it down in the same cell does not, which is what lets
        the ZONE key's resize survive the hand that is still hovering.
        """
        if moving or not self._snapshot:
            return
        loose = list(balls)
        vacant: list[str] = []
        for role, sx, sy in self._snapshot:
            on_spot = min(
                (b for b in loose if hypot(b.x_mm - sx, b.y_mm - sy) <= PLACEMENT_TOLERANCE_MM),
                key=lambda b, sx=sx, sy=sy: hypot(b.x_mm - sx, b.y_mm - sy),  # type: ignore[misc]
                default=None,
            )
            if on_spot is None:
                vacant.append(role)
            else:
                loose.remove(on_spot)
        for role in list(vacant):
            zone = self._targets.get(role)
            placed = (
                None
                if zone is None
                else next((b for b in loose if zone.contains(b.x_mm, b.y_mm)), None)
            )
            if placed is not None:
                loose.remove(placed)
                vacant.remove(role)
        if len(vacant) != 1 or len(loose) != 1:
            return
        role, ball = vacant[0], loose[0]
        anchor = cell_of(ball.x_mm, ball.y_mm)
        if self._anchors.get(role) == anchor:
            return  # same cell: the target stands, and so does its size
        self._anchors[role] = anchor
        self._targets[role] = Zone(col=anchor[0], row=anchor[1])
        self._last_placed = role

    def _watch_setup(self, balls: Sequence[Ball], moving: bool) -> None:
        """Count each ball down to its ghost, and arm when they are all there.

        Arming needs the table **still** as well as placed, and stillness here
        is the vision layer's identity-free motion bit — no roster machinery,
        so nothing it could be lying about. A hand still lowering the last ball
        keeps the bit high and the shot unarmed.
        """
        shot = self.served
        if shot is None:
            return
        matched = _match_roles(shot.layout, balls)
        self.offsets = {
            role: matched[role][1] if role in matched else float("inf")
            for role, _, _ in shot.layout
        }
        if moving or any(d > PLACEMENT_TOLERANCE_MM for d in self.offsets.values()):
            return
        self._armed_ids = {role: matched[role][0].ball_id for role in self.offsets}
        self.state = self.ARMED
        self.ctx.log(DRILL_ARMED, game=self.name, shot_id=shot.id, balls=len(shot.layout))

    # -- judging ----------------------------------------------------------

    def on_settle(self, t: float, balls: Sequence[Ball]) -> None:
        if self.state != self.ARMED or self.served is None:
            return
        shot = self.served
        resting = self._resting_roles(shot, balls)
        verdict: list[tuple[str, float | None]] = []
        for role, zone in shot.requirements:
            ball = resting.get(role)
            # No ball for a role is a failed requirement, not an unjudgeable
            # one: for the cue that is a scratch (it fails its zone, which is
            # exactly right), and for an object ball it is a pot, which has
            # equally left the zone it was asked to stay in.
            verdict.append((role, None if ball is None else zone.miss_mm(ball.x_mm, ball.y_mm)))
        self.verdict = verdict
        self.passed = all(miss == 0.0 for _, miss in verdict)
        self._rest_mm = {role: (ball.x_mm, ball.y_mm) for role, ball in resting.items()}
        self.state = self.VERDICT
        shot.attempts += 1
        shot.made += int(self.passed)
        self.ctx.log(
            DRILL_ATTEMPT,
            game=self.name,
            shot_id=shot.id,
            **{"pass": self.passed},
            misses=[
                {"role": role, "miss_mm": None if miss is None else round(miss)}
                for role, miss in verdict
            ],
        )

    def _resting_roles(self, shot: Shot, balls: Sequence[Ball]) -> dict[str, Ball]:
        """Which resting ball is which role — the three tiers, weakest last.

        1. **The crown.** The cue is judged against the whiteness crown at rest,
           never against track lineage, so whatever the tracks did mid-collision
           the cue's verdict is re-derived visually at the settle.
        2. **Elimination.** With exactly one object ball on the table, it is the
           resting ball not wearing the crown. No lineage, zero identity risk.
        3. **Lineage**, and only then: the track that was standing on that ghost
           when the shot armed. The exposure is narrower than it looks — a
           cue/object swap is always corrected by tier 1 — but two object balls
           passing within merge distance can still corrupt it, which is a
           Flag-and-triage item like any other detection defect.
        """
        roles: dict[str, Ball] = {}
        crowned = [b for b in balls if b.is_cue]
        if len(crowned) == 1:
            roles[CUE_ROLE] = crowned[0]
        others = [b for b in balls if b not in crowned]
        object_roles = [role for role, _, _ in shot.layout if role != CUE_ROLE]
        if len(object_roles) == 1 and len(others) == 1:
            roles[object_roles[0]] = others[0]
            return roles
        by_id = {b.ball_id: b for b in others}
        remaining = list(others)
        for role in object_roles:
            ball = by_id.get(self._armed_ids.get(role, -1))
            if ball is not None and ball in remaining:
                roles[role] = ball
                remaining.remove(ball)
        return roles

    # -- the surfaces -----------------------------------------------------

    def marks(self) -> tuple[Mark, ...]:
        grid: tuple[Mark, ...] = (Grid(cols=GRID_COLS, rows=GRID_ROWS),)
        if self.state == self.CAPTURING:
            # A snapshot spot is a solid ring while its ball is still on it and
            # a dashed ghost once that ball has been taken away to set a
            # target, so the cloth says at a glance which balls are done.
            rings = tuple(
                Circle(
                    x_mm=x,
                    y_mm=y,
                    role=CUE_ROLE if role == CUE_ROLE else "object",
                    dashed=role in self._targets,
                )
                for role, x, y in self._snapshot
            )
            zones = tuple(_zone_mark(z, role) for role, z in self._targets.items())
            return (*grid, *zones, *rings)
        shot = self.served
        if shot is None:
            return grid
        if self.state == self.SERVING:
            return (*grid, *shot.zones(), *shot.ghosts())
        if self.state == self.ARMED:
            return (*grid, *shot.zones())  # ghosts drop; the zone stays lit
        return (*grid, *shot.zones(), *self._miss_lines(shot))

    def _miss_lines(self, shot: Shot) -> tuple[Mark, ...]:
        """One dashed alarm line per failed requirement, ball to zone edge.

        The one place alarm red touches the cloth, and the only thing that says
        which *way* the shot missed — the bar's caption says how big, in cells,
        and deliberately does not guess at a direction (see `cells_out`).
        """
        lines: list[Mark] = []
        for (role, zone), (_, miss) in zip(shot.requirements, self.verdict, strict=True):
            at = self._rest_mm.get(role)
            if miss is None or miss == 0.0 or at is None:
                continue
            nx, ny = zone.nearest_mm(*at)
            # From the ball's **edge**, not its centre: a miss of one ball
            # radius or less is entirely underneath the ball, and a line drawn
            # from the centre is a line nobody can see at the exact moment it
            # is closest. Under a radius there is no room for a line at all,
            # and the millimetres in the bar say it instead.
            span = hypot(nx - at[0], ny - at[1])
            if span <= BALL_RADIUS_MM:
                continue
            f = BALL_RADIUS_MM / span
            lines.append(
                Line(
                    x1_mm=at[0] + (nx - at[0]) * f,
                    y1_mm=at[1] + (ny - at[1]) * f,
                    x2_mm=nx,
                    y2_mm=ny,
                )
            )
        return tuple(lines)

    def hud(self) -> Hud:
        if self.state == self.CAPTURING:
            return self._capture_hud()
        shot = self.served
        if shot is None:
            return Hud(
                name="Shots",
                context="Nothing saved yet" if not self.shots else f"{len(self.shots)} saved",
                answer=Answer(word="CAPTURE" if not self.shots else "PLAY", small=True),
            )
        name = f"{shot.name} · Safety" if shot.safety else shot.name
        pct = shot.make_pct
        context = f"Attempt {shot.attempts + 1}" + (f" · Make {pct}%" if pct is not None else "")
        if self.state == self.SERVING:
            return Hud(
                name=name,
                context=context,
                stats=tuple(
                    _offset_stat(role, shot.object_count, self.offsets.get(role, float("inf")))
                    for role, _, _ in shot.layout
                ),
                answer=Answer(word="PLACE", small=True),
            )
        if self.state == self.ARMED:
            return Hud(
                name=name,
                context=context,
                stats=(
                    Stat(
                        label="REQUIRE",
                        value=" · ".join(
                            f"{role_label(role, shot.object_count)} IN ZONE"
                            for role, _ in shot.requirements
                        ),
                    ),
                ),
                answer=Answer(word="ARMED"),
            )
        return Hud(
            name=name,
            context=f"Attempt {shot.attempts}"
            + (f" · Make {shot.make_pct}%" if shot.make_pct is not None else ""),
            stats=tuple(
                _verdict_stat(role, shot.object_count, miss) for role, miss in self.verdict
            ),
            answer=Answer(
                word="PASS" if self.passed else "FAIL", fill="pass" if self.passed else "fail"
            ),
        )

    def _capture_hud(self) -> Hud:
        saved = len(self._snapshot)
        if CUE_ROLE not in self._targets:
            prompt = (
                "PLACE THE CUE BALL AT ITS TARGET"
                if any(role == CUE_ROLE for role, _, _ in self._snapshot)
                else "NO CUE BALL IN THIS LAYOUT"
            )
            return Hud(
                name="New shot",
                context=f"{saved} balls saved",
                stats=(Stat(label="NEXT", value=prompt),),
                answer=Answer(word="CAPTURE", small=True),
            )
        w, h = self._zone_size()
        placed = role_label(self._last_placed or CUE_ROLE, len(self._snapshot) - 1)
        return Hud(
            name="New shot",
            context=f"{placed.title()} target set",
            stats=(Stat(label="ZONE", value=size_face(w, h), sub="ZONE KEY RESIZES"),),
            answer=Answer(word="SAVE SET", small=True),
        )

    def card(self, stats: GameStats) -> dict[str, object]:
        attempts = stats.numbers.get("attempts", 0.0)
        made = stats.numbers.get("made", 0.0)
        catalog = round(stats.numbers.get("catalog", 0.0))
        return {
            "stats": [
                {"label": "SHOTS", "value": str(catalog)},
                {"label": "MAKE", "value": "—"}
                if not attempts
                else {"label": "MAKE", "value": str(round(100 * made / attempts)), "unit": "%"},
                {"label": "BEST RUN", "value": str(round(stats.numbers.get("best_run", 0.0)))},
            ],
            "last_played": stats.last_played,
        }


def _offset_stat(role: str, object_count: int, distance: float) -> Stat:
    """One ball's distance to its ghost — collapsing to a quiet tick inside it."""
    label = role_label(role, object_count)
    if distance <= PLACEMENT_TOLERANCE_MM:
        return Stat(label=label, value="✓", ok=True)
    if distance == float("inf"):
        return Stat(label=label, value="—")
    return Stat(label=label, value=str(round(distance)), unit="MM")


def _verdict_stat(role: str, object_count: int, miss: float | None) -> Stat:
    label = role_label(role, object_count)
    if miss is None:
        # Off the table: a scratch for the cue, a pot for an object ball. Both
        # have left the zone they were asked to be in.
        return Stat(label=label, value="—", sub="OFF THE TABLE")
    if miss == 0.0:
        return Stat(label=label, value="✓", sub="IN ZONE", ok=True)
    return Stat(label=label, value=str(round(miss)), unit="MM", sub=cells_out(miss))


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------
#
# Deck order — the picker's keys and the TV's cards read in this order.

GAMES: dict[str, type[Game]] = {game.name: game for game in (ShotsGame, LagGame)}


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
        store_dir: Path | None = None,
        history: Callable[[], dict[str, tuple[int, int, str | None]]] = dict,
    ) -> None:
        self._log = log
        self._stats = stats
        self._store_dir = store_dir
        self._history = history
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
        game = factory(self._ctx(players))
        self.active = game
        self._log(GAME_STARTED, game=name, players=players)
        game.start()
        return True

    def _ctx(self, players: list[str] | None = None) -> GameCtx:
        return GameCtx(
            log=self._log,
            players=players or [],
            store_dir=self._store_dir,
            history=self._history,
        )

    def shots(self) -> ShotsGame:
        """A shot catalog to read, whatever mode the session is in.

        The phone's Drills tab has to work from the couch with a match on the
        wall, so it reads the same directory the game does rather than needing
        a live game to ask. Disk is the single source of truth; when a game
        *is* live, an edit tells it to :meth:`Game.reload`.
        """
        active = self.active
        if isinstance(active, ShotsGame):
            return active
        game = ShotsGame(self._ctx())
        game.reload()
        return game

    def reload_active(self) -> None:
        """The catalog changed under a live game — have it re-read."""
        if self.active is not None:
            self.active.reload()

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
        if verb == "back" and (self.active is None or not self.active.transient):
            self.back()
            return True
        return self.active is not None and self.active.press(verb, self.balls)

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
        if self.active.transient:
            return self.active.verbs()  # a transient page owns the whole row
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
