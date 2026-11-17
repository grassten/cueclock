"""The ball list in the live loop (#54, #65): when a list is read, which reads
are written, and the pixels kept for the ones read at rest.

`cueclock run` reads a list at every settle and about once a second while the
table is quiet. The reads run on the detect pool; :meth:`ListReads.drain`
consumes them here in the order they were asked for, so whether a list counts
as changed depends only on the frames, never on which worker finished first.

Written: every settle's list, and a list read at rest when it differs from the
last written one (a still table repeats itself). Only written lists go on to
the hub, so a replay sees what live saw. A written rest list also keeps its
frame as a ``rest`` still (#65, Steven's decision 1): a settle already keeps
one, and without the pixels a wrong list read at rest cannot be diagnosed.
The still goes through the session log's bounded queue like every other one,
so when the writer is behind it is dropped and counted, never waited for.
"""

from __future__ import annotations

import sys
import time
from collections import deque
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from datetime import UTC, datetime

from cueclock.capture.session_log import SessionLogWriter, StillFrame
from cueclock.vision.ball_list import BallList, same_table

REST_S = 1.0  # between lists read at rest


def still_frames(ring: Sequence[StillFrame], i: int, churn_lag: int) -> list[StillFrame]:
    """Ring frame ``i`` with its churn-lag predecessor, when the ring still
    holds it, so churn can be recomputed from a still."""
    return [ring[i - churn_lag], ring[i]] if i >= churn_lag else [ring[i]]


def quiet(churn: Sequence[float | None], ceiling: float) -> bool:
    """Nothing on the table is changing: every detection's appearance churn is
    at or under ``ceiling`` (the event engine's ``spin_churn_max``, the
    measured churn ceiling of a still ball).

    A rest read waits for it (#65). On 2026-11-17 a read landed 0.13 s after
    Steven's hand came loose from the rail in the segmentation and passed as
    one ball-shaped blob; the list fitted a ball in it, the extra 10. That
    blob's churn read 3.9-14.5 over the 0.2 s it lasted; no ball at any of the
    session's other rest reads or settles read over 2.3."""
    return all(c is None or c <= ceiling for c in churn)


class ListReads:
    """The run loop's ball-list reads, in order. ``submit`` reads one frame on
    the pool; ``on_written(found, frame, reason, tracker)`` is called for each
    written list, on the detect loop's thread."""

    def __init__(
        self,
        slog: SessionLogWriter,
        ring: Sequence[StillFrame],
        churn_lag: int,
        submit: Callable[[StillFrame], Future[BallList]],
        on_written: Callable[[BallList, StillFrame, str, int | None], None],
    ) -> None:
        self.next_rest = 0.0
        self._slog = slog
        self._ring = ring
        self._churn_lag = churn_lag
        self._submit = submit
        self._on_written = on_written
        self._jobs: deque[tuple[StillFrame, str, int | None, Future[BallList]]] = deque()
        self._last: BallList | None = None

    def read(self, reason: str, frame: StillFrame, tracker: int | None = None) -> None:
        """Ask for the list of ``frame``; the next rest read is due REST_S later."""
        self._jobs.append((frame, reason, tracker, self._submit(frame)))
        self.next_rest = frame.t + REST_S

    def drain(self) -> None:
        """Write each finished list that is a settle's or differs from the last."""
        while self._jobs and self._jobs[0][3].done():
            frame, reason, tracker, job = self._jobs.popleft()
            try:
                found = job.result()
            except Exception as exc:  # a fit bug must never take the clock down
                print(f"ball list failed at {frame.t:.3f}s: {exc!r}", file=sys.stderr)
                continue
            if reason == "rest" and self._last is not None and same_table(self._last, found):
                continue  # a still table repeats itself
            record = {
                "frame_i": frame.frame_i,
                "t": round(frame.t, 4),
                "reason": reason,
                "tracker": tracker,
                **found.as_dict(),
            }
            if not self._slog.ball_list(record):
                continue
            self._last = found
            if reason == "rest":
                self._keep(frame)
            self._on_written(found, frame, reason, tracker)

    def _keep(self, frame: StillFrame) -> None:
        """The rest still: the list's own frame, found in the ring by identity
        (a read takes at most ~0.1 s and the ring holds ~1 s), never by time."""
        i = next((k for k in range(len(self._ring) - 1, -1, -1) if self._ring[k] is frame), None)
        self._slog.still(
            "rest",
            mono=time.monotonic(),
            wall=datetime.now(UTC).isoformat(timespec="milliseconds"),
            t=frame.t,
            frame_i=frame.frame_i,
            frames=[frame] if i is None else still_frames(self._ring, i, self._churn_lag),
        )
