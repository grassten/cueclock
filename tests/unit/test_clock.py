"""Shot-clock state machine — behavior locked to docs/design.md §2.1."""

from cueclock.clock import (
    MODES,
    RULESETS,
    Ruleset,
    ShotClock,
    State,
    get_mode,
    get_ruleset,
    next_mode,
)
from cueclock.clock.modes import MATCH_BREAK_FLOOR_MMS


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


def test_extension_pool_is_per_rack() -> None:
    clock = make_clock()
    clock.start(now=0.0)
    assert clock.extension()
    assert clock.extension()  # pool is 2: one per player, no turn tracking
    assert not clock.extension()  # spent
    clock.shot_taken()
    clock.balls_settled(now=1.0)  # next shot, same rack — pool stays spent
    assert not clock.extension()
    clock.new_rack(now=2.0)  # only a new rack refills the pool
    clock.start(now=2.0)
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


def test_settle_rearms_cues_but_keeps_extensions() -> None:
    clock = make_clock()
    clock.start(now=0.0)
    clock.extension()  # 30 + 15 = 45 s
    clock.tick(now=35.5)  # fires "ten"
    clock.shot_taken()
    clock.balls_settled(now=36.0)
    snap = clock.tick(now=56.5)  # crosses 10 again on the fresh countdown
    assert snap.extensions_used == 1  # pool is per rack — settle doesn't refill
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
    assert clock.extensions_used == 1  # reset is the foul button — no refill
    clock.set_ruleset(RULESETS["ultimate_pool_8ball_final10"])
    assert clock.snapshot().display == 15
    assert clock.snapshot().ruleset == "ultimate_pool_8ball_final10"
    assert clock.extensions_used == 0  # ruleset switch is a full fresh state


def test_rulesets_validated_values() -> None:
    assert set(RULESETS) == {
        "ultimate_pool_8ball",
        "ultimate_pool_8ball_final10",
        "wnt_9ball",
    }
    default = RULESETS["ultimate_pool_8ball"]
    # extensions_per_frame is the whole-rack pool: one per player, two players.
    assert default == Ruleset("ultimate_pool_8ball", 30, 30, 15, 2, 2.0, 2.0)
    assert RULESETS["ultimate_pool_8ball_final10"] == Ruleset(
        "ultimate_pool_8ball_final10", 15, 15, 15, 2, 2.0, 2.0
    )
    # WNT Event Rules §5: 30 s shot, 60 s first shot, 30 s extension;
    # the pool of 2 is one-per-player, self-policed (no turn tracking).
    assert RULESETS["wnt_9ball"] == Ruleset("wnt_9ball", 30, 60, 30, 2, 2.0, 2.0)


def test_ruleset_aliases_resolve() -> None:
    # The old app's identifier keeps working (Stream Deck compatibility).
    assert get_ruleset("ultimate_9ball") is RULESETS["wnt_9ball"]
    assert get_ruleset("matchroom_9ball") is RULESETS["wnt_9ball"]
    assert get_ruleset("ultimate_pool_8ball") is RULESETS["ultimate_pool_8ball"]


# ---------- rack context (WNT Event Rules §5, added 2026-07-31) ----------


def test_wnt_rack_flow_first_shot_gets_60() -> None:
    clock = make_clock("wnt_9ball")
    clock.new_rack(now=0.0)
    assert clock.state is State.IDLE
    assert clock.snapshot().display == 60  # first-shot window shown pre-break
    clock.shot_taken()  # the untimed break lands in IDLE and counts as shot 1
    assert clock.state is State.MOTION
    clock.balls_settled(now=5.0)
    assert clock.snapshot().display == 60  # WNT §5: first shot after the break-off
    clock.shot_taken()  # rack shot 2 — the 60 s shot itself
    clock.balls_settled(now=10.0)
    assert clock.snapshot().display == 30  # normal clock from here on


def test_shot_taken_in_motion_does_not_double_count() -> None:
    clock = make_clock("wnt_9ball")
    clock.new_rack(now=0.0)
    clock.shot_taken()
    clock.shot_taken()  # vision re-fires while balls still moving — one shot
    clock.balls_settled(now=1.0)
    assert clock.snapshot().display == 60  # still the first shot after the break


def test_8ball_behavior_unchanged_by_rack_context() -> None:
    # first_shot_seconds == base_seconds, so settles still start at 30.
    clock = make_clock()
    clock.new_rack(now=0.0)
    assert clock.snapshot().display == 30
    clock.shot_taken()
    clock.balls_settled(now=1.0)
    assert clock.snapshot().display == 30
    assert clock.state is State.RUNNING


