"""Always-on Stream Deck daemon: renders key faces, dispatches presses.

All HID I/O lives here (StreamDeck imports are function-local so this module
imports everywhere); `layout`/`faces`/`client` stay pure and unit-tested. The
physical deck is exercised live, like the camera. Production runs under
launchd — `scripts/install_deck_agent.sh`; `cueclock deck` is the same loop
in a terminal.

The loop polls `GET /api/state` once every `POLL_S` and rebuilds a `DeckState`
from it — one request carrying clock state, mode, ruleset, the countdown a
Start press would hand you, the extension pool and the score, so every face is
drawn from the same snapshot and none of them can drift apart.
"""

from __future__ import annotations

import contextlib
import json
import queue
import time
from dataclasses import replace
from typing import Any

from PIL import Image

from cueclock.deck import bracket, client, faces, replay
from cueclock.deck.layout import (
    CONFIRM_PENDING_FACE,
    DUMP_PENDING_FACE,
    FLAG_PENDING_FACE,
    LAYOUT,
    RACK_PENDING_FACE,
    REASON_KEYS,
    REPLAY_PENDING_FACE,
    ApiPost,
    BracketConfirm,
    Cycle,
    DeckState,
    FlagReason,
    FlagStart,
    Key,
    ReplayShow,
    SceneToggle,
    ScoreAdjust,
    SessionToggle,
    cycle_listing,
    cycle_next,
    cycle_path,
)
from cueclock.deck.session import SessionManager, SessionState

POLL_S = 2.0  # server/state poll cadence
FLASH_S = 1.2  # how long a press outcome stays on the key
RETRY_S = 3.0  # device reconnect cadence
CONFIRM_S = 5.0  # a bracket confirm writes a result, loads a match and racks
HOLD_FACE = "HOLD…"  # shown while the Session key is held toward a stop

_BY_INDEX: dict[int, Key] = {key.index: key for key in LAYOUT}
_REASONS_BY_INDEX: dict[int, Key] = {key.index: key for key in REASON_KEYS}

_FLAG_KEY: Key = next(k for k in LAYOUT if isinstance(k.action, FlagStart))
# What an unanswered reason page files itself as, resolved once at import so a
# renamed reason key can't leave the auto-file path pointing at nothing.
_AUTO_REASON: Key = next(
    k
    for k in REASON_KEYS
    if isinstance(_FLAG_KEY.action, FlagStart) and k.name == _FLAG_KEY.action.auto_reason
)


def _score_of(payload: object, slot: str) -> int:
    """One player's score out of the state payload's nested `score` object."""
    if not isinstance(payload, dict):
        return 0
    entry = payload.get(slot)
    if not isinstance(entry, dict):
        return 0
    value = entry.get("score")
    return value if isinstance(value, int) else 0


def _bracket_fields(payload: object) -> dict[str, object]:
    """The bracket half of a `GET /api/state` body, flattened for `DeckState`.

    `null` (casual play) leaves every field at its default, which is what makes
    both new keys dark and the deck exactly the deck it was before #43.
    """
    if not isinstance(payload, dict):
        return {}
    match = payload.get("match")
    fields: dict[str, object] = {"bracket_loaded": True}
    if isinstance(match, dict):
        fields["bracket_match_id"] = match.get("id")
        fields["bracket_match"] = f"MATCH {match.get('id')}"
        side = match.get("side")
        fields["bracket_side"] = f"{side} SIDE" if side in ("A", "B") else "FINAL"
    armed = payload.get("armed")
    if isinstance(armed, dict):
        fields["bracket_armed"] = (
            str(armed.get("name", "")),
            int(armed.get("score", 0)),
            int(armed.get("other", 0)),
        )
    return fields


