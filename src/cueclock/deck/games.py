"""The Games scene's OBS half: putting the games wall up and taking it down.

Everything a minigame *is* lives on the server (`server/games.py`); this is
only the scene switch, which has to be here because obs-websocket is the deck's
to talk to (`docs/hardware.md` §C3). Same shape as `deck/bracket.py`, and for
the same reason it remembers the scene it left rather than naming one — the
play scene is called `Stream Detail` on this rig and there is no reason for
that name to be compiled into three places.

The one difference from the bracket's: **no dwell.** The bracket is a thing you
glance at between matches, so its automatic flip is on a timer. Games mode is a
thing you are *in* — the scene follows the Mode key exactly, up when games mode
is entered and down when it is left, and a timer must not second-guess that.
"""

from __future__ import annotations

from dataclasses import dataclass

from cueclock.deck import obsws
from cueclock.deck.bracket import DEFAULT_PLAY_SCENE

# The scene `scripts/setup_obs_games.py` creates (docs/hardware.md §B3).
SCENE_NAME = "Games"

# The HUD page is authored at 1920x1080 and its bar is the bottom `HUD_BAR_PX`
# of that — `#bar { height: 160px }` in `server/static/games.html`, which a unit
# test holds this to. The setup script needs it because the table feed is fitted
# into the space **above** the bar rather than full-bleed: the bar is opaque, so
# full-bleed put a solid plinth across the bottom rail and its two pockets.
# Sitting them side by side costs ~12% of the table's width on the wall and buys
# back the whole table, which is the trade for a game judged on where balls stop.
HUD_PAGE_W = 1920
HUD_PAGE_H = 1080
HUD_BAR_PX = 160


@dataclass(frozen=True)
class Showing:
    """The games scene is on the TV; `live_scene` is what to go back to."""

    live_scene: str


def show() -> Showing | None:
    """Cut to the Games scene. None if OBS couldn't, leaving the TV alone.

    Already being on it with no handle (a daemon restart mid-scene) is not a
    failure and not a reason to switch away: it reports the rig's default play
    scene as the way back, which is where leaving games mode would have gone.
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
    """Back to the scene that was live when games mode was entered."""
    return obsws.set_scene(showing.live_scene)
