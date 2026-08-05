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

import queue
import threading
import time
from typing import Any

import pytest

from cueclock.deck import client, daemon, replay
from cueclock.deck.layout import LAYOUT, ReplayShow
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
def rig(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeDeck, Spy]:
    """A `_loop` running against a fake deck and a server that is up."""
    spy = Spy()
    monkeypatch.setattr(replay, "show", spy.show)
    monkeypatch.setattr(replay, "stop", spy.stop)
    monkeypatch.setattr(client, "get_json", lambda *a, **k: {"state": "IDLE", "mode": "match"})
    # PILHelper wants a real device; the faces are not what this file is about.
    monkeypatch.setattr(daemon, "_set_key", lambda *a, **k: None)

    deck = FakeDeck()
    thread = threading.Thread(
        target=daemon._loop, args=(deck, "http://x", SessionManager()), daemon=True
    )
    thread.start()
    deadline = time.monotonic() + SETTLE_S
    while deck.callback is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert deck.callback is not None, "loop never registered its key callback"
    return deck, spy


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
