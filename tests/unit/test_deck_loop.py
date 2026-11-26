"""The deck loop's press routing — the part a key press decides, not a face.

Everything else about the deck is either pure data (`test_deck.py`) or HID,
which is exercised live like the camera. This file sits between them: it drives
`_loop` with a fake device so the gestures that *dispatch* differently can be
told apart without a finger.

It began with the Replay key's tap-and-hold routing (2026-08-05, removed in
checkpoint 144); what it covers now is the rest of what only a running loop can
show — the bracket's dwell, the holds on RECAL, Session and LIVE, and presses
that must do nothing.
"""

from __future__ import annotations

import contextlib
import queue
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

from cueclock.clock import Settings
from cueclock.deck import bracket as deck_bracket
from cueclock.deck import client, daemon, faces, obsws
from cueclock.deck.layout import (
    LAYOUT,
    BracketConfirm,
    LiveToggle,
    MenuToggle,
    Recalibrate,
    SceneToggle,
    SessionToggle,
)
from cueclock.deck.live import Stream
from cueclock.deck.recal import Recalibrator
from cueclock.deck.session import SessionManager, SessionState

SETTLE_S = 2.0  # generous: the loop's event wait is 0.25 s


@pytest.fixture(autouse=True)
def _no_real_obs(monkeypatch: pytest.MonkeyPatch) -> None:
    """No loop in this file may reach the rig's OBS.

    The LIVE key asks OBS for the stream's state on every poll of a running
    session (#80). A test that forgot to fake it would be asking the real
    obs-websocket, and one hold away from putting the room on YouTube. OBS
    "not answering" is what every test without its own fake sees.
    """
    monkeypatch.setattr(obsws, "request", lambda *a, **k: None)


class FakeDeck:
    """Just enough Stream Deck for `_loop`: a callback, a size, and pixels."""

    def __init__(self) -> None:
        self.callback: Any = None
        self.images: dict[int, Any] = {}

    def key_count(self) -> int:
        return 15

    def set_key_callback(self, callback: Any) -> None:
        self.callback = callback

    def key_image_format(self) -> dict[str, Any]:
        return {"size": (72, 72), "format": "JPEG", "flip": (True, True), "rotation": 0}

    def set_key_image(self, index: int, image: Any) -> None:
        self.images[index] = image

    def __enter__(self) -> FakeDeck:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    # --- what a test drives ---

    def press(self, index: int) -> None:
        self.callback(self, index, True)

    def release(self, index: int) -> None:
        self.callback(self, index, False)


# `_loop` runs until something raises out of it, so a test that just walks away
# leaves a live loop behind — and a leaked one still resolves `client.get_json`
# and `bracket.hide` through whatever the *next* test has patched in, which is
# how a test can see a scene change nobody in it made. Both fixtures stop their
# loop on the way out, through this one event so that any older leak stops too.
_STOP = threading.Event()


def _start_loop(
    deck: FakeDeck,
    session: SessionManager | None = None,
    recal: Recalibrator | None = None,
    live: Stream | None = None,
) -> threading.Thread:
    """`_loop` in a thread that ends quietly when `_STOP` is set."""

    def run() -> None:
        # KeyboardInterrupt out of the poll is the loop's own documented exit;
        # swallowing it here is what keeps the thread from dying "unhandled".
        with contextlib.suppress(KeyboardInterrupt):
            daemon._loop(deck, "http://x", session or SessionManager(), recal, live)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    deadline = time.monotonic() + SETTLE_S
    while deck.callback is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert deck.callback is not None, "loop never registered its key callback"
    return thread


def _stop(thread: threading.Thread) -> None:
    """End the loop, and *fail* if it will not go.

    Asserted rather than hoped for: a surviving loop is not a slow test, it is
    a loop that will call `bracket.hide` through the next test's patches — or,
    once pytest has restored them, through the real `obs-websocket` client, at
    which point a test suite is switching scenes on the wall TV. That happened
    on 2026-08-13, during a live session.
    """
    _STOP.set()
    thread.join(timeout=SETTLE_S)
    assert not thread.is_alive(), "the deck loop outlived its test"


