"""The ball list's say in the scratch hold (#63, docs/design.md §14) and in
ball in hand after a scratch (#64, §15).

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
# move that has to be caught is 8.6 px (`pot_scratch_nohold_0811`).
OBJECT_MOVED_PX = 3.0

# A cue that travelled this far was not carried by hand: the engine's own
# pickup bound (`EventConfig.pickup_travel_max_mm`, duplicated because the hub
# never imports the vision stack; a unit test keeps the two equal). The five
# recorded hand moves of the cue travel 18-288 mm, the five scratches 474-1711.
TRAVEL_MIN_MM = 400.0

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


def table_row(
    now: BallList, rest: BallList | None, cue_before: bool, placing: bool, call: str
) -> str:
    """What a settle was, for the journal.

    ``ball_in_hand`` (a settle during ball in hand that moved only the cue),
    ``shot`` (the cue before and now, an object ball moved), ``scratch`` and
    ``lifted`` (the list's calls, §14), ``cue_moved`` (only the cue moved,
    outside ball in hand: a hand or a stick that touched nothing), ``still``
    (nothing moved). ``arm`` (a hand over the table and no cue seen) and
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
    if not cue_before or not _cues(now):
        return "other"
    if object_moved(rest, now):
        return "shot"
    a, b = _cue(rest), _cue(now)
    if a is None or b is None:
        return "other"
    return "cue_moved" if math.hypot(a.cx - b.cx, a.cy - b.cy) > OBJECT_MOVED_PX else "still"
