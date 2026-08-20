"""Session accuracy report: the clock's own missed-strike compensation, read back.

The accuracy work in this repo was ranked by argument for weeks because nothing
produced a **denominator** — "the clock missed a shot" was only ever observed
when Steven noticed and hit Flag, which samples the failures he saw and nothing
else. The 2026-08-02 answer was a Stream Deck key pressed every shot; it was
used in two sessions and never again, because a key you must remember at the
table is a key that stops being pressed (#19, retired 2026-08-05).

**The denominator was already being written.** Two detectors run per shot, and
only one of them is fragile:

    "he hit the ball"          needs to identify the cue ball and catch it
                               leaving its spot, through the moment the stick
                               and hand are one blob with it.  THIS one misses.

    "everything has stopped"   needs no ball identity at all.  Reliable.

When the first fails and the second fires, `ShotClock.balls_settled` reasons
that balls came to rest without a detected shot, so a shot must have happened,
and bumps the rack shot count (`clock/state.py`, 2026-07-31 — written for
correctness, because a short count replayed the WNT §5 60 s window). Every
journal record carries `rack_shots`. Therefore:

> a `vision_balls_settled` record whose `rack_shots` exceeds the preceding
> record's **is the engine confessing it missed a strike**.

Settles recorded in `IDLE` are excluded: those are racking motion the clock
correctly ignored, not shots.

## Why derived, never accumulated

Nothing here runs live and nothing is tallied into a counter. The journal is
the system of record, so the rate is recomputed from it on demand — which means
it survives a crash, a `kill -9`, a dead deck daemon, and can be recomputed
over every session ever recorded the day the reader improves.

## The opposite error, and why it is only ever a candidate

A **phantom** is the mirror image: the clock freezes on a `ShotTaken` that no
strike produced. There is no compensation to read back for it — the engine
believes the phantom, so it confesses nothing — and the journal records
positions only through the events they produced. So a phantom cannot be
*proven* here. What can be measured is a freeze whose surroundings are
physically wrong for a shot, and this module reports four such symptoms
(measurements in the constants below):

    no roll-out    froze and released with nothing in between
    snap-back      froze again before a ball could have been addressed
    split freeze   froze, then a second strike arrived past the longest
                   roll-out ever measured — the freeze and the roll-out
                   belong to different strikes
    left frozen    never released; an operator key had to rescue the clock

The first two are the headline "phantom candidates". The last two are counted
apart because each is **shared with a different failure** and the journal
cannot say which it saw: a split freeze reads identically to a real shot whose
*settle* was missed, and a left-frozen clock reads identically to the operator
pressing New Rack over a live roll-out.

## Honest limits

- **A floor, not a ceiling** for misses. A shot whose *settle* was also missed
  leaves no trace here at all.
- **The phantom count is neither a floor nor a ceiling.** Each candidate may be
  a real shot with unlucky timing, and the whole population is undercounted:
  the *one* phantom in this history that was ever verified against frames
  (2026-08-01 11:14:49, mid-racking, 19 s before the break it then swallowed —
  CHANGELOG checkpoint 28) is a split freeze, and a phantom that freezes the
  clock two seconds before the real strike leaves a journal record that is
  indistinguishable from a clean shot. Read the number as a **comparator**:
  recompute it before and after a detection change and watch it move. Read as
  an absolute it is only an upper bound on the symptoms listed above.
- **Validated only against 29 shots.** The retired "I shot" key marked two
  2026-08-03 sessions; every one of those 29 strikes is human-confirmed real
  (13 pressed, 6 more confirmed by Steven as forgotten presses at triage, plus
  10 in the second session) and the candidate rules fire on none of them. That
  is zero false positives out of 29 — too small to bound the false-positive
  rate, since a proxy that was wrong every time it fired would still clear 29
  shots 61% of the time. The presses themselves are read here like any other
  record: by compensation.
- **The flag stream corroborates nothing either way.** The deck's PHANTOM
  reason key has been live since 2026-08-01 22:59 and has never been pressed,
  against 10 MISSED presses in the same span. That bounds the *noticeable*
  phantom near zero, and says nothing about the candidates below — every one
  of them released the clock again within 3 s, which is a blip on the wall, not
  something to walk over and flag.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from statistics import median

from cueclock.server.games import DRILL_ATTEMPT as DRILL_ATTEMPT_KIND
from cueclock.server.games import LAG_ATTEMPT as LAG_ATTEMPT_KIND
from cueclock.server.games import GameStats

SETTLE_KIND = "vision_balls_settled"
SHOT_KIND = "vision_shot_taken"
SCRATCH_KIND = "vision_scratch"
# Two-stage scratch verdict (#26): candidate/refuted are the evidence trail;
# SCRATCH_KIND is the confirmed verdict. Journals with no candidate records
# predate the split — their scratch count is the old upper bound (~6x, per
# the live confession) and must be read as such.
SCRATCH_CANDIDATE_KIND = "vision_scratch_candidate"
SCRATCH_REFUTED_KIND = "vision_scratch_refuted"
FLAG_KIND = "flag_dump"
HEARTBEAT_KIND = "detect_heartbeat"
SUMMARY_KIND = "session_summary"
# E8/E9 hub-policy marks (2026-08-06, accuracy plan §13.2/§13.3). The break
# class joined this report the day the hub learned to adjudicate it, so the
# instrument can never again be blind to the head of the Pareto (a missed
# break in IDLE never confessed — §13.0).
BREAK_ADJUDICATED_KIND = "break_adjudicated"
# Historical only: the dark tier's shadow mark, emitted between checkpoints 75
# and #36 (2026-08-11) while the no-fire tier journalled instead of counting.
# Live journals never carry it again — a dark catch is a `break_adjudicated`
# with `tier="dark"` — but it stays parsed so old sessions still read right.
BREAK_DARK_CANDIDATE_KIND = "break_dark_candidate"
SETTLE_SYNTHESIZED_KIND = "settle_synthesized"
# #33 the provisional shot: the latch froze a RUNNING countdown; the settle
# (or the engine's pickup discard) later counted or reverted it. A reverted
# or suppressed settle is NOT a shot — the clock was left untouched — so
# neither enters `shots`; they are tallied separately below.
SHOT_PROVISIONAL_KIND = "shot_provisional"
PROVISIONAL_REVERTED_KIND = "shot_provisional_reverted"
COMPENSATION_SUPPRESSED_KIND = "compensation_suppressed"

# Emitted-then-gated shots. "Vision saw it, the clock declined it" is a
# different bug from "vision never saw it", and only the second is a miss.
GATED_SHOT_KINDS = {
    "vision_shot_taken_ignored_idle": "gated_idle",
    "vision_shot_taken_no_clock": "gated_no_clock",
    "vision_shot_taken_killed": "gated_kill_switch",
}

# The head of a session, judged separately. Not a statistical unit — a
# perception one: a miss on the first shot after walking downstairs costs far
# more than the same miss mid-rack (Steven, 2026-08-05). Measured over every
# session to date, 12% opened with a miss inside this many shots.
OPENING_SHOTS = 3

COMMAND_PREFIX = "cmd_"

# Shot → settle below this is a phantom *candidate*: the clock froze and
# released with no time for a roll-out. Anchored to the measured roll-out range
# on real shots — 2.27-8.62 s (2026-08-03 session, `logs/session-20260803-121204`)
# — so 1 s sits far below the shortest real one. A review candidate, never a
# verdict: the journal cannot prove a phantom.
PHANTOM_SETTLE_S = 1.0

# Previous settle → the strike that froze the clock again, below which the
# freeze is a phantom candidate: the balls stopped and the clock re-froze
# before anyone could have walked over and addressed a ball.
#
# Anchored to the only strikes on record a human confirmed: the 29 marked with
# the retired "I shot" key (2026-08-03, `session-20260803-121204` +
# `-124627`), whose fastest address was **5.55 s**. This sits at less than half
# of that. For scale, all 724 measurable addresses in `logs/` have a median of
# 12.9 s and only 9 fall below this line.
#
# The two mechanisms that produce a sub-2.5 s address are both phantoms, which
# is why the floor is worth having: a settle that fired over a ball still
# creeping, whose continued creep then re-reads as a strike (CHANGELOG
# checkpoint 28 pinned one at 0.7 s, clock stuck in MOTION for 17 s after); and
# a hand entering the frame the moment the table goes quiet.
PHANTOM_ADDRESS_S = 2.5

# Within one freeze, first strike → last strike, above which the two cannot be
# the same ball motion: the longest shot→settle roll-out ever measured is
# 8.62 s (same 2026-08-03 session), so a second `ShotTaken` arriving later than
# that is a separate physical event and the freeze started before it.
#
# Counted apart from the headline candidates on purpose. A real shot whose
# settle was missed, followed by the next real shot, writes exactly these
# records — measured both ways in the same history, and the timing does not
# separate them (the confirmed 2026-08-01 phantom fired 19.1 s early, and the
# median address is 12.9 s, so "too long to be one shot" fits either story).
PHANTOM_LEAD_S = 8.62

# Detect-rate floor for calling a shot's detection starved. The live loop runs
# 70-78 Hz healthy (stride 2 of the camera's 156 fps); flag dumps and corpus
# batteries drag it to 11-30 Hz, and a shot detected down there failed for a
# reason that is not the vision logic.
STARVED_HZ = 50.0

_SINCE = re.compile(r"^(\d+(?:\.\d+)?)([hdw])$")
_SINCE_SECONDS = {"h": 3600.0, "d": 86400.0, "w": 604800.0}


def parse_since(text: str) -> float:
    """`"7d"` → seconds. Accepts hours, days, weeks."""
    match = _SINCE.match(text.strip().lower())
    if match is None:
        raise ValueError(f"expected a span like 7d / 24h / 2w, got {text!r}")
    return float(match.group(1)) * _SINCE_SECONDS[match.group(2)]


@dataclass(frozen=True)
class Shot:
    """One shot, as the session recorded it.

    Created at the *settle*, because that is the event that exists whether or
    not the strike was detected — which is the whole point.
    """

    index: int  # 1-based within the session
    t: float | None  # capture clock, at the settle
    seen: bool  # False = the strike was missed and the settle stood in
    settle_gap_s: float | None  # last strike → settle, when the strike was seen
    hz: float | None  # detect rate reported just before it
    # Previous settle → the strike that *froze* the clock. Deliberately the
    # first strike of the freeze rather than the last: a freeze is opened once,
    # and everything after it is a re-fire the clock discards (`ShotClock.
    # shot_taken` returns early in MOTION), so only the first one is the event
    # whose plausibility is in question.
    address_gap_s: float | None = None
    # First strike → last strike inside one freeze. 0.0 when nothing re-fired.
    freeze_lead_s: float | None = None

    @property
    def starved(self) -> bool:
        return self.hz is not None and self.hz < STARVED_HZ

    @property
    def no_rollout(self) -> bool:
        """Froze and released with no time for a ball to travel."""
        return self.settle_gap_s is not None and self.settle_gap_s < PHANTOM_SETTLE_S

    @property
    def snap_back(self) -> bool:
        """Froze again before the previous settle could have been walked to."""
        return self.address_gap_s is not None and self.address_gap_s < PHANTOM_ADDRESS_S

    @property
    def split_freeze(self) -> bool:
        """The freeze and the roll-out belong to different strikes.

        Ambiguous with a missed *settle* by construction — see `PHANTOM_LEAD_S`
        — so it is reported beside the candidates, never inside them.
        """
        return self.freeze_lead_s is not None and self.freeze_lead_s > PHANTOM_LEAD_S

    @property
    def phantom_candidate(self) -> bool:
        """The two symptoms with no second reading. Still a candidate, not a verdict."""
        return self.no_rollout or self.snap_back


# How each `rejected` reason reads in the summary line. Unknown reasons print
# their raw key rather than being dropped — a refusal the report cannot name
# is exactly the one worth seeing.
REJECTION_BLURBS = {
    "phantom": "phantom (table unchanged)",
    "hand_placement": "ball-in-hand (every arrival hand-delivered)",
}


@dataclass
class SessionReport:
    """One journal, read for accuracy."""

    path: Path
    shots: list[Shot] = field(default_factory=list)
    scratches: int = 0
    # The two-stage scratch verdict's evidence records (#26). A journal with
    # candidates carries adjudicated scratch counts — `scratches` there is a
    # measurement, not the pre-#26 upper bound. candidates == refuted +
    # confirmed once every candidate has resolved (a session cut mid-verdict
    # can be one short).
    scratch_candidates: int = 0
    scratch_refuted: int = 0
    flags: int = 0
    racking_settles: int = 0
    gated: dict[str, int] = field(default_factory=dict)
    hz: list[float] = field(default_factory=list)
    torn_lines: int = 0
    # The break class (§13.0's Pareto head), visible at last. Auto-caught =
    # the hub adjudicated an armed rack's settle as the break; manual = a
    # restart press had to count it (rack_shots jumped 0→1 on the command);
    # dark candidates = shadow marks from the no-fire tier, the measurement
    # that decides whether it goes live.
    breaks_adjudicated: int = 0
    break_presses: int = 0
    dark_candidates: int = 0
    # E9's watchdog confessions: settles the hub synthesized because MOTION
    # sat motion-free past the watchdog. Counted inside `shots` (the clock
    # acted on them) and surfaced separately here.
    settles_synthesized: int = 0
    # #33: provisional freezes (latch edges that stopped a RUNNING countdown)
    # and the episodes adjudicated as not-a-shot. Reverts released a freeze
    # with the time restored; suppressions refused the compensation on a
    # countdown that never stopped. Neither is in `shots`.
    provisionals: int = 0
    provisional_reverts: int = 0
    suppressed_settles: int = 0
    # Settles the hub refused at settle time, total and split by why. NOT in
    # `shots`: the clock did not act on them, and counting them would inflate
    # every rate below with the exact classes these rejections exist to
    # remove. This is also the honest live measure of both features —
    # `phantom_candidates` cannot fall to prove either one worked, because a
    # rejected settle stops producing a `Shot` at all. The pair to read is
    # these numbers rising while `missed` stays flat; if a predicate starts
    # refusing real shots they come back as misses and frozen clocks, not as
    # a quieter phantom count.
    #
    # Two reasons today, and they must be read separately because they make
    # different predictions (handoff §6): `phantom` (#37) is the table
    # provably unchanged against a real quiet baseline; `hand_placement`
    # (#41) is every ball that arrived somewhere new having been put there
    # by a hand.
    settles_rejected: int = 0
    settles_rejected_by_reason: dict[str, int] = field(default_factory=dict)
    # Freezes an operator key had to rescue, because no settle ever released
    # them. Not `Shot`s and not in `total`: a shot here is a settle, and these
    # never reached one. See `left_frozen` in the module docstring for why the
    # count is a symptom rather than a phantom.
    left_frozen: int = 0

    @property
    def total(self) -> int:
        return len(self.shots)

    @property
    def missed(self) -> list[Shot]:
        return [s for s in self.shots if not s.seen]

    @property
    def miss_rate(self) -> float | None:
        return len(self.missed) / self.total if self.total else None

    @property
    def opening_miss(self) -> bool:
        """Did a miss land in the first `OPENING_SHOTS` shots of the session?"""
        return any(not s.seen for s in self.shots[:OPENING_SHOTS])

    @property
    def starved_misses(self) -> list[Shot]:
        """Misses that coincided with a starved detect loop — not vision's fault."""
        return [s for s in self.missed if s.starved]

    @property
    def phantom_candidates(self) -> list[Shot]:
        return [s for s in self.shots if s.phantom_candidate]

    @property
    def phantom_rate(self) -> float | None:
        """Candidates over shots — the same denominator the miss rate uses.

        Comparable to `miss_rate` on purpose: the whole reason to measure this
        is that a second strike path OR-ed into the first trades one for the
        other, and a trade needs both numbers on one scale.
        """
        return len(self.phantom_candidates) / self.total if self.total else None

    @property
    def split_freezes(self) -> list[Shot]:
        return [s for s in self.shots if s.split_freeze]


