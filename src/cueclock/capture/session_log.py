"""The session log (#57): what a live session saw, kept so it can be replayed.

`cueclock run` writes one directory per session, named by the journal's stamp:

    sessions.nosync/<stamp>/
        header.json          everything a replay needs that is not per-frame
        detections/NNNNNN.npz  ~10 s chunks of per-frame detector output
        inputs.jsonl         every non-vision hub input (`hub.hub_input`)
        stills/index.jsonl   one line per still trigger
        stills/*.png         raw Bayer frames, lossless

The detection log is the seam `capture.detect_cache` already caches offline —
the detector's output, before churn's state and everything after it — so a
session replays through the tracker, the event engine and the hub without a
single pixel. Stills are the pixels for the moments that matter (each strike,
the table half a second before it, each settle, each operator command), each
with its churn-lag predecessor so churn can be recomputed when the detector is
re-run on them.

**The detect loop never waits on this.** :meth:`SessionLogWriter.frame` and
:meth:`~SessionLogWriter.still` append to memory and hand finished work to one
writer thread through a bounded queue; when the queue is full the item is
dropped and counted (`on_drop`, which `run` journals), never waited for.
Compression and PNG encoding both run on the writer thread and release the
GIL. A process crash loses at most the open chunk: each one lands by rename.

`sessions.nosync` carries the suffix for the reason every other local data
directory here does: iCloud syncs Desktop and evicts what it thinks is idle
(CLAUDE.md, "Dev workflow").
"""

import json
import os
import queue
import subprocess  # nosec B404: fixed git argv, no shell
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import numpy.typing as npt

from cueclock.archive import ARCHIVE_ROOT, SESSIONS_ROOT
from cueclock.capture.detect_cache import pack_frames, unpack_frames
from cueclock.vision.detect import Detection, Intrusion, Reject

SCHEMA = 1
# One definition of where sessions live, in the stdlib-only module the deck imports.
SESSIONS_DIR = SESSIONS_ROOT
ARCHIVE_DIR = ARCHIVE_ROOT
HEADER_NAME = "header.json"
INPUTS_NAME = "inputs.jsonl"
DETECTIONS_DIR = "detections"
STILLS_DIR = "stills"
STILLS_INDEX = "index.jsonl"

CHUNK_SECONDS = 10.0  # of camera time per detections chunk: what a crash can lose
QUEUE_ITEMS = 32  # chunks and stills waiting for the writer — ~5 min of chunks
PNG_COMPRESSION = 1  # see docs/design.md §12 for the measured trade


@dataclass(frozen=True)
class LoggedFrame:
    """One detect frame as the stateful half saw it.

    ``frame_i`` is the camera's frame counter (gaps are camera drops), ``t``
    the capture time in camera seconds since the session's first frame — the
    ``t`` `VisionPipeline.advance` was given — and ``mono`` the
    ``time.monotonic()`` at which its stateful half ran, which is the hub's
    clock: a replay sets its virtual clock to it before feeding the frame.
    """

    frame_i: int
    t: float
    mono: float
    detections: list[Detection]
    churn: list[float | None]
    intrusions: list[Intrusion]
    rejects: list[Reject] = field(default_factory=list)


@dataclass(frozen=True)
class StillFrame:
    frame_i: int
    t: float
    bayer: npt.NDArray[np.uint8]


@dataclass
class WriterStats:
    frames: int = 0
    chunks: int = 0
    stills: int = 0
    pngs: int = 0
    bytes: int = 0
    dropped_frames: int = 0
    dropped_stills: int = 0
    chunk_s: float = 0.0  # writer-thread seconds spent compressing + writing chunks
    png_s: float = 0.0  # ... encoding + writing PNGs

    def as_dict(self) -> dict[str, object]:
        return {k: round(v, 3) if isinstance(v, float) else v for k, v in self.__dict__.items()}


