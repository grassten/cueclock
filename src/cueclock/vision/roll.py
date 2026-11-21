"""How long the cue ball was seen rolling in the motion a settle ends (#67,
docs/design.md §21).

A ball that moved by itself rolls across the cloth in the open. A ball in a
hand never does: from overhead hand and ball are one blob, so the detector
reports a merged blob or nothing ball-shaped at all. So at every settle this
measures the time a *clean* cue-white detection stood somewhere the cue ball
was not resting, and the hub reads it as evidence: under `ROLL_MIN_S` the
cue ball was not seen rolling, so whatever moved it, it was not a stroke.

Measured on every settle of the corpus and the four logged sessions with play
(the research for #67 lives outside the repo; the issue holds the tables):
hand moves read 0.013 s at most, 0.006 s live; the lowest stick shot on
record is `tip_bump`, a 48 mm bump, at 0.19 s, and the lowest live shot
0.28 s.

Pure, like the rest of the vision core, and computed from each frame's
detections alone, so a session replay and the corpus harness reach the same
number from logged or cached detections with no pixels.
"""

from collections import deque
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

# A detection counts as the cue ball's when it is clean (merged with nothing)
# and this frame's own whiteness reaches this floor. At 0.60 a glove passes as
# white and a settle with no shot reads 0.134 s (session 20261121-153046 at
# 1582.6 s); 0.70 and 0.80 both separate every hand move from every stroke.
# The frame's own reading, never the track's slow average: the average counts
# 17 false frames inside one carry.
CUE_WHITE_MIN = 0.70

# A clean cue-white detection farther than this from every rest spot is the
# cue ball on its way. The split holds at 3, 6 and 10 mm.
REST_RADIUS_MM = 6.0

# How far before the motion start the window opens, as every measurement was
# run. Never earlier than the previous settle.
LOOKBACK_S = 1.0

# Where the cue ball stood: what clean cue-white detections showed in this
# long at the window's start and at the settle.
EDGE_S = 0.05

# Also a rest spot: anywhere clean cue-white detections stood (within
# REST_RADIUS_MM) for this long in total between the window's start and the
# motion start. That second is quiet table, so a ball seen there is resting,
# and it is how a cue ball hidden at the window's first frames still gets its
# spot (on 26 of 152 live shots nothing clean and white was there yet).
# Before the motion start only: over the whole window `tip_bump` reads zero,
# and at 0.25 s two live placings read 0.11 and 0.17 s.
STOOD_S = 0.10


@dataclass
class _Frame:
    whites: npt.NDArray[np.float64]  # (n, 2) clean cue-white positions, table mm
    moving: bool  # the engine's episode latch after this frame
    shot: bool  # a ShotTaken fired on this frame


class CueRollWitness:
    """Feed every frame in order; read :meth:`settle` at each settle.

    Keeps the frames since the previous settle, bounded: while no motion has
    started, only the last second or so (the window cannot reach further back
    than that); once one has, from a second before it, which is the window.
    """

    def __init__(self, cadence_hz: float) -> None:
        self._dt = 1.0 / cadence_hz
        self._lookback = int(LOOKBACK_S / self._dt)
        self._edge = max(2, int(EDGE_S / self._dt))
        self._stood = max(2, round(STOOD_S / self._dt))
        self._frames: deque[_Frame] = deque()
        self._base = 0  # absolute index of self._frames[0]
        self._prev = 0  # absolute index of the previous settle's frame
        self._first_shot: int | None = None  # first ShotTaken since the previous settle
        self._motion = False  # a latch frame or a ShotTaken since the previous settle

    def frame(self, whites: npt.NDArray[np.float64], moving: bool, shot: bool) -> None:
        """One frame: its clean cue-white positions, the latch after it, and
        whether a ShotTaken fired on it."""
        k = self._base + len(self._frames)
        self._frames.append(_Frame(whites.reshape(-1, 2), moving, shot))
        if shot and self._first_shot is None:
            self._first_shot = k
        if moving or shot:
            self._motion = True
        if not self._motion:
            # Quiet since the previous settle: the window will open at most
            # a lookback before the frame before the motion, which is this
            # frame or a later one; its edge reaches EDGE_S further.
            keep = self._lookback + self._edge + 2
            while len(self._frames) > keep:
                self._frames.popleft()
                self._base += 1

    def settle(self) -> float | None:
        """The seconds the cue ball was seen rolling in the motion the last
        frame's settle ends, then reset for the next one. None when no motion
        started since the previous settle (the pipeline's backstop): there is
        no motion to measure."""
        k = self._base + len(self._frames) - 1
        roll = self._measure(k) if self._motion else None
        self._prev = k
        self._first_shot = None
        self._motion = False
        # The next window may open on this frame; its edge reaches back EDGE_S.
        while len(self._frames) > self._edge + 1:
            self._frames.popleft()
            self._base += 1
        return roll

    def _at(self, k: int) -> _Frame:
        return self._frames[k - self._base]

    def _measure(self, k: int) -> float:
        prev = max(self._prev, self._base)
        # The motion start: the frame before the last run of the latch, or
        # the first strike since the previous settle if that came earlier.
        j = k
        while j > prev and not self._at(j).moving:
            j -= 1
        while j > prev and self._at(j).moving:
            j -= 1
        start = j if self._first_shot is None else min(j, self._first_shot)
        lo = max(prev, start - self._lookback)
        first = max(self._base, lo - self._edge)
        rest = [self._at(i).whites for i in range(first, min(lo + 2, k + 1))]
        rest += [self._at(i).whites for i in range(max(first, k - self._edge), k + 1)]
        rest += self._stood_spots(lo, start)
        spots = np.concatenate(rest) if rest else np.zeros((0, 2))
        rolling = 0
        for i in range(lo, k + 1):
            whites = self._at(i).whites
            if not len(whites):
                continue
            if not len(spots):
                rolling += 1
                continue
            d = np.hypot(
                whites[:, None, 0] - spots[None, :, 0], whites[:, None, 1] - spots[None, :, 1]
            )
            if (d.min(axis=1) > REST_RADIUS_MM).any():
                rolling += 1
        return rolling * self._dt

    def _stood_spots(self, lo: int, start: int) -> list[npt.NDArray[np.float64]]:
        """Spots clean cue-white detections occupied for STOOD_S or more,
        frames ``lo`` to ``start``: each spot is where it was first seen, a
        detection joins the first spot within REST_RADIUS_MM, and a spot
        counts once a frame."""
        spots: list[list[float]] = []  # x, y, frames seen
        for i in range(lo, start + 1):
            seen: set[int] = set()
            for x, y in self._at(i).whites:
                hit = next(
                    (
                        n
                        for n, s in enumerate(spots)
                        if np.hypot(s[0] - x, s[1] - y) <= REST_RADIUS_MM
                    ),
                    None,
                )
                if hit is None:
                    spots.append([float(x), float(y), 0.0])
                    hit = len(spots) - 1
                if hit not in seen:
                    seen.add(hit)
                    spots[hit][2] += 1
        stood = [(s[0], s[1]) for s in spots if s[2] >= self._stood]
        return [np.array(stood, dtype=np.float64).reshape(-1, 2)]
