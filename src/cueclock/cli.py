"""cueclock command-line interface.

`run` is the product: the automated shot clock (§7 step 5) — camera + vision
drive the clock, with the overlay server and the Syphon table feed in the same
process. The rest are bring-up and tuning tools: `record` (§7 step 1),
`replay` + `live` + `fit-undistort` (display path, §7 step 6), `calibrate`
(rails homography, corpus-grade §7 step 3), `analyze` (detection offline).

Run via:  uv run python -m cueclock.cli run --rectify
"""

import argparse
import shutil
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from cueclock.vision.calibration import TableCalibration

import cv2
import numpy as np
import numpy.typing as npt

from cueclock.capture.clip import ClipReader, ClipWriter
from cueclock.display.pipeline import (
    DEFAULT_UNDISTORT_PATH,
    DisplayPipeline,
    UndistortParams,
    build_undistort_maps,
)

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


def _build_display_pipeline(args: argparse.Namespace) -> DisplayPipeline:
    if getattr(args, "rectify", False):
        from cueclock.vision.calibration import DEFAULT_CALIBRATION_PATH, TableCalibration

        path = DEFAULT_CALIBRATION_PATH
        if not path.exists():
            print(f"table calibration not found: {path} — run `calibrate` first", file=sys.stderr)
            sys.exit(1)
        calibration = TableCalibration.load(path)
        pipeline = DisplayPipeline.rectified(calibration)
        w, h = pipeline.out_size
        print(f"rectified display from {path}: table axis-aligned, {w}x{h}")
        return pipeline
    return DisplayPipeline(_load_undistort_or_none(args))


def _load_undistort_or_none(args: argparse.Namespace) -> UndistortParams | None:
    if args.no_undistort:
        return None
    path = Path(args.undistort)
    if not path.exists():
        if args.undistort != str(DEFAULT_UNDISTORT_PATH):
            print(f"undistort params not found: {path}", file=sys.stderr)
            sys.exit(1)
        print("no undistort params (run `fit-undistort` first) — publishing distorted")
        return None
    params = UndistortParams.load(path)
    print(f"undistorting with {path} (fit residual {params.fit_residual_px:.2f} px)")
    return params


def _test_pattern_rgba() -> npt.NDArray[np.uint8]:
    """Labeled color bars: proves channel order end-to-end in OBS."""
    bgr = np.zeros((1200, 1920, 3), dtype=np.uint8)
    bars = [
        ("RED", (0, 0, 255)),
        ("GREEN", (0, 255, 0)),
        ("BLUE", (255, 0, 0)),
        ("WHITE", (255, 255, 255)),
    ]
    for i, (label, color) in enumerate(bars):
        x = i * 480
        bgr[:, x : x + 480] = color
        cv2.putText(bgr, label, (x + 90, 620), cv2.FONT_HERSHEY_SIMPLEX, 2.6, (0, 0, 0), 8)
    return np.asarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGBA), dtype=np.uint8)


def _cmd_replay(args: argparse.Namespace) -> int:
    from cueclock.display.syphon_out import SyphonOutput

    if args.test_pattern:
        pattern = _test_pattern_rgba()
        print(f"publishing test pattern as Syphon server {args.name!r} — Ctrl-C to stop")
        with SyphonOutput(args.name) as out:
            try:
                while True:
                    out.publish(pattern, pump_seconds=1 / 30)
            except KeyboardInterrupt:
                return 0

    if not args.clip:
        print("--clip is required unless --test-pattern is given", file=sys.stderr)
        return 1
    reader = ClipReader(Path(args.clip))
    if len(reader) < 2:
        print(f"clip {args.clip} has {len(reader)} frames — nothing to replay", file=sys.stderr)
        return 1
    pipeline = _build_display_pipeline(args)
    # subsample the ~156 fps clip down to display rate by timestamp
    period = 1.0 / args.fps
    grid = np.arange(0.0, float(reader.timestamps[-1]), period)
    indices = np.minimum(np.searchsorted(reader.timestamps, grid), len(reader) - 1)
    print(
        f"replaying {args.clip} ({len(reader)} frames) as Syphon server {args.name!r} "
        f"at {args.fps:.0f} fps, {'looping' if not args.once else 'once'} — Ctrl-C to stop"
    )
    with SyphonOutput(args.name, *pipeline.out_size) as out:
        try:
            while True:
                for i in indices:
                    out.publish(pipeline.to_rgba(reader.frame(int(i))), pump_seconds=period)
                if args.once:
                    return 0
        except KeyboardInterrupt:
            return 0


