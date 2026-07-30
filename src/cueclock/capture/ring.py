"""Rolling in-memory buffer of recent raw frames.

Backs the `/api/dump` endpoint (accuracy-ideas §8, stream-deck-plan build
item 1): when a live event disagrees with what actually happened on the
table, the last few seconds become a labeled corpus clip without stopping
the session — every miss turns into replayable evidence.
"""

from __future__ import annotations

import threading

import numpy as np
import numpy.typing as npt


class FrameRing:
    """Fixed-capacity ring of (timestamp_ns, Bayer frame); thread-safe.

    `push` copies the frame in (the camera recycles its grab buffers), and
    `snapshot` copies everything out oldest-first, so the returned frames
    stay valid while pushes continue. A snapshot holds the lock for the
    full copy (~1 s for a 12 s ring) — the grab loop stalls and drops
    frames for that moment, which a dump can afford.
    """

    def __init__(self, capacity: int, height: int, width: int) -> None:
        if capacity < 1:
            raise ValueError(f"capacity must be >= 1, got {capacity}")
        self._frames: npt.NDArray[np.uint8] = np.zeros((capacity, height, width), np.uint8)
        self._ts_ns: npt.NDArray[np.int64] = np.zeros(capacity, np.int64)
        self._count = 0  # total frames ever pushed
        self._lock = threading.Lock()

    def __len__(self) -> int:
        """Frames currently held (≤ capacity)."""
        return min(self._count, len(self._frames))

    def push(self, timestamp_ns: int, frame: npt.NDArray[np.uint8]) -> None:
        with self._lock:
            i = self._count % len(self._frames)
            self._frames[i] = frame
            self._ts_ns[i] = timestamp_ns
            self._count += 1

    def snapshot(self) -> list[tuple[int, npt.NDArray[np.uint8]]]:
        """Copies of the held frames, oldest first."""
        with self._lock:
            n = len(self)
            start = self._count - n
            return [
                (
                    int(self._ts_ns[(start + k) % len(self._frames)]),
                    self._frames[(start + k) % len(self._frames)].copy(),
                )
                for k in range(n)
            ]
