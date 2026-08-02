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
from cueclock.deck.layout import (
    LAYOUT,
    ApiPost,
    Key,
    RulesetCycle,
    SessionToggle,
    next_ruleset,
    ruleset_face,
)
from cueclock.deck.session import SessionManager, SessionState

POLL_S = 2.0  # server/ruleset poll cadence
FLASH_S = 1.2  # how long a press outcome stays on the key
RETRY_S = 3.0  # device reconnect cadence
HOLD_FACE = "HOLD…"  # shown while the Session key is held toward a stop

_BY_INDEX: dict[int, Key] = {key.index: key for key in LAYOUT}


def _face_for(key: Key, up: bool, active: str | None, state: SessionState) -> Image.Image:
    # The Session key is the one key that stays lit with the server down — it
    # is what starts the server, so dimming it would hide the way back up.
    if isinstance(key.action, SessionToggle):
        return faces.render(state.face)
    if isinstance(key.action, RulesetCycle) and up and active is not None:
        return faces.render(ruleset_face(active))
    face = key.face_for(active)  # ruleset-aware keys (Extension) track the poll
    return faces.render(face) if up else faces.dimmed(face)


def _dispatch(key: Key, base: str, active: str | None) -> tuple[Image.Image, str | None]:
    """Run a key's action against the server; return (flash face, ruleset change|None)."""
    action = key.action
    if isinstance(action, ApiPost):
        result = client.post(base, action.path, timeout=action.timeout)
        print(
            f"{key.name}: POST {action.path} -> {result.status} {result.detail[:200]}",
            flush=True,
        )
        if result.ok:
            return faces.flash_ok(action.ok_face_for(active)), None
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


def _draw_all(
    deck: Any,
    up: bool,
    active: str | None,
    state: SessionState,
    skip: frozenset[int] = frozenset(),
) -> None:
    blank = faces.render("")
    for i in range(deck.key_count()):
        if i in skip:
            continue
        key = _BY_INDEX.get(i)
        _set_key(deck, i, _face_for(key, up, active, state) if key else blank)


def _loop(deck: Any, base: str, session: SessionManager) -> None:
    """Poll + dispatch forever; raises TransportError on unplug, KeyboardInterrupt on stop."""
    events: queue.Queue[tuple[int, bool]] = queue.Queue()
    deck.set_key_callback(lambda _deck, k, pressed: events.put((int(k), bool(pressed))))
    up, active = _poll(base)
    state = session.tick(up)
    _draw_all(deck, up, active, state)
    flashes: dict[int, float] = {}
    holds: dict[int, float] = {}  # Session key index -> monotonic stop deadline
    next_poll = time.monotonic() + POLL_S
    while True:
        now = time.monotonic()
        if now >= next_poll:
            polled = _poll(base)
            # The session machine advances on this same poll: STARTING becomes
            # ON the moment the server answers (that is when OBS is launched),
            # and STOPPING becomes OFF when `run` finally exits.
            polled_state = session.tick(polled[0])
            if (polled, polled_state) != ((up, active), state):
                up, active = polled
                state = polled_state
                _draw_all(deck, up, active, state, skip=frozenset(flashes))
            next_poll = now + POLL_S
        for index, deadline in list(holds.items()):
            if now >= deadline:
                del holds[index]
                session.stop()
                state = session.tick(up)
                _set_key(deck, index, _face_for(_BY_INDEX[index], up, active, state))
        for index, deadline in list(flashes.items()):
            if now >= deadline:
                del flashes[index]
                _set_key(deck, index, _face_for(_BY_INDEX[index], up, active, state))
        try:
            index, pressed = events.get(timeout=0.25)
        except queue.Empty:
            continue
        key = _BY_INDEX.get(index)
        if key is None:
            continue
        if isinstance(key.action, SessionToggle):
            if not pressed:
                # Released before the hold matured: a tap while ON means
                # nothing, so put the honest state back.
                if holds.pop(index, None) is not None:
                    _set_key(deck, index, _face_for(key, up, active, state))
            elif state is SessionState.ON:
                holds[index] = time.monotonic() + key.action.hold_s
                _set_key(deck, index, faces.render(HOLD_FACE))
            elif state is SessionState.OFF:
                session.start()
                state = session.tick(up)
                _set_key(deck, index, _face_for(key, up, active, state))
            else:  # DEV (not ours to touch) or mid-transition — refuse, say why
                print(f"session: press ignored in {state.name}", flush=True)
                _set_key(deck, index, faces.flash_err(state.face))
                flashes[index] = time.monotonic() + FLASH_S
            continue
        if not pressed:
            continue
        if not up:
            print(f"{key.name}: ignored — server down", flush=True)
            continue
        if isinstance(key.action, ApiPost) and key.action.pending_face is not None:
            _set_key(deck, index, faces.render(key.action.pending_face))
        image, changed = _dispatch(key, base, active)
        if changed is not None:
            active = changed
            # Ruleset-aware faces must follow right away — the next poll can't
            # catch this change because `active` already moved.
            _draw_all(deck, up, active, state, skip=frozenset(flashes) | {index})
        _set_key(deck, index, image)
        flashes[index] = time.monotonic() + FLASH_S


def run_deck(base: str, brightness: int, reference: str) -> int:
    """Open the deck (waiting for one if absent) and run the control loop forever.

    Returns only on SIGINT/SIGTERM (cli maps SIGTERM to KeyboardInterrupt, so
    `launchctl bootout` unwinds cleanly and blanks the keys). A session this
    daemon spawned deliberately **survives** its exit — a launchd restart or a
    layout reinstall must never kill a live practice session. The running
    session then reads DEV (no handle to it any more), so stop it from a
    terminal or leave it up.
    """
    from StreamDeck.DeviceManager import DeviceManager
    from StreamDeck.Transport.Transport import TransportError

    try:
        manager = DeviceManager()
    except TransportError as exc:  # missing libhidapi is fatal, not transient
        print(f"HID transport unavailable ({exc}) — `brew install hidapi`?", flush=True)
        return 1

    session = SessionManager(reference=reference)

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
                _loop(deck, base, session)
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
