"""Bracket structure, seeding, handicap and routing (issue #43).

The structure tests are written against the eight-player numbering table in the
issue, because that table is the spec: get the cross-drop wrong and the bracket
still *runs*, it just quietly replays first-round pairings.
"""

import json

import pytest

from cueclock.server import bracket as bk
from cueclock.server.bracket import Bracket, BracketError, Player

FIELD8 = [
    Player("Hank Harrison", 563),
    Player("Joel Braun", 495),
    Player("Steven Asten", 471),
    Player("Marcus Tran", 415),
    Player("Nina Kessler", 355),
    Player("Dana Reyes", 320),
    Player("Priya Shah", 280),
    Player("Tom Whitley", 240),
]


def build(players=None, **kwargs) -> Bracket:
    return Bracket.create("Test Cup", list(players or FIELD8), **kwargs)


def refs(b: Bracket, match_id: int) -> list[tuple[str, int]]:
    return [(r.kind, r.value) for r in b.match(match_id).src]


# ---------------------------------------------------------------- structure


def test_eight_player_numbering_matches_the_issue() -> None:
    b = build()
    assert b.size == 8
    assert [m.id for m in b.matches] == list(range(1, 16))
    sides = {m.id: m.side for m in b.matches}
    assert [sides[i] for i in (1, 2, 3, 4, 5, 6, 7)] == ["A"] * 7
    assert [sides[i] for i in (8, 9, 10, 11, 12, 13)] == ["B"] * 6
    assert sides[14] == sides[15] == "F"
    assert b.match(7).label == "A Final"
    assert b.match(13).label == "B Final"
    assert b.match(14).label == "Final"
    assert b.match(15).label == "Second Final"
    assert b.match(15).conditional and not b.match(14).conditional


def test_total_matches_is_2n_minus_2_plus_the_second_final() -> None:
    for n in range(3, 17):
        b = build(FIELD8[: min(n, 8)] + [Player(f"P{i}", 300) for i in range(n - 8)])
        playable = [m for m in b.matches if not m.conditional]
        assert len(playable) == 2 * b.size - 2
        assert sum(m.conditional for m in b.matches) == 1


def test_a_side_feeds_forward() -> None:
    b = build()
    assert refs(b, 5) == [("winner", 1), ("winner", 2)]
    assert refs(b, 6) == [("winner", 3), ("winner", 4)]
    assert refs(b, 7) == [("winner", 5), ("winner", 6)]
    assert refs(b, 14) == [("winner", 7), ("winner", 13)]
    assert refs(b, 15) == refs(b, 14)


def test_b_side_first_round_takes_the_round_one_losers() -> None:
    b = build()
    assert refs(b, 8) == [("loser", 1), ("loser", 2)]
    assert refs(b, 9) == [("loser", 3), ("loser", 4)]


def test_the_cross_drop_crosses() -> None:
    """Match 10 takes the 1-2 half's survivor against the 3-4 half's dropper.

    Straight down instead produces first-round rematches on the B side.
    """
    b = build()
    assert refs(b, 10) == [("winner", 8), ("loser", 6)]
    assert refs(b, 11) == [("winner", 9), ("loser", 5)]
    assert refs(b, 12) == [("winner", 10), ("winner", 11)]
    assert refs(b, 13) == [("winner", 12), ("loser", 7)]


def test_sixteen_player_structure_and_cross_drop() -> None:
    b = build([Player(f"P{i}", 600 - 20 * i) for i in range(16)])
    assert b.size == 16
    assert [m.id for m in b.matches if m.side == "A"] == list(range(1, 16))
    assert [m.id for m in b.matches if m.side == "B"] == list(range(16, 30))
    assert b.match(30).label == "Final" and b.match(31).conditional
    # Every drop round reverses the incoming A-side losers.
    assert refs(b, 20) == [("winner", 16), ("loser", 12)]
    assert refs(b, 23) == [("winner", 19), ("loser", 9)]
    assert refs(b, 26) == [("winner", 24), ("loser", 14)]
    assert refs(b, 29) == [("winner", 28), ("loser", 15)]


