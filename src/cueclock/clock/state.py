"""Pure shot-clock state machine.

Semantics are the spec recovered from the old app (``docs/design.md``
§2.1)::

    IDLE → RUNNING → MOTION (shot in progress) → RUNNING (reset to full)
                   → GRACE (buzzer at 0, 1 s) → EXPIRED (flash) → auto-restart

One deliberate change from the old implementation: no internal clock. Every
time-dependent method takes ``now`` (monotonic seconds), so the machine is
deterministic under test and replay. The vision events ``shot_taken`` /
``balls_settled`` replace the old ``motion_start`` / ``motion_end`` names.
The confirmed ``scratch`` verdict still does not touch the clock (see
``CHANGELOG.md``), but since checkpoint 78 (#25) the hub applies a
:meth:`hold` over the fresh countdown while a scratch *candidate* stands
un-refuted — the player is at the ball return, not the table.

Rack context (added 2026-07-31 for WNT match play): the clock counts shots per
rack so the WNT §5 first-shot allowance works — 60 s for the first shot after
the break-off or a push out, 30 s otherwise. ``new_rack`` resets the rack
scope (shot count + extension pool); ``reset`` stays the foul/bug button and
deliberately does not.

Session mode (added 2026-08-02, ``modes.py``) is a second, orthogonal axis:
the settings (``settings.py``, which replaced rulesets in checkpoint 145) say
how long each countdown is, a mode says what you are doing at the table. Only
``match`` is rack-scoped, so ``run_out`` gets the base countdown on every shot
including the first, and ``no_clock`` doesn't run at all.
"""

import math
from dataclasses import asdict, dataclass
from enum import Enum

from cueclock.clock.modes import DEFAULT_MODE, Mode, get_mode
from cueclock.clock.settings import (
    ENFORCE_EXTENSION_LIMIT,
    EXTENSIONS_PER_RACK,
    GRACE_SECONDS,
    POST_EXPIRY_SECONDS,
    Settings,
)


class State(Enum):
    IDLE = "IDLE"  # not counting; display shows remaining
    RUNNING = "RUNNING"  # counting down
    MOTION = "MOTION"  # balls in motion, clock frozen
    GRACE = "GRACE"  # hit zero, buzzer sounded; 1 s for a stroke made before it
    EXPIRED = "EXPIRED"  # time's up; flashes, then auto-restarts for next player


