"""Shot-clock rulesets.

Validated against published rules on 2026-07-27 (sources in ``docs/progress.md``):

- **Ultimate Pool** (singles): 30 s shot clock with one 15 s extension per
  player per frame; the clock drops to 15 s late in the timed match. The
  ``final10`` variant is that late-match clock — switching to it is manual
  for now (auto-switch is backlog, ``cv-app-rewrite.md`` §2.2).
- **WNT** (Matchroom World Nineball Tour): 30 s shot clock with one 30 s
  extension per player per rack.

``grace_seconds`` / ``post_expiry_seconds`` are app behavior (overlay timing),
not rulebook values. Rulesets are hardcoded rather than loaded from config —
hot-reload was never implemented in the old app and stays backlog.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Ruleset:
    name: str
    base_seconds: int
    extension_seconds: int
    extensions_per_frame: int
    grace_seconds: float
    post_expiry_seconds: float


RULESETS: dict[str, Ruleset] = {
    ruleset.name: ruleset
    for ruleset in (
        Ruleset(
            name="ultimate_pool_8ball",
            base_seconds=30,
            extension_seconds=15,
            extensions_per_frame=1,
            grace_seconds=2.0,
            post_expiry_seconds=2.0,
        ),
        Ruleset(
            name="ultimate_pool_8ball_final10",
            base_seconds=15,
            extension_seconds=15,
            extensions_per_frame=1,
            grace_seconds=2.0,
            post_expiry_seconds=2.0,
        ),
        Ruleset(
            name="wnt_9ball",
            base_seconds=30,
            extension_seconds=30,
            extensions_per_frame=1,
            grace_seconds=2.0,
            post_expiry_seconds=2.0,
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
