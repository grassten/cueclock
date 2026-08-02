"""Instant replay: the cue-point arithmetic and the show/stop sequence.

The cue point is where the whole feature lives or dies — get the sign of the
display lag wrong and every replay starts after the shot it exists to show —
so it is a pure function with its own tests. `show()` is a sequence of
obs-websocket calls; the tests fake the client and assert the *order*, because
the seek has to land before the scene switch or the TV cuts to the wrong frame.
"""

from __future__ import annotations

from typing import Any

import pytest

from cueclock.deck import client, obsws, replay

# A 20 s buffer, pressed 4 s after the shot — the worked example in the docs.
DURATION = 20.0


# --- cue point ---


def test_seeks_to_just_before_the_shot() -> None:
    cue = replay.cue_point(DURATION, 4.0, lead_s=1.5, display_lag_s=0.1)
    assert cue.cursor_s == pytest.approx(14.6)  # 20 - 4 + 0.1 - 1.5
    assert cue.play_s == pytest.approx(5.4)


def test_lead_is_runway_before_the_shot_not_after() -> None:
    early = replay.cue_point(DURATION, 4.0, lead_s=3.0)
    late = replay.cue_point(DURATION, 4.0, lead_s=1.0)
    assert early.cursor_s < late.cursor_s
    assert early.play_s > late.play_s


def test_display_lag_pushes_the_cue_later_not_earlier() -> None:
    """The recording trails the detector, so the shot sits *later* in the file."""
    with_lag = replay.cue_point(DURATION, 4.0, display_lag_s=0.5)
    without = replay.cue_point(DURATION, 4.0, display_lag_s=0.0)
    assert with_lag.cursor_s == pytest.approx(without.cursor_s + 0.5)


def test_no_shot_this_session_plays_the_whole_buffer() -> None:
    cue = replay.cue_point(DURATION, None, max_play_s=20.0)
    assert cue.cursor_s == 0.0
    assert cue.play_s == pytest.approx(DURATION)


def test_a_shot_older_than_the_buffer_plays_from_the_start() -> None:
    # Pressed a minute after the last shot: nothing to seek to, but a press
    # still shows something rather than failing.
    cue = replay.cue_point(DURATION, 60.0)
    assert cue.cursor_s == 0.0


def test_pressing_the_instant_the_balls_stop_still_shows_footage() -> None:
    cue = replay.cue_point(DURATION, 0.2, min_play_s=2.0)
    assert cue.play_s == pytest.approx(2.0)
    assert cue.cursor_s == pytest.approx(18.0)


def test_a_long_gap_is_capped_rather_than_playing_everything() -> None:
    cue = replay.cue_point(60.0, 45.0, max_play_s=20.0)
    assert cue.play_s == pytest.approx(20.0)
    assert cue.cursor_s == pytest.approx(40.0)


def test_a_file_shorter_than_the_floor_never_seeks_past_its_start() -> None:
    cue = replay.cue_point(1.0, 0.1, min_play_s=2.0)
    assert cue.cursor_s == 0.0
    assert cue.play_s == pytest.approx(1.0)


def test_slow_motion_stretches_the_airtime_not_the_footage() -> None:
    full = replay.cue_point(DURATION, 4.0, speed=1.0)
    half = replay.cue_point(DURATION, 4.0, speed=0.5)
    assert half.cursor_s == full.cursor_s
    assert half.play_s == pytest.approx(full.play_s * 2)


# --- show() / stop() ---


