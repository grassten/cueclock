"""Always-on Stream Deck daemon: renders key faces, dispatches presses.

All HID I/O lives here (StreamDeck imports are function-local so this module
imports everywhere); `layout`/`faces`/`client` stay pure and unit-tested. The
physical deck is exercised live, like the camera. Production runs under
launchd — `scripts/install_deck_agent.sh`; `cueclock deck` is the same loop
in a terminal.

The loop polls `GET /api/state` once every `POLL_S` and rebuilds a `DeckState`
from it — one request carrying clock state, mode, settings, the countdown a
Start press would hand you, the extension pool and the score, so every face is
drawn from the same snapshot and none of them can drift apart.
"""

from __future__ import annotations

import contextlib
import queue
import time
from dataclasses import replace
from typing import Any

from PIL import Image

from cueclock.deck import bracket, client, faces
from cueclock.deck.layout import (
    CONFIRM_PENDING_FACE,
    ApiPost,
    BracketConfirm,
    Cycle,
    DeckState,
    Key,
    LiveToggle,
    MenuToggle,
    Recalibrate,
    SceneToggle,
    ScoreAdjust,
    SessionToggle,
    SettingCycle,
    cycle_listing,
    cycle_next,
    cycle_path,
    page,
    setting_next,
)
from cueclock.deck.live import Stream
from cueclock.deck.recal import Recalibrator
from cueclock.deck.session import SessionManager, SessionState, shot_age_of

POLL_S = 2.0  # server/state poll cadence
FLASH_S = 1.2  # how long a press outcome stays on the key
RETRY_S = 3.0  # device reconnect cadence
CONFIRM_S = 5.0  # a bracket confirm writes a result, loads a match and racks
HOLD_FACE = "HOLD…"  # shown while a key is held toward its action (Session, RECAL, LIVE)
# The SETUP menu closes by itself this long after the last press in it, so it
# can never be left open over a game. Chosen, not measured: long enough to read
# a key and press the next, short enough that a menu nobody closed is gone by
# the time anyone breaks. Change it at the table if it feels wrong.
MENU_IDLE_S = 10.0


def _key_at(index: int, state: DeckState) -> Key | None:
    """The key in cell ``index`` on the page the deck is showing.

    Looked up per page, never by index alone: the menu's empty cells must do
    nothing, not whatever the main page has there.
    """
    return next((key for key in page(state) if key.index == index), None)


def _settings_of(payload: object) -> dict[str, str]:
    """The `settings` object of a state payload, as plain strings."""
    if not isinstance(payload, dict):
        return {}
    return {k: v for k, v in payload.items() if isinstance(k, str) and isinstance(v, str)}


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


def _health_dot(payload: object) -> faces.Rgb:
    """The Session key's dot: green while the rig is healthy, amber otherwise.

    Amber means the server's own health line says so (#52): the reference
    had gone stale at launch, or detection is running slower than its
    constants were tuned at. The numbers are on the control page; the dot is
    what carries across the room.
    """
    if isinstance(payload, dict) and payload.get("warn") is True:
        return faces.DOT_WARN
    return faces.DOT_OK