def test_the_first_drop_round_can_never_replay_round_one() -> None:
    """The property the cross-drop exists for, checked structurally.

    Where the A side's second round drops its losers, a straight drop would put
    the survivor of Matches 1-2 against the player who *just beat* one of them —
    a guaranteed Round-1 rematch. Crossing makes the two branches descend from
    disjoint halves of Round 1, which this walks the reference graph to prove.

    Only the first drop round: further down the B side the field is small enough
    that everyone can meet everyone, and no drop rule changes that.
    """
    for size_field in (8, 16):
        b = build([Player(f"P{i}", 600 - 20 * i) for i in range(size_field)])

        def origins(match_id: int, b: Bracket = b) -> set[int]:
            m = b.match(match_id)
            if all(ref.kind == "position" for ref in m.src):
                return {m.id}
            return set().union(*(origins(ref.value) for ref in m.src))

        first_drop = min(m.round for m in b.matches if m.side == "B" and m.round % 2 == 0)
        drops = [m for m in b.matches if m.side == "B" and m.round == first_drop]
        assert drops
        for m in drops:
            left, right = (origins(ref.value) for ref in m.src)
            assert not (left & right), f"Match {m.id} can rematch Round 1"


# ---------------------------------------------------------------- seeding


def names(b: Bracket, match_id: int) -> list[str | None]:
    return [
        None if s is None or s == bk.BYE else b.players[s].name for s in b.match(match_id).slots
    ]


def test_adjacent_seeding_pairs_similar_ratings() -> None:
    b = build(seeding="adjacent")
    pairs = [sorted(b.match(i).slots) for i in (1, 2, 3, 4)]  # type: ignore[type-var]
    assert sorted(pairs) == [[0, 1], [2, 3], [4, 5], [6, 7]]


def test_standard_seeding_pulls_the_top_seeds_apart() -> None:
    b = build(seeding="standard")
    pairs = [sorted(b.match(i).slots) for i in (1, 2, 3, 4)]  # type: ignore[type-var]
    assert sorted(pairs) == [[0, 7], [1, 6], [2, 5], [3, 4]]


def test_adjacent_mean_gap_beats_standard_on_the_mockup_field() -> None:
    def mean_gap(seeding: str) -> float:
        b = build(seeding=seeding)
        gaps = []
        for i in (1, 2, 3, 4):
            left, right = (b.players[s].rating for s in b.match(i).slots)  # type: ignore[index]
            gaps.append(abs(left - right))
        return sum(gaps) / len(gaps)

    assert round(mean_gap("adjacent")) == 50
    assert round(mean_gap("standard")) == 187


def test_byes_go_to_the_top_seeds_and_land_in_different_halves() -> None:
    b = build(FIELD8[:6])
    assert b.size == 8
    # Seeds 1 and 2 sit out; the other four pair adjacently.
    sat_out = [m.id for m in b.matches if m.side == "A" and m.round == 1 and bk.BYE in m.slots]
    assert len(sat_out) == 2
    resting = {names(b, mid)[0] or names(b, mid)[1] for mid in sat_out}
    assert resting == {"Hank Harrison", "Joel Braun"}
    # Different halves: Match 5 and Match 6 each inherit exactly one of them.
    assert {b.match(5).slots[0], b.match(6).slots[0]} == {0, 1}


def test_a_bye_advances_without_being_played_and_cascades() -> None:
    b = build(FIELD8[:6])
    walkover = next(m for m in b.matches if m.side == "A" and bk.BYE in m.slots)
    assert walkover.played and b.status(walkover) == "bye"
    # Its "loser" is a bye too, so the B-side match waiting on it walks over.
    b_side = b.match(8)
    assert bk.BYE in b_side.slots
    assert b.next_ready() is not None and b.next_ready().side == "A"


def test_seven_players_leaves_one_bye() -> None:
    b = build(FIELD8[:7])
    byes = [m for m in b.matches if m.side == "A" and m.round == 1 and bk.BYE in m.slots]
    assert len(byes) == 1
    assert names(b, byes[0].id) == ["Hank Harrison", None]


def test_three_players_builds_a_four_bracket() -> None:
    b = build(FIELD8[:3])
    assert b.size == 4
    assert len([m for m in b.matches if not m.conditional]) == 6


def test_field_bounds_are_refused() -> None:
    with pytest.raises(BracketError):
        build(FIELD8[:2])
    with pytest.raises(BracketError):
        build([Player(f"P{i}", 300) for i in range(17)])


