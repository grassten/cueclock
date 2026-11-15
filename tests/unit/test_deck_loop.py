"""The deck loop's press routing — the part a key press decides, not a face.

Everything else about the deck is either pure data (`test_deck.py`) or HID,
which is exercised live like the camera. This file sits between them: it drives
`_loop` with a fake device so the gestures that *dispatch* differently can be
told apart without a finger.

It exists because the Replay key grew a second span (2026-08-05). Tap and hold
now route to different anchors, a tap fires on **release** where every other
command key fires on press, and a mature hold must not also fire a tap when the
finger comes off — three things no face test can see, and whose failure mode at
the table is a key that quietly does the wrong replay.
"""

from __future__ import annotations

import contextlib
import queue
import threading
import time
from collections.abc import Iterator
from typing import Any

import pytest

from cueclock.deck import bracket as deck_bracket
from cueclock.deck import client, daemon, replay
from cueclock.deck.layout import (
    LAYOUT,
    BracketConfirm,
    Recalibrate,
    ReplayShow,
    SceneToggle,
    SessionToggle,
)
from cueclock.deck.recal import Recalibrator
from cueclock.deck.session import SessionManager

REPLAY_KEY = next(k for k in LAYOUT if isinstance(k.action, ReplayShow))
HOLD_S = REPLAY_KEY.action.hold_s if isinstance(REPLAY_KEY.action, ReplayShow) else 0.6
SETTLE_S = 2.0  # generous: the loop's event wait is 0.25 s


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
) -> threading.Thread:
    """`_loop` in a thread that ends quietly when `_STOP` is set."""

    def run() -> None:
        # KeyboardInterrupt out of the poll is the loop's own documented exit;
        # swallowing it here is what keeps the thread from dying "unhandled".
        with contextlib.suppress(KeyboardInterrupt):
            daemon._loop(deck, "http://x", session or SessionManager(), recal)

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


class Spy:
    """Records the spans `_loop` asked for, and fakes a replay being on air."""

    def __init__(self) -> None:
        self.spans: list[replay.Span] = []
        self.stops = 0
        self.started = threading.Event()

    def show(self, base: str, *, span: replay.Span = replay.Span.SHOT) -> replay.Playing:
        self.spans.append(span)
        self.started.set()
        # Far enough out that the loop's own auto-return can't fire mid-test.
        return replay.Playing(live_scene="Stream Detail", until=time.monotonic() + 3600.0)

    def stop(self, playing: replay.Playing) -> bool:
        self.stops += 1
        return True


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[FakeDeck, Spy]]:
    """A `_loop` running against a fake deck and a server that is up."""
    spy = Spy()
    _STOP.clear()
    monkeypatch.setattr(replay, "show", spy.show)
    monkeypatch.setattr(replay, "stop", spy.stop)
    monkeypatch.setattr(client, "get_json", _stoppable({"state": "IDLE", "mode": "match"}))
    # PILHelper wants a real device; the faces are not what this file is about.
    monkeypatch.setattr(daemon, "_set_key", lambda *a, **k: None)
    # Nothing here is timed against the poll, and the loop only notices it
    # should stop on its next one — at the stock 2 s that is the whole teardown.
    monkeypatch.setattr(daemon, "POLL_S", 0.05)

    deck = FakeDeck()
    thread = _start_loop(deck)
    yield deck, spy
    _stop(thread)


def _wait(cond: Any, timeout: float = SETTLE_S) -> bool:
    """Spin until `cond()` or the timeout — for what the loop does on its own."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


def _await(spy: Spy, timeout: float = SETTLE_S) -> None:
    assert spy.started.wait(timeout), "no replay was started"


def test_a_tap_plays_the_last_shot(rig: tuple[FakeDeck, Spy]) -> None:
    deck, spy = rig
    deck.press(REPLAY_KEY.index)
    deck.release(REPLAY_KEY.index)
    _await(spy)
    assert spy.spans == [replay.Span.SHOT]


def test_a_tap_fires_on_release_not_on_the_press(rig: tuple[FakeDeck, Spy]) -> None:
    """Held down, the key must stay undecided — the press alone means nothing."""
    deck, spy = rig
    deck.press(REPLAY_KEY.index)
    time.sleep(HOLD_S / 2)
    assert spy.spans == []
    deck.release(REPLAY_KEY.index)
    _await(spy)
    assert spy.spans == [replay.Span.SHOT]


def test_a_hold_plays_the_rack_and_the_release_adds_nothing(
    rig: tuple[FakeDeck, Spy],
) -> None:
    """The failure this guards: a hold that also fires a tap when the finger
    lifts, which would start a second replay from inside the first."""
    deck, spy = rig
    deck.press(REPLAY_KEY.index)
    _await(spy, HOLD_S + SETTLE_S)
    assert spy.spans == [replay.Span.RACK]
    deck.release(REPLAY_KEY.index)
    time.sleep(0.5)
    assert spy.spans == [replay.Span.RACK]


def test_the_hold_fires_under_the_finger(rig: tuple[FakeDeck, Spy]) -> None:
    """A hold that waited for the lift would just feel like a slow tap."""
    deck, spy = rig
    started = time.monotonic()
    deck.press(REPLAY_KEY.index)
    _await(spy, HOLD_S + SETTLE_S)
    assert time.monotonic() - started < HOLD_S + SETTLE_S


def test_a_press_during_a_replay_goes_back_to_live(rig: tuple[FakeDeck, Spy]) -> None:
    deck, spy = rig
    deck.press(REPLAY_KEY.index)
    deck.release(REPLAY_KEY.index)
    _await(spy)

    deck.press(REPLAY_KEY.index)  # mid-replay the key is BACK
    deadline = time.monotonic() + SETTLE_S
    while spy.stops == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert spy.stops == 1
    deck.release(REPLAY_KEY.index)
    time.sleep(0.5)
    # The release must not arm a hold, and must not start anything.
    assert spy.spans == [replay.Span.SHOT]
    assert spy.stops == 1


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

    **The loop is stopped on the way out**, unlike `rig` above, because these
    tests assert that nothing happened *later* — and a leaked loop outlives its
    test while `bracket.hide` still resolves to whichever spy is patched in
    now, so the next test sees a scene change nobody in it made. The loop's own
    documented exit is an exception out of the poll.
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
    dispatches nothing — the Replay key included, hold or tap."""
    spy = Spy()
    monkeypatch.setattr(replay, "show", spy.show)
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

    deck.press(REPLAY_KEY.index)
    time.sleep(HOLD_S + 0.5)
    deck.release(REPLAY_KEY.index)
    assert spy.spans == []
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