def test_wnt_extension_pool_of_two_per_rack() -> None:
    clock = make_clock("wnt_9ball")
    clock.new_rack(now=0.0)
    clock.start(now=0.0)
    assert clock.extension()
    assert clock.extension()  # pool of 2 — one per player, self-policed
    assert not clock.extension()
    clock.new_rack(now=1.0)
    clock.start(now=1.0)
    assert clock.extension()  # refilled


def test_restart_runs_immediately_at_current_base() -> None:
    clock = make_clock("wnt_9ball")
    clock.new_rack(now=0.0)
    clock.start(now=0.0)
    clock.extension()
    clock.restart(now=3.0)  # missed-auto-reset recovery
    assert clock.state is State.RUNNING
    assert clock.remaining == 60  # still inside the post-break window
    assert clock.extensions_used == 1  # a force-restart is not a new rack
    clock.shot_taken()
    clock.balls_settled(now=4.0)
    clock.shot_taken()
    clock.balls_settled(now=5.0)
    clock.restart(now=6.0)
    assert clock.remaining == 30  # window over — same button, base time


def test_restart_after_a_missed_break_counts_the_break() -> None:
    # Live, twice (2026-08-05 19:59, 2026-08-06 01:53): the break was gated in
    # IDLE, Start recovered the clock, and the first shot after it got 60 s a
    # second time because the rack was still one shot short.
    clock = make_clock("wnt_9ball")
    clock.new_rack(now=0.0)
    clock.balls_settled(now=1.0)  # the missed break's settle — no-op in IDLE
    assert clock.rack_shots == 0
    clock.restart(now=2.0)  # Start 60: the break is behind us
    assert clock.remaining == 60  # the press still hands out the 60 it says
    assert clock.rack_shots == 1
    clock.shot_taken()  # the first shot after the break, under that 60
    clock.balls_settled(now=3.0)
    assert clock.remaining == 30  # window over, as it should have been


def test_restart_mid_rack_leaves_the_shot_count_alone() -> None:
    clock = make_clock("wnt_9ball")
    clock.new_rack(now=0.0)
    clock.shot_taken()  # break
    clock.balls_settled(now=1.0)
    clock.restart(now=2.0)  # missed-auto-reset recovery, still in the window
    assert clock.rack_shots == 1  # not bumped a second time
    assert clock.remaining == 60


def test_restart_without_a_rack_does_not_invent_a_break() -> None:
    # run_out has no break to count, so the press is a plain fresh countdown.
    clock = make_clock("wnt_9ball")
    clock.set_mode(get_mode("run_out"))
    clock.restart(now=1.0)
    assert clock.rack_shots == 0
    assert clock.remaining == 30


def test_reset_keeps_rack_shots_and_extensions() -> None:
    clock = make_clock("wnt_9ball")
    clock.new_rack(now=0.0)
    clock.shot_taken()  # break
    clock.balls_settled(now=1.0)
    clock.extension()
    clock.reset()  # foul/bug button: timer normalize only
    assert clock.state is State.IDLE
    assert clock.remaining == 60  # still fewer than two rack shots
    assert clock.extensions_used == 1  # a foul must not refill extensions
    clock.shot_taken()  # rack shot 2
    clock.balls_settled(now=2.0)
    assert clock.snapshot().display == 30


def test_push_out_restarts_at_60_and_rearms_the_window() -> None:
    clock = make_clock("wnt_9ball")
    clock.new_rack(now=0.0)
    clock.shot_taken()  # break
    clock.balls_settled(now=1.0)
    clock.shot_taken()  # the push-out shot
    clock.balls_settled(now=2.0)
    assert clock.snapshot().display == 30  # window over…
    clock.push(now=3.0)  # …until the operator relays the verbal call
    assert clock.state is State.RUNNING
    assert clock.remaining == 60  # WNT §5: first shot after a push out
    assert clock.extensions_used == 0  # untouched (none used here)
    clock.shot_taken()  # the 60 s shot
    clock.balls_settled(now=4.0)
    assert clock.snapshot().display == 30  # back to normal
    clock.push(now=5.0)  # pass-back: the original player gets 60 s again
    assert clock.remaining == 60


def test_push_during_motion_arms_the_following_settle() -> None:
    clock = make_clock("wnt_9ball")
    clock.new_rack(now=0.0)
    clock.shot_taken()  # break
    clock.balls_settled(now=1.0)
    clock.shot_taken()  # the push-out shot
    clock.push(now=2.0)  # called while balls are still rolling
    assert clock.state is State.MOTION  # clock stays frozen
    clock.balls_settled(now=3.0)
    snap = clock.snapshot()
    assert snap.state == "RUNNING"
    assert snap.display == 60  # first shot after the push out