# ---------------------------------------------------------------- handicap


def test_game_odds_is_fargorates_own_model() -> None:
    assert bk.game_odds(0) == pytest.approx(0.5)
    assert bk.game_odds(100) == pytest.approx(2 / 3)
    assert bk.game_odds(-100) == pytest.approx(1 / 3)


def test_published_fargorate_race_to_five_chart() -> None:
    """FargoRate's own Fair Match output for a 400 vs 500 at a race to 5."""
    assert bk.handicap_race(5, 500, 400, "mild") == (5, 4)
    assert bk.handicap_race(5, 500, 400, "medium") == (6, 4)
    assert bk.handicap_race(5, 500, 400, "hot") == (6, 4)
    # And their published odds for those two races.
    assert bk._underdog_match_odds(5, 4, 2 / 3) == pytest.approx(0.259, abs=0.001)
    assert bk._underdog_match_odds(6, 4, 2 / 3) == pytest.approx(0.350, abs=0.001)


def test_published_fargorate_race_to_eleven_chart() -> None:
    assert bk.handicap_race(11, 500, 400, "mild") == (13, 9)
    assert bk.handicap_race(11, 500, 400, "hot") == (14, 8)
    assert bk._underdog_match_odds(14, 8, 2 / 3) == pytest.approx(0.399, abs=0.001)


def test_the_issues_own_example_lands_on_seven_to_two() -> None:
    """A 563 against a 240 at a race to 5 — #43's stated expectation."""
    assert bk.handicap_race(5, 563, 240, "medium") == (7, 2)


def test_the_ladder_ends_where_the_published_charts_end() -> None:
    # FargoRate's base-3 league ladder runs 3-3, 4-3, 4-2, 5-2; ours reaches the
    # same endpoint, and every chart on record floors the underdog at two.
    assert bk._ladder(3)[-1] == (5, 2)
    assert bk._ladder(5)[-1] == (7, 2)
    assert bk._ladder(11)[0] == (11, 11)
    assert all(weak >= bk.MIN_RACE for _, weak in bk._ladder(11))


def test_a_small_gap_gets_no_spot_at_all() -> None:
    """The failure the odds target exists to prevent: at a race to five the
    rungs are coarse, and picking the biggest legal one hands a 20-point gap
    the same spot as a 100-point one."""
    assert bk.handicap_race(5, 520, 500, "hot") == (5, 5)
    assert bk.handicap_race(5, 550, 500, "mild") == (5, 5)


def test_handicap_orders_targets_by_the_players_own_slots() -> None:
    assert bk.handicap_race(5, 400, 500, "medium") == (4, 6)


def test_handicap_never_hands_the_underdog_the_edge() -> None:
    for diff in range(0, 400, 10):
        strong, weak = bk.handicap_race(5, 500 + diff, 500, "hot")
        odds = bk._underdog_match_odds(strong, weak, bk.game_odds(diff))
        assert odds <= 0.5 + 1e-9


def test_handicap_off_and_unrated_race_the_base() -> None:
    assert bk.handicap_race(5, 563, 240, "off") == (5, 5)
    assert bk.handicap_race(5, None, 240, "hot") == (5, 5)
    assert bk.handicap_race(4, 400, 400, "hot") == (4, 4)


def test_a_race_never_drops_below_two() -> None:
    assert bk.handicap_race(3, 800, 100, "hot")[1] >= bk.MIN_RACE


def test_targets_come_from_the_side_base_race() -> None:
    b = build(a_race=5, b_race=4, handicap="off")
    assert b.match(1).targets == [5, 5]
    b.report(1, (5, 2))
    b.report(2, (5, 1))
    assert b.match(8).targets == [4, 4]  # B side races 4
    assert b.match(14).targets == [None, None]  # not ready yet
    assert b.base_race(b.match(15)) == 5  # both finals race the A-side length


def test_handicapped_targets_are_asymmetric() -> None:
    b = build(handicap="medium")
    # Match 4 in the mockup: Priya 280 against Tom 240.
    match = next(m for m in b.matches if m.side == "A" and m.round == 1 and m.slots == [6, 7])
    assert match.targets[0] >= match.targets[1]


# ---------------------------------------------------------------- results


def play_a_side(b: Bracket) -> None:
    for mid in (1, 2, 3, 4):
        b.report(mid, (5, 2))


