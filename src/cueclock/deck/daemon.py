"""Always-on Stream Deck daemon: renders key faces, dispatches presses.

All HID I/O lives here (StreamDeck imports are function-local so this module
imports everywhere); `layout`/`faces`/`client` stay pure and unit-tested. The
physical deck is exercised live, like the camera. Production runs under
launchd — `scripts/install_deck_agent.sh`; `cueclock deck` is the same loop
in a terminal.
"""

from __future__ import annotations

import contextlib
import queue
import time
from typing import Any

from PIL import Image

from cueclock.deck import client, faces
from cueclock.deck.layout import LAYOUT, ApiPost, Key, RulesetCycle, next_ruleset, ruleset_face

POLL_S = 2.0  # server/ruleset poll cadence
FLASH_S = 1.2  # how long a press outcome stays on the key
RETRY_S = 3.0  # device reconnect cadence

_BY_INDEX: dict[int, Key] = {key.index: key for key in LAYOUT}


def _face_for(key: Key, up: bool, active: str | None) -> Image.Image:
    if isinstance(key.action, RulesetCycle) and up and active is not None:
        return faces.render(ruleset_face(active))
    return faces.render(key.face) if up else faces.dimmed(key.face)


def _dispatch(key: Key, base: str, active: str | None) -> tuple[Image.Image, str | None]:
    """Run a key's action against the server; return (flash face, ruleset change|None)."""
    action = key.action
    if isinstance(action, ApiPost):
        result = client.post(base, action.path)
        print(
            f"{key.name}: POST {action.path} -> {result.status} {result.detail[:200]}",
            flush=True,
        )
        if result.ok:
            return faces.flash_ok(action.ok_face), None
        return faces.flash_err(action.err_face), None
    listing = client.active_rulesets(base)
    if listing is None:
        return faces.flash_err("ERROR"), None
    target = next_ruleset(*listing)
    result = client.post(base, f"/api/ruleset/{target}")
    print(f"{key.name}: -> {target} ({result.status})", flush=True)
    if result.ok:
        return faces.flash_ok(ruleset_face(target)), target
    return faces.flash_err("ERROR"), None


def _poll(base: str) -> tuple[bool, str | None]:
    """(server up, active ruleset) in one request."""
    listing = client.active_rulesets(base)
    return (True, listing[0]) if listing is not None else (False, None)


def _set_key(deck: Any, index: int, image: Image.Image) -> None:
    from StreamDeck.ImageHelpers import PILHelper

    size = tuple(deck.key_image_format()["size"])
    if image.size != size:
        image = image.resize(size)
    with deck:
        deck.set_key_image(index, PILHelper.to_native_key_format(deck, image))


def _draw_all(deck: Any, up: bool, active: str | None, skip: frozenset[int] = frozenset()) -> None:
    blank = faces.render("")
    for i in range(deck.key_count()):
        if i in skip:
            continue
        key = _BY_INDEX.get(i)
        _set_key(deck, i, _face_for(key, up, active) if key else blank)


def _loop(deck: Any, base: str) -> None:
    """Poll + dispatch forever; raises TransportError on unplug, KeyboardInterrupt on stop."""
    events: queue.Queue[tuple[int, bool]] = queue.Queue()
    deck.set_key_callback(lambda _deck, k, pressed: events.put((int(k), bool(pressed))))
    up, active = _poll(base)
    _draw_all(deck, up, active)
    flashes: dict[int, float] = {}
    next_poll = time.monotonic() + POLL_S
    while True:
        now = time.monotonic()
        if now >= next_poll:
            polled = _poll(base)
            if polled != (up, active):
                up, active = polled
                _draw_all(deck, up, active, skip=frozenset(flashes))
            next_poll = now + POLL_S
        for index, deadline in list(flashes.items()):
            if now >= deadline:
                del flashes[index]
                _set_key(deck, index, _face_for(_BY_INDEX[index], up, active))
        try:
            index, pressed = events.get(timeout=0.25)
        except queue.Empty:
            continue
        key = _BY_INDEX.get(index)
        if not pressed or key is None:
            continue
        if not up:
            print(f"{key.name}: ignored — server down", flush=True)
            continue
        image, changed = _dispatch(key, base, active)
        if changed is not None:
            active = changed
        _set_key(deck, index, image)
        flashes[index] = time.monotonic() + FLASH_S


def run_deck(base: str, brightness: int) -> int:
    """Open the deck (waiting for one if absent) and run the control loop forever.

    Returns only on SIGINT/SIGTERM (cli maps SIGTERM to KeyboardInterrupt, so
    `launchctl bootout` unwinds cleanly and blanks the keys).
    """
    from StreamDeck.DeviceManager import DeviceManager
    from StreamDeck.Transport.Transport import TransportError

    try:
        manager = DeviceManager()
    except TransportError as exc:  # missing libhidapi is fatal, not transient
        print(f"HID transport unavailable ({exc}) — `brew install hidapi`?", flush=True)
        return 1

    while True:
        try:
            devices = manager.enumerate()
            if not devices:
                print(f"no Stream Deck found; retrying in {RETRY_S:.0f} s", flush=True)
                time.sleep(RETRY_S)
                continue
            deck = devices[0]
            deck.open()
            try:
                deck.reset()
                deck.set_brightness(brightness)
                print(f"deck open: {deck.deck_type()} ({deck.key_count()} keys)", flush=True)
                _loop(deck, base)
            finally:
                with contextlib.suppress(Exception):
                    deck.reset()  # blank the keys on the way out
                    deck.close()
        except TransportError:
            print(f"deck I/O failed (unplugged?); retrying in {RETRY_S:.0f} s", flush=True)
            time.sleep(RETRY_S)
        except KeyboardInterrupt:
            print("deck daemon stopped", flush=True)
            return 0