def build_state(
    data: dict[str, object] | None,
    session: SessionState,
    *,
    replaying: bool = False,
    on_bracket: bool = False,
) -> DeckState:
    """Turn one `GET /api/state` body into everything the faces need.

    Server down is a `DeckState` too, not an exception: every key but Session
    goes dark, and the Session key still says how to bring the server back.

    ``replaying`` and ``on_bracket`` are the face inputs the server knows
    nothing about — both live entirely between the deck and OBS — so the loop
    passes its own answers in rather than inventing a second source of state.
    """
    if data is None:
        return DeckState(
            up=False,
            replaying=replaying,
            on_bracket=on_bracket,
            session_face=session.face,
            session_sub=session.sub,
        )

    def _int(field: str, default: int) -> int:
        value = data.get(field)
        return value if isinstance(value, int) else default

    def _str(field: str) -> str | None:
        value = data.get(field)
        return value if isinstance(value, str) else None

    return DeckState(
        up=True,
        mode=_str("mode"),
        ruleset=_str("ruleset"),
        base=_int("base", 30),
        extensions_used=_int("extensions_used", 0),
        extensions_allowed=_int("extensions_allowed", 0),
        p1=_score_of(data.get("score"), "p1"),
        p2=_score_of(data.get("score"), "p2"),
        replaying=replaying,
        on_bracket=on_bracket,
        session_face=session.face,
        session_sub=session.sub,
        # The session is what the health dot reports on: green once the server
        # is answering, absent otherwise (there is nothing to be healthy).
        session_dot=faces.DOT_OK,
        **_bracket_fields(data.get("bracket")),  # type: ignore[arg-type]
    )


def _dispatch(key: Key, base: str, state: DeckState) -> tuple[Image.Image, DeckState | None]:
    """Run a key's action; return (flash face, an eagerly-updated state | None).

    The second element exists so a cycle key's own face — and every face that
    depends on the axis it just moved — can be redrawn before the next poll,
    which would otherwise report the value we already know we changed.
    """
    action = key.action
    if isinstance(action, FlagReason):
        result = client.post(base, f"/api/dump?label={action.label}", timeout=action.timeout)
        print(f"{key.name}: dump {action.label} -> {result.status}", flush=True)
        return (faces.flash_ok("SAVED") if result.ok else faces.flash_err("ERROR")), None
    if isinstance(action, ApiPost):
        result = client.post(base, action.path, timeout=action.timeout)
        print(
            f"{key.name}: POST {action.path} -> {result.status} {result.detail[:200]}", flush=True
        )
        if result.ok:
            return faces.flash_ok(action.ok_face), None
        return faces.flash_err(action.err_face), None
    if isinstance(action, Cycle):
        listing = client.get_json(base, cycle_listing(action.kind))
        available = listing.get("available") if listing else None
        names = [n for n in available if isinstance(n, str)] if isinstance(available, list) else []
        active = state.mode if action.kind == "mode" else state.ruleset
        target = cycle_next(action.kind, active, names)
        result = client.post(base, cycle_path(action.kind, target))
        print(f"{key.name}: -> {target} ({result.status})", flush=True)
        if not result.ok:
            return faces.flash_err("ERROR"), None
        moved = (
            replace(state, mode=target) if action.kind == "mode" else replace(state, ruleset=target)
        )
        return faces.flash_ok(key.face(moved).text), moved
    raise TypeError(f"{key.name}: action has no dispatch path")  # pragma: no cover


def _ends_the_tournament(detail: str) -> bool:
    """Whether the confirm just sent was the one that decided it.

    The server's own answer says so — `report_result` returns the champion — so
    this is exact and arrives *with* the press. Reading it off the next poll
    instead would put the champion up on a timer and take them away again while
    the room was still looking at it.

    An unreadable body is treated as an ordinary result: a dwell that expires is
    a recoverable wrong guess, a bracket left up forever is not.
    """
    try:
        body = json.loads(detail)
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    return isinstance(body, dict) and body.get("champion") is not None


def _dwell_done(showing: bracket.Showing, now: float, clock_state: str | None) -> bool:
    """Whether a confirmed result has had its moment on the wall.

    Two ways it can be over: the dwell runs out, or the next pair simply start
    — a rack that goes quickly must not be played under a bracket, and the
    clock leaving IDLE is that fact arriving. A server that has stopped
    answering reads as IDLE, so a dead poll can only ever leave the bracket up
    for the rest of its dwell rather than pulling it down early.

    A showing with no deadline is somebody's own press (`SceneToggle`) and
    never expires: a timer must not second-guess a decision.
    """
    if showing.until is None:
        return False
    return now >= showing.until or (clock_state or "IDLE") != "IDLE"