def test_settle_in_idle_is_ignored() -> None:
    # Racking after New Rack genuinely moves and settles balls; the countdown
    # must wait for the break (2026-07-31 live failure).
    clock = make_clock("wnt_9ball")
    clock.new_rack(now=0.0)
    clock.balls_settled(now=1.0)  # rack lifted, balls at rest
    assert clock.state is State.IDLE
    assert clock.remaining == 60  # first-shot window intact, not counting
    clock.shot_taken()  # the break
    clock.balls_settled(now=2.0)
    assert clock.state is State.RUNNING
    assert clock.remaining == 60  # WNT §5 window starts with the break's settle


def test_settle_without_shot_still_counts_the_shot() -> None:
    # A missed strike (or a sub-impulse touch shot) must not replay the WNT
    # 60 s window: balls moved and settled, so a shot happened (2026-07-31
    # live failure — 60 s again on the shot after the break).
    clock = make_clock("wnt_9ball")
    clock.new_rack(now=0.0)
    clock.shot_taken()  # break, detected
    clock.balls_settled(now=1.0)
    assert clock.remaining == 60
    clock.balls_settled(now=2.0)  # first shot happened but vision missed it
    assert clock.state is State.RUNNING
    assert clock.remaining == 30  # window consumed, not replayed


def test_push_then_missed_shot_consumes_the_window() -> None:
    # The push-incident bug: push re-arms 60 s; the shot under that window is
    # missed by vision; the settle must still burn the window so the
    # following shot gets 30 s.
    clock = make_clock("wnt_9ball")
    clock.new_rack(now=0.0)
    clock.shot_taken()  # break
    clock.balls_settled(now=1.0)
    clock.shot_taken()  # push-out
    clock.balls_settled(now=2.0)
    clock.push(now=3.0)
    assert clock.remaining == 60
    clock.balls_settled(now=4.0)  # the 60 s shot, strike missed
    assert clock.remaining == 30  # following shot back on base time


# --- session modes (2026-08-02): the axis orthogonal to the rulebook ---


def test_run_out_has_no_first_shot_allowance() -> None:
    # The WNT 60 s exists for the shot after a break-off. Throw fifteen balls
    # up with no rack and there is no break for it to follow, so every shot —
    # including the opener — gets the base countdown.
    clock = make_clock("wnt_9ball")
    assert clock.current_base() == 60
    clock.set_mode(get_mode("run_out"))
    assert clock.current_base() == 30
    clock.shot_taken()
    clock.balls_settled(now=1.0)
    assert clock.remaining == 30


def test_match_keeps_the_first_shot_allowance() -> None:
    clock = make_clock("wnt_9ball")
    clock.set_mode(get_mode("run_out"))
    clock.set_mode(get_mode("match"))
    assert clock.current_base() == 60


def test_push_outside_a_rack_scoped_mode_gives_base_time() -> None:
    # push() re-enters the first-shot window; without one there is nothing to
    # re-enter, so it must not hand out 60 s anyway.
    clock = make_clock("wnt_9ball")
    clock.set_mode(get_mode("run_out"))
    clock.push(now=0.0)
    assert clock.remaining == 30
    assert clock.state is State.RUNNING


def test_switching_mode_zeroes_the_rack_scope() -> None:
    # Same scope change as switching rulebooks: a rack in progress means
    # nothing once there is no rack, and the extension pool is per rack.
    clock = make_clock("wnt_9ball")
    clock.shot_taken()
    clock.balls_settled(now=1.0)
    clock.extension()
    assert clock.extensions_used == 1
    assert clock.rack_shots == 1
    clock.set_mode(get_mode("run_out"))
    assert clock.extensions_used == 0
    assert clock.rack_shots == 0
    assert clock.state is State.IDLE


def test_no_clock_parks_the_countdown() -> None:
    # Gating vision alone would leave whatever was already counting to run
    # down and fire the urgency cues and EXPIRED mid-drill.
    clock = make_clock("ultimate_pool_8ball")
    clock.start(now=0.0)
    clock.tick(now=25.0)
    assert clock.state is State.RUNNING
    clock.set_mode(get_mode("no_clock"))
    assert clock.state is State.IDLE
    assert clock.remaining == 30
    snap = clock.tick(now=60.0)
    assert snap.state == "IDLE"
    assert snap.cue is None