def _number(record: dict[str, object], key: str) -> float | None:
    value = record.get(key)
    return float(value) if isinstance(value, int | float) else None


def build(path: Path) -> SessionReport:
    """Read one session journal into a `SessionReport`.

    Unknown kinds and unknown fields are ignored, per the journal's additive
    contract; a torn final line from a killed session is counted, not an error.
    """
    report = SessionReport(path=path)
    prev_rack: float | None = None
    pending_shot_t: float | None = None  # newest strike of the open freeze
    freeze_open_t: float | None = None  # the strike that opened it
    last_settle_t: float | None = None  # previous settle the clock acted on
    last_hz: float | None = None
    adjudicated_pending = False  # break_adjudicated seen; the next settle is a caught break

    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            report.torn_lines += 1
            continue
        if not isinstance(record, dict):
            continue
        kind = record.get("kind")
        rack = _number(record, "rack_shots")

        if kind == HEARTBEAT_KIND:
            hz = _number(record, "hz")
            if hz is not None:
                last_hz = hz
                report.hz.append(hz)
        elif kind == SHOT_KIND:
            shot_t = _number(record, "frame_t") or _number(record, "t")
            if pending_shot_t is None:
                # First strike since the last release: this is the one the
                # clock acted on. Later ones land in MOTION and are discarded.
                freeze_open_t = shot_t
            pending_shot_t = shot_t
        elif isinstance(kind, str) and kind.startswith(COMMAND_PREFIX):
            if kind == "cmd_restart" and rack == 1 and prev_rack == 0:
                # Restart with no shot counted this rack counts the break
                # (`ShotClock.restart`) — this press IS the manual rescue of
                # a missed break, the failure class `report` was blind to
                # until 2026-08-06 (§13.0).
                report.break_presses += 1
            if pending_shot_t is not None and record.get("state") != "MOTION":
                # A command is journaled *after* the clock has answered it, so
                # a non-MOTION state here means this key took the clock out of
                # a freeze that no settle ever ended.
                report.left_frozen += 1
                pending_shot_t = freeze_open_t = None
        elif kind == SCRATCH_KIND:
            report.scratches += 1
        elif kind == SCRATCH_CANDIDATE_KIND:
            report.scratch_candidates += 1
        elif kind == SCRATCH_REFUTED_KIND:
            report.scratch_refuted += 1
        elif kind == FLAG_KIND:
            report.flags += 1
        elif kind == BREAK_ADJUDICATED_KIND:
            report.breaks_adjudicated += 1
            adjudicated_pending = True
        elif kind == BREAK_DARK_CANDIDATE_KIND:
            report.dark_candidates += 1
        elif kind == SHOT_PROVISIONAL_KIND:
            report.provisionals += 1
        elif kind == PROVISIONAL_REVERTED_KIND:
            # The episode (and any deferred fire inside it) was adjudicated
            # not-a-shot: release the pending strike so it cannot pair with —
            # or mark seen — the next real settle.
            report.provisional_reverts += 1
            pending_shot_t = freeze_open_t = None
        elif kind == COMPENSATION_SUPPRESSED_KIND:
            report.suppressed_settles += 1
            pending_shot_t = freeze_open_t = None
        elif isinstance(kind, str) and kind in GATED_SHOT_KINDS:
            label = GATED_SHOT_KINDS[kind]
            report.gated[label] = report.gated.get(label, 0) + 1
        elif kind in (SETTLE_KIND, SETTLE_SYNTHESIZED_KIND):
            if record.get("state") == "IDLE":
                # Racking motion: `balls_settled` returned early, so the clock
                # never counted it and it is not a shot.
                report.racking_settles += 1
                adjudicated_pending = False
            elif (reason := record.get("rejected")) is not None:
                # The hub refused this settle on positive evidence (#37's
                # phantom or #41's hand placement) and released the freeze.
                # The clock counted nothing, so neither does the report — the
                # accompanying revert record has already cleared the pending
                # strike.
                report.settles_rejected += 1
                key = str(reason)
                report.settles_rejected_by_reason[key] = (
                    report.settles_rejected_by_reason.get(key, 0) + 1
                )
                adjudicated_pending = False
                last_settle_t = _number(record, "frame_t") or _number(record, "t")
            else:
                if kind == SETTLE_SYNTHESIZED_KIND:
                    report.settles_synthesized += 1
                settle_t = _number(record, "frame_t") or _number(record, "t")
                # The engine bumped the count at the settle ⇒ no strike was
                # seen. The first record of a session has nothing to compare
                # against, so it is credited as seen. A settle the hub just
                # adjudicated as the break bumps the count too, but by being
                # *caught*, not confessed — that is the opposite of a miss.
                # Since #33 a RUNNING fire defers its count to the settle, so
                # post-#33 journals bump at the settle even when the strike
                # was seen — an open pending strike IS the sighting. (Pre-#33
                # journals are unaffected: their seen fires never bump here.)
                seen = (
                    adjudicated_pending
                    or pending_shot_t is not None
                    or prev_rack is None
                    or rack is None
                    or rack <= prev_rack
                )
                adjudicated_pending = False
                gap = (
                    settle_t - pending_shot_t
                    if seen and settle_t is not None and pending_shot_t is not None
                    else None
                )
                # A missed strike has no freeze, so it has neither an address
                # to judge nor a lead to measure — both stay None rather than
                # borrowing the previous shot's numbers.
                address = (
                    freeze_open_t - last_settle_t
                    if seen and freeze_open_t is not None and last_settle_t is not None
                    else None
                )
                lead = (
                    pending_shot_t - freeze_open_t
                    if seen and pending_shot_t is not None and freeze_open_t is not None
                    else None
                )
                report.shots.append(
                    Shot(
                        index=len(report.shots) + 1,
                        t=settle_t,
                        seen=seen,
                        settle_gap_s=gap,
                        hz=last_hz,
                        address_gap_s=address,
                        freeze_lead_s=lead,
                    )
                )
                pending_shot_t = freeze_open_t = None
                last_settle_t = settle_t

        if rack is not None:
            prev_rack = rack
    return report


