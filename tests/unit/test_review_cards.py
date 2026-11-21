"""One card per thing that happened (#69).

In session 20261121-153046 one settle raised two candidates 1 ms apart and
the adjustment after it a third 1.4 s later. As three cards they were judged
wrong, right and wrong: one moment, one card, one verdict.
"""

from pathlib import Path

from cueclock.review import moments
from tests.unit.test_review import run, write_journal

# The live shape: ball in hand ends on a counted settle 0.66 s after its
# strike (a phantom candidate too), then a counted settle that moved only
# the cue.
PLACING = [
    run(9.34, "vision_shot_taken", state="MOTION", t=9.34),
    run(10.0, "ball_in_hand_ended", reason="shot", placed=0),
    run(10.001, "vision_balls_settled", t=10.0, frame_t=10.0),
    run(11.4, "scratch_list", frame_t=11.4, row="cue_moved", counted=True),
]


def test_candidates_at_one_instant_are_one_card(tmp_path: Path) -> None:
    (card,) = moments.find(write_journal(tmp_path / "j.jsonl", PLACING))
    assert card.kinds == ("in_hand_shot", "phantom", "cue_only")
    assert card.kind == "in_hand_shot" and card.id.startswith("in_hand_shot-")
    assert card.wall == PLACING[1]["wall"]
    # Every candidate's sentence, and a span that covers them all.
    for said in ("Ball in hand ended", "Possible phantom", "Only the cue moved"):
        assert said in card.summary
    alone = moments.find(write_journal(tmp_path / "k.jsonl", PLACING[:3]))[0]
    assert card.start_wall == alone.start_wall and card.end_wall > alone.end_wall


def test_candidates_further_apart_stay_separate(tmp_path: Path) -> None:
    later = run(12.1, "scratch_list", frame_t=12.1, row="cue_moved", counted=True)
    found = moments.find(write_journal(tmp_path / "j.jsonl", [*PLACING[:3], later]))
    assert [m.kinds for m in found] == [("in_hand_shot", "phantom"), ("cue_only",)]


def test_a_quiet_stretch_is_its_own_card(tmp_path: Path) -> None:
    """It is stamped with the last thing seen, and is the 90 s after it."""
    journal = [
        run(20, "vision_shot_taken", state="MOTION", t=20.0),
        run(35, "settle_synthesized", quiet_s=12.0),
        *[run(40 + 5 * i, "detect_heartbeat", hz=156.0) for i in range(21)],
    ]
    found = moments.find(write_journal(tmp_path / "j.jsonl", journal))
    assert [m.kinds for m in found] == [("synthesized",), ("quiet",)]
    assert found[0].wall == found[1].wall
