"""The ball list's say in the scratch hold (#63, docs/design.md §14), in
ball in hand after a scratch (#64, §15), in whether a settle moved anything
at all (§16) and in whether a break broke the rack (§17).

Today's logic raises a scratch candidate only when the tracker loses the cue
while it is moving inside a pocket zone. The ball list asks a different
question at every settle: was the cue on the table before the shot, and is it
gone now? This module answers it from the lists and the settle's evidence;
the hub owns what the answer does to the clock. Pure: no I/O, no clocks.

Everything here is read off lists the session log keeps, so a replay reaches
the same verdict live did.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Type-only, like the hub's other vision evidence: the list module drags
    # cv2/numpy, and the hub reads a list as plain attributes.
    from cueclock.vision.ball_list import BallList, ListedBall

# An object ball counts as moved when nothing stands within this many
# (distorted) pixels of it on the other list. At the 9 corpus settles where no
# object ball changed, the largest shift between lists is 1.4 px; the smallest
# move that has to be caught is 8.6 px (`pot_scratch_nohold_0811`). The cue
# ball is held to the same bound (`nothing_moved`): it reads 0-1 px from its
# spot when no shot was played and 27.5 px or more after every shot on record.
OBJECT_MOVED_PX = 3.0

# A cue that travelled this far was not carried by hand: the engine's own
# pickup bound (`EventConfig.pickup_travel_max_mm`, duplicated because the hub
# never imports the vision stack; a unit test keeps the two equal). The five
# recorded hand moves of the cue travel 18-288 mm, the five scratches 474-1711.
TRAVEL_MIN_MM = 400.0

# The fewest numbered balls a list must hold to stand for a rack: nine-ball's,
# the smallest rack in `clock/rulesets.py`.
RACK_MIN_BALLS = 9

# Written at-rest lists kept to find the one before a shot. Lists are written
# only when they change, about one a second at most, so this is minutes of
# reaching and aiming; the oldest one that matters is the last arm-free list.
REST_LISTS_KEPT = 128


@dataclass(frozen=True)
class SettleFacts:
    """What the hub knew at a settle, kept until that settle's list arrives."""

    t: float | None  # the settle frame's capture time
    epoch: int  # the motion episode it ended (`Hub._episode_epoch`)
    counted: bool  # the settle counted a shot
    travel_mm: float | None  # the farthest any mover went, None with no movers
    tracker_cue: bool  # the tracker held a cue-grade ball at the settle
    motion_t: float | None  # when the motion this settle ends began


@dataclass(frozen=True)
class Verdict:
    """The list's reading of one settle, every fact behind it journaled."""

    says: str  # "cue" | "no_cue" | "arm" (an arm over the table, no cue seen)
    before: str  # "cue" | "no_cue": was the cue on the table before the shot?
    object_moved: bool | None  # None when there was no arm-free list before
    call: str  # "scratch" | "lifted" | "none"

    def journal(self) -> dict[str, object]:
        return {"says": self.says, "before": self.before, "object_moved": self.object_moved}


def _cues(found: BallList) -> int:
    return sum(b.cue for b in found.balls)


def _objects(found: BallList) -> list[ListedBall]:
    return [b for b in found.balls if not b.cue]


def object_moved(before: BallList, now: BallList) -> bool:
    """Some object ball on one list has none within OBJECT_MOVED_PX on the other."""

    def covered(xs: list[ListedBall], ys: list[ListedBall]) -> bool:
        return all(
            any(math.hypot(x.cx - y.cx, x.cy - y.cy) <= OBJECT_MOVED_PX for y in ys) for x in xs
        )

    a, b = _objects(before), _objects(now)
    return not (covered(a, b) and covered(b, a))


class RestLists:
    """The written at-rest lists (settle and rest reads), oldest first."""

    def __init__(self) -> None:
        self._reads: deque[tuple[float, BallList]] = deque(maxlen=REST_LISTS_KEPT)

    def add(self, t: float, found: BallList) -> None:
        self._reads.append((t, found))

    def arm_free(self, since: float | None, before: float | None) -> BallList | None:
        """The newest arm-free list read at or after ``since`` and before
        ``before`` (None: no bound on that side)."""
        for read_t, found in reversed(self._reads):
            if before is not None and read_t >= before:
                continue
            if since is not None and read_t < since:
                return None
            if not found.arm:
                return found
        return None

    def last_before(self, t: float | None) -> BallList | None:
        """The newest list read before ``t``, an arm over the table or not."""
        for read_t, found in reversed(self._reads):
            if t is None or read_t < t:
                return found
        return None

    def before(self, t: float | None) -> tuple[BallList | None, bool]:
        """``rest`` — the newest arm-free list read before ``t`` — and whether
        the cue was on the table then: ``rest`` holds exactly one cue, or a
        later list (with an arm over the table) holds one. ``t`` None takes
        every list."""
        later_cue = False
        for read_t, found in reversed(self._reads):
            if t is not None and read_t >= t:
                continue
            if found.arm:
                later_cue = later_cue or _cues(found) > 0
                continue
            return found, later_cue or _cues(found) == 1
        return None, later_cue