def _start_replay(deck: Any, index: int, base: str, span: replay.Span) -> replay.Playing | None:
    """Cue and cut to a replay, saying on the key which span is being cued.

    Blocks the loop for as long as OBS takes to write the file and open it —
    same deal as a ring dump, and longer now that a rack-length buffer is a
    couple of hundred megabytes. The face is what makes that wait legible.
    """
    pending = RACK_PENDING_FACE if span is replay.Span.RACK else REPLAY_PENDING_FACE
    _set_key(deck, index, faces.Face(pending))
    return replay.show(base, span=span)


def _score_post(action: ScoreAdjust, name: str, base: str, *, decrement: bool) -> Image.Image:
    result = client.post(base, action.path(decrement=decrement))
    print(f"{name}: {'-' if decrement else '+'}1 -> {result.status}", flush=True)
    return faces.flash_ok("-1" if decrement else "+1") if result.ok else faces.flash_err("ERROR")


def _draw_all(
    deck: Any,
    state: DeckState,
    *,
    reasons: bool = False,
    skip: frozenset[int] = frozenset(),
) -> None:
    """Repaint every cell: the visible keys, dark everywhere else."""
    overlay = _REASONS_BY_INDEX if reasons else {}
    for i in range(deck.key_count()):
        if i in skip:
            continue
        key = overlay.get(i) or _BY_INDEX.get(i)
        visible = key is not None and (key.index in overlay or key.visible(state))
        _set_key(deck, i, key.face(state) if visible and key else faces.Face())


def _set_key(deck: Any, index: int, face: faces.Face | Image.Image) -> None:
    from StreamDeck.ImageHelpers import PILHelper

    image = face if isinstance(face, Image.Image) else faces.render(face)
    size = tuple(deck.key_image_format()["size"])
    if image.size != size:
        image = image.resize(size)
    with deck:
        deck.set_key_image(index, PILHelper.to_native_key_format(deck, image))


def _visible_key(index: int, state: DeckState, reasons: bool) -> Key | None:
    """The key at `index` if it is pressable right now, else None."""
    if reasons and index in _REASONS_BY_INDEX:
        return _REASONS_BY_INDEX[index]
    key = _BY_INDEX.get(index)
    if key is None or not key.visible(state):
        return None
    return key


