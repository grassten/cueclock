#!/usr/bin/env python3
"""Build the OBS Replay scene over obs-websocket. Idempotent — re-run any time.

    uv run python scripts/setup_obs_replay.py [--server http://localhost:8000]

Creates (or repairs) three things the instant replay needs, so none of it has
to be assembled by hand in the OBS UI at the table:

  * a **Replay** scene, sized to the canvas
  * **Replay Media** — the saved buffer, muted, playing at 100%
  * **Replay Badge** — the `/replay` overlay: scrim, REPLAY wordmark, drain bar

Two of the media source's settings are load-bearing rather than cosmetic, and
both are the opposite of the OBS default:

  ``restart_on_activate: false``  the seek happens *before* the scene switch, so
      a restart-on-activate would throw it away and play from the top — the
      twenty seconds of chalking the seek exists to skip.
  ``close_when_inactive: false``  the source has to hold the file open while the
      scene is dark, because that is when `GetMediaInputStatus` is asked for the
      duration the cue point is computed from.

Prerequisites: OBS running, its WebSocket server enabled, and credentials in
`config/obs_websocket.json` (`docs/hardware.md` §C4).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cueclock.deck import obsws
from cueclock.deck.replay import BADGE_INPUT, MEDIA_INPUT, SCENE_NAME, SPEED

CANVAS_W = 1920
CANVAS_H = 1080
TRANSITION = "Fade"


def _names(request: str, key: str, field: str) -> list[str] | None:
    data = obsws.request(request)
    if data is None:
        return None
    items = data.get(key)
    if not isinstance(items, list):
        return []
    return [item[field] for item in items if isinstance(item, dict) and field in item]


def ensure_scene() -> bool:
    scenes = _names("GetSceneList", "scenes", "sceneName")
    if scenes is None:
        return False
    if SCENE_NAME in scenes:
        print(f"scene {SCENE_NAME!r}: already there")
        return True
    if obsws.request("CreateScene", {"sceneName": SCENE_NAME}) is None:
        return False
    print(f"scene {SCENE_NAME!r}: created")
    return True


def ensure_input(name: str, kind: str, settings: dict[str, object]) -> bool:
    """Create the input in the Replay scene, or re-apply its settings if it exists.

    Re-applying matters as much as creating: a hand-edit in the OBS UI that
    flips `restart_on_activate` back on would break every replay silently, and
    re-running this script is then the fix.
    """
    inputs = _names("GetInputList", "inputs", "inputName")
    if inputs is None:
        return False
    if name in inputs:
        if not obsws.set_input_settings(name, settings):
            return False
        print(f"input {name!r}: settings re-applied")
        return True
    created = obsws.request(
        "CreateInput",
        {
            "sceneName": SCENE_NAME,
            "inputName": name,
            "inputKind": kind,
            "inputSettings": settings,
            "sceneItemEnabled": True,
        },
    )
    if created is None:
        return False
    print(f"input {name!r}: created ({kind})")
    return True


def fill_canvas(name: str) -> bool:
    """Stretch a scene item to the whole canvas, preserving its aspect."""
    data = obsws.request("GetSceneItemId", {"sceneName": SCENE_NAME, "sourceName": name})
    if data is None:
        return False
    transform = {
        "positionX": 0.0,
        "positionY": 0.0,
        "alignment": 5,  # top-left
        "boundsType": "OBS_BOUNDS_SCALE_INNER",
        "boundsAlignment": 0,  # centered inside the bounds
        "boundsWidth": float(CANVAS_W),
        "boundsHeight": float(CANVAS_H),
    }
    ok = obsws.request(
        "SetSceneItemTransform",
        {
            "sceneName": SCENE_NAME,
            "sceneItemId": data["sceneItemId"],
            "sceneItemTransform": transform,
        },
    )
    if ok is None:
        return False
    print(f"item {name!r}: filling {CANVAS_W}x{CANVAS_H}")
    return True


def check_transition() -> None:
    """Report the transition rather than setting it — it is global to OBS."""
    data = obsws.request("GetCurrentSceneTransition")
    if data is None:
        return
    name, ms = data.get("transitionName"), data.get("transitionDuration")
    if name == TRANSITION:
        print(f"transition: {name} {ms} ms")
    else:
        print(f"transition: {name!r} — set it to {TRANSITION} in OBS if you want a soft cut")


def check_replay_buffer() -> None:
    data = obsws.request("GetReplayBufferStatus")
    if data is None:
        return
    if not data.get("outputActive"):
        print("replay buffer: NOT RUNNING — the Session key arms it at launch")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--server", default="http://localhost:8000", help="cueclock server base URL"
    )
    args = parser.parse_args()

    if obsws.load_config() is None:
        print("no OBS credentials — see docs/hardware.md §C4", file=sys.stderr)
        return 1

    media_settings: dict[str, object] = {
        "is_local_file": True,
        "local_file": "",  # every press rewrites this with the file just saved
        "looping": False,
        "restart_on_activate": False,  # would discard the seek — see the docstring
        "close_when_inactive": False,  # duration is read while the scene is dark
        "clear_on_media_end": True,
        "hw_decode": True,
        "speed_percent": round(SPEED * 100),
    }
    badge_settings: dict[str, object] = {
        "url": f"{args.server}/replay",
        "width": CANVAS_W,
        "height": CANVAS_H,
        "reroute_audio": False,
        "restart_when_active": True,  # re-runs the drain bar on every replay
        "shutdown": False,
    }

    steps = (
        ensure_scene(),
        ensure_input(MEDIA_INPUT, "ffmpeg_source", media_settings),
        ensure_input(BADGE_INPUT, "browser_source", badge_settings),
        fill_canvas(MEDIA_INPUT),
        fill_canvas(BADGE_INPUT),
        # Muted because the buffer has the shot-clock chimes baked in: an
        # unmuted replay beeps its old countdown over the live one.
        obsws.request("SetInputMute", {"inputName": MEDIA_INPUT, "inputMuted": True}) is not None,
    )
    if not all(steps):
        print("setup incomplete — is OBS running with its WebSocket server on?", file=sys.stderr)
        return 1

    check_transition()
    check_replay_buffer()
    print(f"\nReplay scene ready. Press Replay on the deck (2,1); {SCENE_NAME} is never manual.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
