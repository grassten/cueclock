"""Minimal obs-websocket v5 client — only what the deck needs.

One request per connection: open, authenticate, send, read, close. No
persistent socket, no reconnect logic, no event subscriptions — the deck talks
to OBS a handful of times per session, and a short-lived connection can't go
stale between presses.

Every call is best-effort: OBS being closed, the WebSocket server being
disabled, or a wrong password all return `None` with a printed line. Nothing
here may raise into the deck loop.

Credentials come from `config/obs_websocket.json` (gitignored), copied from
OBS → Tools → WebSocket Server Settings — see `hardware.md` §C4.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "obs_websocket.json"

# obs-websocket protocol opcodes (v5).
_HELLO = 0
_IDENTIFY = 1
_IDENTIFIED = 2
_REQUEST = 6
_REQUEST_RESPONSE = 7
_RPC_VERSION = 1


@dataclass(frozen=True)
class ObsConfig:
    host: str
    port: int
    password: str

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}"


def load_config(path: Path = CONFIG_PATH) -> ObsConfig | None:
    """Read the gitignored credentials file, or None if it isn't usable."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"obsws: no usable {path.name} ({exc})", flush=True)
        return None
    host, port, password = raw.get("host"), raw.get("port"), raw.get("password")
    if not isinstance(host, str) or not isinstance(port, int) or not isinstance(password, str):
        print(f"obsws: {path.name} needs host/port/password", flush=True)
        return None
    return ObsConfig(host, port, password)


def auth_response(password: str, salt: str, challenge: str) -> str:
    """obs-websocket v5 auth digest.

    Spec: base64(sha256(base64(sha256(password + salt)) + challenge)).
    """
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()


async def _request_async(
    cfg: ObsConfig, request_type: str, request_data: dict[str, Any] | None, timeout: float
) -> dict[str, Any] | None:
    from websockets.asyncio.client import connect

    async with connect(cfg.url, open_timeout=timeout, close_timeout=timeout) as ws:
        hello = json.loads(await asyncio.wait_for(ws.recv(), timeout))
        identify: dict[str, Any] = {"rpcVersion": _RPC_VERSION}
        auth = hello.get("d", {}).get("authentication")
        if auth:  # absent when OBS has authentication switched off
            identify["authentication"] = auth_response(
                cfg.password, auth["salt"], auth["challenge"]
            )
        await ws.send(json.dumps({"op": _IDENTIFY, "d": identify}))
        identified = json.loads(await asyncio.wait_for(ws.recv(), timeout))
        if identified.get("op") != _IDENTIFIED:
            print(f"obsws: not identified ({identified})", flush=True)
            return None

        await ws.send(
            json.dumps(
                {
                    "op": _REQUEST,
                    "d": {
                        "requestType": request_type,
                        "requestId": request_type,  # one request per connection
                        "requestData": request_data or {},
                    },
                }
            )
        )
        while True:  # skip any events OBS pushes before the response
            message = json.loads(await asyncio.wait_for(ws.recv(), timeout))
            if message.get("op") == _REQUEST_RESPONSE:
                break
        payload = message.get("d", {})
        status = payload.get("requestStatus", {})
        if not status.get("result"):
            print(f"obsws: {request_type} failed ({status})", flush=True)
            return None
        data = payload.get("responseData")
        return data if isinstance(data, dict) else {}


def request(
    request_type: str,
    request_data: dict[str, Any] | None = None,
    *,
    timeout: float = 3.0,
    config: ObsConfig | None = None,
) -> dict[str, Any] | None:
    """Send one request; return its responseData, or None on any failure."""
    cfg = config or load_config()
    if cfg is None:
        return None
    try:
        call = _request_async(cfg, request_type, request_data, timeout)
        return asyncio.run(asyncio.wait_for(call, timeout * 2))
    except Exception as exc:  # a control surface never dies because OBS did
        print(f"obsws: {request_type} error ({type(exc).__name__}: {exc})", flush=True)
        return None


# Every output whose being active makes OBS raise the modal quit dialog.
_OUTPUTS: tuple[tuple[str, str], ...] = (
    ("GetReplayBufferStatus", "StopReplayBuffer"),
    ("GetRecordStatus", "StopRecord"),
    ("GetStreamStatus", "StopStream"),
    ("GetVirtualCamStatus", "StopVirtualCam"),
)


