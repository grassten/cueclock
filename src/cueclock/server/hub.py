"""WebSocket hub: owns the clock, its 10 Hz tick loop, and connected clients.

This is where wall-clock time enters the system — ``ShotClock`` is pure, and
only this module calls ``time.monotonic()``. It also owns the match score and
the vision kill switch, and composes them with the clock snapshot into the
one payload every WS client and ``GET /api/state`` see.
"""

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path

from fastapi import WebSocket

from cueclock.clock import (
    DEFAULT_MODE,
    DEFAULT_RULESET,
    ShotClock,
    Snapshot,
    State,
    get_mode,
    get_ruleset,
)
from cueclock.server.journal import SessionJournal
from cueclock.server.score import MatchScore

BROADCAST_INTERVAL_SECONDS = 0.1  # 10 Hz, per docs/design.md §2.1

# `detect_heartbeat` journal cadence: often enough to bracket a starvation
# window (a flag-dump write measurably halved the detect rate for ~10 s —
# BACKLOG, checkpoint 33), rare enough to be journal noise-free.
HEARTBEAT_INTERVAL_SECONDS = 5.0

# IDLE break gate (checkpoint 28): out of a fresh rack the only legal shot is
# a break, and the one live phantom that ever started a rack came from racking
# motion (2026-08-01 11:14:49, journal-pinned: the phantom started the clock
# 19 s early and swallowed the real break). The *number* and the measurements
# behind it live with the mode that owns it — `clock/modes.py`, since it is a
# match rule and `run_out` deliberately has no floor. Two things about the
# inputs belong here, with the code that reads them:
#
# `ShotTaken.speed_mms` is the fastest observed ball in the strike-candidate
# window — NOT the candidate target's own raw, which underreads on handoff
# strikes (the gate ate a real break on 2026-08-01: target remnant read
# 2072 mm/s while the reborn cue flew 5300+; `break_0801` + its corpus speed
# test pin the fix). Since checkpoint 33 it additionally carries the confirm's
# own kinematics, because the gate also ate a break whose white streaked too
# fast to be *observed* (705 mm/s on a frame-verified rack explosion,
# `break_underread_0801`). Fast ordinary shots therefore read higher than the
# pre-33 margins — a real shot passing the gate starts the clock, which is the
# right failure direction. Events without a speed (manual/synthetic) pass.
#
# REMOVED 2026-08-02 (checkpoint 57, closing #10): a second IDLE gate that
# ignored every *newborn* fire regardless of speed (checkpoint 33). Recorded
# here because deleting a guard silently is how it comes back.
#
# It was justified by flagged_9/flagged_14 (3489 and 3926 mm/s), read at the
# time as hard hand-rolled returns that started the rack while Steven was
# still placing balls. On 2026-08-02 he watched both at the cold-replay event
# times: **they are 9-ball breaks**, and both fired newborn. So the stated
# discriminator — real breaks fire rest-anchored, hand-rolled balls fire
# newborn — was measured on two examples of the very thing it was supposed to
# admit, and the residual the original note conceded ("a break from a fully
# occluded address fires newborn and is eaten") was not hypothetical: it was
# the evidence.
#
# The instrumentation run that closed #10 reported both halves honestly:
#
#   * Its *positive control* did NOT pass in the intended sense. Nine
#     rack-setup scenarios produce exactly two ShotTaken between them, and
#     both are the genuine shots in those clips (scratch_replace,
#     cue_grab_0801), both rest-anchored. Racking and placement themselves are
#     now event-silent — checkpoints 28-56 fixed that — so the corpus contains
#     no newborn rack-setup fire at all, and mere silence above the floor
#     could not have proven this gate redundant.
#   * What decided it was positive evidence, not silence. **Seven real strikes
#     in the corpus fire newborn** (jaw_strike_0801, occluded_address_0731,
#     lone_roller_0801, scratch_shot_0801, parked_vote_strike_0801,
#     address_chatter_miss_0801, drill_shot_0801) — the occluded-address and
#     own-rest paths are newborn *by design*. Against that, zero rack-setup
#     motions fire newborn. The premise "newborn implies not a real shot" is
#     contradicted nine times over and supported nowhere.
#
# The speed floor below independently covers the failure this was reaching
# for: design.md measures racking and placement lookalikes below 1000 mm/s
# against breaks at 5845-7361, and the floor sits at 3000. Do not re-derive
# anything from flagged_9/flagged_14 — both were recorded during detect-loop
# starvation (41.9 and 34.0 Hz against ~78 nominal) and have been deleted.