def _stoppable(payload: Any) -> Any:
    """A `client.get_json` stand-in that raises the loop out on `_STOP`."""

    def poll(*_a: object, **_k: object) -> Any:
        if _STOP.is_set():
            raise KeyboardInterrupt
        return payload() if callable(payload) else payload

    return poll


def _wait(cond: Any, timeout: float = SETTLE_S) -> bool:
    """Spin until `cond()` or the timeout — for what the loop does on its own."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


# ---------------------------------------------------------- the bracket dwell


CONFIRM_KEY = next(k for k in LAYOUT if isinstance(k.action, BracketConfirm))
SCENE_KEY = next(k for k in LAYOUT if isinstance(k.action, SceneToggle))
DWELL_S = 0.4  # the real 30 s, compressed — the behaviour is the deadline, not its length


class SceneSpy:
    """Records what the loop did to the TV, and fakes OBS agreeing."""

    def __init__(self) -> None:
        self.shown: list[float | None] = []  # the dwell each show asked for
        self.hidden = 0
        self.up = threading.Event()

    def show(self, dwell_s: float | None = None) -> deck_bracket.Showing:
        self.shown.append(dwell_s)
        self.up.set()
        until = None if dwell_s is None else time.monotonic() + dwell_s
        return deck_bracket.Showing(live_scene="Stream Detail", until=until)

    def hide(self, showing: deck_bracket.Showing) -> bool:
        self.hidden += 1
        return True


def _armed_payload(clock: str = "IDLE", decided: int = 3) -> dict[str, object]:
    return {
        "state": clock,
        "mode": "match",
        "bracket": {
            "revision": 4,
            "decided": decided,
            "match": {"id": 4, "side": "A", "header": "Match 4 · A Side"},
            "armed": {"slot": 0, "name": "Priya Shah", "score": 5, "other": 2},
        },
    }


@pytest.fixture
def tv(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """A `_loop` with a bracket loaded, an armed result, and a fake TV.

    **The loop is stopped on the way out**, because these tests assert that
    nothing happened *later* — and a leaked loop outlives its test while
    `bracket.hide` still resolves to whichever spy is patched in now, so the
    next test sees a scene change nobody in it made. The loop's own documented
    exit is an exception out of the poll.
    """
    spy = SceneSpy()
    # One mutable dict a test can move under the running loop: the clock's
    # state, and how many matches the draw has decided.
    live: dict[str, object] = {"state": "IDLE", "decided": 3, "polls": 0}
    _STOP.clear()

    def payload() -> dict[str, object]:
        # Counted, because a test that changes the draw has to know the loop
        # has already seen it *unchanged* — the first count a loop sees is
        # deliberately never news, and the loop polls on its own cadence.
        live["polls"] = int(live["polls"]) + 1
        return _armed_payload(str(live["state"]), int(live["decided"]))

    monkeypatch.setattr(deck_bracket, "show", spy.show)
    monkeypatch.setattr(deck_bracket, "hide", spy.hide)
    monkeypatch.setattr(deck_bracket, "DWELL_S", DWELL_S)
    monkeypatch.setattr(client, "get_json", _stoppable(payload))
    monkeypatch.setattr(client, "post", lambda *a, **k: client.PostResult(True, 200, "{}"))
    monkeypatch.setattr(daemon, "_set_key", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "POLL_S", 0.05)  # the early return is only as quick as the poll

    deck = FakeDeck()
    thread = _start_loop(deck)
    yield deck, spy, live
    _stop(thread)


def test_a_confirmed_result_puts_the_bracket_up_and_takes_it_down_again(tv: Any) -> None:
    """The flip out was automatic and the flip back was a human remembering.

    That is a wall that can sit on the bracket through a live match — and the
    bracket scene carries no shot clock and no table, so the one thing the rig
    exists to show would be missing exactly when play resumes.
    """
    deck, spy, _live = tv
    deck.press(CONFIRM_KEY.index)
    assert spy.up.wait(SETTLE_S), "the confirmed result never reached the wall"
    assert spy.shown == [DWELL_S]
    assert spy.hidden == 0  # the moment is still on the wall

    deadline = time.monotonic() + DWELL_S + SETTLE_S
    while spy.hidden == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert spy.hidden == 1


def test_the_next_pair_breaking_takes_the_bracket_down_early(
    monkeypatch: pytest.MonkeyPatch, tv: Any
) -> None:
    """A rack that goes quickly must not be played under a bracket.

    The dwell is set to the real thirty seconds here rather than the compressed
    one every other test uses: a hide inside a second then means the *break*
    took the bracket down, with no race against a deadline that would have
    taken it down anyway.
    """
    deck, spy, live = tv
    monkeypatch.setattr(deck_bracket, "DWELL_S", 30.0)
    deck.press(CONFIRM_KEY.index)
    assert spy.up.wait(SETTLE_S)
    live["state"] = "RUNNING"  # somebody broke
    deadline = time.monotonic() + SETTLE_S
    while spy.hidden == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert spy.hidden == 1, "the bracket sat there with play under way"


def test_a_result_nobody_pressed_a_key_for_still_reaches_the_wall(tv: Any) -> None:
    """A match conceded, or a score reported from a phone.

    The confirm key is not the only way a match ends, and to the room it is the
    same event either way — a match is over, the draw has moved, the next pair
    are about to rack. Watching the *count of decided matches* rather than the
    bracket's revision is what keeps a race stepper or a Switch match from
    taking over the wall, since those move the revision too.
    """
    _deck, spy, live = tv
    # The first count a loop sees is never news — a deck restarted mid-event
    # must not flip the wall over results already in the draw — so wait for the
    # loop to have taken that baseline *inside* its own loop before moving it.
    assert _wait(lambda: int(live["polls"]) >= 2), "the loop never polled twice"
    assert spy.shown == []
    live["decided"] = int(live["decided"]) + 1  # somebody conceded, from a phone
    assert spy.up.wait(SETTLE_S), "the result never reached the wall"
    assert spy.shown == [DWELL_S]  # and on the same timer a confirm gets

    deadline = time.monotonic() + DWELL_S + SETTLE_S
    while spy.hidden == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert spy.hidden == 1


def test_a_draw_that_only_moves_does_not_touch_the_wall(tv: Any) -> None:
    """Loading a match, editing one, changing the races — all move `revision`,
    and none of them is a result. A wall that flips on every stepper tap is a
    wall nobody can play under."""
    _deck, spy, _live = tv
    time.sleep(DWELL_S * 2)  # several polls, nothing decided
    assert spy.shown == []
    assert spy.hidden == 0


def test_a_scene_somebody_chose_stays_up(tv: Any) -> None:
    """Only the confirm key's flip is on a timer: nobody chose to look at that
    one. A key press is a decision, and a timer must not second-guess it."""
    deck, spy, _live = tv
    deck.press(SCENE_KEY.index)
    assert spy.up.wait(SETTLE_S)
    assert spy.shown == [None]  # no dwell asked for
    time.sleep(DWELL_S * 3)
    assert spy.hidden == 0


def test_events_are_ignored_while_the_server_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every key but Session goes dark with the server down, and a dark cell
    dispatches nothing — the scene key included, which would cut the TV."""
    spy = SceneSpy()
    monkeypatch.setattr(deck_bracket, "show", spy.show)
    monkeypatch.setattr(client, "get_json", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_set_key", lambda *a, **k: None)

    deck = FakeDeck()
    events: queue.Queue[None] = queue.Queue()
    threading.Thread(
        target=daemon._loop, args=(deck, "http://x", SessionManager()), daemon=True
    ).start()
    deadline = time.monotonic() + SETTLE_S
    while deck.callback is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert deck.callback is not None

    deck.press(SCENE_KEY.index)
    time.sleep(0.5)
    deck.release(SCENE_KEY.index)
    assert spy.shown == []
    assert events.empty()


# --- RECAL (#49): a hold starts it, and the camera is its until it finishes ---

RECAL_KEY = next(k for k in LAYOUT if isinstance(k.action, Recalibrate))
RECAL_HOLD_S = RECAL_KEY.action.hold_s if isinstance(RECAL_KEY.action, Recalibrate) else 2.0
SESSION_KEY = next(k for k in LAYOUT if isinstance(k.action, SessionToggle))


class SlowRecal:
    """A recalibration that runs until the test says it is done."""

    def __init__(self) -> None:
        self.spawned = threading.Event()
        self.finish = threading.Event()

    def __call__(self, cmd: object) -> SlowRecal:
        self.spawned.set()
        return self

    def poll(self) -> int | None:
        return 0 if self.finish.is_set() else None

    def communicate(self) -> tuple[str, str]:
        return '{"ok": true, "reason": "OK", "residual_px": 0.4, "drift_after": 1.9}', ""


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[FakeDeck, SlowRecal, list[object]]]:
    """A `_loop` with the server down and the session at rest."""
    _STOP.clear()
    monkeypatch.setattr(client, "get_json", _stoppable(None))
    monkeypatch.setattr(daemon, "_set_key", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "POLL_S", 0.05)
    slow = SlowRecal()
    spawned: list[object] = []
    # Every command the session runs lands here: a start would show up as a
    # camera probe, and nothing in these tests may reach the real one.
    session = SessionManager(call=lambda cmd: spawned.append(cmd) or 1)
    deck = FakeDeck()
    thread = _start_loop(deck, session, Recalibrator(spawn=slow))
    yield deck, slow, spawned
    slow.finish.set()
    _stop(thread)


