"""cueclock command-line interface.

Implemented: `record` (build order §7 step 1). Stubs: `run`, `calibrate`.

Run via:  PYTHONPATH=src uv run python -m cueclock.cli record --label soft_shot
"""

import argparse
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from cueclock.capture.clip import ClipReader, ClipWriter

BYTES_PER_FRAME = 1920 * 1200
NOMINAL_FPS = 156.25
DISK_HEADROOM = 5 * 1024**3  # never eat the last 5 GiB of the disk


def _unique_dir(root: Path, label: str) -> Path:
    candidate = root / label
    n = 2
    while candidate.exists():
        candidate = root / f"{label}_{n}"
        n += 1
    return candidate


def _cmd_record(args: argparse.Namespace) -> int:
    from cueclock.capture.camera import EXPOSURE_US, GAIN, open_camera, stream

    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    needed = int(BYTES_PER_FRAME * NOMINAL_FPS * args.seconds * 1.1)
    free = shutil.disk_usage(out_root).free
    if free < needed + DISK_HEADROOM:
        print(
            f"not enough disk: need ~{needed / 1e9:.1f} GB + headroom, {free / 1e9:.1f} GB free",
            file=sys.stderr,
        )
        return 1

    clip_dir = _unique_dir(out_root, args.label)
    print(f"recording {args.seconds:.0f}s -> {clip_dir}  (Ctrl-C stops early, clip stays valid)")
    with open_camera() as cam:
        writer = ClipWriter(
            clip_dir,
            label=args.label,
            camera_serial=cam.GetDeviceInfo().GetSerialNumber(),
            exposure_us=EXPOSURE_US,
            gain=GAIN,
            recorded_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        first_ns: int | None = None
        try:
            for frame, ts_ns in stream(cam):
                writer.write(frame, ts_ns)
                if first_ns is None:
                    first_ns = ts_ns
                elapsed = (ts_ns - first_ns) / 1e9
                if writer.frame_count % 32 == 0:
                    print(f"\r  {elapsed:5.1f}s  {writer.frame_count} frames", end="")
                if elapsed >= args.seconds:
                    break
        except KeyboardInterrupt:
            print("\nstopped early")
        finally:
            meta = writer.close()

    reader = ClipReader(clip_dir)
    ts = reader.timestamps
    if len(ts) > 1:
        gaps = np.diff(ts)
        duration = float(ts[-1])
        print(
            f"\ndone: {meta.frame_count} frames / {duration:.2f}s "
            f"= {(meta.frame_count - 1) / duration:.1f} fps | "
            f"frame gap mean {gaps.mean() * 1e3:.2f} ms, max {gaps.max() * 1e3:.2f} ms"
        )
        if gaps.max() > 1.5 / NOMINAL_FPS:
            print("WARNING: max gap exceeds 1.5x the nominal frame period — dropped frames?")
    else:
        print("\nWARNING: recorded fewer than 2 frames")
    return 0


def _cmd_stub(name: str) -> int:
    print(f"`{name}` is not implemented yet (build order: docs/cv-app-rewrite.md §7)")
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cueclock")
    sub = parser.add_subparsers(dest="command", required=True)

    p_record = sub.add_parser("record", help="record a raw timestamped clip")
    p_record.add_argument("--label", required=True, help="scenario name (directory name)")
    p_record.add_argument("--seconds", type=float, default=10.0, help="duration (default 10)")
    p_record.add_argument("--out", default="tests/clips", help="output root (default tests/clips)")

    sub.add_parser("run", help="clock + vision + server (not yet)")
    sub.add_parser("calibrate", help="ChArUco + homography calibration (not yet)")

    args = parser.parse_args(argv)
    if args.command == "record":
        return _cmd_record(args)
    return _cmd_stub(args.command)


if __name__ == "__main__":
    sys.exit(main())
