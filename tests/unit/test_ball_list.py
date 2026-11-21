"""The ball list (#54) on synthetic frames, and everything downstream of it:
what counts as a changed list, the strip the hub serves, the switch and the
session-log record. No camera, no clips."""

import asyncio
import dataclasses
import json
from pathlib import Path

import cv2
import numpy as np
import numpy.typing as npt
from fastapi.testclient import TestClient

from cueclock.capture.session_log import SessionLog, SessionLogWriter
from cueclock.server import create_app
from cueclock.server.hub import Hub
from cueclock.vision.ball_list import (
    CENTROIDS,
    BallList,
    ListedBall,
    assign_numbers,
    read_ball_list,
    same_table,
    stripe_evidence,
)
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


def test_the_cue_is_listed_once() -> None:
    """Two circles pass the cue test: the cue ball, and a white disc with a
    dark notch in it (the logo on a sleeve, sessions 20261119-020755 and
    20261121-153046). The whitest is the cue; the other is not a ball."""
    img = _cloth()
    cv2.circle(img, (600, 300), 20, WHITE, -1)  # a logo: white at the rim ...
    cv2.rectangle(img, (594, 294), (606, 306), (20, 20, 20), -1)  # ... dark inside
    cv2.circle(img, (200, 300), 20, WHITE, -1)  # the cue
    cv2.circle(img, (400, 300), 20, YELLOW, -1)  # the 1
    found = read_ball_list(_mosaic(img), _reference())
    assert found.labels == ["cue", "1"] and found.cues_dropped == 1
    assert abs(found.balls[0].cx - 200) <= 1
    assert BallList.from_dict(json.loads(json.dumps(found.as_dict()))).cues_dropped == 1
    hub = Hub()
    asyncio.run(hub.ball_list(1.0, "rest", found))
    (line,) = [r for r in hub.journal.tail(10_000.0) if r["kind"] == "ball_list"]
    assert line["balls"] == "cue 1" and line["cues_dropped"] == 1
    # Alone, the same disc is still a cue: a cue ball in a hand reads no whiter.
    alone = _cloth()
    cv2.circle(alone, (600, 300), 20, WHITE, -1)
    cv2.rectangle(alone, (594, 294), (606, 306), (20, 20, 20), -1)
    found = read_ball_list(_mosaic(alone), _reference())
    assert found.labels == ["cue"] and found.cues_dropped == 0


def test_an_empty_table_lists_nothing() -> None:
    assert read_ball_list(_mosaic(_cloth()), _reference()) == BallList(balls=(), arm=False)


def _reading(colour: int, white: float, wrap: int = 0, off: float = 0.0) -> tuple:
    """A ball's (centroid distances, stripe evidence), ``off`` Lab units from
    centroid ``colour`` towards nothing in particular."""
    point = CENTROIDS[colour] + np.array([off, 0.0, 0.0])
    return np.linalg.norm(CENTROIDS - point, axis=1), stripe_evidence(white, wrap)


def test_no_duplicate_means_every_ball_keeps_its_own_number() -> None:
    yellow, blue, black = 0, 1, 7
    readings = [_reading(yellow, 0.05), _reading(blue, 0.6), _reading(black, 0.9, wrap=30)]
    assert assign_numbers(readings) == [1, 10, 8]  # the 8 is the 8, whatever its white


def test_a_duplicate_goes_to_the_better_match_and_the_other_takes_its_next_best() -> None:
    """The 2026-11-17 settle: a solid 1 reading 0.21 white (its number circle
    plus the lamp's reflection) beside the real 9 at 0.57. Both read 9."""
    yellow = 0
    one, nine = _reading(yellow, 0.21, wrap=4), _reading(yellow, 0.57, wrap=24)
    assert assign_numbers([one, nine]) == [1, 9]
    assert assign_numbers([nine, one]) == [9, 1]  # order does not decide it


