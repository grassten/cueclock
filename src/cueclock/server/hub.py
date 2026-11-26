"""WebSocket hub: owns the clock, its 10 Hz tick loop, and connected clients.

This is where wall-clock time enters the system — ``ShotClock`` is pure, and
only this module reads the clock, through the ``now`` it is built with
(``time.monotonic`` live; a virtual clock when `cueclock session replay`
drives it, #57). It also owns the match score and
the vision kill switch, and composes them with the clock snapshot into the
one payload every WS client and ``GET /api/state`` see.
"""

import asyncio
import dataclasses
import functools
import inspect
import json
import time
from collections import deque
from collections.abc import Callable, Coroutine, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Concatenate, ParamSpec, TypeVar

from fastapi import WebSocket

from cueclock.clock import (
    DEFAULT_MODE,
    Settings,
    ShotClock,
    Snapshot,
    State,
    get_mode,
    legacy_name,
    legacy_ruleset,
)
from cueclock.server import bracket as bracket_mod
from cueclock.server.bracket import Bracket, BracketError, Match, Player
from cueclock.server.journal import SessionJournal
from cueclock.server.score import MatchScore
from cueclock.server.scratch import (
    TRAVEL_MIN_MM,
    RestLists,
    SettleFacts,
    cue_px,
    holds_rack,
    judge,
    moved_by_hand,
    nothing_moved,
    only_cue_changed,
    rack_stands,
    table_row,
)

if TYPE_CHECKING:
    # Type-only: importing the vision stack at runtime would drag cv2/numpy
    # into the bare server. The hub reads the summary as plain evidence.
    from cueclock.vision.ball_list import BallList
    from cueclock.vision.events import SettleMover
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
#     between track identities), the pack un-merging is the evidence — an
#     appeared-count floor, live since #36 (see the dark-tier note below).
#
# The rearrangement floors are anchored to the 2026-08-06 corpus probe
# (`rearrange2_probe.py`, quiet-anchored baselines): breaks involve 7-12
# balls (7, 7, 8, 10, 12 across the well-founded runs), ordinary shots 1-2,
# pickups 1, and racking/placement clips never settle at all — so 5 clears
# every ordinary settle with the same margin it gives every measured break.
# The named hazard this policy is measured against is the 2026-08-01
# racking countdown (checkpoint 28). Worst case for the fired tier,
# pre-accepted in the plan (§13.4): a spurious IDLE→MOTION during racking
# costs one New Rack press, against 13-22 missed-break presses per 30
# sessions today.
#
# THE DARK TIER IS LIVE, and it keys on `appeared` (#36, 2026-08-11).
#
# The claim that stood here until #36 — "no measured axis separates a racking
# gather settle from a dark break, the tier stays off" — was FALSE, and how it
# got here is worth keeping: the 21:07:56 settle it rested on was called
# racking by inferring from when Steven pressed buttons afterwards. Replaying
# the dump (`flag_missed_2`, warm and cold) shows 10 pack tracks dying inside
# ~0.9 s and 13 newborns flying up to 1534 mm. Hand-placing balls does not do
# that. 21:07:56 IS the missed break. Ground truth comes from the physics in a
# replay, never from press timing.
#
# The separating axis is `appeared`, and the reason it separates is structural
# rather than statistical: a break UN-MERGES the racked pack — balls the
# detector could not resolve inside a blob acquire their own positions, which
# is precisely what an unmatched current ball (`appeared`) is. Racking runs
# the process backwards, singles merging into the pack, which lands in
# `vanished`. So the axis keys on the un-merge itself and is independent of
# how many balls the rack held — which the old `involved` floor was not.
#
# Measured over every journal carrying the field (111 settles across
# 2026-08-07/08-10/08-11), IDLE-domain — the only domain this tier acts in:
#
#   9-ball breaks (4)                       appeared 5, 6, 6, 6
#   8-ball breaks (corpus + live)           appeared 6, 7, 10, 10, 12
#   every non-break IDLE settle (13)        appeared 0
#     — racking gathers (mv=0 vn=6, the merge signature), placements up to
#       1313 mm, a 2278 mm two-ball gather, cue positioning. Travel does not
#       lift them off zero; only un-merging does.
#
# The 1-4 band is empty, so the floor of 5 sits under the lowest measured
# break with the whole gap beneath it. `involved >= 8` is DEAD: all four
# 9-ball breaks measure involved=7 and 9-ball racking measures 6, a one-ball
# gap that no absolute or population-normalised floor survives.
#
# `pickup_snatch_0801` — the loudest hand event in the corpus, the clip that
# priced this floor — measures appeared=6 and is NOT a counterexample: its
# baseline is a single ball (the clip opens mid-action with no quiet layout
# to anchor), so every ball resolving at the settle had nothing to match
# against. Every corpus break clip is degenerate the same way (baselines
# 1-2), which is why the live journals are the load-bearing record here and
# the corpus anchors nothing on this axis. The mechanism is the check that
# makes it safe: a hand kills tracks by OCCLUSION and they are reborn where
# they already were, so against an anchored baseline they match and land as
# moved/unchanged. `pickup_newborn_0801`, the one hand clip with an honest
# baseline (5 balls -> 5), measures appeared=0 with 814 mm of travel.
#
# Two residuals, both accepted rather than solved:
#   * a soft break that barely separates the pack reads low on `appeared` and
#     stays missed — one press, exactly as today. The settle is journaled
#     with its `appeared`, so the class is visible if it ever appears.
#   * tearing a BUILT rack apart by hand un-merges the pack and is genuinely
#     break-shaped here. Not in the live record; costs one New Rack press —
#     the pre-accepted §13.4 worst case, and the right direction to fail.
BREAK_FIRED_MIN_INVOLVED = 5
BREAK_DARK_MIN_APPEARED = 5
# A fire arms adjudication only for the settle of its own motion episode:
# the longest measured fire→settle roll-out is 8.62 s (report.PHANTOM_LEAD_S;
# the dark-flight break settles 7.8 s after its fire), so 15 s covers every
# real break while refusing to let stale racking noise arm a much later
# settle.
BREAK_FIRE_SETTLE_MAX_SECONDS = 15.0
# Post-scratch grace (#25): how long an un-refuted ScratchCandidate freezes
# the fresh countdown — the walk to the return, fish the cue out, place it.
# The issue's own spec (5 s), not a measurement; the felt number to revisit
# from live sessions if it reads short on the wall. Policy shape (§13): a
# candidate or the settle's ball list (#63, §14) calls it, a refutation or the
# list seeing the cue releases it early (typically same-frame for a false
# scratch, so a phantom never visibly touches the clock), and each shot grants
# at most once — the confirmed Scratch verdict itself still never moves the
# clock.
SCRATCH_HOLD_SECONDS = 5.0

# How long a settle that waits for its ball list (ball in hand, #64 §15; a
# freeze with nothing seen to move, §16; a break over a standing rack, §17)
# is held before it is decided as today. The pipeline's calls that arrive
# meanwhile wait behind it for the same time (#69, `Hub._vision`). Measured
# live, hub time from the settle to its list, over the 81 settles of sessions
# 20261117-201731 and 20261119-020755: median 74 ms, 90th percentile 128 ms,
# and it grows with the table (1-5 balls 34-81 ms, 13 or more 71-174 ms,
# median 142). The one outlier, 537 ms, came in a stretch
# where the detect rate had fallen to 68-89 Hz. The first value was 0.15 s,
# twice the slowest *fit* in #63's brief (20-75 ms), which left out the wait
# for a detect worker: 4 of those 81 lists would have missed it, all with
# nine or more balls listed, which is where a break scratch leaves ball in
# hand. 0.3 s is 1.7x the slowest list of a healthy session. The wait costs
# no clock time: the decision is made as of the settle's own instant.
SETTLE_LIST_WAIT_S = 0.3


@dataclass
class _ShotScratch:
    """Where one shot's scratch question stands (#25, #63).

    A shot is one motion episode (`Hub._episode_epoch`). Either today's logic
    (a ScratchCandidate) or the ball list may call the scratch; either one
    seeing the cue clears or refutes it. Once called, the hold is *owed* until
    it is granted, refuted or cleared, or the next episode begins — a
    confirmed verdict does not cancel it, and it is granted the moment the
    countdown is RUNNING, whichever call gets it there. One hold per shot.
    """

    candidate: bool = False  # today's logic raised a candidate this shot
    listed: bool = False  # the ball list called the scratch at the settle
    granted: bool = False
    closed: str | None = None  # "refuted" (today's logic) | "cleared" (the list)
    says: str | None = None  # what the settle's list said, once it arrived

    @property
    def by(self) -> str:
        return "both" if self.candidate and self.listed else "list" if self.listed else "candidate"

    @property
    def owed(self) -> bool:
        return (self.candidate or self.listed) and not self.granted and self.closed is None


@dataclass
class _BallInHand:
    """Ball in hand after a scratch (#64, §15): from the scratch's shot until
    a settle that moves an object ball. Every settle in between that moves
    only the cue is placing, not a shot."""

    since: float | None  # the scratch settle's frame time: lists before it are the old table
    scratch: _ShotScratch  # the scratch's question: a refutation ends ball in hand
    placed: int = 0  # settles kept off the clock so far


@dataclass
class _HeldSettle:
    """A settle whose clock decision waits for its ball list: during ball in
    hand the list says whether it moved only the cue (#64), and on a freeze
    with nothing seen to move it says whether anything moved at all (§16).
    Everything the decision needs, as it stood when the settle arrived."""

    now: float  # the hub's clock at the settle: the decision is made as of then
    t: float | None
    detail: dict[str, object]
    rearrange: "RearrangeSummary | None"
    backstop: bool


# #37 phantom rejection: how many balls the pre-episode quiet layout must have
# resolved before an `involved == 0` reading is allowed to mean anything.
#
# `involved == 0` says two opposite things depending on where the baseline came
# from (`RearrangeSummary.baseline_balls`): either the table is provably
# unchanged, or the pipeline had no quiet layout and diffed the settled frame
# against itself. `baseline_balls is None` separates those exactly, and is the
# load-bearing half of this gate. The COUNT is the second half, and it covers a
# subtler degeneracy: a baseline that exists but resolved almost nothing (a
# racked pack the detector never split) can also read zero while balls scatter,
# because both sides of the diff hold the same one or two tracks.
#
# Anchored on every settle the corpus produces (2026-08-11 probe, 55 settles
# across 51 clips). Baselines that resolved 1-2 balls are all racked-pack or
# mid-action clips where the layout was never really seen — `break_0801` 1,
# `break_0801b` 1, `break_outrun_0805` 1, `pickup_snatch_0801` 1,
# `break_dark_flight_0806` 2, `jaw_strike_0801` 2, `merge_yank_0731` 2.
# Baselines of 3 and up are honest layouts of the table that was actually
# there, including late-rack tables with genuinely few balls left
# (`pot_scratch_nohold_0811` 3, `lone_roller_0801` 3, `cluster_shot_0801` 4,
# `cluster_rest_shot_0806` 4, and the live phantom's own 4). So 3 is the lowest
# floor that excludes every unresolved-pack baseline on record while still
# covering the end of a rack, which is exactly where the live phantom happened.
#
# The residual, accepted rather than solved: a table down to two balls (cue and
# the 8) cannot be adjudicated at all and every phantom on it stands, as today.
# That is the failing-open direction, and the whole predicate is built to fail
# that way — see `_settle_is_phantom`.
QUIET_BASELINE_MIN_BALLS = 3

