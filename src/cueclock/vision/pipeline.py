"""Full vision pipeline: raw Bayer frames + timestamps in → events out.

Chains the §4.1 stages — detect (distorted px) → calibrate coordinates
(undistort points + homography → table mm) → track → event logic — behind
one `process()` call. Pure like every stage it wraps: no I/O, no clocks,
no threads; replaying a recorded clip through it is deterministic.

The empty-table reference and the table mask come from the corpus's own
`empty_table` clip (or, live, from a capture taken at startup).

Warm replay: the pipeline's mutable state (tracker, event engine, previous
gray frame) can be captured with :meth:`VisionPipeline.dump_state` and
restored with :meth:`VisionPipeline.load_state` — `cueclock run` snapshots
it at ~1 Hz so a flag-key ring dump carries the live session's accumulated
state (whiteness-vote position, roster, episode latches) and
`analyze --warm` reproduces exactly what live computed instead of starting
cold. Still pure: bytes in/out, no file I/O.
"""

import hashlib
import pickle  # nosec B403 — state snapshots only; loaded blobs are our own dumps
from dataclasses import dataclass
from typing import cast

import cv2
import numpy as np
import numpy.typing as npt

from cueclock.display.undistort_fit import cloth_mask
from cueclock.vision.calibration import TableCalibration
from cueclock.vision.detect import (
    DetectConfig,
    Detection,
    ball_churn,
    bayer_to_gray,
    build_reference,
    detect_balls,
)
from cueclock.vision.events import BallState, Event, EventConfig, EventEngine
from cueclock.vision.track import BallTracker, Observation, TrackConfig


def build_table_mask(reference_bayer: npt.NDArray[np.uint8]) -> npt.NDArray[np.uint8]:
    """Cloth region of the empty table, dilated so cushion-hugging balls fit."""
    bgr = cv2.cvtColor(reference_bayer, cv2.COLOR_BayerBG2BGR_EA)
    mask = cloth_mask(cast(npt.NDArray[np.uint8], bgr))
    return cast(
        npt.NDArray[np.uint8],
        cv2.dilate(mask, np.ones((31, 31), np.uint8)),
    )


@dataclass(eq=False)
class ReferenceModel:
    """The three static arrays `detect_balls` consumes, built once per camera position.

    Bundled for two reasons. It costs ~0.30 s to build (median of 15 grays +
    the dilated cloth mask), which the corpus battery otherwise pays 51 times
    over; and it is the *complete* non-frame input to detection, so hashing
    these three arrays content-addresses everything upstream — the reference
    clip's pixels, how many frames were sampled and which, `build_reference`,
    `cloth_mask`, and the dilation. That is what makes a detection cache key
    provably complete instead of a list of source files someone must remember
    to extend.
    """

    gray: npt.NDArray[np.uint8]  # per-pixel median of the empty-table frames
    bayer: npt.NDArray[np.uint8]  # first sampled frame, for shadow rejection
    mask: npt.NDArray[np.uint8]  # dilated cloth region

    @classmethod
    def build(cls, reference_frames: list[npt.NDArray[np.uint8]]) -> "ReferenceModel":
        return cls(
            gray=build_reference(reference_frames),
            bayer=reference_frames[0],
            mask=build_table_mask(reference_frames[0]),
        )

    def digest(self) -> str:
        """Content hash of all three arrays (memoized — ~20 ms over 6.9 MB)."""
        if self._digest is None:
            h = hashlib.sha256()
            for array in (self.gray, self.bayer, self.mask):
                h.update(f"{array.shape}{array.dtype}".encode())
                h.update(array.tobytes())
            self._digest = h.hexdigest()
        return self._digest

    _digest: str | None = None