class Hub:
    def __init__(self, score_path: Path | None = None, journal_path: Path | None = None) -> None:
        self.clock = ShotClock(
            get_ruleset(DEFAULT_RULESET), now=time.monotonic(), mode=get_mode(DEFAULT_MODE)
        )
        self.clients: set[WebSocket] = set()
        self.score = MatchScore(path=score_path)  # None = in-memory only
        # Match-day kill switch: when False, vision events are ignored and
        # the clock runs manually (Stream Deck / voice / control panel).
        self.vision_enabled = True
        # Every command / vision event / clock state transition, timestamped —
        # ring dumps copy the tail so a flagged clip records what the live
        # engine actually did (journal.py module docstring).
        self.journal = SessionJournal(path=journal_path)
        self._journal_state = self.clock.state.value
        # Detect-loop telemetry: the `cueclock run` camera thread stamps each
        # processed detect frame via note_frame(); the tick loop turns the
        # stamps into `detect_heartbeat` journal lines. Plain attribute
        # rebinding only on the camera thread (atomic under the GIL) — all
        # journal writes stay on the asyncio loop (SessionJournal contract).
        self._detect_frames = 0
        self._detect_last: tuple[float, float] | None = None  # (frame_t, mono when processed)
        self._heartbeat_prev: tuple[float, int] | None = None  # (mono, frames) at last line
        # When the last shot was seen, monotonic — the instant replay's anchor
        # (`GET /api/replay_anchors`). Stamped for every ShotTaken the vision core
        # emits, *before* the kill switch / mode / break gates: those decide
        # whether a shot moves the clock, and replay asks a different question —
        # what is on the video. So the anchor still works in `no_clock` mode,
        # which is exactly where drills happen. Cost: a phantom shot anchors a
        # phantom replay, which is a bug you would want to see anyway.
        self._last_shot_mono: float | None = None
        # Where the current rack starts — the other anchor the Replay key seeks
        # to, on a *held* press ("play the whole rack" rather than "play that
        # shot"). Two stamps because the useful start is the break, not the
        # racking: the SET UP press is taken first, then superseded by the first
        # shot after it, so a rack replay opens on the break instead of on a
        # minute of someone building a triangle. None until either happens,
        # which the deck reads as "as far back as the buffer goes" — the same
        # answer it already gives for a shot older than the buffer.
        self._rack_mono: float | None = None
        self._rack_break_mono: float | None = None
        # Called after a *granted* extension has been broadcast, because the
        # overlay answers that broadcast by speaking "Extension called" out of
        # the TV. `cueclock run` points it at the mic listener's mute window:
        # the phrase contains the keyword, so an un-muted mic hears the
        # announcement and calls the next extension itself (docs/hardware.md
        # Part D). Set by whoever owns both halves; None when nothing listens.
        self.on_extension_granted: Callable[[], None] | None = None

    def _log(self, kind: str, **detail: object) -> None:
        state = self.clock.state.value
        self.journal.log(
            kind,
            state=state,
            remaining=round(self.clock.remaining, 2),
            rack_shots=self.clock.rack_shots,
            **detail,
        )
        self._journal_state = state

    def last_shot_age(self) -> float | None:
        """Seconds since the last ShotTaken, or None if none this session.

        Deliberately an *age* rather than a timestamp: the caller is the deck
        daemon, a separate process whose monotonic clock shares no epoch with
        this one. An age is comparable across processes; a `monotonic()` value
        is not.
        """
        if self._last_shot_mono is None:
            return None
        return time.monotonic() - self._last_shot_mono

    def rack_age(self) -> float | None:
        """Seconds since this rack began, or None if it hasn't begun yet.

        The break when one has been seen, the SET UP press before that — see
        the stamps in `__init__`. An age rather than a timestamp for the same
        reason `last_shot_age` is one: the caller is another process.

        `no_clock` has no SET UP key at all, so out there this is the first
        shot of the session until something racks. That is the honest answer:
        a drill has no racks, and the deck's fallback (the whole buffer) is
        what "the rack" means when nothing has declared one.
        """
        anchor = self._rack_break_mono if self._rack_break_mono is not None else self._rack_mono
        if anchor is None:
            return None
        return time.monotonic() - anchor

    def payload(self, snapshot: Snapshot | None = None) -> dict[str, object]:
        """The composed broadcast/state payload: clock + `vision` + `score`."""
        snap = snapshot if snapshot is not None else self.clock.snapshot()
        return {
            **snap.as_dict(),
            "vision": self.vision_enabled,
            "score": self.score.as_dict(),
        }

    # ---------- WebSocket clients ----------

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.clients.add(ws)
        # Snapshot immediately so a new overlay renders without waiting a tick.
        await ws.send_text(json.dumps(self.payload()))

    def disconnect(self, ws: WebSocket) -> None:
        self.clients.discard(ws)

    async def broadcast(self, snapshot: Snapshot) -> None:
        message = json.dumps(self.payload(snapshot))
        for ws in list(self.clients):
            try:
                await ws.send_text(message)
            except Exception:
                self.clients.discard(ws)

    async def tick_loop(self) -> None:
        while True:
            now = time.monotonic()
            snapshot = self.clock.tick(now)
            if snapshot.state != self._journal_state:
                # GRACE/EXPIRED/auto-restart transitions happen inside tick.
                self._log("tick_transition")
            self._maybe_heartbeat(now)
            await self.broadcast(snapshot)
            await asyncio.sleep(BROADCAST_INTERVAL_SECONDS)

    # ---------- detect-loop telemetry (journal heartbeat) ----------

    def note_frame(self, frame_t: float) -> None:
        """Camera-thread stamp: one detect frame processed, captured at ``frame_t``.

        ``frame_t`` is the frame's capture timestamp (camera clock, seconds
        since the session's first frame — the same timeline vision events
        carry). Attribute rebinding only; no journal I/O off the asyncio loop.
        """
        self._detect_frames += 1
        self._detect_last = (frame_t, time.monotonic())

    def _maybe_heartbeat(self, now: float) -> None:
        """Journal a `detect_heartbeat` line every ~5 s while frames flow.

        Fields: ``hz`` — effective detect rate since the previous line;
        ``frame_age_s`` — how stale the newest *processed* frame is at emit
        time (healthy: one detect period; a flag-dump write stalling the
        grab loop shows up as seconds); ``frame_t`` — that frame's capture
        timestamp, which paired with this record's ``mono`` anchors the
        camera timeline to the journal clock, making per-event capture→emit
        lag computable. Emitted from the tick loop, which keeps beating
        while the camera thread is starved — exactly when it matters.
        """
        if self._detect_last is None:
            return  # no vision feeding this hub (bare server): stay silent
        if self._heartbeat_prev is None:
            self._heartbeat_prev = (now, self._detect_frames)
            return
        prev_mono, prev_frames = self._heartbeat_prev
        if now - prev_mono < HEARTBEAT_INTERVAL_SECONDS:
            return
        frame_t, frame_mono = self._detect_last
        self._log(
            "detect_heartbeat",
            hz=round((self._detect_frames - prev_frames) / (now - prev_mono), 1),
            frame_age_s=round(now - frame_mono, 3),
            frame_t=round(frame_t, 3),
        )
        self._heartbeat_prev = (now, self._detect_frames)

    # ---------- ground truth (#19) ----------

    def ground_truth_press(self) -> None:
        """Record that a shot was actually taken. Never touches the clock.

        One unconditional key, "I shot", pressed every shot with no judgement
        attached — that is the only fact a human holds which the machine does
        not. Everything else (missed / phantom / late) the server derives by
        diffing this press stream against emitted ShotTaken events; see
        `cueclock report`. **If a change to this feature would add a case the
        player must recognise at the table, the change is wrong** — push the
        branch into the server (Steven, 2026-08-02).

        Deliberately not a coroutine and deliberately not broadcasting: an
        observation is not a command, and nothing about the clock changes.

        Stamped on the *capture* clock, like vision events, by extrapolating
        from the newest processed frame — so alignment never has to cross the
        emit-time/capture-time boundary, which is exactly where detect-loop
        starvation hides. With no vision feeding this hub there is no capture
        clock to stamp against, and `t` is null: the report treats such
        presses as unanchored rather than guessing.
        """
        anchor: float | None = None
        if self._detect_last is not None:
            frame_t, frame_mono = self._detect_last
            anchor = round(frame_t + (time.monotonic() - frame_mono), 3)
        self._log("ground_truth_press", t=anchor, frame_t=anchor)

    # ---------- commands (each also broadcasts, per the "on every event" rule) ----------

    async def start(self) -> None:
        self.clock.start(time.monotonic())
        self._log("cmd_start")
        await self.broadcast(self.clock.snapshot())

    async def pause(self) -> None:
        self.clock.pause()
        self._log("cmd_pause")
        await self.broadcast(self.clock.snapshot())

    async def reset(self) -> None:
        self.clock.reset()
        self._log("cmd_reset")
        await self.broadcast(self.clock.snapshot())

    async def new_rack(self) -> None:
        now = time.monotonic()
        self.clock.new_rack(now)
        # A new rack retires the old replay anchor: the press starts it, and
        # the next shot (the break) will supersede this stamp.
        self._rack_mono, self._rack_break_mono = now, None
        self._log("cmd_new_rack")
        await self.broadcast(self.clock.snapshot())

    async def restart(self) -> None:
        self.clock.restart(time.monotonic())
        self._log("cmd_restart")
        await self.broadcast(self.clock.snapshot())

    async def push(self) -> None:
        self.clock.push(time.monotonic())
        self._log("cmd_push")
        await self.broadcast(self.clock.snapshot())

    async def extension(self) -> bool:
        """Grant an extension if the rules allow, and have the overlay say so.

        The cue rides the same one-shot `cue` field as the urgency tones, so
        the announcement fires for the deck key, the API and the voice alike —
        a ref calls the extension, not the microphone. A refused call still
        cues (`extension_denied`): silence would be indistinguishable from a
        mic that never heard the shout, at the table, mid-shot.
        """
        accepted = self.clock.extension()
        self._log("cmd_extension", accepted=accepted)
        await self.broadcast(
            self.clock.snapshot(cue="extension" if accepted else "extension_denied")
        )
        if accepted and self.on_extension_granted is not None:
            self.on_extension_granted()
        return accepted

    async def set_ruleset(self, name: str) -> bool:
        """Switch ruleset by canonical name or alias; False if unknown."""
        try:
            ruleset = get_ruleset(name)
        except KeyError:
            return False
        self.clock.set_ruleset(ruleset)
        self._log("cmd_ruleset", ruleset=self.clock.ruleset.name)
        await self.broadcast(self.clock.snapshot())
        return True

    async def set_mode(self, name: str) -> bool:
        """Switch session mode by name; False if unknown.

        One command, one journal line — `no_clock` has to both stop vision
        driving the clock and park the countdown, and a triage reader should
        see that as one intent rather than reassembling it from two.
        """
        try:
            mode = get_mode(name)
        except KeyError:
            return False
        self.clock.set_mode(mode)
        self._log("cmd_mode", mode=self.clock.mode.name)
        await self.broadcast(self.clock.snapshot())
        return True

    async def set_vision(self, enabled: bool) -> None:
        self.vision_enabled = enabled
        self._log("cmd_vision", enabled=enabled)
        await self.broadcast(self.clock.snapshot())

    async def score_changed(self) -> None:
        """Broadcast after any MatchScore mutation (score rides every payload)."""
        await self.broadcast(self.clock.snapshot())

    # ---------- vision events (dispatched from the `cueclock run` camera thread) ----------

    # Journal timestamp contract (2026-08-01): `frame_t` is the *confirming
    # frame's capture timestamp* — camera clock, seconds since the session's
    # first frame, the exact `t` the vision core was fed — named unambiguously
    # against the record's own `mono` stamp, which is emit time. `t` carries
    # the same value and is kept for old readers. Capture→emit lag per event =
    # this record's `mono` minus the camera-to-mono anchor the nearest
    # `detect_heartbeat` line provides (its `mono` minus `frame_age_s` ≈ the mono
    # instant of its `frame_t`).

    async def shot_taken(
        self,
        t: float | None = None,
        ball_id: int | None = None,
        speed_mms: float | None = None,
        newborn: bool = False,
    ) -> None:
        detail: dict[str, object] = {"t": t, "frame_t": t, "ball": ball_id, "newborn": newborn}
        if speed_mms is not None:
            detail["speed"] = round(speed_mms)
        self._last_shot_mono = time.monotonic()  # replay anchor — before every gate
        if self._rack_break_mono is None:
            # First shot since the last SET UP: the break, and from here on the
            # rack replay opens on it. Ungated for the same reason as above.
            self._rack_break_mono = self._last_shot_mono
        if not self.vision_enabled:
            self._log("vision_shot_taken_killed", **detail)
            return  # kill switch: misbehaving vision must not move the clock
        if not self.clock.mode.clock_live:
            self._log("vision_shot_taken_no_clock", **detail)
            return  # no_clock mode: still journaled as evidence, never clocked
        floor = self.clock.mode.break_floor_mms
        if (
            self.clock.state is State.IDLE
            and floor is not None
            and speed_mms is not None
            and speed_mms < floor
        ):
            # Racking / placement motion, not a break — see the gate note
            # above. Speed only: the newborn half was removed at checkpoint 57.
            self._log("vision_shot_taken_ignored_idle", **detail)
            return
        self.clock.shot_taken()
        self._log("vision_shot_taken", **detail)
        await self.broadcast(self.clock.snapshot())

    async def balls_settled(self, t: float | None = None) -> None:
        if not self.vision_enabled:
            self._log("vision_balls_settled_killed", t=t, frame_t=t)
            return  # kill switch, as above
        if not self.clock.mode.clock_live:
            self._log("vision_balls_settled_no_clock", t=t, frame_t=t)
            return  # journaled, not clocked — as above
        self.clock.balls_settled(time.monotonic())
        self._log("vision_balls_settled", t=t, frame_t=t)
        await self.broadcast(self.clock.snapshot())

    async def scratch_seen(self, t: float | None = None) -> None:
        # No clock action in v1 (CHANGELOG decision) — journal only.
        self._log("vision_scratch", t=t, frame_t=t)
