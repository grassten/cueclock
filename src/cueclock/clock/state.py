"""Pure shot-clock state machine.

Semantics are the spec recovered from the old app (``docs/cv-app-rewrite.md``
§2.1)::

    IDLE → RUNNING → MOTION (shot in progress) → RUNNING (reset to full)
                   → GRACE (2 s after 0) → EXPIRED (flash) → auto-restart

One deliberate change from the old implementation: no internal clock. Every
time-dependent method takes ``now`` (monotonic seconds), so the machine is
deterministic under test and replay. The vision events ``shot_taken`` /
``balls_settled`` replace the old ``motion_start`` / ``motion_end`` names;
``scratch`` intentionally does not touch the clock (see ``docs/progress.md``).
"""

import math
from dataclasses import asdict, dataclass
from enum import Enum

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
    extensions_used: int
    extensions_allowed: int
    cue: str | None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class ShotClock:
    def __init__(self, ruleset: Ruleset, now: float) -> None:
        self.ruleset = ruleset
        self.state = State.IDLE
        self.remaining = float(ruleset.base_seconds)
        self.extensions_used = 0
        self.last_cue: str | None = None
        self._last_tick = now
        self._cues_fired: set[str] = set()
        self._grace_left = 0.0
        self._expired_for = 0.0

    # ---------- operator commands (HTTP API / Stream Deck) ----------

    def start(self, now: float) -> None:
        if self.state in (State.IDLE, State.EXPIRED):
            self.remaining = float(self.ruleset.base_seconds)
            self._cues_fired.clear()
        self.state = State.RUNNING
        self._last_tick = now

    def pause(self) -> None:
        # Verbatim old behavior: pause drops to IDLE and a later start()
        # restarts from full base time — remaining is not preserved.
        self.state = State.IDLE

    def reset(self) -> None:
        self.state = State.IDLE
        self.remaining = float(self.ruleset.base_seconds)
        self.extensions_used = 0
        self._cues_fired.clear()
        self.last_cue = None

    def set_ruleset(self, ruleset: Ruleset) -> None:
        self.ruleset = ruleset
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

    # ---------- vision events ----------

    def shot_taken(self) -> None:
        """Freeze the clock while balls are moving."""
        if self.state in (State.RUNNING, State.GRACE):
            self.state = State.MOTION

    def balls_settled(self, now: float) -> None:
        """Balls at rest — start a fresh shot clock for the next shot."""
        self.remaining = float(self.ruleset.base_seconds)
        self.extensions_used = 0
        self._cues_fired.clear()
        self.last_cue = None
        self.state = State.RUNNING
        self._last_tick = now

    # ---------- time ----------

    def tick(self, now: float) -> Snapshot:
        """Advance to ``now``; returns the broadcast payload."""
        dt = now - self._last_tick
        self._last_tick = now

        cue: str | None = None

        if self.state is State.RUNNING:
            prev = self.remaining
            self.remaining = max(0.0, self.remaining - dt)
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
                self.remaining = float(self.ruleset.base_seconds)
                self.extensions_used = 0
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
            extensions_used=self.extensions_used,
            extensions_allowed=self.ruleset.extensions_per_frame,
            cue=cue,
        )