def active_outputs() -> list[str] | None:
    """Stop-requests for the outputs currently running.

    Empty list = OBS is quiet. **None = unknown** (OBS unreachable, WebSocket
    server off, bad password) — deliberately not the same thing as empty, so
    a caller can't mistake "couldn't ask" for "nothing is running".
    """
    running = []
    for status_request, stop_request in _OUTPUTS:
        data = request(status_request)
        if data is None:
            return None
        if data.get("outputActive"):
            running.append(stop_request)
    return running


SYPHON_SOURCE = "Syphon Client"  # the OBS source name — must match §B3
SYPHON_SERVER = "cueclock"  # the Syphon server name `run` publishes
TV_MONITOR_INDEX = 0  # the mini drives exactly one display: the wall TV


def open_projector(monitor_index: int = TV_MONITOR_INDEX) -> bool:
    """Put the program output fullscreen on the TV.

    Asking OBS directly beats relying on "Save projectors on exit", which only
    restores what happened to be open at the last quit — so a session that
    ended without one comes back with a blank TV, and the key's whole promise
    is that it doesn't matter what the last session did. Verified idempotent
    2026-08-02: calling it with a projector already up reuses that window
    rather than stacking a second one.
    """
    ok = request(
        "OpenVideoMixProjector",
        {
            "videoMixType": "OBS_WEBSOCKET_VIDEO_MIX_TYPE_PROGRAM",
            "monitorIndex": monitor_index,
        },
    )
    if ok is None:
        return False
    print(f"obsws: program projector on monitor {monitor_index}", flush=True)
    return True


def bind_syphon(source: str = SYPHON_SOURCE, server: str = SYPHON_SERVER) -> bool:
    """Point the OBS Syphon source at the cueclock server running *now*.

    Two things move underneath that source and both black the table feed:
    every `run` publishes a **new Syphon UUID**, and the advertised app name
    is whatever launched it — `[python] cueclock` from the deck daemon's
    interpreter versus `[python3] cueclock` from a terminal `uv run`. OBS
    stores uuid + name + app_name, so a source bound by one launcher silently
    shows black under the other (measured 2026-08-02: the overhead vanished
    from OBS only when the Session key started the session).

    Asking OBS for its own property list — rather than guessing an identity —
    keeps this correct whoever started the session.
    """
    data = request(
        "GetInputPropertiesListPropertyItems",
        {"inputName": source, "propertyName": "uuid"},
    )
    if data is None:
        return False
    live = [
        item
        for item in data.get("propertyItems", [])
        if item.get("itemEnabled") and server in str(item.get("itemName", ""))
    ]
    if not live:
        return False
    item = live[0]
    label = str(item["itemName"])  # "[python] cueclock"
    app_name = label.split("]")[0].lstrip("[")
    name = label.split("] ", 1)[1] if "] " in label else server
    settings = {"uuid": item["itemValue"], "name": name, "app_name": app_name}
    if request("SetInputSettings", {"inputName": source, "inputSettings": settings}) is None:
        return False
    print(f"obsws: {source} bound to {label}", flush=True)
    return True


def save_replay_buffer() -> bool:
    """Write the replay buffer's last ~20 s to disk. True when OBS took it.

    The buffer is armed by the Session key's `--startreplaybuffer` launch, so
    the clip is already in memory and this is just the "keep that" command —
    it lands in OBS's recording folder. Playing it back on the TV is
    `deck/replay.py`; this stays the one-line save.
    """
    if request("SaveReplayBuffer") is None:
        return False
    print("obsws: replay buffer saved", flush=True)
    return True


def last_replay_path() -> str | None:
    """Path of the most recently saved replay, or None if OBS has none/can't say.

    `SaveReplayBuffer` returns the moment OBS accepts the command, not when the
    file exists — so the only way to know a save landed is to watch this value
    change. `deck/replay.py` polls it.
    """
    data = request("GetLastReplayBufferReplay")
    if data is None:
        return None
    path = data.get("savedReplayPath")
    return path if isinstance(path, str) and path else None


def current_scene() -> str | None:
    """The program scene's name — what to return to when a replay finishes."""
    data = request("GetCurrentProgramScene")
    if data is None:
        return None
    # OBS 30 renamed the field; accept both so an OBS upgrade can't strand a
    # replay in the Replay scene with nowhere recorded to go back to.
    for field in ("currentProgramSceneName", "sceneName"):
        value = data.get(field)
        if isinstance(value, str) and value:
            return value
    return None


