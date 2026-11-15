"""Recalibrate the rig in one step: new reference, undistort and calibration.

`cueclock recalibrate` (and the deck's RECAL hold, which runs it) replaces the
three-command recipe of `docs/hardware.md` §E2 plus a code edit. Nobody is
looking at a preview when it runs, so it makes the checks a person used to make
by eye, and it changes nothing unless they all pass:

1. **Capture** a short empty-table clip into a staging directory.
2. **Reject a bad capture.** Balls still on the cloth, a fit residual over
   2 px, or no table found: nothing on disk changes, and the reason is the
   result.
3. **Swap only on success.** The undistort file, the calibration and the
   reference pointer are replaced together. The previous set is kept under
   `config/backup/` first, so a bad recalibration can be undone by hand.

The **reference pointer** (`config/reference.json`) replaces the constant the
launch command used to hard-code. `run` reads it at launch, so a recalibration
takes effect on the next session start with no code or command-line edit.

The live reference is stored on the internal disk (`~/cueclock-reference`),
not on the T7: a session start then does not depend on the T7 being mounted,
and it is outside iCloud's reach, which matters because a launchd job cannot
bring back a file iCloud has evicted (CLAUDE.md). When the T7 is mounted the
clip is also copied into `tests/clips`, so corpus clips recorded on this mount
have their reference.

Everything that touches the camera is injected (`capture`), so the rules
above are unit-tested without one.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import numpy.typing as npt

from cueclock.capture.clip import ClipReader
from cueclock.display.pipeline import UndistortParams
from cueclock.display.undistort_fit import cloth_mask
from cueclock.display.undistort_fit import fit_from_frame as fit_undistort
from cueclock.vision.calibration import TableCalibration, boundary_drift
from cueclock.vision.calibration import fit_from_frame as fit_calibration

CONFIG_DIR = Path("config")
POINTER_NAME = "reference.json"
UNDISTORT_NAME = "display_undistort.json"
CALIBRATION_NAME = "table_calibration.json"
BACKUP_DIR_NAME = "backup"
REFERENCE_ROOT = Path("~/cueclock-reference")
CORPUS_DIR = Path("tests/clips")
# mkstemp creates files 0600; the files it replaces were ordinary 0644 ones.
CONFIG_MODE = 0o644

# Same 3 s every corpus reference clip uses (docs/cli.md). `run` reads only
# 15 frames of it, but the corpus copy should look like the others.
CAPTURE_SECONDS = 3.0
# Both fits already warn above 2 px (`fit-undistort`, `calibrate`). Here the
# warning becomes a refusal, since nobody is reading it.
MAX_RESIDUAL_PX = 2.0
# A ball on the cloth leaves a hole in the cloth mask. Measured 2026-11-15 on
# the middle frame of corpus clips: the empty-table references have holes of
# at most 291 px (the spots and cloth marks: empty_table 258/117, _0729 118,
# _0801 291), and balls leave 500-1460 px (static_spread and
# all_balls_still_1115: 12 of 14 holes over 500). 450 sits between the two.
# A ball against a cushion is not a hole, but it bends the boundary and
# fails the residual check instead.
BALL_HOLE_MIN_PX = 450.0
# Reference frames `run` builds its background from; this module medians the
# same count for the drift readings so the numbers match what `run` logs.
REFERENCE_FRAMES = 15


@dataclass(frozen=True)
class Result:
    """What a recalibration did. `ok=False` means nothing on disk changed."""

    ok: bool
    reason: str  # one short line, printed and shown on the deck key
    residual_px: float | None = None  # the worse of the two fit residuals
    drift_before: float | None = None  # old reference vs. the new capture
    drift_after: float | None = None  # new reference vs. its own last frame
    reference: str | None = None
    corpus_copy: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, text: str) -> Result:
        data = json.loads(text)
        return cls(**{k: data.get(k) for k in cls.__dataclass_fields__})


# ---------- the pointer ----------


def read_pointer(config_dir: Path = CONFIG_DIR) -> Path | None:
    """The reference `run` should use, or None if no pointer has been written."""
    try:
        payload = json.loads((config_dir / POINTER_NAME).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    value = payload.get("reference") if isinstance(payload, dict) else None
    return Path(value) if isinstance(value, str) else None


def write_pointer(path: Path, reference: Path, **extra: object) -> None:
    """Write the pointer atomically (temp file, then rename)."""
    payload = {"reference": str(reference), **extra}
    _atomic_write(path, json.dumps(payload, indent=2) + "\n")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.chmod(tmp, CONFIG_MODE)
    os.replace(tmp, path)


# ---------- checks ----------


def ball_holes(bgr: npt.NDArray[np.uint8]) -> int:
    """How many ball-sized holes the cloth mask has (0 on an empty table)."""
    cloth = cloth_mask(bgr)
    contours, hierarchy = cv2.findContours(cloth, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if hierarchy is None:
        return 0
    return sum(
        1
        for contour, (_next, _prev, _child, parent) in zip(contours, hierarchy[0], strict=True)
        if parent >= 0 and cv2.contourArea(contour) >= BALL_HOLE_MIN_PX
    )


def _median_gray(reader: ClipReader, count: int = REFERENCE_FRAMES) -> npt.NDArray[np.uint8]:
    """The median of `count` evenly spaced frames, in gray: what `run` compares."""
    indices = np.linspace(0, len(reader) - 1, num=min(count, len(reader)), dtype=int)
    stack = np.stack([cv2.cvtColor(reader.frame(int(i)), cv2.COLOR_BayerBG2GRAY) for i in indices])
    return np.asarray(np.median(stack, axis=0), dtype=np.uint8)


def _gray(bayer: npt.NDArray[np.uint8]) -> npt.NDArray[np.uint8]:
    return np.asarray(cv2.cvtColor(bayer, cv2.COLOR_BayerBG2GRAY), dtype=np.uint8)


def _old_drift(
    config_dir: Path, new_live: npt.NDArray[np.uint8], old_reference: Path | None
) -> float | None:
    """The reading the old set would have logged at launch, against this capture."""
    cal_path = config_dir / CALIBRATION_NAME
    if old_reference is None or not old_reference.is_dir() or not cal_path.exists():
        return None
    try:
        return boundary_drift(
            _median_gray(ClipReader(old_reference)), new_live, TableCalibration.load(cal_path)
        )
    except (OSError, ValueError, KeyError):
        return None  # a broken old set is exactly what is being replaced


# ---------- the swap ----------


def _backup(config_dir: Path) -> None:
    """Copy the current set to `config/backup/`, replacing the previous backup."""
    backup = config_dir / BACKUP_DIR_NAME
    backup.mkdir(parents=True, exist_ok=True)
    for name in (UNDISTORT_NAME, CALIBRATION_NAME, POINTER_NAME):
        src = config_dir / name
        if src.exists():
            shutil.copy2(src, backup / name)


def _prune(reference_root: Path, keep: set[Path]) -> None:
    """Delete references in our own root except the new one and the previous one.

    Each is about a gigabyte. Only `reference_root` is ever pruned: a pointer
    that still names a corpus clip on the T7 leaves that clip alone.
    """
    for child in reference_root.iterdir():
        if child.is_dir() and not child.name.startswith(".") and child.resolve() not in keep:
            shutil.rmtree(child, ignore_errors=True)


def _unique(root: Path, label: str) -> Path:
    candidate, n = root / label, 2
    while candidate.exists():
        candidate, n = root / f"{label}_{n}", n + 1
    return candidate


def recalibrate(
    capture: Callable[[Path, str], None],
    *,
    config_dir: Path = CONFIG_DIR,
    reference_root: Path = REFERENCE_ROOT,
    corpus_dir: Path = CORPUS_DIR,
    now: Callable[[], datetime] = datetime.now,
) -> Result:
    """Record, check, and swap in a new reference set. See the module docstring.

    `capture(clip_dir, label)` records an empty-table clip into `clip_dir`,
    which must not exist yet. It raises `RuntimeError` with a short reason
    when the camera cannot be used.
    """
    reference_root = reference_root.expanduser()
    reference_root.mkdir(parents=True, exist_ok=True)
    stamp = now()
    label = f"empty_table_{stamp:%m%d}"
    old_reference = read_pointer(config_dir)

    with tempfile.TemporaryDirectory(dir=reference_root, prefix=".staging-") as staging_str:
        staging = Path(staging_str)
        clip = staging / label
        try:
            capture(clip, label)
        except RuntimeError as exc:
            print(f"capture failed: {exc}")
            lines = str(exc).splitlines()
            return Result(ok=False, reason=lines[0] if lines else "CAMERA FAILED")
        reader = ClipReader(clip)
        if len(reader) < 2:
            return Result(ok=False, reason="NO FRAMES")
        middle = reader.frame(len(reader) // 2)
        bgr = np.asarray(cv2.cvtColor(middle, cv2.COLOR_BayerBG2BGR_EA), dtype=np.uint8)

        try:
            holes = ball_holes(bgr)
            if holes:
                print(f"{holes} ball-sized hole(s) in the cloth — clear the table and retry")
                return Result(ok=False, reason="BALLS ON CLOTH")
            undistort = fit_undistort(bgr)
            calibration = fit_calibration(bgr, undistort)
        except ValueError:  # no cloth, or too few boundary points on a side
            return Result(ok=False, reason="NO TABLE")
        residual = max(undistort.fit_residual_px, calibration.line_residual_px)
        if residual > MAX_RESIDUAL_PX:
            return Result(ok=False, reason=f"BAD FIT {residual:.1f}px", residual_px=residual)

        new_live = _gray(reader.frame(len(reader) - 1))
        drift_before = _old_drift(config_dir, new_live, old_reference)
        drift_after = boundary_drift(_median_gray(reader), new_live, calibration)

        # Every check passed: from here on, the old set is replaced.
        final = _unique(reference_root, label)
        clip.rename(final)

    _backup(config_dir)
    _install(config_dir, undistort, calibration)
    write_pointer(
        config_dir / POINTER_NAME,
        final,
        recorded_at=stamp.isoformat(timespec="seconds"),
        residual_px=round(residual, 3),
        drift_before=None if drift_before is None else round(drift_before, 1),
    )
    keep = {final.resolve()}
    if old_reference is not None:
        keep.add(old_reference.resolve())
    _prune(reference_root, keep)

    corpus_copy: str | None = None
    if corpus_dir.is_dir():  # the T7 is mounted (the symlink resolves)
        target = _unique(corpus_dir, final.name)
        try:
            shutil.copytree(final, target)
            corpus_copy = str(target)
        except OSError as exc:  # never undoes a good recalibration
            print(f"corpus copy failed ({exc}) — copy {final} to {corpus_dir} by hand")
    return Result(
        ok=True,
        reason="OK",
        residual_px=residual,
        drift_before=drift_before,
        drift_after=drift_after,
        reference=str(final),
        corpus_copy=corpus_copy,
    )


def _install(config_dir: Path, undistort: UndistortParams, calibration: TableCalibration) -> None:
    """Write both fits beside their targets, then rename them into place.

    The pointer is written last by the caller, so a session started in the
    middle of this sees either the old reference with the old fits or, at
    worst, new fits against the old reference for one launch, which its drift
    check reports.
    """
    staged: list[tuple[Path, Path]] = []
    for name, save in ((UNDISTORT_NAME, undistort.save), (CALIBRATION_NAME, calibration.save)):
        target = config_dir / name
        fd, tmp_name = tempfile.mkstemp(dir=config_dir, prefix=f".{name}.", suffix=".tmp")
        os.close(fd)
        save(Path(tmp_name))
        os.chmod(tmp_name, CONFIG_MODE)
        staged.append((Path(tmp_name), target))
    for staged_path, target in staged:
        os.replace(staged_path, target)
