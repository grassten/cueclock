"""Double-elimination bracket: the draw, the handicap, and where results go.

**The bracket is optional state layered on top of the score already kept**
(issue #43). ``MatchScore`` stays the single source of truth for what is on the
TV; a bracket hands it two players and two race targets and takes a result
back. It never keeps a score of its own — the loaded match's live scores are
read out of ``MatchScore`` at render time (``as_dict(live_scores=…)``), which is
why the bracket page and the score bar cannot disagree in front of everyone.

With no bracket loaded every page behaves exactly as it did before, which is
what makes the casual night and the tournament night the same software.

Pure and I/O-free apart from :meth:`Bracket.save` / :func:`load`, so the whole
structure — seeding, the cross-drop, byes, handicap races, result routing — is
unit-testable without a server.

Vocabulary is Steven's and is load-bearing in the UI: sides are **A** and **B**,
never winners/losers; match numbers are spelled out (``Match 8``); Match ``2B-1``
is the **second final**, never a "bracket reset".
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MIN_FIELD = 3  # a double-elimination draw needs three to mean anything
MAX_FIELD = 16  # what the bracket page renders without scrolling (§43 answer)

BYE = -1  # a resolved slot with nobody in it
_UNKNOWN = None  # a slot whose feeder match has not been played

SEEDING_MODES = ("adjacent", "standard")
HANDICAP_MODES = ("off", "mild", "medium", "hot")

DEFAULT_A_RACE = 5
DEFAULT_B_RACE = 4
MIN_RACE = 2  # nobody plays a race to one — the same instinct as control.html
MAX_RACE = 21

# Written as an escape rather than the glyph: an en dash beside a digit is
# exactly the character ruff's confusable rule exists to catch in source, and
# the score bar and the phone both want the typographic dash, not a hyphen.
_EN_DASH = "\u2013"


class BracketError(ValueError):
    """A request the bracket refuses — reported to the caller as a 4xx."""


# ---------------------------------------------------------------- handicap


def game_odds(rating_diff: float) -> float:
    """FargoRate's own game-win model: 100 points of rating is 2:1 in games.

    ``1 / (1 + 2**(-diff/100))`` — the published relationship (a 500 against a
    400 wins 2 games in 3). Everything else here is arithmetic on top of it, so
    the one number this file takes on faith is FargoRate's, not ours.
    """
    return float(1.0 / (1.0 + 2.0 ** (-rating_diff / 100.0)))


def _underdog_match_odds(strong_target: int, weak_target: int, p_game: float) -> float:
    """P(the weaker player reaches their target first), exactly.

    A race is a negative-binomial race: the stronger player wins the match by
    collecting ``strong_target`` games before the weaker collects
    ``weak_target``, so P(strong) is the sum over how many games they concede
    on the way. Exact, not simulated — the numbers are small.
    """
    q_game = 1.0 - p_game
    strong = sum(
        math.comb(strong_target - 1 + k, k) * p_game**strong_target * q_game**k
        for k in range(weak_target)
    )
    return 1.0 - strong


# **The target** is how close to even a mode tries to make the match, stated as
# the underdog's chance of winning it. FargoRate's charts are banded to hit a
# figure like this rather than to squeeze out every legal point: their published
# 7-to-4 band covers rating differences of 86-106 and is centred on 96, where
# the split is 55/45 — so the hot chart aims the underdog at about 45%, and the
# milder charts aim lower. Without a target the rule degenerates into "always
# take the biggest legal spot", which hands a 68-point gap the same 6-4 as a
# 100-point one, because at a race to five the ladder's rungs are coarse.
#
# 50% is a hard ceiling in every mode and never a target: FargoRate's stated
# rule is the closest match-up to the race you asked for "without giving the
# lower skill player an advantage".
_TARGET_ODDS = {"mild": 0.25, "medium": 0.40, "hot": 0.45}


def _strong_ceiling(base: int) -> int:
    """The longest race the favourite may be given, off a base of ``base``.

    Some ceiling is needed because the 50% rule alone does not bound anything:
    against a huge gap the underdog's chances keep creeping up as the favourite's
    race lengthens, so an uncapped search answers a family match with a race to
    twenty. FargoRate's charts all stop somewhere around a third above the race
    you asked for, and this reproduces every endpoint on record:

    * their base-3 league ladder ends at **5-2** (3 + 2)
    * their Fair Match race-to-5 column ends at **6-4**, and a 563 against a 240
      at that base lands on **7-2** — the example in issue #43 (5 + 2)
    * their Fair Match race-to-11 hot column ends at **14-8** (11 + 3)
    """
    return base + max(2, round(0.3 * base))


def _ladder(base: int) -> list[tuple[int, int]]:
    """Every race on offer at this base, evenly-matched first.

    The ladder shortens the underdog's race and lengthens the favourite's,
    alternating — 5-5, 5-4, 6-4, 6-3, 7-3, 7-2 — and once the underdog reaches
    the floor of two it can only keep lengthening the favourite's, which is what
    every published chart does at its extreme end (…, 5-2, 6-2, 7-2).
    """
    strong = weak = base
    rungs = [(strong, weak)]
    ceiling, shorten = _strong_ceiling(base), True
    while strong < ceiling or weak > MIN_RACE:
        if shorten and weak > MIN_RACE:
            weak -= 1
        elif strong < ceiling:
            strong += 1
        else:
            weak -= 1  # the favourite is capped; the spot can only widen below
        shorten = not shorten
        rungs.append((strong, weak))
    return rungs


def handicap_race(
    base: int, rating_a: int | None, rating_b: int | None, mode: str
) -> tuple[int, int]:
    """The two race targets for a match, in the players' own order.

    Returns ``(target_a, target_b)``. With handicapping off, an unrated player,
    or equal ratings, both race the side's base — there is nothing to spot.

    The rung chosen is the one whose odds land nearest the mode's target
    without crossing 50% — see :data:`_TARGET_ODDS` and :func:`_strong_ceiling`.
    The cost of the ceiling, worth knowing at the table: against a very large
    gap no rung on offer gets near the target, all three modes converge on the
    longest spot there is, and the match is still lopsided. There is nothing
    more to give without turning it into a different game.
    """
    if mode == "off" or rating_a is None or rating_b is None or rating_a == rating_b:
        return base, base
    p_game = game_odds(abs(rating_a - rating_b))
    target = _TARGET_ODDS[mode]
    strong, weak = base, base
    best = abs(_underdog_match_odds(base, base, p_game) - target)
    for cand_strong, cand_weak in _ladder(base)[1:]:
        odds = _underdog_match_odds(cand_strong, cand_weak, p_game)
        if odds > 0.5:
            break  # this rung hands the underdog the edge; so would every rung above
        if abs(odds - target) < best:
            strong, weak, best = cand_strong, cand_weak, abs(odds - target)
    return (strong, weak) if rating_a > rating_b else (weak, strong)


# ---------------------------------------------------------------- structure


@dataclass(frozen=True)
class Ref:
    """Where a match slot's player comes from.

    ``kind`` is ``"position"`` (a bracket position filled by the draw),
    ``"winner"`` or ``"loser"`` (of match ``value``). Every unfilled slot on the
    page is rendered from one of these, which is what keeps ``Winner of Match
    13`` generated rather than typed.
    """

    kind: str
    value: int

    def label(self) -> str:
        if self.kind == "winner":
            return f"Winner of Match {self.value}"
        if self.kind == "loser":
            return f"Loser of Match {self.value}"
        return "—"


@dataclass
class Match:
    id: int
    side: str  # "A" | "B" | "F" — the final and second final belong to neither
    round: int
    src: tuple[Ref, Ref]
    label: str | None = None  # "A Final", "B Final", "Final", "Second Final"
    conditional: bool = False  # the second final: drawn always, played sometimes
    slots: list[int | None] = field(default_factory=lambda: [_UNKNOWN, _UNKNOWN])
    scores: list[int] = field(default_factory=lambda: [0, 0])
    targets: list[int | None] = field(default_factory=lambda: [None, None])
    winner: int | None = None  # 0 or 1 once played

    @property
    def played(self) -> bool:
        return self.winner is not None

    @property
    def ready(self) -> bool:
        """Both players known and real — the only matches anyone can go play."""
        return all(isinstance(s, int) and s != BYE for s in self.slots)

    def title(self) -> str:
        return f"Match {self.id}" + (f" · {self.label}" if self.label else "")


def _slot_order(size: int) -> list[int]:
    """Standard bracket seed order: which seed sits in each bracket position.

    Built by the usual reflection — [1] → [1,2] → [1,4,2,3] → [1,8,4,5,2,7,3,6]
    — so seeds 1 and 2 can only meet in the final, seeds 1-4 only in the semis,
    and so on. Used for *placement* even under adjacent seeding, because where
    the byes sit is a structural question and this is the structure.
    """
    order = [1]
    while len(order) < size:
        span = len(order) * 2
        order = [x for seed in order for x in (seed, span + 1 - seed)]
    return order


def _structure(size: int) -> list[Match]:
    """Every match of a double-elimination draw of ``size`` (a power of two).

    Numbering is the issue's: the A side takes 1…size-1 in round order, the B
    side size…2·size-3, then the final and the conditional second final. For
    eight that is exactly the table in #43 — A rounds 1-4/5-6/7, B 8-9/10-11/
    12/13, final 14, second final 15.

    **The cross-drop is the load-bearing part.** Where losers drop from the A
    side into the B side, the incoming order is *reversed* against the B-side
    matches waiting for them, so a player never meets the half of the draw they
    just came from. Drop them straight down instead and the bracket still runs
    — it quietly replays first-round pairings, which is exactly the kind of bug
    nobody notices during a party.
    """
    rounds = size.bit_length() - 1
    matches: list[Match] = []
    next_id = 1

    a_rounds: list[list[int]] = []
    for r in range(1, rounds + 1):
        ids = list(range(next_id, next_id + (size >> r)))
        next_id += len(ids)
        for i, mid in enumerate(ids):
            if r == 1:
                src = (Ref("position", 2 * i), Ref("position", 2 * i + 1))
            else:
                prev = a_rounds[-1]
                src = (Ref("winner", prev[2 * i]), Ref("winner", prev[2 * i + 1]))
            matches.append(Match(mid, "A", r, src, label="A Final" if r == rounds else None))
        a_rounds.append(ids)

    # The B side alternates: odd rounds pair its own survivors, even rounds
    # take the fresh losers dropping out of the A side.
    b_rounds: list[list[int]] = []
    for j in range(1, 2 * rounds - 1):
        if j == 1:
            count = size // 4
        elif j % 2 == 0:
            count = len(b_rounds[-1])
        else:
            count = len(b_rounds[-1]) // 2
        ids = list(range(next_id, next_id + count))
        next_id += count
        for i, mid in enumerate(ids):
            if j == 1:
                src = (Ref("loser", a_rounds[0][2 * i]), Ref("loser", a_rounds[0][2 * i + 1]))
            elif j % 2 == 0:
                dropping = a_rounds[j // 2]  # A round j/2 + 1, zero-indexed
                src = (
                    Ref("winner", b_rounds[-1][i]),
                    Ref("loser", dropping[count - 1 - i]),  # the cross-drop
                )
            else:
                prev = b_rounds[-1]
                src = (Ref("winner", prev[2 * i]), Ref("winner", prev[2 * i + 1]))
            matches.append(
                Match(mid, "B", j, src, label="B Final" if j == 2 * rounds - 2 else None)
            )
        b_rounds.append(ids)

    a_final, b_final = a_rounds[-1][0], b_rounds[-1][0]
    finals_src = (Ref("winner", a_final), Ref("winner", b_final))
    matches.append(Match(next_id, "F", 1, finals_src, label="Final"))
    matches.append(Match(next_id + 1, "F", 2, finals_src, label="Second Final", conditional=True))
    return matches


def _draw(size: int, field_size: int, seeding: str) -> list[int | None]:
    """Which player (by seed index, 0-based) sits in each bracket position.

    Standard seeding pulls the top seeds apart (1v8, 4v5, …) so they meet last.
    **Adjacent** — Steven's ask, and the opposite — pairs 1v2, 3v4, 5v6 so every
    opening match is close: on the mockup's field the mean rating gap is 50
    adjacent against 187 standard, where three of four standard openers are
    blowouts. The cost, stated once here where the mode lives: the two strongest
    players meet in Round 1, so one of them spends the day on the B side and the
    A final is likely weaker than the B final. Chosen anyway, deliberately, for
    a family event — hence a mode and not a hardcode.

    **Byes go to the top seeds** in both modes. Under adjacent seeding that
    needs saying, because a bye rule and an adjacent draw can disagree about who
    sits out: seeds 1 and 2 are paired by the seeding rule but cannot both sit
    out of the same match. So the byes are placed first, into the bracket
    positions the standard order reserves for the top seeds (which spreads them
    across the halves), and only the *remaining* players are paired adjacently.
    The leftover pairs are then assigned strongest-first to the matches feeding
    the strongest byes, so Round 2 stays as close as the byes allow.
    """
    order = _slot_order(size)  # position -> seed number (1-based)
    positions: list[int | None] = [None] * size
    byes = size - field_size

    if seeding == "standard":
        for pos, seed in enumerate(order):
            positions[pos] = seed - 1 if seed <= field_size else None
        return positions

    # Round-1 matches ranked by the strongest seed the standard order puts in
    # them — that ranking is what "the top seeds sit out" means structurally.
    pairs = [(m, min(order[2 * m], order[2 * m + 1])) for m in range(size // 2)]
    by_strength = [m for m, _ in sorted(pairs, key=lambda item: item[1])]
    bye_matches = by_strength[:byes]
    for i, m in enumerate(bye_matches):
        # The bye sits in the position the standard order gives the better seed;
        # the other half of the match stays empty and resolves to BYE.
        top = 0 if order[2 * m] < order[2 * m + 1] else 1
        positions[2 * m + top] = i

    # Sibling here means "the other match feeding the same Round 2 match": a
    # leftover match whose sibling holds seed 1 is the one seed 1 will meet.
    def sibling_rank(m: int) -> int:
        sibling = m ^ 1
        return bye_matches.index(sibling) if sibling in bye_matches else size

    remaining = sorted(
        (m for m in range(size // 2) if m not in bye_matches),
        key=lambda m: (sibling_rank(m), by_strength.index(m)),
    )
    seed = byes  # the strongest player who actually plays Round 1
    for m in remaining:
        positions[2 * m], positions[2 * m + 1] = seed, seed + 1
        seed += 2
    return positions


# ---------------------------------------------------------------- the bracket


@dataclass
class Player:
    name: str
    rating: int | None = None  # the 0-800 Fargo scale, estimated or looked up
    established: bool = True  # FargoRate robustness >= 200; False shows as prov

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name, "rating": self.rating, "established": self.established}


@dataclass
class Bracket:
    name: str
    players: list[Player]
    a_race: int = DEFAULT_A_RACE
    b_race: int = DEFAULT_B_RACE
    handicap: str = "medium"
    seeding: str = "adjacent"
    matches: list[Match] = field(default_factory=list)
    positions: list[int | None] = field(default_factory=list)
    loaded: int | None = None  # the match currently in MatchScore
    revision: int = 0  # bumped on every change; the page refetches when it moves
    path: Path | None = None  # None = in-memory only (tests, bare dev servers)

    # ---------- construction ----------

    @classmethod
    def create(
        cls,
        name: str,
        players: list[Player],
        *,
        a_race: int = DEFAULT_A_RACE,
        b_race: int = DEFAULT_B_RACE,
        handicap: str = "medium",
        seeding: str = "adjacent",
        path: Path | None = None,
    ) -> Bracket:
        if not MIN_FIELD <= len(players) <= MAX_FIELD:
            raise BracketError(f"field must be {MIN_FIELD}-{MAX_FIELD} players")
        if handicap not in HANDICAP_MODES:
            raise BracketError(f"unknown handicap mode: {handicap}")
        if seeding not in SEEDING_MODES:
            raise BracketError(f"unknown seeding mode: {seeding}")
        for race in (a_race, b_race):
            if not MIN_RACE <= race <= MAX_RACE:
                raise BracketError(f"race must be {MIN_RACE}-{MAX_RACE}")
        # Seeding decides the draw and then disappears — no seed is ever shown,
        # so this sort is the only place the order exists. Unrated players seed
        # last; ties keep the order they were entered in (Python's sort is
        # stable), so re-building the same field twice gives the same draw.
        ranked = sorted(players, key=lambda p: -1 if p.rating is None else p.rating, reverse=True)
        size = 1 << max(2, (len(ranked) - 1).bit_length())
        bracket = cls(
            name=name.strip() or "Tournament",
            players=ranked,
            a_race=a_race,
            b_race=b_race,
            handicap=handicap,
            seeding=seeding,
            matches=_structure(size),
            positions=_draw(size, len(ranked), seeding),
            path=path,
        )
        bracket._resolve()
        bracket.save()
        return bracket

    @property
    def size(self) -> int:
        """Bracket size — the next power of two at or above the field."""
        return len(self.positions)

    @property
    def finished(self) -> bool:
        return self.champion() is not None

    def match(self, match_id: int) -> Match:
        for m in self.matches:
            if m.id == match_id:
                return m
        raise BracketError(f"no such match: {match_id}")

    # ---------- resolution ----------

    def _resolve(self) -> None:
        """Recompute every slot from the results on record, from scratch.

        Wholesale rather than incrementally, deliberately: a partial update is
        how a `Winner of Match N` reference goes stale after an edit, and the
        whole draw is a few dozen matches. Runs after every result, every edit
        and every load, so a stale reference has nowhere to survive.
        """
        for m in self.matches:
            m.slots = [_UNKNOWN, _UNKNOWN]
        # Ids ascend with dependency order on both sides, so one pass suffices.
        for m in sorted(self.matches, key=lambda m: m.id):
            for i, ref in enumerate(m.src):
                m.slots[i] = self._resolve_ref(ref)
            self._auto_advance(m)
            self._set_targets(m)
        self._clear_orphan_results()
        # A correction upstream can take the players out from under the loaded
        # match. Nothing crashes if it does — every reader handles an unknown
        # slot — but the wall would keep naming a match that no longer has two
        # players in it, so it stops being the loaded one.
        if self.loaded is not None and not self.match(self.loaded).ready:
            self.loaded = None

    def _resolve_ref(self, ref: Ref) -> int | None:
        if ref.kind == "position":
            player = self.positions[ref.value] if ref.value < len(self.positions) else None
            return BYE if player is None else player
        feeder = self.match(ref.value)
        if feeder.winner is None:
            return _UNKNOWN
        slot = feeder.winner if ref.kind == "winner" else 1 - feeder.winner
        return feeder.slots[slot]

    def _auto_advance(self, m: Match) -> None:
        """A match nobody has to play: one real player against a bye.

        Byes cascade — the B-side match waiting on that match's loser receives a
        bye in turn — so this is the same rule applied down the chain rather
        than a special case per round.
        """
        if m.played or m.conditional:
            return
        left, right = m.slots
        if left == BYE and right == BYE:
            m.winner = 0  # nobody advances; the slot below reads BYE too
        elif left == BYE and isinstance(right, int):
            m.winner = 1
        elif right == BYE and isinstance(left, int):
            m.winner = 0

    def _clear_orphan_results(self) -> None:
        """Drop results whose players no longer exist (an edit re-drew them)."""
        changed = True
        while changed:
            changed = False
            for m in self.matches:
                if m.winner is not None and not m.ready and BYE not in m.slots:
                    m.winner, m.scores = None, [0, 0]
                    changed = True
            if changed:
                for m in sorted(self.matches, key=lambda m: m.id):
                    for i, ref in enumerate(m.src):
                        m.slots[i] = self._resolve_ref(ref)
                    self._auto_advance(m)

    def base_race(self, m: Match) -> int:
        """The side's configured race. Both finals play the A-side length —
        the A-side finalist has not lost yet, and shortening the title match
        because the B side's races are shorter would be the wrong answer."""
        return self.b_race if m.side == "B" else self.a_race

    def _set_targets(self, m: Match) -> None:
        base = self.base_race(m)
        if not m.ready:
            m.targets = [None, None]
            return
        left, right = (self.players[s] for s in m.slots)  # type: ignore[index]
        m.targets = list(handicap_race(base, left.rating, right.rating, self.handicap))

    # ---------- mutation ----------

    def _touch(self) -> None:
        self.revision += 1
        self.save()

    def load(self, match_id: int) -> Match:
        """Make ``match_id`` the match the score keys are keeping."""
        m = self.match(match_id)
        if not m.ready:
            raise BracketError(f"Match {m.id} does not have both players yet")
        if m.conditional and not self._second_final_needed():
            raise BracketError("the second final is only played if the B side wins the final")
        self.loaded = m.id
        self._touch()
        return m

    def unload(self) -> None:
        """Nothing is loaded — the tournament is over, or between matches."""
        self.loaded = None
        self._touch()

    def _second_final_needed(self) -> bool:
        """True once the B-side player has won the final.

        The A-side finalist arrives undefeated, so losing the final is their
        *first* loss and the two play again for the title. If the A-side player
        wins it the tournament ends there and the second final is never played.
        """
        final, second = self.matches[-2], self.matches[-1]
        if final.winner is None:
            return False
        return final.src[final.winner].value == second.src[1].value  # the B-side feeder

    def report(self, match_id: int, scores: tuple[int, int] | None = None) -> Match:
        """Record a result and route both players onward.

        With ``scores`` omitted the loaded match's live scores are the result —
        that is the Stream Deck's confirm press. Reopening a played match to fix
        a bad score is allowed while everything downstream is still unplayed;
        behind a played match it is **refused** by name, so a correction is
        walked back one match at a time and no real result is ever silently
        erased.
        """
        m = self.match(match_id)
        if m.conditional and not self._second_final_needed():
            raise BracketError("the second final is only played if the B side wins the final")
        if not m.ready:
            raise BracketError(f"Match {m.id} does not have both players yet")
        if scores is None:
            scores = (m.scores[0], m.scores[1])
        if scores[0] == scores[1]:
            raise BracketError("a match cannot end level")
        if m.played:
            self._refuse_if_downstream_played(m)
        m.scores = [max(0, int(scores[0])), max(0, int(scores[1]))]
        m.winner = 0 if m.scores[0] > m.scores[1] else 1
        self._resolve()
        self._touch()
        return m

    def clear_result(self, match_id: int) -> Match:
        """Un-play a match, so the one feeding it can be corrected.

        The other half of the refusal above: a correction is walked back one
        match at a time, and each step is a deliberate act. A walkover is not a
        result and cannot be cleared — the bye is a fact about the draw.
        """
        m = self.match(match_id)
        if not m.played:
            return m
        if self._is_bye_walkover(m):
            raise BracketError(f"Match {m.id} was a bye, not a result")
        self._refuse_if_downstream_played(m)
        m.winner, m.scores = None, [0, 0]
        if self.loaded == m.id:
            self.loaded = None
        self._resolve()
        self._touch()
        return m

    def _refuse_if_downstream_played(self, m: Match) -> None:
        blocker = self._played_downstream(m)
        if blocker is not None:
            raise BracketError(f"Match {blocker} has already been played — clear its result first")

    def _played_downstream(self, m: Match) -> int | None:
        """The first played match that depends on ``m``'s result, if any."""
        dependents = {m.id}
        for other in sorted(self.matches, key=lambda x: x.id):
            if other.id in dependents:
                continue
            fed = (ref.kind in ("winner", "loser") and ref.value in dependents for ref in other.src)
            if any(fed):
                if other.played and not self._is_bye_walkover(other):
                    return other.id
                dependents.add(other.id)
        return None

    @staticmethod
    def _is_bye_walkover(m: Match) -> bool:
        """A match "played" only because somebody had a bye — not a result."""
        return BYE in m.slots

    def edit(self, match_id: int, p1: int | None, p2: int | None) -> Match:
        """Swap who is in a Round-1 match; downstream references re-derive.

        Only the draw is editable. Slots fed by ``Winner of Match N`` are
        derived by definition, and letting one be typed over is how a bracket
        starts disagreeing with itself.
        """
        m = self.match(match_id)
        if any(ref.kind != "position" for ref in m.src):
            raise BracketError(f"Match {m.id} is fed by earlier matches — edit those instead")
        if m.played:
            self._refuse_if_downstream_played(m)
        for ref, value in zip(m.src, (p1, p2), strict=True):
            if value is not None and not 0 <= value < len(self.players):
                raise BracketError(f"no such player: {value}")
            # A player can only be in one place, so moving one into this match
            # vacates wherever they were.
            if value is not None:
                for pos, held in enumerate(self.positions):
                    if held == value:
                        self.positions[pos] = None
            self.positions[ref.value] = value
        m.winner, m.scores = None, [0, 0]
        self._resolve()
        self._touch()
        return m

    def set_targets(self, match_id: int, p1: int | None, p2: int | None) -> Match:
        """Override a match's race targets (the handicap chart is a default)."""
        m = self.match(match_id)
        for value in (p1, p2):
            if value is not None and not MIN_RACE <= value <= MAX_RACE:
                raise BracketError(f"race must be {MIN_RACE}-{MAX_RACE}")
        if p1 is not None:
            m.targets[0] = p1
        if p2 is not None:
            m.targets[1] = p2
        self._touch()
        return m

    # ---------- reading ----------

    def champion(self) -> str | None:
        final, second = self.matches[-2], self.matches[-1]
        decider = second if second.played else final
        if not decider.played or (decider is final and self._second_final_needed()):
            return None
        slot = decider.slots[decider.winner or 0]
        return self.players[slot].name if isinstance(slot, int) and slot != BYE else None

    def next_ready(self, *, after: int | None = None) -> Match | None:
        """The lowest-numbered match that can be played now.

        ``after`` skips a match that was just reported, so confirming a result
        never re-loads the match that was only this instant finished.
        """
        for m in sorted(self.matches, key=lambda m: m.id):
            if m.played or m.id == after or not m.ready:
                continue
            if m.conditional and not self._second_final_needed():
                continue
            return m
        return None

    def status(self, m: Match) -> str:
        if m.played and not self._is_bye_walkover(m):
            return "played"
        if m.conditional and not self._second_final_needed():
            return "conditional"
        if m.played:
            return "bye"
        if m.id == self.loaded:
            return "live"
        return "ready" if m.ready else "waiting"

    def _slot_dict(self, m: Match, i: int, live: tuple[int, int] | None) -> dict[str, object]:
        slot = m.slots[i]
        entry: dict[str, object] = {
            "name": None,
            "rating": None,
            "established": True,
            "ref": m.src[i].label(),
            "score": m.scores[i] if live is None else live[i],
            "target": m.targets[i],
            "bye": slot == BYE,
            "won": m.winner == i if m.played else None,
        }
        if isinstance(slot, int) and slot != BYE:
            player = self.players[slot]
            entry.update(
                name=player.name,
                rating=player.rating,
                established=player.established,
                index=slot,
            )
        elif slot == BYE:
            entry["name"] = "Bye"
        return entry

    def routes(self, m: Match) -> dict[str, int | None]:
        """Where this match's winner and loser go next — generated, never typed."""
        out: dict[str, int | None] = {"winner": None, "loser": None}
        for other in self.matches:
            for ref in other.src:
                if ref.value == m.id and ref.kind in out and out[ref.kind] is None:
                    # The second final shares the final's feeders; the first
                    # match that references us is the one to name.
                    if other.conditional:
                        continue
                    out[ref.kind] = other.id
        return out

    def as_dict(self, live_scores: tuple[int, int] | None = None) -> dict[str, object]:
        """Everything the bracket page draws. The loaded match's scores come
        from ``MatchScore`` (``live_scores``), never from here."""
        return {
            "name": self.name,
            "state": "finished" if self.finished else "live",
            "revision": self.revision,
            "size": self.size,
            "field": len(self.players),
            "a_race": self.a_race,
            "b_race": self.b_race,
            "handicap": self.handicap,
            "seeding": self.seeding,
            "loaded": self.loaded,
            "champion": self.champion(),
            "players": [p.as_dict() for p in self.players],
            "matches": [
                {
                    "id": m.id,
                    "side": m.side,
                    "round": m.round,
                    "label": m.label,
                    "title": m.title(),
                    "conditional": m.conditional,
                    "status": self.status(m),
                    "base_race": self.base_race(m),
                    "routes": self.routes(m),
                    "p1": self._slot_dict(m, 0, live_scores if m.id == self.loaded else None),
                    "p2": self._slot_dict(m, 1, live_scores if m.id == self.loaded else None),
                }
                for m in self.matches
            ],
        }

    def live_summary(self, live_scores: tuple[int, int] | None) -> dict[str, object]:
        """The compact object that rides every 10 Hz broadcast.

        The full bracket is a few kilobytes and changes a handful of times an
        evening, so it is fetched from ``GET /api/bracket`` and refetched when
        ``revision`` moves here — rather than re-serialized ten times a second
        for every connected page, forever.
        """
        summary: dict[str, object] = {
            "revision": self.revision,
            "state": "finished" if self.finished else "live",
            "name": self.name,
            "champion": self.champion(),
            "match": None,
            "armed": None,
        }
        if self.loaded is None:
            return summary
        m = self.match(self.loaded)
        scores = live_scores if live_scores is not None else (m.scores[0], m.scores[1])
        names = [self.players[s].name if isinstance(s, int) and s != BYE else "—" for s in m.slots]
        summary["match"] = {
            "id": m.id,
            "title": m.title(),
            "side": m.side,
            "header": self._header(m),
            "routes": self.routes(m),
            "targets": m.targets,
            "names": names,
        }
        # Reaching the target ARMS the result, it never fires it (§43 §7):
        # plus/minus is the easiest control to fat-finger, and one stray press
        # would otherwise corrupt a bracket someone has to unwind mid-party.
        # Derived rather than stored, so taking the point back clears the arm
        # by itself and there is no undo to find.
        for i in (0, 1):
            target = m.targets[i]
            if target is not None and scores[i] >= target and scores[i] > scores[1 - i]:
                summary["armed"] = {
                    "slot": i,
                    "name": names[i],
                    "score": scores[i],
                    "other": scores[1 - i],
                }
                break
        return summary

    def _header(self, m: Match) -> str:
        """``Match 4 · A Side · Race 5-4`` — the phone's match header.

        Handicapping makes the two targets differ, which is the whole reason
        the header carries both rather than one race-to number.
        """
        parts = [m.title()]
        if m.side in ("A", "B"):
            parts.append(f"{m.side} Side")
        left, right = m.targets
        if left is not None and right is not None:
            parts.append(f"Race {left}{_EN_DASH}{right}" if left != right else f"Race to {left}")
        return " · ".join(parts)

    # ---------- persistence ----------

    def state_dict(self) -> dict[str, object]:
        """The on-disk shape — results and the draw, not the derived slots."""
        return {
            "name": self.name,
            "a_race": self.a_race,
            "b_race": self.b_race,
            "handicap": self.handicap,
            "seeding": self.seeding,
            "loaded": self.loaded,
            "revision": self.revision,
            "players": [p.as_dict() for p in self.players],
            "positions": self.positions,
            "results": [
                {"id": m.id, "winner": m.winner, "scores": m.scores, "targets": m.targets}
                for m in self.matches
                if m.winner is not None or m.targets != [None, None]
            ],
        }

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # The atomic-enough temp-file rename MatchScore uses: never leave a torn
        # file where the loader looks. A tournament must survive a restart.
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state_dict(), indent=2) + "\n")
        tmp.replace(self.path)

    def archive(self, directory: Path) -> Path:
        """Write the bracket beside the journals so family results survive.

        A file, not a feature: no stats, no index, nothing that has to be
        maintained. `load_archive` reads one back.
        """
        directory.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", self.name.lower()).strip("-") or "bracket"
        path = directory / f"bracket-{slug}.json"
        n = 2
        while path.exists():
            path = directory / f"bracket-{slug}-{n}.json"
            n += 1
        path.write_text(json.dumps(self.state_dict(), indent=2) + "\n")
        return path


