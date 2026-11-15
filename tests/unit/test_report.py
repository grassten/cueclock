"""The accuracy reader: missed strikes recovered from the clock's own compensation.

The signal under test is subtle enough to be worth stating once more: nothing in
the journal says "missed". What says it is `rack_shots` going *up* on a
`vision_balls_settled` record, because `ShotClock.balls_settled` only increments
when no ShotTaken arrived since the previous settle. These tests pin that
reading, and pin the two ways it can be read wrongly — racking settles in IDLE,
and the first record of a session having nothing to compare against.

The phantom half has no such compensation to read — the engine believes a
phantom, so it confesses nothing — and these tests pin what stands in for it:
four symptoms of a freeze that no strike earned, and the boundary between the
two that are reported as candidates and the two that are reported beside them
because each is shared with a different failure.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from cueclock.server.report import (
    OPENING_SHOTS,
    build,
    find_journals,
    format_rollup,
    format_session,
    parse_since,
    summary_detail,
)


def journal(tmp_path: Path, *records: dict[str, Any], name: str = "session-x.jsonl") -> Path:
    path = tmp_path / name
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


def shot(t: float, rack: int) -> dict[str, Any]:
    return {"kind": "vision_shot_taken", "t": t, "frame_t": t, "rack_shots": rack, "speed": 2100}


def settle(t: float, rack: int, state: str = "RUNNING") -> dict[str, Any]:
    return {
        "kind": "vision_balls_settled",
        "t": t,
        "frame_t": t,
        "rack_shots": rack,
        "state": state,
    }


def heartbeat(hz: float, rack: int = 0) -> dict[str, Any]:
    return {"kind": "detect_heartbeat", "hz": hz, "rack_shots": rack, "state": "RUNNING"}


def command(kind: str, rack: int, state: str) -> dict[str, Any]:
    """An operator key. `state` is what the clock reported *after* answering it."""
    return {"kind": kind, "rack_shots": rack, "state": state}


# ---------- the core reading ----------


def test_a_strike_then_a_settle_at_the_same_count_is_a_seen_shot(tmp_path: Path) -> None:
    path = journal(tmp_path, shot(10.0, 1), settle(14.0, 1))
    report = build(path)
    assert report.total == 1
    assert report.missed == []
    assert report.miss_rate == 0.0


def test_settle_that_bumps_the_count_is_a_missed_strike(tmp_path: Path) -> None:
    """No ShotTaken, and the settle had to stand in for it — that is the miss."""
    path = journal(tmp_path, shot(10.0, 1), settle(14.0, 1), settle(30.0, 2))
    report = build(path)
    assert report.total == 2
    assert [s.index for s in report.missed] == [2]
    assert report.miss_rate == 0.5


def test_racking_settles_are_not_shots(tmp_path: Path) -> None:
    """A settle the clock ignored in IDLE is racking motion, not a shot.

    Counting it would inflate the denominator with non-shots and understate the
    miss rate — the direction of error that makes the number look better than
    the table does.
    """
    path = journal(tmp_path, settle(5.0, 0, state="IDLE"), shot(10.0, 1), settle(14.0, 1))
    report = build(path)
    assert report.total == 1
    assert report.racking_settles == 1


def test_first_record_has_nothing_to_compare_against(tmp_path: Path) -> None:
    """Credit it as seen rather than inventing a miss out of a missing baseline."""
    path = journal(tmp_path, settle(4.0, 7))
    report = build(path)
    assert report.total == 1
    assert report.missed == []


def test_unrelated_records_carry_the_count_forward(tmp_path: Path) -> None:
    """Any record's `rack_shots` is the baseline — heartbeats included.

    A heartbeat between the strike and the settle must not read as a miss just
    because it sits in between.
    """
    path = journal(tmp_path, shot(10.0, 1), heartbeat(77.0, 1), settle(14.0, 1))
    assert build(path).missed == []


# ---------- what the report says about the misses ----------


def test_opening_miss_is_flagged(tmp_path: Path) -> None:
    """A miss in the first shots costs more than the same miss mid-rack."""
    early = journal(tmp_path, settle(4.0, 1), settle(20.0, 3), name="session-early.jsonl")
    assert build(early).opening_miss is True

    records: list[dict[str, Any]] = []
    for i in range(1, OPENING_SHOTS + 2):
        records += [shot(float(i), i), settle(float(i) + 0.5, i)]
    records.append(settle(99.0, OPENING_SHOTS + 2))  # a miss, well past the opening
    late = journal(tmp_path, *records, name="session-late.jsonl")
    assert build(late).opening_miss is False


def test_a_starved_miss_is_separated_from_a_vision_miss(tmp_path: Path) -> None:
    """A miss at 12 Hz failed for a reason that is not the vision logic."""
    path = journal(tmp_path, heartbeat(12.0), shot(10.0, 1), settle(14.0, 1), settle(30.0, 2))
    report = build(path)
    assert len(report.missed) == 1
    assert len(report.starved_misses) == 1


def test_phantom_candidate_is_an_implausibly_short_roll_out(tmp_path: Path) -> None:
    """Measured roll-out is 2.27-8.62 s; a settle 0.2 s later moved nothing.

    A candidate, never a verdict — the journal cannot prove a phantom.
    """
    quick = journal(tmp_path, shot(10.0, 1), settle(10.2, 1), name="session-quick.jsonl")
    assert len(build(quick).phantom_candidates) == 1

    healthy = journal(tmp_path, shot(10.0, 1), settle(14.0, 1), name="session-ok.jsonl")
    assert build(healthy).phantom_candidates == []


def test_a_missed_shot_has_no_roll_out_to_measure(tmp_path: Path) -> None:
    """With no ShotTaken there is no interval, so a miss is never a phantom."""
    path = journal(tmp_path, settle(4.0, 1), settle(20.0, 2))
    assert build(path).phantom_candidates == []


def test_gated_shots_are_counted_apart_from_misses(tmp_path: Path) -> None:
    """'Vision saw it, the clock declined it' is a different bug from a miss."""
    path = journal(
        tmp_path,
        {"kind": "vision_shot_taken_ignored_idle", "t": 3.0, "rack_shots": 0},
        shot(10.0, 1),
        settle(14.0, 1),
    )
    report = build(path)
    assert report.gated == {"gated_idle": 1}
    assert report.missed == []


def test_scratches_are_tallied(tmp_path: Path) -> None:
    path = journal(
        tmp_path,
        shot(10.0, 1),
        settle(14.0, 1),
        {"kind": "vision_scratch", "t": 15.0, "rack_shots": 1},
    )
    report = build(path)
    assert report.scratches == 1


def test_scratch_adjudication_records_are_tallied(tmp_path: Path) -> None:
    """#26: candidates/refutations count beside the confirmed verdict, and
    their presence is what marks a journal's scratch count as adjudicated
    rather than the pre-#26 upper bound."""
    path = journal(
        tmp_path,
        shot(10.0, 1),
        settle(14.0, 1),
        {"kind": "vision_scratch_candidate", "t": 15.0, "rack_shots": 1},
        {"kind": "vision_scratch_refuted", "t": 16.0, "rack_shots": 1},
        shot(20.0, 2),
        settle(24.0, 2),
        {"kind": "vision_scratch_candidate", "t": 25.0, "rack_shots": 2},
        {"kind": "vision_scratch", "t": 26.0, "rack_shots": 2},
    )
    report = build(path)
    assert (report.scratch_candidates, report.scratch_refuted, report.scratches) == (2, 1, 1)


