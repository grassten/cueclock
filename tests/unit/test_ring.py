"""FrameRing: wraparound order, snapshot isolation, capacity accounting."""

import numpy as np
import pytest

from cueclock.capture.ring import FrameRing


def _frame(value: int) -> np.ndarray:
    return np.full((4, 6), value, np.uint8)


def test_fills_and_wraps_oldest_first() -> None:
    ring = FrameRing(capacity=3)
    assert len(ring) == 0
    for i in range(5):  # frames 0..4 into capacity 3 -> keeps 2, 3, 4
        ring.push(1000 + i, _frame(i))
    assert len(ring) == 3
    snap = ring.snapshot()
    assert [ts for ts, _ in snap] == [1002, 1003, 1004]
    assert [int(f[0, 0]) for _, f in snap] == [2, 3, 4]


def test_snapshot_is_isolated_from_later_pushes() -> None:
    ring = FrameRing(capacity=2)
    ring.push(1, _frame(7))
    snap = ring.snapshot()
    ring.push(2, _frame(9))
    ring.push(3, _frame(9))  # evicts frame 7 from the ring
    assert [ts for ts, _ in snap] == [1]
    assert int(snap[0][1][0, 0]) == 7


def test_empty_and_bad_capacity() -> None:
    ring = FrameRing(capacity=2)
    assert ring.snapshot() == []
    with pytest.raises(ValueError):
        FrameRing(capacity=0)
