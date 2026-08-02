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

import pickle  # nosec B403 — state snapshots only; loaded blobs are our own dumps
from typing import cast

import cv2
import numpy as np
import numpy.typing as npt

from cueclock.display.undistort_fit import cloth_mask
from cueclock.vision.calibration import TableCalibration
from cueclock.vision.detect import (
    DetectConfig,
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


class VisionPipeline:
    """Feed frames in timestamp order; collect shot/settle/scratch events."""

    def __init__(
        self,
        calibration: TableCalibration,
        reference_frames: list[npt.NDArray[np.uint8]],
        detect_config: DetectConfig | None = None,
        track_config: TrackConfig | None = None,
        event_config: EventConfig | None = None,
    ) -> None:
        self._calibration = calibration
        self._reference = build_reference(reference_frames)
        self._reference_bayer = reference_frames[0]
        self._mask = build_table_mask(reference_frames[0])
        self._detect_cfg = detect_config or DetectConfig()
        self._tracker = BallTracker(track_config)
        self._engine = EventEngine(event_config)
        self._prev_gray: npt.NDArray[np.uint8] | None = None
        self.last_balls: list[BallState] = []  # diagnostics for analyze/scoreboard

    def process(self, t: float, bayer: npt.NDArray[np.uint8]) -> list[Event]:
        detections = detect_balls(
            bayer, self._reference, self._detect_cfg, self._mask, self._reference_bayer
        )
        gray = bayer_to_gray(bayer)
        observations: list[Observation] = []
        if detections:
            px = np.array([[d.cx, d.cy] for d in detections], dtype=np.float64)
            mm = self._calibration.to_table_mm(px)
            observations = [
                Observation(
                    x_mm=float(p[0]),
                    y_mm=float(p[1]),
                    whiteness=d.whiteness,
                    churn=None
                    if self._prev_gray is None
                    else ball_churn(
                        gray, self._prev_gray, d.cx, d.cy, self._detect_cfg.churn_radius_px
                    ),
                    merged=d.merged,
                )
                for p, d in zip(mm, detections, strict=True)
            ]
        self._prev_gray = gray
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
        """
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
