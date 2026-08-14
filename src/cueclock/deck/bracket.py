"""The bracket's OBS half: putting the draw on the TV and taking it off again.

Everything the bracket *is* lives on the server (`server/bracket.py`); this is
only the scene switch, which has to be here because obs-websocket is the deck's
to talk to (`docs/hardware.md` §C3).

It remembers the scene it left rather than naming one, exactly as `replay.py`
does — the play scene is called `Stream Detail` on this rig and there is no
reason for that name to be compiled into two places.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from cueclock.deck import obsws

# The scene `scripts/setup_obs_bracket.py` creates (docs/hardware.md §B3).
SCENE_NAME = "Bracket"

# How long a confirmed result leaves the bracket on the wall before the TV
# returns on its own. **The flip out was automatic and the flip back was a human
# remembering a key**, which is a wall that can sit on the bracket through a
# live match — and the bracket scene has no shot clock and no table on it, so
# that is the one thing this rig exists to show, missing exactly when play
# resumes. Thirty seconds is long enough for a room to read who advanced and who
# is up next, and still shorter than any rack: the TV is back watching them rack
# before anybody breaks.
#
# **The result that ends the tournament gets no dwell at all** — see
# `daemon._ends_the_tournament`. There is no next match to rack for, and a
# champion who is swept off the wall on a timer is the opposite of the point.
DWELL_S = 30.0


@dataclass(frozen=True)
class Showing:
    """The bracket is on the TV; `live_scene` is what to go back to.

    ``until`` is the monotonic deadline for the automatic return, as
    ``replay.Playing`` uses — or ``None`` for "stay until a human says
    otherwise", which is what the scene key means and a timer must not
    second-guess.
    """

    live_scene: str
    until: float | None = None


def show(dwell_s: float | None = None) -> Showing | None:
    """Cut to the bracket scene. None if OBS couldn't, leaving the TV alone.

    With ``dwell_s`` the return is on a timer (the confirm key); without one it
    stays up until the scene key takes it down.

    Already being on the bracket with no handle (a daemon restart mid-scene)
    is not a failure and not a reason to switch away: it reports the rig's
    default play scene as the way back, which is where the key would have gone
    anyway.
    """
    until = None if dwell_s is None else time.monotonic() + dwell_s
    live_scene = obsws.current_scene()
    if live_scene is None:
        return None
    if live_scene == SCENE_NAME:
        return Showing(live_scene=DEFAULT_PLAY_SCENE, until=until)
    if not obsws.set_scene(SCENE_NAME):
        return None
    return Showing(live_scene=live_scene, until=until)


def hide(showing: Showing) -> bool:
    """Back to the scene that was live when the bracket went up."""
    return obsws.set_scene(showing.live_scene)


# Only used when the daemon finds itself already on the bracket scene with no
# memory of where it came from. Named here rather than in `obsws` because it is
# a fallback for one key, not a fact about the rig.
DEFAULT_PLAY_SCENE = "Stream Detail"
