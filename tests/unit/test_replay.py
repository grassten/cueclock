"""Instant replay: the cue-point arithmetic and the show/stop sequence.

The cue point is where the whole feature lives or dies — get the sign of the
display lag wrong and every replay starts after the shot it exists to show —
so it is a pure function with its own tests. `show()` is a sequence of
obs-websocket calls; the tests fake the client and assert the *order*, because
the seek has to land before the scene switch or the TV cuts to the wrong frame.
"""

from __future__ import annotations

import time
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


# --- masking the burned-in shot clock ---

# The corner clock as OBS reports it on the rig (probed 2026-08-02 against the
# live `Stream Detail` scene): top-left aligned, unrotated, 193x193 at 1141,0.
CORNER_CLOCK: dict[str, Any] = {
    "positionX": 1141.0,
    "positionY": 0.0,
    "width": 193.0,
    "height": 193.0,
    "alignment": 5,  # left | top
    "rotation": 0.0,
}


def full_canvas() -> dict[str, Any]:
    return {
        "positionX": 0.0,
        "positionY": 0.0,
        "width": 1920.0,
        "height": 1080.0,
        "alignment": 5,
        "rotation": 0.0,
    }


def browser_item(name: str, transform: dict[str, Any], *, enabled: bool = True) -> dict[str, Any]:
    return {
        "sourceName": name,
        "inputKind": "browser_source",
        "sceneItemEnabled": enabled,
        "sceneItemTransform": transform,
    }


def clock_page(name: str) -> str | None:
    return "http://localhost:8000/shotclock" if name == "clock" else None


def test_the_mask_covers_the_clock_rect_as_a_fraction_of_the_canvas() -> None:
    masks = replay.clock_masks([browser_item("clock", CORNER_CLOCK)], (1920, 1080), clock_page)
    assert len(masks) == 1
    # 1141/1920 = 59.43%, 193/1080 = 17.87%, plus the 2 px pad on every side.
    assert masks[0].x == pytest.approx(100 * 1139 / 1920, abs=0.01)
    assert masks[0].y == 0.0  # clamped: the clock starts at the canvas edge
    assert masks[0].w == pytest.approx(100 * 197 / 1920, abs=0.01)
    assert masks[0].h == pytest.approx(100 * 195 / 1080, abs=0.01)


def test_the_mask_is_never_smaller_than_the_thing_it_hides() -> None:
    """A seam of live countdown on the wall is the whole failure being fixed."""
    mask = replay.clock_masks([browser_item("clock", CORNER_CLOCK)], (1920, 1080), clock_page)[0]
    assert mask.x <= 100 * 1141 / 1920
    assert mask.x + mask.w >= 100 * (1141 + 193) / 1920
    assert mask.y + mask.h >= 100 * 193 / 1080


def test_only_the_clock_page_is_masked() -> None:
    """The scoreboard is a full-canvas browser source too — masking it by
    source kind alone would black out the entire replay."""
    items = [browser_item("score", full_canvas()), browser_item("clock", CORNER_CLOCK)]
    masks = replay.clock_masks(items, (1920, 1080), clock_page)
    assert len(masks) == 1
    assert masks[0].w < 20.0


def test_a_hidden_clock_is_not_masked() -> None:
    # It was never burned into the recording, so there is nothing to cover.
    items = [browser_item("clock", CORNER_CLOCK, enabled=False)]
    assert replay.clock_masks(items, (1920, 1080), clock_page) == []


def test_a_second_copy_of_the_clock_gets_its_own_mask() -> None:
    """Identity is the page, not the source name — that is what stops the mask
    drifting from the overlay the way a hardcoded rect would."""
    items = [
        browser_item("clock", CORNER_CLOCK),
        browser_item("clock", {**CORNER_CLOCK, "positionX": 10.0, "positionY": 400.0}),
    ]
    assert len(replay.clock_masks(items, (1920, 1080), clock_page)) == 2


