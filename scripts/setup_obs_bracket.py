#!/usr/bin/env python3
"""Build the OBS Bracket scene over obs-websocket. Idempotent — re-run any time.

    uv run python scripts/setup_obs_bracket.py [--server http://localhost:8000]

Creates (or repairs) the two things the bracket scene needs, so none of it has
to be assembled by hand in the OBS UI on tournament night (issue #43):

  * a **Bracket** scene, sized to the canvas
  * **Bracket Page** — a browser source on `/bracket`, filling it

The page is authored at 1920x1080 and scales itself to the source, exactly as
the bar does (`fitStage()` in `bracket.html`), so the source is created at the
canvas size and the text rasterizes at the wall's own resolution rather than
being upscaled by OBS.

One setting is load-bearing:

  ``restart_when_active: true``  the opposite of the Replay badge's. This page
      has no timeline to preserve and no pre-cut load: it fetches the draw over
      HTTP and then sits on a WebSocket. Reloading it at the cut is the cheapest
      way to heal a source that first loaded while the server was down, which
      OBS otherwise parks on a silent error page forever (§B3).

Prerequisites: OBS running, its WebSocket server enabled, and credentials in
`config/obs_websocket.json` (`docs/hardware.md` §C4).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cueclock.deck import obsws
from cueclock.deck.bracket import SCENE_NAME

PAGE_INPUT = "Bracket Page"
CANVAS_W = 3840
CANVAS_H = 2160


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


def ensure_page(url: str) -> bool:
    """Create the browser source, or re-apply its settings if it is already there."""
    settings: dict[str, object] = {
        "url": url,
        "width": CANVAS_W,
        "height": CANVAS_H,
        "reroute_audio": False,
        "restart_when_active": True,  # see the module docstring
        "shutdown": False,
    }
    inputs = _names("GetInputList", "inputs", "inputName")
    if inputs is None:
        return False
    if PAGE_INPUT in inputs:
        if not obsws.set_input_settings(PAGE_INPUT, settings):
            return False
        print(f"input {PAGE_INPUT!r}: settings re-applied")
        return True
    created = obsws.request(
        "CreateInput",
        {
            "sceneName": SCENE_NAME,
            "inputName": PAGE_INPUT,
            "inputKind": "browser_source",
            "inputSettings": settings,
            "sceneItemEnabled": True,
        },
    )
    if created is None:
        return False
    print(f"input {PAGE_INPUT!r}: created (browser_source -> {url})")
    return True


def fill_canvas() -> bool:
    data = obsws.request("GetSceneItemId", {"sceneName": SCENE_NAME, "sourceName": PAGE_INPUT})
    if data is None:
        return False
    ok = obsws.request(
        "SetSceneItemTransform",
        {
            "sceneName": SCENE_NAME,
            "sceneItemId": data["sceneItemId"],
            "sceneItemTransform": {
                "positionX": 0.0,
                "positionY": 0.0,
                "alignment": 5,  # top-left
                "boundsType": "OBS_BOUNDS_SCALE_INNER",
                "boundsAlignment": 0,
                "boundsWidth": float(CANVAS_W),
                "boundsHeight": float(CANVAS_H),
            },
        },
    )
    if ok is None:
        return False
    print(f"item {PAGE_INPUT!r}: filling {CANVAS_W}x{CANVAS_H}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--server", default="http://localhost:8000", help="cueclock server base URL"
    )
    args = parser.parse_args()

    if obsws.load_config() is None:
        print("no OBS credentials — see docs/hardware.md §C4", file=sys.stderr)
        return 1

    steps = (ensure_scene(), ensure_page(f"{args.server}/bracket"), fill_canvas())
    if not all(steps):
        print("setup incomplete — is OBS running with its WebSocket server on?", file=sys.stderr)
        return 1

    print(
        f"\n{SCENE_NAME} scene ready. The deck's scene key (2,3) switches to it, and a"
        "\nconfirmed result flips to it by itself — both only while a bracket is loaded."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
