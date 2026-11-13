"""Side-camera settings: the session-start enforce step, against a fake helper."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from cueclock.deck import sidecam

SDK_LOG = "[11-12 23:08:32.477]  88019 17907165 I uvc_camera_control_imp.mm:0631:  rc1: -5368\n"


class FakeCamera:
    """Speaks the `link-settings` protocol: `get`, and `set k=v ...` then a read-back."""

    def __init__(
        self, settings: dict[str, Any], stream_open: bool = True, refuses: Sequence[str] = ()
    ) -> None:
        self.settings = dict(settings)
        self.stream_open = stream_open
        self.refuses = set(refuses)
        self.calls: list[list[str]] = []
        self.reachable = True

    def __call__(self, args: Sequence[str]) -> str | None:
        self.calls.append(list(args))
        if not self.reachable:
            return None
        report: dict[str, Any] = {}
        if args[0] == "set":
            applied = {}
            for arg in args[1:]:
                key, value = arg.split("=", 1)
                applied[key] = key not in self.refuses
                if applied[key]:
                    self.settings[key] = json.loads(value)
            report["applied"] = applied
        report["camera"] = {"name": "Insta360 Link 2C Pro", "stream_open": self.stream_open}
        report["settings"] = self.settings
        # The real SDK logs to stdout ahead of the JSON line.
        return SDK_LOG + json.dumps(report) + "\n"

    @property
    def sets(self) -> list[list[str]]:
        return [call for call in self.calls if call[0] == "set"]


SAVED = {"auto_exposure": False, "shutter": 250, "ev": -0.9, "noise_reduction": False}


@pytest.fixture
def config(tmp_path: Path) -> Path:
    path = tmp_path / "sidecam.json"
    sidecam.save_config(SAVED, path)
    return path


def test_read_skips_the_sdk_log_lines_ahead_of_the_json() -> None:
    report = sidecam.read(FakeCamera({"ev": -0.9}))
    assert report is not None
    assert report["settings"] == {"ev": -0.9}


def test_saving_drops_unread_values_and_values_an_auto_mode_owns() -> None:
    # Auto exposure keeps changing shutter and ISO; pinning a snapshot of them
    # would fight the camera, so only manual modes keep their values.
    reading = {
        "auto_exposure": True,
        "shutter": 60,
        "iso": 429,
        "wb_auto": False,
        "wb_kelvin": 5000,
        "autofocus": False,
        "focus": 85,
        "hdr": None,
    }
    assert sidecam.wanted_from(reading) == {
        "auto_exposure": True,
        "wb_auto": False,
        "wb_kelvin": 5000,
        "autofocus": False,
        "focus": 85,
    }


def test_nothing_saved_means_nothing_to_enforce(tmp_path: Path) -> None:
    camera = FakeCamera({})
    assert sidecam.ensure(camera, tmp_path / "absent.json") is True
    assert camera.calls == []


def test_an_unreachable_camera_is_asked_again(config: Path) -> None:
    camera = FakeCamera(SAVED)
    camera.reachable = False
    assert sidecam.ensure(camera, config) is False


def test_settings_wait_for_obs_to_open_the_stream(config: Path) -> None:
    # Starting a stream can reset a webcam, so applying before OBS has the
    # camera open could be undone a moment later.
    camera = FakeCamera({**SAVED, "shutter": 60}, stream_open=False)
    assert sidecam.ensure(camera, config) is False
    assert camera.sets == []


def test_a_camera_already_on_its_settings_is_left_alone(config: Path) -> None:
    camera = FakeCamera({**SAVED, "ev": -0.92, "iso": 400})  # ev within tolerance
    assert sidecam.ensure(camera, config) is True
    assert camera.sets == []


def test_only_the_drifted_settings_are_rewritten(config: Path) -> None:
    camera = FakeCamera({**SAVED, "shutter": 60, "noise_reduction": True})
    assert sidecam.ensure(camera, config) is True
    assert camera.sets == [["set", "shutter=250", "noise_reduction=false"]]
    assert camera.settings["shutter"] == 250


def test_a_refused_setting_is_named_and_asked_again(
    config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Read-back is what catches firmware quietly ignoring a setting.
    camera = FakeCamera({**SAVED, "noise_reduction": True}, refuses=["noise_reduction"])
    assert sidecam.ensure(camera, config) is False
    assert "noise_reduction=True (want False)" in capsys.readouterr().out
