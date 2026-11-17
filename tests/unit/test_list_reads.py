"""The ball list in the live loop (#54, #65): which reads are written, and the
`rest` still each written rest list keeps. Synthetic lists, a real session log."""

import threading
from collections import deque
from concurrent.futures import Future
from pathlib import Path

import numpy as np

from cueclock.capture.list_reads import ListReads, quiet
from cueclock.capture.session_log import LoggedFrame, SessionLog, SessionLogWriter, StillFrame
from cueclock.vision.ball_list import BallList, ListedBall

CHURN_LAG = 2


def _list(*xs: float, arm: bool = False) -> BallList:
    return BallList(
        balls=tuple(
            ListedBall(
                cx=x,
                cy=100.0,
                r_pred=24.0,
                cue=False,
                number=3,
                rim=0.1,
                white=0.05,
                margin=12.0,
                colour_px=200,
                vote=5.0,
                coverage=0.9,
            )
            for x in xs
        ),
        arm=arm,
    )


class Loop:
    """The run loop's side: a ring of frames and the lists each read returns."""

    def __init__(self, writer: SessionLogWriter, lists: dict[int, BallList]) -> None:
        self.ring: deque[StillFrame] = deque(maxlen=50)
        self.lists = lists
        self.written: list[tuple[int, str]] = []
        self.reads = ListReads(writer, self.ring, CHURN_LAG, self._submit, self._written)

    def _submit(self, frame: StillFrame) -> "Future[BallList]":
        job: Future[BallList] = Future()
        job.set_result(self.lists[frame.frame_i])
        return job

    def _written(
        self, found: BallList, frame: StillFrame, reason: str, tracker: int | None
    ) -> None:
        self.written.append((frame.frame_i, reason))

    def frames(self, n: int) -> None:
        for i in range(n):
            bayer = np.full((8, 8), i, np.uint8)
            self.ring.append(StillFrame(100 + i, i / 156.25, bayer))

    def read(self, reason: str, frame_i: int) -> None:
        frame = next(f for f in self.ring if f.frame_i == frame_i)
        self.reads.read(reason, frame)
        self.reads.drain()


def test_a_written_rest_list_keeps_its_frame_and_an_unwritten_one_none(tmp_path: Path) -> None:
    writer = SessionLogWriter(tmp_path / "s", {"stamp": "s"})
    a, moved = _list(100.0, 300.0), _list(100.0, 340.0)
    loop = Loop(writer, {105: a, 110: a, 115: a, 120: moved})
    loop.frames(25)
    loop.read("settle", 105)  # a settle's list: its still is the settle's own
    loop.read("rest", 110)  # the same table again: not written
    loop.read("rest", 115)
    loop.read("rest", 120)  # a ball moved: written, with its pixels
    writer.close()

    assert loop.written == [(105, "settle"), (120, "rest")]
    log = SessionLog(tmp_path / "s")
    assert [r["frame_i"] for r in log.ball_lists()] == [105, 120]
    (still,) = log.stills()
    assert still["reason"] == "rest" and still["frame_i"] == 120
    # The list's own frame, with its churn-lag predecessor, as every still has.
    assert still["files"] == ["000000118.png", "000000120.png"]
    assert np.array_equal(log.still_frame("000000120.png"), np.full((8, 8), 20, np.uint8))


def test_a_rest_still_waits_for_nothing_when_the_writer_is_behind(tmp_path: Path) -> None:
    """Through the bounded queue like every still: dropped and counted. The
    queue has room for the list but not its still."""
    entered, release = threading.Event(), threading.Event()
    dropped: list[tuple[str, int]] = []
    writer = SessionLogWriter(
        tmp_path / "s",
        {"stamp": "s"},
        chunk_seconds=0.0,
        queue_items=2,
        on_drop=lambda what, n: dropped.append((what, n)),
    )
    real = writer._write_chunk

    def stalled(index: int, frames: list[LoggedFrame]) -> None:
        entered.set()
        release.wait(5.0)
        real(index, frames)

    writer._write_chunk = stalled  # type: ignore[method-assign]
    writer.frame(LoggedFrame(0, 0.0, 0.0, [], [], [], []))
    assert entered.wait(5.0)  # the writer is stuck on it
    writer.frame(LoggedFrame(1, 0.01, 0.01, [], [], [], []))  # one of two slots taken
    loop = Loop(writer, {104: _list(100.0)})
    loop.frames(10)
    loop.read("rest", 104)
    assert loop.written == [(104, "rest")]  # the list went in, its still could not
    assert writer.stats.dropped_stills == 1 and dropped == [("still", 1)]
    release.set()
    writer.close()


def test_a_rest_read_waits_for_a_quiet_table() -> None:
    assert quiet([0.4, None, 2.3], ceiling=2.5)
    assert quiet([], ceiling=2.5)
    assert not quiet([0.4, 9.8], ceiling=2.5)  # the hand that read as a 10
