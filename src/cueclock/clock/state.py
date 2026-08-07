"""Pure shot-clock state machine.

Semantics are the spec recovered from the old app (``docs/design.md``
§2.1)::

    IDLE → RUNNING → MOTION (shot in progress) → RUNNING (reset to full)
                   → GRACE (2 s after 0) → EXPIRED (flash) → auto-restart

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

Session mode (added 2026-08-02, ``modes.py``) is a second, orthogonal axis: a
ruleset says which rulebook, a mode says what you are doing at the table. Only
``match`` is rack-scoped, so ``run_out`` gets the base countdown on every shot
including the first, and ``no_clock`` doesn't run at all.
"""

import math
from dataclasses import asdict, dataclass
from enum import Enum

from cueclock.clock.modes import DEFAULT_MODE, Mode, get_mode
from cueclock.clock.rulesets import Ruleset


class State(Enum):
    IDLE = "IDLE"  # not counting; display shows remaining
    RUNNING = "RUNNING"  # counting down
    MOTION = "MOTION"  # balls in motion, clock frozen
    GRACE = "GRACE"  # hit zero; short window still accepting extension/shot
    EXPIRED = "EXPIRED"  # time's up; flashes, then auto-restarts for next player


@dataclass(frozen=True)
class Snapshot:
    """Broadcast payload — one per tick, plus on-demand for new WS clients."""

    state: str
    time: float
    display: int
    ruleset: str
    mode: str
    base: int  # what a fresh countdown gives you now — the deck's START N face
    extensions_used: int
    extensions_allowed: int
    cue: str | None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class ShotClock:
    def __init__(self, ruleset: Ruleset, now: float, mode: Mode | None = None) -> None:
        self.ruleset = ruleset
        self.mode = mode if mode is not None else get_mode(DEFAULT_MODE)
        self.state = State.IDLE
        self._rack_shots = 0  # shots taken this rack; the break is shot 1
        self._shot_since_settle = False  # a ShotTaken has landed since the last settle
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
            return self.ruleset.first_shot_seconds
        return self.ruleset.base_seconds

    # ---------- operator commands (HTTP API / Stream Deck) ----------

    def start(self, now: float) -> None:
        if self.state in (State.IDLE, State.EXPIRED):
            self.remaining = float(self.current_base())
            self._cues_fired.clear()
        self.state = State.RUNNING
        self._hold_until = 0.0
        self._last_tick = now

    def pause(self) -> None:
        # Verbatim old behavior: pause drops to IDLE and a later start()
        # restarts from full base time — remaining is not preserved.
        self.state = State.IDLE

    def reset(self) -> None:
        # Timer normalize only — the foul/bug button. Rack scope (shot count,
        # extension pool) deliberately survives: a foul must not refill
        # extensions. The per-rack reset is new_rack().
        self.state = State.IDLE
        self.remaining = float(self.current_base())
        self._cues_fired.clear()
        self.last_cue = None
        self._hold_until = 0.0

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
        if self.mode.rack_scoped and self._rack_shots == 0:
            self._rack_shots = 1
        self.remaining = float(self.current_base())
        self.state = State.RUNNING
        self._cues_fired.clear()
        self.last_cue = None
        self._hold_until = 0.0
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
        self._rack_shots = 1  # as if the break just happened
        if self.state is not State.MOTION:
            # current_base(), not first_shot_seconds directly: outside a
            # rack-scoped mode there is no allowance to re-enter.
            self.remaining = float(self.current_base())
            self.state = State.RUNNING
            self._cues_fired.clear()
            self.last_cue = None
            self._hold_until = 0.0
            self._last_tick = now

    def set_ruleset(self, ruleset: Ruleset) -> None:
        # Switching game types is a bigger scope change than a new rack:
        # zero everything, then normalize the timer.
        self.ruleset = ruleset
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
        """Grant an extension if allowed; returns True when accepted."""
        if self.state not in (State.RUNNING, State.GRACE):
            return False
        if self.extensions_used >= self.ruleset.extensions_per_frame:
            return False
        self.extensions_used += 1
        self.remaining += float(self.ruleset.extension_seconds)
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

        IDLE→MOTION makes the untimed break count: Set up leaves IDLE, the
        break's ShotTaken lands in IDLE, and the following settle starts the
        WNT 60 s first-shot clock (§5 — the break itself is not clocked).
        """
        if self.state is State.MOTION:
            return  # vision re-fired mid-motion — don't double-count the shot
        self._rack_shots += 1
        self._shot_since_settle = True
        if self.state in (State.RUNNING, State.GRACE, State.IDLE):
            self.state = State.MOTION

    def balls_settled(self, now: float) -> None:
        """Balls at rest — start a fresh shot clock for the next shot.

        Ignored in IDLE: IDLE means the operator said hold (Set up, pause),
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

        Does not refill extensions: the pool is per rack (WNT §5), restored
        only by new_rack().
        """
        if self.state is State.IDLE:
            return
        if not self._shot_since_settle:
            self._rack_shots += 1
        self._shot_since_settle = False
        self.remaining = float(self.current_base())
        self._cues_fired.clear()
        self.last_cue = None
        self.state = State.RUNNING
        self._hold_until = 0.0
        self._last_tick = now

    # ---------- time ----------

    def tick(self, now: float, motion: bool = False) -> Snapshot:
        """Advance to ``now``; returns the broadcast payload.

        ``motion`` is the vision layer's identity-free table-motion bit
        (accuracy plan §13.3). Its one power is refusing the GRACE→EXPIRED
        transition: a buzzer through live ball flight is a false foul — the
        worst felt failure, 2 in 30 sessions — while a false ``motion`` only
        delays the buzzer by however long the flicker lasts. So while it is
        set, the grace window holds at full; the settle that follows rescues
        the shot through the existing compensation in :meth:`balls_settled`.
        It deliberately cannot stop RUNNING from reaching zero (a countdown
        that pauses whenever something rolls is a different, wrong clock).
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
                self._grace_left = self.ruleset.grace_seconds
        elif self.state is State.GRACE:
            if motion:
                # Balls are moving: the missed strike's settle is coming.
                # Re-arm rather than merely pause so the buzzer always gets a
                # full, motion-free grace window before it sounds.
                self._grace_left = self.ruleset.grace_seconds
            else:
                self._grace_left -= dt
            if self._grace_left <= 0.0:
                self.state = State.EXPIRED
                self._expired_for = 0.0
                if "expired" not in self._cues_fired:
                    cue = "expired"
                    self._cues_fired.add("expired")
        elif self.state is State.EXPIRED:
            # Loss of turn — flash EXPIRED briefly, then auto-restart the
            # clock for the next player's shot.
            self._expired_for += dt
            if self._expired_for >= self.ruleset.post_expiry_seconds:
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
            ruleset=self.ruleset.name,
            mode=self.mode.name,
            base=self.current_base(),
            extensions_used=self.extensions_used,
            extensions_allowed=self.ruleset.extensions_per_frame,
            cue=cue,
        )