# REMOVED 2026-08-11 (checkpoint 87, #38): the #33 Part A settle refusal and
# its SHOTLIKE_MOVER_MIN_TRAVEL_MM floor. Recorded here because deleting a
# guard silently is how it comes back.
#
# It refused a RUNNING settle whose rearrangement read `moved == 0 and
# involved == 1`, to stop a ball-in-hand placement counting as a shot. The
# first played session on it (2026-08-11, 80 shots) measured the predicate
# selecting the wrong population in both directions:
#
#   * It fired **3 times all session** — right once (a post-scratch
#     retrieval, t=1181.8) and wrong twice, on a jump shot (t=585.4) and a
#     no-contact kick (t=1589.6). Both refusals let the countdown run
#     through the following stroke; the jump shot went 15.7 -> 0 -> GRACE.
#     The retrieval and the kick are byte-identical evidence apart from a
#     travel figure the predicate never consulted.
#   * It caught **none of the session's five ball-in-hand carries**. A carry
#     moves a ball from A to B, so `moved == 1` — counted at the predicate's
#     first test, before any evidence #33 added is read. The refusable shape
#     cannot contain the thing it targets.
#
# The #38 measurement pass then tested all five discriminators that could
# have replaced the shape (deceleration profile, path curvature, cue-stick
# presence at onset, hand/arm merged blob at onset, onset->displacement
# latency) over 9 hand episodes and 6 strike episodes: every one overlaps.
# `merged` is one-sided (0.0 on all six strikes) but reads ~0 on every corpus
# hand event too — checkpoint 50's finding, reproduced. Removal was measured
# against the full corpus at zero cost: no clip changes outcome.
#
# Ball-in-hand still must not reset the clock (Steven, 2026-08-11) — that
# problem stays open, without a predicate that is wrong twice as often as it
# is right. `ShotClock.balls_settled(suppress=...)` is deliberately KEPT: the
# clock-side mechanism was never what failed, only the hub's evidence test.
#
# RE-SUPPLIED 2026-08-11 (#41), on evidence rather than shape: a settle was
# refused when every ball that arrived somewhere new was under a hand-scale
# intrusion as it arrived (`pipeline._hand_delivered`). The safety argument
# was the conjunction — "**every** arrival, not any" — resting on the claim
# that a real stroke always leaves at least one ball on open cloth: the
# struck ball itself, or the object ball it sent away.
#
# REMOVED AGAIN 2026-08-29 (checkpoint 104), because live play falsified that
# claim and the conjunction turned out to be vacuous in the shape it almost
# always fires in. Measured over all 43 sessions on record:
#
#   * It fired **10 times ever**, and **9 of the 10 read `arrivals == 1`**.
#     With one arrival there is no conjunction left — "every arrival" and
#     "any arrival" are the same test, and the redundancy the docstring
#     leaned on is simply absent. The shape is not rare: it is what a
#     *pot* looks like. The object ball is potted, so it vanishes and never
#     arrives; the cue rolls a short distance and is the only arrival.
#   * `flag_missed_2` (2026-08-23 20:41:03) is that case with ground truth
#     attached: Steven took a real shot at ~15.1 s and **never touched a
#     ball by hand**. It read `arrivals 1 / hand 1` — the cue's arrival was
#     covered because he was still leaning over the table where it stopped.
#     The clock ran on from 16.14 s into GRACE, EXPIRED, and he restarted by
#     hand and flagged it.
#   * A genuine ball-in-hand carry reads `arrivals 1 / hand 1` too — the
#     four labelled 2026-08-11 carries all did. So the refused population
#     and the protected population are **byte-identical in the evidence this
#     predicate consults**. That is the same wall #38 hit, reached from the
#     other side.
#
# Ball-in-hand still must not reset the clock (Steven, 2026-08-11) and that
# problem is open again — but a test that cannot tell a carry from a pot is
# not a partial answer to it, and refusing a real shot is the worse error by
# the policy `_settle_is_phantom` states. Two removals now agree that this
# verdict needs evidence the rig does not yet produce; the next attempt
# should bring a new *measurement*, not a third arrangement of this one.
#
# `ShotClock.balls_settled(suppress=...)` is KEPT once more: the clock-side
# mechanism was never what failed, only the hub's evidence test.


# `detect_heartbeat` journal cadence: often enough to bracket a starvation
# window (a flag-dump write measurably halved the detect rate for ~10 s —
# BACKLOG, checkpoint 33), rare enough to be journal noise-free.
HEARTBEAT_INTERVAL_SECONDS = 5.0

# Below this detect rate the health line warns (#52). 78 Hz is the cadence
# every event constant was tuned at before detection went to every frame
# (checkpoint 106): under it the engine is running slower than anything it
# was ever measured at, not merely slower than nominal.
DETECT_HZ_WARN = 78.0

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


def _as_int(value: object, default: int) -> int:
    return int(value) if isinstance(value, int | float) else default


def _as_opt_int(value: object) -> int | None:
    return int(value) if isinstance(value, int | float) else None


def _monotonic() -> float:
    """The live clock, looked up per call so tests can pin the module's `time`."""
    return time.monotonic()


_P = ParamSpec("_P")
_R = TypeVar("_R")
# Set while a hub input runs, so the inputs it calls itself (a bracket result
# racks the table through `new_rack`) are not logged a second time. A
# ContextVar rather than a flag: every API request and every
# run_coroutine_threadsafe call is its own asyncio task, and interleaved tasks
# must not see each other's.
_IN_INPUT: ContextVar[bool] = ContextVar("hub_input", default=False)


def hub_input(
    method: "Callable[Concatenate[Hub, _P], Coroutine[Any, Any, _R]]",
) -> "Callable[Concatenate[Hub, _P], Coroutine[Any, Any, _R]]":
    """Mark a Hub coroutine as an input: something from outside vision (#57).

    Every API command, deck key, voice extension and setting / mode / score /
    bracket change reaches the hub through one of these, so this is the one
    seam where they are recorded — `Hub.on_input` gets
    ``{"mono", "wall", "method", "kwargs"}`` before the method runs, and a
    session replay calls ``method(**kwargs)`` at that ``mono``. Vision events
    are not inputs: replay re-derives them from the detection log.
    """
    signature = inspect.signature(method)

    @functools.wraps(method)
    async def wrapper(hub: "Hub", /, *args: _P.args, **kwargs: _P.kwargs) -> _R:
        if _IN_INPUT.get() or hub.on_input is None:
            # A settle waiting for its ball list (#64) is decided as today
            # before any command touches the clock, and what queued behind
            # it is applied: a press is authoritative and never waits.
            hub._flush_held("input")
            return await method(hub, *args, **kwargs)
        bound = signature.bind(hub, *args, **kwargs)
        hub.on_input(
            {
                "mono": round(hub._now(), 4),
                "wall": hub._wall().isoformat(timespec="milliseconds"),
                "method": method.__name__,
                "kwargs": dict(list(bound.arguments.items())[1:]),  # all but self
            }
        )
        token = _IN_INPUT.set(True)
        try:
            hub._flush_held("input")
            return await method(hub, *args, **kwargs)
        finally:
            _IN_INPUT.reset(token)

    return wrapper


def _load_settings(path: Path | None) -> tuple[Settings, str | None]:
    """The remembered settings, and why they could not be read (None if fine).

    No file is the first run, not an error: the defaults, silently.
    """
    if path is None or not path.exists():
        return Settings(), None
    try:
        return Settings.from_dict(json.loads(path.read_text())), None
    except (OSError, ValueError) as exc:
        return Settings(), f"{type(exc).__name__}: {exc}"


