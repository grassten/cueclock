#!/usr/bin/env python3
"""Build the OBS Replay scene over obs-websocket. Idempotent — re-run any time.

    uv run python scripts/setup_obs_replay.py [--server http://localhost:8000]

Creates (or repairs) four things the instant replay needs, so none of it has
to be assembled by hand in the OBS UI at the table:

  * a **Replay** scene, sized to the canvas
  * **Replay Media** — the saved buffer, muted, playing at 100%
  * **Replay Badge** — the `/replay` overlay: scrim, REPLAY wordmark, drain bar
  * a replay buffer long enough to hold a **rack** (`--buffer-seconds`)

The buffer length is the one setting the feature cannot work around: a held
Replay key plays the rack from its break, and 20 s of program output has no
rack in it. It is written into the active profile here rather than clicked into
Settings → Output, so the length the deck assumes and the length OBS keeps
cannot drift apart. **OBS reads it when the buffer starts**, so a change lands
at the next Session start, not immediately.

Three settings are load-bearing rather than cosmetic, and all three are the
opposite of the OBS default:

  ``restart_on_activate: false``  the seek happens *before* the scene switch, so
      a restart-on-activate would throw it away and play from the top — the
      twenty seconds of chalking the seek exists to skip.
  ``close_when_inactive: false``  the source has to hold the file open while the
      scene is dark, because that is when `GetMediaInputStatus` is asked for the
      duration the cue point is computed from.
  ``restart_when_active: false``  on the badge: the deck loads and reloads that
      page itself, before the cut, so the page is already painted when the TV
      sees it (`deck/replay.py`, `BADGE_PAINT_S`).

Prerequisites: OBS running, its WebSocket server enabled, and credentials in
`config/obs_websocket.json` (`docs/hardware.md` §C4).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cueclock.deck import obsws
from cueclock.deck.replay import BADGE_INPUT, BUFFER_TARGET_S, MEDIA_INPUT, SCENE_NAME, SPEED

CANVAS_W = 1920
CANVAS_H = 1080
TRANSITION = "Fade"

MB_PER_S = 0.66
"""Measured size of one second of program output (`replay.BUFFER_TARGET_S`).

Only used to check the *other* buffer limit: OBS caps the replay buffer by time
**and** by memory, whichever binds first, and the memory one is silent — a
buffer set to five minutes that only ever holds three looks like a bug in the
seek. So the size cap is reported in seconds, which is the unit the failure
would be noticed in."""


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


def _profile_param(category: str, name: str) -> str | None:
    data = obsws.request(
        "GetProfileParameter", {"parameterCategory": category, "parameterName": name}
    )
    if data is None:
        return None
    for field in ("parameterValue", "defaultParameterValue"):
        value = data.get(field)
        if isinstance(value, str) and value:
            return value
    return None


def ensure_buffer_seconds(seconds: int) -> bool:
    """Set the profile's replay-buffer length, and report what limits it.

    Which key holds it depends on the output mode, so the mode is asked for
    rather than assumed — writing `SimpleOutput/RecRBTime` on an Advanced
    profile would succeed, change nothing, and print that it worked.
    """
    mode = _profile_param("Output", "Mode")
    if mode is None:
        return False
    category = "SimpleOutput" if mode == "Simple" else "AdvOut"
    current = _profile_param(category, "RecRBTime")

    if current == str(seconds):
        print(f"replay buffer: already {seconds} s ({category})")
    else:
        set_ok = obsws.request(
            "SetProfileParameter",
            {
                "parameterCategory": category,
                "parameterName": "RecRBTime",
                "parameterValue": str(seconds),
            },
        )
        if set_ok is None:
            return False
        print(f"replay buffer: {current or '?'} s -> {seconds} s ({category})")
        print("  takes effect when the buffer next starts — i.e. the next Session start")

    # The memory cap is the limit nobody sets on purpose and everybody hits.
    size_mb = _profile_param(category, "RecRBSize")
    if size_mb and size_mb.isdigit() and int(size_mb) > 0:
        capped_s = int(size_mb) / MB_PER_S
        note = (
            "  binds first — raise it or the buffer is shorter than it says"
            if (capped_s < seconds)
            else "  clear of the time limit"
        )
        print(f"replay buffer: {size_mb} MB cap ≈ {capped_s:.0f} s at {MB_PER_S} MB/s")
        print(note)
    print(f"replay buffer: each save now writes ≈ {seconds * MB_PER_S:.0f} MB to the recording dir")
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
    parser.add_argument(
        "--buffer-seconds",
        type=int,
        default=int(BUFFER_TARGET_S),
        help="how much program output OBS keeps — a held Replay key needs a whole rack of it",
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
        # Deliberately off, and it is the third load-bearing inversion here.
        # A reload triggered by the cut takes ~0.57 s (measured), and OBS shows
        # the previous replay's last frame throughout — which is a fully
        # blacked-out one, since the badge fades to black on the way out. The
        # deck reloads it itself while the scene is still dark instead, and
        # waits for the paint before switching (`replay.BADGE_PAINT_S`).
        "restart_when_active": False,
        "shutdown": False,
    }

    steps = (
        ensure_buffer_seconds(args.buffer_seconds),
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