def test_a_loser_whose_twin_is_taken_falls_to_the_next_colour() -> None:
    """Three yellows: the 1 and the 9 go to the clearest solid and stripe; the
    third, the weakest stripe, falls to the next-nearest colour (orange)."""
    yellow, orange = 0, 4
    solid = _reading(yellow, 0.02)
    stripe = _reading(yellow, 0.7, wrap=30)
    faint = _reading(yellow, 0.25, wrap=10, off=4.0)
    numbers = assign_numbers([faint, solid, stripe])
    assert numbers[1:] == [1, 9]
    assert numbers[0] == orange + 9  # the 13: orange, still a stripe


def test_more_numbered_balls_than_numbers_leaves_the_rest_unnumbered() -> None:
    readings = [_reading(k % 8, 0.05 + 0.4 * (k % 2)) for k in range(16)]
    numbers = assign_numbers(readings)
    assert sorted(n for n in numbers if n is not None) == list(range(1, 16))
    assert numbers.count(None) == 1


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
    reassigned = dataclasses.replace(_ball("1", 400), wrap=7, own=9)
    found = BallList(
        balls=(_ball("cue", 100), _ball("11", 200), _ball("?", 300), reassigned), arm=True
    )
    record = json.loads(json.dumps(found.as_dict()))
    assert record["balls"][3]["own"] == "9" and "own" not in record["balls"][1]
    again = BallList.from_dict(record)
    assert again.labels == ["cue", "11", "?", "1"] and again.arm
    assert again.balls[3].own == 9 and again.balls[3].wrap == 7
    assert same_table(found, again)
    for ball in record["balls"]:  # a record from before #65
        del ball["wrap"]
        ball.pop("own", None)
    assert BallList.from_dict(record).labels == again.labels


def test_the_strip_shows_the_latest_list_without_an_arm() -> None:
    hub = Hub()
    assert hub.payload()["ball_strip"] == {"shown": True, "balls": None}
    shown = BallList(balls=(_ball("cue", 100), _ball("3", 200), _ball("?", 300)), arm=False)
    asyncio.run(hub.ball_list(1.0, "rest", shown))
    # a hand over the table proves no absence
    asyncio.run(hub.ball_list(2.0, "rest", BallList(balls=(_ball("3", 200),), arm=True)))
    assert hub.payload()["ball_strip"] == {"shown": True, "balls": ["cue", "3", "?"]}


def test_the_strip_shows_a_standing_rack() -> None:
    """A rack is one hand-sized blob, so its list comes back `arm`; `pack`
    says the blob is the list's own balls, and the strip shows it. On
    2026-11-18 it showed 8 of 15 balls for 75 s, until the break."""
    hub = Hub()
    partial = BallList(balls=(_ball("cue", 100), _ball("3", 200)), arm=False)
    rack = BallList(balls=tuple(_ball(str(n), 300 + 40 * n) for n in range(1, 16)), arm=True)
    asyncio.run(hub.ball_list(1.0, "rest", partial))
    asyncio.run(hub.ball_list(2.0, "rest", rack))  # hands still on it
    assert hub.payload()["ball_strip"]["balls"] == ["cue", "3"]
    asyncio.run(hub.ball_list(3.0, "rest", BallList(balls=rack.balls, arm=True, pack=True)))
    assert hub.payload()["ball_strip"]["balls"] == [str(n) for n in range(1, 16)]


def test_a_pack_survives_its_record_and_counts_as_a_change() -> None:
    rack = BallList(balls=(_ball("1", 100), _ball("2", 140)), arm=True, pack=True)
    again = BallList.from_dict(json.loads(json.dumps(rack.as_dict())))
    assert again.pack and again.arm
    handled = BallList(balls=rack.balls, arm=True)
    assert "pack" not in handled.as_dict()  # older logs read back unchanged
    assert not BallList.from_dict(handled.as_dict()).pack
    assert not same_table(rack, handled)  # the hands coming off the rack is news


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
