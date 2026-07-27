"""Syphon frame publisher — the Python -> OBS handoff (macOS only).

All Syphon/PyObjC imports are lazy so this module imports cleanly on CI
(Linux); constructing SyphonOutput is what requires macOS.

Two integration facts learned the hard way (2026-07-27, syphon-python 0.1.1):

- A Syphon server answers directory-retrieve broadcasts on its process's
  runloop. Publish without pumping the runloop and the server streams
  invisibly — no client (including OBS) can discover it. `publish()`
  therefore pumps the runloop, and `pump_seconds` doubles as the frame-pacing
  sleep.
- Python-side *clients* of headless Python servers hit a syphon-python bug
  (KeyError on the missing icon key). OBS's native client is unaffected.
- The announce distributed notification is one-shot at server creation; if a
  long-lived directory (OBS runs one for its whole lifetime) misses it, this
  server stays invisible until the next system-wide announce-request round.
  OBS also re-binds a server whose app name + server name match on *every*
  announce it hears. `publish()` therefore re-broadcasts the announce every
  couple of seconds — restarting a publisher then reattaches in OBS within
  ~2 s, no trip to the source properties needed.
"""

import time
from typing import Any

import numpy as np
import numpy.typing as npt


class SyphonOutput:
    """Publishes RGBA frames as a named Syphon server for OBS's Syphon Client.

    Byte order is RGBA end-to-end as OBS sees it (confirmed eyes-on
    2026-07-27; BGRA renders red/blue swapped despite the BGRA8 texture tag).
    """

    def __init__(
        self,
        name: str = "cueclock",
        width: int = 1920,
        height: int = 1200,
        reannounce_seconds: float = 2.0,
    ) -> None:
        import syphon
        from Foundation import NSDate, NSDefaultRunLoopMode, NSRunLoop
        from syphon.utils.raw import create_mtl_texture

        self._nsdate: Any = NSDate
        self._mode: Any = NSDefaultRunLoopMode
        self._run_loop: Any = NSRunLoop.currentRunLoop()
        self._server: Any = syphon.SyphonMetalServer(name)
        self._texture: Any = create_mtl_texture(self._server.device, width, height)
        self._size = (height, width)
        self._reannounce_seconds = reannounce_seconds
        self._last_announce = time.monotonic()

    def publish(self, rgba: npt.NDArray[np.uint8], pump_seconds: float = 0.001) -> None:
        """Publish one frame; pumps the runloop for `pump_seconds` after.

        Callers pacing a fixed display rate should pass their per-frame time
        budget as `pump_seconds` — the pump then serves as the frame sleep.
        """
        from syphon.utils.numpy import copy_image_to_mtl_texture

        if rgba.shape != (*self._size, 4) or rgba.dtype != np.uint8:
            raise ValueError(f"expected {self._size} RGBA uint8, got {rgba.dtype} {rgba.shape}")
        if not rgba.flags["C_CONTIGUOUS"]:
            rgba = np.ascontiguousarray(rgba)
        copy_image_to_mtl_texture(rgba, self._texture)
        self._server.publish_frame_texture(self._texture)
        if (
            self._reannounce_seconds > 0
            and time.monotonic() - self._last_announce >= self._reannounce_seconds
        ):
            # private framework selector on SyphonServerBase; verified present
            # in the syphon-python 0.1.1 bundled framework
            self._server.context.broadcastServerAnnounce()
            self._last_announce = time.monotonic()
        self._run_loop.runMode_beforeDate_(
            self._mode, self._nsdate.dateWithTimeIntervalSinceNow_(pump_seconds)
        )

    def pump(self, seconds: float) -> None:
        """Keep the server discoverable while idle (no new frame to publish)."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self._run_loop.runMode_beforeDate_(
                self._mode, self._nsdate.dateWithTimeIntervalSinceNow_(0.05)
            )

    def close(self) -> None:
        self._server.stop()

    def __enter__(self) -> "SyphonOutput":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
