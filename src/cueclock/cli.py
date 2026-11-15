"""cueclock command-line interface.

`run` is the product: the automated shot clock (§7 step 5) — camera + vision
drive the clock, with the overlay server and the Syphon table feed in the same
process. The rest are bring-up and tuning tools: `record` (§7 step 1),
`replay` + `live` + `fit-undistort` (display path, §7 step 6), `calibrate`
(rails homography, corpus-grade §7 step 3), `analyze` (detection offline).

Run via:  uv run python -m cueclock.cli run --rectify
"""

import argparse
import contextlib
import shutil
import signal
import sys
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from cueclock.server.hub import Hub
    from cueclock.vision.calibration import TableCalibration
    from cueclock.vision.pipeline import DetectedBlobs

    # find_blobs' result plus when it started and ended on its worker (#52).
    TimedBlobs = tuple[DetectedBlobs, float, float]

import cv2
import numpy as np
import numpy.typing as npt

from cueclock.audio.listener import DEFAULT_DEVICE as DEFAULT_MIC_DEVICE  # no vosk import
from cueclock.capture.clip import CAMERA_FPS, ClipReader, ClipWriter
from cueclock.display.pipeline import (
    DEFAULT_UNDISTORT_PATH,
    DisplayPipeline,
    UndistortParams,
    build_undistort_maps,
)

BYTES_PER_FRAME = 1920 * 1200
NOMINAL_FPS = CAMERA_FPS
DISK_HEADROOM = 5 * 1024**3  # never eat the last 5 GiB of the disk

DETECT_WORKERS = 3  # the M2 has 4 performance cores; the 4th runs the loop itself
DETECT_INFLIGHT = 2 * DETECT_WORKERS  # ~38 ms of queue at 156 Hz before the grab waits


def _unique_dir(root: Path, label: str) -> Path:
    candidate = root / label
    n = 2
    while candidate.exists():
        candidate = root / f"{label}_{n}"
        n += 1
    return candidate