def judge(
    now: BallList, rest: BallList | None, cue_before: bool, facts: SettleFacts, candidate: bool
) -> Verdict:
    """The list's verdict at a settle.

    ``scratch``: a shot was counted, the cue was on the table before it, the
    list (with no arm over the table) and the tracker both miss it now, and
    it was not lifted by hand — an object ball moved, something travelled
    TRAVEL_MIN_MM or more, or today's logic has a candidate open for this
    shot. ``lifted``: all of that but the last; #64's case, nothing here.
    When the list sees the cue (``says == "cue"``) the hub clears any
    candidate instead: the cue counts as present if either one sees it.
    """
    says = "cue" if _cues(now) else "arm" if now.arm else "no_cue"
    moved = None if rest is None else object_moved(rest, now)
    before = "cue" if cue_before else "no_cue"
    if says != "no_cue" or not facts.counted or not cue_before or facts.tracker_cue:
        call = "none"
    elif moved or candidate or (facts.travel_mm or 0.0) >= TRAVEL_MIN_MM:
        call = "scratch"
    else:
        call = "lifted"
    return Verdict(says=says, before=before, object_moved=moved, call=call)


def only_cue_changed(now: BallList, rest: BallList | None, facts: SettleFacts) -> bool:
    """Ball in hand (#64, §15): did this settle move nothing but the cue?

    Every object ball on ``rest`` (the newest arm-free list since the
    scratch) has one within OBJECT_MOVED_PX on ``now`` and the reverse, and
    nothing travelled TRAVEL_MIN_MM or more. The cue itself is not looked at:
    placed, lifted, or moved spot to spot, it is all placing. An arm over the
    table is allowed — a hand on the cue hides the cue, not the object balls;
    one hiding an object ball reads as moved, and the settle counts.
    """
    if rest is None or (facts.travel_mm or 0.0) >= TRAVEL_MIN_MM:
        return False
    return not object_moved(rest, now)


def _cue(found: BallList) -> ListedBall | None:
    cues = [b for b in found.balls if b.cue]
    return cues[0] if len(cues) == 1 else None


def cue_px(now: BallList, rest: BallList | None) -> float | None:
    """How far the cue ball stands from where it stood, in (distorted)
    pixels: from the one cue on ``now`` to the nearest cue on ``rest`` (the
    newest arm-free list before the motion). None when ``now`` does not hold
    exactly one cue, or ``rest`` holds none or is missing.

    The nearest of several, because a hand resting on the cloth can be read
    as a second cue ball (#73): the real one is still on that list.
    """
    cue = _cue(now)
    if cue is None or rest is None:
        return None
    return min(
        (math.hypot(cue.cx - b.cx, cue.cy - b.cy) for b in rest.balls if b.cue), default=None
    )


def nothing_moved(now: BallList, rest: BallList | None) -> bool:
    """Was no shot played before this settle (§16, §19)? The cue ball stands
    within OBJECT_MOVED_PX of where it stood before the motion.

    Every shot moves the cue ball, so the cue is the only ball asked about.
    Over the 114 settles of the first four logged sessions that can be
    measured, the cue stood 0-1 px from its spot at 8, none of them a shot,
    and 27.5 px or more at the other 106, every real shot among them. The
    object balls are not asked: a stick lying across one hides it, a hand is
    read as one, and either made §16's first form ("every ball read in
    place") count a shot nobody played (#73). No list to compare with, no
    cue on it, or anything but one cue now answers False, and the settle
    counts as before.

    This is what the tracker cannot say. A sleeve beside a ball swallows its
    blob, the track dies and the settle reports the ball vanished, the same
    report a pot makes; the list reads the pixels of the settle's own frame
    and finds the cue where it was (session 20261119-020755, 176 s).
    """
    px = cue_px(now, rest)
    return px is not None and px <= OBJECT_MOVED_PX


def holds_rack(found: BallList | None) -> bool:
    """Does this list hold enough numbered balls to stand for a rack?"""
    return found is not None and sum(b.number is not None for b in found.balls) >= RACK_MIN_BALLS


def rack_stands(now: BallList, before: BallList | None) -> bool:
    """Is the rack that stood before a strike still standing after it (§17)?

    ``before`` (the newest list read before the strike, an arm in it or not)
    holds a rack, and every numbered ball on it has a ball within
    OBJECT_MOVED_PX on ``now``. One way only, and the cue is not looked at:
    what is new on ``now`` (the cue put down somewhere else, a hand read as a
    ball) says nothing about the rack. A break moves the rack; a cue ball set
    down, rolled out of a hand or carried does not. Anything short of every
    ball read in place answers False, and the strike stands as the break it
    was taken for.
    """
    if before is None or not holds_rack(before):
        return False
    return all(
        any(math.hypot(b.cx - n.cx, b.cy - n.cy) <= OBJECT_MOVED_PX for n in now.balls)
        for b in before.balls
        if b.number is not None
    )


def table_row(
    now: BallList, rest: BallList | None, cue_before: bool, placing: bool, call: str
) -> str:
    """What a settle was, for the journal.

    ``ball_in_hand`` (a settle during ball in hand that moved only the cue),
    ``shot`` (the cue before and now, an object ball moved), ``scratch`` and
    ``lifted`` (the list's calls, §14), ``cue_moved`` (only the cue moved,
    outside ball in hand: a hand or a stick that touched nothing), ``still``
    (the cue ball on its spot: no shot was played, whatever else the list
    read, §19). ``arm`` (a hand over the table and no cue seen) and
    ``no_rest`` (nothing to compare with) cannot be read; ``other`` is
    anything else (no cue before or after, a cue the tracker still holds).
    """
    if placing:
        return "ball_in_hand"
    if call in ("scratch", "lifted"):
        return call
    if rest is None:
        return "no_rest"
    if now.arm and not _cues(now):
        return "arm"
    if nothing_moved(now, rest):
        return "still"
    if not cue_before or not _cues(now):
        return "other"
    if object_moved(rest, now):
        return "shot"
    if _cue(rest) is None or _cue(now) is None:
        return "other"
    return "cue_moved"