def test_a_full_hold_starts_a_recalibration(
    offline: tuple[FakeDeck, SlowRecal, list[object]],
) -> None:
    deck, slow, _ = offline
    deck.press(RECAL_KEY.index)
    assert slow.spawned.wait(RECAL_HOLD_S + SETTLE_S), "the hold never started it"
    deck.release(RECAL_KEY.index)


def test_letting_go_early_runs_nothing(offline: tuple[FakeDeck, SlowRecal, list[object]]) -> None:
    deck, slow, _ = offline
    deck.press(RECAL_KEY.index)
    time.sleep(RECAL_HOLD_S / 4)
    deck.release(RECAL_KEY.index)
    assert not slow.spawned.wait(RECAL_HOLD_S + 0.5)


def test_session_start_is_refused_while_the_camera_is_recalibrating(
    offline: tuple[FakeDeck, SlowRecal, list[object]],
) -> None:
    deck, slow, spawned = offline
    deck.press(RECAL_KEY.index)
    assert slow.spawned.wait(RECAL_HOLD_S + SETTLE_S)
    deck.release(RECAL_KEY.index)
    deck.press(SESSION_KEY.index)
    deck.release(SESSION_KEY.index)
    time.sleep(0.5)
    assert spawned == []  # not even the camera probe: the start never began


