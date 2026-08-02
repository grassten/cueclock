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


def stop_replay_buffer() -> bool:
    """Stop the replay buffer if it is running. True when it is now stopped.

    This is what keeps the Session key's stop path headless: quitting OBS with
    any output active raises a modal "OBS is still currently active" dialog,
    and there is no keyboard at the table to dismiss it.
    """
    status = request("GetReplayBufferStatus")
    if status is None:
        return False
    if not status.get("outputActive"):
        return True
    return request("StopReplayBuffer") is not None