def test_a_centred_item_is_measured_from_its_middle() -> None:
    centred = {**CORNER_CLOCK, "positionX": 960.0, "positionY": 540.0, "alignment": 0}
    mask = replay.clock_masks([browser_item("clock", centred)], (1920, 1080), clock_page)[0]
    assert mask.x == pytest.approx(100 * (960 - 193 / 2 - 2) / 1920, abs=0.01)
    assert mask.y == pytest.approx(100 * (540 - 193 / 2 - 2) / 1080, abs=0.01)


def test_a_rotated_item_is_covered_by_its_bounding_box() -> None:
    """The overhead strip's own transform: rotated 270 about a top-left anchor.

    Nothing rotates the clock today; getting this wrong would show up as a mask
    in the wrong place entirely, so it is pinned against a rect measured off
    the live scene rather than derived the same way twice.
    """
    strip = {
        "positionX": 1334.0,
        "positionY": 1083.0,
        "width": 1094.0,
        "height": 594.0,
        "alignment": 5,
        "rotation": 270.0,
    }
    mask = replay.item_mask(strip, 1920.0, 1080.0)
    assert mask is not None
    assert mask.x == pytest.approx(100 * 1332 / 1920, abs=0.01)  # 1334 - pad
    assert mask.y == 0.0  # the strip starts 11 px above the canvas
    assert mask.x + mask.w == 100.0  # and runs off its right edge


def test_an_item_entirely_off_canvas_is_not_masked() -> None:
    offscreen = {**CORNER_CLOCK, "positionX": -400.0}
    assert replay.item_mask(offscreen, 1920.0, 1080.0) is None


def test_a_zero_sized_item_is_not_masked() -> None:
    assert replay.item_mask({**CORNER_CLOCK, "width": 0.0}, 1920.0, 1080.0) is None


def test_the_badge_url_carries_no_mask_when_there_is_nothing_to_cover() -> None:
    assert replay.badge_url("http://x", 5.4, []) == "http://x/replay?s=5.4"


def test_the_badge_url_separates_masks_with_semicolons() -> None:
    masks = [replay.Mask(1.0, 2.0, 3.0, 4.0), replay.Mask(5.0, 6.0, 7.0, 8.0)]
    url = replay.badge_url("http://x", 5.4, masks)
    assert url == "http://x/replay?s=5.4&mask=1.00,2.00,3.00,4.00;5.00,6.00,7.00,8.00"


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
        self.canvas: tuple[int, int] | None = (1920, 1080)
        # The live scene as the rig actually has it (probed 2026-08-02): the
        # corner clock at 1141,0 sized 193x193, the scoreboard filling the
        # canvas, and the overhead strip rotated 270 down the right.
        self.items: list[dict[str, Any]] | None = [
            browser_item("Browser 2", CORNER_CLOCK),
            browser_item("Browser 3", full_canvas()),
            {
                "sourceName": "Syphon Client",
                "inputKind": "syphon-input",
                "sceneItemEnabled": True,
                "sceneItemTransform": {
                    "positionX": 1334.0,
                    "positionY": 1083.0,
                    "width": 1094.0,
                    "height": 594.0,
                    "alignment": 5,
                    "rotation": 270.0,
                },
            },
        ]
        self.urls = {
            "Browser 2": "http://localhost:8000/shotclock",
            "Browser 3": "http://localhost:8000/score",
        }

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def log(name: str, value: Any) -> Any:
            self.calls.append(name)
            return value

        monkeypatch.setattr(obsws, "current_scene", lambda: log("current_scene", self.scene))
        monkeypatch.setattr(obsws, "canvas_size", lambda: log("canvas", self.canvas))
        monkeypatch.setattr(obsws, "scene_items", lambda name: log("items", self.items))
        monkeypatch.setattr(obsws, "input_url", lambda name: self.urls.get(name))
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
        monkeypatch.setattr(obsws, "refresh_browser", lambda name: log(f"refresh:{name}", True))
        # No test should pay for the badge's real load time; the one test that
        # is *about* the wait sets its own value after installing this.
        monkeypatch.setattr(replay, "BADGE_PAINT_S", 0.0)

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