def _cmd_record(args: argparse.Namespace) -> int:
    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    clip_dir = _unique_dir(out_root, args.label)
    try:
        _record_clip(clip_dir, args.label, args.seconds)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def _record_clip(clip_dir: Path, label: str, seconds: float) -> None:
    """Record `seconds` of raw frames into `clip_dir` (which must not exist).

    Raises `RuntimeError` with a short reason when the disk or the camera
    can't be used. `recalibrate` shows the reason on a deck key, so it is
    the first line of the message.
    """
    from cueclock.capture.camera import EXPOSURE_US, GAIN, open_camera, stream

    clip_dir.parent.mkdir(parents=True, exist_ok=True)
    needed = int(BYTES_PER_FRAME * NOMINAL_FPS * seconds * 1.1)
    free = shutil.disk_usage(clip_dir.parent).free
    if free < needed + DISK_HEADROOM:
        raise RuntimeError(
            f"DISK FULL\nneed ~{needed / 1e9:.1f} GB + headroom, {free / 1e9:.1f} GB free"
        )

    print(f"recording {seconds:.0f}s -> {clip_dir}  (Ctrl-C stops early, clip stays valid)")
    with contextlib.ExitStack() as stack:
        try:
            cam = stack.enter_context(open_camera())
        except Exception as exc:  # pylon raises its own genicam types
            from pypylon import pylon

            attached = bool(pylon.TlFactory.GetInstance().EnumerateDevices())
            raise RuntimeError(f"{'CAMERA BUSY' if attached else 'NO CAMERA'}\n{exc}") from exc
        writer = ClipWriter(
            clip_dir,
            label=label,
            camera_serial=cam.GetDeviceInfo().GetSerialNumber(),
            exposure_us=EXPOSURE_US,
            gain=GAIN,
            recorded_at=datetime.now(UTC).isoformat(timespec="seconds"),
            cadence_hz=NOMINAL_FPS,
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
                if elapsed >= seconds:
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


def _cmd_recalibrate(args: argparse.Namespace) -> int:
    """Record a fresh reference and refit both calibrations (`recalibrate.py`).

    `run` owns the camera while a session is up, so this refuses when the
    server answers rather than fighting it for the device. With `--json` the
    last line of stdout is the result object, which is what the deck reads.
    """
    from cueclock.deck.client import get_json
    from cueclock.recalibrate import CAPTURE_SECONDS, Result, recalibrate

    if get_json(args.server, "/api/state") is not None:
        result = Result(ok=False, reason="SESSION ON")
    else:
        result = recalibrate(
            lambda clip_dir, label: _record_clip(clip_dir, label, CAPTURE_SECONDS),
            reference_root=Path(args.reference_root),
        )
    if result.ok:
        before = "n/a" if result.drift_before is None else f"{result.drift_before:.1f}"
        print(
            f"recalibrated: residual {result.residual_px:.2f} px, drift {before} -> "
            f"{result.drift_after:.1f} (noise ~2)\n  reference {result.reference}"
            + (f"\n  corpus copy {result.corpus_copy}" if result.corpus_copy else "")
            + "\n  the next session start uses it; the previous set is in config/backup/"
        )
    else:
        print(f"not recalibrated: {result.reason} — the previous calibration is unchanged")
    if args.json:
        print(result.to_json())
    return 0 if result.ok else 1


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
    from cueclock.vision.events import BallsSettled
    from cueclock.vision.pipeline import VisionPipeline

    cal_path = Path(args.calibration)
    if not cal_path.exists():
        print(f"calibration not found: {cal_path} — run `calibrate` first", file=sys.stderr)
        return 1
    calibration = TableCalibration.load(cal_path)

    ref_reader = ClipReader(Path(args.reference))
    ref_indices = np.linspace(0, len(ref_reader) - 1, num=min(15, len(ref_reader)), dtype=int)
    reader = ClipReader(Path(args.clip))
    pipeline = VisionPipeline(
        calibration,
        [ref_reader.frame(int(i)) for i in ref_indices],
        cadence_hz=reader.cadence_hz / args.stride,
    )

    n_events = 0
    next_status = 0.0
    for i in reader.detect_indices(stride=args.stride):
        t = float(reader.timestamps[i])
        for event in pipeline.process(t, reader.frame(i)):
            n_events += 1
            extra = ""
            if isinstance(event, BallsSettled) and pipeline.last_settle_rearrange is not None:
                s = pipeline.last_settle_rearrange
                extra = (
                    f"  [rearrange: involved {s.involved} "
                    f"(moved {s.moved} / appeared {s.appeared} / vanished {s.vanished}) "
                    f"{s.moved_mm:.0f} mm]"
                )
                # #37's consistency clause: the episode's per-mover travel is
                # what vetoes an `involved 0` refusal, so triage needs to see
                # it beside the diff rather than replaying to find it.
                movers = pipeline.last_settle_movers
                if movers:
                    extra += (
                        f"  [movers {len(movers)}, "
                        f"max travel {max(m.travel_mm for m in movers):.0f} mm]"
                    )
                if pipeline.last_settle_backstop:
                    extra += "  [BACKSTOP — the engine slept through this episode]"
            print(f"{t:7.3f}s  {type(event).__name__}{extra}")
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
) -> tuple[float, bool]:
    """One-shot startup check: does the live scene still match the reference?

    A stale reference (camera moved, lighting changed) kills detection with
    no other symptom — the clock just never starts (2026-07-29 lesson).
    Returns (reading, crossed the warn level), which the hub journals and
    shows (#52): three months of these warnings went unread in a launchd log.
    """
    from cueclock.vision.calibration import BOUNDARY_DRIFT_WARN, boundary_drift

    live_gray = cast(npt.NDArray[np.uint8], cv2.cvtColor(bayer, cv2.COLOR_BayerBG2GRAY))
    drift = boundary_drift(ref_gray, live_gray, calibration)
    stale = drift > BOUNDARY_DRIFT_WARN
    if stale:
        print(
            f"WARNING: table boundary drift {drift:.1f} vs the reference clip "
            f"(same-mount noise ~2, warn >{BOUNDARY_DRIFT_WARN:.0f}).\n"
            "  The camera or lighting changed since the reference was recorded —\n"
            "  detection may be silently blind (no events, clock never starts).\n"
            "  Fix: clear the cloth, stop the session and hold RECAL on the deck\n"
            "  (or run `cueclock recalibrate`), then start again.",
            file=sys.stderr,
        )
    else:
        print(f"reference check ok (boundary drift {drift:.1f}, noise ~2)")
    return drift, stale


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
    from concurrent.futures import Future, ThreadPoolExecutor

    import uvicorn
    from pypylon import pylon

    # Import-safe despite the lazy-import rule: listener.py defers its
    # vosk/sounddevice imports to run() (docs/hardware.md Part D).
    from cueclock.audio.listener import ExtensionListener
    from cueclock.capture.camera import open_camera
    from cueclock.display.syphon_out import DisplayWorker
    from cueclock.server.app import create_app
    from cueclock.server.hub import Hub
    from cueclock.vision.calibration import TableCalibration
    from cueclock.vision.events import BallsSettled, ScratchCandidate, ScratchRefuted, ShotTaken
    from cueclock.vision.pipeline import VisionPipeline

    cal_path = Path(args.calibration)
    if not cal_path.exists():
        print(f"calibration not found: {cal_path} — run `calibrate` first", file=sys.stderr)
        return 1
    from cueclock.recalibrate import read_pointer

    ref_path = Path(args.reference) if args.reference else read_pointer()
    if ref_path is None:
        print(
            "no reference: config/reference.json is missing — run `cueclock recalibrate` "
            "(or pass --reference <empty-table clip>)",
            file=sys.stderr,
        )
        return 1
    if not ref_path.is_dir():
        print(f"reference clip not found: {ref_path}", file=sys.stderr)
        return 1
    print(f"reference {ref_path}")
    calibration = TableCalibration.load(cal_path)
    ref_reader = ClipReader(ref_path)
    ref_indices = np.linspace(0, len(ref_reader) - 1, num=min(15, len(ref_reader)), dtype=int)
    ref_frames = [ref_reader.frame(int(i)) for i in ref_indices]
    cadence_hz = NOMINAL_FPS / args.stride
    vision = VisionPipeline(calibration, ref_frames, cadence_hz=cadence_hz)
    ref_gray = np.median(
        np.stack([cv2.cvtColor(f, cv2.COLOR_BayerBG2GRAY) for f in ref_frames]), axis=0
    ).astype(np.uint8)
    display = _build_display_pipeline(args)

    # Scores persist across restarts (a mid-match crash must not lose the match).
    # So does a live bracket (#43), for the same reason and by the same means;
    # finished ones are archived beside the session journals.
    session_stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    journal_path = Path("logs") / f"session-{session_stamp}.jsonl"
    app = create_app(
        score_path=Path("config/score.json"),
        journal_path=journal_path,
        bracket_path=Path("config/bracket.json"),
        archive_dir=journal_path.parent,
    )
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

    # Voice "extension" (docs/hardware.md Part D): same Hub call as the deck
    # key, fed by the mic instead of HID. Absent model = quiet skip, so `run`
    # works unchanged on a machine that never ran scripts/fetch_vosk_model.sh.
    listener: ExtensionListener | None = None
    if not args.no_mic:
        model_dir = Path(args.mic_model)
        if not model_dir.is_dir():
            print(
                f'voice "extension" off — Vosk model not found at {model_dir} '
                "(scripts/fetch_vosk_model.sh downloads it)"
            )
        else:

            def _extension_done(future: "Future[bool]") -> None:
                exc = future.exception()
                if exc is not None:
                    print(f"voice extension dispatch failed: {exc!r}", file=sys.stderr)
                else:
                    print(f'voice: "extension" -> accepted={future.result()}')

            def _voice_extension() -> None:
                asyncio.run_coroutine_threadsafe(hub.extension(), loop).add_done_callback(
                    _extension_done
                )

            listener = ExtensionListener(
                _voice_extension, model_path=model_dir, device_name=args.mic_device
            )
            listener.start()
            # Every granted extension is announced out of the TV, whoever
            # called it — deafen the mic for the phrase so it cannot hear the
            # keyword in its own announcement (docs/hardware.md Part D).
            hub.on_extension_granted = listener.mute

    def _surface(future: "Future[None]") -> None:
        exc = future.exception()
        if exc is not None:
            print(f"clock dispatch failed: {exc!r}", file=sys.stderr)

    def dispatch(event: object, t: float) -> None:
        print(f"{t:8.3f}s  {type(event).__name__}")
        if isinstance(event, ShotTaken):
            coro = hub.shot_taken(
                t, ball_id=event.ball_id, speed_mms=event.speed_mms, newborn=event.newborn
            )
        elif isinstance(event, BallsSettled):
            # The rearrangement summary and mover evidence set by the same
            # process() call that emitted this settle — the hub's E8 break
            # adjudication and #33 settle-time shot adjudication evidence.
            coro = hub.balls_settled(
                t,
                rearrange=vision.last_settle_rearrange,
                backstop=vision.last_settle_backstop,
                movers=vision.last_settle_movers,
            )
        elif isinstance(event, ScratchCandidate):
            # Two-stage verdict (#26): the candidate also grants the #25
            # post-scratch hold, which a refutation releases; the confirmed
            # Scratch below stays journal-only.
            coro = hub.scratch_candidate(
                t,
                x_mm=event.x_mm,
                y_mm=event.y_mm,
                pocket_x_mm=event.pocket_x_mm,
                pocket_y_mm=event.pocket_y_mm,
                whiteness=event.whiteness,
                color_age_s=event.color_age_s,
            )
        elif isinstance(event, ScratchRefuted):
            coro = hub.scratch_refuted(
                t,
                witness_id=event.witness_id,
                x_mm=event.x_mm,
                y_mm=event.y_mm,
                whiteness=event.whiteness,
            )
        else:
            # Scratch (confirmed verdict): journaled only — the clock action
            # lives on the candidate/refuted pair above (#25)
            coro = hub.scratch_seen(t)
        asyncio.run_coroutine_threadsafe(coro, loop).add_done_callback(_surface)

    print(
        f"run -> overlay http://{args.host}:{args.port}/shotclock?view=bar | control /control | "
        f"detect ~{cadence_hz:.0f} Hz | display ~{args.fps:.0f} fps "
        f"Syphon {args.name!r}\n"
        "clock starts when your first shot settles — Ctrl-C to stop"
    )
    display_period = 1.0 / args.fps
    # Detection runs on worker threads (OpenCV drops the GIL; measured on the
    # M2 2026-10-17: 152 frames/s on one thread, 400-570 on three — a
    # break's busiest frames cost 15-40 ms against a 6.4 ms budget at 156 Hz).
    # Everything stateful — churn, tracker, events, hub — stays on this thread
    # in frame order, so the vision core sees exactly the sequence it would
    # see single-threaded. DETECT_INFLIGHT bounds the queue: when the pool
    # falls behind, the grab loop waits, LatestImageOnly drops frames at the
    # camera, and the grabs/s deficit shows it, as before.
    pool = ThreadPoolExecutor(DETECT_WORKERS, thread_name_prefix="cueclock-detect")
    find_blobs = vision.find_blobs
    vision.reference.table_edge  # noqa: B018 — build the memos before workers race to them
    vision.reference.table_roi  # noqa: B018
    inflight: deque[tuple[float, float, Future[TimedBlobs]]] = deque()
    detects = 0
    detect_s = 0.0
    detect_max_s = 0.0
    episode_prev = False  # #33: the latch's last exported state

    def timed_find_blobs(frame: npt.NDArray[np.uint8]) -> "TimedBlobs":
        """`find_blobs` on a worker, stamped so the heartbeat can split the time."""
        started = time.perf_counter()
        blobs = find_blobs(frame)
        return blobs, started, time.perf_counter()

    def finish(t: float, t_submit: float, job: "Future[TimedBlobs]") -> None:
        """One frame's stateful half, in frame order."""
        nonlocal detects, detect_s, detect_max_s, episode_prev
        blobs, blobs_start, blobs_end = job.result()
        loop_start = time.perf_counter()
        events = vision.advance(t, *vision.churn(*blobs))
        dt_proc = time.perf_counter() - t_submit  # grab-to-events latency
        detects += 1
        detect_s += dt_proc
        if dt_proc > detect_max_s:
            detect_max_s = dt_proc
        for event in events:
            dispatch(event, t)
        # #33 Part B: the motion-episode latch edges, posted to the loop AFTER
        # this frame's events so a same-frame ShotTaken opens the provisional
        # itself and a same-frame settle resolves it (a falling edge with a
        # settle is the settle; without one it is the engine's pickup discard).
        episode = vision.episode_active
        if episode != episode_prev:
            episode_prev = episode
            if episode:
                asyncio.run_coroutine_threadsafe(hub.episode_started(t), loop).add_done_callback(
                    _surface
                )
            elif not any(isinstance(e, BallsSettled) for e in events):
                asyncio.run_coroutine_threadsafe(hub.episode_discarded(t), loop).add_done_callback(
                    _surface
                )
        # Detect-loop heartbeat stamp (attribute-only; the Hub's tick loop
        # journals it every ~5 s). The three stages are where a frame's time
        # goes (#52): waiting for a worker, inside find_blobs, and this
        # thread's half (churn + advance + dispatch).
        hub.note_frame(
            t,
            balls=len(vision.last_balls),
            stages=(
                blobs_start - t_submit,
                blobs_end - blobs_start,
                time.perf_counter() - loop_start,
            ),
        )
        # E9 motion bit — same attribute-only contract.
        hub.note_motion(vision.last_motion)

    try:
        with open_camera() as cam:
            # LatestImageOnly: if a detect cycle runs long, the camera hands us
            # the freshest frame instead of building a queue. The event engine
            # is cadence-agnostic, so skipped frames only cost samples.
            cam.StartGrabbing(pylon.GrabStrategy_LatestImageOnly)
            try:
                # Publishing runs on its own thread: inline at 60 fps it cost
                # the detect loop a third of its cadence (77 -> 51 Hz,
                # 2026-08-07). The loop below only copies the Bayer frame.
                with DisplayWorker(display.to_rgba, args.name, *display.out_size) as out:
                    t0_ns: int | None = None
                    frame_i = 0
                    next_display = 0.0
                    next_status = 5.0
                    # Detect-loop telemetry: with LatestImageOnly, retrieve rate
                    # == processing rate, so grabs/s below the camera's 156.25
                    # means frames are being skipped — the deficit is the only
                    # visible sign of the loop falling behind (checkpoint 21).
                    grabs = 0
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
                                hub.note_reference_drift(
                                    *_warn_if_reference_stale(ref_gray, frame, calibration)
                                )
                            if frame_i % args.stride == 0:
                                if len(inflight) >= DETECT_INFLIGHT:
                                    finish(*inflight.popleft())  # the pool is behind: wait
                                job = pool.submit(timed_find_blobs, frame)
                                inflight.append((t, time.perf_counter(), job))
                                while inflight and inflight[0][2].done():
                                    finish(*inflight.popleft())
                            frame_i += 1
                            if t >= next_display:
                                next_display = t + display_period
                                out.offer(frame)
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
        pool.shutdown(wait=False, cancel_futures=True)
        if listener is not None:
            listener.stop()
        server.should_exit = True
        server_thread.join(timeout=5)
        _log_session_summary(hub, journal_path)
    return 0


