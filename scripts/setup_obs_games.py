#!/usr/bin/env python3
"""Build the OBS Games scene over obs-websocket. Idempotent — re-run any time.

    uv run python scripts/setup_obs_games.py [--server http://localhost:8000]

Creates (or repairs) the two things the games scene needs, so none of it has to
be assembled by hand in the OBS UI (issue #46):

  * a **Games** scene, sized to the canvas
  * the **existing** `Syphon Client` source, added to it and fitted into the
    space above the HUD bar — the same input the play scene uses, not a second
    one. One input means `obsws.bind_syphon` (the Session key's startup rebind)
    keeps both scenes live with one call, and the colour grading filters on it
    apply to both. The **transform** is per scene item, so fitting it here
    leaves the play scene's own full-bleed framing alone.
  * **Games Page** — a browser source on `/games`, filling the canvas

The 1920x1080 authored page is an exact 2x fit on the 4K canvas, which is why
the scene is these two sources stacked and nothing else: no side panel, no
side-angle camera. Judging is overhead, and a panel would only shrink the table
to make room for chrome (mockups v1 §7).

The feed is fitted **above the bar** rather than full-bleed. The bar is opaque,
and the rectified feed is 1.84:1 against a 1.78:1 canvas, so full-bleed left the
bottom rail and its two pockets under the plinth — on a game judged on where
balls stop, exactly the wrong 12% to lose. Fitted, the table is pillarboxed
instead: smaller, and all of it visible.

Two settings are load-bearing:

  ``restart_when_active: true``  as the bracket page has, and for the same
      reason: this page has no timeline to preserve and no pre-cut load, so
      reloading it at the cut is the cheapest way to heal a source that first
      loaded while the server was down (§B3).

  the page is created **after** the feed, so it lands on top of it. The page is
      transparent everywhere but its bottom bar; underneath it, the marks the
      games layer draws on the cloth arrive inside the Syphon frame itself.

Prerequisites: OBS running, its WebSocket server enabled, and credentials in
`config/obs_websocket.json` (`docs/hardware.md` §C4).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cueclock.deck import obsws
from cueclock.deck.games import HUD_BAR_PX, HUD_PAGE_H, SCENE_NAME

PAGE_INPUT = "Games Page"
CANVAS_W = 3840
CANVAS_H = 2160
# The canvas height left over above the HUD bar, in canvas pixels.
FEED_H = CANVAS_H - round(CANVAS_H * HUD_BAR_PX / HUD_PAGE_H)


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


def scene_item(source: str) -> int | None:
    """This scene's item id for `source`, or None if it is not in the scene."""
    data = obsws.request("GetSceneItemId", {"sceneName": SCENE_NAME, "sourceName": source})
    if data is None:
        return None
    item = data.get("sceneItemId")
    return item if isinstance(item, int) else None


def ensure_feed() -> bool:
    """Put the play scene's own Syphon source into this scene, above the bar."""
    if scene_item(obsws.SYPHON_SOURCE) is not None:
        print(f"item {obsws.SYPHON_SOURCE!r}: already in {SCENE_NAME!r}")
        return fit(obsws.SYPHON_SOURCE, CANVAS_W, FEED_H)
    inputs = _names("GetInputList", "inputs", "inputName")
    if inputs is None:
        return False
    if obsws.SYPHON_SOURCE not in inputs:
        print(
            f"no {obsws.SYPHON_SOURCE!r} source in OBS — set the play scene up first "
            "(docs/hardware.md §B3)",
            file=sys.stderr,
        )
        return False
    created = obsws.request(
        "CreateSceneItem",
        {"sceneName": SCENE_NAME, "sourceName": obsws.SYPHON_SOURCE, "sceneItemEnabled": True},
    )
    if created is None:
        return False
    print(f"item {obsws.SYPHON_SOURCE!r}: added to {SCENE_NAME!r}")
    return fit(obsws.SYPHON_SOURCE, CANVAS_W, FEED_H)


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
        return fit(PAGE_INPUT, CANVAS_W, CANVAS_H)
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
    return fit(PAGE_INPUT, CANVAS_W, CANVAS_H)


def fit(source: str, width: int, height: int) -> bool:
    """Scale `source` to fit `width` x `height` from the canvas' top-left.

    SCALE_INNER with a centred bounds alignment: the source keeps its aspect
    and centres in whatever it does not fill, which is what pillarboxes the
    1.84:1 table feed inside the 2.09:1 strip above the HUD bar.
    """
    item = scene_item(source)
    if item is None:
        return False
    ok = obsws.request(
        "SetSceneItemTransform",
        {
            "sceneName": SCENE_NAME,
            "sceneItemId": item,
            "sceneItemTransform": {
                "positionX": 0.0,
                "positionY": 0.0,
                "alignment": 5,  # top-left
                "boundsType": "OBS_BOUNDS_SCALE_INNER",
                "boundsAlignment": 0,  # centred inside the bounds
                "boundsWidth": float(width),
                "boundsHeight": float(height),
            },
        },
    )
    if ok is None:
        return False
    print(f"item {source!r}: fitted to {width}x{height}")
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

    steps = (ensure_scene(), ensure_feed(), ensure_page(f"{args.server}/games"))
    if not all(steps):
        print("setup incomplete — is OBS running with its WebSocket server on?", file=sys.stderr)
        return 1

    print(
        f"\n{SCENE_NAME} scene ready. The Mode key's `games` step cuts to it and leaving"
        "\ngames mode cuts back — there is no scene key for it, because the mode IS the"
        "\nchoice. On-cloth markings need `cueclock run --rectify`."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
