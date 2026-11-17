"""The ball list (#54) on synthetic frames, and everything downstream of it:
what counts as a changed list, the strip the hub serves, the switch and the
session-log record. No camera, no clips."""

import json
from pathlib import Path

import cv2
import numpy as np
import numpy.typing as npt
from fastapi.testclient import TestClient

from cueclock.capture.session_log import SessionLog, SessionLogWriter
from cueclock.server import create_app
from cueclock.server.hub import Hub
from cueclock.vision.ball_list import BallList, ListedBall, read_ball_list, same_table
from cueclock.vision.detect import ChromaReference, build_reference
from cueclock.vision.pipeline import ReferenceModel

WIDTH, HEIGHT = 960, 600
CLOTH_BGR = (190, 120, 40)  # blue cloth
R_PRED = 24.0  # predicted radius everywhere; visible edge ~20 px
WHITE = (235, 235, 235)
YELLOW = (40, 200, 235)  # BGR, nearest the yellow centroid (1 and 9)


def _mosaic(bgr: npt.NDArray[np.uint8]) -> npt.NDArray[np.uint8]:
    """BGR into a BayerRG8 mosaic (R at even/even), as the camera delivers."""
    bayer = np.empty(bgr.shape[:2], dtype=np.uint8)
    bayer[0::2, 0::2] = bgr[0::2, 0::2, 2]
    bayer[0::2, 1::2] = bgr[0::2, 1::2, 1]
    bayer[1::2, 0::2] = bgr[1::2, 0::2, 1]
    bayer[1::2, 1::2] = bgr[1::2, 1::2, 0]
    return bayer


def _cloth() -> npt.NDArray[np.uint8]:
    img = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
    img[:, :] = CLOTH_BGR
    return img


def _reference() -> ReferenceModel:
    empty = _mosaic(_cloth())
    mask = np.zeros((HEIGHT, WIDTH), np.uint8)
    mask[40:-40, 40:-40] = 1
    return ReferenceModel(
        gray=build_reference([empty]),
        bayer=empty,
        mask=mask,
        radius_px=np.full((HEIGHT, WIDTH), R_PRED, np.float32),
        chroma=ChromaReference.build([empty], mask),
    )


def test_reads_cue_solid_stripe_and_a_white_cap_with_no_number() -> None:
    img = _cloth()
    cv2.circle(img, (200, 300), 20, WHITE, -1)  # the cue
    cv2.circle(img, (400, 300), 20, YELLOW, -1)  # the 1
    cv2.circle(img, (600, 300), 20, YELLOW, -1)  # the 9: white caps over a band
    img[280:290, 580:621] = WHITE
    img[310:321, 580:621] = WHITE
    cv2.circle(img, (600, 300), 21, CLOTH_BGR, 2)  # keep the outline round
    cv2.circle(img, (800, 300), 20, YELLOW, -1)  # a stripe resting white cap up
    cv2.circle(img, (800, 300), 16, WHITE, -1)
    found = read_ball_list(_mosaic(img), _reference())
    assert not found.arm
    by_x = {round(b.cx / 100): b for b in found.balls}
    assert sorted(by_x) == [2, 4, 6, 8]
    assert by_x[2].cue and by_x[2].rim >= 0.65
    assert by_x[4].number == 1 and not by_x[4].cue
    assert by_x[6].number == 9
    # No coloured pixel inside the disc: listed, but with no number.
    assert by_x[8].number is None and not by_x[8].cue
    assert found.labels == ["cue", "1", "9", "?"]  # strip order
    for b in found.balls:
        assert abs(b.cx - round(b.cx / 100) * 100) <= 1 and abs(b.cy - 300) <= 1


def test_an_empty_table_lists_nothing() -> None:
    assert read_ball_list(_mosaic(_cloth()), _reference()) == BallList(balls=(), arm=False)


def _ball(label: str, x: float, y: float = 100.0) -> ListedBall:
    return ListedBall(
        cx=x,
        cy=y,
        r_pred=24.0,
        cue=label == "cue",
        number=int(label) if label.isdigit() else None,
        rim=0.9 if label == "cue" else 0.3,
        white=0.1,
        margin=12.0,
        colour_px=200,
        vote=5.0,
        coverage=0.9,
    )


def test_what_counts_as_a_changed_list() -> None:
    a = BallList(balls=(_ball("cue", 100), _ball("3", 200)), arm=False)
    jitter = BallList(balls=(_ball("cue", 101), _ball("3", 199)), arm=False)
    moved = BallList(balls=(_ball("cue", 120), _ball("3", 200)), arm=False)
    relabelled = BallList(balls=(_ball("cue", 100), _ball("5", 200)), arm=False)
    gone = BallList(balls=(_ball("3", 200),), arm=False)
    with_arm = BallList(balls=a.balls, arm=True)
    assert same_table(a, jitter)
    for other in (moved, relabelled, gone, with_arm):
        assert not same_table(a, other)


def test_a_list_survives_its_record() -> None:
    found = BallList(balls=(_ball("cue", 100), _ball("11", 200), _ball("?", 300)), arm=True)
    again = BallList.from_dict(json.loads(json.dumps(found.as_dict())))
    assert again.labels == ["cue", "11", "?"] and again.arm
    assert same_table(found, again)


def test_the_strip_shows_the_latest_list_without_an_arm() -> None:
    hub = Hub()
    assert hub.payload()["ball_strip"] == {"shown": True, "balls": None}
    hub.note_ball_list(["cue", "3", "3", "?"], arm=False)  # a duplicate is drawn twice
    hub.note_ball_list(["3"], arm=True)  # a hand over the table proves no absence
    assert hub.payload()["ball_strip"] == {"shown": True, "balls": ["cue", "3", "3", "?"]}


def test_the_control_page_switch_hides_the_strip() -> None:
    client = TestClient(create_app())
    client.post("/api/ball_strip/off")
    assert client.get("/api/state").json()["ball_strip"]["shown"] is False
    client.post("/api/ball_strip/on")
    assert client.get("/api/state").json()["ball_strip"]["shown"] is True


def test_lists_are_written_to_the_session_log(tmp_path: Path) -> None:
    root = tmp_path / "20261117-120000"
    writer = SessionLogWriter(root, {"stamp": root.name})
    for frame_i, labels in ((100, ["cue", "1"]), (300, ["cue"])):
        found = BallList(balls=tuple(_ball(n, 100 * k) for k, n in enumerate(labels)), arm=False)
        assert writer.ball_list({"frame_i": frame_i, "t": frame_i / 156, **found.as_dict()})
    writer.close()
    log = SessionLog(root)
    assert [r["frame_i"] for r in log.ball_lists()] == [100, 300]
    assert BallList.from_dict(log.ball_lists()[0]).labels == ["cue", "1"]
