"""Detection-cache tests on synthetic clips — no camera, no corpus, runs in CI.

The cache stands between the detector and the corpus scoreboard, so a stale
or lossy entry would not crash: it would silently change what the corpus
grades. These tests pin the three things that prevent that — exact
round-tripping, a key that moves when any input moves, and loud failure on a
corrupt or mismatched entry.
"""

import zipfile
from pathlib import Path

import numpy as np
import numpy.typing as npt
import pytest

from cueclock.capture.clip import ClipWriter
from cueclock.capture.detect_cache import DetectionCache, cache_key
from cueclock.vision.detect import DetectConfig, Detection, Intrusion
from cueclock.vision.pipeline import ReferenceModel

WIDTH, HEIGHT = 64, 48


def _clip(clip_dir: Path, frames: int = 3) -> Path:
    rng = np.random.default_rng(11)
    with ClipWriter(
        clip_dir,
        label="cache-test",
        camera_serial="0000",
        exposure_us=6000.0,
        gain=12.0,
        recorded_at="2026-08-02T00:00:00+00:00",
    ) as w:
        for i in range(frames):
            w.write(rng.integers(0, 256, size=(HEIGHT, WIDTH), dtype=np.uint8), 1_000_000 * i)
    return clip_dir


def _array(fill: int) -> npt.NDArray[np.uint8]:
    return np.full((HEIGHT, WIDTH), fill, dtype=np.uint8)


def _reference(gray: int = 10, bayer: int = 20, mask: int = 1, radius: int = 22) -> ReferenceModel:
    return ReferenceModel(
        gray=_array(gray),
        bayer=_array(bayer),
        mask=_array(mask),
        radius_px=_array(radius).astype(np.float32),
    )


def _detection(cx: float, merged: bool = False) -> Detection:
    return Detection(cx=cx, cy=2.5, area=1234.5, bbox=(1, 2, 3, 4), whiteness=0.625, merged=merged)


def _frames() -> list[tuple[list[Detection], list[float | None], list[Intrusion]]]:
    """Three frames covering the packing edge cases in one fixture.

    Frame 0 exercises the None-churn sentinel alongside a real 0.0 (they must
    not collapse — 0.0 is a measurement, None is "no predecessor"); frame 1 is
    empty (the cumsum-offset bug lives here); frame 2 carries two detections
    whose order must survive, because the tracker's association is greedy.

    The intrusion channel (#41) is packed with its own per-frame counts, so it
    varies independently of the ball counts here: 1, 0, 2 against 2, 0, 2.
    """
    return [
        (
            [_detection(1.5), _detection(2.5, merged=True)],
            [None, 0.0],
            [Intrusion(bbox=(5, 6, 70, 80), area=4321.5, core_radius_px=25.5)],
        ),
        ([], [], []),
        (
            [_detection(10.125), _detection(20.25)],
            [1.5, 2.75],
            [
                Intrusion(bbox=(1, 1, 60, 60), area=2500.25, core_radius_px=22.125),
                Intrusion(bbox=(9, 9, 90, 90), area=9000.0, core_radius_px=44.0),
            ],
        ),
    ]


def test_round_trip_preserves_detections_exactly(tmp_path: Path) -> None:
    clip = _clip(tmp_path / "clip")
    cache = DetectionCache(tmp_path / "cache")
    indices = [0, 1, 2]
    original = _frames()

    cache.store(clip, "k", indices, original)
    loaded = cache.load(clip, "k", indices)

    assert loaded == original  # frozen dataclasses: exact float equality
    assert loaded is not None
    assert loaded[0][1][0] is None and loaded[0][1][1] == 0.0


def test_lookup_misses_on_a_different_key(tmp_path: Path) -> None:
    clip = _clip(tmp_path / "clip")
    cache = DetectionCache(tmp_path / "cache")
    cache.store(clip, "key-a", [0, 1, 2], _frames())
    assert cache.load(clip, "key-b", [0, 1, 2]) is None


def test_frame_indices_must_match(tmp_path: Path) -> None:
    """Churn is sequence-dependent: a different index set is not reusable."""
    clip = _clip(tmp_path / "clip")
    cache = DetectionCache(tmp_path / "cache")
    cache.store(clip, "k", [0, 1, 2], _frames())
    with pytest.raises(ValueError, match="frame indices"):
        cache.load(clip, "k", [0, 2, 4])


def test_corrupt_entry_raises(tmp_path: Path) -> None:
    """Corrupt must fail loudly, never fall back silently to a cold run."""
    clip = _clip(tmp_path / "clip")
    cache = DetectionCache(tmp_path / "cache")
    cache.store(clip, "k", [0, 1, 2], _frames())
    entry = next((tmp_path / "cache").glob("*.npz"))
    entry.write_bytes(entry.read_bytes()[: len(entry.read_bytes()) // 2])
    with pytest.raises((zipfile.BadZipFile, ValueError, OSError, EOFError)):
        cache.load(clip, "k", [0, 1, 2])


def test_every_keyed_input_changes_the_key(tmp_path: Path) -> None:
    """A missing keyed input shows up here as a collision, which is the bug.

    Each mutator perturbs exactly one thing detection depends on; all keys
    must be distinct. `events.py` and `track.py` are deliberately absent —
    the whole point is that they may change without invalidating the cache.
    """
    clip = _clip(tmp_path / "clip")
    other = _clip(tmp_path / "other")
    base_ref, base_cfg = _reference(), DetectConfig()

    keys = {
        "base": cache_key(clip, base_ref, base_cfg),
        "config": cache_key(clip, base_ref, DetectConfig(diff_threshold=26)),
        "config-shadow": cache_key(clip, base_ref, DetectConfig(shadow_ratio_lo=0.66)),
        "ref-gray": cache_key(clip, _reference(gray=11), base_cfg),
        "ref-bayer": cache_key(clip, _reference(bayer=21), base_cfg),
        "ref-mask": cache_key(clip, _reference(mask=0), base_cfg),
        "clip": cache_key(other, base_ref, base_cfg),
    }
    assert len(set(keys.values())) == len(keys), keys


def test_key_tracks_frame_bytes_not_just_metadata(tmp_path: Path) -> None:
    """meta.json does not pin pixel content, so the key tracks the file too.

    Real corpus evidence for why metadata is not enough:
    `medium_shot/meta.json` carries `"label": "soft_shot"`, and every flag
    dump shares `"label": "flagged"` with the same frame count. The key
    therefore includes `frames.raw`'s size and mtime — not a content hash of
    134 GB — so a rewritten clip cannot read as a hit even at identical
    length and identical metadata.
    """
    clip = _clip(tmp_path / "clip")
    before = cache_key(clip, _reference(), DetectConfig())
    raw = clip / "frames.raw"
    data = bytearray(raw.read_bytes())
    data[0] ^= 0xFF  # same length, same meta.json, different pixels
    raw.write_bytes(bytes(data))
    assert cache_key(clip, _reference(), DetectConfig()) != before


def test_reference_digest_is_content_addressed() -> None:
    """Same arrays → same digest; any array differing → different digest."""
    assert _reference().digest() == _reference().digest()
    assert _reference().digest() != _reference(mask=0).digest()