def _git(*argv: str) -> str:
    done = subprocess.run(  # nosec B603 B607: fixed git argv, no shell
        ["git", *argv], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def git_state() -> tuple[str | None, bool]:
    """(HEAD sha, tracked files modified) — None when not in a git checkout."""
    try:
        sha = _git("rev-parse", "HEAD")
        dirty = _git("status", "--porcelain", "--untracked-files=no")
    except (OSError, subprocess.CalledProcessError):
        return None, False
    return sha, bool(dirty)


def _write_atomic(path: Path, write: Callable[[Any], None], mode: str = "wb") -> int:
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, mode) as fh:
        write(fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    return path.stat().st_size


def pack_logged(frames: list[LoggedFrame]) -> dict[str, Any]:
    """A chunk's arrays: `detect_cache`'s layout plus the per-frame stamps and
    the rejected blobs."""
    rflat = [r for f in frames for r in f.rejects]
    return {
        "frame_i": np.asarray([f.frame_i for f in frames], dtype=np.int64),
        "t": np.asarray([f.t for f in frames], dtype=np.float64),
        "mono": np.asarray([f.mono for f in frames], dtype=np.float64),
        **pack_frames([(f.detections, f.churn, f.intrusions) for f in frames]),
        "reject_counts": np.asarray([len(f.rejects) for f in frames], dtype=np.int32),
        "reject_bbox": np.asarray([r.bbox for r in rflat], dtype=np.int32).reshape(-1, 4),
        "reject_area": np.asarray([r.area for r in rflat], dtype=np.float64),
        "reject_reason": np.asarray([r.reason for r in rflat], dtype=np.uint8),
    }


def unpack_logged(data: Any, name: str) -> list[LoggedFrame]:
    detected = unpack_frames(data, name)
    counts = data["reject_counts"]
    bbox, area, reason = data["reject_bbox"], data["reject_area"], data["reject_reason"]
    if int(counts.sum()) != len(area):
        raise ValueError(f"{name}: reject counts sum to {counts.sum()} but hold {len(area)} rows")
    offsets = np.concatenate([[0], np.cumsum(counts)])
    out = []
    for f, (dets, churn, intrusions) in enumerate(detected):
        rejects = [
            Reject(
                bbox=(int(bbox[i, 0]), int(bbox[i, 1]), int(bbox[i, 2]), int(bbox[i, 3])),
                area=float(area[i]),
                reason=int(reason[i]),
            )
            for i in range(int(offsets[f]), int(offsets[f + 1]))
        ]
        out.append(
            LoggedFrame(
                frame_i=int(data["frame_i"][f]),
                t=float(data["t"][f]),
                mono=float(data["mono"][f]),
                detections=dets,
                churn=churn,
                intrusions=intrusions,
                rejects=rejects,
            )
        )
    return out


class SessionLogWriter:
    """Writes one session directory; see the module docstring for threading.

    :meth:`frame` and :meth:`still` belong to the detect loop's thread,
    :meth:`input` to the server's asyncio loop (it appends synchronously, as
    the journal does — inputs are rare and replay parity needs every one).
    ``on_drop(kind, count)`` is called on the detect loop's thread when the
    queue is full and an item is thrown away.
    """

    def __init__(
        self,
        root: Path,
        header: dict[str, object],
        *,
        chunk_seconds: float = CHUNK_SECONDS,
        queue_items: int = QUEUE_ITEMS,
        png_compression: int = PNG_COMPRESSION,
        on_drop: Callable[[str, int], None] | None = None,
    ) -> None:
        self.root = root
        (root / DETECTIONS_DIR).mkdir(parents=True, exist_ok=False)
        (root / STILLS_DIR).mkdir()
        self.stats = WriterStats()
        self.stats.bytes += _write_atomic(
            root / HEADER_NAME,
            lambda fh: fh.write(json.dumps({"schema": SCHEMA, **header}, indent=2) + "\n"),
            mode="w",
        )
        self._chunk_seconds = chunk_seconds
        self._png = [cv2.IMWRITE_PNG_COMPRESSION, png_compression]
        self._on_drop = on_drop
        self._open: list[LoggedFrame] = []
        self._chunk_index = 0
        self._written_pngs: set[int] = set()
        self._queue: queue.Queue[tuple[str, Any] | None] = queue.Queue(maxsize=queue_items)
        self._thread = threading.Thread(target=self._run, name="cueclock-session-log", daemon=True)
        self._thread.start()

    # ---------- producer side ----------

    def frame(self, record: LoggedFrame) -> None:
        self._open.append(record)
        if record.t - self._open[0].t >= self._chunk_seconds:
            self._hand_off_chunk()

    def still(
        self,
        reason: str,
        *,
        mono: float,
        wall: str,
        t: float,
        frame_i: int,
        frames: list[StillFrame],
    ) -> None:
        """One still trigger: ``frames`` are the frame and its predecessor(s)."""
        entry = {"mono": round(mono, 4), "wall": wall, "t": round(t, 4), "frame_i": frame_i}
        if not self._offer(("still", ({**entry, "reason": reason}, frames))):
            self.stats.dropped_stills += 1
            if self._on_drop is not None:
                self._on_drop("still", 1)

    def input(self, record: dict[str, object]) -> None:
        line = json.dumps(record, default=_jsonable) + "\n"
        with (self.root / INPUTS_NAME).open("a") as fh:
            fh.write(line)
        self.stats.bytes += len(line)

    def close(self) -> WriterStats:
        """Flush the open chunk and wait for the writer to drain."""
        if self._open:
            self._hand_off_chunk(block=True)
        self._queue.put(None)
        self._thread.join()
        return self.stats

    def _hand_off_chunk(self, block: bool = False) -> None:
        chunk, self._open = self._open, []
        index = self._chunk_index
        self._chunk_index += 1
        if block:
            self._queue.put(("chunk", (index, chunk)))
        elif not self._offer(("chunk", (index, chunk))):
            self.stats.dropped_frames += len(chunk)
            if self._on_drop is not None:
                self._on_drop("chunk", len(chunk))

    def _offer(self, item: tuple[str, Any]) -> bool:
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            return False
        return True

    # ---------- writer thread ----------

    def _run(self) -> None:
        while (item := self._queue.get()) is not None:
            kind, payload = item
            if kind == "chunk":
                self._write_chunk(*payload)
            else:
                self._write_still(*payload)

    def _write_chunk(self, index: int, frames: list[LoggedFrame]) -> None:
        started = time.perf_counter()
        arrays = pack_logged(frames)
        self.stats.bytes += _write_atomic(
            self.root / DETECTIONS_DIR / f"{index:06d}.npz",
            lambda fh: np.savez_compressed(fh, **arrays),
        )
        self.stats.chunk_s += time.perf_counter() - started
        self.stats.chunks += 1
        self.stats.frames += len(frames)

    def _write_still(self, entry: dict[str, object], frames: list[StillFrame]) -> None:
        started = time.perf_counter()
        files = []
        for f in frames:
            name = f"{f.frame_i:09d}.png"
            files.append(name)
            if f.frame_i in self._written_pngs:
                continue  # a strike's frame can also be a command's
            ok, png = cv2.imencode(".png", f.bayer, self._png)
            if not ok:
                continue
            (self.root / STILLS_DIR / name).write_bytes(png.tobytes())
            self._written_pngs.add(f.frame_i)
            self.stats.bytes += len(png)
            self.stats.pngs += 1
        line = json.dumps({**entry, "files": files, "t_frames": [round(f.t, 4) for f in frames]})
        with (self.root / STILLS_DIR / STILLS_INDEX).open("a") as fh:
            fh.write(line + "\n")
        self.stats.png_s += time.perf_counter() - started
        self.stats.stills += 1


def _jsonable(value: object) -> object:
    """Input kwargs are JSON from the API already; tuples and the odd Path aren't."""
    if isinstance(value, tuple):
        return list(value)
    return str(value)


class SessionLog:
    """Read side of one session directory."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.header: dict[str, Any] = json.loads((root / HEADER_NAME).read_text())
        if self.header.get("schema") != SCHEMA:
            schema = self.header.get("schema")
            raise ValueError(f"{root}: session log schema {schema}, want {SCHEMA}")

    @property
    def stamp(self) -> str:
        return str(self.header["stamp"])

    def frames(self) -> Iterator[LoggedFrame]:
        for path in sorted((self.root / DETECTIONS_DIR).glob("*.npz")):
            with np.load(path, allow_pickle=False) as data:
                frames = unpack_logged(data, path.name)
            yield from frames

    def inputs(self) -> list[dict[str, Any]]:
        return _read_jsonl(self.root / INPUTS_NAME)

    def stills(self) -> list[dict[str, Any]]:
        return _read_jsonl(self.root / STILLS_DIR / STILLS_INDEX)

    def still_frame(self, name: str) -> npt.NDArray[np.uint8]:
        bayer = cv2.imread(str(self.root / STILLS_DIR / name), cv2.IMREAD_UNCHANGED)
        if bayer is None:
            raise FileNotFoundError(self.root / STILLS_DIR / name)
        return np.asarray(bayer, dtype=np.uint8)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def resolve_session(arg: str) -> Path:
    """A session directory, or a stamp looked up locally then on the archive."""
    path = Path(arg)
    if (path / HEADER_NAME).is_file():
        return path
    for root in (SESSIONS_DIR, ARCHIVE_DIR):
        if (root / arg / HEADER_NAME).is_file():
            return root / arg
    raise FileNotFoundError(f"no session log {arg!r} (looked in {SESSIONS_DIR} and {ARCHIVE_DIR})")