class FakeObs:
    """Records every obs-websocket call `replay` makes, in order."""

    def __init__(self, *, scene: str = "Stream Detail", duration: float = DURATION) -> None:
        self.calls: list[str] = []
        self.scene = scene
        self.duration = duration
        self.saved_path: str | None = "/Users/steven/Movies/Replay 01.mov"
        self.new_path: str | None = "/Users/steven/Movies/Replay 02.mov"
        self.settings: dict[str, dict[str, Any]] = {}
        self.cursor_s: float = 0.0
        self.drop_seeks = 0  # how many seeks OBS swallows before honouring one
        self.snap_s = 0.3  # keyframe snap: a seek always lands slightly early

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def log(name: str, value: Any) -> Any:
            self.calls.append(name)
            return value

        monkeypatch.setattr(obsws, "current_scene", lambda: log("current_scene", self.scene))
        monkeypatch.setattr(obsws, "save_replay_buffer", lambda: log("save", True))
        monkeypatch.setattr(
            obsws,
            "last_replay_path",
            lambda: log(
                "last_path", self.saved_path if "save" not in self.calls else self.new_path
            ),
        )
        monkeypatch.setattr(
            obsws, "media_status", lambda name: log("status", (self.cursor_s, self.duration))
        )
        monkeypatch.setattr(
            obsws, "set_media_cursor", lambda name, s: log("cursor", self._cursor(s))
        )
        monkeypatch.setattr(
            obsws,
            "set_input_settings",
            lambda name, s: log(f"settings:{name}", self._settings(name, s)),
        )
        monkeypatch.setattr(obsws, "set_scene", lambda name: log("set_scene", self._scene(name)))

    def _cursor(self, seconds: float) -> bool:
        # OBS drops a seek silently while the source is still opening, and lands
        # on a keyframe (slightly early) once it takes. Both are modelled here
        # because both are what the real thing measurably does.
        if self.drop_seeks > 0:
            self.drop_seeks -= 1
            return True
        self.cursor_s = max(0.0, seconds - self.snap_s)
        return True

    def _settings(self, name: str, settings: dict[str, Any]) -> bool:
        self.settings[name] = settings
        return True

    def _scene(self, name: str) -> bool:
        self.scene = name
        return True


@pytest.fixture
def shot_4s_ago(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "get_json", lambda *a, **k: {"age_s": 4.0})


def test_show_cuts_to_the_replay_scene_and_remembers_the_way_back(
    monkeypatch: pytest.MonkeyPatch, shot_4s_ago: None
) -> None:
    fake = FakeObs()
    fake.install(monkeypatch)

    playing = replay.show("http://x", now=100.0)

    assert playing is not None
    assert playing.live_scene == "Stream Detail"
    assert fake.scene == replay.SCENE_NAME
    # 20 s file, shot 4 s back, 1.5 s lead, 0.1 s display lag -> 5.4 s on air.
    assert playing.until == pytest.approx(105.4)


def test_show_seeks_before_switching_scenes(
    monkeypatch: pytest.MonkeyPatch, shot_4s_ago: None
) -> None:
    """Order is the feature: seek after the switch and the TV cuts to frame 0."""
    fake = FakeObs()
    fake.install(monkeypatch)

    replay.show("http://x")

    assert fake.calls.index("cursor") < fake.calls.index("set_scene")
    assert fake.cursor_s == pytest.approx(14.6 - fake.snap_s)


def test_a_dropped_seek_is_re_asked_until_it_lands(
    monkeypatch: pytest.MonkeyPatch, shot_4s_ago: None
) -> None:
    """OBS swallows a seek issued before the source is ready, and says nothing.

    Measured 2026-08-02 against OBS 32: the first attempt vanished, the second
    landed ~70 ms later. Unverified, that failure is invisible — the replay just
    plays from the top.
    """
    fake = FakeObs()
    fake.install(monkeypatch)
    fake.drop_seeks = 3
    monkeypatch.setattr(replay, "POLL_S", 0.001)

    playing = replay.show("http://x")

    assert playing is not None
    assert fake.calls.count("cursor") == 4
    assert fake.cursor_s == pytest.approx(14.6 - fake.snap_s)


def test_a_seek_that_never_lands_keeps_the_tv_live(
    monkeypatch: pytest.MonkeyPatch, shot_4s_ago: None
) -> None:
    fake = FakeObs()
    fake.install(monkeypatch)
    fake.drop_seeks = 10_000
    monkeypatch.setattr(replay, "SEEK_TIMEOUT_S", 0.05)
    monkeypatch.setattr(replay, "POLL_S", 0.01)

    assert replay.show("http://x") is None
    assert fake.scene == "Stream Detail"