def find_journals(logs_dir: Path, since_s: float | None = None) -> list[Path]:
    """Session journals, oldest first, optionally limited to a recent span."""
    paths = sorted(logs_dir.glob("session-*.jsonl"))
    if since_s is None:
        return paths
    newest = max((p.stat().st_mtime for p in paths), default=0.0)
    return [p for p in paths if p.stat().st_mtime >= newest - since_s]


# ---------- minigames (#46) ----------
#
# The picker cards' numbers and the phone Drills list's make % are *derived*
# from the journal, exactly like every other number in this file — nothing in
# the games layer keeps a score of its own. That is what makes abandoning a
# game lossless, and it is why weighted-random serving becomes possible later
# without any new bookkeeping: the weakness report is already on disk.


def _wall_day(record: dict[str, object]) -> str | None:
    """A journal record's wall date as the cards print it ("Aug 18")."""
    wall = record.get("wall")
    if not isinstance(wall, str):
        return None
    try:
        stamp = datetime.fromisoformat(wall)
    except ValueError:
        return None
    return f"{stamp:%b} {stamp.day}"


def game_stats(logs_dir: Path) -> dict[str, GameStats]:
    """Per-game history over every session journal, keyed by game name.

    Free-form `numbers` because each game reads its own kinds and knows what
    they mean — see `GameStats`. A game with no records is simply absent, which
    its card renders as em-dashes rather than as zeroes: never played and
    played badly are different facts.
    """
    numbers: dict[str, dict[str, float]] = {}
    last_played: dict[str, str] = {}
    # The shot catalog's two derived card numbers, accumulated in the same
    # single pass: how many distinct shots have ever been attempted, and the
    # longest streak of consecutive passes. A run does not end because a
    # session did — journals are walked in chronological order — but it does
    # end at any attempt that failed, which is all the number claims.
    shot_ids: set[str] = set()
    best_run = run = 0
    for path in find_journals(logs_dir):
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue  # torn line from a killed session: skip, never fatal
            if not isinstance(record, dict):
                continue
            kind = record.get("kind")
            if kind not in (LAG_ATTEMPT_KIND, DRILL_ATTEMPT_KIND):
                continue
            game = record.get("game")
            if not isinstance(game, str):
                continue
            tally = numbers.setdefault(game, {})
            tally["attempts"] = tally.get("attempts", 0.0) + 1.0
            day = _wall_day(record)
            if day is not None:
                last_played[game] = day
            if kind == LAG_ATTEMPT_KIND:
                distance = _number(record, "distance_mm")
                if distance is not None:
                    best = tally.get("best_mm")
                    tally["best_mm"] = distance if best is None else min(best, distance)
            else:
                made = 1 if record.get("pass") is True else 0
                tally["made"] = tally.get("made", 0.0) + made
                run = run + 1 if made else 0
                best_run = max(best_run, run)
                shot_id = record.get("shot_id")
                if isinstance(shot_id, str):
                    shot_ids.add(shot_id)
                tally["catalog"] = float(len(shot_ids))
                tally["best_run"] = float(best_run)
    return {
        game: GameStats(numbers=tally, last_played=last_played.get(game))
        for game, tally in numbers.items()
    }


