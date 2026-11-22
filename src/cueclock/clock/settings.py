"""The clock's settings: six independent values instead of named rulesets (#74).

Until checkpoint 145 the clock ran on a *ruleset*, a named bundle of a shot
time, a time after the break, an extension length, whether push outs were
called and whether there was a clock at all. The names never said the game
(Steven played 15-ball racks under ``wnt_9ball`` for its 60 s after the break),
and a new combination meant a new ruleset. So each of those is now its own
setting, plus the game being played, which no ruleset ever carried:

==============  =====================  ==========================================
setting         values                 what it decides
==============  =====================  ==========================================
``game``        8ball 9ball 10ball     which ball ends the rack (#72 reads it)
                other
``shot``        30 15                  the countdown for each shot
``after_break`` same 60                the countdown while fewer than two shots
                                       are counted this rack, and after a push
``extension``   15 30                  seconds an extension adds
``push``        off on                 whether the deck shows the PUSH key
``clock``       on off                 off = no shot clock at all (was
                                       ``freestyle``)
==============  =====================  ==========================================

The value lists are data: a 45 s shot clock is one more entry in
``OPTIONS["shot"]``, never a new type. Values are strings so one URL shape
(``POST /api/setting/{name}/{value}``) and one JSON file cover all six.
``after_break`` is "same" or a number rather than a number, so the late-match
clock (Ultimate Pool's last ten minutes at 15 s) stays one change: SHOT to 15.

Where the numbers come from (sources in ``CHANGELOG.md``, 2026-07-27 and
2026-07-31): **Ultimate Pool** singles is 30 s a shot, one 15 s extension per
player per frame, 15 s late in the match, no first-shot allowance. **WNT**
(Matchroom World Nineball Tour, Event Rules §5) is 30 s a shot, one 30 s
extension per player per rack, and 60 s for the first shot after the
break-off or a push out. The break itself is untimed in both.

Grace and post-expiry are app behaviour, not rulebook values, and are the
same for every setting.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields, replace

# The options for each setting, in the order the deck cycles them. The first is
# the default: an 8-ball match on Ultimate Pool's clock, what `cueclock run`
# started on before settings were remembered.
OPTIONS: dict[str, tuple[str, ...]] = {
    "game": ("8ball", "9ball", "10ball", "other"),
    "shot": ("30", "15"),
    "after_break": ("same", "60"),
    "extension": ("15", "30"),
    "push": ("off", "on"),
    "clock": ("on", "off"),
}

# Extension pools are data, not law, while this is False: `extension()` grants
# past `EXTENSIONS_PER_RACK` and the snapshot reports the pool as 0 so no
# surface draws a count. Off since 2026-08-13 — the verbal extension can be
# triggered by table-side conversation, and against an unlimited pool a false
# grant costs nothing instead of burning a real one mid-rack. Flip back on
# when the microphone earns enforcement.
ENFORCE_EXTENSION_LIMIT = False

# One extension per *player* per rack in both rulebooks, and the clock has no
# turn tracking, so the pool is the whole rack's: two, self-policed.
EXTENSIONS_PER_RACK = 2

# How long after the buzzer at zero a detected shot still saves the countdown
# (#68, checkpoint 134). The buzzer sounds the instant the clock reads 0, and a
# stroke made just before it is not confirmed until later: the motion latch
# rises ~0.25 s after the stroke and the strike confirm lands a median 0.02 s
# after the latch (p99 0.22 s, max 1.18 s; 872 strikes over every journal to
# 2026-11-17). So stroke-to-confirm is ~0.5 s at p99, and 1 s is about twice
# that. It was 2 s, which also let a stroke made ~1.5 s after zero count.
GRACE_SECONDS = 1.0

# How long EXPIRED flashes before the clock restarts itself for the next shot.
POST_EXPIRY_SECONDS = 2.0


@dataclass(frozen=True)
class Settings:
    """The six values, each one of its ``OPTIONS``. Immutable: change with ``with_``."""

    game: str = OPTIONS["game"][0]
    shot: str = OPTIONS["shot"][0]
    after_break: str = OPTIONS["after_break"][0]
    extension: str = OPTIONS["extension"][0]
    push: str = OPTIONS["push"][0]
    clock: str = OPTIONS["clock"][0]

    def __post_init__(self) -> None:
        for name, value in self.as_dict().items():
            if value not in OPTIONS[name]:
                raise ValueError(f"{name} cannot be {value!r}")

    @property
    def base_seconds(self) -> int:
        """The countdown for an ordinary shot."""
        return int(self.shot)

    @property
    def first_shot_seconds(self) -> int:
        """The countdown after the break or a push out (WNT §5's 60 s)."""
        return self.base_seconds if self.after_break == "same" else int(self.after_break)

    @property
    def extension_seconds(self) -> int:
        return int(self.extension)

    @property
    def timed(self) -> bool:
        """False: no shot clock. The countdown never runs, vision keeps
        journaling but can never move the clock, and every clock surface
        (wall, deck, phone) drops its countdown controls."""
        return self.clock == "on"

    def with_(self, name: str, value: str) -> Settings:
        """A copy with one setting changed. KeyError for an unknown setting,
        ValueError for a value it does not offer."""
        if name not in OPTIONS:
            raise KeyError(name)
        return replace(self, **{name: value})

    def as_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: object) -> Settings:
        """Read a saved or logged set, tolerating what it cannot use.

        A setting that is missing, or holds a value no longer offered, falls
        back to its default rather than failing: the file is written by this
        code, but a removed option must not stop a session from starting.
        """
        if not isinstance(data, dict):
            return cls()
        values = {
            f.name: data[f.name]
            for f in fields(cls)
            if isinstance(data.get(f.name), str) and data[f.name] in OPTIONS[f.name]
        }
        return cls(**values)


# ---------- the old rulesets, for logs written before checkpoint 145 ----------

# A session log from before the settings records `set_ruleset` inputs and names
# a ruleset in its header, and `cueclock session replay` must reproduce it. These
# are those rulesets as settings. Nothing live sets them any more: the deck and
# the API speak settings. None of them names a game, so applying one keeps the
# game that is set.
LEGACY_RULESETS: dict[str, Settings] = {
    "ultimate_pool_8ball": Settings(shot="30", after_break="same", extension="15", push="off"),
    "ultimate_pool_8ball_final10": Settings(shot="15", after_break="same", extension="15"),
    "wnt_9ball": Settings(shot="30", after_break="60", extension="30", push="on"),
    "freestyle": Settings(clock="off"),
}
LEGACY_ALIASES: dict[str, str] = {
    "ultimate_9ball": "wnt_9ball",
    "matchroom_9ball": "wnt_9ball",
}


def legacy_ruleset(name: str, game: str = OPTIONS["game"][0]) -> Settings:
    """A pre-settings ruleset name as settings, keeping ``game``. KeyError if unknown."""
    return replace(LEGACY_RULESETS[LEGACY_ALIASES.get(name, name)], game=game)


def legacy_name(name: str) -> str:
    """The canonical name a logged ruleset was journaled under."""
    return LEGACY_ALIASES.get(name, name)
