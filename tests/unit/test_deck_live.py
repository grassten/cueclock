"""The LIVE key's stream machine (#80), driven by a fake obs-websocket.

The loop's half (holds, presses on a changed face) is in `test_deck_loop.py`;
this is what OBS's answers turn into, with nothing running.
"""

from __future__ import annotations

from typing import Any

from cueclock.deck import faces
from cueclock.deck.live import START_WAIT_S, STOP_WAIT_S, Stream


class Obs:
    """GetStreamStatus from fields a test sets; every other request recorded."""

    def __init__(self, **status: Any) -> None:
        self.answering = True
        self.refuse = False
        self.status: dict[str, Any] = {
            "outputActive": False,
            "outputReconnecting": False,
            "outputDuration": 0,
            **status,
        }
        self.sent: list[str] = []

    def __call__(self, request_type: str, **_k: object) -> dict[str, Any] | None:
        if not self.answering:
            return None
        if request_type == "GetStreamStatus":
            return dict(self.status)
        self.sent.append(request_type)
        return None if self.refuse else {}


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _stream(obs: Obs) -> tuple[Stream, Clock]:
    clock = Clock()
    return Stream(request=obs, now=clock), clock


def test_off_a_session_the_key_is_dark_and_obs_is_not_asked() -> None:
    obs = Obs()
    stream, _ = _stream(obs)
    assert stream.poll(session_on=False) is None
    assert obs.sent == []


def test_no_answer_from_obs_is_a_dark_key() -> None:
    obs = Obs()
    obs.answering = False
    stream, _ = _stream(obs)
    assert stream.poll(session_on=True) is None
    assert stream.intent() is None


def test_off_air_offers_the_hold() -> None:
    stream, _ = _stream(Obs())
    face = stream.poll(session_on=True)
    assert face is not None and (face.text, face.sub) == ("GO LIVE", "HOLD 1s")
    assert stream.intent() == "start"


def test_on_air_is_red_with_the_minutes() -> None:
    stream, _ = _stream(Obs(outputActive=True, outputDuration=28 * 60_000 + 59_000))
    face = stream.poll(session_on=True)
    assert face is not None and face.text == "LIVE\n28 MIN" and face.bg == faces.BG_LIVE
    assert stream.intent() == "stop"


def test_reconnecting_is_amber_and_still_stoppable() -> None:
    stream, _ = _stream(Obs(outputActive=True, outputReconnecting=True))
    face = stream.poll(session_on=True)
    assert face is not None and face.text == "LIVE\nRETRYING" and face.bg == faces.BG_WARN
    assert stream.intent() == "stop"


def test_a_start_shows_starting_until_obs_is_on_air() -> None:
    obs = Obs()
    stream, clock = _stream(obs)
    stream.poll(session_on=True)
    stream.act("start")
    assert obs.sent == ["StartStream"]
    face = stream.poll(session_on=True)
    assert face is not None and face.text == "STARTING…"
    assert stream.intent() is None  # nothing to hold for mid-start
    obs.status["outputActive"] = True
    clock.t = 1.0
    face = stream.poll(session_on=True)
    assert face is not None and face.text.startswith("LIVE")


def test_a_start_that_never_goes_on_air_stays_failed_until_the_next_hold() -> None:
    obs = Obs()
    stream, clock = _stream(obs)
    stream.poll(session_on=True)
    stream.act("start")
    clock.t = START_WAIT_S
    face = stream.poll(session_on=True)
    assert face is not None and face.text == "STREAM\nFAILED" and face.bg == faces.BG_ERR
    clock.t += 60.0
    assert stream.poll(session_on=True) == face  # sticky
    assert stream.intent() == "start"  # the hold retries
    stream.act("start")
    assert obs.sent == ["StartStream", "StartStream"]
    face = stream.poll(session_on=True)
    assert face is not None and face.text == "STARTING…"


def test_a_start_obs_refuses_is_failed_at_once() -> None:
    obs = Obs()
    obs.refuse = True
    stream, _ = _stream(obs)
    stream.poll(session_on=True)
    stream.act("start")
    face = stream.poll(session_on=True)
    assert face is not None and face.text == "STREAM\nFAILED"


def test_a_failure_does_not_outlive_the_session() -> None:
    obs = Obs()
    obs.refuse = True
    stream, _ = _stream(obs)
    stream.poll(session_on=True)
    stream.act("start")
    stream.poll(session_on=False)
    face = stream.poll(session_on=True)
    assert face is not None and face.text == "GO LIVE"


def test_a_stop_shows_stopping_until_obs_is_off_air() -> None:
    obs = Obs(outputActive=True)
    stream, _ = _stream(obs)
    stream.poll(session_on=True)
    stream.act("stop")
    assert obs.sent == ["StopStream"]
    face = stream.poll(session_on=True)
    assert face is not None and face.text == "STOPPING…"
    obs.status["outputActive"] = False
    face = stream.poll(session_on=True)
    assert face is not None and face.text == "GO LIVE"


def test_a_stop_obs_never_finishes_shows_the_truth_afterwards() -> None:
    obs = Obs(outputActive=True)
    stream, clock = _stream(obs)
    stream.poll(session_on=True)
    stream.act("stop")
    clock.t = STOP_WAIT_S
    face = stream.poll(session_on=True)
    assert face is not None and face.text.startswith("LIVE")  # still on air, and says so


def test_a_hold_meant_for_one_direction_never_does_the_other() -> None:
    # Held to stop, but the stream had already ended: nothing starts.
    obs = Obs(outputActive=True)
    stream, _ = _stream(obs)
    stream.poll(session_on=True)
    obs.status["outputActive"] = False
    stream.act("stop")
    assert obs.sent == []
    # Held to go live, but something else already had: nothing stops.
    obs.status["outputActive"] = True
    stream.act("start")
    assert obs.sent == []
