"""Round-trip tests for the raw clip container (no camera required)."""

from pathlib import Path

import numpy as np
import pytest

from cueclock.capture.clip import ClipReader, ClipWriter


def _writer(clip_dir: Path) -> ClipWriter:
    return ClipWriter(
        clip_dir,
        label="test",
        camera_serial="0000",
        exposure_us=6000.0,
        gain=12.0,
        recorded_at="2026-07-27T00:00:00+00:00",
    )


def test_round_trip(tmp_path: Path) -> None:
    rng = np.random.default_rng(7)
    frames = rng.integers(0, 256, size=(3, 4, 6), dtype=np.uint8)
    ts_ns = [1_000_000_000, 1_006_400_000, 1_012_800_000]

    with _writer(tmp_path / "clip") as w:
        for f, t in zip(frames, ts_ns, strict=True):
            w.write(f, t)

    r = ClipReader(tmp_path / "clip")
    assert len(r) == 3
    assert r.meta.width == 6 and r.meta.height == 4
    assert r.meta.first_timestamp_ns == ts_ns[0]
    np.testing.assert_array_equal(r.frame(2), frames[2])
    np.testing.assert_allclose(r.timestamps, [0.0, 0.0064, 0.0128])
    replayed = list(r)
    np.testing.assert_array_equal(replayed[1][0], frames[1])
    assert replayed[1][1] == pytest.approx(0.0064)


def test_writer_rejects_bad_frames(tmp_path: Path) -> None:
    w = _writer(tmp_path / "clip")
    w.write(np.zeros((4, 6), dtype=np.uint8), 0)
    with pytest.raises(ValueError, match="shape"):
        w.write(np.zeros((4, 8), dtype=np.uint8), 1)
    with pytest.raises(ValueError, match="uint8"):
        w.write(np.zeros((4, 6), dtype=np.uint16), 2)  # type: ignore[arg-type]
    w.close()


def test_interrupted_clip_is_valid(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError), _writer(tmp_path / "clip") as w:
        w.write(np.zeros((4, 6), dtype=np.uint8), 5_000_000_000)
        raise RuntimeError("camera unplugged")
    r = ClipReader(tmp_path / "clip")
    assert len(r) == 1
    assert r.meta.first_timestamp_ns == 5_000_000_000


def test_empty_clip(tmp_path: Path) -> None:
    w = _writer(tmp_path / "clip")
    w.close()
    r = ClipReader(tmp_path / "clip")
    assert len(r) == 0
    assert list(r) == []
