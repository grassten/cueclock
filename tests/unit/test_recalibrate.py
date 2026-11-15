"""`cueclock recalibrate` (#49): the reject paths, the swap and the pointer.

No camera: `capture` is injected, and writes a synthetic clip of a blue cloth
rectangle on a dark surround. That is enough for both real fits to run, so
the checks are the production ones and not stand-ins.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import numpy.typing as npt
import pytest

from cueclock.capture.clip import ClipReader, ClipWriter
from cueclock.deck.recal import Recalibrator, parse_result
from cueclock.recalibrate import (
    BACKUP_DIR_NAME,
    CALIBRATION_NAME,
    POINTER_NAME,
    UNDISTORT_NAME,
    Result,
    ball_holes,
    read_pointer,
    recalibrate,
    write_pointer,
)

H, W = 1200, 1920
NOW = datetime(2026, 11, 15, 14, 0, 0)


def table_bgr(*, balls: int = 0, bump: bool = False) -> npt.NDArray[np.uint8]:
    """A straight-railed table: blue cloth inside a dark cushion surround."""
    bgr = np.full((H, W, 3), 20, np.uint8)
    bgr[200:1000, 160:1760] = (180, 90, 40)  # B, G, R
    for i in range(balls):
        cv2.circle(bgr, (500 + 200 * i, 600), 28, (230, 230, 230), -1)
    if bump:
        # A dent in the top rail: the cloth edge stops being a straight line.
        bgr[200:260, 600:1300] = 20
    return bgr


def mosaic(bgr: npt.NDArray[np.uint8]) -> npt.NDArray[np.uint8]:
    """BGR → the camera's Bayer layout (round-trips through COLOR_BayerBG2BGR)."""
    bayer = bgr[..., 1].copy()
    bayer[0::2, 0::2] = bgr[0::2, 0::2, 2]
    bayer[1::2, 1::2] = bgr[1::2, 1::2, 0]
    return bayer


def writer_for(bgr: npt.NDArray[np.uint8], frames: int = 4):
    def capture(clip_dir: Path, label: str) -> None:
        with ClipWriter(
            clip_dir,
            label=label,
            camera_serial="test",
            exposure_us=1000.0,
            gain=0.0,
            recorded_at="2026-11-15T14:00:00+00:00",
            cadence_hz=156.25,
        ) as writer:
            for i in range(frames):
                writer.write(mosaic(bgr), i * 6_400_000)

    return capture


@pytest.fixture
def rig(tmp_path: Path) -> dict[str, Path]:
    config = tmp_path / "config"
    config.mkdir()
    return {
        "config": config,
        "refs": tmp_path / "refs",
        "corpus": tmp_path / "clips",  # absent unless a test makes it: T7 unmounted
    }


def run(rig: dict[str, Path], capture) -> Result:  # type: ignore[no-untyped-def]
    return recalibrate(
        capture,
        config_dir=rig["config"],
        reference_root=rig["refs"],
        corpus_dir=rig["corpus"],
        now=lambda: NOW,
    )


def snapshot(config: Path) -> dict[str, str]:
    return {p.name: p.read_text() for p in config.iterdir() if p.is_file()}


# --- the pointer ---


def test_pointer_round_trips_and_a_missing_one_reads_none(tmp_path: Path) -> None:
    assert read_pointer(tmp_path) is None
    write_pointer(tmp_path / POINTER_NAME, Path("/refs/empty_table_1115"), residual_px=0.4)
    assert read_pointer(tmp_path) == Path("/refs/empty_table_1115")
    (tmp_path / POINTER_NAME).write_text("{not json")
    assert read_pointer(tmp_path) is None


# --- reject paths: nothing on disk changes ---


def test_balls_on_the_cloth_are_refused(rig: dict[str, Path]) -> None:
    before = snapshot(rig["config"])
    result = run(rig, writer_for(table_bgr(balls=2)))
    assert not result.ok and result.reason == "BALLS ON CLOTH"
    assert snapshot(rig["config"]) == before
    assert [p.name for p in rig["refs"].iterdir()] == []  # staging cleaned up


def test_the_ball_hole_threshold_ignores_the_spots() -> None:
    # The cloth's own marks are small holes in the mask (<= 291 px measured);
    # a ball is not (500+ px). A 6 px spot must not read as a ball.
    spotted = table_bgr()
    cv2.circle(spotted, (500, 600), 6, (230, 230, 230), -1)
    assert ball_holes(spotted) == 0
    assert ball_holes(table_bgr(balls=3)) == 3


def test_a_bent_rail_fails_the_fit_and_changes_nothing(rig: dict[str, Path]) -> None:
    before = snapshot(rig["config"])
    result = run(rig, writer_for(table_bgr(bump=True)))
    assert not result.ok and result.reason.startswith("BAD FIT")
    assert result.residual_px is not None and result.residual_px > 2.0
    assert snapshot(rig["config"]) == before