def _loop(deck: Any, base: str, session: SessionManager) -> None:
    """Poll + dispatch forever; raises TransportError on unplug, KeyboardInterrupt on stop."""
    events: queue.Queue[tuple[int, bool]] = queue.Queue()
    deck.set_key_callback(lambda _deck, k, pressed: events.put((int(k), bool(pressed))))

    playing: replay.Playing | None = None  # a replay currently on the TV
    showing: bracket.Showing | None = None  # the bracket is on the TV
    # The clock's own state, kept beside the poll rather than in `DeckState`:
    # no face draws it, and adding it there would repaint all fifteen keys on
    # every RUNNING/IDLE transition — several times a rack, for nothing.
    clock_state: str | None = None

    def poll() -> DeckState:
        """One request serves both the session machine and every face."""
        nonlocal clock_state
        data = client.get_json(base, "/api/state")
        raw_clock = data.get("state") if isinstance(data, dict) else None
        clock_state = raw_clock if isinstance(raw_clock, str) else None
        # The session machine advances on this same poll: STARTING becomes ON
        # the moment the server answers (that is when OBS is launched), and
        # STOPPING becomes OFF when `run` finally exits.
        return build_state(
            data,
            session.tick(data is not None),
            replaying=playing is not None,
            on_bracket=showing is not None,
        )

    state = poll()
    _draw_all(deck, state)
    flashes: dict[int, float] = {}
    holds: dict[int, tuple[float, str]] = {}  # index -> (deadline, kind)
    reason_deadline: float | None = None  # flag reason page auto-files at this time

    def begin_replay(index: int, span: replay.Span) -> None:
        """Cue a replay and repaint what it changed — tap and hold share this."""
        nonlocal playing, state
        playing = _start_replay(deck, index, base, span)
        if playing is None:
            _set_key(deck, index, faces.flash_err("ERROR"))
            flashes[index] = time.monotonic() + FLASH_S
        state = replace(state, replaying=playing is not None)
        _draw_all(deck, state, reasons=reason_deadline is not None, skip=frozenset(flashes))

    next_poll = time.monotonic() + POLL_S
    while True:
        now = time.monotonic()
        if now >= next_poll:
            polled = poll()
            if polled != state:
                state = polled
                _draw_all(deck, state, reasons=reason_deadline is not None, skip=frozenset(flashes))
            next_poll = now + POLL_S
        if playing is not None and now >= playing.until:
            # The file ran out — cut back to whatever was live when it started.
            # A failed cut still clears the handle: leaving the key saying BACK
            # forever would be worse than one press that does nothing.
            replay.stop(playing)
            playing = None
            state = replace(state, replaying=False)
            _draw_all(deck, state, reasons=reason_deadline is not None, skip=frozenset(flashes))
        if showing is not None and _dwell_done(showing, now, clock_state):
            # A failed cut still clears the handle, exactly as the replay return
            # does: a stuck key is worse than one press that does nothing.
            bracket.hide(showing)
            showing = None
            state = replace(state, on_bracket=False)
            _draw_all(deck, state, reasons=reason_deadline is not None, skip=frozenset(flashes))
        if reason_deadline is not None and now >= reason_deadline:
            # Walked away: an unspecified flag has always meant OTHER, and a
            # saved clip beats a lost one.
            reason_deadline = None
            auto = _AUTO_REASON
            _set_key(deck, auto.index, faces.Face(DUMP_PENDING_FACE))
            image, _ = _dispatch(auto, base, state)
            _set_key(deck, auto.index, image)
            flashes[auto.index] = time.monotonic() + FLASH_S
            _draw_all(deck, state, skip=frozenset(flashes))
        for index, (deadline, kind) in list(holds.items()):
            if now < deadline:
                continue
            del holds[index]
            if kind == "session":
                session.stop()
                state = poll()
                _draw_all(deck, state, skip=frozenset(flashes))
                continue
            if kind == "replay":
                # Held past the threshold: the whole rack, not the last shot.
                # It fires here, under the finger, rather than on release —
                # a hold that waited for the lift would feel like a slow tap.
                begin_replay(index, replay.Span.RACK)
                continue
            # A score key held past its threshold: take the point back.
            action = _BY_INDEX[index].action
            if isinstance(action, ScoreAdjust):
                _set_key(
                    deck, index, _score_post(action, _BY_INDEX[index].name, base, decrement=True)
                )
                flashes[index] = time.monotonic() + FLASH_S
        for index, deadline in list(flashes.items()):
            if now >= deadline:
                del flashes[index]
                key = _visible_key(index, state, reason_deadline is not None)
                _set_key(deck, index, key.face(state) if key else faces.Face())
        try:
            index, pressed = events.get(timeout=0.25)
        except queue.Empty:
            continue

        key = _visible_key(index, state, reason_deadline is not None)
        if key is None:
            continue  # dark cell: this mode has no such key

        if isinstance(key.action, SessionToggle):
            sess_state = session.tick(state.up)
            if not pressed:
                if holds.pop(index, None) is not None:
                    _set_key(deck, index, key.face(state))
            elif sess_state is SessionState.ON:
                holds[index] = (time.monotonic() + key.action.hold_s, "session")
                _set_key(deck, index, faces.Face(HOLD_FACE))
            elif sess_state is SessionState.OFF:
                session.start()
                state = poll()
                _set_key(deck, index, key.face(state))
            else:  # DEV (not ours to touch) or mid-transition — refuse, say why
                print(f"session: press ignored in {sess_state.name}", flush=True)
                _set_key(deck, index, faces.flash_err(sess_state.face))
                flashes[index] = time.monotonic() + FLASH_S
            continue

        if isinstance(key.action, ScoreAdjust):
            if pressed:
                holds[index] = (time.monotonic() + key.action.hold_s, "score")
            elif holds.pop(index, None) is not None:
                # Released before the hold matured: the ordinary tap.
                _set_key(deck, index, _score_post(key.action, key.name, base, decrement=False))
                flashes[index] = time.monotonic() + FLASH_S
            continue

        if isinstance(key.action, ReplayShow):
            if playing is not None:
                # Mid-replay the key is BACK, and has no spans: one press ends
                # it, on the press rather than the release, and the matching
                # release finds nothing armed.
                if pressed:
                    replay.stop(playing)
                    playing = None
                    state = replace(state, replaying=False)
                    _draw_all(
                        deck, state, reasons=reason_deadline is not None, skip=frozenset(flashes)
                    )
            elif pressed:
                holds[index] = (time.monotonic() + key.action.hold_s, "replay")
            elif holds.pop(index, None) is not None:
                # Released before the hold matured: the ordinary tap, the last
                # shot. A `None` here means the hold already fired the rack.
                begin_replay(index, replay.Span.SHOT)
            continue

        if not pressed:
            continue
        if not state.up:
            print(f"{key.name}: ignored — server down", flush=True)
            continue

        if isinstance(key.action, SceneToggle):
            if showing is not None:
                # A failed cut back still clears the handle: a key stuck saying
                # BACK forever is worse than one press that does nothing — the
                # same rule the Replay key follows.
                bracket.hide(showing)
                showing = None
            else:
                # No dwell: a scene somebody chose stays until they choose
                # otherwise. Only the confirm key's flip is on a timer, because
                # only that one was nobody's decision to look at the bracket.
                showing = bracket.show()
            state = replace(state, on_bracket=showing is not None)
            _draw_all(deck, state, reasons=reason_deadline is not None, skip=frozenset(flashes))
            continue

        if isinstance(key.action, BracketConfirm):
            if state.bracket_armed is None or state.bracket_match_id is None:
                # Unarmed: nobody has reached their race yet. Say so rather
                # than doing nothing — a key that swallows a press reads broken.
                _set_key(deck, index, faces.flash_err("NOT YET"))
                flashes[index] = time.monotonic() + FLASH_S
                continue
            _set_key(deck, index, faces.Face(CONFIRM_PENDING_FACE))
            result = client.post(
                base, f"/api/bracket/match/{state.bracket_match_id}/result", timeout=CONFIRM_S
            )
            print(f"confirm: match {state.bracket_match_id} -> {result.status}", flush=True)
            if result.ok:
                # The wall shows the updated bracket while the next pair racks,
                # and takes itself down again — the flip out being automatic
                # while the flip back waited on somebody remembering a key is
                # how a match gets played with no shot clock on the TV.
                # **Unless that was the last match**: the champion keeps the
                # wall until somebody takes it, because there is no next pair to
                # rack for and a timer would end the evening on a shot of an
                # empty table.
                # A failed flip is not a failed result: the bracket has already
                # advanced, so the key still says SAVED and the TV stays put.
                decided = _ends_the_tournament(result.detail)
                showing = bracket.show(dwell_s=None if decided else bracket.DWELL_S) or showing
                state = poll()
                _draw_all(deck, state, skip=frozenset(flashes) | {index})
            outcome = faces.flash_ok("SAVED") if result.ok else faces.flash_err("ERROR")
            _set_key(deck, index, outcome)
            flashes[index] = time.monotonic() + FLASH_S
            continue

        if isinstance(key.action, FlagStart):
            reason_deadline = time.monotonic() + key.action.auto_s
            _set_key(deck, index, faces.Face(FLAG_PENDING_FACE))
            _draw_all(deck, state, reasons=True, skip=frozenset(flashes) | {index})
            continue

        if isinstance(key.action, FlagReason):
            reason_deadline = None
            _set_key(deck, index, faces.Face(DUMP_PENDING_FACE))
            image, _ = _dispatch(key, base, state)
            _set_key(deck, index, image)
            flashes[index] = time.monotonic() + FLASH_S
            _draw_all(deck, state, skip=frozenset(flashes) | {index})
            continue

        if isinstance(key.action, ApiPost) and key.action.pending_face is not None:
            _set_key(deck, index, faces.Face(key.action.pending_face))
        image, moved = _dispatch(key, base, state)
        if moved is not None:
            # A cycle changed the axis every other face keys off — redraw now;
            # the next poll can't catch it because `state` already moved.
            state = moved
            _draw_all(deck, state, skip=frozenset(flashes) | {index})
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