def from_state(data: dict[str, Any], path: Path | None = None) -> Bracket:
    """Rebuild a bracket from :meth:`Bracket.state_dict` output.

    The structure is regenerated rather than stored: it is a pure function of
    the field size, so persisting it would only create a way for a saved
    bracket and the generator to disagree.
    """
    players = [
        Player(
            name=str(p.get("name") or "?"),
            rating=int(p["rating"]) if p.get("rating") is not None else None,
            established=bool(p.get("established", True)),
        )
        for p in data.get("players", [])
    ]
    if not players:
        raise BracketError("no players in saved bracket")
    positions_raw = data.get("positions") or []
    positions = [None if p is None else int(p) for p in positions_raw]
    size = len(positions) or 1 << max(2, (len(players) - 1).bit_length())
    bracket = Bracket(
        name=str(data.get("name") or "Tournament"),
        players=players,
        a_race=int(data.get("a_race", DEFAULT_A_RACE)),
        b_race=int(data.get("b_race", DEFAULT_B_RACE)),
        handicap=str(data.get("handicap", "medium")),
        seeding=str(data.get("seeding", "adjacent")),
        matches=_structure(size),
        positions=positions or _draw(size, len(players), str(data.get("seeding", "adjacent"))),
        loaded=data.get("loaded") if isinstance(data.get("loaded"), int) else None,
        revision=int(data.get("revision", 0)),
        path=path,
    )
    by_id = {m.id: m for m in bracket.matches}
    for record in data.get("results", []):
        m = by_id.get(int(record.get("id", -1)))
        if m is None:
            continue
        winner = record.get("winner")
        m.winner = int(winner) if winner in (0, 1) else None
        scores = record.get("scores") or [0, 0]
        m.scores = [max(0, int(scores[0])), max(0, int(scores[1]))]
        targets = record.get("targets") or [None, None]
        m.targets = [int(t) if t is not None else None for t in targets]
    if bracket.loaded is not None and bracket.loaded not in by_id:
        # A file naming a match that isn't in this draw would otherwise make
        # every payload raise — a saved bracket must never be able to do that.
        bracket.loaded = None
    bracket._resolve()
    return bracket


def load(path: Path) -> Bracket | None:
    """The live bracket from disk, or None if there isn't a usable one.

    A missing or corrupt file means "no bracket", which is casual mode — the
    same failing-open answer ``MatchScore`` gives, and the right one: a bad file
    must never stop the clock from working.
    """
    try:
        return from_state(json.loads(path.read_text()), path=path)
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return None