# ---------- robustness: the journal is written by a process that gets killed ----------


def test_a_torn_final_line_is_counted_not_fatal(tmp_path: Path) -> None:
    path = tmp_path / "session-torn.jsonl"
    path.write_text(
        json.dumps(shot(10.0, 1)) + "\n" + json.dumps(settle(14.0, 1)) + '\n{"kind": "vis'
    )
    report = build(path)
    assert report.total == 1
    assert report.torn_lines == 1


def test_blank_lines_and_non_objects_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "session-odd.jsonl"
    path.write_text("\n\n[1, 2]\n" + json.dumps(settle(4.0, 1)) + "\n")
    assert build(path).total == 1


def test_an_empty_journal_reports_nothing_rather_than_dividing_by_zero(tmp_path: Path) -> None:
    path = tmp_path / "session-empty.jsonl"
    path.write_text("")
    report = build(path)
    assert report.total == 0
    assert report.miss_rate is None
    assert "no shots" in format_session(report)


# ---------- selection and formatting ----------


def test_find_journals_is_oldest_first(tmp_path: Path) -> None:
    for name in ("session-b.jsonl", "session-a.jsonl", "notes.txt"):
        (tmp_path / name).write_text("")
    assert [p.name for p in find_journals(tmp_path)] == ["session-a.jsonl", "session-b.jsonl"]