# --- the Session key answers the face it was pressed on (2026-11-18) ---
#
# Stopping a session is seconds of work done on the loop itself (OBS's
# recording, its outputs, its quit, then the TV). Presses made in that time
# used to be read afterwards, against the key's new face: Steven held the key
# to stop, saw it sit on HOLD… for 4.5 s, pressed again, and the deck started
# a second session with the first of those presses.

SESSION_HOLD_S = SESSION_KEY.action.hold_s if isinstance(SESSION_KEY.action, SessionToggle) else 1.0


class SlowStopSession:
    """A session whose stop holds the loop the way the real one does."""

    def __init__(self, stop_s: float) -> None:
        self.state = SessionState.ON
        self.stop_s = stop_s
        self.starts = 0
        self.stopping = threading.Event()
        self.stopped = threading.Event()

    def tick(self, server_up: bool) -> SessionState:
        return self.state

    def stop(self) -> None:
        self.stopping.set()
        time.sleep(self.stop_s)
        self.state = SessionState.OFF
        self.stopped.set()

    def start(self) -> None:
        self.starts += 1
        self.state = SessionState.STARTING


@pytest.fixture
def stopping(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[FakeDeck, SlowStopSession, list[str]]]:
    """A `_loop` on a running session, and every face the Session key was given."""
    _STOP.clear()
    monkeypatch.setattr(client, "get_json", _stoppable({"state": "RUNNING", "mode": "match"}))
    painted: list[str] = []

    def set_key(_deck: object, index: int, face: object) -> None:
        if index == SESSION_KEY.index:
            painted.append(getattr(face, "text", ""))

    monkeypatch.setattr(daemon, "_set_key", set_key)
    monkeypatch.setattr(daemon, "POLL_S", 0.05)
    session = SlowStopSession(stop_s=0.8)
    deck = FakeDeck()
    thread = _start_loop(deck, session)  # type: ignore[arg-type]
    yield deck, session, painted
    _stop(thread)