def shot_records(logs_dir: Path) -> dict[str, tuple[int, int, str | None]]:
    """Per-shot (attempts, made, last played) — the phone Drills list's numbers."""
    tallies: dict[str, list[int]] = {}
    days: dict[str, str] = {}
    for path in find_journals(logs_dir):
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict) or record.get("kind") != DRILL_ATTEMPT_KIND:
                continue
            shot_id = record.get("shot_id")
            if not isinstance(shot_id, str):
                continue
            tallies.setdefault(shot_id, []).append(1 if record.get("pass") is True else 0)
            day = _wall_day(record)
            if day is not None:
                days[shot_id] = day
    return {shot_id: (len(hits), sum(hits), days.get(shot_id)) for shot_id, hits in tallies.items()}


def summary_detail(report: SessionReport) -> dict[str, object]:
    """The `session_summary` journal record's payload."""
    rate = report.miss_rate
    phantom = report.phantom_rate
    return {
        "shots": report.total,
        "missed": len(report.missed),
        "miss_rate": round(rate, 4) if rate is not None else None,
        "opening_miss": report.opening_miss,
        "phantom_candidates": len(report.phantom_candidates),
        "phantom_rate": round(phantom, 4) if phantom is not None else None,
        "left_frozen": report.left_frozen,
        "scratches": report.scratches,
        "scratch_candidates": report.scratch_candidates,
        "scratch_refuted": report.scratch_refuted,
        "flags": report.flags,
        "breaks_adjudicated": report.breaks_adjudicated,
        "break_presses": report.break_presses,
        "dark_candidates": report.dark_candidates,
        "settles_synthesized": report.settles_synthesized,
        "provisionals": report.provisionals,
        "provisional_reverts": report.provisional_reverts,
        "suppressed_settles": report.suppressed_settles,
        "settles_rejected": report.settles_rejected,
        "settles_rejected_by_reason": report.settles_rejected_by_reason,
    }


