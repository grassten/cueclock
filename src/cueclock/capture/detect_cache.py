"""Offline replay accelerator: per-frame detector output, cached on disk.

**Offline grading only — `cueclock run` never touches this.** It exists for
the corpus battery and for scripts that replay clips repeatedly.

The corpus battery replays ~53,000 frames per run, and re-derives the same
detections every time even though almost every change under test is to
`vision/events.py` or `vision/track.py`. Measured on the M2 (2026-08-02),
per frame: reading the raw Bayer frame 2.4 ms, `detect_balls` 5.5 ms,
churn 0.4 ms — against 0.08 ms for the tracker and event engine that actually
change. Caching the seam between the two halves (see
`VisionPipeline.detect`/`advance`) turns a ~5 minute battery into seconds and
removes the T7 read entirely: ~21 MB of cached detections stand in for
~134 GB of frames.

Correctness rests entirely on the key. It is built from the *inputs*
`detect_balls` consumes, not from a list of source files someone has to
remember to extend:

- `ReferenceModel.digest()` content-addresses the three arrays detection
  reads (median gray, the shadow-rejection Bayer frame, the cloth mask), so
  the reference clip's pixels, how many frames were sampled and which,
  `build_reference`, `cloth_mask` and the dilation are all covered at once.
- `detect.py`'s bytes cover `detect_balls`, `ball_churn` and `bayer_to_gray`.
- `DetectConfig`'s field values cover every geometry, shadow and merge gate.
- Clip identity includes `frames.raw`'s size and mtime, because the metadata
  alone does not pin pixel content (`medium_shot/meta.json` carries
  `"label": "soft_shot"`, and every flag dump shares `"label": "flagged"`
  with the same frame count).
- OpenCV and NumPy versions, since connected components and median are
  theirs.

Deliberately *not* in the key: `events.py` and `track.py` (the whole point —
they must be free to change), `pipeline.py` (keying it would make every edit
to the cache's own glue invalidate everything), and the table calibration
(detection runs on distorted pixels and never sees it, so a post-bump
recalibration must not throw the cache away).

Frame indices are stored, not derived, and validated on load: churn couples
each frame to the previous one *in the replayed sequence*, so a different
stride or a warm-replay start offset is a different cache entry, and a
mismatch must be an error rather than a silent misalignment.
"""

import hashlib
import json
import os
import uuid
from dataclasses import asdict
from pathlib import Path

import cv2
import numpy as np

from cueclock.capture.clip import FRAMES_NAME, META_NAME
from cueclock.vision.detect import DetectConfig, Detection
from cueclock.vision.pipeline import ReferenceModel

SCHEMA = 1
DEFAULT_CACHE_DIR = Path(".detect-cache.nosync")
"""Repo-local and gitignored. The `.nosync` suffix is load-bearing, not
decoration: iCloud syncs Desktop and stamps `UF_HIDDEN` on what it touches,
which has already broken this repo once (CHANGELOG checkpoint 13)."""

# One frame's detections: the blobs plus each blob's appearance churn
# (None on the first replayed frame, which has no predecessor to difference).
FrameDetections = tuple[list[Detection], list[float | None]]


def _clip_identity(clip_dir: Path) -> str:
    meta = json.loads((clip_dir / META_NAME).read_text())
    stat = (clip_dir / FRAMES_NAME).stat()
    return "|".join(
        str(x)
        for x in (
            clip_dir.name,
            meta["frame_count"],
            meta["first_timestamp_ns"],
            meta["recorded_at"],
            stat.st_size,
            stat.st_mtime_ns,
        )
    )


def cache_key(clip_dir: Path, reference: ReferenceModel, config: DetectConfig) -> str:
    """Everything that can change what `VisionPipeline.detect` returns."""
    from cueclock.vision import detect as detect_module

    digest = hashlib.sha256()
    digest.update(f"schema={SCHEMA}".encode())
    digest.update(Path(str(detect_module.__file__)).read_bytes())
    digest.update(repr(sorted(asdict(config).items())).encode())
    digest.update(reference.digest().encode())
    digest.update(_clip_identity(clip_dir).encode())
    digest.update(f"cv2={cv2.__version__} np={np.__version__}".encode())
    return digest.hexdigest()[:32]