def build_state(
    data: dict[str, object] | None,
    session: SessionState,
    *,
    on_bracket: bool = False,
    menu: bool = False,
    recal_face: faces.Face | None = None,
    live_face: faces.Face | None = None,
) -> DeckState:
    """Turn one `GET /api/state` body into everything the faces need.

    Server down is a `DeckState` too, not an exception: every key but Session
    goes dark, and the Session key still says how to bring the server back.

    ``on_bracket`` and ``menu`` are the face inputs the server knows nothing
    about — the scene lives between the deck and OBS, the page on the deck
    alone — so the loop passes its own answers in rather than inventing a
    second source of state. ``live_face`` is OBS's, through `deck/live.py`.
    The menu and the LIVE key never survive the server going down.
    """
    if data is None:
        return DeckState(
            up=False,
            on_bracket=on_bracket,
            session_face=session.face,
            session_sub=session.sub,
            session_bg=faces.BG_ERR if session.failed else faces.BG,
            session_idle=session is SessionState.OFF or session.failed,
            recal_face=recal_face or faces.Face(),
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
        settings=_settings_of(data.get("settings")),
        base=_int("base", 30),
        extensions_used=_int("extensions_used", 0),
        extensions_allowed=_int("extensions_allowed", 0),
        p1=_score_of(data.get("score"), "p1"),
        p2=_score_of(data.get("score"), "p2"),
        on_bracket=on_bracket,
        menu=menu,
        session_face=session.face,
        session_sub=session.sub,
        session_bg=faces.BG_ERR if session.failed else faces.BG,
        # The session is what the health dot reports on: present once the
        # server is answering (there is nothing to be healthy otherwise), and
        # amber when its health line warns.
        session_dot=_health_dot(data.get("health")),
        live_face=live_face,
        **_bracket_fields(data.get("bracket")),  # type: ignore[arg-type]
    )


def _dispatch(key: Key, base: str, state: DeckState) -> tuple[Image.Image, DeckState | None]:
    """Run a key's action; return (flash face, an eagerly-updated state | None).

    The second element exists so a cycle key's own face — and every face that
    depends on the axis it just moved — can be redrawn before the next poll,
    which would otherwise report the value we already know we changed.
    """
    action = key.action
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
        target = cycle_next(action.kind, state.mode, names)
        result = client.post(base, cycle_path(action.kind, target))
        print(f"{key.name}: -> {target} ({result.status})", flush=True)
        if not result.ok:
            return faces.flash_err("ERROR"), None
        moved = replace(state, mode=target)
        return faces.flash_ok(key.face(moved).text), moved
    if isinstance(action, SettingCycle):
        target = setting_next(action.setting, state.settings.get(action.setting))
        result = client.post(base, f"/api/setting/{action.setting}/{target}")
        print(f"{key.name}: -> {target} ({result.status})", flush=True)
        if not result.ok:
            return faces.flash_err("ERROR"), None
        # The new value shows on the key and on every key it gates (PUSH,
        # START, EXT) without waiting for the poll.
        moved = replace(state, settings={**state.settings, action.setting: target})
        moved_face = key.face(moved)
        return faces.flash_ok(moved_face.big or moved_face.text), moved
    raise TypeError(f"{key.name}: action has no dispatch path")  # pragma: no cover


def _decided_of(payload: object) -> int | None:
    """How many matches the draw has finished, out of a state payload.

    `None` in casual play and with the server down — both meaning "no answer",
    which the loop treats as no news rather than as a result.
    """
    if not isinstance(payload, dict):
        return None
    summary = payload.get("bracket")
    if not isinstance(summary, dict):
        return None
    value = summary.get("decided")
    return value if isinstance(value, int) else None


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


def _score_post(action: ScoreAdjust, name: str, base: str, *, decrement: bool) -> Image.Image:
    result = client.post(base, action.path(decrement=decrement))
    print(f"{name}: {'-' if decrement else '+'}1 -> {result.status}", flush=True)
    return faces.flash_ok("-1" if decrement else "+1") if result.ok else faces.flash_err("ERROR")


def _draw_all(
    deck: Any,
    state: DeckState,
    *,
    skip: frozenset[int] = frozenset(),
) -> None:
    """Repaint every cell: the visible keys, dark everywhere else."""
    for i in range(deck.key_count()):
        if i in skip:
            continue
        key = _key_at(i, state)
        visible = key is not None and key.visible(state)
        _set_key(deck, i, key.face(state) if visible and key else faces.Face())


def _set_key(deck: Any, index: int, face: faces.Face | Image.Image) -> None:
    from StreamDeck.ImageHelpers import PILHelper

    image = face if isinstance(face, Image.Image) else faces.render(face)
    size = tuple(deck.key_image_format()["size"])
    if image.size != size:
        image = image.resize(size)
    with deck:
        deck.set_key_image(index, PILHelper.to_native_key_format(deck, image))


