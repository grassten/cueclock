"""Shot-clock rulesets.

Validated against published rules on 2026-07-27 (sources in ``CHANGELOG.md``):

- **Ultimate Pool** (singles): 30 s shot clock with one 15 s extension per
  player per frame; the clock drops to 15 s late in the timed match. The
  ``final10`` variant is that late-match clock — switching to it is manual
  for now (auto-switch is backlog, ``design.md`` §2.2).
- **WNT** (Matchroom World Nineball Tour, Event Rules §5, re-verified
  2026-07-31): 30 s shot clock; one 30 s extension per player per rack; 60 s
  for the first shot after the break-off or after a push out (the break
  itself is untimed).

Every ruleset's extension allowance is per *player* per rack, and the clock
has no turn tracking, so ``extensions_per_frame`` is the whole-rack pool —
twice the per-player figure, self-policed at the table. Per-player enforcement
is backlog.

``first_shot_seconds`` is the countdown used while fewer than two shots have
been taken in the rack (the break is shot 1). The Ultimate Pool sources give
no first-shot allowance, so both 8-ball rulesets set it equal to
``base_seconds`` — preserving the behavior validated 2026-07-27.

``grace_seconds`` / ``post_expiry_seconds`` are app behavior (overlay timing),
not rulebook values. Rulesets are hardcoded rather than loaded from config —
hot-reload was never implemented in the old app and stays backlog.
"""

from dataclasses import dataclass

# Extension pools are data, not law, while this is False: `extension()` grants
# past `extensions_per_frame` and the snapshot reports the pool as 0 so no
# surface draws a count. Off since 2026-08-13 — the verbal extension can be
# triggered by table-side conversation, and against an unlimited pool a false
# grant costs nothing instead of burning a real one mid-rack. Flip back on
# when the microphone earns enforcement.
ENFORCE_EXTENSION_LIMIT = False


@dataclass(frozen=True)
class Ruleset:
    name: str
    base_seconds: int
    first_shot_seconds: int  # countdown while < 2 shots taken this rack
    extension_seconds: int
    extensions_per_frame: int
    grace_seconds: float
    post_expiry_seconds: float
    # False = this ruleset has no shot clock at all: the countdown never runs,
    # vision keeps journaling but can never move the clock, and every clock
    # surface (wall, deck, phone) drops its countdown controls.
    timed: bool = True


RULESETS: dict[str, Ruleset] = {
    ruleset.name: ruleset
    for ruleset in (
        Ruleset(
            name="ultimate_pool_8ball",
            base_seconds=30,
            first_shot_seconds=30,  # == base: no first-shot allowance in the sources
            extension_seconds=15,
            # One extension per *player* per frame, and the clock has no turn
            # tracking — so the rack pool is 2, self-policed at the table, the
            # same modelling the WNT ruleset below has always used. It was 1
            # until 2026-08-03, which silently enforced one per *rack* and left
            # the second player with none.
            extensions_per_frame=2,
            grace_seconds=2.0,
            post_expiry_seconds=2.0,
        ),
        Ruleset(
            name="ultimate_pool_8ball_final10",
            base_seconds=15,
            first_shot_seconds=15,  # == base, as above
            extension_seconds=15,
            extensions_per_frame=2,  # per player, as above
            grace_seconds=2.0,
            post_expiry_seconds=2.0,
        ),
        Ruleset(
            name="wnt_9ball",
            base_seconds=30,  # WNT Event Rules §5: 30 s per shot
            first_shot_seconds=60,  # WNT §5: 60 s after the break-off / a push out
            extension_seconds=30,  # WNT §5: one per player per rack
            extensions_per_frame=2,
            grace_seconds=2.0,
            post_expiry_seconds=2.0,
        ),
        # Match play with no shot clock (2026-08-13). A ruleset rather than a
        # session mode because the mode axis says what you're doing at the
        # table, and this is still a match — racks, scores, two players — just
        # untimed. `no_clock` mode is the drill surface (no racks, rep
        # counters); this keeps everything match mode has except the countdown.
        # The clock fields are inert while `timed` is False; base_seconds keeps
        # a sane value so `current_base()` never has to special-case.
        Ruleset(
            name="freestyle",
            base_seconds=30,
            first_shot_seconds=30,
            extension_seconds=0,
            extensions_per_frame=0,
            grace_seconds=2.0,
            post_expiry_seconds=2.0,
            timed=False,
        ),
    )
}

# Alternate identifiers accepted anywhere a ruleset name is taken (API, CLI).
# "ultimate_9ball" is the old app's name for what is really the WNT ruleset —
# kept so existing Stream Deck buttons don't break.
ALIASES: dict[str, str] = {
    "ultimate_9ball": "wnt_9ball",
    "matchroom_9ball": "wnt_9ball",
}

DEFAULT_RULESET = "ultimate_pool_8ball"


def get_ruleset(name: str) -> Ruleset:
    """Resolve a canonical name or alias to its ruleset (KeyError if unknown)."""
    return RULESETS[ALIASES.get(name, name)]
