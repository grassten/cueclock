"""The ball list (#54) against the corpus: the gates it shipped on.

Local-only like the rest of the corpus tier: each test skips when its clips
are absent. The list never feeds the tracker or the engine, so nothing here
touches a golden; the goldens are read only for the settle times.
"""

import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from cueclock.vision.ball_list import BallList, ListedBall, read_ball_list

from .test_corpus_events import CLIPS, REFERENCES, _reference_model

GOLDENS = Path(__file__).parent.parent / "goldens"
# 13 stills of a real 9-ball game, session 20261117-001351 (1115 mount),
# copied into the corpus so archive retention cannot take this check away.
GAME_STILLS = CLIPS / "stills_20261117-001351"
# The 33 settle stills of session 20261117-201731 (1115 mount, two racks of
# 9-ball), with truth.json: which balls are on the table at each, checked
# ball by ball by eye on 2026-11-17 (#65).
TRUTH_STILLS = CLIPS / "stills_20261117-201731"


def _need(*names: str) -> None:
    for name in names:
        if not (CLIPS / name).exists():
            pytest.skip(f"{name} not in the corpus")


def _frame_at(clip: str, t: float) -> np.ndarray:
    from cueclock.capture.clip import ClipReader

    reader = ClipReader(CLIPS / clip)
    i = int(np.searchsorted(np.asarray(reader.timestamps), t))
    return reader.frame(max(0, min(len(reader) - 1, i)))


def test_real_game_stills() -> None:
    """The rack, then each ball leaving in the order it was potted."""
    _need("stills_20261117-001351", "empty_table_1115")
    ref = _reference_model("empty_table_1115")
    seen = []
    for line in (GAME_STILLS / "index.jsonl").read_text().splitlines():
        entry = json.loads(line)
        if entry["reason"] not in ("settle", "cmd_set_ruleset"):
            continue
        bayer = cv2.imread(str(GAME_STILLS / entry["files"][-1]), cv2.IMREAD_UNCHANGED)
        seen.append(" ".join(read_ball_list(bayer, ref).labels))
    rack = ["cue"] + [str(n) for n in range(1, 10)]
    order = ["5", "8", "1", "2", "3", "4", "6", "7"]
    expected_sets = [rack]
    for gone in order:
        expected_sets.append([n for n in expected_sets[-1] if n != gone])
    # Consecutive duplicates are settles where nothing dropped.
    distinct = [s for k, s in enumerate(seen) if k == 0 or s != seen[k - 1]]
    assert distinct == [" ".join(s) for s in expected_sets]


def test_truth_stills_read_exactly() -> None:
    """Every settle of a live session, each number once (#65): the solid 1
    that read as a second 9 at 159.7 s now reads 1, and says what it read."""
    _need("stills_20261117-201731", "empty_table_1115")
    ref = _reference_model("empty_table_1115")
    truth = json.loads((TRUTH_STILLS / "truth.json").read_text())["settles"]
    assert len(truth) == 33
    for settle in truth:
        bayer = cv2.imread(str(TRUTH_STILLS / f"{settle['frame_i']:09d}.png"), cv2.IMREAD_UNCHANGED)
        found = read_ball_list(bayer, ref)
        assert sorted(found.labels) == sorted(settle["expected"]), settle["t"]
        assert not found.arm
        if settle["frame_i"] == 24953:
            (one,) = [b for b in found.balls if b.number == 1]
            assert one.own == 9 and (one.cx, one.cy) == (640.0, 330.0)


# Five frames of session 20261119-020755 (1115 mount) with truth.json: who is
# over the table, and whether the only hand-sized blob is the rack itself.
PACK_STILLS = CLIPS / "stills_20261119-020755"


def test_a_standing_rack_is_a_pack_and_a_hand_is_not() -> None:
    """A rack is one hand-sized blob, so the hand test flags it; `pack` says
    it is only the list's own balls, and the strip may show it. A hand on the
    rack, a hand placing the cue ball beside it and a sleeve next to a ball
    are not packs. `arm` itself is as it was: the clock's rules still treat
    every one of these lists as unable to show absence."""
    _need("stills_20261119-020755", "empty_table_1115")
    ref = _reference_model("empty_table_1115")
    frames = json.loads((PACK_STILLS / "truth.json").read_text())["frames"]
    assert len(frames) == 5
    for frame in frames:
        bayer = cv2.imread(str(PACK_STILLS / f"{frame['frame_i']:09d}.png"), cv2.IMREAD_UNCHANGED)
        found = read_ball_list(bayer, ref)
        assert (found.arm, found.pack) == (frame["arm"], frame["pack"]), frame["what"]
        if "expected" in frame:
            assert sorted(found.labels) == sorted(frame["expected"]), frame["what"]


# Four stills of sessions 20261119-020755 and 20261121-153046 (1115 mount)
# whose list held two cue balls, with truth.json: which circle is the cue
# ball and what the other one is, checked by eye on 2026-11-21.
TWO_CUE_STILLS = CLIPS / "stills_two_cues"


def test_the_cue_is_listed_once_on_the_stills_that_held_two() -> None:
    """A white logo on a black sleeve and a gloved hand on the cloth both
    pass the cue test. The list keeps the cue ball, leaves the other circle
    off, and reads every other ball as it did."""
    _need("stills_two_cues", "empty_table_1115")
    ref = _reference_model("empty_table_1115")
    lists = json.loads((TWO_CUE_STILLS / "truth.json").read_text())["lists"]
    assert len(lists) == 4
    for truth in lists:
        bayer = cv2.imread(str(TWO_CUE_STILLS / truth["file"]), cv2.IMREAD_UNCHANGED)
        found = read_ball_list(bayer, ref)
        assert sorted(found.labels) == sorted(truth["expected"]), truth["what"]
        (cue,) = [b for b in found.balls if b.cue]
        assert [cue.cx, cue.cy] == truth["cue"], truth["what"]
        assert found.cues_dropped == 1 and found.arm == truth["arm"]