def test_the_badge_timeline_covers_its_own_invisible_head_start(
    monkeypatch: pytest.MonkeyPatch, shot_4s_ago: None
) -> None:
    """The page animates from the moment it is handed the URL, which is before
    the TV can see it — so its timeline is the airtime plus that head start.

    Hand it the bare airtime and the drain bar empties, the countdown reaches
    zero and the fade to black completes half a second early, leaving the wall
    on black waiting for a deck that has not cut yet.
    """
    fake = FakeObs()
    fake.install(monkeypatch)
    monkeypatch.setattr(replay, "BADGE_PAINT_S", 0.6)

    playing = replay.show("http://x", now=100.0)

    assert playing is not None
    assert playing.until == pytest.approx(105.4)  # the airtime is unchanged
    assert fake.settings[replay.BADGE_INPUT]["url"].startswith("http://x/replay?s=6.0&")


def test_the_badge_is_loaded_and_refreshed_before_the_cut(
    monkeypatch: pytest.MonkeyPatch, shot_4s_ago: None
) -> None:
    """The page's own clock starts when it paints, so it has to paint first.

    Measured 2026-08-02: a reload takes ~0.57 s and OBS shows the *previous*
    frame throughout — which, now that the badge ends on black, is a black one.
    """
    fake = FakeObs()
    fake.install(monkeypatch)
    monkeypatch.setattr(replay, "BADGE_PAINT_S", 0.0)

    replay.show("http://x")

    assert fake.calls.index(f"settings:{replay.BADGE_INPUT}") < fake.calls.index("set_scene")
    assert fake.calls.index(f"refresh:{replay.BADGE_INPUT}") < fake.calls.index("set_scene")
    # Behind the seek, not in front of it: the load hides under work already
    # being done, so the press does not cost the whole paint time.
    assert fake.calls.index(f"refresh:{replay.BADGE_INPUT}") < fake.calls.index("cursor")


def test_the_cut_waits_for_the_badge_to_have_painted(
    monkeypatch: pytest.MonkeyPatch, shot_4s_ago: None
) -> None:
    elapsed: list[float] = []
    fake = FakeObs()
    fake.install(monkeypatch)
    monkeypatch.setattr(replay, "BADGE_PAINT_S", 0.25)
    started = time.monotonic()
    monkeypatch.setattr(
        obsws, "set_scene", lambda name: elapsed.append(time.monotonic() - started) or True
    )

    replay.show("http://x")

    assert elapsed and elapsed[0] >= 0.25


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


def test_show_hands_the_badge_its_own_duration_and_the_clock_to_cover(
    monkeypatch: pytest.MonkeyPatch, shot_4s_ago: None
) -> None:
    fake = FakeObs()
    fake.install(monkeypatch)

    replay.show("http://x")

    url = fake.settings[replay.BADGE_INPUT]["url"]
    assert url == "http://x/replay?s=5.4&mask=59.32,0.00,10.26,18.06"


def test_show_measures_the_live_scene_not_the_replay_scene(
    monkeypatch: pytest.MonkeyPatch, shot_4s_ago: None
) -> None:
    """The mask describes what was burned into the buffer — the live layout."""
    fake = FakeObs()
    fake.install(monkeypatch)
    seen: list[str] = []
    monkeypatch.setattr(obsws, "scene_items", lambda name: seen.append(name) or fake.items)

    replay.show("http://x")

    assert seen == ["Stream Detail"]


def test_show_measures_the_scene_before_reading_the_shot_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing may sit between the anchor read and the save it is measured
    against — three extra OBS round trips there would age every cue point."""
    fake = FakeObs()
    fake.install(monkeypatch)
    monkeypatch.setattr(
        client, "get_json", lambda *a, **k: fake.calls.append("age") or {"age_s": 4.0}
    )

    replay.show("http://x")

    assert fake.calls.index("items") < fake.calls.index("age")


def test_a_replay_still_plays_when_obs_cannot_say_where_the_clock_is(
    monkeypatch: pytest.MonkeyPatch, shot_4s_ago: None
) -> None:
    # Degrading to checkpoint 46's visible-clock behaviour beats refusing the
    # cut: that was the old bug, not a new one.
    fake = FakeObs()
    fake.install(monkeypatch)
    fake.items = None

    playing = replay.show("http://x")

    assert playing is not None
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