class DetectionCache:
    """Content-addressed store of `VisionPipeline.detect` output, one file per clip+key."""

    def __init__(self, root: Path = DEFAULT_CACHE_DIR) -> None:
        self.root = root

    def _path(self, clip_dir: Path, key: str) -> Path:
        return self.root / f"{clip_dir.name}-{key[:16]}.npz"

    def load(
        self, clip_dir: Path, key: str, frame_indices: list[int]
    ) -> list[FrameDetections] | None:
        """Cached detections for exactly ``frame_indices``, or None on a miss.

        Raises on a corrupt or mismatched entry — a cache that silently falls
        back to a cold run hides a failing disk, and one that silently returns
        a different frame sequence changes what the corpus grades.
        """
        path = self._path(clip_dir, key)
        if not path.exists():
            return None
        with np.load(path, allow_pickle=False) as data:  # CRC32-checked by zipfile
            stored = data["frame_indices"]
            if stored.tolist() != frame_indices:
                raise ValueError(
                    f"{path.name}: cached {len(stored)} frame indices do not match the "
                    f"{len(frame_indices)} requested — churn is sequence-dependent, so this "
                    "entry cannot be reused. Delete the cache directory and re-run."
                )
            counts = data["counts"]
            cx, cy, area = data["cx"], data["cy"], data["area"]
            whiteness, churn = data["whiteness"], data["churn"]
            merged, bbox = data["merged"], data["bbox"]
        if int(counts.sum()) != len(cx):
            raise ValueError(f"{path.name}: counts sum to {counts.sum()} but hold {len(cx)} rows")

        out: list[FrameDetections] = []
        offsets = np.concatenate([[0], np.cumsum(counts)])
        for f in range(len(counts)):
            lo, hi = int(offsets[f]), int(offsets[f + 1])
            out.append(
                (
                    [
                        Detection(
                            cx=float(cx[i]),
                            cy=float(cy[i]),
                            area=float(area[i]),
                            bbox=(
                                int(bbox[i, 0]),
                                int(bbox[i, 1]),
                                int(bbox[i, 2]),
                                int(bbox[i, 3]),
                            ),
                            whiteness=float(whiteness[i]),
                            merged=bool(merged[i]),
                        )
                        for i in range(lo, hi)
                    ],
                    [None if np.isnan(churn[i]) else float(churn[i]) for i in range(lo, hi)],
                )
            )
        return out

    def store(
        self,
        clip_dir: Path,
        key: str,
        frame_indices: list[int],
        frames: list[FrameDetections],
    ) -> None:
        """Write atomically: tmp file, fsync, rename.

        Four corpus clips are replayed by two tests each, so two xdist workers
        can target the same entry. Detection is deterministic, so both write
        identical bytes and last-writer-wins is harmless — but the tmp name
        must be unique, or two workers interleave into one path and produce a
        genuinely corrupt file.
        """
        flat = [(d, c) for dets, churns in frames for d, c in zip(dets, churns, strict=True)]
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(clip_dir, key)
        tmp = path.with_suffix(f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            with open(tmp, "wb") as fh:
                # float64 throughout: the corpus turns on knife-edge margins (a
                # 12.04 mm tug against a 12 mm floor), so the cache must not
                # perturb a centroid. Not savez_compressed — float64 centroids
                # barely compress and every write would pay for it.
                np.savez(
                    fh,
                    frame_indices=np.asarray(frame_indices, dtype=np.int32),
                    counts=np.asarray([len(d) for d, _ in frames], dtype=np.int32),
                    cx=np.asarray([d.cx for d, _ in flat], dtype=np.float64),
                    cy=np.asarray([d.cy for d, _ in flat], dtype=np.float64),
                    area=np.asarray([d.area for d, _ in flat], dtype=np.float64),
                    whiteness=np.asarray([d.whiteness for d, _ in flat], dtype=np.float64),
                    churn=np.asarray(
                        [np.nan if c is None else c for _, c in flat], dtype=np.float64
                    ),
                    merged=np.asarray([d.merged for d, _ in flat], dtype=np.bool_),
                    bbox=np.asarray([d.bbox for d, _ in flat], dtype=np.int32).reshape(-1, 4),
                )
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