# Six stills of session 20261123-180144 (1115 mount, 9-ball) with truth.json:
# where the solid 5 lies on each, looked at on 2026-11-23 (#79).
FIVE_STILLS = CLIPS / "stills_20261123-180144"


def test_a_solid_five_at_rest_reads_five() -> None:
    """The 5 lay untouched and the list called it 13 on five of these stills
    and 5 on the sixth, as its fitted centre moved by a pixel: the rim ring
    reached the ball's edge, where orange blends with the cloth. Nine-ball,
    so nothing on these lists is numbered above 9."""
    _need("stills_20261123-180144", "empty_table_1115")
    ref = _reference_model("empty_table_1115")
    frames = json.loads((FIVE_STILLS / "truth.json").read_text())["frames"]
    assert len(frames) == 6
    for frame in frames:
        bayer = cv2.imread(str(FIVE_STILLS / f"{frame['frame_i']:09d}.png"), cv2.IMREAD_UNCHANGED)
        found = read_ball_list(bayer, ref)
        numbers = [b.number for b in found.balls if b.number is not None]
        assert max(numbers) <= 9, frame["t"]
        (five,) = [b for b in found.balls if b.number == 5]
        assert np.hypot(five.cx - frame["five"][0], five.cy - frame["five"][1]) <= 2, frame["t"]
        assert five.own is None, frame["t"]  # read as a 5, not handed the number


def test_a_solid_five_reads_five_on_an_older_mount() -> None:
    """`break_outrun_0805` at rest (0801 mount): the 5, alone on the cloth
    and looked at on 2026-11-23, read 13 until the rim ring was pulled in
    from the ball's edge (#79). The frame was not used to choose the ring."""
    clip = "break_outrun_0805"
    _need(clip, REFERENCES[clip])
    found = read_ball_list(_frame_at(clip, 13.26), _reference_model(REFERENCES[clip]))
    (five,) = [b for b in found.balls if b.number == 5]
    assert np.hypot(five.cx - 389, five.cy - 527) <= 2 and five.own is None
    assert 13 not in [b.number for b in found.balls]


def _largest_cluster(balls: tuple[ListedBall, ...], link: float = 2.0) -> int:
    """Balls chained by centre gaps under ``link`` predicted radii: the rack."""
    best, seen = 0, set()
    for start in range(len(balls)):
        if start in seen:
            continue
        stack, size = [start], 0
        seen.add(start)
        while stack:
            k = stack.pop()
            size += 1
            for j, other in enumerate(balls):
                gap = np.hypot(balls[k].cx - other.cx, balls[k].cy - other.cy)
                if j not in seen and gap < link * balls[k].r_pred:
                    seen.add(j)
                    stack.append(j)
        best = max(best, size)
    return best


RACKS = {
    "break_0801": 9,
    "break_0801b": 9,
    "rack_place_0801": 9,
    "pickup_snatch_0801": 9,
    "break_outrun_0805": 10,
    "break_shot": 15,
    "break_dark_flight_0806": 15,
}


@pytest.mark.parametrize("clip", sorted(RACKS))
def test_static_rack_counts_exactly(clip: str) -> None:
    """Every frame (2 Hz) that holds a rack holds exactly the rack."""
    reference = REFERENCES.get(clip, "empty_table")
    _need(clip, reference)
    from cueclock.capture.clip import ClipReader

    reader = ClipReader(CLIPS / clip)
    ts = np.asarray(reader.timestamps)
    ref = _reference_model(reference)
    sizes = []
    for t in np.arange(float(ts[0]), float(ts[-1]), 0.5):
        i = min(len(reader) - 1, int(np.searchsorted(ts, t)))
        size = _largest_cluster(read_ball_list(reader.frame(i), ref).balls)
        if size >= 6:
            sizes.append(size)
    assert sizes, "no rack found at all"
    assert set(sizes) == {RACKS[clip]}, sizes


# The golden settles with no cue on the list: the five golden scratches, the
# cue lifted off the table, and a hand on it.
NO_CUE = {
    ("kick_scratch_0811", 15.37),
    ("pot_scratch_nohold_0811", 8.35),
    ("scratch_shot_0801", 5.73),
    ("scratch_replace", 8.45),
    ("shot_debug", 11.90),
    ("ball_in_hand_carry_b_0811", 13.35),
    ("break_0801b", 17.16),
}


def test_one_cue_at_every_golden_settle_but_seven() -> None:
    settles = []
    for path in sorted(GOLDENS.glob("*.json")):
        doc = json.loads(path.read_text())
        for event in doc["events"]:
            if event["type"] == "BallsSettled":
                settles.append((doc["scenario"], doc["reference"], float(event["t"])))
    _need(*{s for s, _, _ in settles}, *{r for _, r, _ in settles})
    lists: dict[tuple[str, float], BallList] = {
        (clip, round(t, 2)): read_ball_list(_frame_at(clip, t), _reference_model(ref))
        for clip, ref, t in settles
    }
    cues = {key: sum(b.cue for b in found.balls) for key, found in lists.items()}
    assert len(cues) == 64
    assert {key for key, n in cues.items() if n == 0} == NO_CUE
    assert {n for key, n in cues.items() if key not in NO_CUE} == {1}