def test_presses_made_while_a_stop_runs_do_not_start_a_session(
    stopping: tuple[FakeDeck, SlowStopSession, list[str]],
) -> None:
    deck, session, painted = stopping
    deck.press(SESSION_KEY.index)  # hold to stop
    assert session.stopping.wait(SESSION_HOLD_S + SETTLE_S), "the hold never stopped it"
    assert painted[-1] == SessionState.STOPPING.face  # it said so before it went quiet
    deck.release(SESSION_KEY.index)
    for _ in range(3):  # "is it stuck?"
        deck.press(SESSION_KEY.index)
        deck.release(SESSION_KEY.index)
    assert session.stopped.wait(SETTLE_S)
    time.sleep(0.6)  # the loop reads everything that queued up
    assert session.starts == 0
    assert session.state is SessionState.OFF

    deck.press(SESSION_KEY.index)  # and a press on START still starts
    deck.release(SESSION_KEY.index)
    deadline = time.monotonic() + SETTLE_S
    while session.starts == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert session.starts == 1


def test_a_press_on_a_face_the_session_has_already_left_does_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The key is repainted on the poll. Between polls the session can move on
    (a `run` that died): a press then meets START on a key still reading ON."""
    _STOP.clear()
    monkeypatch.setattr(client, "get_json", _stoppable({"state": "RUNNING", "mode": "match"}))
    monkeypatch.setattr(daemon, "_set_key", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "POLL_S", 0.7)
    session = SlowStopSession(stop_s=0.0)
    deck = FakeDeck()
    thread = _start_loop(deck, session)  # type: ignore[arg-type]
    try:
        session.state = SessionState.OFF  # the key still reads ON until the next poll
        deck.press(SESSION_KEY.index)
        deck.release(SESSION_KEY.index)
        time.sleep(0.4)
        assert session.starts == 0
        time.sleep(0.8)  # a poll has painted START by now
        deck.press(SESSION_KEY.index)
        deck.release(SESSION_KEY.index)
        deadline = time.monotonic() + SETTLE_S
        while session.starts == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert session.starts == 1
    finally:
        _stop(thread)


# --- the SETUP menu (#74): a second page, and what must not cross pages ---

SETUP_KEY = next(k for k in LAYOUT if isinstance(k.action, MenuToggle))
P1_KEY = next(k for k in LAYOUT if k.name == "p1")


class MenuRig:
    """What the loop posted and last drew, per cell."""

    def __init__(self) -> None:
        self.posted: list[str] = []
        self.drawn: dict[int, Any] = {}

    def text(self, index: int) -> str | None:
        face = self.drawn.get(index)
        return face.text if isinstance(face, faces.Face) else None


@pytest.fixture
def menu(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[FakeDeck, MenuRig]]:
    rig = MenuRig()
    _STOP.clear()
    payload = {"state": "IDLE", "mode": "match", "settings": Settings().as_dict()}

    def post(base: str, path: str, timeout: float = 3.0) -> client.PostResult:
        rig.posted.append(path)
        return client.PostResult(True, 200, "{}")

    monkeypatch.setattr(client, "get_json", _stoppable(payload))
    monkeypatch.setattr(client, "post", post)
    monkeypatch.setattr(daemon, "_set_key", lambda _d, i, face: rig.drawn.__setitem__(i, face))
    monkeypatch.setattr(daemon, "POLL_S", 0.05)

    deck = FakeDeck()
    thread = _start_loop(deck)
    yield deck, rig
    _stop(thread)


def _tap(deck: FakeDeck, index: int) -> None:
    deck.press(index)
    deck.release(index)


def test_setup_opens_the_menu_and_done_closes_it(menu: tuple[FakeDeck, MenuRig]) -> None:
    deck, rig = menu
    _tap(deck, SETUP_KEY.index)
    assert _wait(lambda: rig.text(1) == "GAME"), "the menu never opened"
    assert rig.text(SETUP_KEY.index) == "DONE"
    _tap(deck, SETUP_KEY.index)
    assert _wait(lambda: rig.text(1) == "MATCH"), "DONE did not bring the main page back"
    assert rig.posted == []  # opening and closing a page asks nothing of the server


def test_a_setting_press_posts_and_the_menu_stays_open(menu: tuple[FakeDeck, MenuRig]) -> None:
    deck, rig = menu
    _tap(deck, SETUP_KEY.index)
    assert _wait(lambda: rig.text(1) == "GAME")
    _tap(deck, 2)  # (1,3) SHOT
    assert _wait(lambda: rig.posted == ["/api/setting/shot/15"])
    time.sleep(0.2)
    assert rig.text(1) == "GAME"


def test_an_empty_menu_cell_does_nothing(menu: tuple[FakeDeck, MenuRig]) -> None:
    """Keys are found on the page that is showing: (3,3) is P1 on the main
    page and nothing on the menu, so a tap there must not score a point."""
    deck, rig = menu
    _tap(deck, SETUP_KEY.index)
    assert _wait(lambda: rig.text(1) == "GAME")
    _tap(deck, P1_KEY.index)
    _tap(deck, 5)  # (2,1): empty on both pages
    time.sleep(0.3)
    assert rig.posted == []


def test_the_menu_closes_itself(
    menu: tuple[FakeDeck, MenuRig], monkeypatch: pytest.MonkeyPatch
) -> None:
    deck, rig = menu
    monkeypatch.setattr(daemon, "MENU_IDLE_S", 0.3)
    _tap(deck, SETUP_KEY.index)
    assert _wait(lambda: rig.text(1) == "GAME")
    assert _wait(lambda: rig.text(1) == "MATCH", 2.0), "the menu stayed open"


def test_opening_the_menu_drops_a_hold_in_progress(menu: tuple[FakeDeck, MenuRig]) -> None:
    """A P1 hold begun on the main page must not take a point back once the
    page has changed under the finger."""
    deck, rig = menu
    deck.press(P1_KEY.index)
    _tap(deck, SETUP_KEY.index)
    time.sleep(1.0)  # past the 0.6 s score hold
    deck.release(P1_KEY.index)
    time.sleep(0.2)
    assert rig.posted == []


# --- the LIVE key (#80): both directions are holds, judged on the face pressed ---

LIVE_KEY = next(k for k in LAYOUT if isinstance(k.action, LiveToggle))
LIVE_HOLD_S = LIVE_KEY.action.hold_s if isinstance(LIVE_KEY.action, LiveToggle) else 1.0


class FakeObs:
    """obs-websocket's stream requests, with a stream a test can move."""

    def __init__(self) -> None:
        self.active = False
        self.sent: list[str] = []
        self.lock = threading.Lock()

    def __call__(self, request_type: str, **_k: object) -> dict[str, object] | None:
        with self.lock:
            if request_type == "GetStreamStatus":
                return {
                    "outputActive": self.active,
                    "outputReconnecting": False,
                    "outputDuration": 0,
                }
            self.sent.append(request_type)
            self.active = request_type == "StartStream"
            return {}


