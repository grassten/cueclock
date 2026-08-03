"""Ground-truth report: the press-vs-detection diff (#19).

The feature's whole value is a **denominator**, so the arithmetic and the
arming rules have to be right in the cases that actually occur: presses that
land after the stroke, sessions with a break in the middle, and a player who
stops pressing partway through.
"""

import json
from pathlib import Path

from cueclock.server.report import (
    IDLE_SPLIT_S,
    Event,
    arm_windows,
    build,
    format_report,
    load,
)


def journal(tmp_path: Path, *records: dict[str, object]) -> Path:
    path = tmp_path / "session.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


def press(t: float) -> dict[str, object]:
    return {"kind": "ground_truth_press", "t": t, "frame_t": t, "mono": t}


def shot(t: float, kind: str = "vision_shot_taken", **extra: object) -> dict[str, object]:
    return {"kind": kind, "t": t, "frame_t": t, "mono": t, **extra}


def test_every_press_paired_is_full_accuracy(tmp_path: Path) -> None:
    path = journal(
        tmp_path,
        press(10.0),
        shot(9.4),  # the shot precedes its press: the hand comes off the cue after
        press(30.0),
        shot(29.5),
    )
    report = build(path)
    assert len(report.pairs) == 2
    assert report.missed == []
    assert report.phantom == []
    assert report.accuracy == 1.0


def test_press_with_no_shot_is_a_miss(tmp_path: Path) -> None:
    path = journal(tmp_path, press(10.0), shot(9.5), press(30.0))
    report = build(path)
    assert len(report.pairs) == 1
    assert [e.t for e in report.missed] == [30.0]
    assert report.accuracy == 0.5


def test_shot_with_no_press_inside_a_window_is_a_phantom(tmp_path: Path) -> None:
    path = journal(tmp_path, press(10.0), shot(9.5), shot(20.0), press(30.0), shot(29.5))
    report = build(path)
    assert [e.t for e in report.phantom] == [20.0]
    assert len(report.pairs) == 2


def test_shots_outside_every_window_are_ignored(tmp_path: Path) -> None:
    """The arming rule: a shot before the first press is not a phantom.

    Without this, every unmarked shot in a session where Steven pressed for
    only part of it would be reported as invented.
    """
    path = journal(tmp_path, shot(1.0), press(10.0), shot(9.5), press(20.0), shot(19.5), shot(99.0))
    report = build(path)
    assert report.phantom == []
    assert len(report.pairs) == 2


def test_the_window_does_not_extend_past_the_last_press(tmp_path: Path) -> None:
    """Deliberately conservative: under-report phantoms, never invent them."""
    path = journal(tmp_path, press(10.0), shot(9.5), press(20.0), shot(19.5), shot(21.0))
    report = build(path)
    assert report.phantom == [], "a shot after the final press must not be judged"


def test_an_idle_gap_splits_the_run(tmp_path: Path) -> None:
    presses = [Event(10.0, "p"), Event(20.0, "p"), Event(10_000.0, "p"), Event(10_010.0, "p")]
    assert arm_windows(presses) == [(10.0, 20.0), (10_000.0, 10_010.0)]


def test_a_single_press_is_a_zero_length_window(tmp_path: Path) -> None:
    assert arm_windows([Event(5.0, "p")]) == [(5.0, 5.0)]


def test_no_presses_arms_nothing(tmp_path: Path) -> None:
    path = journal(tmp_path, shot(1.0), shot(2.0))
    report = build(path)
    assert report.windows == []
    assert report.phantom == []
    assert report.accuracy is None
    assert "nothing to diff" in format_report(report)


def test_gap_shorter_than_the_split_stays_one_run() -> None:
    presses = [Event(0.0, "p"), Event(IDLE_SPLIT_S - 1, "p")]
    assert arm_windows(presses) == [(0.0, IDLE_SPLIT_S - 1)]


def test_a_shot_beyond_the_pair_window_does_not_pair(tmp_path: Path) -> None:
    path = journal(tmp_path, press(10.0), shot(30.0), press(31.0))
    report = build(path, pair_window_s=2.0)
    assert [e.t for e in report.missed] == [10.0]
    assert len(report.pairs) == 1  # 31.0 pairs with 30.0


def test_one_shot_cannot_satisfy_two_presses(tmp_path: Path) -> None:
    """A double tap must read as a miss, not silently pair twice."""
    path = journal(tmp_path, press(10.0), press(10.5), shot(9.8))
    report = build(path)
    assert len(report.pairs) == 1
    assert len(report.missed) == 1


def test_gated_shots_still_count_as_detected(tmp_path: Path) -> None:
    """ "Vision saw it, the clock declined it" is a different bug from a miss.

    The IDLE break gate rejecting a real break is a clock-policy failure, not
    a detection failure, and conflating them would send the next investigation
    to the wrong module.
    """
    path = journal(tmp_path, press(10.0), shot(9.5, kind="vision_shot_taken_ignored_idle"))
    report = build(path)
    assert len(report.pairs) == 1
    assert report.missed == []
    assert "detected but NOT clocked" in format_report(report)


def test_presses_with_no_capture_anchor_are_excluded_not_guessed(tmp_path: Path) -> None:
    """No vision running means no capture clock. Report it, don't invent one."""
    path = journal(tmp_path, {"kind": "ground_truth_press", "t": None, "mono": 5.0}, press(10.0))
    presses, _shots, unanchored = load(path)
    assert unanchored == 1
    assert [p.t for p in presses] == [10.0]
    assert "unanchored" in format_report(build(path))


def test_unknown_kinds_and_torn_lines_are_ignored(tmp_path: Path) -> None:
    """The journal contract is additive; a killed session leaves a torn line."""
    path = tmp_path / "session.jsonl"
    path.write_text(
        json.dumps(press(10.0))
        + "\n"
        + json.dumps(shot(9.5))
        + "\n"
        + json.dumps({"kind": "some_future_record", "t": 11.0})
        + "\n"
        + '{"kind": "ground_truth_pr'  # torn
    )
    report = build(path)
    assert len(report.pairs) == 1


def test_delay_distribution_is_reported(tmp_path: Path) -> None:
    """The distribution is what re-derives the pair window — it must be shown."""
    path = journal(
        tmp_path,
        press(10.0),
        shot(9.0),
        press(20.0),
        shot(19.5),
        press(30.0),
        shot(28.0),
    )
    report = build(path)
    assert report.delays == [-2.0, -1.0, -0.5]
    text = format_report(report)
    assert "press → detection delay" in text
    assert "slowest pairs" in text


def test_report_says_a_disagreement_is_not_a_label(tmp_path: Path) -> None:
    """The press stream has its own error rate; the output must say so."""
    path = journal(tmp_path, press(10.0), shot(9.5))
    assert "review candidate" in format_report(build(path))