def _cmd_live(args: argparse.Namespace) -> int:
    """Camera -> display pipeline -> Syphon at display rate (no recording).

    Exclusive: can't run while `record` (or anything else) holds the camera.
    """
    from pypylon import pylon

    from cueclock.capture.camera import open_camera
    from cueclock.display.syphon_out import SyphonOutput

    pipeline = _build_display_pipeline(args)
    period = 1.0 / args.fps
    with open_camera() as cam:
        # LatestImageOnly: display always shows the freshest frame; the camera
        # free-runs at 156 fps and we sample it at display rate.
        cam.StartGrabbing(pylon.GrabStrategy_LatestImageOnly)
        print(f"live -> Syphon server {args.name!r} at ~{args.fps:.0f} fps — Ctrl-C to stop")
        try:
            with SyphonOutput(args.name, *pipeline.out_size) as out:
                while True:
                    grab = cam.RetrieveResult(5000, pylon.TimeoutHandling_ThrowException)
                    try:
                        if grab.GrabSucceeded():
                            out.publish(pipeline.to_rgba(grab.Array), pump_seconds=period)
                    finally:
                        grab.Release()
        except KeyboardInterrupt:
            return 0
        finally:
            cam.StopGrabbing()


def _load_frame_bgr(src: Path) -> npt.NDArray[np.uint8]:
    """Middle frame of a clip directory, or a .npy frame (Bayer or BGR)."""
    if src.is_dir():
        reader = ClipReader(src)
        if len(reader) == 0:
            raise SystemExit(f"clip {src} is empty")
        bayer = reader.frame(len(reader) // 2)
        return cast(npt.NDArray[np.uint8], cv2.cvtColor(bayer, cv2.COLOR_BayerBG2BGR_EA))
    arr = np.load(src)
    if arr.ndim == 2:
        return cast(npt.NDArray[np.uint8], cv2.cvtColor(arr, cv2.COLOR_BayerBG2BGR_EA))
    return cast(npt.NDArray[np.uint8], np.clip(arr, 0, 255).astype(np.uint8))


def _cmd_fit_undistort(args: argparse.Namespace) -> int:
    from cueclock.display.undistort_fit import fit_from_frame

    bgr = _load_frame_bgr(Path(args.frame))
    params = fit_from_frame(bgr)
    k1, k2 = params.dist[0], params.dist[1]
    print(f"fit: k1={k1:.4f} k2={k2:.4f} residual {params.fit_residual_px:.2f} px")
    if params.fit_residual_px > 2.0:
        print("WARNING: residual > 2 px — check the frame (balls on cushions? glare?)")
    out_path = Path(args.out)
    params.save(out_path)
    map1, map2 = build_undistort_maps(params)
    preview = cv2.remap(bgr, map1, map2, cv2.INTER_LINEAR)
    # brighten linear data so the preview is judgeable by eye
    lut = (255.0 * (np.linspace(0.0, 1.0, 256) ** (1 / 2.2))).astype(np.uint8)
    preview_path = out_path.with_name("undistort_preview.png")
    cv2.imwrite(str(preview_path), cv2.LUT(preview, lut))
    print(f"wrote {out_path} and {preview_path} — check the preview: rails straight?")
    return 0


def _cmd_calibrate(args: argparse.Namespace) -> int:
    from cueclock.vision.calibration import fit_from_frame

    undistort_path = Path(args.undistort)
    if not undistort_path.exists():
        print(
            f"undistort params not found: {undistort_path} — run `fit-undistort` first",
            file=sys.stderr,
        )
        return 1
    undistort = UndistortParams.load(undistort_path)
    bgr = _load_frame_bgr(Path(args.frame))
    calibration = fit_from_frame(bgr, undistort)

    print(
        f"line residual {calibration.line_residual_px:.2f} px "
        f"(undistort fit residual {undistort.fit_residual_px:.2f} px)"
    )
    # health readout: frame center in table coords + local scale at the center
    cx, cy = undistort.cx, undistort.cy
    center, right = calibration.to_table_mm(np.array([[cx, cy], [cx + 100.0, cy]]))
    scale = float(np.hypot(*(right - center))) / 100.0
    print(f"frame center -> table ({center[0]:.0f}, {center[1]:.0f}) mm; {scale:.2f} mm/px")
    if calibration.line_residual_px > 2.0:
        print("WARNING: residual > 2 px — check the frame (balls on cushions? glare?)")
    out_path = Path(args.out)
    calibration.save(out_path)
    print(f"wrote {out_path}")
    return 0


def _cmd_analyze(args: argparse.Namespace) -> int:
    from cueclock.vision.calibration import TableCalibration
    from cueclock.vision.pipeline import VisionPipeline

    cal_path = Path(args.calibration)
    if not cal_path.exists():
        print(f"calibration not found: {cal_path} — run `calibrate` first", file=sys.stderr)
        return 1
    calibration = TableCalibration.load(cal_path)

    ref_reader = ClipReader(Path(args.reference))
    ref_indices = np.linspace(0, len(ref_reader) - 1, num=min(15, len(ref_reader)), dtype=int)
    pipeline = VisionPipeline(calibration, [ref_reader.frame(int(i)) for i in ref_indices])

    reader = ClipReader(Path(args.clip))
    n_events = 0
    next_status = 0.0
    for i in range(0, len(reader), args.stride):
        t = float(reader.timestamps[i])
        for event in pipeline.process(t, reader.frame(i)):
            n_events += 1
            print(f"{t:7.3f}s  {type(event).__name__}")
        if args.verbose and t >= next_status:
            next_status = t + 1.0
            cue = next((b for b in pipeline.last_balls if b.is_cue), None)
            cue_txt = (
                f"cue @({cue.x_mm:6.0f},{cue.y_mm:5.0f}) "
                f"v={cue.speed_mms:6.0f}/{cue.raw_speed_mms:6.0f} mm/s"
                if cue is not None and cue.raw_speed_mms is not None
                else "no cue"
            )
            print(f"{t:7.3f}s  [{len(pipeline.last_balls):2d} balls] {cue_txt}")
    print(f"{args.clip}: {n_events} events over {float(reader.timestamps[-1]):.2f}s")
    return 0


def _warn_if_reference_stale(
    ref_gray: npt.NDArray[np.uint8],
    bayer: npt.NDArray[np.uint8],
    calibration: "TableCalibration",
) -> None:
    """One-shot startup check: does the live scene still match the reference?

    A stale reference (camera moved, lighting changed) kills detection with
    no other symptom — the clock just never starts (2026-07-29 lesson).
    """
    from cueclock.vision.calibration import BOUNDARY_DRIFT_WARN, boundary_drift

    live_gray = cast(npt.NDArray[np.uint8], cv2.cvtColor(bayer, cv2.COLOR_BayerBG2GRAY))
    drift = boundary_drift(ref_gray, live_gray, calibration)
    if drift > BOUNDARY_DRIFT_WARN:
        print(
            f"WARNING: table boundary drift {drift:.1f} vs the reference clip "
            f"(same-mount noise ~2, warn >{BOUNDARY_DRIFT_WARN:.0f}).\n"
            "  The camera or lighting changed since the reference was recorded —\n"
            "  detection may be silently blind (no events, clock never starts).\n"
            "  Fix: record a fresh empty-table clip, `fit-undistort` + `calibrate`\n"
            "  against it, and relaunch with --reference <new clip>.",
            file=sys.stderr,
        )
    else:
        print(f"reference check ok (boundary drift {drift:.1f}, noise ~2)")


def _cmd_run(args: argparse.Namespace) -> int:
    """The automated shot clock: camera + vision + clock/server + display feed.

    One process owns everything. Camera and Syphon stay on the main thread
    (the runloop must be pumped here — see syphon_out.py); uvicorn serves the
    overlay/API from a background thread; vision events cross into its asyncio
    loop via run_coroutine_threadsafe. Exclusive with `record`/`live`, and it
    replaces a standalone `cueclock.server` — stop that first or the port bind
    fails.
    """
    import asyncio
    import threading
    from concurrent.futures import Future

    import uvicorn
    from pypylon import pylon

    from cueclock.capture.camera import EXPOSURE_US, GAIN, open_camera
    from cueclock.capture.ring import FrameRing
    from cueclock.display.syphon_out import SyphonOutput
    from cueclock.server.app import create_app
    from cueclock.server.hub import Hub
    from cueclock.vision.calibration import TableCalibration
    from cueclock.vision.events import BallsSettled, ShotTaken
    from cueclock.vision.pipeline import VisionPipeline

    cal_path = Path(args.calibration)
    if not cal_path.exists():
        print(f"calibration not found: {cal_path} — run `calibrate` first", file=sys.stderr)
        return 1
    ref_path = Path(args.reference)
    if not ref_path.is_dir():
        print(f"reference clip not found: {ref_path}", file=sys.stderr)
        return 1
    calibration = TableCalibration.load(cal_path)
    ref_reader = ClipReader(ref_path)
    ref_indices = np.linspace(0, len(ref_reader) - 1, num=min(15, len(ref_reader)), dtype=int)
    ref_frames = [ref_reader.frame(int(i)) for i in ref_indices]
    vision = VisionPipeline(calibration, ref_frames)
    ref_gray = np.median(
        np.stack([cv2.cvtColor(f, cv2.COLOR_BayerBG2GRAY) for f in ref_frames]), axis=0
    ).astype(np.uint8)
    display = _build_display_pipeline(args)

    ring: FrameRing | None = None
    ring_capacity = int(args.ring_seconds * NOMINAL_FPS / args.stride)
    camera_serial = "unknown"

    def dump_ring(label: str) -> str:
        """Write the ring to a corpus-style clip (called from the server)."""
        if ring is None or len(ring) == 0:
            raise RuntimeError("ring buffer is empty — no frames grabbed yet")
        clip_dir = _unique_dir(Path("tests/clips"), label)
        writer = ClipWriter(
            clip_dir,
            label=label,
            camera_serial=camera_serial,
            exposure_us=EXPOSURE_US,
            gain=GAIN,
            recorded_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        for ts_ns, ring_frame in ring.snapshot():
            writer.write(ring_frame, ts_ns)
        writer.close()
        print(f"ring dumped -> {clip_dir}")
        return str(clip_dir)

    app = create_app()
    if ring_capacity > 0:
        app.state.dump = dump_ring
    hub = cast(Hub, app.state.hub)
    server = uvicorn.Server(
        uvicorn.Config(app, host=args.host, port=args.port, log_level="warning")
    )
    server_thread = threading.Thread(target=server.run, name="cueclock-server", daemon=True)
    server_thread.start()
    while not server.started and server_thread.is_alive():
        time.sleep(0.05)
    if not server_thread.is_alive():
        print(
            f"server failed to start on {args.host}:{args.port} — port in use? "
            "(stop any standalone `python -m cueclock.server` first)",
            file=sys.stderr,
        )
        return 1
    loop = cast("asyncio.AbstractEventLoop", app.state.loop)

    def _surface(future: "Future[None]") -> None:
        exc = future.exception()
        if exc is not None:
            print(f"clock dispatch failed: {exc!r}", file=sys.stderr)

    def dispatch(event: object, t: float) -> None:
        print(f"{t:8.3f}s  {type(event).__name__}")
        coro = None
        if isinstance(event, ShotTaken):
            coro = hub.shot_taken()
        elif isinstance(event, BallsSettled):
            coro = hub.balls_settled()
        # Scratch: logged only — no clock action in v1 (CHANGELOG.md decision)
        if coro is not None:
            asyncio.run_coroutine_threadsafe(coro, loop).add_done_callback(_surface)

    print(
        f"run -> overlay http://{args.host}:{args.port}/shotclock | control /control | "
        f"detect ~{NOMINAL_FPS / args.stride:.0f} Hz | display ~{args.fps:.0f} fps "
        f"Syphon {args.name!r}\n"
        "clock starts when your first shot settles — Ctrl-C to stop"
    )
    display_period = 1.0 / args.fps
    try:
        with open_camera() as cam:
            camera_serial = cam.GetDeviceInfo().GetSerialNumber()
            # LatestImageOnly: if a detect cycle runs long, the camera hands us
            # the freshest frame instead of building a queue. The event engine
            # is cadence-agnostic, so skipped frames only cost samples.
            cam.StartGrabbing(pylon.GrabStrategy_LatestImageOnly)
            try:
                with SyphonOutput(args.name, *display.out_size) as out:
                    t0_ns: int | None = None
                    frame_i = 0
                    next_display = 0.0
                    next_status = 5.0
                    # Detect-loop telemetry: with LatestImageOnly, retrieve rate
                    # == processing rate, so grabs/s below the camera's 156.25
                    # means frames are being skipped — the deficit is the only
                    # visible sign of the loop falling behind (checkpoint 21).
                    grabs = 0
                    detects = 0
                    detect_s = 0.0
                    detect_max_s = 0.0
                    last_status_t = 0.0
                    while True:
                        grab = cam.RetrieveResult(5000, pylon.TimeoutHandling_ThrowException)
                        try:
                            if not grab.GrabSucceeded():
                                continue
                            ts_ns = int(grab.GetTimeStamp())
                            if t0_ns is None:
                                t0_ns = ts_ns
                            t = (ts_ns - t0_ns) / 1e9
                            grabs += 1
                            frame = grab.Array
                            if frame_i == 0:
                                _warn_if_reference_stale(ref_gray, frame, calibration)
                            if frame_i % args.stride == 0:
                                if ring_capacity > 0:
                                    if ring is None:
                                        ring = FrameRing(
                                            ring_capacity, frame.shape[0], frame.shape[1]
                                        )
                                    ring.push(ts_ns, frame)
                                t_proc = time.perf_counter()
                                events = vision.process(t, frame)
                                dt_proc = time.perf_counter() - t_proc
                                detects += 1
                                detect_s += dt_proc
                                if dt_proc > detect_max_s:
                                    detect_max_s = dt_proc
                                for event in events:
                                    dispatch(event, t)
                            frame_i += 1
                            if t >= next_display:
                                next_display = t + display_period
                                out.publish(display.to_rgba(frame), pump_seconds=0.001)
                            if args.verbose and t >= next_status:
                                next_status = t + 1.0
                                cue = next((b for b in vision.last_balls if b.is_cue), None)
                                cue_txt = (
                                    f"cue @({cue.x_mm:6.0f},{cue.y_mm:5.0f}) "
                                    f"v={cue.speed_mms:6.0f} mm/s"
                                    if cue is not None
                                    else "no cue"
                                )
                                elapsed = t - last_status_t
                                last_status_t = t
                                rate = grabs / elapsed if elapsed > 0 else 0.0
                                avg_ms = detect_s / detects * 1e3 if detects else 0.0
                                print(
                                    f"{t:8.3f}s  [{len(vision.last_balls):2d} balls] "
                                    f"{cue_txt} | clock {hub.clock.state.value} | "
                                    f"{rate:5.1f} grabs/s | detect {avg_ms:.1f}ms avg "
                                    f"{detect_max_s * 1e3:.1f}ms max"
                                )
                                grabs = 0
                                detects = 0
                                detect_s = 0.0
                                detect_max_s = 0.0
                        finally:
                            grab.Release()
            finally:
                cam.StopGrabbing()
    except KeyboardInterrupt:
        pass
    finally:
        server.should_exit = True
        server_thread.join(timeout=5)
    return 0


def _sigterm_to_interrupt(signum: int, frame: FrameType | None) -> None:
    raise KeyboardInterrupt


def _add_display_args(p: argparse.ArgumentParser) -> None:
    """Args consumed by `_build_display_pipeline` — shared by replay/live/run."""
    p.add_argument("--fps", type=float, default=30.0, help="display rate (default 30)")
    p.add_argument("--name", default="cueclock", help="Syphon server name")
    p.add_argument(
        "--undistort",
        default=str(DEFAULT_UNDISTORT_PATH),
        help="undistort params JSON (default config/display_undistort.json)",
    )
    p.add_argument("--no-undistort", action="store_true", help="publish distorted frames")
    p.add_argument(
        "--rectify",
        action="store_true",
        help="render the table axis-aligned via config/table_calibration.json",
    )


def _cmd_deck(args: argparse.Namespace) -> int:
    from cueclock.deck.daemon import run_deck

    return run_deck(args.server, args.brightness)


def main(argv: list[str] | None = None) -> int:
    # `kill <pid>` unwinds like Ctrl-C: clips close cleanly, Syphon servers
    # post their retire notification instead of leaving a stale entry.
    signal.signal(signal.SIGTERM, _sigterm_to_interrupt)
    parser = argparse.ArgumentParser(prog="cueclock")
    sub = parser.add_subparsers(dest="command", required=True)

    p_record = sub.add_parser("record", help="record a raw timestamped clip")
    p_record.add_argument("--label", required=True, help="scenario name (directory name)")
    p_record.add_argument("--seconds", type=float, default=10.0, help="duration (default 10)")
    p_record.add_argument("--out", default="tests/clips", help="output root (default tests/clips)")

    p_replay = sub.add_parser("replay", help="publish a clip (or test pattern) to OBS via Syphon")
    p_replay.add_argument("--clip", help="clip directory to replay")
    p_replay.add_argument("--once", action="store_true", help="play once instead of looping")
    p_replay.add_argument("--test-pattern", action="store_true", help="labeled RGB bars instead")
    _add_display_args(p_replay)

    p_live = sub.add_parser("live", help="live camera feed to OBS via Syphon (no recording)")
    _add_display_args(p_live)

    p_fit = sub.add_parser("fit-undistort", help="fit display undistort from the table's rails")
    p_fit.add_argument("frame", help="clip directory (middle frame) or .npy frame")
    p_fit.add_argument(
        "--out",
        default=str(DEFAULT_UNDISTORT_PATH),
        help="output JSON (default config/display_undistort.json)",
    )

    p_cal = sub.add_parser("calibrate", help="fit px→mm table calibration from the rails")
    p_cal.add_argument("frame", help="empty-table clip directory (middle frame) or .npy frame")
    p_cal.add_argument(
        "--undistort",
        default=str(DEFAULT_UNDISTORT_PATH),
        help="undistort params JSON (default config/display_undistort.json)",
    )
    p_cal.add_argument(
        "--out",
        default="config/table_calibration.json",
        help="output JSON (default config/table_calibration.json)",
    )

    p_an = sub.add_parser("analyze", help="run the detection pipeline over a clip, print events")
    p_an.add_argument("--clip", required=True, help="clip directory to analyze")
    p_an.add_argument(
        "--reference",
        default="tests/clips/empty_table",
        help="empty-table clip for the background reference",
    )
    p_an.add_argument(
        "--calibration",
        default="config/table_calibration.json",
        help="table calibration JSON (default config/table_calibration.json)",
    )
    p_an.add_argument(
        "--stride", type=int, default=2, help="detect every Nth frame (default 2 ≈ 78 Hz)"
    )
    p_an.add_argument("--verbose", action="store_true", help="per-second track status lines")

    p_run = sub.add_parser(
        "run", help="automated shot clock: camera + vision drive the clock/server + display"
    )
    p_run.add_argument("--host", default="127.0.0.1", help="server bind address")
    p_run.add_argument("--port", type=int, default=8000, help="server port (default 8000)")
    p_run.add_argument(
        "--reference",
        default="tests/clips/empty_table",
        help="empty-table clip for the background reference",
    )
    p_run.add_argument(
        "--calibration",
        default="config/table_calibration.json",
        help="table calibration JSON (default config/table_calibration.json)",
    )
    p_run.add_argument(
        "--stride", type=int, default=2, help="detect every Nth frame (default 2 ≈ 78 Hz)"
    )
    p_run.add_argument(
        "--ring-seconds",
        type=float,
        default=12.0,
        help="in-RAM rolling buffer POST /api/dump saves as a labeled clip "
        "(~2.2 GB at the default 12 s; 0 disables)",
    )
    p_run.add_argument("--verbose", action="store_true", help="per-second track status lines")
    _add_display_args(p_run)

    p_deck = sub.add_parser(
        "deck", help="Stream Deck control-surface daemon (docs/hardware.md Part C)"
    )
    p_deck.add_argument(
        "--server", default="http://127.0.0.1:8000", help="cueclock server base URL"
    )
    p_deck.add_argument(
        "--brightness", type=int, default=60, help="key backlight 0-100 (default 60)"
    )

    args = parser.parse_args(argv)
    if args.command == "record":
        return _cmd_record(args)
    if args.command == "replay":
        return _cmd_replay(args)
    if args.command == "live":
        return _cmd_live(args)
    if args.command == "fit-undistort":
        return _cmd_fit_undistort(args)
    if args.command == "calibrate":
        return _cmd_calibrate(args)
    if args.command == "analyze":
        return _cmd_analyze(args)
    if args.command == "deck":
        return _cmd_deck(args)
    return _cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())