def set_scene(name: str) -> bool:
    return request("SetCurrentProgramScene", {"sceneName": name}) is not None


def set_input_settings(name: str, settings: dict[str, Any], *, overlay: bool = True) -> bool:
    """Merge settings into an input (`overlay=False` replaces them wholesale)."""
    payload = {"inputName": name, "inputSettings": settings, "overlay": overlay}
    return request("SetInputSettings", payload) is not None


def refresh_browser(name: str) -> bool:
    """Reload a browser source now, whether or not its URL changed.

    The Replay badge is loaded while its scene is still dark, so it cannot rely
    on OBS's own "refresh when scene becomes active" — that fires at the cut,
    which is half a second too late (`replay.BADGE_PAINT_S`). Pressing the
    source's own refresh button is also honest about repeats: two presses can
    compute the same URL, and an unchanged URL reloads nothing.
    """
    payload = {"inputName": name, "propertyName": "refreshnocache"}
    return request("PressInputPropertiesButton", payload) is not None


def canvas_size() -> tuple[int, int] | None:
    """OBS's base canvas — the coordinate space every scene transform is in.

    Asked rather than assumed: `replay.py` turns scene-item rectangles into
    percentages of this, and a hardcoded 1920x1080 would silently misplace them
    the day the canvas changes (§B3 settled it in 2026, not forever).
    """
    data = request("GetVideoSettings")
    if data is None:
        return None
    width, height = data.get("baseWidth"), data.get("baseHeight")
    if not isinstance(width, int) or not isinstance(height, int):
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


def scene_items(scene: str) -> list[dict[str, Any]] | None:
    """Every item in a scene, each with its `sceneItemTransform`.

    None means "couldn't ask"; an empty list means the scene is empty — the
    same distinction `active_outputs` draws, and for the same reason.
    """
    data = request("GetSceneItemList", {"sceneName": scene})
    if data is None:
        return None
    items = data.get("sceneItems")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def input_url(name: str) -> str | None:
    """The page a browser source is showing, or None if it has no `url`."""
    data = request("GetInputSettings", {"inputName": name})
    if data is None:
        return None
    settings = data.get("inputSettings")
    url = settings.get("url") if isinstance(settings, dict) else None
    return url if isinstance(url, str) else None


def media_status(name: str) -> tuple[float, float] | None:
    """``(cursor_s, duration_s)`` for a media source, or None until it is ready.

    OBS reports a null/negative duration while the file is still opening, which
    is indistinguishable from "no file" in the payload — both come back here as
    None, so the caller polls rather than trusting the first answer.
    """
    data = request("GetMediaInputStatus", {"inputName": name})
    if data is None:
        return None
    duration_ms, cursor_ms = data.get("mediaDuration"), data.get("mediaCursor")
    if not isinstance(duration_ms, int | float) or duration_ms <= 0:
        return None
    cursor = float(cursor_ms) if isinstance(cursor_ms, int | float) else 0.0
    return cursor / 1000.0, float(duration_ms) / 1000.0


def set_media_cursor(name: str, seconds: float) -> bool:
    payload = {"inputName": name, "mediaCursor": int(max(0.0, seconds) * 1000)}
    return request("SetMediaInputCursor", payload) is not None


def stop_all_outputs(attempts: int = 6, settle_s: float = 0.4) -> bool:
    """Drive OBS to zero active outputs. True when it got there.

    This is what keeps the Session key's stop path headless: quitting OBS with
    any output active raises a modal "OBS is still currently active" dialog,
    and there is no keyboard at the table to dismiss it.

    It loops rather than checking once, because `--startreplaybuffer` arms the
    buffer *asynchronously*: a stop pressed a few seconds after launch sees
    "not active", skips the stop, and then the buffer starts anyway — measured
    2026-08-02, and the reason the first fix for this didn't take.
    """
    for _ in range(attempts):
        running = active_outputs()
        if running is None:
            return False  # can't ask — say so rather than assume it's quiet
        if not running:
            return True
        for stop_request in running:
            request(stop_request)
        time.sleep(settle_s)
    return active_outputs() == []
