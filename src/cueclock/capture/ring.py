"""Rolling in-memory buffer of recent raw frames.

Backs the `/api/dump` endpoint (deck plan build item 1): when a live event
disagrees with what actually happened on the table, the last few seconds
become a labeled corpus clip without stopping the session — every miss
turns into replayable evidence.
"""

from __future__ import annotations

import threading
from collections import deque

import numpy as np
import numpy.typing as npt


class FrameRing:
    """Fixed-capacity ring of (timestamp_ns, Bayer frame); thread-safe.

    Holds references, not copies: `push` takes ownership of the frame (the
    caller must never write to it again — pypylon's `grab.Array` is already a
    fresh copy of the camera's buffer), and `snapshot` returns the held
    references oldest-first. A frame is never mutated after the push, so a
    snapshot stays valid while pushes continue; later pushes only drop the
    ring's own reference. The lock is held for a list copy, not a pixel copy:
    at 156 Hz a 20 s ring is 7.2 GB, and copying that under the lock (as
    before checkpoint 106) would freeze detection for seconds on every Flag.
    """

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError(f"capacity must be >= 1, got {capacity}")
        self._frames: deque[tuple[int, npt.NDArray[np.uint8]]] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def __len__(self) -> int:
        """Frames currently held (≤ capacity)."""
        return len(self._frames)

    def push(self, timestamp_ns: int, frame: npt.NDArray[np.uint8]) -> None:
        with self._lock:
            self._frames.append((timestamp_ns, frame))

    def snapshot(self) -> list[tuple[int, npt.NDArray[np.uint8]]]:
        """The held frames, oldest first."""
        with self._lock:
            return list(self._frames)