def _log_session_summary(hub: "Hub", journal_path: Path) -> None:
    """Print this session's miss rate and journal it, on the way out.

    Pure convenience: the number is *derived* from records already on disk
    (`server/report.py`), so a session killed hard loses nothing — `cueclock
    report` reconstructs the same answer from the same file. This exists so the
    number is in front of you when you hold the Session key, without ever
    asking you to press anything during play.
    """
    from cueclock.server.report import SUMMARY_KIND, build, format_session, summary_detail

    try:
        report = build(journal_path)
    except OSError:
        return  # no journal (or unreadable): nothing to summarise, never fatal
    if not report.total:
        return
    hub.journal.log(SUMMARY_KIND, **summary_detail(report))
    print()
    print(format_session(report))


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

    return run_deck(args.server, args.brightness, args.reference)


def _cmd_listen(args: argparse.Namespace) -> int:
    """Voice bring-up / false-positive soak tool (docs/hardware.md Part D).

    Prints every finalized recognition with its confidence and verdict; with
    --post, accepted hits also drive a running clock — the end-to-end check.
    """
    from cueclock.audio.listener import ExtensionListener, input_devices
    from cueclock.audio.spotter import Final

    if args.devices:
        for index, name in input_devices():
            print(f"{index:3d}  {name}")
        return 0

    model_dir = Path(args.mic_model)
    if not model_dir.is_dir():
        print(
            f"Vosk model not found at {model_dir} — run scripts/fetch_vosk_model.sh",
            file=sys.stderr,
        )
        return 1

    def _show(final: Final) -> None:
        verdict = "ACCEPTED" if final.accepted else f"rejected: {final.reason}"
        print(f"heard {final.text!r}  conf={final.confidence:.2f}  {verdict}")

    def _on_accept() -> None:
        if args.post is None:
            return
        import urllib.request

        # B310: --post is the operator's own clock API URL (http://localhost), never file:
        try:
            with urllib.request.urlopen(  # nosec B310
                urllib.request.Request(args.post, method="POST"), timeout=3.0
            ) as resp:
                print(f"  POST {args.post} -> {resp.status}")
        except OSError as exc:  # URLError/HTTPError both land here
            print(f"  POST {args.post} failed: {exc}", file=sys.stderr)

    listener = ExtensionListener(
        _on_accept, model_path=model_dir, device_name=args.mic_device, on_final=_show
    )
    print('say "extension" — Ctrl-C to stop')
    with contextlib.suppress(KeyboardInterrupt):
        listener.run()
    return 0


