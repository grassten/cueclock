"""Side-angle camera settings, enforced at session start (hardware.md Part F).

The Insta360 Link 2C Pro keeps exposure, white balance, focus and its mic's
noise suppression inside the camera, and the only first-party way to change
them is Insta360 Link Controller. That app cannot stay open during play: on
2026-11-12 its two open windows lined up exactly with detection dropping from
~125 Hz to ~80 Hz and OBS dropping 3.6% of its frames (CHANGELOG checkpoint 108).

So the settings live in `config/sidecam.json` instead. `cueclock sidecam save`
captures whatever the camera is doing now (after setting it up in Link
Controller once). At every session start `ensure()` reads the camera back and
re-applies anything that drifted. The camera is reached through
`link-settings`, a small helper built against Insta360's SDK by
`scripts/fetch_insta360_sdk.sh`, because Python cannot call the SDK's C++
classes directly.

Same contract as `obsws.py` and `tv.py`: best-effort, bool results, and nothing
here may raise into the deck loop. The side angle does not feed detection, so a
camera that refuses its settings costs a warning, never the shot clock.
"""

from __future__ import annotations

import json
import subprocess  # nosec B404: fixed argv, no shell, values from our own config
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
HELPER = REPO_ROOT / "vendor.nosync" / "insta360-link-sdk" / "bin" / "link-settings"
CONFIG_PATH = REPO_ROOT / "config" / "sidecam.json"
# One `get` measured 0.8 s on 2026-11-12. This runs on the deck's 2 s poll, so
# a camera slower than this is skipped until the next tick, not waited on.
TIMEOUT_S = 5.0
# How far a read-back may sit from what was asked before it counts as refused.
# Only exposure compensation is fractional, and the camera reports it rounded.
FLOAT_TOLERANCE = 0.05

# While a mode is automatic, the camera picks these values itself and keeps
# changing them. Saving one would pin a snapshot of a moving target, and
# enforcing it would fight the camera, so they are only kept in manual mode.
AUTO_OWNS: dict[str, tuple[str, ...]] = {
    "auto_exposure": ("shutter", "iso"),
    "wb_auto": ("wb_kelvin",),
    "autofocus": ("focus",),
}

Runner = Callable[[Sequence[str]], str | None]


def _run(args: Sequence[str]) -> str | None:
    """Run the helper; return its stdout, or None (and say why) on any failure."""
    if not HELPER.exists():
        print(f"sidecam: {HELPER.name} not built — run scripts/fetch_insta360_sdk.sh", flush=True)
        return None
    try:
        done = subprocess.run(  # nosec B603: fixed helper path, no shell
            [str(HELPER), *args], capture_output=True, text=True, timeout=TIMEOUT_S, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"sidecam: {HELPER.name} failed ({exc})", flush=True)
        return None
    if done.returncode != 0:
        print(f"sidecam: {done.stderr.strip() or f'exit {done.returncode}'}", flush=True)
        return None
    return done.stdout


def _parse(stdout: str) -> dict[str, Any] | None:
    """The helper's JSON line. The SDK logs to stdout too, so take the last `{` line."""
    for line in reversed(stdout.splitlines()):
        if line.startswith("{"):
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                return None
            return parsed if isinstance(parsed, dict) else None
    return None


def read(run: Runner = _run) -> dict[str, Any] | None:
    """The camera's identity, settings and ranges, or None if it can't be reached."""
    stdout = run(["get"])
    return None if stdout is None else _parse(stdout)


def wanted_from(settings: Mapping[str, Any]) -> dict[str, Any]:
    """The part of a reading worth enforcing: no unread values, no auto-owned ones."""
    wanted = {key: value for key, value in settings.items() if value is not None}
    for mode, owned in AUTO_OWNS.items():
        if wanted.get(mode) is True:
            for key in owned:
                wanted.pop(key, None)
    return wanted


def _matches(want: Any, have: Any) -> bool:
    if isinstance(want, float) and isinstance(have, int | float) and not isinstance(have, bool):
        return abs(have - want) <= FLOAT_TOLERANCE
    return bool(have == want)


def drifted(wanted: Mapping[str, Any], actual: Mapping[str, Any]) -> dict[str, Any]:
    """The subset of `wanted` the camera is not currently at."""
    return {key: want for key, want in wanted.items() if not _matches(want, actual.get(key))}


def mismatches(wanted: Mapping[str, Any], actual: Mapping[str, Any]) -> list[str]:
    """Settings the camera is not at, as `key=actual (want wanted)`."""
    off = drifted(wanted, actual)
    return [f"{key}={actual.get(key)} (want {want})" for key, want in off.items()]


def _arg(value: Any) -> str:
    return ("true" if value else "false") if isinstance(value, bool) else str(value)


def apply(wanted: Mapping[str, Any], run: Runner = _run) -> list[str] | None:
    """Write `wanted`, then read it back. Returns what didn't take, or None if unreachable."""
    stdout = run(["set", *(f"{key}={_arg(value)}" for key, value in wanted.items())])
    report = None if stdout is None else _parse(stdout)
    if report is None:
        return None
    return mismatches(wanted, report.get("settings", {}))


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any] | None:
    """The saved settings, or None if there are none (or they are unreadable)."""
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        print(f"sidecam: unreadable {path.name} ({exc})", flush=True)
        return None
    return raw if isinstance(raw, dict) else None


def save_config(settings: Mapping[str, Any], path: Path = CONFIG_PATH) -> None:
    path.write_text(json.dumps(dict(settings), indent=2) + "\n")


def ensure(run: Runner = _run, path: Path = CONFIG_PATH) -> bool:
    """Session-start step: True once the camera matches the saved settings.

    False means "ask again on the next tick", the same as the TV and the
    projector. That covers an unreachable camera, and a camera OBS hasn't opened
    yet: settings are applied *after* the stream starts, because starting a
    stream can reset a webcam's settings. Nothing saved means nothing to
    enforce, so it counts as done.
    """
    wanted = load_config(path)
    if not wanted:
        print("sidecam: nothing saved — `cueclock sidecam save` to enforce settings", flush=True)
        return True
    report = read(run)
    if report is None:
        return False
    if not report.get("camera", {}).get("stream_open"):
        return False
    # Only what drifted: rewriting a value the camera already has can still
    # blip the picture or the mic on some firmware, and this runs mid-stream.
    todo = drifted(wanted, report.get("settings", {}))
    if not todo:
        return True
    refused = apply(todo, run)
    if refused is None:
        return False
    if refused:
        print(f"sidecam: camera did not take {'; '.join(refused)}", flush=True)
        return False
    print(f"sidecam: re-applied {', '.join(todo)}", flush=True)
    return True