@dataclass(frozen=True)
class Snapshot:
    """Broadcast payload — one per tick, plus on-demand for new WS clients."""

    state: str
    time: float
    display: int
    settings: dict[str, str]  # the six values (`settings.OPTIONS`)
    mode: str
    timed: bool  # False = CLOCK off: no countdown exists
    base: int  # what a fresh countdown gives you now — the deck's START N face
    extensions_used: int
    extensions_allowed: int  # 0 while the extension limit is unenforced
    cue: str | None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class ShotClock:
    def __init__(self, settings: Settings, now: float, mode: Mode | None = None) -> None:
        self.settings = settings
        self.mode = mode if mode is not None else get_mode(DEFAULT_MODE)
        self.state = State.IDLE
        self._rack_shots = 0  # shots taken this rack; the break is shot 1
        self._shot_since_settle = False  # a ShotTaken has landed since the last settle
        # #33 Part B: a RUNNING fire waiting for its settle's verdict. The
        # settle counts it exactly once; a revert (placement) drops it.
        # Cleared by every operator path that rescopes the countdown.
        self._episode_fired = False
        self.remaining = float(self.current_base())
        self.extensions_used = 0
        self.last_cue: str | None = None
        self._last_tick = now
        self._cues_fired: set[str] = set()
        self._grace_left = 0.0
        self._expired_for = 0.0
        # Post-scratch grace (#25): while now < _hold_until, a RUNNING
        # countdown does not decrement. Set only by hold(); cleared by every
        # path that starts a fresh countdown, so a hold can never leak into
        # the next shot's clock.
        self._hold_until = 0.0

    @property
    def extensions_allowed(self) -> int:
        """The rack's pool: two with a clock, none without one."""
        return EXTENSIONS_PER_RACK if self.settings.timed else 0

    @property
    def rack_shots(self) -> int:
        """Shots counted this rack (diagnostics — the session journal)."""
        return self._rack_shots

    def current_base(self) -> int:
        """Full countdown for the next shot — rack- and mode-aware.

        WNT Event Rules §5: 60 s for the first shot after the break-off or a
        push out, 30 s otherwise. With the break as shot 1, "fewer than two
        shots this rack" is exactly the first-shot window.

        Only ``match`` is rack-scoped. Without a rack there is no break-off, so
        the allowance it exists to grant has nothing to attach to and every
        shot gets the base countdown.
        """
        if self.mode.rack_scoped and self._rack_shots < 2:
            return self.settings.first_shot_seconds
        return self.settings.base_seconds

    # ---------- operator commands (HTTP API / Stream Deck) ----------

    def start(self, now: float) -> None:
        if not self.settings.timed:
            return  # CLOCK off: there is no countdown to start
        if self.state in (State.IDLE, State.EXPIRED):
            self.remaining = float(self.current_base())
            self._cues_fired.clear()
        self.state = State.RUNNING
        self._hold_until = 0.0
        self._episode_fired = False
        self._last_tick = now

    def pause(self) -> None:
        # Verbatim old behavior: pause drops to IDLE and a later start()
        # restarts from full base time — remaining is not preserved. The
        # deferred fire dies with the episode (#33): paused over a live
        # motion episode, the abandoned flag must not ride into a later
        # IDLE-fire cycle and double-count its settle.
        self.state = State.IDLE
        self._episode_fired = False

    def reset(self) -> None:
        # Timer normalize only — the foul/bug button. Rack scope (shot count,
        # extension pool) deliberately survives: a foul must not refill
        # extensions. The per-rack reset is new_rack().
        self.state = State.IDLE
        self.remaining = float(self.current_base())
        self._cues_fired.clear()
        self.last_cue = None
        self._hold_until = 0.0
        self._episode_fired = False

    def new_rack(self, now: float) -> None:
        """Per-rack scope reset — pressed between games.

        Fresh shot count and extension pool (WNT §5: extensions are per rack);
        IDLE showing the first-shot countdown, so the untimed break can happen
        whenever the player is ready.
        """
        self._rack_shots = 0
        self._shot_since_settle = False
        self.extensions_used = 0
        self.state = State.IDLE
        self.remaining = float(self.current_base())
        self._cues_fired.clear()
        self.last_cue = None
        self._hold_until = 0.0
        self._episode_fired = False
        self._last_tick = now

    def restart(self, now: float) -> None:
        """Force a fresh countdown, running immediately.

        Mid-rack recovery for a missed auto-reset — rack-aware (60 s during
        the WNT post-break window, base otherwise), extensions untouched.

        Pressed with no shot yet counted this rack, it also **counts the
        break**. Out of a fresh rack the clock parks in IDLE so the untimed
        break can happen whenever the player is ready, and a break vision
        missed increments nothing — the strike is gated by the break floor and
        the settle is a no-op in IDLE. So the press means the break is behind
        us; without counting it the rack sits one shot short and the WNT §5
        60 s window replays on the shot after (live, 2026-08-05 19:59 and
        2026-08-06 01:53: break missed, Start pressed, 60 s handed out twice).
        """
        if not self.settings.timed:
            return
        if self.mode.rack_scoped and self._rack_shots == 0:
            self._rack_shots = 1
        self.remaining = float(self.current_base())
        self.state = State.RUNNING
        self._cues_fired.clear()
        self.last_cue = None
        self._hold_until = 0.0
        self._episode_fired = False
        self._last_tick = now

    def push(self, now: float) -> None:
        """Operator-called push out — re-enter the first-shot window.

        WNT Event Rules §5: "The first shot after the Break-Off or after a
        Push Out, players will have 60 seconds available." Vision can't hear
        the verbal call, and on a pass-back the original player gets 60 s
        again — so this is a button, pressed once per 60 s shot. If balls
        aren't moving the fresh countdown starts immediately; during MOTION
        only the window is re-armed, so the following settle starts at 60 s.
        Extensions untouched.
        """
        if not self.settings.timed:
            return
        self._rack_shots = 1  # as if the break just happened
        if self.state is not State.MOTION:
            # current_base(), not first_shot_seconds directly: outside a
            # rack-scoped mode there is no allowance to re-enter.
            self.remaining = float(self.current_base())
            self.state = State.RUNNING
            self._cues_fired.clear()
            self.last_cue = None
            self._hold_until = 0.0
            self._episode_fired = False
            self._last_tick = now
        else:
            # Pressed mid-flight: the rolling balls ARE the push out, and the
            # press just accounted for it (_rack_shots = 1 above). The settle
            # must start the 60 s window without counting the episode again —
            # the deferred fire (#33) is absorbed into the declaration.
            self._episode_fired = False
            self._shot_since_settle = True

    def change_settings(self, settings: Settings) -> None:
        """Apply new settings; they take effect on the next countdown.

        A countdown already running keeps its time, and the rack keeps its
        count: changing SHOT to 15 for the last ten minutes of a match must
        not hand the next player the 60 s after-break window again, which a
        rack reset would. An IDLE clock shows the new number straight away.
        Turning the clock on or off parks it, as choosing the untimed ruleset
        did, so a countdown never runs on under CLOCK off.
        """
        clock_changed = settings.timed != self.settings.timed
        self.settings = settings
        if clock_changed:
            self.reset()
        elif self.state is State.IDLE:
            self.remaining = float(self.current_base())

    def apply_legacy_ruleset(self, settings: Settings) -> None:
        """A pre-checkpoint-145 ruleset switch, as session logs recorded it.

        Kept for `cueclock session replay` only, so a logged `set_ruleset`
        replays to the clock it produced live: switching rulesets zeroed the
        rack and the extension pool, then parked the clock.
        """
        self.settings = settings
        self._rack_shots = 0
        self.extensions_used = 0
        self.reset()

    def set_mode(self, mode: Mode) -> None:
        """Switch session mode — same scope change as switching rulebooks.

        Whatever was half-played under the old mode doesn't carry: a rack in
        progress means nothing once there is no rack, and the extension pool is
        per rack. Parking in IDLE is also what makes ``no_clock`` honest —
        gating vision alone would leave whatever was already counting to run
        down and fire the urgency cues and EXPIRED mid-drill.
        """
        self.mode = mode
        self._rack_shots = 0
        self._shot_since_settle = False
        self.extensions_used = 0
        self.reset()

    def extension(self) -> bool:
        """Grant an extension if allowed; returns True when accepted.

        The pool cap applies only while ``ENFORCE_EXTENSION_LIMIT`` is on
        (`settings.py` — off since 2026-08-13, so every call over a live
        countdown is granted).
        """
        if self.state not in (State.RUNNING, State.GRACE):
            return False
        if ENFORCE_EXTENSION_LIMIT and self.extensions_used >= self.extensions_allowed:
            return False
        self.extensions_used += 1
        self.remaining += float(self.settings.extension_seconds)
        # Re-arm urgency cues so the extended countdown fires them again.
        self._cues_fired.clear()
        self.last_cue = None
        self.state = State.RUNNING
        return True

    # ---------- post-scratch grace (#25, checkpoint 78) ----------

    def hold(self, now: float, seconds: float) -> None:
        """Freeze a RUNNING countdown at its current value for ``seconds``.

        The post-scratch grace: with no referee to spot the ball, the player
        is walking to the return and fishing the cue out — the clock must not
        run against the retrieval. Only meaningful over a running countdown;
        any path that starts a fresh one clears it.
        """
        if self.state is State.RUNNING:
            self._hold_until = now + seconds

    def release_hold(self, now: float) -> bool:
        """End an active hold early; True if one was actually holding.

        The hub calls this on ScratchRefuted — the candidate was a potted
        object ball wearing the crown, nobody is at the return, and the
        countdown resumes from wherever the hold froze it.
        """
        active = self.state is State.RUNNING and now < self._hold_until
        self._hold_until = 0.0
        return active

    def holding(self, now: float) -> bool:
        """Is a hold currently freezing the countdown?"""
        return self.state is State.RUNNING and now < self._hold_until

    # ---------- vision events ----------

    def shot_taken(self) -> None:
        """Freeze the clock while balls are moving; count the rack shot.

        IDLE→MOTION makes the untimed break count: New Rack leaves IDLE, the
        break's ShotTaken lands in IDLE, and the following settle starts the
        WNT 60 s first-shot clock (§5 — the break itself is not clocked).

        A RUNNING fire defers its count to the settle (#33 Part B): the
        clock freezes now, but "was that a shot?" is a settle-time verdict —
        placements fire honest-looking newborn ShotTakens (2026-08-10:
        1463 and 399 mm/s during ball-in-hand), and only the settled table
        can tell them from a real stroke. The settle counts exactly one
        shot for the episode (fired or compensated) unless the hub's
        adjudication reverts it. GRACE and IDLE fires stay immediate: a
        shot at the buzzer is committed the moment it saves the countdown,
        and the break is E8's settle-time decision already.
        """
        if self.state is State.MOTION:
            return  # vision re-fired mid-motion — don't double-count the shot
        if self.state is State.RUNNING:
            self._episode_fired = True
            self.state = State.MOTION
            return
        self._rack_shots += 1
        self._shot_since_settle = True
        if self.state in (State.GRACE, State.IDLE):
            self.state = State.MOTION

    def break_reverted(self, now: float) -> bool:
        """The strike that left IDLE was not the break: back to the armed rack.

        The hub's settle-time verdict (design §17: the rack never moved, so
        what fired was the cue ball being set down or carried). Everything the
        fire did is undone: the shot it counted, the MOTION it entered. Only
        the break can be taken back, and only before its settle: by then
        nothing else in the rack has happened that this could erase.

        Returns True when the clock went back to IDLE.
        """
        if not (self.state is State.MOTION and self._rack_shots == 1 and self._shot_since_settle):
            return False
        self._rack_shots = 0
        self._shot_since_settle = False
        self.state = State.IDLE
        self.remaining = float(self.current_base())
        self._last_tick = now
        return True

    def motion_started(self, now: float) -> bool:
        """#33 Part B: freeze a RUNNING countdown at the motion-episode latch.

        The provisional shot: the table started moving ~0.25 s after the
        stroke, which is faster than any strike confirm (~0.5 s), so a
        missed strike stops bleeding time at the latch instead of at its
        settle ~5 s later. The freeze is provisional — the settle counts it
        (or a ShotTaken merges into it), and a placement-shaped verdict
        reverts it with the remaining time untouched. RUNNING only: GRACE
        has the E9 motion re-arm, IDLE belongs to the break.

        Returns True when the countdown actually froze.
        """
        if self.state is not State.RUNNING:
            return False
        self.state = State.MOTION
        self._last_tick = now
        return True

    def motion_reverted(self, now: float) -> bool:
        """#33 Part B: resolve a provisional freeze as not-a-shot.

        The countdown resumes with exactly the remaining it froze at (MOTION
        never decrements). Refused once a shot has been committed this cycle
        (a GRACE/IDLE fire counted immediately): committed counts do not
        un-happen here — mirror of balls_settled's suppression rule.

        Returns True when a freeze was actually reverted.
        """
        if self.state is not State.MOTION or self._shot_since_settle:
            return False
        self._episode_fired = False
        self.state = State.RUNNING
        self._last_tick = now
        return True

    def balls_settled(self, now: float, suppress: bool = False) -> bool:
        """Balls at rest — start a fresh shot clock for the next shot.

        Ignored in IDLE: IDLE means the operator said hold (New Rack, pause),
        and racking motion produces genuine settles — a settle with no
        detected shot must not start the clock (2026-07-31 live failure: New
        Rack, rack the balls, countdown starts). The untimed break is
        unaffected: its ShotTaken moves IDLE to MOTION first.

        A settle that arrives with no ShotTaken since the previous settle
        still counts the shot: balls moved and came to rest, so a shot
        happened even if the strike gates missed it (or it was below the
        impulse floor — the ultra-soft touch shot). Without this, a missed
        strike leaves the rack shot count short and the WNT §5 60 s window
        replays on the following shot (2026-07-31 live failure, twice: after
        a push out and after the break).

        ``suppress`` (#33 Part A) is the hub's settle-time verdict that this
        episode was ball-in-hand placement/retrieval, not a shot: nothing is
        counted and the countdown is left exactly as it was — a placement
        must neither reset the clock nor inflate the rack. Suppression is
        refused when a ShotTaken already committed this cycle (the shot is
        counted; evidence policy lives in the hub, committed counts do not
        un-happen here).

        Returns True when the settle moved the clock (counted / fresh
        countdown), False when ignored or suppressed.

        Does not refill extensions: the pool is per rack (WNT §5), restored
        only by new_rack().
        """
        if self.state is State.IDLE:
            return False
        if suppress and not self._shot_since_settle:
            # #33: not a shot. A provisional freeze resumes with exactly the
            # remaining it froze at; an unfrozen countdown is left alone.
            # A deferred fire inside the refused episode is dropped with it.
            self._episode_fired = False
            if self.state is State.MOTION:
                self.state = State.RUNNING
                self._last_tick = now
            return False
        if self._episode_fired or not self._shot_since_settle:
            # Exactly one shot per episode: the deferred fire if there was
            # one, the compensation otherwise. Both funnels — that is the
            # "10 frames late or 300" merge (#33): lateness within the
            # episode cannot double-count or lose a shot.
            self._rack_shots += 1
        self._episode_fired = False
        self._shot_since_settle = False
        self.remaining = float(self.current_base())
        self._cues_fired.clear()
        self.last_cue = None
        self.state = State.RUNNING
        self._hold_until = 0.0
        self._last_tick = now
        return True

    # ---------- time ----------

    def tick(self, now: float) -> Snapshot:
        """Advance to ``now``; returns the broadcast payload.

        Reaching zero sounds the ``buzzer`` cue that instant (#68): time is up
        the moment the wall reads 0, and a player down on a shot must hear it
        then, not some seconds later. GRACE is only the window in which a
        stroke made *before* zero is still being confirmed (``grace_seconds``);
        nothing extends it, so the time on zero is fixed.

        Until checkpoint 134 the vision motion bit re-armed GRACE while the
        table moved, so no buzzer could sound through a shot whose strike was
        missed. Since checkpoint 83 the engine's motion latch freezes the
        countdown before zero on such a shot, and the journals showed the hold
        saving none of 50 zeros while holding the clock on zero for 3.7 to
        40.6 s eleven times. A shot that does fly dark through zero still gets
        its fresh countdown: its settle lands in EXPIRED or in the restarted
        countdown and counts like any other.
        """
        dt = now - self._last_tick
        self._last_tick = now

        cue: str | None = None

        if self.state is State.RUNNING:
            prev = self.remaining
            # A scratch hold absorbs the held portion of the interval: no
            # decrement while now < _hold_until, exact resume on a tick that
            # straddles the deadline (#25).
            run_dt = min(dt, max(0.0, now - self._hold_until))
            self.remaining = max(0.0, self.remaining - run_dt)
            # Edge-triggered urgency cues, fired exactly once per countdown.
            if prev > 10 and self.remaining <= 10 and "ten" not in self._cues_fired:
                cue = "ten"
                self._cues_fired.add("ten")
            elif prev > 5 and self.remaining <= 5 and "five" not in self._cues_fired:
                cue = "five"
                self._cues_fired.add("five")
            if self.remaining <= 0.0:
                self.state = State.GRACE
                self._grace_left = GRACE_SECONDS
                # The buzzer outranks an urgency cue crossed in the same tick:
                # zero is the one that must be heard.
                cue = "buzzer"
        elif self.state is State.GRACE:
            self._grace_left -= dt
            if self._grace_left <= 0.0:
                self.state = State.EXPIRED
                self._expired_for = 0.0
        elif self.state is State.EXPIRED:
            # Loss of turn — flash EXPIRED briefly, then auto-restart the
            # clock for the next player's shot.
            self._expired_for += dt
            if self._expired_for >= POST_EXPIRY_SECONDS:
                self.remaining = float(self.current_base())
                self._cues_fired.clear()
                self.state = State.RUNNING
        # MOTION / IDLE: time passes but the countdown does not.

        self.last_cue = cue
        return self.snapshot(cue=cue)

    def snapshot(self, cue: str | None = None) -> Snapshot:
        return Snapshot(
            state=self.state.value,
            time=round(self.remaining, 2),
            display=0 if self.state is State.EXPIRED else math.ceil(self.remaining),
            settings=self.settings.as_dict(),
            mode=self.mode.name,
            timed=self.settings.timed,
            base=self.current_base(),
            extensions_used=self.extensions_used,
            # 0 while unenforced: no surface draws pips or an "x of y" for a
            # pool that cannot run out.
            extensions_allowed=self.extensions_allowed if ENFORCE_EXTENSION_LIMIT else 0,
            cue=cue,
        )
