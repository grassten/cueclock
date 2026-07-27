"""Raw clip container: BayerRG8 frames + per-frame camera timestamps.

Clip directory layout:
    meta.json        — dimensions, camera settings, frame count, label
    frames.raw       — frame_count x height x width uint8, concatenated
    timestamps.npy   — float64 seconds relative to the first frame

Raw Bayer keeps clips bit-identical to the live feed (~2.2 MB/frame at
1920x1200). Clips are local fixtures, never published — see tests/clips/README.md.
"""

import json
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from types import TracebackType
from typing import BinaryIO

import numpy as np
import numpy.typing as npt

META_NAME = "meta.json"
FRAMES_NAME = "frames.raw"
TIMESTAMPS_NAME = "timestamps.npy"


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
    ) -> None:
        clip_dir.mkdir(parents=True, exist_ok=False)
        self._dir = clip_dir
        self._label = label
        self._serial = camera_serial
        self._exposure_us = exposure_us
        self._gain = gain
        self._recorded_at = recorded_at
        self._pixel_format = pixel_format
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
    """Memory-mapped reader; iterates (frame, seconds-from-first-frame)."""

    def __init__(self, clip_dir: Path) -> None:
        raw = json.loads((clip_dir / META_NAME).read_text())
        self.meta = ClipMeta(**raw)
        self.timestamps: npt.NDArray[np.float64] = np.load(clip_dir / TIMESTAMPS_NAME)
        self._frames: npt.NDArray[np.uint8]
        if self.meta.frame_count:
            self._frames = np.memmap(
                clip_dir / FRAMES_NAME,
                dtype=np.uint8,
                mode="r",
                shape=(self.meta.frame_count, self.meta.height, self.meta.width),
            )
        else:
            self._frames = np.empty((0, 0, 0), dtype=np.uint8)

    def __len__(self) -> int:
        return self.meta.frame_count

    def frame(self, index: int) -> npt.NDArray[np.uint8]:
        return np.asarray(self._frames[index])

    def __iter__(self) -> Iterator[tuple[npt.NDArray[np.uint8], float]]:
        for i in range(self.meta.frame_count):
            yield self.frame(i), float(self.timestamps[i])