def test_parse_since_accepts_hours_days_weeks() -> None:
    assert parse_since("24h") == 86400.0
    assert parse_since("7d") == 604800.0
    assert parse_since("2w") == 1209600.0


def test_parse_since_rejects_nonsense() -> None:
    with pytest.raises(ValueError, match="expected a span"):
        parse_since("soon")


def test_summary_detail_is_the_journal_payload(tmp_path: Path) -> None:
    path = journal(tmp_path, shot(10.0, 1), settle(14.0, 1), settle(30.0, 2))
    detail = summary_detail(build(path))
    assert detail["shots"] == 2
    assert detail["missed"] == 1
    assert detail["miss_rate"] == 0.5
    assert detail["opening_miss"] is True


def test_rollup_totals_across_sessions(tmp_path: Path) -> None:
    a = journal(tmp_path, shot(10.0, 1), settle(14.0, 1), name="session-a.jsonl")
    b = journal(tmp_path, settle(4.0, 1), settle(20.0, 2), name="session-b.jsonl")
    text = format_rollup([build(a), build(b)])
    assert "TOTAL" in text
    assert "33.33%" in text  # 1 miss in 3 shots
    assert "1/2 sessions" in text  # only session-b opened badly


def test_session_format_names_the_missed_shots(tmp_path: Path) -> None:
    path = journal(tmp_path, shot(10.0, 1), settle(14.0, 1), settle(30.0, 2))
    text = format_session(build(path))
    assert "MISSED" in text
    assert "#2" in text


# ---------- #33: deferred fires, provisionals, reverts ----------


def test_deferred_fire_settle_bump_is_still_a_seen_shot(tmp_path: Path) -> None:
    # Post-#33 journals: a RUNNING fire defers its count, so the settle bumps
    # rack_shots even though the strike WAS seen — the open strike record is
    # the sighting. (Pre-#33 journals never bump on seen strikes, so this
    # reading changes nothing for them.)
    report = build(journal(tmp_path, shot(5.0, 1), settle(10.0, 2)))
    assert report.total == 1
    assert report.missed == []


def test_reverted_provisional_is_not_a_shot_and_releases_the_strike(tmp_path: Path) -> None:
    # A placement episode: latch freeze, phantom fire, revert. The revert is
    # not a shot, and its fire must not mark the NEXT settle seen — that one
    # is a genuinely confessed miss.
    report = build(
        journal(
            tmp_path,
            {"kind": "shot_provisional", "rack_shots": 1, "state": "MOTION"},
            shot(5.0, 1),  # the placement's phantom fire, deferred
            {"kind": "shot_provisional_reverted", "rack_shots": 1, "state": "RUNNING"},
            settle(20.0, 2),  # later: a real missed strike, confessed
        )
    )
    assert report.provisionals == 1
    assert report.provisional_reverts == 1
    assert report.total == 1
    assert len(report.missed) == 1


