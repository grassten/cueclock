"""Raw clip container: BayerRG8 frames + per-frame camera timestamps.

Clip directory layout:
    meta.json        — dimensions, camera settings, frame count, label
    frames.raw       — frame_count x height x width uint8, concatenated
    timestamps.npy   — float64 seconds relative to the first frame

Raw Bayer keeps clips bit-identical to the live feed (~2.2 MB/frame at
1920x1200). Clips are local fixtures, never published — see tests/clips/README.md.
"""

import json
import os
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from types import TracebackType
from typing import BinaryIO, cast

import numpy as np
import numpy.typing as npt

META_NAME = "meta.json"
FRAMES_NAME = "frames.raw"
TIMESTAMPS_NAME = "timestamps.npy"
CAMERA_FPS = 156.25  # the camera free-runs here at the locked 6 ms exposure


@dataclass
class ClipMeta:
    width: int
    height: int
    pixel_format: str
    frame_count: int
    label: str
    camera_serial: str
    exposure_us: float
    gain: float
    first_timestamp_ns: int
    recorded_at: str  # ISO 8601, host clock
    # The detect cadence the frames were stored at (checkpoint 106). Absent on
    # older clips; ClipReader.cadence_hz infers it for those.
    cadence_hz: float | None = None


class ClipWriter:
    """Streams frames to disk; close() finalizes meta.json + timestamps.npy.

    Usable as a context manager — the clip on disk is valid (with the actual
    frame count) even if recording is interrupted mid-stream.
    """

    def __init__(
        self,
        clip_dir: Path,
        label: str,
        camera_serial: str,
        exposure_us: float,
        gain: float,
        recorded_at: str,
        pixel_format: str = "BayerRG8",
        cadence_hz: float | None = None,
    ) -> None:
        clip_dir.mkdir(parents=True, exist_ok=False)
        self._dir = clip_dir
        self._label = label
        self._serial = camera_serial
        self._exposure_us = exposure_us
        self._gain = gain
        self._recorded_at = recorded_at
        self._pixel_format = pixel_format
        self._cadence_hz = cadence_hz
        self._file: BinaryIO | None = open(clip_dir / FRAMES_NAME, "wb")  # noqa: SIM115 — lifetime spans write() calls; closed in close()
        self._timestamps_ns: list[int] = []
        self._shape: tuple[int, int] | None = None

    @property
    def frame_count(self) -> int:
        return len(self._timestamps_ns)

    def write(self, frame: npt.NDArray[np.uint8], timestamp_ns: int) -> None:
        if self._file is None:
            raise ValueError("writer is closed")
        if frame.dtype != np.uint8 or frame.ndim != 2:
            raise ValueError(f"expected 2-D uint8 Bayer frame, got {frame.dtype} {frame.shape}")
        shape = (int(frame.shape[0]), int(frame.shape[1]))
        if self._shape is None:
            self._shape = shape
        elif shape != self._shape:
            raise ValueError(f"frame shape {shape} != first frame {self._shape}")
        self._file.write(frame.tobytes())
        self._timestamps_ns.append(timestamp_ns)

    def close(self) -> ClipMeta:
        if self._file is None:
            raise ValueError("writer already closed")
        self._file.close()
        self._file = None
        ts = np.array(self._timestamps_ns, dtype=np.int64)
        first_ns = int(ts[0]) if len(ts) else 0
        seconds = (ts - first_ns).astype(np.float64) / 1e9
        np.save(self._dir / TIMESTAMPS_NAME, seconds)
        height, width = self._shape if self._shape else (0, 0)
        meta = ClipMeta(
            width=width,
            height=height,
            pixel_format=self._pixel_format,
            frame_count=len(ts),
            label=self._label,
            camera_serial=self._serial,
            exposure_us=self._exposure_us,
            gain=self._gain,
            first_timestamp_ns=first_ns,
            recorded_at=self._recorded_at,
            cadence_hz=self._cadence_hz,
        )
        (self._dir / META_NAME).write_text(json.dumps(asdict(meta), indent=2) + "\n")
        return meta

    def __enter__(self) -> "ClipWriter":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._file is not None:
            self.close()


