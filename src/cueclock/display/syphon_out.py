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

import threading
import time
import traceback
from collections.abc import Callable
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


class DisplayWorker:
    """Debayers, undistorts and publishes display frames on its own thread.

    At 60 fps the inline display branch starved the detect loop: one publish
    is ~10 ms of debayer + remap + Metal upload, and every millisecond of it
    blocked the grab loop — detection fell from ~77 Hz to ~51 Hz when `run
    --fps 60` published inline (measured 2026-08-07). Here the detect loop
    only hands over a copy of the raw Bayer frame (~0.3 ms); the heavy work
    happens on this thread, whose OpenCV/numpy calls release the GIL.

    Drop-oldest by design: only the newest offered frame is ever published,
    so a slow publish costs a display frame, never backpressure on detection.

    The SyphonOutput is constructed *inside* the thread: a Syphon server
    answers discovery on the runloop of the thread that created it (module
    docstring), so the server must live on the thread that pumps.
    """

    def __init__(
        self,
        to_rgba: Callable[[npt.NDArray[np.uint8]], npt.NDArray[np.uint8]],
        name: str,
        width: int,
        height: int,
    ) -> None:
        self._to_rgba = to_rgba
        self._name = name
        self._size = (width, height)
        self._cond = threading.Condition()
        self._latest: npt.NDArray[np.uint8] | None = None
        self._stop = False
        self._thread = threading.Thread(target=self._loop, name="display", daemon=True)

    def offer(self, bayer: npt.NDArray[np.uint8]) -> None:
        """Hand the newest frame over; copies, because pylon reuses the buffer."""
        frame = bayer.copy()
        with self._cond:
            self._latest = frame
            self._cond.notify()

    def _loop(self) -> None:
        try:
            with SyphonOutput(self._name, *self._size) as out:
                while True:
                    with self._cond:
                        while self._latest is None and not self._stop:
                            self._cond.wait()
                        if self._stop:
                            return
                        frame = self._latest
                        self._latest = None
                    if frame is None:
                        continue
                    out.publish(self._to_rgba(frame), pump_seconds=0.001)
        except Exception:  # a dead display must not kill detection
            print("display worker died — TV feed frozen, detection unaffected:", flush=True)
            traceback.print_exc()

    def __enter__(self) -> "DisplayWorker":
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        with self._cond:
            self._stop = True
            self._cond.notify()
        self._thread.join(timeout=5.0)