def test_no_table_in_frame_is_refused(rig: dict[str, Path]) -> None:
    result = run(rig, writer_for(np.full((H, W, 3), 20, np.uint8)))
    assert not result.ok and result.reason == "NO TABLE"


def test_a_camera_failure_reports_its_first_line(rig: dict[str, Path]) -> None:
    def busy(clip_dir: Path, label: str) -> None:
        raise RuntimeError("CAMERA BUSY\nthe device is controlled by another application")

    result = run(rig, busy)
    assert not result.ok and result.reason == "CAMERA BUSY"


# --- success: the swap ---


def test_success_swaps_all_three_and_keeps_a_backup(rig: dict[str, Path]) -> None:
    config = rig["config"]
    (config / UNDISTORT_NAME).write_text('{"old": "undistort"}')
    (config / CALIBRATION_NAME).write_text('{"old": "calibration"}')
    write_pointer(config / POINTER_NAME, Path("/Volumes/T7/empty_table_0801"))

    result = run(rig, writer_for(table_bgr()))

    assert result.ok, result.reason
    assert result.residual_px is not None and result.residual_px <= 2.0
    assert result.drift_after is not None and result.drift_after < 1.0  # same frames
    reference = rig["refs"] / "empty_table_1115"
    assert result.reference == str(reference)
    assert read_pointer(config) == reference
    assert len(ClipReader(reference)) == 4  # the captured clip, whole
    # New fits are real files the loaders accept, not the old contents.
    assert "old" not in json.loads((config / UNDISTORT_NAME).read_text())
    assert "homography" in json.loads((config / CALIBRATION_NAME).read_text())
    # The previous set survives, whole.
    backup = config / BACKUP_DIR_NAME
    assert json.loads((backup / UNDISTORT_NAME).read_text()) == {"old": "undistort"}
    assert json.loads((backup / CALIBRATION_NAME).read_text()) == {"old": "calibration"}
    assert read_pointer(backup) == Path("/Volumes/T7/empty_table_0801")
    assert result.corpus_copy is None  # the T7 is "unmounted" here


def test_old_references_are_pruned_but_the_previous_one_is_kept(rig: dict[str, Path]) -> None:
    first = run(rig, writer_for(table_bgr()))
    second = run(rig, writer_for(table_bgr()))
    third = run(rig, writer_for(table_bgr()))
    assert first.ok and second.ok and third.ok
    names = sorted(p.name for p in rig["refs"].iterdir() if not p.name.startswith("."))
    # Same day three times: unique names, and only the last two remain.
    assert names == ["empty_table_1115_2", "empty_table_1115_3"]
    assert read_pointer(rig["config"]) == rig["refs"] / "empty_table_1115_3"


def test_a_mounted_corpus_gets_a_copy(rig: dict[str, Path]) -> None:
    rig["corpus"].mkdir()
    result = run(rig, writer_for(table_bgr()))
    assert result.ok
    assert result.corpus_copy == str(rig["corpus"] / "empty_table_1115")
    assert sorted(p.name for p in Path(result.corpus_copy).iterdir()) == sorted(
        p.name for p in (rig["refs"] / "empty_table_1115").iterdir()
    )


# --- the deck side: the command's JSON line → the key face ---


def test_the_deck_reads_the_last_json_line_and_a_crash_is_a_failure() -> None:
    ok = Result(ok=True, reason="OK", residual_px=0.43, drift_after=1.9)
    parsed = parse_result(f"recording 3s\nrecalibrated ...\n{ok.to_json()}\n")
    assert parsed.ok and parsed.residual_px == 0.43 and parsed.drift_after == 1.9
    assert not parse_result("Traceback (most recent call last):\n  ...").ok


class FakeProc:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.done = False

    def poll(self) -> int | None:
        return 0 if self.done else None

    def communicate(self) -> tuple[str, str]:
        return self.stdout, ""


def test_the_key_shows_running_then_the_result_until_the_next_hold() -> None:
    proc = FakeProc(Result(ok=True, reason="OK", residual_px=0.43, drift_after=1.9).to_json())
    recal = Recalibrator(spawn=lambda cmd: proc)
    assert (recal.face().text, recal.face().sub) == ("RECAL", "HOLD 2s")
    assert recal.start() and not recal.start()  # one at a time
    assert recal.busy and recal.face().text == "RECAL…"
    assert not recal.poll()  # still running
    proc.done = True
    assert recal.poll() and not recal.poll()  # finished: reported exactly once
    face = recal.face()
    assert face.text == "OK 0.4PX\nDRIFT 2" and face.sub == "HOLD 2s"


def test_a_refusal_stays_on_the_key_in_red() -> None:
    proc = FakeProc(Result(ok=False, reason="BALLS ON CLOTH").to_json())
    proc.done = True
    recal = Recalibrator(spawn=lambda cmd: proc)
    recal.start()
    recal.poll()
    face = recal.face()
    assert face.text == "BALLS\nON CLOTH" and face.bg != (0, 0, 0)