def _visible_key(index: int, state: DeckState) -> Key | None:
    """The key at `index` if it is pressable right now, else None."""
    key = _key_at(index, state)
    if key is None or not key.visible(state):
        return None
    return key


def _loop(
    deck: Any,
    base: str,
    session: SessionManager,
    recal: Recalibrator | None = None,
    live: Stream | None = None,
) -> None:
    """Poll + dispatch forever; raises TransportError on unplug, KeyboardInterrupt on stop."""
    recal = recal if recal is not None else Recalibrator()
    live = live if live is not None else Stream()
    # Each key event with the moment it happened: the loop can be busy for
    # seconds (a session stop), and a press must be judged against what the
    # key said when the finger came down, not when the loop got to it.
    events: queue.Queue[tuple[int, bool, float]] = queue.Queue()
    deck.set_key_callback(
        lambda _deck, k, pressed: events.put((int(k), bool(pressed), time.monotonic()))
    )

    showing: bracket.Showing | None = None  # the bracket is on the TV
    # The clock's own state, kept beside the poll rather than in `DeckState`:
    # no face draws it, and adding it there would repaint all fifteen keys on
    # every RUNNING/IDLE transition — several times a rack, for nothing.
    clock_state: str | None = None
    # Results on the draw, and how many the TV has already reacted to. A result
    # can land without this deck pressing anything — somebody concedes a match
    # or reports a score from a phone — and to the room that is the same event
    # as the confirm key, so the wall does the same thing.
    decided: int | None = None
    shown_decided: int | None = None
    menu_until: float | None = None  # the SETUP menu is open until then

    def poll() -> DeckState:
        """One request serves both the session machine and every face."""
        nonlocal clock_state, decided, menu_until
        data = client.get_json(base, "/api/state")
        if data is None:
            menu_until = None  # nothing on the menu works without the server
        raw_clock = data.get("state") if isinstance(data, dict) else None
        clock_state = raw_clock if isinstance(raw_clock, str) else None
        decided = _decided_of(data)
        # The session machine advances on this same poll: STARTING becomes ON
        # the moment the server answers (that is when OBS is launched), and
        # STOPPING becomes OFF when `run` finally exits.
        session_state = session.tick(data is not None)
        return build_state(
            data,
            session_state,
            on_bracket=showing is not None,
            menu=menu_until is not None,
            recal_face=recal.face(),
            # OBS is asked only while the deck's own session is up: that is
            # the OBS the deck launched, and the only one it may stream from.
            live_face=live.poll(data is not None and session_state is SessionState.ON),
        )

    state = poll()
    _draw_all(deck, state)
    flashes: dict[int, float] = {}
    holds: dict[int, tuple[float, str]] = {}  # index -> (deadline, kind)
    # What the Session key says, and since when (see the Session branch below).
    session_face, session_face_since = state.session_face, time.monotonic()

    next_poll = time.monotonic() + POLL_S
    while True:
        now = time.monotonic()
        if now >= next_poll:
            polled = poll()
            if polled != state:
                state = polled
                _draw_all(deck, state, skip=frozenset(flashes))
            next_poll = now + POLL_S
            # A result landed that nobody pressed a key for — a match conceded
            # or a score reported from a phone. The wall does what the confirm
            # key does, because to the room it is the same event: a match
            # ended, the draw moved, and the next pair are about to rack.
            # `None` on either side is no news (casual play, or a server that
            # stopped answering), never a result.
            if decided is not None and shown_decided is not None and decided > shown_decided:
                if showing is None:
                    showing = bracket.show(dwell_s=bracket.DWELL_S)
                    state = replace(state, on_bracket=showing is not None)
                    _draw_all(deck, state, skip=frozenset(flashes))
                elif showing.until is not None:
                    # Already up on a timer — a second result inside the dwell
                    # gives it a fresh one rather than a new scene cut. A
                    # showing somebody *chose* keeps having no deadline.
                    showing = replace(showing, until=now + bracket.DWELL_S)
            shown_decided = decided
        if menu_until is not None and now >= menu_until:
            # Nobody closed it: it closes itself before it can sit over a game.
            menu_until = None
            holds.clear()
            state = replace(state, menu=False)
            _draw_all(deck, state, skip=frozenset(flashes))
        if recal.poll():
            # A recalibration finished: its result goes on the key now, not
            # on the next 2 s poll.
            state = poll()
            _draw_all(deck, state, skip=frozenset(flashes))
        if showing is not None and _dwell_done(showing, now, clock_state):
            # A failed cut still clears the handle: a stuck key is worse than
            # one press that does nothing.
            bracket.hide(showing)
            showing = None
            state = replace(state, on_bracket=False)
            _draw_all(deck, state, skip=frozenset(flashes))
        for index, (deadline, kind) in list(holds.items()):
            if now < deadline:
                continue
            del holds[index]
            if kind == "session":
                # Say it before doing it. `stop()` returns only once OBS is
                # down, and the poll behind it waits on the TV: seconds in
                # which nothing here repaints. Left on HOLD… the key looked
                # dead for 4.5 s on 2026-11-18, and got pressed again.
                _set_key(deck, index, faces.Face(SessionState.STOPPING.face))
                session.stop()
                state = poll()
                _draw_all(deck, state, skip=frozenset(flashes))
                continue
            if kind in ("live_start", "live_stop"):
                # A LIVE hold. `act` asks OBS again first, so a hold begun to
                # stop a stream that has since died starts nothing.
                starting = kind == "live_start"
                _set_key(deck, index, faces.Face("STARTING…" if starting else "STOPPING…"))
                live.act("start" if starting else "stop")
                state = poll()
                _draw_all(deck, state, skip=frozenset(flashes))
                continue
            if kind == "recal":
                # Re-checked at the moment it fires: a session may have been
                # started from a terminal while the key was held.
                if not state.up and state.session_idle and recal.start():
                    state = poll()
                _draw_all(deck, state, skip=frozenset(flashes))
                continue
            # A score key held past its threshold: take the point back.
            held = _key_at(index, state)
            if held is not None and isinstance(held.action, ScoreAdjust):
                _set_key(deck, index, _score_post(held.action, held.name, base, decrement=True))
                flashes[index] = time.monotonic() + FLASH_S
        for index, deadline in list(flashes.items()):
            if now >= deadline:
                del flashes[index]
                key = _visible_key(index, state)
                _set_key(deck, index, key.face(state) if key else faces.Face())
        if state.session_face != session_face:
            # Stamped after everything above, so any press still waiting in
            # the queue from a busy stretch is older than the face it met.
            session_face, session_face_since = state.session_face, time.monotonic()
        try:
            index, pressed, pressed_at = events.get(timeout=0.25)
        except queue.Empty:
            continue

        key = _visible_key(index, state)
        if key is None:
            continue  # dark cell: this mode (or page) has no such key
        if menu_until is not None:
            menu_until = time.monotonic() + MENU_IDLE_S  # any press keeps it open

        if isinstance(key.action, Recalibrate):
            if pressed and not recal.busy:
                holds[index] = (time.monotonic() + key.action.hold_s, "recal")
                _set_key(deck, index, faces.Face(HOLD_FACE))
            elif not pressed and holds.pop(index, None) is not None:
                _set_key(deck, index, key.face(state))  # let go early: nothing ran
            continue

        if isinstance(key.action, LiveToggle):
            if pressed:
                # What the hold will do is fixed by the face the finger came
                # down on, never by whatever the stream is doing a second later.
                intent = live.intent()
                if intent is None:  # STARTING… or STOPPING…: nothing to hold for
                    _set_key(deck, index, faces.flash_err(key.face(state).text))
                    flashes[index] = time.monotonic() + FLASH_S
                    continue
                holds[index] = (time.monotonic() + key.action.hold_s, f"live_{intent}")
                _set_key(deck, index, faces.Face(HOLD_FACE))
            elif holds.pop(index, None) is not None:
                _set_key(deck, index, key.face(state))  # let go early: nothing ran
            continue

        if isinstance(key.action, SessionToggle):
            sess_state = session.tick(state.up)
            if pressed and recal.busy:
                # The recalibration has the camera; `run` would fail to open it.
                print("session: press ignored — recalibration running", flush=True)
                _set_key(deck, index, faces.flash_err("RECAL…"))
                flashes[index] = time.monotonic() + FLASH_S
                continue
            if not pressed:
                if holds.pop(index, None) is not None:
                    _set_key(deck, index, key.face(state))
            elif pressed_at < session_face_since or sess_state.face != session_face:
                # The finger came down on a face the key no longer shows: it
                # was pressed while the loop was busy changing the session, or
                # the session changed since the last poll painted it. Acting
                # on it would do what the *new* face offers, which nobody
                # asked for. On 2026-11-18 three presses made during a stop
                # were read once the key said START, and the first of them
                # started a second session. Show the truth and do nothing.
                print(
                    f"session: press ignored — the key had changed to {sess_state.name}", flush=True
                )
                state = poll()
                _set_key(deck, index, key.face(state))
            elif sess_state is SessionState.ON:
                holds[index] = (time.monotonic() + key.action.hold_s, "session")
                _set_key(deck, index, faces.Face(HOLD_FACE))
            elif sess_state is SessionState.OFF or sess_state.failed:
                # A failed start is retried by pressing it again — `start()`
                # clears the reason and re-runs the camera probe, so the press
                # after the cable goes back in just works.
                _set_key(deck, index, faces.Face(SessionState.STARTING.face))
                session.start()
                recal.result = None  # read by now; the next night starts clean
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

        if not pressed:
            continue
        if not state.up:
            print(f"{key.name}: ignored — server down", flush=True)
            continue

        if isinstance(key.action, MenuToggle):
            # The page changes under every finger: a hold begun on one page
            # must not mature on the other.
            holds.clear()
            menu_until = None if menu_until is not None else time.monotonic() + MENU_IDLE_S
            state = replace(state, menu=menu_until is not None)
            _draw_all(deck, state, skip=frozenset(flashes))
            continue

        if isinstance(key.action, SceneToggle):
            if showing is not None:
                # A failed cut back still clears the handle: a key stuck saying
                # BACK forever is worse than one press that does nothing.
                bracket.hide(showing)
                showing = None
            else:
                # No dwell: a scene somebody chose stays until they choose
                # otherwise. Only the confirm key's flip is on a timer, because
                # only that one was nobody's decision to look at the bracket.
                showing = bracket.show()
            state = replace(state, on_bracket=showing is not None)
            _draw_all(deck, state, skip=frozenset(flashes))
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
                # A failed flip is not a failed result: the bracket has already
                # advanced, so the key still says SAVED and the TV stays put.
                showing = bracket.show(dwell_s=bracket.DWELL_S) or showing
                state = poll()
                # This press *is* the result the watcher above reacts to, and
                # it has already been acted on — so it is news to nobody.
                shown_decided = decided
                _draw_all(deck, state, skip=frozenset(flashes) | {index})
            outcome = faces.flash_ok("SAVED") if result.ok else faces.flash_err("ERROR")
            _set_key(deck, index, outcome)
            flashes[index] = time.monotonic() + FLASH_S
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


def run_deck(base: str, brightness: int, reference: str | None) -> int:
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

    session = SessionManager(
        reference=reference,
        # The daemon's --server base, not session.py's default — the one deck
        # talking to a non-default port must clear the match on that port too.
        clear_match=lambda: client.post(base, "/api/match/clear", timeout=3.0).ok,
        shot_age=lambda: shot_age_of(client.get_json(base, "/api/shot_age")),
    )

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