def test_a_seek_landing_early_on_a_keyframe_is_accepted(
    monkeypatch: pytest.MonkeyPatch, shot_4s_ago: None
) -> None:
    # Early is the harmless direction — extra runway before the shot, never
    # less — so it must not be mistaken for a dropped seek and re-asked.
    fake = FakeObs()
    fake.install(monkeypatch)
    fake.snap_s = replay.SEEK_SNAP_S - 0.1

    assert replay.show("http://x") is not None
    assert fake.calls.count("cursor") == 1


def test_show_loads_the_file_that_was_just_saved(
    monkeypatch: pytest.MonkeyPatch, shot_4s_ago: None
) -> None:
    fake = FakeObs()
    fake.install(monkeypatch)

    replay.show("http://x")

    assert fake.settings[replay.MEDIA_INPUT]["local_file"] == "/Users/steven/Movies/Replay 02.mov"


def test_show_hands_the_badge_its_own_duration(
    monkeypatch: pytest.MonkeyPatch, shot_4s_ago: None
) -> None:
    fake = FakeObs()
    fake.install(monkeypatch)

    replay.show("http://x")

    assert fake.settings[replay.BADGE_INPUT]["url"] == "http://x/replay?s=5.4"


def test_show_gives_up_without_touching_the_program_when_obs_is_unreachable(
    monkeypatch: pytest.MonkeyPatch, shot_4s_ago: None
) -> None:
    fake = FakeObs()
    fake.install(monkeypatch)
    monkeypatch.setattr(obsws, "current_scene", lambda: None)

    assert replay.show("http://x") is None
    assert "set_scene" not in fake.calls


def test_show_gives_up_when_the_buffer_never_lands(
    monkeypatch: pytest.MonkeyPatch, shot_4s_ago: None
) -> None:
    fake = FakeObs()
    fake.install(monkeypatch)
    fake.new_path = fake.saved_path  # OBS reports the same old file forever
    monkeypatch.setattr(replay, "SAVE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(replay, "POLL_S", 0.01)

    assert replay.show("http://x") is None
    assert fake.scene == "Stream Detail"


def test_show_gives_up_when_the_media_source_never_opens_the_file(
    monkeypatch: pytest.MonkeyPatch, shot_4s_ago: None
) -> None:
    fake = FakeObs()
    fake.install(monkeypatch)
    monkeypatch.setattr(obsws, "media_status", lambda name: None)
    monkeypatch.setattr(replay, "LOAD_TIMEOUT_S", 0.05)
    monkeypatch.setattr(replay, "POLL_S", 0.01)

    assert replay.show("http://x") is None
    assert fake.scene == "Stream Detail"


def test_show_refuses_to_start_a_replay_from_inside_one(
    monkeypatch: pytest.MonkeyPatch, shot_4s_ago: None
) -> None:
    # A daemon restart mid-replay leaves OBS in the Replay scene with no handle;
    # cutting again would record Replay as the scene to "return" to.
    fake = FakeObs(scene=replay.SCENE_NAME)
    fake.install(monkeypatch)

    assert replay.show("http://x") is None
    assert "save" not in fake.calls


def test_show_survives_a_server_that_cannot_say_when_the_shot_was(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeObs()
    fake.install(monkeypatch)
    monkeypatch.setattr(client, "get_json", lambda *a, **k: None)

    playing = replay.show("http://x", now=0.0)

    assert playing is not None
    assert fake.cursor_s == 0.0  # whole buffer rather than a wrong guess


def test_show_reads_the_anchor_before_saving(monkeypatch: pytest.MonkeyPatch) -> None:
    """The age has to be measured against the file's end, which is the save."""
    fake = FakeObs()
    fake.install(monkeypatch)
    monkeypatch.setattr(
        client, "get_json", lambda *a, **k: fake.calls.append("age") or {"age_s": 4.0}
    )

    replay.show("http://x")

    assert fake.calls.index("age") < fake.calls.index("save")


def test_stop_returns_to_the_scene_that_was_live(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeObs()
    fake.install(monkeypatch)
    playing = replay.Playing(live_scene="Stream Detail", until=0.0)
    fake.scene = replay.SCENE_NAME

    assert replay.stop(playing)
    assert fake.scene == "Stream Detail"
