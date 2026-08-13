"""The bracket's OBS half: putting the draw on the TV and taking it off again.

Everything the bracket *is* lives on the server (`server/bracket.py`); this is
only the scene switch, which has to be here because obs-websocket is the deck's
to talk to (`docs/hardware.md` §C3).

It remembers the scene it left rather than naming one, exactly as `replay.py`
does — the play scene is called `Stream Detail` on this rig and there is no
reason for that name to be compiled into two places.
"""

from __future__ import annotations

from dataclasses import dataclass

from cueclock.deck import obsws

# The scene `scripts/setup_obs_bracket.py` creates (docs/hardware.md §B3).
SCENE_NAME = "Bracket"


@dataclass(frozen=True)
class Showing:
    """The bracket is on the TV; `live_scene` is what to go back to."""

    live_scene: str


def show() -> Showing | None:
    """Cut to the bracket scene. None if OBS couldn't, leaving the TV alone.

    Already being on the bracket with no handle (a daemon restart mid-scene)
    is not a failure and not a reason to switch away: it reports the rig's
    default play scene as the way back, which is where the key would have gone
    anyway.
    """
    live_scene = obsws.current_scene()
    if live_scene is None:
        return None
    if live_scene == SCENE_NAME:
        return Showing(live_scene=DEFAULT_PLAY_SCENE)
    if not obsws.set_scene(SCENE_NAME):
        return None
    return Showing(live_scene=live_scene)


def hide(showing: Showing) -> bool:
    """Back to the scene that was live when the bracket went up."""
    return obsws.set_scene(showing.live_scene)


# Only used when the daemon finds itself already on the bracket scene with no
# memory of where it came from. Named here rather than in `obsws` because it is
# a fallback for one key, not a fact about the rig.
DEFAULT_PLAY_SCENE = "Stream Detail"
