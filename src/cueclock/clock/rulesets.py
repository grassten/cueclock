"""Shot-clock rulesets.

Values carried verbatim from the old app's ``config.yaml`` (see
``docs/cv-app-rewrite.md`` §2.1). Hardcoded rather than loaded from a config
file — hot-reload was never implemented in the old app and stays backlog.
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
            extension_seconds=30,
            extensions_per_frame=1,
            grace_seconds=2.0,
            post_expiry_seconds=2.0,
        ),
        Ruleset(
            name="ultimate_pool_8ball_final10",
            base_seconds=15,
            extension_seconds=30,
            extensions_per_frame=1,
            grace_seconds=2.0,
            post_expiry_seconds=2.0,
        ),
        Ruleset(
            name="ultimate_9ball",
            base_seconds=30,
            extension_seconds=30,
            extensions_per_frame=1,
            grace_seconds=2.0,
            post_expiry_seconds=2.0,
        ),
    )
}

DEFAULT_RULESET = "ultimate_pool_8ball"
