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
from functools import partial
from typing import TYPE_CHECKING, Any

import numpy as np
import numpy.typing as npt

from cueclock.display.pipeline import remap_coordinates

if TYPE_CHECKING:
    from cueclock.display.pipeline import DisplayPipeline


# Rectify as a Metal compute pass (#53). `map` holds the source pixel each
# output pixel samples (pipeline.remap_coordinates, pixel centres at
# integers); coord::pixel puts centres at +0.5, and clamp_to_zero renders
# off-frame black, as cv2.remap's constant border does.
_WARP_SHADER = """
#include <metal_stdlib>
using namespace metal;
kernel void warp(texture2d<float, access::sample> src [[texture(0)]],
                 texture2d<float, access::read> map [[texture(1)]],
                 texture2d<float, access::write> dst [[texture(2)]],
                 uint2 gid [[thread_position_in_grid]]) {
    if (gid.x >= dst.get_width() || gid.y >= dst.get_height()) return;
    constexpr sampler s(coord::pixel, address::clamp_to_zero, filter::linear);
    float2 xy = map.read(gid).xy;
    float4 c = src.sample(s, xy + 0.5);
    dst.write(float4(c.rgb, 1.0), gid);
}
"""


class _GpuRemap:
    """The rectify on the GPU: debayered RGBA in shared memory -> warped texture.

    Why (#53, measured 2026-11-15): `cv2.remap` on eight threads cost ~12.5 ms
    of CPU per frame, about one full core at 60 fps, and its memory traffic
    beside OBS's 4K compositing slowed `find_blobs` on the performance cores
    from 9.7 to 17.9 ms. On the GPU the whole feed costs ~3.4 ms of CPU.

    The source texture is backed by a shared-storage buffer, so the CPU writes
    the debayered frame straight into GPU-visible memory and nothing is
    uploaded. The previous frame's command buffer is waited on before that
    write, or the GPU could sample a half-written frame.
    """

    def __init__(
        self,
        server: Any,
        coords: npt.NDArray[np.float32],
        src_width: int,
        src_height: int,
    ) -> None:
        import Metal

        device = server.device
        out_h, out_w = coords.shape[:2]
        descriptor = (
            Metal.MTLTextureDescriptor.texture2DDescriptorWithPixelFormat_width_height_mipmapped_
        )

        n = src_width * src_height * 4
        self._buffer: Any = device.newBufferWithLength_options_(
            n, Metal.MTLResourceStorageModeShared
        )
        d = descriptor(Metal.MTLPixelFormatRGBA8Unorm, src_width, src_height, False)
        d.setStorageMode_(Metal.MTLStorageModeShared)
        self._src: Any = self._buffer.newTextureWithDescriptor_offset_bytesPerRow_(
            d, 0, src_width * 4
        )
        if self._src is None:
            raise RuntimeError("device refused the buffer-backed source texture")
        self.source = np.frombuffer(self._buffer.contents().as_buffer(n), np.uint8).reshape(
            src_height, src_width, 4
        )

        d = descriptor(Metal.MTLPixelFormatRG32Float, out_w, out_h, False)
        self._map: Any = device.newTextureWithDescriptor_(d)
        coords = np.ascontiguousarray(coords)
        self._map.replaceRegion_mipmapLevel_withBytes_bytesPerRow_(
            Metal.MTLRegionMake2D(0, 0, out_w, out_h), 0, coords.tobytes(), out_w * 8
        )

        d = descriptor(Metal.MTLPixelFormatRGBA8Unorm, out_w, out_h, False)
        d.setUsage_(Metal.MTLTextureUsageShaderWrite | Metal.MTLTextureUsageShaderRead)
        self.output: Any = device.newTextureWithDescriptor_(d)

        library, err = device.newLibraryWithSource_options_error_(_WARP_SHADER, None, None)
        if library is None:
            raise RuntimeError(f"warp shader failed to compile: {err}")
        self._pipe, err = device.newComputePipelineStateWithFunction_error_(
            library.newFunctionWithName_("warp"), None
        )
        if self._pipe is None:
            raise RuntimeError(f"warp pipeline failed: {err}")
        self._groups = Metal.MTLSizeMake((out_w + 15) // 16, (out_h + 15) // 16, 1)
        self._group = Metal.MTLSizeMake(16, 16, 1)
        self._queue: Any = server.command_queue
        self._last: Any = None

    def begin(self) -> npt.NDArray[np.uint8]:
        """The source pixels to fill, once the GPU is done reading the last frame."""
        if self._last is not None:
            self._last.waitUntilCompleted()
        return self.source

    def encode(self) -> Any:
        """A command buffer holding the warp; the caller's publish commits it."""
        cb = self._queue.commandBuffer()
        enc = cb.computeCommandEncoder()
        enc.setComputePipelineState_(self._pipe)
        enc.setTexture_atIndex_(self._src, 0)
        enc.setTexture_atIndex_(self._map, 1)
        enc.setTexture_atIndex_(self.output, 2)
        enc.dispatchThreadgroups_threadsPerThreadgroup_(self._groups, self._group)
        enc.endEncoding()
        self._last = cb
        return cb


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
        self._gpu: _GpuRemap | None = None

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
        self._announce_and_pump(pump_seconds)

    def enable_gpu_remap(
        self, coords: npt.NDArray[np.float32], src_width: int = 1920, src_height: int = 1200
    ) -> None:
        """Rectify on the GPU from now on (`publish_remapped`); raises if Metal refuses."""
        if coords.shape[:2] != self._size:
            raise ValueError(f"coords {coords.shape[:2]} do not match output {self._size}")
        self._gpu = _GpuRemap(self._server, coords, src_width, src_height)

    def publish_remapped(
        self, fill: Callable[[npt.NDArray[np.uint8]], None], pump_seconds: float = 0.001
    ) -> None:
        """Publish one frame through the GPU rectify.

        `fill` writes the unwarped RGBA source frame into the array it is
        given, which is GPU-visible memory: nothing is copied or uploaded.
        """
        if self._gpu is None:
            raise RuntimeError("enable_gpu_remap() first")
        fill(self._gpu.begin())
        self._server.publish_frame_texture(self._gpu.output, command_buffer=self._gpu.encode())
        self._announce_and_pump(pump_seconds)

    def _announce_and_pump(self, pump_seconds: float) -> None:
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
        """Stop the server, but only once the GPU is done with its last frame.

        Syphon posts each frame to its clients from the Metal command buffer's
        completion handler, on a GPU notification thread, and that handler
        syncs onto the server's connection queue. `stop()` frees that queue.
        Stopping while the last frame's handler was still inside it aborted
        the whole process (2026-11-15, `python3.12-2026-11-14-203838.ips`:
        "BUG IN CLIENT OF LIBDISPATCH: Premature release of a locked queue"
        in `-[SyphonMetalServer stop]` while another thread sat in
        `-[SyphonServerConnectionManager publishNewFrame]`). `run` died before
        its shutdown path ran, so the session journal had no summary.

        The fence is an empty command buffer committed to the same queue. A
        queue completes its buffers in order and their completion
        notifications are delivered one at a time, so once the fence's
        handler has run, the previous frame's handler has returned.
        """
        done = threading.Event()
        fence = self._server.command_queue.commandBuffer()
        fence.addCompletedHandler_(lambda _buffer: done.set())
        fence.commit()
        done.wait(timeout=1.0)  # bounded: a wedged GPU must not hang shutdown
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

    def __init__(self, pipeline: "DisplayPipeline", name: str) -> None:
        self._pipeline = pipeline
        self._name = name
        self._size = pipeline.out_size
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
                gpu = self._start_gpu(out)
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
                    if gpu:
                        out.publish_remapped(
                            partial(self._pipeline.debayer_rgba, frame), pump_seconds=0.001
                        )
                    else:
                        out.publish(self._pipeline.to_rgba(frame), pump_seconds=0.001)
        except Exception:  # a dead display must not kill detection
            print("display worker died — TV feed frozen, detection unaffected:", flush=True)
            traceback.print_exc()

    def _start_gpu(self, out: SyphonOutput) -> bool:
        """GPU rectify when there is a warp to do; the CPU path if Metal refuses."""
        maps = self._pipeline.maps
        if maps is None:
            return False  # --no-undistort: nothing to warp, the CPU path is just a debayer
        try:
            out.enable_gpu_remap(remap_coordinates(*maps))
        except Exception as exc:
            print(f"display: GPU rectify unavailable ({exc}), using cv2.remap", flush=True)
            return False
        print("display: rectify on the GPU", flush=True)
        return True

    def __enter__(self) -> "DisplayWorker":
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        with self._cond:
            self._stop = True
            self._cond.notify()
        self._thread.join(timeout=5.0)