def test_rejected_phantom_settle_is_tallied_and_not_a_shot(tmp_path: Path) -> None:
    """#37: the hub proved nothing happened, so the report must not count it.

    The record shape the 2026-08-11 `flag_phantom` episode writes today: latch
    freeze, revert, then the settle carrying the verdict. `total` is the
    number of shots the CLOCK acted on, and a rejected settle landing in
    `shots` would inflate every rate below it with the exact class this
    rejection exists to remove.
    """
    rejected = settle(10.0, 1)
    rejected["rejected"] = "phantom"
    rejected["involved"] = 0
    rejected["baseline"] = 4
    report = build(
        journal(
            tmp_path,
            {"kind": "shot_provisional", "rack_shots": 1, "state": "MOTION"},
            {"kind": "shot_provisional_reverted", "rack_shots": 1, "state": "RUNNING"},
            rejected,
            shot(20.0, 1),  # the real stroke, 1.6 s later at the table
            settle(26.0, 2),
        )
    )
    assert report.settles_rejected == 1
    assert report.settles_rejected_by_reason == {"phantom": 1}
    assert report.total == 1  # the real shot only
    assert report.missed == []  # and it was seen


def test_rejections_are_split_by_reason(tmp_path: Path) -> None:
    """#37 and #41 both refuse settles, and they must be counted apart.

    They share one code path and one journal marker, so after the two landed
    together the summary line would otherwise report a ball-in-hand carry as
    "phantoms rejected". They make different predictions (handoff §6) and are
    validated against different clips, so a session that refuses five carries
    and no phantoms has to be readable as exactly that.
    """
    phantom = settle(10.0, 1)
    phantom["rejected"] = "phantom"
    carry_a, carry_b = settle(20.0, 1), settle(30.0, 1)
    carry_a["rejected"] = carry_b["rejected"] = "hand_placement"
    report = build(journal(tmp_path, phantom, carry_a, carry_b, shot(40.0, 1), settle(46.0, 2)))
    assert report.settles_rejected == 3
    assert report.settles_rejected_by_reason == {"phantom": 1, "hand_placement": 2}
    assert report.total == 1  # none of the three is a shot


def test_suppressed_settle_is_tallied_and_not_a_shot(tmp_path: Path) -> None:
    report = build(
        journal(
            tmp_path,
            shot(5.0, 1),
            settle(6.0, 1),
            {"kind": "compensation_suppressed", "rack_shots": 1, "state": "RUNNING"},
        )
    )
    assert report.suppressed_settles == 1
    assert report.total == 1  # the real shot only


# --- balls appeared mid-rack (#52) ---


def test_mid_rack_appearances_counts_only_settles_inside_a_real_rack(tmp_path: Path) -> None:
    """Share of mid-rack settles that report a ball which was not there before.

    Eligible: past the break (`rack_shots >= 2`) on a table whose baseline is a
    real one (3+ balls). The break settle and a degenerate 1-ball baseline are
    left out, and so is a settle recorded before the rearrangement summary
    existed.
    """
    from cueclock.server.report import mid_rack_appearances

    def mid(rack: int, baseline: int, appeared: int) -> dict[str, Any]:
        return {**settle(10.0, rack), "baseline": baseline, "appeared": appeared}

    path = journal(
        tmp_path,
        mid(1, 1, 5),  # the break: excluded
        mid(2, 9, 1),  # counts, appeared
        mid(3, 8, 0),  # counts, clean
        mid(4, 2, 3),  # degenerate baseline: excluded
        settle(20.0, 5),  # no summary fields (older journal): excluded
        mid(6, 7, 2),  # counts, appeared
    )
    assert mid_rack_appearances(path) == (2, 3)
