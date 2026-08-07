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
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    # Type-only: importing the vision stack at runtime would drag cv2/numpy
    # into the bare server. The hub reads the summary as plain evidence.
    from cueclock.vision.pipeline import RearrangeSummary

BROADCAST_INTERVAL_SECONDS = 0.1  # 10 Hz, per docs/design.md §2.1

# E9 frozen-clock watchdog (accuracy plan §13.3): MOTION with the vision
# motion bit low for this long synthesizes the settle the vision layer lost.
# Anchored: the corpus settle-latency distribution puts p99 at ~8.5 s and the
# longest measured real roll-out at 8.62 s (report.PHANTOM_LEAD_S), so 12 s is
# past every real settle with margin while still healing a frozen clock
# before the player has to walk over. Journal-marked `settle_synthesized` so
# triage still sees the underlying miss.
FROZEN_WATCHDOG_SECONDS = 12.0

# E8 break adjudication (accuracy plan §13.2): out of an armed rack the break
# is a settle-time decision — WNT §5 makes the break untimed and starts the
# 60 s window when the balls come to rest, so there is nothing to catch in
# flight. A settle whose rearrangement is break-scale counts the break:
#
#   * with a ShotTaken fire since the rack armed — at ANY speed, including
#     the sub-floor fires the IDLE gate eats (84 across 31 sessions; 15 of
#     them needed a manual rescue press, with eaten fires measured from
#     443 up to 13059 mm/s) — a moderate rearrangement floor suffices.
#   * with no fire at all (the dark-flight class: motion that happens
#     between track identities), only an overwhelming rearrangement would
#     count — currently SHADOW-ONLY (`break_dark_candidate` journal mark,
#     no clock action) because its one hazard is unmeasurable offline.
#
# The rearrangement floors are anchored to the 2026-08-06 corpus probe
# (`rearrange2_probe.py`, quiet-anchored baselines): breaks involve 7-12
# balls (7, 7, 8, 10, 12 across the well-founded runs), ordinary shots 1-2,
# pickups 1, and racking/placement clips never settle at all — so 5 clears
# every ordinary settle with the same margin it gives every measured break.
# The named hazard this policy is measured against is the 2026-08-01
# racking countdown (checkpoint 28). Corpus racking is event-silent, but
# live sessions do log ~3 IDLE settles per session whose rearrangement was
# never recorded — that unpriced population is why the dark tier stays
# shadow, and every settle now journals its rearrangement so the next
# session prices it. Worst case for the fired tier, pre-accepted in the
# plan (§13.4): a spurious IDLE→MOTION during racking costs one New Rack
# press, against 13-22 missed-break presses per 30 sessions today.
BREAK_FIRED_MIN_INVOLVED = 5
BREAK_DARK_MIN_INVOLVED = 8
# A fire arms adjudication only for the settle of its own motion episode:
# the longest measured fire→settle roll-out is 8.62 s (report.PHANTOM_LEAD_S;
# the dark-flight break settles 7.8 s after its fire), so 15 s covers every
# real break while refusing to let stale racking noise arm a much later
# settle.
BREAK_FIRE_SETTLE_MAX_SECONDS = 15.0
# Post-scratch grace (#25): how long an un-refuted ScratchCandidate freezes
# the fresh countdown — the walk to the return, fish the cue out, place it.
# The issue's own spec (5 s), not a measurement; the felt number to revisit
# from live sessions if it reads short on the wall. Policy shape (§13): the
# candidate grants it, a refutation releases it early (typically same-frame
# for a false scratch, so a phantom never visibly touches the clock), and
# each candidate grants at most once — the confirmed Scratch verdict itself
# still never moves the clock.
SCRATCH_HOLD_SECONDS = 5.0

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
        # E9: the vision motion bit (note_motion, camera thread) and the two
        # monotonic stamps its policies compare against — when the bit was
        # last high, and when the clock last entered MOTION. Both feed the
        # frozen-clock watchdog; the bit itself also holds GRACE open.
        self._table_motion = False
        self._motion_high_mono: float | None = None
        self._motion_entered_mono: float | None = None
        # E8: the newest ShotTaken that landed while the clock sat in IDLE —
        # at any speed, before the break floor gate. (mono, speed_mms). The
        # adjudicated settle reads it as its fired-tier evidence; operator
        # commands that move the clock clear it.
        self._idle_fire: tuple[float, float | None] | None = None
        # When the last shot was seen, monotonic — the instant replay's anchor
        # (`GET /api/replay_anchors`). Stamped for every ShotTaken the vision core
        # emits, *before* the kill switch / mode / break gates: those decide
        # whether a shot moves the clock, and replay asks a different question —
        # what is on the video. So the anchor still works in `no_clock` mode,
        # which is exactly where drills happen. Cost: a phantom shot anchors a
        # phantom replay, which is a bug you would want to see anyway.
        self._last_shot_mono: float | None = None
        # Where the rack worth replaying starts — the other anchor the Replay
        # key seeks to, on a *held* press ("play the whole rack" rather than
        # "play that shot"). It is the **break**, never the racking: New Rack only
        # *arms* the next shot to become it. So between racking and breaking the
        # anchor is still the rack that just ended, which is the whole point —
        # you run out, you rack up, and then you ask to see how you did it.
        # None until the session's first break, which the deck reads as "as far
        # back as the buffer goes" — the same answer it already gives for an
        # anchor older than the buffer.
        self._rack_break_mono: float | None = None
        self._rack_pending = False  # New Rack seen: the next shot is the break
        # #25 bookkeeping: a ScratchCandidate stands un-refuted (the vision
        # core guarantees exactly one verdict per candidate, within its 30 s
        # horizon), and whether it has already spent its one hold grant —
        # a candidate that outlives a whole shot cycle must not hold the
        # *next* shot's clock too.
        self._scratch_candidate_open = False
        self._scratch_hold_done = False
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
        """Seconds since the last break, or None if none has been seen.

        Always the break, never the New Rack press — see the stamps in
        `__init__`. An age rather than a timestamp for the same reason
        `last_shot_age` is one: the caller is another process.

        Between a New Rack press and the break that answers it this still reports
        the *previous* rack, deliberately. A table that has just been racked has
        nothing on it to watch, and the rack you want at that moment is the one
        you just played. Reporting the press instead was the live failure of
        2026-08-05: New Rack, then a held Replay 2.0 s later, cued 595.1 s into a
        598.5 s file and put 3.4 s of an empty table on the wall.

        `no_clock` has no New Rack key at all, so out there this is the first
        shot of the session until something racks. That is the honest answer:
        a drill has no racks, and the deck's fallback (the whole buffer) is
        what "the rack" means when nothing has declared one.
        """
        if self._rack_break_mono is None:
            return None
        return time.monotonic() - self._rack_break_mono

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
            snapshot = self.clock.tick(now, motion=self._table_motion)
            if snapshot.state != self._journal_state:
                # GRACE/EXPIRED/auto-restart transitions happen inside tick.
                self._log("tick_transition")
            self._watch_frozen_clock(now)
            self._maybe_heartbeat(now)
            await self.broadcast(snapshot)
            await asyncio.sleep(BROADCAST_INTERVAL_SECONDS)

    # ---------- detect-loop telemetry (journal heartbeat) ----------

    def note_motion(self, moving: bool) -> None:
        """Camera-thread stamp: the vision layer's table-motion bit (§13.3).

        Identity-free evidence, not an event: the pipeline computes it from
        raw per-track speeds with none of the event layer's roster or rest
        machinery, which is what lets it catch that machinery lying. Same
        threading contract as :meth:`note_frame` — attribute rebinding only.
        """
        self._table_motion = moving
        if moving:
            self._motion_high_mono = time.monotonic()

    def _watch_frozen_clock(self, now: float) -> None:
        """E9: synthesize the settle a frozen MOTION clock is waiting for.

        MOTION with the motion bit low for `FROZEN_WATCHDOG_SECONDS` means
        the vision layer latched a shot (real or phantom) and lost the
        settle: nothing is moving, and without help the clock stays frozen
        until an operator key rescues it (14 of those over 30 sessions).
        The synthesized settle rides the normal compensation path — a shot
        with no strike still gets counted — and journals its own kind so
        triage sees the underlying miss rather than a clean settle.
        """
        if self.clock.state is not State.MOTION:
            self._motion_entered_mono = None
            return
        if self._motion_entered_mono is None:
            self._motion_entered_mono = now
        anchor = self._motion_entered_mono
        if self._motion_high_mono is not None:
            anchor = max(anchor, self._motion_high_mono)
        if self._table_motion or now - anchor < FROZEN_WATCHDOG_SECONDS:
            return
        self.clock.balls_settled(now)
        self._log("settle_synthesized", quiet_s=round(now - anchor, 2))
        self._motion_entered_mono = None

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

    # ---------- commands (each also broadcasts, per the "on every event" rule) ----------

    async def start(self) -> None:
        self.clock.start(time.monotonic())
        self._idle_fire = None
        self._log("cmd_start")
        await self.broadcast(self.clock.snapshot())

    async def pause(self) -> None:
        self.clock.pause()
        self._idle_fire = None
        self._log("cmd_pause")
        await self.broadcast(self.clock.snapshot())

    async def reset(self) -> None:
        self.clock.reset()
        self._idle_fire = None
        self._log("cmd_reset")
        await self.broadcast(self.clock.snapshot())

    async def new_rack(self) -> None:
        now = time.monotonic()
        self.clock.new_rack(now)
        self._idle_fire = None
        # New Rack *arms* the next shot to become the replay anchor; it does not
        # retire the old one. Until the break lands there is nothing on the
        # table to watch, so the rack worth replaying is still the one that
        # just ended — see `rack_age`.
        self._rack_pending = True
        self._log("cmd_new_rack")
        await self.broadcast(self.clock.snapshot())

    async def restart(self) -> None:
        self.clock.restart(time.monotonic())
        self._idle_fire = None
        self._log("cmd_restart")
        await self.broadcast(self.clock.snapshot())

    async def push(self) -> None:
        self.clock.push(time.monotonic())
        self._idle_fire = None
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
        self._idle_fire = None
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
        self._idle_fire = None
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
        if self._rack_pending or self._rack_break_mono is None:
            # The break: the first shot after a New Rack press, or the session's
            # first shot (a `no_clock` drill has no New Rack key at all). From
            # here on the rack replay opens on it, and the previous rack is
            # released. Ungated for the same reason as above.
            self._rack_break_mono = self._last_shot_mono
            self._rack_pending = False
        if not self.vision_enabled:
            self._log("vision_shot_taken_killed", **detail)
            return  # kill switch: misbehaving vision must not move the clock
        if not self.clock.mode.clock_live:
            self._log("vision_shot_taken_no_clock", **detail)
            return  # no_clock mode: still journaled as evidence, never clocked
        floor = self.clock.mode.break_floor_mms
        if self.clock.state is State.IDLE:
            # E8 evidence, stamped before the floor gate: whatever the gate
            # decides, a fire happened out of IDLE, and the settle that
            # follows may adjudicate it as the break (`balls_settled`).
            # In-flight tracks under-read speed 2-4x, so the fires the floor
            # eats are mostly real breaks — 15 of the 84 eaten fires on
            # record needed a manual rescue press.
            self._idle_fire = (time.monotonic(), speed_mms)
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

    async def balls_settled(
        self,
        t: float | None = None,
        rearrange: "RearrangeSummary | None" = None,
        backstop: bool = False,
    ) -> None:
        detail: dict[str, object] = {"t": t, "frame_t": t}
        if backstop:
            # The pipeline's quiet-rearrangement backstop emitted this settle
            # because the event engine slept through the whole episode
            # (flag_missed_4: a 15-ball break, zero engine events). Marked so
            # triage and `report` see the engine's miss, not a clean settle.
            detail["backstop"] = True
        if rearrange is not None:
            detail["involved"] = rearrange.involved
            detail["moved"] = rearrange.moved
            detail["appeared"] = rearrange.appeared
            detail["vanished"] = rearrange.vanished
            detail["moved_mm"] = round(rearrange.moved_mm)
        if not self.vision_enabled:
            self._log("vision_balls_settled_killed", **detail)
            return  # kill switch, as above
        if not self.clock.mode.clock_live:
            self._log("vision_balls_settled_no_clock", **detail)
            return  # journaled, not clocked — as above
        now = time.monotonic()
        if self._adjudicate_break(now, rearrange):
            # E8: an armed rack's settle with break-scale rearrangement IS
            # the break (WNT §5 starts the 60 s window at the rest, so
            # nothing was ever lost by deciding here). Count the shot, then
            # let the settle below start the first-shot window.
            self.clock.shot_taken()
            if self._rack_pending or self._rack_break_mono is None:
                self._rack_break_mono = now  # replay anchor, as in shot_taken
                self._rack_pending = False
        self.clock.balls_settled(now)
        self._log("vision_balls_settled", **detail)
        # #25: the scratch's candidate can precede its settle (shot_debug:
        # candidate 9.54 s, settle 11.92 s) — the fresh countdown this settle
        # just started is the one the retrieval needs held.
        self._maybe_hold_for_scratch()
        await self.broadcast(self.clock.snapshot())

    def _adjudicate_break(self, now: float, rearrange: "RearrangeSummary | None") -> bool:
        """E8 (§13.2): should this IDLE settle be counted as the break?

        Policy, not detection: the event layer emits whatever fires it can
        honestly confirm, and this is the one place that decides what an
        armed rack's settle means. Two tiers, both anchored to the corpus
        rearrangement probe (constants above): a fire of *any* speed since
        the rack armed plus a moderate scatter, or — for the dark-flight
        class, where the whole break happens between track identities — an
        overwhelming scatter alone. Racking placements settle 1-3 balls at
        a time and never arm a fire faster than 15 s before their settle,
        so the 2026-08-01 racking-countdown hazard fails both tiers.
        """
        if self.clock.state is not State.IDLE:
            return False
        if not (self.clock.mode.rack_scoped and self.clock.rack_shots == 0):
            return False  # only an armed rack has a break to adjudicate
        if rearrange is None:
            return False
        fired = (
            self._idle_fire is not None
            and now - self._idle_fire[0] <= BREAK_FIRE_SETTLE_MAX_SECONDS
        )
        if fired and rearrange.involved >= BREAK_FIRED_MIN_INVOLVED:
            speed = self._idle_fire[1] if self._idle_fire is not None else None
            self._log(
                "break_adjudicated",
                tier="fired",
                involved=rearrange.involved,
                moved_mm=round(rearrange.moved_mm),
                fire_speed=round(speed) if speed is not None else None,
            )
            self._idle_fire = None
            return True
        if not fired and rearrange.involved >= BREAK_DARK_MIN_INVOLVED:
            # Shadow only (E5's philosophy, §13.3): the no-fire tier's one
            # unmeasured hazard is a racking settle — the pack resolving from
            # a merged blob into singles is appear-heavy exactly like a break,
            # and the corpus cannot price it because corpus racking never
            # settles at all. The journal mark makes the next few sessions
            # the measurement; flip it live when racking settles prove to
            # stay under the floor.
            self._log(
                "break_dark_candidate",
                involved=rearrange.involved,
                moved_mm=round(rearrange.moved_mm),
            )
        return False

    async def scratch_seen(self, t: float | None = None) -> None:
        # The confirmed verdict never moves the clock — the hold (#25) was
        # already granted on the candidate, and by census-completion time the
        # retrieval is underway. Since #26 this is a candidate the whiteness
        # census could not refute; journals without candidate records predate
        # the split and stay what they always were — an upper bound.
        self._scratch_candidate_open = False
        self._log("vision_scratch", t=t, frame_t=t)

    async def scratch_candidate(
        self,
        t: float,
        *,
        x_mm: float,
        y_mm: float,
        pocket_x_mm: float,
        pocket_y_mm: float,
        whiteness: float | None,
        color_age_s: float | None,
    ) -> None:
        # Two-stage scratch verdict (#26), evidence half — always journaled,
        # so a live candidate is auditable from the journal alone.
        self._scratch_candidate_open = True
        self._scratch_hold_done = False
        self._log(
            "vision_scratch_candidate",
            t=t,
            frame_t=t,
            x_mm=round(x_mm),
            y_mm=round(y_mm),
            pocket_x_mm=round(pocket_x_mm),
            pocket_y_mm=round(pocket_y_mm),
            whiteness=round(whiteness, 3) if whiteness is not None else None,
            color_age_s=round(color_age_s, 2) if color_age_s is not None else None,
        )
        # #25: the candidate usually lands ~0.3-1.3 s *after* the settle
        # already started the next countdown (scratch_shot_0801: settle 5.73,
        # candidate 6.37) — hold that countdown now. The other order (candidate
        # during MOTION, shot_debug's shape) is covered in balls_settled.
        self._maybe_hold_for_scratch()

    async def scratch_refuted(
        self, t: float, *, witness_id: int, x_mm: float, y_mm: float, whiteness: float
    ) -> None:
        # The witness's position and reading ride along so any refutation
        # can be spot-audited against footage later.
        self._scratch_candidate_open = False
        self._log(
            "vision_scratch_refuted",
            t=t,
            frame_t=t,
            witness_id=witness_id,
            x_mm=round(x_mm),
            y_mm=round(y_mm),
            whiteness=round(whiteness, 3),
        )
        # #25: nobody is at the return — resume the countdown immediately.
        # For a typical false scratch the refutation is same-frame with the
        # candidate, so the grant/release pair never visibly touches the wall.
        if self.clock.release_hold(time.monotonic()):
            self._log("scratch_hold_released")
            await self.broadcast(self.clock.snapshot())

    def _maybe_hold_for_scratch(self) -> None:
        """Grant the one #25 hold this candidate is entitled to, if due now.

        Gated exactly like the events that move the clock: the kill switch
        and `no_clock` mode must also keep the hold off it. RUNNING-only by
        way of ShotClock.hold — in MOTION the countdown is frozen anyway and
        the settle that ends it re-invokes this.
        """
        if self._scratch_hold_done or not self._scratch_candidate_open:
            return
        if not self.vision_enabled or not self.clock.mode.clock_live:
            return
        if self.clock.state is not State.RUNNING:
            return
        self.clock.hold(time.monotonic(), SCRATCH_HOLD_SECONDS)
        self._scratch_hold_done = True
        self._log("scratch_hold", hold_s=SCRATCH_HOLD_SECONDS)