def test_reporting_routes_the_winner_and_the_loser() -> None:
    b = build()
    b.report(1, (5, 2))  # slot 0 wins
    assert b.match(5).slots[0] == b.match(1).slots[0]
    assert b.match(8).slots[0] == b.match(1).slots[1]
    assert b.routes(b.match(1)) == {"winner": 5, "loser": 8}


def test_the_final_routes_from_both_sides() -> None:
    b = build()
    assert b.routes(b.match(7)) == {"winner": 14, "loser": 13}
    assert b.routes(b.match(13)) == {"winner": 14, "loser": None}


def test_a_level_score_is_refused() -> None:
    b = build()
    with pytest.raises(BracketError):
        b.report(1, (5, 5))


def test_an_unready_match_is_refused() -> None:
    b = build()
    with pytest.raises(BracketError):
        b.report(5, (5, 0))
    with pytest.raises(BracketError):
        b.load(5)


def test_reopening_is_allowed_while_downstream_is_unplayed() -> None:
    b = build()
    b.report(1, (5, 2))
    first = b.match(5).slots[0]
    b.report(1, (2, 5))
    assert b.match(5).slots[0] != first
    assert b.match(8).slots[0] == first  # the other player dropped instead


def test_reopening_behind_a_played_match_is_refused_by_name() -> None:
    b = build()
    b.report(1, (5, 2))
    b.report(2, (5, 3))
    b.report(5, (5, 1))
    with pytest.raises(BracketError, match="Match 5"):
        b.report(1, (2, 5))
    # Walk it back one at a time and it goes through.
    b.clear_result(5)
    b.report(1, (2, 5))


def test_clearing_a_result_is_refused_behind_a_played_match_too() -> None:
    b = build()
    b.report(1, (5, 2))
    b.report(2, (5, 3))
    b.report(5, (5, 1))
    with pytest.raises(BracketError, match="Match 5"):
        b.clear_result(1)
    b.clear_result(5)
    b.clear_result(1)
    assert b.match(1).winner is None and b.match(5).slots[0] is None


def test_clearing_a_walkover_is_refused() -> None:
    b = build(FIELD8[:6])
    walkover = next(m for m in b.matches if m.side == "A" and bk.BYE in m.slots)
    with pytest.raises(BracketError, match="bye"):
        b.clear_result(walkover.id)


def test_orphaned_results_are_cleared_when_the_draw_moves() -> None:
    b = build()
    b.report(1, (5, 2))
    b.report(2, (5, 2))
    b.report(5, (5, 0))
    b.clear_result(5)  # so match 1 is correctable again
    b.report(1, (2, 5))
    assert b.match(5).winner is None and b.match(5).scores == [0, 0]


def test_editing_a_round_one_matchup_rederives_downstream() -> None:
    b = build()
    swapped = b.edit(1, 7, 0)  # Tom Whitley against Hank Harrison
    assert [b.players[s].name for s in swapped.slots] == ["Tom Whitley", "Hank Harrison"]
    # The player Tom vacated leaves a hole rather than a duplicate.
    assert sum(1 for m in b.matches if 7 in m.slots) == 1


def test_editing_a_derived_slot_is_refused() -> None:
    b = build()
    with pytest.raises(BracketError, match="edit those"):
        b.edit(5, 0, 1)


def test_targets_can_be_overridden() -> None:
    b = build(handicap="off")
    b.set_targets(1, 7, 3)
    assert b.match(1).targets == [7, 3]


# ---------------------------------------------------------------- the finals


def run_to_the_final(b: Bracket) -> None:
    for mid in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13):
        b.report(mid, (5, 1))


def test_the_second_final_is_only_played_if_the_b_side_wins() -> None:
    b = build()
    run_to_the_final(b)
    assert b.status(b.match(15)) == "conditional"
    b.report(14, (5, 2))  # the A-side finalist wins — it ends here
    assert b.champion() == b.players[b.match(14).slots[0]].name
    assert b.finished
    with pytest.raises(BracketError):
        b.load(15)


def test_the_b_side_winning_the_final_forces_a_second_final() -> None:
    b = build()
    run_to_the_final(b)
    b.report(14, (2, 5))  # the B-side player wins — first loss for the other
    assert b.champion() is None and not b.finished
    assert b.status(b.match(15)) == "ready"
    b.load(15)
    b.report(15, (5, 3))
    assert b.finished and b.champion() is not None