class Hub:
    def __init__(
        self,
        score_path: Path | None = None,
        journal_path: Path | None = None,
        bracket_path: Path | None = None,
        archive_dir: Path | None = None,
        *,
        settings_path: Path | None = None,
        now: Callable[[], float] = _monotonic,
        wall: Callable[[], datetime] | None = None,
    ) -> None:
        """``now`` is the monotonic clock every policy reads and ``wall`` the
        matching UTC wall clock the journal stamps; both default to the real
        ones. A session replay passes a virtual pair (#57).

        ``settings_path`` is where the clock's settings are remembered between
        sessions (#74), as the score is; None keeps them in memory."""
        self._now = now
        self._wall = wall if wall is not None else (lambda: datetime.now(UTC))
        # Called with each input record (see `hub_input`); `cueclock run`
        # points it at the session log. None records nothing.
        self.on_input: Callable[[dict[str, object]], None] | None = None
        # The `stream` object of every payload (#81): the app points it at its
        # stream desk (`server/stream.py`). None, as on a bare server and in
        # a session replay, reports `stream: null`.
        self.stream_payload: Callable[[], dict[str, object]] | None = None
        self.settings_path = settings_path
        settings, settings_error = _load_settings(settings_path)
        self.clock = ShotClock(settings, now=now(), mode=get_mode(DEFAULT_MODE))
        self.clients: set[WebSocket] = set()
        self.score = MatchScore(path=score_path)  # None = in-memory only
        # The bracket (#43) is optional state layered on top of the score: with
        # none loaded every page behaves exactly as it did before it existed.
        # A live one is restored from disk so a server restart mid-tournament
        # is survivable, the same way the score is.
        self.bracket_path = bracket_path
        self.archive_dir = archive_dir
        self.bracket: Bracket | None = (
            bracket_mod.load(bracket_path)
            if bracket_path is not None and bracket_path.exists()
            else None
        )
        # Match-day kill switch: when False, vision events are ignored and
        # the clock runs manually (Stream Deck / voice / control panel).
        self.vision_enabled = True
        # Every command / vision event / clock state transition, timestamped —
        # ring dumps copy the tail so a flagged clip records what the live
        # engine actually did (journal.py module docstring).
        self.journal = SessionJournal(path=journal_path, now=now, wall=self._wall)
        self._journal_state = self.clock.state.value
        if settings_error is not None:
            # Today's defaults rather than a failed start. `config/` is an
            # iCloud-pinned symlink, and a launchd job can be refused a file
            # there (EDEADLK, CLAUDE.md): say so, then play on the defaults.
            self._log("settings_unreadable", error=settings_error)
        # Detect-loop telemetry: the `cueclock run` camera thread stamps each
        # processed detect frame via note_frame(); the tick loop turns the
        # stamps into `detect_heartbeat` journal lines. Plain attribute
        # rebinding only on the camera thread (atomic under the GIL) — all
        # journal writes stay on the asyncio loop (SessionJournal contract).
        self._detect_frames = 0
        self._detect_last: tuple[float, float] | None = None  # (frame_t, mono when processed)
        self._heartbeat_prev: tuple[float, int] | None = None  # (mono, frames) at last line
        # Per-stage seconds summed over every frame (queue wait, find_blobs,
        # the loop thread's half), rebound whole on each frame like the
        # counters above; the heartbeat diffs them against its last line.
        self._stage_totals: tuple[float, float, float] = (0.0, 0.0, 0.0)
        self._stage_prev: tuple[float, float, float] = (0.0, 0.0, 0.0)
        # The health line (#52): what the control page and the Session key
        # show. All None on a bare server, which has no vision to report on.
        self._detect_hz: float | None = None
        self._balls: int | None = None
        self._reference_drift: tuple[float, bool] | None = None  # (reading, stale)
        self._drift_journaled = False
        # E9: the vision motion bit (note_motion, camera thread) and the two
        # monotonic stamps its policies compare against — when the bit was
        # last high, and when the clock last entered MOTION. Both feed the
        # frozen-clock watchdog. (It also held GRACE open until #68.)
        self._table_motion = False
        self._motion_high_mono: float | None = None
        self._motion_entered_mono: float | None = None
        # E8: the newest ShotTaken that landed while the clock sat in IDLE —
        # at any speed, before the break floor gate. (mono, speed_mms). The
        # adjudicated settle reads it as its fired-tier evidence; operator
        # commands that move the clock clear it.
        self._idle_fire: tuple[float, float | None] | None = None
        # When the last shot was seen, monotonic — what the deck's idle stop
        # reads (`GET /api/shot_age`, #60) and where solo pace measures to.
        # Stamped for every ShotTaken the vision core emits, *before* the kill
        # switch / mode / break gates: those decide whether a shot moves the
        # clock, and the idle stop asks a different question — is anybody
        # playing. So a `no_clock` drill keeps the session alive.
        self._last_shot_mono: float | None = None
        # #25/#63 bookkeeping: this shot's scratch question (None until a
        # call, a clear or a refutation touches it), the motion episode it
        # belongs to, and whether that episode is still open. A new episode
        # retires the previous shot's question, so a call that outlives its
        # shot can never hold the *next* shot's clock.
        self._scratch: _ShotScratch | None = None
        self._episode_epoch = 0
        self._episode_open = False
        self._episode_motion_t: float | None = None  # frame t the episode began
        # The ball list's side (#63): written at-rest lists by frame, and the
        # last settle's facts until its own list arrives.
        self._rest_lists = RestLists()
        self._settle_facts: SettleFacts | None = None
        self._settle_listed = True  # the last settle got its list (or none was due)
        # #64: ball in hand after a scratch, a settle whose clock decision
        # waits for its list (at most SETTLE_LIST_WAIT_S), and whether the
        # last settle was held at all.
        self._ball_in_hand: _BallInHand | None = None
        self._held_settle: _HeldSettle | None = None
        self._settle_was_held = False
        # #69: the pipeline's calls that arrived while a settle was held,
        # each with the hub time it arrived at (`_vision`). Non-empty only
        # while a settle is held.
        self._queued: deque[tuple[float, Callable[[float], bool]]] = deque()
        # A strike fired in the open episode (§16: a freeze with none is a
        # pure compensation, and its settle may wait for its list).
        self._episode_struck = False
        # §17: this episode's strike took the clock out of IDLE as the break.
        # Its settle may still take that back (the rack never moved).
        self._break_fired = False
        # #33 Part B: when the current provisional episode froze the clock
        # (mono). Doubles as the episode anchor for the scratch guard — a
        # candidate stamped at or after it was born inside this episode, so
        # its vanish is the pot, not a lift. Cleared at every resolution.
        self._provisional_open_mono: float | None = None
        # The solo-pace measurement riding on the provisional's verdict:
        # (pace stamp it consumed, seconds the shot took at the freeze).
        # Commit on a counted shot, restore the stamp on a revert.
        self._pace_pending: tuple[float, float] | None = None
        # Solo panel (#30): run_out and no_clock are single-player, so the
        # bar's scoreboard area shows session numbers instead of a match
        # score. All of it is wall-clock bookkeeping, which is why it lives
        # here and not in the pure clock. Pace: `_pace_since_mono` stamps the
        # start of each fresh countdown, and the ShotTaken that ends it
        # records how long the shot actually took — wall time, so a scratch
        # hold or an extension honestly lengthens the shot that used it.
        self._pace_since_mono: float | None = None
        self._pace_last_s: float | None = None
        self._pace_sum_s = 0.0
        self._pace_shots = 0
        # Drill counters: ShotTaken fires seen while no_clock (approximate —
        # they inherit detection accuracy) and when the mode was entered.
        self._drill_reps = 0
        self._mode_entered_mono = self._now()
        # The ball list (#54) behind the bar's test strip: the labels of the
        # latest *written* list that had no arm over the table (a list with
        # one can show a ball is there, never that one is gone). Set by
        # `cueclock run` through `ball_list`; the switch starts on.
        self._ball_strip: list[str] | None = None
        self.ball_strip_shown = True
        # Spoken control (#76): off = a heard "extension" is journaled and
        # dropped, so the clock takes commands only from the deck and the
        # control page. On with every `run`, like the vision and strip switches.
        self.voice_enabled = True
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
        return self._now() - self._last_shot_mono

    @property
    def clock_live(self) -> bool:
        """May vision move the clock right now?

        Two axes park it: `no_clock` mode (drills) and CLOCK off (a match with
        no shot clock). The journal lines on the False branch keep their
        `_no_clock` names for both; the payload's `mode` and `timed` fields say
        which axis it was.
        """
        return self.clock.mode.clock_live and self.clock.settings.timed

    def payload(self, snapshot: Snapshot | None = None) -> dict[str, object]:
        """The composed broadcast/state payload: clock + `vision` + `score` +
        `solo` + `bracket` + `health` + `ball_strip` + `stream`."""
        snap = snapshot if snapshot is not None else self.clock.snapshot()
        return {
            **snap.as_dict(),
            "vision": self.vision_enabled,
            "voice": self.voice_enabled,
            "score": self.score.as_dict(),
            "solo": self._solo(),
            "bracket": self._bracket_summary(),
            "health": self.health(),
            "ball_strip": {"shown": self.ball_strip_shown, "balls": self._ball_strip},
            "stream": self.stream_payload() if self.stream_payload is not None else None,
        }

    def live_scores(self) -> tuple[int, int]:
        """What the loaded match's two players are on right now.

        Straight off `MatchScore`, because that is the single source of truth
        for the score — the bracket never keeps one of its own, or the wall and
        the bracket page would disagree in front of everyone (#43).
        """
        return self.score.p1_score, self.score.p2_score

    def _bracket_summary(self) -> dict[str, object] | None:
        """The compact bracket object on every broadcast — `null` in casual play.

        Compact deliberately: the full draw is a few kilobytes and changes a
        handful of times an evening, so it rides `GET /api/bracket` and the page
        refetches when `revision` moves here, rather than being re-serialized ten
        times a second for every connected page all night.
        """
        if self.bracket is None:
            return None
        return self.bracket.live_summary(self.live_scores())

    def _solo(self) -> dict[str, object] | None:
        """The solo panel's numbers (#30) — pace in run_out, reps in no_clock.

        None in match, where the scoreboard is the panel. `shots` is the
        clock's own rack counter, so it inherits the missed-strike settle
        compensation for free; `last_s`/`avg_s` are None until the first
        *timed* shot — the opening shot of a run-out starts from IDLE with no
        countdown over it, so there is nothing honest to report for it.
        """
        mode = self.clock.mode.name
        if mode == "run_out":
            return {
                "shots": self.clock.rack_shots,
                "last_s": round(self._pace_last_s, 1) if self._pace_last_s is not None else None,
                "avg_s": round(self._pace_sum_s / self._pace_shots, 1)
                if self._pace_shots
                else None,
            }
        if mode == "no_clock":
            return {
                "reps": self._drill_reps,
                "elapsed_s": round(self._now() - self._mode_entered_mono, 1),
            }
        return None

    def _reset_pace(self) -> None:
        """Zero the pace aggregates — the panel's scope is one continuous run."""
        self._pace_since_mono = None
        self._pace_last_s = None
        self._pace_sum_s = 0.0
        self._pace_shots = 0

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
            await self.broadcast(self.tick())
            await asyncio.sleep(BROADCAST_INTERVAL_SECONDS)

    def tick(self) -> Snapshot:
        """One beat of the tick loop, synchronous so a replay can drive it at
        the live cadence on its virtual clock (#57)."""
        now = self._now()
        self._decide_overdue_settle(now)
        snapshot = self.clock.tick(now)
        if snapshot.state != self._journal_state:
            # GRACE/EXPIRED/auto-restart transitions happen inside tick.
            self._log("tick_transition")
        self._watch_frozen_clock(now)
        self._maybe_journal_drift()
        self._maybe_heartbeat(now)
        return snapshot

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
            self._motion_high_mono = self._now()

    def _watch_frozen_clock(self, now: float) -> None:
        """E9: synthesize the settle a frozen MOTION clock is waiting for.

        MOTION with the motion bit low for `FROZEN_WATCHDOG_SECONDS` means
        the vision layer latched a shot (real or phantom) and lost the
        settle: nothing is moving, and without help the clock stays frozen
        until an operator key rescues it (14 of those over 30 sessions).
        The synthesized settle rides the normal compensation path — a shot
        with no strike still gets counted — and journals its own kind so
        triage sees the underlying miss rather than a clean settle.

        The one freeze it does not count is a break over a standing rack
        (§17): that one goes back to the armed rack instead.
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
        if (
            self._break_fired
            and holds_rack(self._rest_lists.last_before(self._episode_motion_t))
            and self.clock.break_reverted(now)
        ):
            # §17 for the settle that never came (#69): the strike left IDLE
            # as the break with a rack standing, and the table has been quiet
            # ever since. A break is never quiet: its balls roll, and its
            # settle comes within 8.62 s. Counting here would start a
            # countdown on a racked table (session 20261121-153046, 10:38:33:
            # the cue ball set down for the break, 12 s of nothing, 30 s on
            # the wall). Back to the armed rack, as the settle's list would
            # have decided it.
            self._break_fired = False
            self._log(
                "shot_provisional_reverted",
                reason="rack_standing",
                by="watchdog",
                quiet_s=round(now - anchor, 2),
            )
            self._motion_entered_mono = None
            return
        self.clock.balls_settled(now)
        # A synthesized settle carries no evidence, so it fails open and
        # counts (#33) — commit any pace the provisional held and close the
        # episode the same way a real counted settle would.
        self._commit_pace()
        self._provisional_open_mono = None
        # From MOTION a settle always lands RUNNING; the synthesized one
        # starts a real countdown, so it opens the pace timer like any other.
        self._pace_since_mono = now
        self._log("settle_synthesized", quiet_s=round(now - anchor, 2))
        self._motion_entered_mono = None

    def note_frame(
        self,
        frame_t: float,
        *,
        balls: int | None = None,
        stages: tuple[float, float, float] | None = None,
    ) -> None:
        """Camera-thread stamp: one detect frame processed, captured at ``frame_t``.

        ``frame_t`` is the frame's capture timestamp (camera clock, seconds
        since the session's first frame — the same timeline vision events
        carry). ``balls`` is how many balls the tracker holds; ``stages`` is
        this frame's (queue wait, find_blobs, loop half) in seconds.
        Attribute rebinding only; no journal I/O off the asyncio loop.
        """
        self._detect_frames += 1
        self._detect_last = (frame_t, self._now())
        if balls is not None:
            self._balls = balls
        if stages is not None:
            q, b, loop = self._stage_totals
            self._stage_totals = (q + stages[0], b + stages[1], loop + stages[2])

    def note_reference_drift(self, drift: float, stale: bool) -> None:
        """Camera-thread stamp: the launch-time reference check (#52).

        Journaled once by the tick loop (`reference_drift`), and carried on
        every payload so the reading is in front of somebody instead of in a
        log file behind the TV.
        """
        self._reference_drift = (drift, stale)

    def _maybe_journal_drift(self) -> None:
        if self._reference_drift is None or self._drift_journaled:
            return
        drift, stale = self._reference_drift
        self._log("reference_drift", drift=round(drift, 1), stale=stale)
        self._drift_journaled = True

    def health(self) -> dict[str, object] | None:
        """The rig's health line: reference drift at launch, detect Hz, balls.

        ``warn`` is the one bit the deck needs (an amber dot on Session): the
        reference has gone stale, or detection is running slower than any
        rate its constants were tuned at. None on a bare server.
        """
        if self._detect_last is None and self._reference_drift is None:
            return None
        drift, stale = self._reference_drift if self._reference_drift else (None, False)
        slow = self._detect_hz is not None and self._detect_hz < DETECT_HZ_WARN
        return {
            "drift": None if drift is None else round(drift, 1),
            "drift_stale": stale,
            "hz": self._detect_hz,
            "hz_slow": slow,
            "balls": self._balls,
            "warn": stale or slow,
        }

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
            self._stage_prev = self._stage_totals
            return
        prev_mono, prev_frames = self._heartbeat_prev
        if now - prev_mono < HEARTBEAT_INTERVAL_SECONDS:
            return
        frame_t, frame_mono = self._detect_last
        frames = self._detect_frames - prev_frames
        self._detect_hz = round(frames / (now - prev_mono), 1)
        totals = self._stage_totals
        # Mean ms per frame in each stage since the last line (#52). Absent
        # when nothing reported stages, so older readers and bare tests see
        # exactly the record they always did.
        stages: dict[str, float] = {}
        if frames and totals != self._stage_prev:
            for name, total, prev in zip(
                ("queue_ms", "blobs_ms", "loop_ms"), totals, self._stage_prev, strict=True
            ):
                stages[name] = round((total - prev) / frames * 1e3, 2)
        self._log(
            "detect_heartbeat",
            hz=self._detect_hz,
            frame_age_s=round(now - frame_mono, 3),
            frame_t=round(frame_t, 3),
            **stages,
        )
        self._heartbeat_prev = (now, self._detect_frames)
        self._stage_prev = totals

    # ---------- commands (each also broadcasts, per the "on every event" rule) ----------

    @hub_input
    async def start(self) -> None:
        now = self._now()
        fresh = self.clock.state is not State.RUNNING  # resuming doesn't restart the pace
        self.clock.start(now)
        self._idle_fire = None
        # Abandon before stamping: the abandoned provisional hands its pace
        # measurement back as an open stamp, which a fresh countdown replaces.
        self._abandon_provisional()
        if fresh:
            self._pace_since_mono = now
        self._log("cmd_start")
        await self.broadcast(self.clock.snapshot())

    @hub_input
    async def pause(self) -> None:
        self.clock.pause()
        self._idle_fire = None
        self._abandon_provisional()
        self._pace_since_mono = None
        self._log("cmd_pause")
        await self.broadcast(self.clock.snapshot())

    @hub_input
    async def reset(self) -> None:
        self.clock.reset()
        self._idle_fire = None
        self._abandon_provisional()
        self._pace_since_mono = None
        self._log("cmd_reset")
        await self.broadcast(self.clock.snapshot())

    @hub_input
    async def new_rack(self) -> None:
        now = self._now()
        self.clock.new_rack(now)
        self._idle_fire = None
        self._abandon_provisional()
        self._end_ball_in_hand("new_rack")
        # In run_out a New Rack is a fresh fifteen thrown up — the panel's
        # pace numbers describe one run, so they restart with it.
        self._reset_pace()
        self._log("cmd_new_rack")
        await self.broadcast(self.clock.snapshot())

    @hub_input
    async def restart(self) -> None:
        now = self._now()
        self.clock.restart(now)
        self._idle_fire = None
        self._abandon_provisional()
        self._pace_since_mono = now
        self._log("cmd_restart")
        await self.broadcast(self.clock.snapshot())

    @hub_input
    async def push(self) -> None:
        now = self._now()
        self.clock.push(now)
        self._idle_fire = None
        self._abandon_provisional()
        if self.clock.state is State.RUNNING:
            # During MOTION a push only re-arms the window; the settle that
            # follows starts (and stamps) the fresh countdown itself.
            self._pace_since_mono = now
        self._log("cmd_push")
        await self.broadcast(self.clock.snapshot())

    @hub_input
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

    @hub_input
    async def voice_extension(self) -> bool | None:
        """A spoken "extension": the deck key's call, unless voice is off.

        None = ignored (voice off), journaled as `voice_ignored` and never
        reaching the clock. The gate is here rather than in the listener
        thread so it is a logged input, and a session replay decides it the
        same way the live session did (#76).
        """
        if not self.voice_enabled:
            self._log("voice_ignored", call="extension", reason="voice_off")
            return None
        return await self.extension()

    @hub_input
    async def set_setting(self, name: str, value: str) -> bool:
        """Change one of the clock's settings (#74); False if unknown.

        It takes effect on the next countdown (`ShotClock.change_settings`),
        and is remembered for the next session. Turning the clock on or off
        parks it, so whatever a provisional shot was waiting on is dropped
        with it, as a mode switch does.
        """
        try:
            settings = self.clock.settings.with_(name, value)
        except (KeyError, ValueError):
            return False
        if settings.timed != self.clock.settings.timed:
            self._idle_fire = None
            self._abandon_provisional()
        self.clock.change_settings(settings)
        self._save_settings()
        self._log("cmd_setting", setting=name, value=value)
        await self.broadcast(self.clock.snapshot())
        return True

    @hub_input
    async def set_ruleset(self, name: str) -> bool:
        """A ruleset switch from a session logged before checkpoint 145.

        Rulesets are gone (#74): nothing live calls this, and no URL reaches
        it. It stays so `cueclock session replay` can apply the `set_ruleset`
        inputs old logs recorded, with the clock and the journal line they
        produced live. It keeps the game that is set and is not remembered.
        """
        try:
            settings = legacy_ruleset(name, game=self.clock.settings.game)
        except KeyError:
            return False
        self.clock.apply_legacy_ruleset(settings)
        self._idle_fire = None
        self._abandon_provisional()
        self._reset_pace()  # the clock zeroed its rack scope; the panel follows
        self._log("cmd_ruleset", ruleset=legacy_name(name))
        await self.broadcast(self.clock.snapshot())
        return True

    def _save_settings(self) -> None:
        """Write the settings where the next session reads them. Atomic, as
        the score's file is; a failed write is journaled, never raised."""
        if self.settings_path is None:
            return
        try:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.settings_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.clock.settings.as_dict(), indent=2) + "\n")
            tmp.replace(self.settings_path)
        except OSError as exc:
            self._log("settings_unsaved", error=str(exc))

    @hub_input
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
        self._abandon_provisional()
        # A mode switch is a new activity: the whole solo panel rescopes.
        self._reset_pace()
        self._drill_reps = 0
        self._mode_entered_mono = self._now()
        self._log("cmd_mode", mode=self.clock.mode.name)
        await self.broadcast(self.clock.snapshot())
        return True

    @hub_input
    async def set_vision(self, enabled: bool) -> None:
        self.vision_enabled = enabled
        self._log("cmd_vision", enabled=enabled)
        await self.broadcast(self.clock.snapshot())

    @hub_input
    async def set_voice(self, enabled: bool) -> None:
        """Turn spoken control on or off (#76); the deck and the page are unaffected."""
        self.voice_enabled = enabled
        self._log("cmd_voice", enabled=enabled)
        await self.broadcast(self.clock.snapshot())

    @hub_input
    async def set_ball_strip(self, shown: bool) -> None:
        """Show or hide the bar's ball strip (#54's test graphic)."""
        self.ball_strip_shown = shown
        self._log("cmd_ball_strip", shown=shown)
        await self.broadcast(self.clock.snapshot())

    async def score_changed(self) -> None:
        """Broadcast after any MatchScore mutation (score rides every payload)."""
        await self.broadcast(self.clock.snapshot())

    # The score's own mutations come through here rather than straight onto
    # `self.score` from the API, so they are hub inputs like every other key.

    @hub_input
    async def score_reset(self) -> None:
        self.score.reset_scores()
        await self.score_changed()

    @hub_input
    async def score_configure(self, body: dict[str, object]) -> None:
        self.score.configure(body)
        await self.score_changed()

    @hub_input
    async def score_adjust(self, player: str, delta: int) -> None:
        self.score.adjust(player, delta)  # floors at 0
        await self.score_changed()

    # ---------- the bracket (#43) ----------
    #
    # Policy lives here, structure lives in `bracket.py`. The division is the
    # one the rest of this file already uses: the pure module decides what a
    # double-elimination draw *is*, and the hub decides what happens to the
    # clock and the scoreboard when a result lands.

    def require_bracket(self) -> Bracket:
        if self.bracket is None:
            raise BracketError("no bracket loaded")
        return self.bracket

    @hub_input
    async def create_bracket(self, body: Mapping[str, object]) -> Bracket:
        """Build a draw from a field of players. Replaces any existing one."""
        raw = body.get("players")
        if not isinstance(raw, list):
            raise BracketError("players must be a list")
        players = []
        for entry in raw:
            if not isinstance(entry, dict):
                raise BracketError("each player must be an object")
            name = str(entry.get("name") or "").strip()
            if not name:
                raise BracketError("every player needs a name")
            rating = entry.get("rating")
            players.append(
                Player(
                    name=name,
                    rating=int(rating) if isinstance(rating, int | float) else None,
                    established=bool(entry.get("established", True)),
                )
            )
        self.bracket = Bracket.create(
            str(body.get("name") or "Tournament"),
            players,
            a_race=_as_int(body.get("a_race"), bracket_mod.DEFAULT_A_RACE),
            b_race=_as_int(body.get("b_race"), bracket_mod.DEFAULT_B_RACE),
            handicap=str(body.get("handicap") or "medium"),
            seeding=str(body.get("seeding") or "adjacent"),
            path=self.bracket_path,
        )
        self._log(
            "bracket_created",
            name=self.bracket.name,
            field=len(players),
            size=self.bracket.size,
            handicap=self.bracket.handicap,
            seeding=self.bracket.seeding,
            # The races belong here for the same reason every other setting
            # does: the first bug this feature had was a build going out with
            # races nobody chose, and without these the journal could not say
            # so — it took reading the page's JavaScript.
            a_race=self.bracket.a_race,
            b_race=self.bracket.b_race,
        )
        # Building the draw puts the first match up. Without this the bracket
        # exists with nothing loaded, which is a state the deck can only show as
        # "—" and the phone has no obvious way out of — and it is not a state
        # anybody wants anyway: you build the draw because you are about to play
        # the first match.
        opener = self.bracket.next_ready()
        if opener is not None:
            self.bracket.load(opener.id)
            self._apply_match_to_score(self.bracket, opener)
        await self.broadcast(self.clock.snapshot())
        return self.bracket

    @hub_input
    async def clear_bracket(self) -> Path | None:
        """Back to casual: the pages behave exactly as they did before #43.

        The file goes with it — a cleared bracket that reappears on the next
        server restart is a bracket nobody can get rid of at the table.

        **Anything played is archived on the way out**, and the path comes back
        so the phone can say where it went. Clearing is how you get back to
        casual scoring, not how you throw an evening away; leaving that to a
        separate Save & archive button meant the destructive path was the one
        nobody had to remember anything to take.
        """
        if self.bracket is None:
            return None
        archived: Path | None = None
        if self.archive_dir is not None and self.bracket.has_results:
            archived = self.bracket.archive(self.archive_dir)
        self._log(
            "bracket_cleared",
            name=self.bracket.name,
            archived=str(archived) if archived is not None else None,
        )
        self.bracket = None
        if self.bracket_path is not None:
            self.bracket_path.unlink(missing_ok=True)
        # The per-player targets were the bracket's, so they go with it —
        # otherwise the wall keeps showing a handicap nothing can now explain,
        # and the Setup tab's race stepper cannot reach it. Names and scores
        # stay: they are whoever is at the table right now.
        self.score.configure({"p1_target": None, "p2_target": None})
        await self.broadcast(self.clock.snapshot())
        return archived

    @hub_input
    async def clear_match_day(self) -> Path | None:
        """The end-of-day reset: the deck's Session hold calls this on the way
        down (2026-08-13), so the next session comes up clean instead of
        wearing yesterday's match.

        A loaded bracket is archived-and-cleared first (same path as the
        phone's Clear bracket button — an unfinished tournament reopens from
        "Load past bracket"), then the whole `MatchScore` goes back to
        defaults: PLAYER 1 / PLAYER 2, 0-0, no race, no ratings.
        """
        archived = await self.clear_bracket()
        self.score.clear()
        self._log("cmd_match_clear", archived=str(archived) if archived is not None else None)
        await self.broadcast(self.clock.snapshot())
        return archived

    def archive_bracket(self) -> Path:
        """Write the bracket beside the journals (`archive_dir`)."""
        bracket = self.require_bracket()
        if self.archive_dir is None:
            raise BracketError("this server has no archive directory")
        path = bracket.archive(self.archive_dir)
        self._log("bracket_archived", name=bracket.name, path=str(path))
        return path

    @hub_input
    async def reopen_archive(self, name: str) -> Bracket:
        """Make an archived bracket the live one again.

        The name comes from the archive listing and is re-derived here rather
        than trusted: it is a filename arriving over HTTP, and the directory it
        may address is the one holding the session journals.
        """
        if self.archive_dir is None:
            raise BracketError("this server has no archive directory")
        path = self.archive_dir / Path(name).name
        if path.suffix != ".json" or not path.is_file():
            raise BracketError(f"no such archived bracket: {name}")
        restored = bracket_mod.load(path)
        if restored is None:
            raise BracketError(f"{path.name} is not a readable bracket")
        restored.path = self.bracket_path  # from here on it saves as the live one
        restored.save()
        self.bracket = restored
        self._log("bracket_reopened", name=restored.name, path=str(path))
        await self.broadcast(self.clock.snapshot())
        return restored

    @hub_input
    async def load_match(self, match_id: int) -> Match:
        """Put a match's two players and their own race targets on the wall."""
        bracket = self.require_bracket()
        self._park_loaded(bracket)
        match = bracket.load(match_id)
        self._apply_match_to_score(bracket, match)
        self._log("bracket_match_loaded", match=match.id, targets=match.targets)
        await self.score_changed()
        return match

    def _park_loaded(self, bracket: Bracket) -> None:
        """Put the live score back with its own match before loading another.

        `MatchScore` is one slot, so switching away used to *be* the act of
        destroying what was in it — and the Switch match list offers the live
        match as a row, on a page passed round the room. Parking makes the
        switch reversible instead: interrupt a match at 3-2, play somebody
        else's, come back to 3-2.
        """
        if bracket.loaded is not None:
            bracket.park(bracket.loaded, self.live_scores())

    def _apply_match_to_score(self, bracket: Bracket, match: Match) -> None:
        """The bracket's one job against `MatchScore`: two players, two targets.

        `race_to` is cleared because a handicapped match has no single race —
        the two targets are the race, and the bar reads them in preference. The
        rating only rides across when it is **established**: an estimate typed
        in for an unrated relative is a fine seed and a fine handicap input, but
        putting it on the wall under a FARGO label would be a lie.

        The score comes from the match rather than being zeroed, which is the
        same statement in the other direction: a fresh match carries 0-0 anyway,
        and one being resumed or corrected carries what it had.
        """
        updates: dict[str, object] = {"race_to": None}
        for slot, prefix in ((0, "p1"), (1, "p2")):
            index = match.slots[slot]
            player = bracket.players[index] if isinstance(index, int) and index >= 0 else None
            updates[f"{prefix}_name"] = player.name if player else "—"
            updates[f"{prefix}_fargo"] = (
                player.rating if player is not None and player.established else None
            )
            updates[f"{prefix}_target"] = match.targets[slot]
        self.score.configure(updates)
        self.score.set_scores(match.scores[0], match.scores[1])

    @hub_input
    async def report_result(
        self, match_id: int, scores: tuple[int, int] | None = None
    ) -> dict[str, object]:
        """Confirm a result — and, for the loaded match, start the next one.

        This is the Stream Deck's confirm press and the phone's Report button,
        and it is deliberately one action with several effects: the result is
        written and both players routed, the next ready match is loaded at 0-0
        with its own targets, and the rack is reset (a new match is a new rack:
        clock parks, extensions refill, shot count zeroes). The scene flip to
        the bracket is the deck's half — obs-websocket lives out there, not in
        the server (`docs/hardware.md` §C3).

        Reporting a match that is *not* loaded is a correction, not a new match:
        it touches the bracket and nothing else, so fixing a score from the
        phone can never park a clock somebody is playing under.
        """
        bracket = self.require_bracket()
        was_loaded = bracket.loaded == match_id
        if scores is None and was_loaded:
            scores = self.live_scores()
        match = bracket.report(match_id, scores)
        winner = bracket.players[match.slots[match.winner or 0]]  # type: ignore[index]
        self._log(
            "bracket_result",
            match=match.id,
            scores=match.scores,
            winner=winner.name,
            routes=bracket.routes(match),
            champion=bracket.champion(),
        )
        loaded_next: Match | None = None
        if was_loaded:
            loaded_next = bracket.next_ready(after=match.id)
            if loaded_next is not None:
                bracket.load(loaded_next.id)
                self._apply_match_to_score(bracket, loaded_next)
            else:
                bracket.unload()
            await self.new_rack()  # a new match is a new rack
        await self.broadcast(self.clock.snapshot())
        return {
            "match": match.id,
            "winner": winner.name,
            "loaded": loaded_next.id if loaded_next else None,
            "champion": bracket.champion(),
        }

    @hub_input
    async def withdraw_player(self, index: int, out: bool) -> Player:
        """Somebody has gone home, or come back. Their matches follow.

        Every unplayed match of theirs is conceded — down both sides of the draw
        — without anybody inventing a score for a match that was not played.
        If the concession takes the match currently on the wall, the wall moves
        on to the next one rather than sitting on a match nobody will play.
        """
        bracket = self.require_bracket()
        self._park_loaded(bracket)
        player = bracket.withdraw(index, out=out)
        self._log("bracket_withdrawal", player=player.name, index=index, withdrawn=out)
        await self._follow_the_draw(bracket)
        return player

    @hub_input
    async def concede_match(self, match_id: int, body: Mapping[str, object] | None) -> Match:
        """One match given up by one player, the rest of their draw kept.

        The same two steps a withdrawal takes, because it is the same act at the
        scale of a single match: park whatever is on the wall, then follow the
        draw to a match somebody can actually play. No ``slot`` undoes it.
        """
        bracket = self.require_bracket()
        slot = _as_opt_int((body or {}).get("slot"))
        self._park_loaded(bracket)
        match = bracket.concede(match_id, slot)
        who: str | None = None
        if slot is not None:
            index = match.slots[slot]
            if isinstance(index, int) and index >= 0:
                who = bracket.players[index].name
        self._log("bracket_conceded", match=match.id, slot=slot, player=who)
        await self._follow_the_draw(bracket)
        return match

    @hub_input
    async def set_bracket_races(self, body: Mapping[str, object]) -> Bracket:
        """Change the base races mid-tournament — unplayed matches only."""
        bracket = self.require_bracket()
        bracket.set_races(_as_opt_int(body.get("a_race")), _as_opt_int(body.get("b_race")))
        self._log("bracket_races_changed", a_race=bracket.a_race, b_race=bracket.b_race)
        if bracket.loaded is not None:
            live = bracket.match(bracket.loaded)
            self.score.configure({"p1_target": live.targets[0], "p2_target": live.targets[1]})
        await self.score_changed()
        return bracket

    async def _follow_the_draw(self, bracket: Bracket) -> None:
        """Keep the wall on a match somebody can actually play.

        A withdrawal can settle the loaded match from off the table, which is
        the one way the score bar can end up naming a match that is already
        over. Moving on is the same act a confirmed result performs, rack reset
        and all — a new match is a new rack.
        """
        loaded = bracket.loaded
        if loaded is not None and not bracket.match(loaded).played:
            await self.score_changed()
            return
        nxt = bracket.next_ready()
        if nxt is not None:
            bracket.load(nxt.id)
            self._apply_match_to_score(bracket, nxt)
        else:
            bracket.unload()
        await self.new_rack()
        await self.score_changed()

    @hub_input
    async def clear_result(self, match_id: int) -> Match:
        """Un-play a match so the one feeding it can be corrected."""
        bracket = self.require_bracket()
        match = bracket.clear_result(match_id)
        self._log("bracket_result_cleared", match=match.id, scores=match.scores)
        await self.broadcast(self.clock.snapshot())
        return match

    @hub_input
    async def edit_match(self, match_id: int, body: Mapping[str, object]) -> Match:
        """Redraw a Round-1 matchup, or override its race targets."""
        bracket = self.require_bracket()
        match = bracket.match(match_id)
        if "p1" in body or "p2" in body:
            match = bracket.edit(
                match_id,
                _as_opt_int(body.get("p1")),
                _as_opt_int(body.get("p2")),
            )
        if "p1_target" in body or "p2_target" in body:
            match = bracket.set_targets(
                match_id,
                _as_opt_int(body.get("p1_target")),
                _as_opt_int(body.get("p2_target")),
            )
            if bracket.loaded == match_id:
                self.score.configure({"p1_target": match.targets[0], "p2_target": match.targets[1]})
        self._log("bracket_match_edited", match=match.id, targets=match.targets)
        await self.broadcast(self.clock.snapshot())
        return match

    # ---------- vision events (dispatched from the `cueclock run` camera thread) ----------

    # Journal timestamp contract (2026-08-01): `frame_t` is the *confirming
    # frame's capture timestamp* — camera clock, seconds since the session's
    # first frame, the exact `t` the vision core was fed — named unambiguously
    # against the record's own `mono` stamp, which is emit time. `t` carries
    # the same value and is kept for old readers. Capture→emit lag per event =
    # this record's `mono` minus the camera-to-mono anchor the nearest
    # `detect_heartbeat` line provides (its `mono` minus `frame_age_s` ≈ the mono
    # instant of its `frame_t`).

    #
    # Each of the pipeline's calls goes through `_vision`: applied at once, or
    # queued behind a settle that is waiting for its list (#69). The halves
    # that do the work are synchronous and take the hub time the call arrived
    # at; they return whether the clock may have changed (a broadcast is due).

    def _vision(self, call: Callable[[float], bool]) -> bool:
        """Apply one pipeline call, or queue it behind a held settle.

        A settle held for its ball list is decided by that list, not by
        whatever the pipeline says next (#69). During placing the hand is
        still moving when the settle fires: in session 20261121-153046 the
        motion latch rose 32 ms after a held settle and decided it as a shot,
        47 ms before the list that would have read it as placing; ball in
        hand ended there, and the next adjustment restarted the countdown
        again. So the calls that arrive while a settle is held wait behind
        it, for at most what is left of SETTLE_LIST_WAIT_S, and are then
        applied in the order they came, each as of its own arrival. The cost
        is that a freeze right behind a held settle can show up to 0.3 s late
        on the wall; no clock time is lost.

        The ball list is what the settle waits for and never queues. Neither
        do a scratch candidate or a refutation, which are evidence about the
        cue in the held settle's own shot and decide it as before, nor an
        operator command (`hub_input`): a press is authoritative.
        """
        now = self._now()
        held = self._held_settle
        if held is not None and now - held.now >= SETTLE_LIST_WAIT_S:
            self._flush_held("timeout")  # the tick loop is up to 0.1 s behind
        if self._held_settle is not None:
            self._queued.append((now, call))
            return False
        return call(now)

    def _drain_queued(self) -> bool:
        """Apply what queued behind a held settle, now that it is decided:
        in arrival order, each as of its own arrival. Stops if one of them is
        a settle that is held in turn; the rest then wait behind that one.
        True when anything was applied."""
        applied = False
        while self._queued and self._held_settle is None:
            now, call = self._queued.popleft()
            call(now)
            applied = True
        return applied

    def _flush_held(self, why: str) -> bool:
        """Decide a held settle as today (``why``) and apply everything
        queued behind it; a queued settle that is held in turn goes the same
        way. True when a settle was waiting. The caller broadcasts."""
        flushed = False
        while self._held_settle is not None:
            self._decide_held_settle(why)
            self._drain_queued()
            flushed = True
        return flushed

    async def shot_taken(
        self,
        t: float | None = None,
        ball_id: int | None = None,
        speed_mms: float | None = None,
        newborn: bool = False,
    ) -> None:
        if self._vision(lambda now: self._shot_taken(now, t, ball_id, speed_mms, newborn)):
            await self.broadcast(self.clock.snapshot())

    def _shot_taken(
        self,
        now: float,
        t: float | None,
        ball_id: int | None,
        speed_mms: float | None,
        newborn: bool,
    ) -> bool:
        detail: dict[str, object] = {"t": t, "frame_t": t, "ball": ball_id, "newborn": newborn}
        if speed_mms is not None:
            detail["speed"] = round(speed_mms)
        self._last_shot_mono = now  # the idle stop's anchor — before every gate
        if not self._episode_open:
            self._begin_episode(t)  # a strike the latch never saw is still a shot (#63)
        self._episode_struck = True
        if not self.vision_enabled:
            self._log("vision_shot_taken_killed", **detail)
            return False  # kill switch: misbehaving vision must not move the clock
        if not self.clock_live:
            if not self.clock.mode.clock_live:
                self._drill_reps += 1  # the solo panel's REPS — approximate by design
            self._log("vision_shot_taken_no_clock", **detail)
            return False  # no_clock mode / freestyle: journaled as evidence, never clocked
        floor = self.clock.mode.break_floor_mms
        if self.clock.state is State.IDLE:
            # E8 evidence, stamped before the floor gate: whatever the gate
            # decides, a fire happened out of IDLE, and the settle that
            # follows may adjudicate it as the break (`balls_settled`).
            # In-flight tracks under-read speed 2-4x, so the fires the floor
            # eats are mostly real breaks — 15 of the 84 eaten fires on
            # record needed a manual rescue press.
            self._idle_fire = (now, speed_mms)
        if (
            self.clock.state is State.IDLE
            and floor is not None
            and speed_mms is not None
            and speed_mms < floor
        ):
            # Racking / placement motion, not a break — see the gate note
            # above. Speed only: the newborn half was removed at checkpoint 57.
            self._log("vision_shot_taken_ignored_idle", **detail)
            return False
        state_before = self.clock.state
        if self._pace_since_mono is not None and state_before in (State.RUNNING, State.GRACE):
            # Solo pace (#30): this shot ends the countdown the stamp opened.
            # GRACE counts — the shot took longer than base, which is exactly
            # what the number is for. Consumed here so a mid-motion re-fire
            # cannot measure the same countdown twice. A RUNNING fire is
            # provisional (#33 Part B), so its measurement rides the verdict:
            # committed by the settle that counts it, restored on a revert.
            took = self._last_shot_mono - self._pace_since_mono
            if state_before is State.GRACE:
                self._pace_last_s = took
                self._pace_sum_s += took
                self._pace_shots += 1
            else:
                self._pace_pending = (self._pace_since_mono, took)
            self._pace_since_mono = None
        self.clock.shot_taken()
        if state_before is State.IDLE and self.clock.state is State.MOTION:
            self._break_fired = True  # the break, until its settle says otherwise (§17)
        if state_before is State.RUNNING and self.clock.state is State.MOTION:
            # #33 Part B: the fire opened (or joined) the provisional
            # episode — its count is the settle's decision now.
            if self._provisional_open_mono is None:
                self._provisional_open_mono = now
            detail["provisional"] = True
        self._log("vision_shot_taken", **detail)
        return True

    async def balls_settled(
        self,
        t: float | None = None,
        rearrange: "RearrangeSummary | None" = None,
        backstop: bool = False,
        movers: "list[SettleMover] | None" = None,
        tracker_cue: bool = False,
        cue_roll_s: float | None = None,
    ) -> None:
        """``cue_roll_s``: how long the cue ball was seen rolling in the
        motion this settle ends (#67, `vision.roll`); None with no motion to
        measure, which counts as rolling."""
        if self._vision(
            lambda now: self._balls_settled(
                now, t, rearrange, backstop, movers, tracker_cue, cue_roll_s
            )
        ):
            await self.broadcast(self.clock.snapshot())

    def _balls_settled(
        self,
        now: float,
        t: float | None,
        rearrange: "RearrangeSummary | None",
        backstop: bool,
        movers: "list[SettleMover] | None",
        tracker_cue: bool,
        cue_roll_s: float | None = None,
    ) -> bool:
        detail: dict[str, object] = {"t": t, "frame_t": t}
        # #63: what this settle's ball list will be judged against, kept until
        # the list arrives (20-75 ms later live). Before every gate, so a
        # killed or untimed settle still journals what the list said.
        self._note_settle_unlisted()
        # §23: was there a motion episode (a latch or a strike) behind this
        # settle? On record only the pipeline's backstop settles without one
        # (2 of 149 list rows), and then the last motion on record is the shot
        # before: its list is compared with the newest one before the settle.
        episode, self._episode_open = self._episode_open, False
        facts = SettleFacts(
            t=t,
            epoch=self._episode_epoch,
            counted=False,
            travel_mm=max(m.travel_mm for m in movers) if movers else None,
            tracker_cue=tracker_cue,
            motion_t=self._episode_motion_t if episode else t,
            cue_roll_s=cue_roll_s,
        )
        self._settle_facts = facts
        self._settle_listed = False
        self._settle_was_held = False
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
            detail["spread_mm"] = round(rearrange.spread_mm)
            # #37: what the diff was taken against. Without it a journaled
            # `involved=0` is unreadable after the fact — the 2026-08-11
            # `flag_phantom` record had to be settled by replaying the dump.
            detail["baseline"] = rearrange.baseline_balls
        if movers:
            detail["movers_alive"] = sum(m.alive for m in movers)
            detail["mover_max_mm"] = round(max(m.travel_mm for m in movers))
        # #67: on every record, null with no motion to measure, so the next
        # question about it needs no replay.
        detail["cue_roll_s"] = None if cue_roll_s is None else round(cue_roll_s, 3)
        if not self.vision_enabled:
            self._log("vision_balls_settled_killed", **detail)
            return False  # kill switch, as above
        if not self.clock_live:
            self._log("vision_balls_settled_no_clock", **detail)
            return False  # journaled, not clocked — as above
        # #64: during ball in hand a settle waits for its list before it
        # touches the clock — at most SETTLE_LIST_WAIT_S — because only the
        # list can say whether it moved nothing but the cue. The clock stays
        # as it is meanwhile: frozen through a provisional, running otherwise.
        # Waiting beats counting and undoing: a fresh countdown would flash on
        # the wall and the shot count, pace and rack would have to un-happen.
        # Only settles that could be placing wait: an arm-free list since the
        # scratch to compare with, and travel under the hand bound. IDLE
        # settles are the clock's no-op and E8's business, never held.
        # §16: so does a settle on a freeze in which the tracker saw no ball
        # displaced, or one ball and no strike — its list says whether
        # anything moved at all. §17: and
        # the settle of a strike that left IDLE as the break, when a rack
        # stood before it — its list says whether the rack still does. §21:
        # and a settle whose cue ball was never seen rolling — its list says
        # whether an object ball went, or a hand moved only the cue.
        if (
            self.clock.state is not State.IDLE
            and not self._settle_is_phantom(rearrange, backstop)
            and (
                self._may_be_placing(facts)
                or self._may_be_no_shot(facts, rearrange, backstop)
                or self._may_be_false_start(facts)
                or self._may_be_by_hand(facts, backstop)
            )
        ):
            self._held_settle = _HeldSettle(now, t, detail, rearrange, backstop)
            self._settle_was_held = True
            return False
        # §23: the backstop is the break's evidence. With a clock that is not
        # IDLE and no motion episode behind it, it is balls hidden or shown
        # (a stance, an arm, racking), never a shot: all four on a live clock
        # in 112 journals restarted a countdown nobody had shot on. With an
        # episode open it counts as before, the failing-open side.
        unseen = backstop and not episode and self.clock.state is not State.IDLE
        self._decide_settle(now, t, detail, rearrange, backstop, "backstop" if unseen else None)
        return True

    def _decide_settle(
        self,
        now: float,
        t: float | None,
        detail: dict[str, object],
        rearrange: "RearrangeSummary | None",
        backstop: bool = False,
        refusal: str | None = None,
    ) -> bool:
        """The clock half of a settle, as of ``now`` (the settle's own hub
        time, also when it waited for its list). ``refusal`` is its list's
        verdict that it was not a shot: ``ball_in_hand`` (#64: it moved only
        the cue, during ball in hand), ``rack_standing`` (§17), ``nothing_moved``
        (§16, §19: the cue ball was read on its spot) or ``by_hand`` (§21: the
        cue ball moved by hand in open play). ``backstop`` (§23) is not a
        list's verdict: the pipeline's backstop settle on a live clock, with
        no motion behind it. True when the settle counted a shot."""
        # Both settle-time refusals are decided here, before anything touches
        # the clock, and against the state the settle ARRIVED in. That is what
        # keeps them disjoint from the break adjudication below: E8 acts only
        # out of IDLE, these only out of MOTION/RUNNING. Deciding after it
        # would let a break the hub had just counted be refused by a predicate
        # reading the post-break state.
        #
        if self._break_fired:
            self._break_fired = False
            if refusal is not None and self.clock.break_reverted(now):
                # §17: the strike that started this rack was not the break —
                # the rack is standing where it stood. Back to the armed
                # rack: the count is undone, and the real break counts when
                # it comes.
                detail["rejected"] = refusal
                self._log("shot_provisional_reverted", t=t, frame_t=t, reason=refusal)
                self._log("vision_balls_settled", **detail)
                return False
        # #37 — was this episode a phantom? The table is provably unchanged.
        phantom = self._settle_is_phantom(rearrange, backstop)
        if self._adjudicate_break(now, rearrange):
            # E8: an armed rack's settle with break-scale rearrangement IS
            # the break (WNT §5 starts the 60 s window at the rest, so
            # nothing was ever lost by deciding here). Count the shot, then
            # let the settle below start the first-shot window.
            self.clock.shot_taken()
        reverting = self._provisional_open_mono is not None
        moved_clock = self.clock.balls_settled(now, suppress=phantom or refusal is not None)
        if self._settle_facts is not None and self._settle_facts.t == t:
            self._settle_facts = dataclasses.replace(self._settle_facts, counted=moved_clock)
        if phantom:
            # The provisional freeze is undone: no shot counted, the countdown
            # resumes with exactly the remaining it froze at, and the pace
            # measurement it was holding goes back to being an open stamp.
            # Same resolution `episode_discarded` performs, from the other end
            # of the episode — so it journals the same kind, and `report`
            # tallies it as a revert without knowing this branch exists.
            self._revert_pace()
            self._provisional_open_mono = None
            detail["rejected"] = "phantom"
            self._log(
                "shot_provisional_reverted",
                t=t,
                frame_t=t,
                reason="phantom_settle",
                baseline=rearrange.baseline_balls if rearrange is not None else None,
                movers_alive=detail.get("movers_alive"),
                mover_max_mm=detail.get("mover_max_mm"),
            )
        elif refusal is not None and not moved_clock:
            # The list's refusal: placing the cue during ball in hand (#64),
            # or a freeze on a table where nothing moved (§16). Or §23's: a
            # backstop settle on a live clock with no motion behind it. Same
            # resolution as a phantom: nothing counted, a frozen countdown
            # resumes at the remaining it froze with, a running one (and a
            # scratch hold in progress) is left alone. Two kinds, as #41
            # journaled them, so `report` tells a reverted freeze from a
            # refused compensation.
            reason = refusal
            self._revert_pace()
            self._provisional_open_mono = None
            detail["rejected"] = reason
            self._log(
                "shot_provisional_reverted" if reverting else "compensation_suppressed",
                t=t,
                frame_t=t,
                reason=reason,
                mover_max_mm=detail.get("mover_max_mm"),
            )
        elif refusal is not None:
            # The clock refused: a GRACE or IDLE fire already committed a shot
            # this cycle, and committed counts do not un-happen. Left counted.
            detail["hand_refused"] = refusal
        if moved_clock:
            self._end_ball_in_hand("shot")
            # The settle counted this episode's one shot (fired or
            # compensated): commit the pace measurement the provisional was
            # holding, if any, and close the episode.
            self._commit_pace()
            self._provisional_open_mono = None
            if self.clock.state is State.RUNNING:
                # Solo pace (#30): the fresh countdown this settle started is
                # the next shot's timer. A settle the clock ignored (IDLE)
                # stamps nothing — racking motion must not time a phantom shot.
                self._pace_since_mono = now
        self._log("vision_balls_settled", **detail)
        # #25: the scratch's candidate can precede its settle (shot_debug:
        # candidate 9.54 s, settle 11.92 s) — the fresh countdown this settle
        # just started is the one the retrieval needs held. Unconditional
        # (#39): the hold belongs to the *cue ball*, not to the shot, so no
        # verdict about what this settle was may swallow it.
        self._maybe_hold_for_scratch()
        return moved_clock

    def _decide_held_settle(self, why: str) -> bool:
        """Decide a settle that is waiting for its list as today, because
        something that does not wait reached the hub first (``why``: a
        command, a scratch candidate or refutation) or the wait ran out
        (``timeout``). True when one was waiting. Callers go through
        `_flush_held`, which also applies what queued behind it."""
        held, self._held_settle = self._held_settle, None
        if held is None:
            return False
        held.detail["held"] = why
        self._decide_settle(held.now, held.t, held.detail, held.rearrange, held.backstop)
        return True

    def _decide_overdue_settle(self, now: float) -> None:
        """The tick loop's half of the wait: a settle whose list has not come
        within SETTLE_LIST_WAIT_S is decided as today."""
        held = self._held_settle
        if held is not None and now - held.now >= SETTLE_LIST_WAIT_S:
            self._flush_held("timeout")

    def _placing_rest(self, facts: SettleFacts) -> "BallList | None":
        """During ball in hand: the newest arm-free list since the scratch and
        before this settle's motion, the table placing is compared with."""
        bih = self._ball_in_hand
        if bih is None:
            return None
        return self._rest_lists.arm_free(since=bih.since, before=facts.motion_t)

    def _may_be_placing(self, facts: SettleFacts) -> bool:
        """Could this settle's list make it placing? Ball in hand, a list to
        compare with, and travel under the hand bound (§15) or the cue ball
        never seen rolling: a carry is placing however far it went (§21)."""
        return self._placing_rest(facts) is not None and (
            (facts.travel_mm or 0.0) < TRAVEL_MIN_MM or not facts.seen_rolling
        )

    def _may_be_by_hand(self, facts: SettleFacts, backstop: bool) -> bool:
        """Could this settle's list show the cue ball moved by hand (§21)?
        The cue ball was never seen rolling in the motion it ends, and there
        is an arm-free list from before the motion to compare with. The
        backstop settle is never held: §23 decides it at once."""
        return (
            not backstop
            and not facts.seen_rolling
            and self._rest_lists.before(facts.motion_t)[0] is not None
        )

    def _may_be_no_shot(
        self, facts: SettleFacts, rearrange: "RearrangeSummary | None", backstop: bool
    ) -> bool:
        """Could this settle's list show that nothing moved (§16)?

        The countdown is frozen on a provisional, so being wrong costs what
        #33 already accepts; the tracker saw no ball displaced (a ball it
        merely lost or found reads the same whether a pot or a sleeve did
        it), or one ball in an episode with no strike; and there is an
        arm-free list from before the motion to compare with. The pipeline's
        backstop settle is never held: §23 decides it at once.

        One ball and no strike (#69): in session 20261121-153046 at 712 s the
        tracker moved one ball 38 mm while he stood over the shot, the settle
        counted, and the list 74 ms later read every ball where it stood. A
        distance cannot separate that from a shot (real ones in the same
        journals read 42, 44 and 49 mm), so the list decides, and it refuses
        only a cue ball read on its spot (§19): none of the 101 counted
        settles it read as a shot in the four logged sessions has the cue
        within 27 px of it. A settle with a strike in it, or with two or
        more balls moved, is decided at once.
        """
        if rearrange is None:
            return False
        unmoved = rearrange.moved == 0 or (rearrange.moved == 1 and not self._episode_struck)
        return (
            self.clock.state is State.MOTION
            and self._provisional_open_mono is not None
            and not backstop
            and unmoved
            and self._rest_lists.before(facts.motion_t)[0] is not None
        )

    def _may_be_false_start(self, facts: SettleFacts) -> bool:
        """Could this settle's list show the rack still standing (§17)? The
        episode's strike left IDLE as the break and nothing has counted
        since, and the newest list before it holds a rack to compare with."""
        return (
            self._break_fired
            and self.clock.state is State.MOTION
            and self.clock.rack_shots == 1
            and holds_rack(self._rest_lists.last_before(facts.motion_t))
        )

    def _begin_ball_in_hand(self, state: _ShotScratch) -> None:
        """A shot with a scratch standing (called, not refuted or cleared)
        ended in a counted settle: ball in hand, until an object ball moves."""
        facts = self._settle_facts
        if facts is None or not facts.counted or facts.epoch != self._episode_epoch:
            return  # the scratch's shot never counted a settle: nothing to anchor on
        self._ball_in_hand = _BallInHand(since=facts.t, scratch=state)
        self._log("ball_in_hand", frame_t=facts.t, by=state.by)

    def _end_ball_in_hand(self, reason: str) -> None:
        """Ball in hand is over: an object ball moved (``shot``), the rack was
        reset (``new_rack``), or the scratch was refuted (``refuted``)."""
        bih, self._ball_in_hand = self._ball_in_hand, None
        if bih is not None:
            self._log("ball_in_hand_ended", reason=reason, placed=bih.placed)

    async def ball_list(
        self, t: float, reason: str, found: "BallList", tracker: int | None = None
    ) -> None:
        """A ball list (#54) was written to the session log: ``reason`` is
        ``settle`` or ``rest``, ``t`` its frame's capture time.

        Live it arrives 20-75 ms after its frame, dispatched in the order the
        run loop drained it; a replay feeds it back after the same frame
        (`balls.jsonl` records which). The strip shows the latest list with no
        arm over the table, or with nothing over it but a pack of its own
        balls. A settle's own list is judged for the scratch hold
        (#63, `server/scratch.py`), and every list is kept, by frame, as the
        "before" of the next shot.
        """
        if not found.arm or found.pack:
            # A standing rack trips the hand test by itself (`BallList.pack`):
            # shown, or the strip sits on a mid-racking read until the break.
            self._ball_strip = list(found.labels)
        self._log(
            "ball_list",
            frame_t=round(t, 3),
            reason=reason,
            arm=found.arm,
            **({"pack": True} if found.pack else {}),
            **({"cues_dropped": found.cues_dropped} if found.cues_dropped else {}),
            balls=" ".join(found.labels),
            tracker=tracker,
            reassigned=sum(b.own is not None for b in found.balls),
        )
        if reason == "settle":
            await self._judge_settle_list(t, found)
        self._rest_lists.add(t, found)
        # #69: what the pipeline said while the settle waited for this list.
        if self._drain_queued():
            await self.broadcast(self.clock.snapshot())

    async def _judge_settle_list(self, t: float, found: "BallList") -> None:
        """#63: the list's say at the settle it was read at — call the
        scratch, clear it, or leave today's logic to decide alone. A settle
        held for this list is decided here: during ball in hand, one that
        moved only the cue is kept off the clock (#64); so is one that left
        the rack standing (§17) or the cue ball on its spot (§16, §19), and
        one in which a hand moved only the cue ball (§21)."""
        facts = self._settle_facts
        if facts is None or self._settle_listed:
            return  # no settle waiting, or the next motion began first (journaled)
        if facts.t is not None and abs(facts.t - t) > 1e-3:
            return  # some other settle's list
        self._settle_listed = True
        rest, cue_before = self._rest_lists.before(facts.motion_t)
        state = self._scratch if self._scratch is not None else _ShotScratch()
        self._scratch = state
        candidate = state.candidate and state.closed is None
        placing = only_cue_changed(found, self._placing_rest(facts), facts)
        hand = "ball_in_hand" if placing and self._ball_in_hand is not None else None
        # The refusals in the order they are asked; the first that holds is
        # the verdict. The two hand ones (#64, §21) journal `hand` as well.
        rack = self._break_fired and rack_stands(
            found, self._rest_lists.last_before(facts.motion_t)
        )
        still = nothing_moved(found, rest)
        by_hand = hand is None and not rack and not still and moved_by_hand(found, rest, facts)
        refusal = (
            hand
            or ("rack_standing" if rack else None)
            or ("nothing_moved" if still else None)
            or ("by_hand" if by_hand else None)
        )
        hand_kind = hand or ("by_hand" if by_hand else None)
        held = self._held_settle
        moved_clock = False
        if held is not None and held.t == facts.t:
            self._held_settle = None
            held.detail["held"] = "list"
            moved_clock = self._decide_settle(
                held.now, held.t, held.detail, held.rearrange, held.backstop, refusal
            )
            facts = dataclasses.replace(facts, counted=moved_clock)
            # "applied": kept off the clock. "refused": the clock had already
            # committed a shot this cycle, so the settle counted anyway.
            applied = None if hand_kind is None else "refused" if moved_clock else "applied"
            if applied == "applied" and hand is not None and self._ball_in_hand is not None:
                self._ball_in_hand.placed += 1
        else:
            # Not held: the clock was idle or off, or something else decided
            # the settle before its list came (``late``). Journaled as a row.
            applied = None if hand_kind is None else "late" if self._settle_was_held else "not_held"
        verdict = judge(found, rest, cue_before, facts, candidate)
        state.says = verdict.says
        released = False
        call = verdict.call
        if hand is not None:
            # Placing during ball in hand: a cue seen is a cue put back, which
            # clears nothing (the scratch hold it may run under keeps going),
            # and a cue gone is a cue in the hand, not a scratch.
            call = "none"
        elif verdict.says == "cue" and state.closed is None:
            # The cue counts as present if either the list or today's logic
            # sees it: no hold for this shot, and one this shot was granted is
            # released. A candidate arriving later for the same shot finds it
            # cleared.
            state.closed = "cleared"
            released = state.granted and self.clock.release_hold(self._now())
            if candidate:
                call = "cleared"
        elif call == "scratch" and state.closed is None:
            state.listed = True
        self._log(
            "scratch_list",
            frame_t=round(t, 3),
            **verdict.journal(),
            verdict=call,
            row=table_row(found, rest, cue_before, hand is not None, call, by_hand=by_hand),
            cue_px=None if (px := cue_px(found, rest)) is None else round(px, 1),
            hand=applied,
            counted=facts.counted,
            tracker_cue=facts.tracker_cue,
            travel_mm=None if facts.travel_mm is None else round(facts.travel_mm),
            candidate=candidate,
        )
        if released:
            self._log("scratch_hold_released", reason="cleared")
        if self._maybe_hold_for_scratch() or released or held is not None:
            await self.broadcast(self.clock.snapshot())

    def _note_settle_unlisted(self) -> None:
        """Journal the last settle's list as missing when it did not arrive
        before the next motion or settle: today's logic decided alone."""
        facts = self._settle_facts
        if facts is not None and not self._settle_listed:
            self._settle_listed = True
            self._log(
                "scratch_list",
                frame_t=None if facts.t is None else round(facts.t, 3),
                verdict="no_list",
                counted=facts.counted,
            )

    def _begin_episode(self, t: float | None) -> None:
        """A new shot's motion began (the latch rose, or a strike fired with
        no episode open): the previous shot's scratch question is retired."""
        self._note_settle_unlisted()
        self._retire_scratch()
        self._episode_epoch += 1
        self._episode_open = True
        self._episode_motion_t = t
        self._break_fired = False
        self._episode_struck = False

    def _retire_scratch(self) -> None:
        """One ``scratch_call`` line per shot that had a scratch called (#63):
        who called it, whether it was held, and if not, why."""
        state, self._scratch = self._scratch, None
        if state is None or not (state.candidate or state.listed):
            return
        if state.closed is None:
            self._begin_ball_in_hand(state)  # #64: the scratch stands
        detail: dict[str, object] = {
            "by": state.by,
            "held": state.granted,
            "closed": state.closed,
            "says": state.says,
        }
        if state.owed:
            detail["missed"] = (
                "vision_off"
                if not self.vision_enabled
                else "no_clock"
                if not self.clock_live
                else self.clock.state.value
            )
        self._log("scratch_call", **detail)

    def _settle_is_phantom(self, rearrange: "RearrangeSummary | None", backstop: bool) -> bool:
        """#37: did this settle prove that no shot was taken?

        The symmetric half of #33. That issue gave the clock a window — motion
        onset through settle — and spent it in one direction only: a missed
        strike is rescued by the freeze. An *invented* strike still stood,
        froze the clock, counted a shot and reset the countdown. The mechanism
        to undo it was already general (`ShotClock.balls_settled(suppress=)`,
        plus the provisional revert path); only this predicate was missing.

        **Positive evidence, never absence of it.** Refusing a real shot is
        worse than tolerating a phantom — the countdown then runs through the
        shooter's stroke, which is the failure #33 exists to kill, and it is
        how #38's predicate died (3 fires in one session, 2 of them wrong).
        So every clause here is a thing that was measured, and anything
        missing or degraded fails open:

        * **MOTION only.** The clock must be sitting on a provisional freeze,
          which bounds the cost of being wrong to exactly what #33 already
          accepts: the countdown resumes with the remaining it froze at. In
          GRACE the same refusal would let the buzzer fire on a live shot (the
          worst felt failure, 2 in 30 sessions); in IDLE a settle is a no-op
          and the break has its own adjudication.
        * **A real quiet baseline** of `QUIET_BASELINE_MIN_BALLS` or more —
          see that constant. This is the trap #33 fell into: `involved == 0`
          also means "there was no baseline", which is a zero by arithmetic.
        * **The table is unchanged.** `involved == 0` against that baseline:
          every ball the settle resolved greedy-matches a baseline ball within
          `moving_enter_mm`, with none left over on either side. A shot always
          rearranges something — measured over the whole corpus, every
          honestly-anchored settle of a real shot reads 1 or more, including
          the softest touches in the collection (`soft_touch_0731` 2,
          `tip_bump` 2 involved balls). A phantom crowned on a hoodie, a cue
          shaft, or a coasting track leaves the layout identical.
        * **Not the pipeline's backstop settle**, which carries no episode at
          all — and by construction cannot read zero anyway (it needs
          `SETTLE_BACKSTOP_MIN_INVOLVED`). Stated rather than implied, because
          the implication is the kind that quietly stops being true.
        **The known false refusal, unfixed on purpose — `flag_missed`
        (2026-08-20 19:17:53).** This predicate's `involved == 0` comes from a
        greedy *position* match that never consults ball identity, and that is
        where it can be fooled. Steven potted a ball with a cue the tracker had
        never held before the strike, so the cue was not in the quiet baseline;
        the potted ball's track died, and the cue rolled to rest **23.1 mm**
        from where that ball had stood. Seven baseline balls, seven settled
        balls, every pair inside the 30 mm `moving_enter_mm` calls "moved" —
        `involved 0`, a real shot revoked, the countdown run from 14.25 s into
        GRACE. The hub journaled `mover_max_mm: 489` in that same record.

        **Mover travel was tried as the veto for exactly this, on 2026-08-29,
        and the corpus killed it — do not try it again without new evidence.**
        The idea was that a mover travelling past the diff's own movement floor
        refutes "the table is unchanged", using no new constant. It fixes
        `flag_missed` and it is wrong: `parked_vote_strike_0801`'s phantom —
        graded, reproducible, and the very clip this predicate was built for —
        carries a mover that travelled **122 mm**. Measured together the
        populations interleave completely:

            real shot   489 mm (flag_missed)
            phantom     364 mm (flag_phantom, a white tank top)
            phantom     122 mm (parked_vote_strike_0801)
            phantom      26 mm (the live 2026-08-11 phantom)

        Any floor separating 489 from 122 is fitted to one clip, and the
        2026-08-11 probe had already said the same thing from the other
        direction: real shots read 0 mm of *surviving*-mover travel on 8 corpus
        clips because every mover dies in the scatter. Mover travel does not
        carry this verdict in either polarity.

        So `flag_missed` stands as an understood, currently unseparable false
        refusal. The real defect under it is that the rearrangement diff is
        identity-blind — a ball coming to rest near where a *different* ball
        stood is invisible to it — and the honest repair is an identity-aware
        match, which reaches break adjudication and the settle backstop and
        wants its own measured pass. Making this predicate *more* sensitive is
        not that repair: every candidate tried so far inflates `involved`
        exactly when a person is leaning over the table, which is when phantoms
        happen.

        One further evidence axis the issue proposed is also NOT here:

        * **Roll-out length.** `report.PHANTOM_SETTLE_S` flags a sub-1 s
          fire→settle as a phantom *candidate*, and promoting it to a verdict
          would be a false refusal: `pot_scratch_nohold_0811` is a real pot
          whose settle lands **0.73 s** after its fire. It also catches
          neither proof case (1.37 s and 1.65 s). It stays a diagnostic.
        """
        if self.clock.state is not State.MOTION:
            return False
        if backstop or rearrange is None:
            return False
        if rearrange.baseline_balls is None or rearrange.baseline_balls < QUIET_BASELINE_MIN_BALLS:
            return False
        return rearrange.involved == 0

    def _adjudicate_break(self, now: float, rearrange: "RearrangeSummary | None") -> bool:
        """E8 (§13.2): should this IDLE settle be counted as the break?

        Policy, not detection: the event layer emits whatever fires it can
        honestly confirm, and this is the one place that decides what an
        armed rack's settle means. Two tiers, both anchored above: a fire of
        *any* speed since the rack armed plus a moderate scatter, or — for
        the dark-flight class, where the whole break happens between track
        identities — the pack un-merging, with no fire at all. The
        2026-08-01 racking-countdown hazard fails both: racking settles 1-3
        balls at a time (under the fired floor), never arms a fire within
        15 s of its settle, and MERGES rather than un-merges, so every
        racking settle on record measures appeared=0.
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
                appeared=rearrange.appeared,
                involved=rearrange.involved,
                moved_mm=round(rearrange.moved_mm),
                fire_speed=round(speed) if speed is not None else None,
            )
            self._idle_fire = None
            return True
        if not fired and rearrange.appeared >= BREAK_DARK_MIN_APPEARED:
            # The dark-flight class: the whole break happened between track
            # identities, so there is no fire to arm the tier above. The pack
            # un-merging is the evidence — see the appeared anchoring above.
            self._log(
                "break_adjudicated",
                tier="dark",
                appeared=rearrange.appeared,
                involved=rearrange.involved,
                moved_mm=round(rearrange.moved_mm),
                spread_mm=round(rearrange.spread_mm),
            )
            return True
        return False

    async def episode_started(self, t: float | None = None) -> None:
        """#33 Part B: the engine's motion-episode latch set (quiet→moving).

        The provisional shot: a RUNNING countdown freezes ~0.25 s after the
        stroke — faster than any strike confirm (~0.5 s) — and the settle
        (or a ShotTaken merging in) decides what the episode was. The
        trigger is the hardened roster-mover latch exported by the pipeline,
        deliberately not the raw E9 motion bit. Gated exactly like the
        events that move the clock. Freeze latency is settled — do not
        optimize it (#33): the 0.2 s displacement window dominates, stride 1
        is a measured null (plan §3.1 item 4).
        """
        if self._vision(lambda now: self._episode_started(now, t)):
            await self.broadcast(self.clock.snapshot())

    def _episode_started(self, now: float, t: float | None) -> bool:
        if not self._episode_open:
            self._begin_episode(t)
        if not self.vision_enabled or not self.clock_live:
            return False
        if not self.clock.motion_started(now):
            return False  # only a RUNNING countdown freezes; GRACE/IDLE decline
        self._provisional_open_mono = now
        if self._pace_since_mono is not None:
            # The stroke pace-wise ends at the freeze (~0.25 s after it) —
            # held with the provisional, committed or restored by its verdict.
            self._pace_pending = (self._pace_since_mono, now - self._pace_since_mono)
            self._pace_since_mono = None
        self._log("shot_provisional", t=t, frame_t=t)
        return True

    async def episode_discarded(self, t: float | None = None) -> None:
        """#33 Part B: the engine unlatched with no settle — its pickup
        discard (ball in hand carried off, mid-table). The engine only ever
        discards unfired episodes (a fired one always ends in a settle), so
        the provisional reverts outright: no shot, exact remaining restored.
        A scratch hold owed through the freeze is granted on the way out
        (#63: lost once, 2026-08-13, when the verdict came during the freeze).
        """
        if self._vision(lambda now: self._episode_discarded(now, t)):
            await self.broadcast(self.clock.snapshot())

    def _episode_discarded(self, now: float, t: float | None) -> bool:
        self._episode_open = False
        if not self.vision_enabled or not self.clock_live:
            return False
        reverted = self.clock.motion_reverted(now)
        if reverted:
            self._revert_pace()
            self._log("shot_provisional_reverted", t=t, frame_t=t, reason="pickup_discard")
            self._maybe_hold_for_scratch()
        self._provisional_open_mono = None
        return reverted

    def _abandon_provisional(self) -> None:
        """An operator key rescoped the clock: the open episode is over (#33).

        Every command that clears `_idle_fire` clears this too — they are the
        same statement about the same thing, that whatever vision was in the
        middle of no longer describes what the clock is doing. The pace
        measurement the provisional held goes back to being an open stamp
        (`_revert_pace`); the commands that reopen a countdown re-stamp it
        themselves right after.
        """
        self._provisional_open_mono = None
        self._revert_pace()

    def _commit_pace(self) -> None:
        """The provisional's shot counted: land its held pace measurement (#30)."""
        if self._pace_pending is not None:
            _, took = self._pace_pending
            self._pace_last_s = took
            self._pace_sum_s += took
            self._pace_shots += 1
            self._pace_pending = None

    def _revert_pace(self) -> None:
        """The provisional reverted: the shot is still in progress. Discard the
        held measurement and let the pace stamp keep accruing wall time — a
        carry honestly lengthens the shot that used it, like a hold (#30)."""
        if self._pace_pending is not None:
            self._pace_since_mono = self._pace_pending[0]
            self._pace_pending = None

    async def scratch_seen(self, t: float | None = None) -> None:
        # The confirmed verdict never moves the clock, and since #63 it does
        # not cancel a hold that is still owed either (2026-08-13: the verdict
        # 0.14 s into a freeze that was then reverted left the countdown
        # running with no hold). Since #26 this is a candidate the whiteness
        # census could not refute; journals without candidate records predate
        # the split and stay what they always were — an upper bound.
        self._vision(lambda now: self._scratch_seen(t))

    def _scratch_seen(self, t: float | None) -> bool:
        self._log("vision_scratch", t=t, frame_t=t)
        return False

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
        # so a live candidate is auditable from the journal alone. A settle
        # still waiting for its list is decided first: with a candidate open
        # it is no pick-up (#64), and the hold needs the countdown it starts.
        decided = self._flush_held("candidate")
        state = self._scratch if self._scratch is not None else _ShotScratch()
        self._scratch = state
        if state.closed == "refuted" and not state.granted:
            state.closed = None  # fresh evidence; still one hold per shot
        state.candidate = True
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
            # #63: the list already called this shot's scratch (no second
            # hold), or saw the cue at the settle (no hold at all).
            listed=state.listed,
            cleared=state.closed == "cleared",
        )
        # #25: the candidate usually lands ~0.3-1.3 s *after* the settle
        # already started the next countdown (scratch_shot_0801: settle 5.73,
        # candidate 6.37) — hold that countdown now. The other order (candidate
        # during MOTION, shot_debug's shape) is covered in balls_settled.
        if self._maybe_hold_for_scratch() or decided:
            await self.broadcast(self.clock.snapshot())

    async def scratch_refuted(
        self, t: float, *, witness_id: int, x_mm: float, y_mm: float, whiteness: float
    ) -> None:
        # The witness's position and reading ride along so any refutation
        # can be spot-audited against footage later.
        self._flush_held("refuted")
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
        # A refutation stands even when the list called the scratch too
        # (#63: the cue counts as present if either one sees it).
        state = self._scratch
        bih = self._ball_in_hand
        if (state is None or not state.candidate) and bih is not None:
            # #64: the scratch that began ball in hand was not one after all.
            if bih.scratch.candidate and bih.scratch.closed is None:
                bih.scratch.closed = "refuted"
                self._end_ball_in_hand("refuted")
            return
        if state is None or not state.candidate or state.closed is not None:
            return  # a candidate of an earlier shot: nothing of this one to release
        state.closed = "refuted"
        if self.clock.release_hold(self._now()):
            self._log("scratch_hold_released", reason="refuted")
            await self.broadcast(self.clock.snapshot())

    def _maybe_hold_for_scratch(self) -> bool:
        """Grant this shot's one hold if it is owed and can be granted now.

        Gated exactly like the events that move the clock: the kill switch
        and `no_clock` mode must also keep the hold off it. RUNNING-only by
        way of ShotClock.hold — in MOTION the countdown is frozen anyway, and
        the settle or reverted freeze that ends it tries again. True when a
        hold was granted.
        """
        state = self._scratch
        if state is None or not state.owed:
            return False
        if not self.vision_enabled or not self.clock_live:
            return False
        if self.clock.state is not State.RUNNING:
            return False
        self.clock.hold(self._now(), SCRATCH_HOLD_SECONDS)
        state.granted = True
        self._log("scratch_hold", hold_s=SCRATCH_HOLD_SECONDS, by=state.by)
        return True
