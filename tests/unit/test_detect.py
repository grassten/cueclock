"""Detector tests on synthetic Bayer scenes — no camera, no clips.

The scenes encode the §2.3 lessons: streaked balls must be detected
(lesson 2 — no circularity filtering), cue sticks and arms must not be,
and whiteness must separate the cue ball from a yellow object ball.
"""

import numpy as np
import numpy.typing as npt

from cueclock.vision.detect import DetectConfig, Detection, build_reference, detect_balls

CLOTH_BGR = (150, 60, 20)  # dark blue cloth, linear-ish sensor values
WIDTH, HEIGHT = 960, 600


def _mosaic(bgr: npt.NDArray[np.uint8]) -> npt.NDArray[np.uint8]:
    """Sample a BGR image into a BayerRG8 mosaic (R at even/even sites)."""
    h, w = bgr.shape[:2]
    bayer = np.empty((h, w), dtype=np.uint8)
    bayer[0::2, 0::2] = bgr[0::2, 0::2, 2]  # R
    bayer[0::2, 1::2] = bgr[0::2, 1::2, 1]  # G
    bayer[1::2, 0::2] = bgr[1::2, 0::2, 1]  # G
    bayer[1::2, 1::2] = bgr[1::2, 1::2, 0]  # B
    return bayer


def _cloth_scene() -> npt.NDArray[np.uint8]:
    img = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
    img[:, :] = CLOTH_BGR
    return img


def _reference() -> npt.NDArray[np.uint8]:
    return build_reference([_mosaic(_cloth_scene())])


def _ball(
    img: npt.NDArray[np.uint8], x: int, y: int, bgr: tuple[int, int, int], radius: int = 21
) -> None:
    import cv2

    cv2.circle(img, (x, y), radius, bgr, -1)


def test_empty_scene_yields_nothing() -> None:
    assert detect_balls(_mosaic(_cloth_scene()), _reference()) == []


def test_detects_balls_and_ranks_cue_by_whiteness() -> None:
    img = _cloth_scene()
    _ball(img, 300, 200, (230, 230, 230))  # cue: bright, unsaturated
    _ball(img, 600, 400, (20, 200, 210))  # yellow object ball
    detections = detect_balls(_mosaic(img), _reference())
    assert len(detections) == 2
    by_x = sorted(detections, key=lambda d: d.cx)
    assert np.isclose(by_x[0].cx, 300, atol=2) and np.isclose(by_x[0].cy, 200, atol=2)
    assert np.isclose(by_x[1].cx, 600, atol=2) and np.isclose(by_x[1].cy, 400, atol=2)
    assert by_x[0].whiteness > by_x[1].whiteness + 0.2


def test_detects_black_ball_on_cloth() -> None:
    img = _cloth_scene()
    _ball(img, 500, 300, (12, 12, 12))
    detections = detect_balls(_mosaic(img), _reference())
    assert len(detections) == 1
    assert np.isclose(detections[0].cx, 500, atol=2)


def test_detects_streaked_ball() -> None:
    """Lesson 2: a fast ball smears into an elongated blob — still a ball."""
    import cv2

    img = _cloth_scene()
    cv2.ellipse(img, (400, 300), (60, 21), 0.0, 0.0, 360.0, (230, 230, 230), -1)
    detections = detect_balls(_mosaic(img), _reference())
    assert len(detections) == 1
    assert np.isclose(detections[0].cx, 400, atol=3)


def test_rejects_cue_stick_and_arm() -> None:
    import cv2

    img = _cloth_scene()
    cv2.rectangle(img, (200, 100), (212, 500), (90, 120, 160), -1)  # cue: 12 px thin
    cv2.rectangle(img, (500, 150), (800, 450), (100, 140, 180), -1)  # arm: huge
    assert detect_balls(_mosaic(img), _reference()) == []


def test_table_mask_excludes_outside_blobs() -> None:
    img = _cloth_scene()
    _ball(img, 100, 100, (230, 230, 230))
    _ball(img, 700, 500, (230, 230, 230))
    mask = np.zeros((HEIGHT, WIDTH), dtype=np.uint8)
    mask[:, WIDTH // 2 :] = 1  # only the right half is "table"
    detections = detect_balls(_mosaic(img), _reference(), table_mask=mask)
    assert len(detections) == 1
    assert detections[0].cx > WIDTH / 2


def test_reference_median_removes_transient() -> None:
    frames = [_mosaic(_cloth_scene()) for _ in range(3)]
    transient = _cloth_scene()
    _ball(transient, 300, 300, (230, 230, 230))
    frames.append(_mosaic(transient))
    reference = build_reference(frames)
    assert detect_balls(_mosaic(_cloth_scene()), reference) == []


def test_detection_is_frozen_dataclass() -> None:
    d = Detection(cx=1.0, cy=2.0, area=3.0, bbox=(0, 0, 1, 1), whiteness=0.5)
    try:
        d.cx = 5.0  # type: ignore[misc]
        raise AssertionError("Detection must be frozen")
    except AttributeError:
        pass


def test_config_defaults_reject_nothing_ball_sized() -> None:
    cfg = DetectConfig()
    nominal_area = float(np.pi * 21**2)
    assert cfg.min_area_px < nominal_area < cfg.max_area_px