def _rate(report: SessionReport) -> str:
    rate = report.miss_rate
    return f"{rate:6.2%}" if rate is not None else "     —"


def _phantom_rate(report: SessionReport) -> str:
    rate = report.phantom_rate
    return f"{rate:6.2%}" if rate is not None else "     —"


def format_session(report: SessionReport) -> str:
    """Human-readable single-session summary."""
    out: list[str] = []
    out.append(f"{report.path.name}")
    out.append("=" * 64)
    if not report.total:
        out.append("  no shots in this journal")
        return "\n".join(out)

    out.append(f"  shots              {report.total}")
    out.append(f"  MISSED             {len(report.missed)}   {_rate(report).strip()}")
    if report.missed:
        marks = ", ".join(
            f"#{s.index}" + (f" @{s.t:.0f}s" if s.t is not None else "") for s in report.missed
        )
        out.append(f"    at               {marks}")
    if report.opening_miss:
        out.append(f"    ⚠ a miss landed in the first {OPENING_SHOTS} shots of the session")
    if report.starved_misses:
        out.append(
            f"    {len(report.starved_misses)} of those ran below {STARVED_HZ:.0f} Hz "
            "— starved detect loop, not a vision failure"
        )
    out.append("")

    candidates = report.phantom_candidates
    out.append(
        f"  phantom candidates {len(candidates)}   "
        f"{_phantom_rate(report).strip()}   (review, not a verdict)"
    )
    if candidates:
        marks = ", ".join(
            f"#{s.index}" + (f" @{s.t:.0f}s" if s.t is not None else "") for s in candidates
        )
        out.append(f"    at               {marks}")
        for label, kept in (
            (
                f"no roll-out (settled <{PHANTOM_SETTLE_S:.0f}s after the strike)",
                [s for s in candidates if s.no_rollout],
            ),
            (
                f"snap-back (froze <{PHANTOM_ADDRESS_S:.1f}s after the last settle)",
                [s for s in candidates if s.snap_back],
            ),
        ):
            if kept:
                out.append(f"    {len(kept)} {label}")
    # Beside the candidates, never inside them: each of these reads identically
    # to a different failure, and the journal holds nothing that separates them.
    if report.split_freezes:
        out.append(
            f"    {len(report.split_freezes)} split freeze(s): a second strike "
            f">{PHANTOM_LEAD_S:.1f}s later carried the roll-out"
        )
        out.append("        — or the first shot's settle was missed; can't tell from here")
    if report.left_frozen:
        out.append(f"    {report.left_frozen} left frozen, rescued by an operator key")
        out.append("        — or New Rack pressed over a live roll-out; can't tell from here")
    out.append("")
    if report.breaks_adjudicated or report.break_presses or report.dark_candidates:
        out.append(
            f"  breaks             {report.breaks_adjudicated} auto-caught / "
            f"{report.break_presses} rescued by press"
            + (
                f"   ({report.dark_candidates} dark candidates — pre-#36 shadow)"
                if report.dark_candidates
                else ""
            )
        )
    if report.settles_synthesized:
        out.append(
            f"  settles synthesized {report.settles_synthesized}   "
            "(frozen-clock watchdog; the underlying miss is counted above)"
        )
    if report.provisionals or report.provisional_reverts or report.suppressed_settles:
        out.append(
            f"  provisional shots  {report.provisionals} frozen at the latch / "
            f"{report.provisional_reverts} reverted (not a shot, time restored)"
            + (
                f" / {report.suppressed_settles} suppressed without a freeze"
                if report.suppressed_settles
                else ""
            )
        )
    if report.settles_rejected:
        by_reason = ", ".join(
            f"{n} {REJECTION_BLURBS.get(reason, reason)}"
            for reason, n in sorted(report.settles_rejected_by_reason.items())
        )
        out.append(
            f"  settles refused    {report.settles_rejected}   "
            f"({by_reason}; not counted, not in the rates above)"
        )
    if report.scratch_candidates:
        out.append(
            f"  scratches          {report.scratches}   "
            f"({report.scratch_candidates} candidates, {report.scratch_refuted} refuted)"
        )
    else:
        # No candidate records: a pre-#26 journal, where every vision_scratch
        # confirmed unadjudicated — an upper bound (~6x live), not a count.
        out.append(f"  scratches          {report.scratches}   (upper bound: pre-#26 journal)")
    if report.total:
        out.append(
            f"    rate             1 per {report.total / max(report.scratches, 1):.0f} shots"
        )
    out.append(f"  flag dumps         {report.flags}")
    if report.racking_settles:
        out.append(f"  racking settles    {report.racking_settles}   (ignored in IDLE, not shots)")
    for label, count in sorted(report.gated.items()):
        out.append(f"  {label:<18} {count}   (vision saw it, the clock declined it)")
    if report.hz:
        ordered = sorted(report.hz)
        out.append("")
        out.append(
            f"  detect loop        median {median(ordered):.1f} Hz   "
            f"p05 {ordered[len(ordered) // 20]:.1f} Hz   min {ordered[0]:.1f} Hz"
        )
    if report.torn_lines:
        out.append(f"  torn lines         {report.torn_lines}   (killed session — not an error)")
    return "\n".join(out)