def test_snapshot_carries_mode_and_the_countdown_a_start_would_give() -> None:
    clock = make_clock("wnt_9ball")
    snap = clock.snapshot()
    assert snap.mode == "match"
    assert snap.base == 60  # first-shot window: what the deck's START key says
    clock.shot_taken()
    clock.balls_settled(now=1.0)
    clock.shot_taken()
    clock.balls_settled(now=2.0)
    assert clock.snapshot().base == 30


def test_mode_table_break_floors_match_their_purpose() -> None:
    # Only match gates the opening shot; run_out exists precisely because that
    # gate ate honest ~1000 mm/s drill openers (BACKLOG, checkpoint 33).
    assert get_mode("match").break_floor_mms == MATCH_BREAK_FLOOR_MMS
    assert get_mode("run_out").break_floor_mms is None
    assert get_mode("run_out").clock_live
    assert not get_mode("no_clock").clock_live


def test_next_mode_cycles_and_wraps() -> None:
    seen = ["match"]
    for _ in MODES:
        seen.append(next_mode(seen[-1]))
    assert seen == ["match", "run_out", "no_clock", "match"]
    assert next_mode("garbage") == "match"


def test_grace_holds_while_table_motion_then_expires_clean() -> None:
    """E9 expiry hold (accuracy plan §13.3): no buzzer through live flight.

    A missed strike lets the countdown run to zero mid-shot; the false foul
    at the end of GRACE was the worst felt failure on record (2 of 30
    sessions, and live on 2026-08-06 19:20 the clock reached EXPIRED through
    a real roll-out — flag_missed_6). While the vision motion bit is set the
    grace window re-arms to full, so the buzzer only ever sounds after a
    complete, motion-free grace period. RUNNING is deliberately unaffected:
    a countdown that pauses whenever something rolls is a different clock.
    """
    clock = make_clock()
    clock.start(now=0.0)
    clock.tick(31.0)  # zero crossed -> GRACE
    assert clock.state is State.GRACE
    grace = clock.ruleset.grace_seconds
    # Motion holds it open far past the grace window (10 Hz tick cadence)...
    t = 31.0
    for _ in range(int(20 * grace * 10)):
        t += 0.1
        clock.tick(t, motion=True)
    assert clock.state is State.GRACE
    # ...and once motion clears, expiry needs one full quiet grace window,
    # counted from the last moving tick — not the remnant of the old one.
    quiet_start = t
    while clock.state is State.GRACE:
        t += 0.1
        clock.tick(t, motion=False)
    assert clock.state is State.EXPIRED
    assert t - quiet_start >= grace


def test_running_still_reaches_zero_during_motion() -> None:
    # The hold is GRACE-scoped: motion must not freeze the countdown itself.
    clock = make_clock()
    clock.start(now=0.0)
    clock.tick(31.0, motion=True)
    assert clock.state is State.GRACE


# ---------- post-scratch grace (#25, checkpoint 78) ----------


def test_scratch_hold_freezes_a_running_countdown() -> None:
    clock = make_clock()
    clock.start(now=0.0)
    clock.tick(now=2.0)  # 28 left
    clock.hold(now=2.0, seconds=5.0)
    assert clock.tick(now=6.0).time == 28  # held: no decrement
    assert clock.holding(6.0)
    # A tick straddling the 7.0 s deadline counts exactly the portion after it.
    assert clock.tick(now=9.0).time == 26
    assert not clock.holding(9.0)


def test_release_hold_resumes_immediately() -> None:
    clock = make_clock()
    clock.start(now=0.0)
    clock.hold(now=0.0, seconds=5.0)
    assert clock.tick(now=2.0).time == 30
    assert clock.release_hold(now=2.0)  # was actively holding
    assert clock.tick(now=4.0).time == 28
    assert not clock.release_hold(now=4.0)  # nothing left to release


def test_hold_does_not_leak_into_the_next_countdown() -> None:
    # Every path that starts a fresh countdown clears the hold — a scratch
    # hold granted this shot must never freeze the next shot's clock.
    clock = make_clock()
    clock.start(now=0.0)
    clock.hold(now=0.0, seconds=20.0)
    clock.shot_taken()
    clock.balls_settled(now=1.0)
    assert clock.tick(now=4.0).time == 27


def test_hold_is_running_only() -> None:
    # No countdown, nothing to hold: an IDLE hold call must not arm anything.
    clock = make_clock()
    clock.hold(now=0.0, seconds=5.0)
    clock.start(now=0.0)
    assert clock.tick(now=3.0).time == 27
