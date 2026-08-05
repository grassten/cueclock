"""Session modes — what kind of practice the clock is shaping.

A mode is orthogonal to a ruleset. A ruleset is a *rulebook* (Ultimate Pool
8-ball, WNT 9-ball) and every entry in ``rulesets.py`` cites published rules; a
mode is what you are doing at the table, and the same rulebook applies across
all of them. Steven runs three (2026-08-02):

- **match** — a rack. Break, rack boundaries, the ruleset's first-shot
  allowance, extensions per rack, and the IDLE break gate.
- **run_out** — fifteen balls thrown up, no rack, run them. There is no break,
  so there is no first-shot allowance and no break gate; the clock is the
  ruleset's base countdown on every shot including the first.
- **no_clock** — actual drills: repeat a shot as long as you like. Vision keeps
  running and keeps journaling (a drill still produces evidence, and a flag
  press still dumps a real ring) — it just doesn't drive the clock.

Modes are hardcoded and ordered: the Stream Deck's Mode key cycles them in the
order below, so it reads as decreasing formality.
"""

from dataclasses import dataclass

# The IDLE break gate (checkpoint 28, revised 33). Out of a fresh rack the only
# legal shot is a break, and the one live phantom that ever started a rack came
# from racking motion (2026-08-01 11:14:49, journal-pinned: it started the clock
# 19 s early and swallowed the real break). Measured with the semantics
# `Hub.shot_taken` uses: breaks 5845-7361 mm/s, every ordinary shot <= 2868,
# placement lookalikes below 1000. 3000 was 1.9x under the slowest measured
# break.
#
# This is a *match* number, and it is why `run_out` exists as a mode: the gate
# ate honest ~1000 mm/s drill openers twice (flagged_7 session, Start pressed
# both times — BACKLOG). Lowering the floor for everyone was the wrong fix,
# because placement lookalikes sit at ~1000 too; giving the no-rack case its
# own mode is the right one.
#
# 3000 -> 2500 (checkpoint 64, flag_missed 2026-08-03 17:37:23Z). The gate ate
# a normal break that reported **2996 mm/s** — 4 mm/s under the floor. Ground
# truth is Steven's: a normal-power break. The clock stayed IDLE through it and
# he recovered by switching to run_out and pressing Restart, which is precisely
# the table-side intervention the floor's "Start key recovers" trade-off was
# waving off.
#
# Anchored between the two ground-truth-labeled populations, both from session
# journals (every IDLE impulse the gate ever saw, `vision_shot_taken_ignored_idle`):
#
#   real break, gated     2996 (this one, Steven-confirmed)
#   racking, gated        2072 (journal-pinned phantom, 2026-08-01) and a
#                         placement cluster at 251-1118
#
# 2500 is the geometric middle of that boundary (sqrt(2072 * 2996) = 2492):
# 17% under the confirmed break, 1.2x over the worst labeled racking impulse,
# 2.5x over the placement cluster. It admits five historical IDLE impulses
# measured at 2568-2974 whose ground truth is unknown.
#
# The asymmetry is what justifies moving toward the racking side rather than
# away from it: a gated real break leaves the clock dead and needs a press at
# the table, while an early start from racking self-heals — the real break that
# follows fires a ShotTaken and resets the clock to 30.
#
# No floor can cover the whole class, and this one does not pretend to:
# break_underread_0801 is a frame-verified rack explosion whose ShotTaken read
# **705 mm/s** (the white streaked too fast to be observed). 705 sits inside the
# placement cluster, so speed alone cannot separate it — that gap stays open and
# stays documented on that scenario, not papered over here.
MATCH_BREAK_FLOOR_MMS = 2500.0


@dataclass(frozen=True)
class Mode:
    name: str
    clock_live: bool  # False: vision still journals, but never moves the clock
    rack_scoped: bool  # False: no first-shot allowance — base countdown always
    break_floor_mms: float | None  # opening-shot speed floor in IDLE; None = any speed


MODES: dict[str, Mode] = {
    mode.name: mode
    for mode in (
        Mode(
            name="match",
            clock_live=True,
            rack_scoped=True,
            break_floor_mms=MATCH_BREAK_FLOOR_MMS,
        ),
        Mode(
            name="run_out",
            clock_live=True,
            rack_scoped=False,
            # No break to gate: the opening shot of a run-out is an ordinary
            # shot and arms the clock at any speed.
            break_floor_mms=None,
        ),
        Mode(
            name="no_clock",
            clock_live=False,
            rack_scoped=False,
            break_floor_mms=None,
        ),
    )
}

DEFAULT_MODE = "match"


def get_mode(name: str) -> Mode:
    """Resolve a mode by name (KeyError if unknown)."""
    return MODES[name]


def next_mode(active: str) -> str:
    """The mode after ``active`` in table order, wrapping; unknown -> the first."""
    names = list(MODES)
    try:
        return names[(names.index(active) + 1) % len(names)]
    except ValueError:
        return names[0]