def test_next_ready_skips_the_match_just_reported() -> None:
    b = build()
    b.report(1, (5, 0))
    assert b.next_ready(after=1).id == 2


# ---------------------------------------------------------------- live view


def test_live_scores_come_from_the_match_score_not_the_bracket() -> None:
    b = build()
    b.load(1)
    payload = b.as_dict(live_scores=(3, 2))
    loaded = next(m for m in payload["matches"] if m["id"] == 1)
    assert (loaded["p1"]["score"], loaded["p2"]["score"]) == (3, 2)
    other = next(m for m in payload["matches"] if m["id"] == 2)
    assert (other["p1"]["score"], other["p2"]["score"]) == (0, 0)


def test_unfilled_slots_render_generated_references() -> None:
    b = build()
    payload = b.as_dict()
    m10 = next(m for m in payload["matches"] if m["id"] == 10)
    assert m10["p1"]["ref"] == "Winner of Match 8"
    assert m10["p2"]["ref"] == "Loser of Match 6"


def test_reaching_the_target_arms_but_does_not_advance() -> None:
    b = build(handicap="off", a_race=5)
    b.load(1)
    assert b.live_summary((4, 2))["armed"] is None
    armed = b.live_summary((5, 2))["armed"]
    assert armed == {"slot": 0, "name": "Hank Harrison", "score": 5, "other": 2}
    assert not b.match(1).played  # the bracket has not moved
    # Taking the point back clears the arm by itself — no undo to find.
    assert b.live_summary((4, 2))["armed"] is None


def test_the_live_header_names_the_match_side_and_race() -> None:
    b = build(handicap="off", a_race=5)
    b.load(1)
    assert b.live_summary(None)["match"]["header"] == "Match 1 · A Side · Race to 5"
    b2 = build(handicap="medium")
    b2.set_targets(1, 5, 3)
    b2.load(1)
    assert b2.live_summary(None)["match"]["header"] == "Match 1 · A Side · Race 5\u20133"


def test_no_loaded_match_still_summarises() -> None:
    b = build()
    summary = b.live_summary(None)
    assert summary["match"] is None and summary["armed"] is None


# ---------------------------------------------------------------- persistence


def test_round_trips_through_disk(tmp_path) -> None:
    path = tmp_path / "bracket.json"
    b = build(path=path, handicap="hot")
    b.report(1, (5, 2))
    b.load(2)
    again = bk.load(path)
    assert again is not None
    assert again.match(1).winner == 0 and again.match(1).scores == [5, 2]
    assert again.loaded == 2
    assert again.handicap == "hot"
    assert again.match(5).slots == b.match(5).slots
    assert again.as_dict() == b.as_dict()


def test_a_corrupt_file_means_casual_not_a_crash(tmp_path) -> None:
    path = tmp_path / "bracket.json"
    path.write_text("{ not json")
    assert bk.load(path) is None
    assert bk.load(tmp_path / "absent.json") is None


def test_a_saved_file_cannot_point_at_a_match_that_does_not_exist(tmp_path) -> None:
    # Otherwise every payload would raise on a bracket nobody can now clear.
    path = tmp_path / "bracket.json"
    build(path=path)
    state = json.loads(path.read_text())
    state["loaded"] = 999
    path.write_text(json.dumps(state))
    again = bk.load(path)
    assert again is not None and again.loaded is None
    assert again.live_summary(None)["match"] is None


def test_a_correction_upstream_unloads_the_match_it_emptied() -> None:
    b = build()
    b.report(1, (5, 2))
    b.report(2, (5, 1))
    b.load(5)
    b.clear_result(1)  # Match 5 no longer has two players
    assert b.loaded is None
    assert b.live_summary(None)["match"] is None


def test_archive_writes_beside_the_journals(tmp_path) -> None:
    b = build()
    b.report(1, (5, 2))
    first = b.archive(tmp_path)
    assert first.name == "bracket-test-cup.json"
    second = b.archive(tmp_path)
    assert second.name == "bracket-test-cup-2.json"  # never overwrites
    saved = json.loads(first.read_text())
    assert saved["results"][0]["scores"] == [5, 2]