def format_rollup(reports: list[SessionReport]) -> str:
    """Per-session table plus the total. The total is the number that matters."""
    played = [r for r in reports if r.total]
    out: list[str] = []
    out.append(
        f"{'session':<34}{'shots':>7}{'missed':>8}{'miss':>8}"
        f"{'phant':>7}{'phan':>8}{'scr':>6}{'flag':>6}"
    )
    out.append("-" * 84)
    for report in played:
        out.append(
            f"{report.path.name:<34}{report.total:>7}{len(report.missed):>8}"
            f"{_rate(report):>8}{len(report.phantom_candidates):>7}"
            f"{_phantom_rate(report):>8}{report.scratches:>6}{report.flags:>6}"
        )
    shots = sum(r.total for r in played)
    missed = sum(len(r.missed) for r in played)
    phantoms = sum(len(r.phantom_candidates) for r in played)
    scratches = sum(r.scratches for r in played)
    out.append("-" * 84)
    out.append(
        f"{'TOTAL':<34}{shots:>7}{missed:>8}{missed / shots if shots else 0:>7.2%}"
        f"{phantoms:>7}{phantoms / shots if shots else 0:>7.2%}{scratches:>6}"
    )
    if not shots:
        return "\n".join(out)

    out.append("")
    out.append(f"  miss rate          {missed / shots:.2%}")
    if missed:
        out.append(f"  felt as            a miss every {shots / missed / 14:.1f} racks (14 shots)")
    opening = [r for r in played if r.opening_miss]
    out.append(
        f"  bad first impression  {len(opening)}/{len(played)} sessions "
        f"({len(opening) / len(played):.0%}) opened with a miss in {OPENING_SHOTS} shots"
    )
    # Both rates on one line because the pair is the point: a second strike
    # path OR-ed into the first buys misses with phantoms, and neither number
    # means anything without the other beside it.
    out.append("")
    out.append(f"  phantom candidates {phantoms / shots:.2%}   ({phantoms} of {shots})")
    splits = sum(len(r.split_freezes) for r in played)
    frozen = sum(r.left_frozen for r in played)
    if splits or frozen:
        out.append(
            f"  also, shared with other failures: {splits} split freezes, {frozen} left frozen"
        )
    if scratches:
        candidates = sum(r.scratch_candidates for r in played)
        refuted = sum(r.scratch_refuted for r in played)
        adjudication = (
            f"   ({candidates} candidates, {refuted} refuted)"
            if candidates
            else "   (upper bound: pre-#26 journals)"
        )
        out.append(
            f"  scratches          {scratches}   (1 per {shots / scratches:.0f} shots)"
            + adjudication
        )
    adjudicated = sum(r.breaks_adjudicated for r in played)
    presses = sum(r.break_presses for r in played)
    dark = sum(r.dark_candidates for r in played)
    if adjudicated or presses or dark:
        out.append("")
        out.append(
            f"  breaks             {adjudicated} auto-caught / {presses} rescued by press"
            + (f"   ({dark} dark candidates — pre-#36 shadow)" if dark else "")
        )
    synthesized = sum(r.settles_synthesized for r in played)
    if synthesized:
        out.append(f"  settles synthesized {synthesized}   (frozen-clock watchdog)")
    return "\n".join(out)