def _add_mic_args(p: argparse.ArgumentParser) -> None:
    """Args shared by `run` and `listen` (voice "extension", docs/hardware.md Part D)."""
    # Defaulted, not required (2026-08-03): the deck's session key spawns a
    # fixed argv (`deck/session.py` run_command) with no room for flags, so a
    # required flag meant deck-started sessions — the normal way to start one —
    # were silently deaf. The default is still a *name*, resolved by the same
    # exact-then-substring match, and still never "system default": an absent
    # device disables the listener with a printed reason rather than binding
    # whatever else is plugged in. `--no-mic` opts out.
    p.add_argument(
        "--mic-device",
        default=DEFAULT_MIC_DEVICE,
        help=f"mic CoreAudio name, exact then substring match (default {DEFAULT_MIC_DEVICE!r})",
    )
    p.add_argument(
        "--mic-model",
        default="models/vosk-model-small-en-us-0.15",
        help="Vosk model directory (default models/vosk-model-small-en-us-0.15)",
    )


def _cmd_report(args: argparse.Namespace) -> int:
    """Miss rate from the session journals — the clock's own compensation, read back."""
    from cueclock.server.report import (
        build,
        find_journals,
        format_rollup,
        format_session,
        parse_since,
    )

    if args.journal is not None:
        if not args.journal.exists():
            print(f"no such journal: {args.journal}", file=sys.stderr)
            return 2
        print(format_session(build(args.journal)))
        return 0

    if not args.logs.is_dir():
        print(f"no such logs directory: {args.logs}", file=sys.stderr)
        return 2
    since_s: float | None = None
    if args.since is not None:
        try:
            since_s = parse_since(args.since)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    journals = find_journals(args.logs, since_s)
    if not journals:
        print(f"no session journals in {args.logs}", file=sys.stderr)
        return 2
    if since_s is None and not args.all:
        print(format_session(build(journals[-1])))  # newest session
        return 0
    print(format_rollup([build(path) for path in journals]))
    return 0