@pytest.fixture
def streaming(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[FakeDeck, FakeObs]]:
    """A `_loop` on a running session whose OBS answers through `FakeObs`."""
    _STOP.clear()
    monkeypatch.setattr(client, "get_json", _stoppable({"state": "IDLE", "mode": "match"}))
    monkeypatch.setattr(daemon, "_set_key", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "POLL_S", 0.05)
    obs = FakeObs()
    deck = FakeDeck()
    session = SlowStopSession(stop_s=0.0)  # ON until stopped
    thread = _start_loop(deck, session, live=Stream(request=obs))  # type: ignore[arg-type]
    time.sleep(0.2)  # a poll has asked OBS and lit the key
    yield deck, obs
    _stop(thread)


def test_a_full_hold_goes_live_and_another_stops(
    streaming: tuple[FakeDeck, FakeObs],
) -> None:
    deck, obs = streaming
    deck.press(LIVE_KEY.index)
    assert _wait(lambda: obs.sent == ["StartStream"], LIVE_HOLD_S + SETTLE_S)
    deck.release(LIVE_KEY.index)
    time.sleep(0.2)  # the poll sees it on air
    deck.press(LIVE_KEY.index)
    assert _wait(lambda: obs.sent == ["StartStream", "StopStream"], LIVE_HOLD_S + SETTLE_S)
    deck.release(LIVE_KEY.index)