class VisionPipeline:
    """Feed frames in timestamp order; collect shot/settle/scratch events."""

    def __init__(
        self,
        calibration: TableCalibration,
        reference: list[npt.NDArray[np.uint8]] | ReferenceModel,
        detect_config: DetectConfig | None = None,
        track_config: TrackConfig | None = None,
        event_config: EventConfig | None = None,
    ) -> None:
        self._calibration = calibration
        self.reference = (
            reference if isinstance(reference, ReferenceModel) else ReferenceModel.build(reference)
        )
        self.detect_config = detect_config or DetectConfig()
        self._tracker = BallTracker(track_config)
        self._engine = EventEngine(event_config)
        self._prev_gray: npt.NDArray[np.uint8] | None = None
        self._advanced = 0
        self.last_balls: list[BallState] = []  # diagnostics for analyze/scoreboard

    def process(self, t: float, bayer: npt.NDArray[np.uint8]) -> list[Event]:
        detections, churn = self.detect(bayer)
        return self.advance(t, detections, churn)

    def detect(self, bayer: npt.NDArray[np.uint8]) -> tuple[list[Detection], list[float | None]]:
        """Frame-bound half: ball-like blobs plus each one's appearance churn.

        The expensive half (~5.9 ms/frame on the M2 against 0.08 ms for
        :meth:`advance`) and the half that does *not* change while event logic
        is being tuned — which is why it is separable and cacheable offline
        (`capture.detect_cache`). Stateful only through the previous frame's
        gray, which churn differences against.
        """
        detections = detect_balls(
            bayer,
            self.reference.gray,
            self.detect_config,
            self.reference.mask,
            self.reference.bayer,
        )
        gray = bayer_to_gray(bayer)
        churn = [
            None
            if self._prev_gray is None
            else ball_churn(gray, self._prev_gray, d.cx, d.cy, self.detect_config.churn_radius_px)
            for d in detections
        ]
        self._prev_gray = gray
        return detections, churn

    def advance(
        self, t: float, detections: list[Detection], churn: list[float | None]
    ) -> list[Event]:
        """State-bound half: pixels → table mm → tracker → events.

        Stateful only through the tracker and event engine, so a whole clip's
        :meth:`detect` results may be computed (or loaded) up front and fed
        here afterwards: the two halves share no state and interleaving them
        is not required.
        """
        observations: list[Observation] = []
        if detections:
            px = np.array([[d.cx, d.cy] for d in detections], dtype=np.float64)
            mm = self._calibration.to_table_mm(px)
            observations = [
                Observation(
                    x_mm=float(p[0]),
                    y_mm=float(p[1]),
                    whiteness=d.whiteness,
                    churn=c,
                    merged=d.merged,
                )
                for p, d, c in zip(mm, detections, churn, strict=True)
            ]
        self._advanced += 1
        balls = self._tracker.update(t, observations)
        self.last_balls = balls
        return self._engine.update(t, balls)

    def dump_state(self) -> bytes:
        """Serialize the mutable state for warm replay (`analyze --warm`).

        A whole-object pickle of the state-bearing stages — deliberately not
        a field-by-field schema, so state added to the tracker or engine
        later rides along automatically. The static stages (reference,
        mask, configs) are rebuilt by the caller from the same inputs the
        live session used. Restoring under *different* vision-core code is
        guarded by the fingerprint stamp in the dump's `state.json` sidecar
        (cli.py), not here.

        Refuses to snapshot a cache-fed pipeline: churn differences each frame
        against the previous one's gray, and frames replayed from
        `capture.detect_cache` never produce one. Such a snapshot would restore
        with ``prev_gray=None`` and silently mis-compute churn on the first
        frame after the resume — a wrong answer, not an error. Warm work
        replays real frames.
        """
        if self._advanced and self._prev_gray is None:
            raise RuntimeError(
                "dump_state() on a pipeline fed from a detection cache: prev_gray was never "
                "computed, so a warm resume would mis-compute churn on its first frame. "
                "Replay real frames (VisionPipeline.process) for anything that snapshots state."
            )
        return pickle.dumps(
            {
                "tracker": self._tracker,
                "engine": self._engine,
                "prev_gray": self._prev_gray,
                "last_balls": self.last_balls,
            },
            protocol=pickle.HIGHEST_PROTOCOL,
        )

    def load_state(self, blob: bytes) -> None:
        """Restore :meth:`dump_state` output onto an identically built pipeline."""
        state = pickle.loads(blob)  # nosec B301 — our own dump_state output
        self._tracker = state["tracker"]
        self._engine = state["engine"]
        self._prev_gray = state["prev_gray"]
        self.last_balls = state["last_balls"]