def _cmd_sidecam(args: argparse.Namespace) -> int:
    """Side-angle camera settings: show them, save them, or put them back."""
    from cueclock.deck import sidecam

    report = sidecam.read()
    if report is None:
        return 2
    camera, current = report.get("camera", {}), report.get("settings", {})
    saved = sidecam.load_config()

    if args.action == "save":
        wanted = sidecam.wanted_from(current)
        sidecam.save_config(wanted)
        print(f"saved {len(wanted)} settings to {sidecam.CONFIG_PATH}")
        for key, value in wanted.items():
            print(f"  {key} = {value}")
        return 0
    if args.action == "apply":
        if not saved:
            print("nothing saved — `cueclock sidecam save` first", file=sys.stderr)
            return 2
        refused = sidecam.apply(saved)
        if refused is None:
            return 2
        for line in refused:
            print(f"refused: {line}", file=sys.stderr)
        print(f"applied {len(saved) - len(refused)}/{len(saved)} saved settings")
        return 1 if refused else 0

    print(
        f"{camera.get('name')}  firmware {camera.get('firmware')}  "
        f"stream {'open' if camera.get('stream_open') else 'closed'}"
    )
    for key, value in current.items():
        note = ""
        if saved is not None and key in saved and saved[key] != value:
            note = f"   (saved: {saved[key]})"
        print(f"  {key:16} {value}{note}")
    if saved is None:
        print("nothing saved — `cueclock sidecam save` captures the above")
    return 0


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
        "--stride",
        type=int,
        default=1,
        help="detect every Nth stored frame (default 1: the clip's own cadence — "
        "record clips and dumps since checkpoint 106 store 156 fps, older dumps "
        "the 78 Hz stream they were detected at)",
    )
    p_an.add_argument("--verbose", action="store_true", help="per-second track status lines")

    p_run = sub.add_parser(
        "run", help="automated shot clock: camera + vision drive the clock/server + display"
    )
    p_run.add_argument("--host", default="127.0.0.1", help="server bind address")
    p_run.add_argument("--port", type=int, default=8000, help="server port (default 8000)")
    p_run.add_argument(
        "--reference",
        help="empty-table clip for the background reference "
        "(default: the one config/reference.json names, written by `recalibrate`)",
    )
    p_run.add_argument(
        "--calibration",
        default="config/table_calibration.json",
        help="table calibration JSON (default config/table_calibration.json)",
    )
    p_run.add_argument(
        "--stride",
        type=int,
        default=1,
        help="detect every Nth camera frame (default 1 = 156 Hz; 2 = the 78 Hz "
        "cadence every constant was tuned at, before checkpoint 106)",
    )
    p_run.add_argument("--verbose", action="store_true", help="per-second track status lines")
    p_run.add_argument("--no-mic", action="store_true", help='disable the voice "extension" mic')
    _add_mic_args(p_run)
    _add_display_args(p_run)

    p_listen = sub.add_parser(
        "listen", help='voice "extension" bring-up/soak: print every recognition + verdict'
    )
    p_listen.add_argument("--devices", action="store_true", help="list input devices and exit")
    p_listen.add_argument(
        "--post", help="URL to POST on accepted hits (e.g. http://localhost:8000/api/extension)"
    )
    _add_mic_args(p_listen)

    p_report = sub.add_parser(
        "report",
        help="accuracy report: missed strikes per session, read back from the journals",
    )
    p_report.add_argument(
        "--journal", type=Path, help="one session journal .jsonl (default: the newest)"
    )
    p_report.add_argument(
        "--logs", type=Path, default=Path("logs"), help="journal directory (default logs/)"
    )
    p_report.add_argument(
        "--since",
        help="roll up every session in a recent span, e.g. 7d / 24h / 2w",
    )
    p_report.add_argument(
        "--all", action="store_true", help="roll up every session journal on disk"
    )

    p_sidecam = sub.add_parser(
        "sidecam",
        help="side-angle camera settings: show, save the current ones, or apply the saved ones",
    )
    p_sidecam.add_argument("action", choices=["show", "save", "apply"], nargs="?", default="show")

    p_deck = sub.add_parser(
        "deck", help="Stream Deck control-surface daemon (docs/hardware.md Part C)"
    )
    p_deck.add_argument(
        "--server", default="http://127.0.0.1:8000", help="cueclock server base URL"
    )
    p_deck.add_argument(
        "--brightness", type=int, default=60, help="key backlight 0-100 (default 60)"
    )
    p_deck.add_argument(
        "--reference",
        help=(
            "pin the empty-table reference the Session key launches `run` with "
            "(default: none, so `run` reads config/reference.json and a RECAL hold "
            "takes effect on the next start)"
        ),
    )

    p_recal = sub.add_parser(
        "recalibrate",
        help="record a fresh empty-table reference and refit undistort + calibration",
    )
    p_recal.add_argument(
        "--server",
        default="http://127.0.0.1:8000",
        help="refuse while a session answers here (it owns the camera)",
    )
    p_recal.add_argument(
        "--reference-root",
        default="~/cueclock-reference",
        help="where the live reference is kept (default ~/cueclock-reference, internal disk)",
    )
    p_recal.add_argument(
        "--json", action="store_true", help="print the result as JSON on the last line"
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
    if args.command == "recalibrate":
        return _cmd_recalibrate(args)
    if args.command == "analyze":
        return _cmd_analyze(args)
    if args.command == "listen":
        return _cmd_listen(args)
    if args.command == "report":
        return _cmd_report(args)
    if args.command == "deck":
        return _cmd_deck(args)
    if args.command == "sidecam":
        return _cmd_sidecam(args)
    return _cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())