def test_a_tap_on_live_does_nothing(streaming: tuple[FakeDeck, FakeObs]) -> None:
    deck, obs = streaming
    deck.press(LIVE_KEY.index)
    time.sleep(LIVE_HOLD_S / 4)
    deck.release(LIVE_KEY.index)
    time.sleep(LIVE_HOLD_S + 0.3)
    assert obs.sent == []


def test_a_hold_to_stop_never_starts_a_stream_that_died_meanwhile(
    streaming: tuple[FakeDeck, FakeObs],
) -> None:
    deck, obs = streaming
    obs.active = True  # on air, started elsewhere
    time.sleep(0.2)
    deck.press(LIVE_KEY.index)  # held to stop...
    with obs.lock:
        obs.active = False  # ...and the stream ends on its own first
    time.sleep(LIVE_HOLD_S + 0.4)
    deck.release(LIVE_KEY.index)
    assert obs.sent == []


def test_live_is_dark_while_obs_is_not_answering(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no answer the key cannot do anything, so a hold must not either."""
    _STOP.clear()
    monkeypatch.setattr(client, "get_json", _stoppable({"state": "IDLE", "mode": "match"}))
    monkeypatch.setattr(daemon, "_set_key", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "POLL_S", 0.05)
    sent: list[str] = []

    def silent(request_type: str, **_k: object) -> None:
        sent.append(request_type)

    deck = FakeDeck()
    thread = _start_loop(deck, SlowStopSession(stop_s=0.0), live=Stream(request=silent))  # type: ignore[arg-type]
    try:
        deck.press(LIVE_KEY.index)
        time.sleep(LIVE_HOLD_S + 0.3)
        deck.release(LIVE_KEY.index)
        assert sent and set(sent) == {"GetStreamStatus"}
    finally:
        _stop(thread)
