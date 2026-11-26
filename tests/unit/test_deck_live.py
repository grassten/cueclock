"""The LIVE key's stream machine (#80, #81), driven by a fake obs-websocket
and a fake server.

The loop's half (holds, presses on a changed face) is in `test_deck_loop.py`;
this is what OBS's and the server's answers turn into, with nothing running.
"""

from __future__ import annotations

from typing import Any

from cueclock.deck import faces
from cueclock.deck.live import PREPARE_WAIT_S, START_WAIT_S, Stream


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


class Server:
    """The server's `stream` object (#81), and every post the key makes to it."""

    def __init__(self, **stream: Any) -> None:
        self.stream: dict[str, Any] = {
            "account": "ok",
            "phase": "idle",
            "on_air": False,
            "visibility": "unlisted",
            "error": None,
            **stream,
        }
        self.posts: list[str] = []
        self.refuse = False

    def __call__(self, path: str) -> bool:
        self.posts.append(path)
        if self.refuse:
            return False
        if path == "/api/stream/prepare":
            self.stream["phase"] = "preparing"
        elif path.startswith("/api/stream/on_air"):
            self.stream["on_air"] = True
        elif path == "/api/stream/off_air":
            self.stream["on_air"] = False
        return True


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class Rig:
    def __init__(self, obs: Obs | None = None, server: Server | None = None) -> None:
        self.obs = obs or Obs()
        self.server = server or Server()
        self.clock = Clock()
        self.stream = Stream(request=self.obs, post=self.server, now=self.clock)

    def poll(self) -> faces.Face | None:
        return self.stream.poll(True, self.server.stream)

    def on_air(self) -> None:
        """A whole hold, to the stream being on air."""
        self.poll()
        self.stream.act("start")
        self.server.stream["phase"] = "ready"
        self.poll()
        self.obs.status["outputActive"] = True
        self.poll()


def _text(face: faces.Face | None) -> str:
    assert face is not None
    return face.text


# ---------- what the key says ----------


def test_off_a_session_the_key_is_dark_and_obs_is_not_asked() -> None:
    rig = Rig()
    assert rig.stream.poll(session_on=False) is None
    assert rig.obs.sent == [] and rig.server.posts == []


def test_no_answer_from_obs_is_a_dark_key() -> None:
    rig = Rig()
    rig.obs.answering = False
    assert rig.poll() is None
    assert rig.stream.intent() is None


def test_off_air_says_what_a_hold_will_do_and_to_whom() -> None:
    rig = Rig()
    face = rig.poll()
    assert face is not None and (face.text, face.sub) == ("GO LIVE\nUNLISTED", "HOLD 1s")
    assert rig.stream.intent() == "start"
    rig.server.stream["visibility"] = "public"
    assert _text(rig.poll()) == "GO LIVE\nPUBLIC"


def test_signed_out_the_key_says_so_and_a_hold_does_nothing() -> None:
    rig = Rig(server=Server(account="signed_out"))
    face = rig.poll()
    assert face is not None and (face.text, face.sub) == ("SIGN IN", "ON PHONE")
    assert rig.stream.intent() is None
    rig.server.stream["account"] = "ok"  # signed in on the phone: back on its own
    assert _text(rig.poll()) == "GO LIVE\nUNLISTED"
    assert rig.stream.intent() == "start"


def test_with_no_youtube_account_on_the_machine_the_key_names_that() -> None:
    for server in (Server(account="unconfigured"), None):
        rig = Rig()
        face = rig.stream.poll(True, server.stream if server else None)  # None: an older server
        assert face is not None and (face.text, face.sub) == ("NO\nYOUTUBE", "SEE PHONE")
        assert rig.stream.intent() is None


def test_on_air_is_red_with_the_minutes() -> None:
    rig = Rig(Obs(outputActive=True, outputDuration=28 * 60_000 + 59_000), Server(on_air=True))
    face = rig.poll()
    assert face is not None and face.text == "LIVE\n28 MIN" and face.bg == faces.BG_LIVE
    assert rig.stream.intent() == "stop"


def test_reconnecting_is_amber_and_still_stoppable() -> None:
    rig = Rig(Obs(outputActive=True, outputReconnecting=True), Server(on_air=True))
    face = rig.poll()
    assert face is not None and face.text == "LIVE\nRETRYING" and face.bg == faces.BG_WARN
    assert rig.stream.intent() == "stop"


# ---------- going live: the video first, then OBS ----------


def test_a_hold_asks_for_the_video_and_starts_obs_only_when_it_is_ready() -> None:
    rig = Rig()
    rig.poll()
    rig.stream.act("start")
    assert rig.server.posts == ["/api/stream/prepare"]
    assert rig.obs.sent == []  # YouTube has not answered: OBS must not start
    assert _text(rig.poll()) == "STARTING…"
    assert rig.stream.intent() is None and rig.stream.busy
    assert rig.obs.sent == []
    rig.server.stream["phase"] = "ready"
    assert _text(rig.poll()) == "STARTING…"
    assert rig.obs.sent == ["StartStream"]
    rig.obs.status["outputActive"] = True
    rig.clock.t = 1.0
    assert _text(rig.poll()).startswith("LIVE")
    assert not rig.stream.busy


