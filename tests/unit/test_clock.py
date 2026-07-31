"""Shot-clock state machine — behavior locked to docs/design.md §2.1."""

from cueclock.clock import RULESETS, Ruleset, ShotClock, State, get_ruleset


def make_clock(name: str = "ultimate_pool_8ball") -> ShotClock:
    return ShotClock(RULESETS[name], now=0.0)


def test_initial_state() -> None:
    clock = make_clock()
    snap = clock.snapshot()
    assert snap.state == "IDLE"
    assert snap.display == 30
    assert snap.extensions_used == 0
    assert snap.cue is None


def test_countdown_and_display_ceiling() -> None:
    clock = make_clock()
    clock.start(now=0.0)
    snap = clock.tick(now=4.5)
    assert clock.state is State.RUNNING
    assert snap.time == 25.5
    assert snap.display == 26  # ceiling, matching the old overlay


def test_idle_and_motion_do_not_count_down() -> None:
    clock = make_clock()
    assert clock.tick(now=5.0).time == 30  # IDLE
    clock.start(now=5.0)
    clock.tick(now=10.0)
    clock.shot_taken()
    assert clock.state is State.MOTION
    assert clock.tick(now=60.0).time == 25  # frozen during MOTION


def test_urgency_cues_edge_triggered_once() -> None:
    clock = make_clock()
    clock.start(now=0.0)
    assert clock.tick(now=19.0).cue is None  # 11 s left
    assert clock.tick(now=20.5).cue == "ten"  # crossed 10
    assert clock.tick(now=21.0).cue is None  # no refire
    assert clock.tick(now=25.5).cue == "five"  # crossed 5
    assert clock.tick(now=26.0).cue is None


def test_zero_to_grace_to_expired_to_auto_restart() -> None:
    clock = make_clock()
    clock.start(now=0.0)
    clock.tick(now=15.0)
    clock.tick(now=25.0)  # fire "five" so the expiry path is isolated below
    snap = clock.tick(now=30.0)
    assert snap.state == "GRACE"
    assert snap.display == 0
    snap = clock.tick(now=32.0)  # grace_seconds elapsed
    assert snap.state == "EXPIRED"
    assert snap.cue == "expired"
    assert snap.display == 0
    assert clock.tick(now=33.0).cue is None  # expired cue fires once
    snap = clock.tick(now=34.0)  # post_expiry_seconds elapsed → next player
    assert snap.state == "RUNNING"
    assert snap.time == 30
    assert snap.extensions_used == 0


def test_extension_accepted_only_in_running_and_grace() -> None:
    clock = make_clock()
    assert not clock.extension()  # IDLE
    clock.start(now=0.0)
    clock.tick(now=1.0)
    clock.shot_taken()
    assert not clock.extension()  # MOTION
    clock.balls_settled(now=1.0)
    assert clock.extension()  # RUNNING
    clock.tick(now=2.0)
    snap = clock.snapshot()
    assert snap.extensions_used == 1
    assert snap.time == 44  # 30 - 1 + 15


def test_extension_limit_per_frame() -> None:
    clock = make_clock()
    clock.start(now=0.0)
    assert clock.extension()
    assert not clock.extension()  # extensions_per_frame == 1
    clock.balls_settled(now=1.0)  # fresh frame re-allows
    assert clock.extension()


def test_extension_in_grace_resumes_running() -> None:
    clock = make_clock()
    clock.start(now=0.0)
    clock.tick(now=30.5)
    assert clock.state is State.GRACE
    assert clock.extension()
    assert clock.state is State.RUNNING
    assert clock.remaining == 15


def test_extension_rearms_urgency_cues() -> None:
    clock = make_clock()
    clock.start(now=0.0)
    assert clock.tick(now=20.5).cue == "ten"
    clock.extension()  # 9.5 + 15 = 24.5 s
    assert clock.tick(now=35.0).cue == "ten"  # re-crossed 10, refires


def test_shot_during_grace_freezes_then_settle_restarts() -> None:
    clock = make_clock()
    clock.start(now=0.0)
    clock.tick(now=30.5)
    clock.shot_taken()  # legal shot within grace
    assert clock.state is State.MOTION
    clock.balls_settled(now=35.0)
    snap = clock.tick(now=36.0)
    assert snap.state == "RUNNING"
    assert snap.time == 29


def test_settle_resets_extensions_and_cues() -> None:
    clock = make_clock()
    clock.start(now=0.0)
    clock.extension()  # 30 + 15 = 45 s
    clock.tick(now=35.5)  # fires "ten"
    clock.shot_taken()
    clock.balls_settled(now=36.0)
    snap = clock.tick(now=56.5)  # crosses 10 again on the fresh countdown
    assert snap.extensions_used == 0
    assert snap.cue == "ten"


def test_pause_drops_to_idle_and_start_restarts_full() -> None:
    clock = make_clock()
    clock.start(now=0.0)
    clock.tick(now=12.0)
    clock.pause()
    assert clock.state is State.IDLE
    assert clock.tick(now=20.0).time == 18  # held, not counting
    clock.start(now=20.0)  # verbatim old behavior: restart from full
    assert clock.remaining == 30


def test_reset_and_set_ruleset() -> None:
    clock = make_clock()
    clock.start(now=0.0)
    clock.extension()
    clock.tick(now=3.0)
    clock.reset()
    assert clock.state is State.IDLE
    assert clock.remaining == 30
    assert clock.extensions_used == 0
    clock.set_ruleset(RULESETS["ultimate_pool_8ball_final10"])
    assert clock.snapshot().display == 15
    assert clock.snapshot().ruleset == "ultimate_pool_8ball_final10"


def test_rulesets_validated_values() -> None:
    assert set(RULESETS) == {
        "ultimate_pool_8ball",
        "ultimate_pool_8ball_final10",
        "wnt_9ball",
    }
    default = RULESETS["ultimate_pool_8ball"]
    assert default == Ruleset("ultimate_pool_8ball", 30, 15, 1, 2.0, 2.0)
    assert RULESETS["ultimate_pool_8ball_final10"] == Ruleset(
        "ultimate_pool_8ball_final10", 15, 15, 1, 2.0, 2.0
    )
    assert RULESETS["wnt_9ball"] == Ruleset("wnt_9ball", 30, 30, 1, 2.0, 2.0)


def test_ruleset_aliases_resolve() -> None:
    # The old app's identifier keeps working (Stream Deck compatibility).
    assert get_ruleset("ultimate_9ball") is RULESETS["wnt_9ball"]
    assert get_ruleset("matchroom_9ball") is RULESETS["wnt_9ball"]
    assert get_ruleset("ultimate_pool_8ball") is RULESETS["ultimate_pool_8ball"]
