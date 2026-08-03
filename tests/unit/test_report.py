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


def test_a_shot_beyond_the_reach_does_not_pair(tmp_path: Path) -> None:
    path = journal(tmp_path, press(10.0), shot(30.0), press(31.0))
    report = build(path, press_lag_s=2.0)
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


def test_lag_distribution_is_reported(tmp_path: Path) -> None:
    """The distribution is what anchors the reach-back — it must be shown."""
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
    assert report.lags == [0.5, 1.0, 2.0]
    text = format_report(report)
    assert "stroke → press lag" in text
    assert "LATE detections: none" in text


def test_only_a_shot_after_its_press_counts_as_late(tmp_path: Path) -> None:
    """Ranking by absolute gap put the *promptest* pairs under a "late" heading.

    Every healthy pair has its shot before its press, so the sort has to be on
    the signed delay or the list means the opposite of its own label.
    """
    path = journal(tmp_path, press(10.0), shot(1.0), press(20.0), shot(21.5))
    report = build(path)
    assert [(p.t, s.t) for p, s in report.late] == [(20.0, 21.5)]
    assert "LATE detections (shot emitted after its press)" in format_report(report)


def test_a_late_detection_on_the_final_press_pairs_but_is_never_a_phantom(
    tmp_path: Path,
) -> None:
    """The trailing reach rescues the last press without arming phantom judgement."""
    paired = build(journal(tmp_path, press(10.0), shot(12.0)))
    assert [(p.t, s.t) for p, s in paired.pairs] == [(10.0, 12.0)]
    assert paired.missed == []

    unpaired = build(journal(tmp_path, press(10.0), shot(9.5), shot(12.0)))
    assert [(p.t, s.t) for p, s in unpaired.pairs] == [(10.0, 9.5)]
    assert unpaired.phantom == [], "a shot past the last press is never judged invented"


def test_the_reach_is_asymmetric_so_a_long_rollout_keeps_its_own_shot(tmp_path: Path) -> None:
    """The bug a single symmetric window cannot avoid, at real timings.

    From 2026-08-03: the press at 638.11 s sits 11.48 s after its own stroke
    (a long roll-out) but only 8.65 s before the *next* one. A symmetric reach
    wide enough to find the stroke takes the nearer shot instead — pairing the
    press to a stroke that had not happened yet, and turning two correct
    detections into a miss.
    """
    path = journal(tmp_path, shot(626.63), press(638.11), shot(646.76), press(652.65))

    report = build(path)
    assert [(p.t, s.t) for p, s in report.pairs] == [(638.11, 626.63), (652.65, 646.76)]
    assert report.accuracy == 1.0

    symmetric = build(path, press_lag_s=12.0, late_detect_s=12.0)
    assert [(p.t, s.t) for p, s in symmetric.pairs] == [(638.11, 646.76)]
    assert [e.t for e in symmetric.missed] == [652.65]


# The first real marked session, verbatim: every stroke → press interval from
# `logs/session-20260803-121204.jsonl`, with the six shots Steven confirmed he
# simply forgot to mark. This is the regression the provisional 5 s window
# failed — it scored 2/13 (15.4%) on a session where vision caught every shot
# that was marked, because 11 of the 13 presses land 5.07-11.48 s after their
# stroke and the window stopped at 5.0 s.
SESSION_0803_SHOTS = [
    325.07, 344.19, 376.03, 398.51, 416.51, 430.00, 448.17, 464.44, 482.58, 509.03,
    522.06, 535.86, 553.42, 575.19, 589.91, 603.63, 626.63, 646.76, 663.90,
]  # fmt: skip
SESSION_0803_PRESSES = [
    330.39, 349.94, 404.01, 436.58, 453.65, 469.60, 526.16, 540.78, 558.50, 611.48,
    638.11, 652.65, 669.78,
]  # fmt: skip
SESSION_0803_UNMARKED = [376.03, 416.51, 482.58, 509.03, 575.19, 589.91]


def test_the_first_real_marked_session_scores_full_detection(tmp_path: Path) -> None:
    records = [shot(t) for t in SESSION_0803_SHOTS] + [press(t) for t in SESSION_0803_PRESSES]
    report = build(journal(tmp_path, *sorted(records, key=lambda r: float(str(r["t"])))))

    assert report.accuracy == 1.0, "vision caught every marked shot; the window hid it"
    assert report.missed == []
    assert [round(e.t, 2) for e in report.phantom] == SESSION_0803_UNMARKED
    assert report.late == [], "no shot in that session was emitted after its press"

    # Each press took the stroke immediately before it, never a later one.
    assert all(shot.t < press.t for press, shot in report.pairs)
    assert round(max(report.lags), 2) == 11.48
    assert round(min(report.lags), 2) == 4.10


def test_report_says_a_disagreement_is_not_a_label(tmp_path: Path) -> None:
    """The press stream has its own error rate; the output must say so."""
    path = journal(tmp_path, press(10.0), shot(9.5))
    assert "review candidate" in format_report(build(path))