class ClipReader:
    """Whole-frame reader; iterates (frame, seconds-from-first-frame).

    Deliberately **not** `np.memmap`. The corpus lives on an external exFAT
    SSD, where mapped reads fault in at 4 KB granularity with no useful
    readahead: measured 2026-08-02 on cold clips (100 frames each, methods
    alternated across six clips so neither warms the other's page cache),
    memmap sustained **207 MB/s** against **958 MB/s** for one `pread` per
    frame — 11.1 vs 2.4 ms per 1920x1200 frame. The corpus battery streams
    ~130 GB per run, so the mapping cost ~9 ms/frame of pure wait against a
    5.5 ms/frame detect budget.

    `pread` keeps no shared file offset, so a reader stays safe to share
    across threads. Frames are read-only, exactly as the `mode="r"` mapping's
    views were.
    """

    def __init__(self, clip_dir: Path) -> None:
        self._fd: int | None = None  # set first: __del__ runs even if __init__ raises
        raw = json.loads((clip_dir / META_NAME).read_text())
        self.meta = ClipMeta(**raw)
        self.timestamps: npt.NDArray[np.float64] = np.load(clip_dir / TIMESTAMPS_NAME)
        self._frame_bytes = self.meta.height * self.meta.width
        if self.meta.frame_count:
            self._fd = os.open(clip_dir / FRAMES_NAME, os.O_RDONLY)

    def __len__(self) -> int:
        return self.meta.frame_count

    @property
    def cadence_hz(self) -> float:
        """The detect cadence these frames were stored at — what a pipeline
        replaying every one of them must be built for (`VisionPipeline`'s
        ``cadence_hz``). Recorded in meta.json from checkpoint 106; older
        clips are either `record`-made at the camera's full rate or ring
        dumps of the stride-2 stream, told apart by their measured rate (a
        dump whose live loop sagged is still a stride-2 dump: 37-78 fps in
        the corpus, against 144-156 for the full-rate clips)."""
        if self.meta.cadence_hz is not None:
            return self.meta.cadence_hz
        span = float(self.timestamps[-1] - self.timestamps[0]) if len(self.timestamps) > 1 else 0.0
        fps = (self.meta.frame_count - 1) / span if span > 0 else 0.0
        return CAMERA_FPS if fps > 120 else CAMERA_FPS / 2

    def detect_indices(self, start: int = 0, stride: int = 1) -> list[int]:
        """Frame indices to replay: every stored frame by default, at
        :attr:`cadence_hz` (live keeps every frame it detects on since
        checkpoint 106, so a dump replays exactly what the session saw).
        ``stride`` thins it for experiments — the pipeline is then built for
        ``cadence_hz / stride``. ``start`` skips a warm-replay prefix.
        """
        return list(range(start, self.meta.frame_count, stride))

    def frame(self, index: int) -> npt.NDArray[np.uint8]:
        i = index + self.meta.frame_count if index < 0 else index
        if self._fd is None or not 0 <= i < self.meta.frame_count:
            raise IndexError(f"frame {index} out of range for a {self.meta.frame_count}-frame clip")
        buf = os.pread(self._fd, self._frame_bytes, i * self._frame_bytes)
        if len(buf) != self._frame_bytes:
            # Truncated frames.raw — a half-written clip must fail loudly, not
            # replay short and silently change what the corpus grades.
            raise ValueError(
                f"{FRAMES_NAME} is short at frame {i}: read {len(buf)} of {self._frame_bytes} bytes"
            )
        return cast(
            npt.NDArray[np.uint8],
            np.frombuffer(buf, dtype=np.uint8).reshape(self.meta.height, self.meta.width),
        )

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "ClipReader":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()

    def __iter__(self) -> Iterator[tuple[npt.NDArray[np.uint8], float]]:
        for i in range(self.meta.frame_count):
            yield self.frame(i), float(self.timestamps[i])
