"""One-time Android TV Remote v2 pairing for the wall TV (docs/hardware.md §E1).

The Hisense CanvasTV runs Google TV, so it speaks the Android TV Remote
protocol: TLS + protobuf on 6466, with a pairing handshake on 6467. Pairing is
a one-time exchange — the TV shows a 6-character code, we echo it back, and
both sides keep a client certificate. After that the deck can send keys
unattended.

The cert and key land in `config/` (gitignored) beside `obs_websocket.json`;
they are this Mac's identity to the TV, so losing them just means pairing again.

Needs the TV **on** — its network stack is down in standby (measured
2026-08-02), which is the whole reason power-on may need Wake-on-LAN.

Run:  uv run python scripts/pair_tv.py [host]
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from androidtvremote2 import AndroidTVRemote, CannotConnect, ConnectionClosed, InvalidAuth

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "config"
CERTFILE = CONFIG_DIR / "tv_cert.pem"
KEYFILE = CONFIG_DIR / "tv_key.pem"

# The wall TV, found by subnet sweep 2026-08-02. Overridable on the command
# line so a DHCP move doesn't need a code edit — but a reservation on the
# router is the real fix, and `tv.py` reads the host from config.
DEFAULT_HOST = "192.168.4.27"
CLIENT_NAME = "cueclock"
# Mistyping a code off a TV across the room is ordinary, so give it a few goes
# before quitting — but not unlimited, or a wrong-TV host hangs here.
PAIR_ATTEMPTS = 3
PROMPT = "6-character code shown on the TV: "


def is_pairing_code(code: str) -> bool:
    """Would the library accept this code?

    The code is **hex**, not decimal — `B195EF` and `410A85` are both normal
    (a first pass rejecting non-digits threw out every real code, 2026-08-02).
    This mirrors exactly what `pairing.async_finish_pairing` checks, so a code
    that passes here never fails validation downstream.
    """
    if len(code) != 6:
        return False
    try:
        bytes.fromhex(code)
    except ValueError:
        return False
    return True


async def stdin_reader() -> asyncio.StreamReader:
    """Wire stdin into the running loop.

    Deliberately not `input()`: called from a coroutine it blocks the loop, and
    parked in a worker thread it leaves a thread the interpreter can't join, so
    Ctrl-C exits through a page of traceback. A loop-native reader does neither.
    """
    reader = asyncio.StreamReader()
    loop = asyncio.get_running_loop()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    return reader


async def prompt_code(reader: asyncio.StreamReader) -> str:
    """Read codes until one is well-formed, without touching the TV.

    Rejecting locally matters: a malformed code sent to the library tears the
    pairing session down and the TV drops its dialog, so a typo would cost a
    restart. Terminals that auto-activate a venv also inject a line of shell
    into stdin here — that gets rejected the same way, and the dialog survives.
    """
    while True:
        print(PROMPT, end="", flush=True)
        line = await reader.readline()
        if not line:
            raise EOFError("stdin closed before a code was entered")
        code = line.decode(errors="replace").strip()
        if is_pairing_code(code):
            return code
        print(f"expected 6 hex characters (0-9, A-F), got {code!r}")


async def pair(host: str) -> int:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    remote = AndroidTVRemote(CLIENT_NAME, str(CERTFILE), str(KEYFILE), host)
    if await remote.async_generate_cert_if_missing():
        print(f"generated a client certificate at {CERTFILE.name}")

    try:
        await remote.async_connect()
        print("already paired — nothing to do")
    except CannotConnect as exc:
        print(f"cannot reach {host} — is the TV on? ({exc})")
        return 1
    except InvalidAuth:
        # Expected on a first run: the cert exists but the TV has never seen it.
        # A rejected code invalidates the whole handshake, so each retry has to
        # restart pairing and put a fresh code on screen.
        reader = await stdin_reader()
        for attempt in range(1, PAIR_ATTEMPTS + 1):
            await remote.async_start_pairing()
            try:
                await remote.async_finish_pairing(await prompt_code(reader))
                break
            except InvalidAuth as exc:
                print(f"the TV rejected that code ({exc})")
                if attempt == PAIR_ATTEMPTS:
                    return 1
            except ConnectionClosed as exc:
                print(f"the TV closed the pairing dialog ({exc})")
                return 1
        await remote.async_connect()
        print("paired")

    name, mac = await remote.async_get_name_and_mac()
    # This is the Wi-Fi MAC, whatever link the TV is on. On the cable (#62)
    # `config/tv.json` needs the wired port's MAC instead, which is what the
    # magic packet must name: read it with `arp -an -i en0` (hardware.md §E1).
    print(f"TV: {name}  mac={mac}  is_on={remote.is_on}")
    remote.disconnect()
    return 0


def main() -> int:
    host = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_HOST
    try:
        return asyncio.run(pair(host))
    except (InvalidAuth, CannotConnect, ConnectionClosed, EOFError) as exc:
        print(f"pairing failed: {type(exc).__name__}: {exc}")
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
