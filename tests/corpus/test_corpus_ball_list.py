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
