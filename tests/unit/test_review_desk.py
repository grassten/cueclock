"""#70: what a session review reads — timeline, frames, and the dossier's classifications."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cueclock.review import dossier, frames, sessions, timeline
from cueclock.server import report

STAMP = "20261116-120000"
T0 = datetime(2026, 11, 16, 12, 0, 0, tzinfo=UTC)
MONO0 = 1000.0


def rec(s: float, kind: str, rack: int = 3, state: str = "RUNNING", **kw: Any) -> dict[str, Any]:
    """A journal record `s` seconds into the session (mono and wall in step)."""
    wall = (T0 + timedelta(seconds=s)).isoformat(timespec="milliseconds")
    return {
        "wall": wall,
        "mono": MONO0 + s,
        "kind": kind,
        "state": state,
        "remaining": 30.0,
        "rack_shots": rack,
        **kw,
    }


def write_journal(path: Path, records: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


def balls(s: float, numbers: str, rack: int = 3, arm: bool = False) -> dict[str, Any]:
    return rec(s, "ball_list", rack=rack, reason="rest", arm=arm, balls=numbers)


# ---------------------------------------------------------------- timeline


def test_a_moment_id_names_its_mono_time() -> None:
    assert timeline.center_of("in_hand_shot-1717566108") == pytest.approx(1717566.108)
    assert timeline.center_of("1717566.5") == pytest.approx(1717566.5)
    with pytest.raises(ValueError):
        timeline.center_of("nonsense")


def test_the_window_drops_only_what_repeats() -> None:
    found = [
        balls(0, "cue 1 2"),  # before the window
        balls(5, "cue 1 2"),  # repeats one outside the window: still the window's first list
        rec(6, "detect_heartbeat", hz=150.0),
        rec(7, "voice_final", text=""),
        rec(8, "voice_final", text="extension", accepted=True),
        balls(9, "cue 1 2"),  # a repeat inside the window
        balls(10, "cue 1"),
        rec(11, "vision_balls_settled", t=11.0, held="list"),
    ]
    window = timeline.window(found, MONO0 + 10, before=6, after=2)
    assert [r["mono"] - MONO0 for r in window] == [5, 8, 10, 11]
    assert len(timeline.window(found, MONO0 + 10, before=6, after=2, everything=True)) == 7


def test_a_line_keeps_every_decision_field_and_marks_the_center() -> None:
    settle = rec(11, "vision_balls_settled", t=11.0, held="list", rejected="phantom")
    line = timeline.line(settle, MONO0 + 11)
    assert line.startswith(">  +0.00")
    assert '"held":"list"' in line and '"rejected":"phantom"' in line
    assert '"wall"' not in line  # the fixed columns already show it
    assert timeline.line(settle, MONO0 + 10).startswith("   +1.00")


# ---------------------------------------------------------- miss classification


def test_a_miss_is_named_by_its_settle_and_its_list() -> None:
    settle = rec(10, "vision_balls_settled", moved=1, vanished=0)
    assert dossier.miss_class(settle, None, after_watchdog=True) == dossier.WATCHDOG
    assert dossier.miss_class(rec(10, "settle_synthesized"), None) == dossier.WATCHDOG
    assert dossier.miss_class(settle, {"row": "still"}) == dossier.CUE_ON_SPOT
    assert dossier.miss_class(settle, {"row": "other", "cue_px": 2.5}) == dossier.CUE_ON_SPOT
    assert dossier.miss_class(settle, {"row": "other", "cue_px": 30.0}) == dossier.UNEXPLAINED
    nothing = rec(10, "vision_balls_settled", moved=0, vanished=0)
    assert dossier.miss_class(nothing, {"row": "other"}) == dossier.NOTHING_MOVED
    gone = rec(10, "vision_balls_settled", moved=0, vanished=2)
    assert dossier.miss_class(gone, None) == dossier.UNEXPLAINED


# A rack: a seen shot, a watchdog release and the late settle after it, a
# shot counted with the cue ball on its spot, and a soft shot.
RACK = [
    rec(0, "cmd_new_rack", rack=0, state="IDLE"),
    rec(10, "vision_shot_taken", rack=0, state="MOTION", t=10.0),
    rec(14, "vision_balls_settled", rack=1, t=14.0, moved=3, vanished=0),
    rec(20, "vision_shot_taken", rack=1, state="MOTION", t=20.0),
    rec(32, "settle_synthesized", rack=2, quiet_s=12.0),
    rec(33, "vision_balls_settled", rack=3, t=33.0, moved=0, vanished=1),
    rec(33.01, "scratch_list", rack=3, row="shot", counted=True),
    rec(50, "shot_provisional", rack=3, state="MOTION", t=50.0),
    rec(51, "vision_balls_settled", rack=4, t=51.0, moved=1, vanished=0, held="list"),
    rec(51.01, "scratch_list", rack=4, row="still", counted=True),
    rec(70, "vision_balls_settled", rack=5, t=70.0, moved=1, vanished=0, held="motion"),
    rec(70.01, "scratch_list", rack=5, row="cue_moved", counted=True),
]


def test_misses_join_to_their_settles_and_are_classified(tmp_path: Path) -> None:
    journal = write_journal(tmp_path / "j.jsonl", RACK)
    misses = dossier.classify_misses(RACK, report.build(journal))
    assert [(m.shot.t, m.why) for m in misses] == [
        (33.0, dossier.WATCHDOG),
        (51.0, dossier.CUE_ON_SPOT),
        (70.0, dossier.UNEXPLAINED),
    ]
    assert misses[1].listing is not None and misses[1].listing["row"] == "still"


def test_what_decided_held_settles_and_the_shape_of_each_freeze() -> None:
    assert dossier.held_by(RACK) == {"list": 1, "motion": 1}
    # 14 s: strike, 3 moved. 32 s: strike, synthesized (no `moved`).
    # 51 s: a freeze with no strike, 1 moved. 33 s and 70 s ended no freeze.
    assert dossier.freeze_shapes(RACK) == {(True, "2+"): 1, (True, "?"): 1, (False, "1"): 1}


def test_racking_on_a_live_clock_is_one_stretch_until_new_rack() -> None:
    found = [
        rec(0, "cmd_new_rack", rack=0, state="IDLE"),
        balls(1, "cue 1 2 3 4 5 6", rack=1),
        balls(2, "cue 1 2", rack=4),  # 3 4 5 6 potted
        balls(3, "cue 1", rack=5),  # one number back is a misread, not racking
        balls(4, "cue 1 2", rack=5),
        balls(9, "cue 1 2 3 4 5", rack=5),  # three back at once: racking
        rec(10, "vision_balls_settled", rack=6, t=10.0),
        rec(11, "vision_balls_settled", rack=7, t=11.0),
        rec(20, "cmd_new_rack", rack=0, state="IDLE"),
    ]
    (stretch,) = dossier.racking_stretches(found)
    assert stretch.start["mono"] == MONO0 + 9
    assert stretch.returned == 3
    assert stretch.settles == 2
    assert stretch.end is not None and stretch.end["kind"] == "cmd_new_rack"
    # Pre-136 stats counted every number back: the misread 2 and the three
    # that began racking. Only the three are the stretch's.
    assert stretch.raw_back == 3
    assert dossier.raw_numbers_back(found) == 4


# ----------------------------------------------------------------- the page


def make_session(root: Path, verdicts: dict[str, Any], moments: list[dict[str, Any]]) -> Path:
    path = root / STAMP
    path.mkdir(parents=True)
    write_journal(path / "journal.jsonl", RACK)
    (path / "header.json").write_text(
        json.dumps({"git_sha": "0" * 40, "git_dirty": False, "hub": {"ruleset": "test"}})
    )
    (path / "moments.json").write_text(json.dumps(moments))
    (path / "verdicts.json").write_text(json.dumps(verdicts))
    (path / "replay.json").write_text(json.dumps({"ok": True, "diff_count": 0, "diffs": []}))
    return path


def moment(mid: str, s: float) -> dict[str, Any]:
    wall = T0 + timedelta(seconds=s)
    return {
        "id": mid,
        "wall": wall.isoformat(),
        "kind": mid.split("-")[0],
        "summary": f"summary of {mid}",
        "start_wall": (wall - timedelta(seconds=12)).isoformat(),
        "end_wall": (wall + timedelta(seconds=8)).isoformat(),
    }


def test_the_dossier_joins_verdicts_and_names_conflicts(tmp_path: Path) -> None:
    path = make_session(
        tmp_path,
        verdicts={
            "in_hand_shot-1051000": {"verdict": "wrong", "note": "my arm, not a shot"},
            "phantom-1051001": {"verdict": "right", "note": "looks right"},
            "gone-1": {"verdict": "wrong", "note": ""},
        },
        moments=[
            moment("in_hand_shot-1051000", 51.0),
            moment("phantom-1051001", 51.001),
            moment("no_strike-1070000", 70.0),
        ],
    )
    page = dossier.build(sessions.Session(STAMP, path), path / "journal.jsonl")
    assert '"my arm, not a shot"' in page
    assert "same episode (0.001 s apart)" in page and "CONFLICT" in page
    assert "no_strike-1070000" in page.split("## Unjudged")[1].split("##")[0]
    assert "gone-1" in page  # a verdict whose moment is no longer listed is named
    assert dossier.CUE_ON_SPOT in page
    assert "Landed since the session" in page  # an unknown sha is not HEAD
    before = dossier.verdicts_fingerprint(sessions.Session(STAMP, path))
    assert f"verdicts fingerprint {before}" in page
    (path / "verdicts.json").write_text("{}")
    assert dossier.verdicts_fingerprint(sessions.Session(STAMP, path)) != before


# ------------------------------------------------------------------- frames


def test_frames_prefer_a_recording_over_the_cut(tmp_path: Path) -> None:
    path = tmp_path / STAMP
    (path / "cuts").mkdir(parents=True)
    (path / "recording").mkdir()
    (path / "cuts" / "phantom-1051001.mp4").write_bytes(b"cut")
    (path / "moments.json").write_text(json.dumps([moment("phantom-1051001", 51.001)]))
    session = sessions.Session(STAMP, path)
    at = frames.when(session, "phantom-1051001")
    assert at == T0 + timedelta(seconds=51.001)

    cut = frames.source(session, at, "phantom-1051001", tmp_path / "none")
    assert cut is not None and not cut.exact and cut.path.name == "phantom-1051001.mp4"

    (path / "recording" / "full.mov").write_bytes(b"mov")
    (path / "recording.json").write_text(
        json.dumps(
            [
                {
                    "file": "recording/full.mov",
                    "start_wall": (T0 - timedelta(seconds=6)).isoformat(),
                    "stop_wall": (T0 + timedelta(seconds=600)).isoformat(),
                }
            ]
        )
    )
    kept = frames.source(session, at, "phantom-1051001", tmp_path / "none")
    assert kept is not None and kept.exact and kept.path.name == "full.mov"
    argv = frames.command(kept, at - timedelta(seconds=3), 6.0, 2.0, tmp_path / "out.jpg")
    assert argv[argv.index("-ss") + 1] == "54.001"  # 6 s of lead-in + 51.001 - 3
    assert "tile=4x3" in argv[argv.index("-vf") + 1]
    assert "tile 12 = +2.50 s" in frames.legend(kept, at, at - timedelta(seconds=3), 6.0, 2.0)
