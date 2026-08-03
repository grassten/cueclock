"""The golden comparator itself — synthetic events, no clips.

`tests/corpus/goldens.py` is the regression net for every threshold change in
`vision/events.py`, so its own failure modes have to be covered where they
always run. A comparator that silently passes is worse than no comparator: it
reads green while the timelines drift.

Measured 2026-08-02, the argument for this file existing: perturbing
`settle_dwell_s` 0.5 -> 0.6 moved 33 of 53 corpus timelines. The count
assertions in `EXPECTED` caught **one**.
"""

from dataclasses import dataclass

import pytest

from cueclock.vision.events import BallsSettled, Scratch, ShotTaken
from tests.corpus import goldens


def _golden(*events: object) -> list[dict[str, object]]:
    return goldens.to_records(list(events))  # type: ignore[arg-type]


def test_identical_timelines_agree() -> None:
    events = [ShotTaken(t=1.0, ball_id=3, speed_mms=1200.0), BallsSettled(t=4.0)]
    assert goldens._mismatch(_golden(*events), _golden(*events)) is None


def test_none_fields_are_omitted_not_written_as_null() -> None:
    """A synthetic ShotTaken carries None speed; the record must not pin it.

    Otherwise every golden gains `"speed_mms": null` keys that compare unequal
    against a real replay's floats.
    """
    (record,) = goldens.to_records([ShotTaken(t=1.0)])
    assert record == {"type": "ShotTaken", "t": 1.0, "newborn": False}


def test_added_event_is_caught() -> None:
    before = _golden(ShotTaken(t=1.0), BallsSettled(t=4.0))
    after = _golden(ShotTaken(t=1.0), Scratch(t=2.0), BallsSettled(t=4.0))
    problem = goldens._mismatch(after, before)
    assert problem is not None and "sequence changed" in problem


def test_dropped_event_is_caught() -> None:
    before = _golden(ShotTaken(t=1.0), Scratch(t=2.0))
    after = _golden(ShotTaken(t=1.0))
    problem = goldens._mismatch(after, before)
    assert problem is not None and "sequence changed" in problem


def test_reordered_events_are_caught() -> None:
    """Same counts, same timestamps, different order — invisible to EXPECTED."""
    before = _golden(ShotTaken(t=1.0), Scratch(t=2.0))
    after = _golden(Scratch(t=1.0), ShotTaken(t=2.0))
    problem = goldens._mismatch(after, before)
    assert problem is not None and "sequence changed" in problem


def test_slid_timestamp_is_caught() -> None:
    """The motivating case: a settle sliding while the count stays 1."""
    before = _golden(BallsSettled(t=10.23))
    after = _golden(BallsSettled(t=10.53))
    problem = goldens._mismatch(after, before)
    assert problem is not None and "10.530" in problem


def test_one_frame_of_jitter_is_tolerated() -> None:
    """~12.8 ms is one detect frame at 78 Hz — inside the tolerance by design."""
    before = _golden(BallsSettled(t=10.23))
    after = _golden(BallsSettled(t=10.23 + 0.0128))
    assert goldens._mismatch(after, before) is None


def test_tolerance_edge_is_where_it_is_documented() -> None:
    before = _golden(BallsSettled(t=10.0))
    assert goldens._mismatch(_golden(BallsSettled(t=10.0 + 0.019)), before) is None
    assert goldens._mismatch(_golden(BallsSettled(t=10.0 + 0.021)), before) is not None


def test_speed_drift_is_caught() -> None:
    """The IDLE break gate reads speed_mms — a silent drift is a live failure."""
    before = _golden(ShotTaken(t=1.0, speed_mms=3000.0))
    after = _golden(ShotTaken(t=1.0, speed_mms=2500.0))
    problem = goldens._mismatch(after, before)
    assert problem is not None and "speed_mms" in problem


def test_small_speed_drift_is_tolerated() -> None:
    before = _golden(ShotTaken(t=1.0, speed_mms=3000.0))
    after = _golden(ShotTaken(t=1.0, speed_mms=3000.0 * 1.01))
    assert goldens._mismatch(after, before) is None


def test_slow_speeds_use_the_absolute_floor() -> None:
    """2% of 40 mm/s is under a millimetre — the floor keeps that from being
    a hair trigger on near-zero speeds."""
    before = _golden(ShotTaken(t=1.0, speed_mms=40.0))
    assert goldens._mismatch(_golden(ShotTaken(t=1.0, speed_mms=44.0)), before) is None
    assert goldens._mismatch(_golden(ShotTaken(t=1.0, speed_mms=50.0)), before) is not None


def test_strike_path_change_is_caught() -> None:
    """newborn flips when a different strike path fires — a design-level fact."""
    before = _golden(ShotTaken(t=1.0, speed_mms=900.0, newborn=False))
    after = _golden(ShotTaken(t=1.0, speed_mms=900.0, newborn=True))
    problem = goldens._mismatch(after, before)
    assert problem is not None and "newborn" in problem


def test_ball_id_change_is_deliberately_not_asserted() -> None:
    """Track ids are birth counters; a renumbering refactor must not go red.

    The id is still written to the file — it is what makes a diff readable —
    but asserting it would train the reader to regenerate reflexively.
    """
    before = _golden(ShotTaken(t=1.0, ball_id=3, speed_mms=900.0))
    after = _golden(ShotTaken(t=1.0, ball_id=17, speed_mms=900.0))
    assert goldens._mismatch(after, before) is None
    assert before[0]["ball_id"] == 3, "the id must still be recorded"


def test_missing_golden_names_the_regenerate_command() -> None:
    with pytest.raises(AssertionError, match="--update-goldens"):
        goldens.check("no_such_scenario_exists", "empty_table", [])


def test_every_event_type_round_trips() -> None:
    """A new Event variant must not silently fall out of the record.

    `to_records` reads dataclass fields, so this fails loudly the day someone
    adds an event type that is not a frozen dataclass.
    """
    from cueclock.vision import events as events_module

    variants = [ShotTaken(t=1.0), BallsSettled(t=2.0), Scratch(t=3.0)]
    names = {type(e).__name__ for e in variants}
    declared = {
        name
        for name, obj in vars(events_module).items()
        if isinstance(obj, type)
        and hasattr(obj, "__dataclass_fields__")
        and "t" in getattr(obj, "__dataclass_fields__", {})
    }
    assert names == declared, f"an Event variant is unrecorded by goldens: {declared - names}"
    for record, event in zip(goldens.to_records(variants), variants, strict=True):
        assert record["type"] == type(event).__name__
        assert record["t"] == event.t


@dataclass(frozen=True)
class _Unrounded:
    t: float = 1.0 / 3.0


def test_floats_are_rounded_so_diffs_stay_readable() -> None:
    (record,) = goldens.to_records([_Unrounded()])  # type: ignore[list-item]
    assert record["t"] == 0.3333