def test_youtube_refusing_never_starts_obs_and_stays_on_the_key() -> None:
    rig = Rig()
    rig.poll()
    rig.stream.act("start")
    rig.server.stream.update(phase="failed", error="Creating the stream: YouTube did not answer")
    face = rig.poll()
    assert face is not None and face.text == "YOUTUBE\nFAILED" and face.bg == faces.BG_ERR
    assert face.sub == "HOLD 1s"
    assert rig.obs.sent == []
    rig.clock.t += 60.0
    assert rig.poll() == face  # sticky
    assert rig.stream.intent() == "start"  # the hold retries
    rig.stream.act("start")
    assert rig.server.posts == ["/api/stream/prepare", "/api/stream/prepare"]
    assert _text(rig.poll()) == "STARTING…"


def test_a_server_that_refuses_the_hold_fails_it_at_once() -> None:
    rig = Rig()
    rig.server.refuse = True
    rig.poll()
    rig.stream.act("start")
    assert _text(rig.poll()) == "YOUTUBE\nFAILED"
    assert rig.obs.sent == []


def test_a_video_that_never_comes_is_given_up_on_and_cancelled() -> None:
    rig = Rig()
    rig.poll()
    rig.stream.act("start")
    rig.clock.t = PREPARE_WAIT_S - 1
    assert _text(rig.poll()) == "STARTING…"
    rig.clock.t = PREPARE_WAIT_S
    assert _text(rig.poll()) == "YOUTUBE\nFAILED"
    assert rig.server.posts[-1] == "/api/stream/cancel"
    assert rig.obs.sent == []


def test_a_start_that_never_goes_on_air_is_stopped_and_its_video_taken_back() -> None:
    rig = Rig()
    rig.poll()
    rig.stream.act("start")
    rig.server.stream["phase"] = "ready"
    rig.poll()
    rig.clock.t = START_WAIT_S
    face = rig.poll()
    assert face is not None and face.text == "STREAM\nFAILED" and face.bg == faces.BG_ERR
    # OBS is told to stop dialling before the video goes: connecting with no
    # video behind it would have YouTube make one with the last settings.
    assert rig.obs.sent == ["StartStream", "StopStream"]
    assert rig.server.posts[-1] == "/api/stream/cancel"
    rig.clock.t += 60.0
    assert rig.poll() == face  # sticky
    assert rig.stream.intent() == "start"


def test_a_start_obs_refuses_is_failed_at_once_and_its_video_taken_back() -> None:
    rig = Rig()
    rig.obs.refuse = True
    rig.poll()
    rig.stream.act("start")
    rig.server.stream["phase"] = "ready"
    assert _text(rig.poll()) == "STREAM\nFAILED"
    assert rig.server.posts[-1] == "/api/stream/cancel"


def test_a_failure_does_not_outlive_the_session() -> None:
    rig = Rig()
    rig.server.refuse = True
    rig.poll()
    rig.stream.act("start")
    rig.stream.poll(session_on=False)
    assert _text(rig.poll()) == "GO LIVE\nUNLISTED"


def test_signing_out_shows_over_an_old_failure() -> None:
    rig = Rig()
    rig.poll()
    rig.stream.act("start")
    rig.server.stream.update(phase="failed", account="signed_out")
    assert _text(rig.poll()) == "SIGN IN"  # a hold could not retry anyway
    assert rig.stream.intent() is None


# ---------- telling the server what OBS says ----------


def test_the_server_is_told_when_the_stream_goes_on_air_and_comes_off() -> None:
    rig = Rig()
    rig.on_air()
    assert rig.server.posts == ["/api/stream/prepare", "/api/stream/on_air?duration_ms=0"]
    rig.poll()
    rig.poll()
    assert len(rig.server.posts) == 2  # they agree: nothing more is said
    rig.obs.status["outputActive"] = False  # it ended on its own
    assert _text(rig.poll()) == "GO LIVE\nUNLISTED"
    assert rig.server.posts[-1] == "/api/stream/off_air"


def test_a_daemon_restarted_mid_stream_tells_the_server_how_long_it_has_run() -> None:
    rig = Rig(Obs(outputActive=True, outputDuration=754_000), Server())  # the server lost track
    rig.poll()
    assert rig.server.posts == ["/api/stream/on_air?duration_ms=754000"]


def test_a_server_with_no_stream_desk_is_told_nothing() -> None:
    rig = Rig(Obs(outputActive=True))
    rig.stream.poll(True, None)
    assert rig.server.posts == []


# ---------- stopping ----------


def test_a_stop_shows_stopping_until_obs_is_off_air() -> None:
    rig = Rig()
    rig.on_air()
    rig.stream.act("stop")
    assert rig.obs.sent == ["StartStream", "StopStream"]
    assert _text(rig.poll()) == "STOPPING…"
    rig.obs.status["outputActive"] = False
    assert _text(rig.poll()) == "GO LIVE\nUNLISTED"
    assert rig.server.posts[-1] == "/api/stream/off_air"


def test_a_hold_to_stop_does_nothing_once_the_stream_has_died() -> None:
    rig = Rig(Obs(outputActive=True), Server(on_air=True))
    rig.poll()
    assert rig.stream.intent() == "stop"
    rig.obs.status["outputActive"] = False  # the stream ends while the key is held
    rig.stream.act("stop")
    assert rig.obs.sent == [] and "/api/stream/prepare" not in rig.server.posts


def test_a_hold_to_start_does_nothing_once_the_stream_is_up() -> None:
    rig = Rig()
    rig.poll()
    rig.obs.status["outputActive"] = True
    rig.stream.act("start")
    assert rig.obs.sent == [] and rig.server.posts == []


def test_obs_gone_when_the_hold_matures_does_nothing() -> None:
    rig = Rig()
    rig.poll()
    rig.obs.answering = False
    rig.stream.act("start")
    assert rig.obs.sent == [] and rig.server.posts == []
    assert rig.poll() is None
