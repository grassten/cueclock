"""Thin pypylon wrapper: production settings + timestamped Bayer frame stream.

The camera powers on with UserSet1 (saved 2026-07-27, docs/hardware.md
§A4). apply_production_settings() re-asserts the same values defensively so a
recording is correct even if the camera was left in another state. White-balance
ratios are deliberately NOT touched here — they were measured against a neutral
target and live in the camera (UserSet1).
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import numpy as np
import numpy.typing as npt
from pypylon import pylon

PIXEL_FORMAT = "BayerRG8"
EXPOSURE_US = 6000.0
GAIN = 12.0  # ≈x2 on this camera — its gain scale applies at half strength
FRAME_RATE = 160.0  # runs at 156.25 under the default link throughput limit
WIDTH = 1920
HEIGHT = 1200


def apply_production_settings(cam: Any) -> None:
    cam.PixelFormat.Value = PIXEL_FORMAT
    cam.ExposureAuto.Value = "Off"
    cam.ExposureTime.Value = EXPOSURE_US
    cam.GainAuto.Value = "Off"
    cam.Gain.Value = GAIN
    cam.BalanceWhiteAuto.Value = "Off"
    cam.AcquisitionFrameRateEnable.Value = True
    cam.AcquisitionFrameRate.Value = FRAME_RATE


@contextmanager
def open_camera() -> Iterator[Any]:
    """Open the first attached camera with production settings; close on exit."""
    cam = pylon.InstantCamera(pylon.TlFactory.GetInstance().CreateFirstDevice())
    cam.Open()
    try:
        apply_production_settings(cam)
        yield cam
    finally:
        cam.Close()


def stream(cam: Any, buffer_count: int = 128) -> Iterator[tuple[npt.NDArray[np.uint8], int]]:
    """Yield (Bayer frame, camera timestamp in ns) until the consumer stops.

    OneByOne grab strategy: frames queue in driver buffers, so brief consumer
    stalls delay delivery instead of dropping frames.
    """
    cam.MaxNumBuffer.Value = buffer_count
    cam.StartGrabbing(pylon.GrabStrategy_OneByOne)
    try:
        while cam.IsGrabbing():
            grab = cam.RetrieveResult(5000, pylon.TimeoutHandling_ThrowException)
            try:
                if grab.GrabSucceeded():
                    yield grab.Array.copy(), int(grab.GetTimeStamp())
            finally:
                grab.Release()
    finally:
        cam.StopGrabbing()
