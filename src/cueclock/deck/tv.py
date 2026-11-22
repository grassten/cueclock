"""Wall-TV power for the deck's Session key (hardware.md §E1).

The TV is a Hisense CanvasTV running **Google TV**, so it speaks the Android TV
Remote protocol — TLS + protobuf on 6466. Pairing is one-time and its
certificate lives in `config/`; see `scripts/pair_tv.py`.

What shapes everything here was measured on the real set (2026-08-02 on
Wi-Fi, 2026-11-22 on the cable, #62), and none of it is guessable:

  * POWER is a **toggle** that steps through three states: on → art mode →
    standby. **Art mode** keeps the panel lit and the TV reports itself *on*;
    only its foreground app (`ART_MODE_APP`) tells the two apart. A long press
    makes an unpredictable number of steps (on the cable it landed in standby,
    in art mode and back on), so it is never sent.
  * A tap sometimes does nothing. So every tap is checked against the state
    the TV pushes back about a second later, and another is sent only while
    that state still disagrees with the target.
  * Settled in standby, the TV ignores key presses (on Wi-Fi it leaves the
    network entirely). A **Wake-on-LAN** magic packet brings it back, screen
    included, in about a second, and the packet is no toggle. Art mode
    ignores the packet, so waking from there means tapping down first.

Hence: off is taps until the TV reports standby, on is a magic packet. Same
contract as `obsws.py` — every call is best-effort, returns a bool (or `None`
for "couldn't ask"), and nothing here may raise into the deck loop.
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
import subprocess  # nosec B404: fixed argv, no shell, no user input
from collections.abc import Coroutine, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from androidtvremote2 import AndroidTVRemote

CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "tv.json"
CERT_PATH = CONFIG_PATH.parent / "tv_cert.pem"
KEY_PATH = CONFIG_PATH.parent / "tv_key.pem"

CLIENT_NAME = "cueclock"  # how this Mac identifies itself to the TV
POWER = "POWER"
# The remote API port. Its being open is the cheapest proof the TV is out of
# standby, and unlike a connect it costs nothing when the TV is gone.
PORT = 6466
# The Canvas TV's art-mode app: the only sign of art mode, which reports "on".
ART_MODE_APP = "com.vt.arttv.artmode"
# The foreground app is pushed just after connecting (0.07 s measured).
APP_WAIT_S = 1.0
# A tap's effect is pushed 1.0-1.2 s after it (measured 2026-11-22). Silence
# for this long means the TV dropped the press, so it is safe to send another.
TAP_ANSWER_S = 3.0
# One transition can push its two fields 1.26 s apart (standby → on: the app,
# then the power state), so the state is read only after this much quiet.
SETTLE_S = 2.0
# on → art mode → standby is two taps; the rest is room for dropped presses.
MAX_TAPS = 4
# Short, because this runs on the deck's 2 s poll: a TV that needs longer than
# this to answer is one we should give up on until the next tick, not block for.
CONNECT_TIMEOUT_S = 4.0
# Both are conventional for Wake-on-LAN and neither costs anything to include.
WOL_PORTS = (9, 7)


@dataclass(frozen=True)
class TvConfig:
    host: str
    mac: str
    certfile: str = str(CERT_PATH)
    keyfile: str = str(KEY_PATH)


def load_config(path: Path = CONFIG_PATH) -> TvConfig | None:
    """Read the gitignored TV settings, or None if they aren't usable."""
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"tv: no usable {path.name} ({exc})", flush=True)
        return None
    host, mac = raw.get("host"), raw.get("mac")
    if not isinstance(host, str) or not isinstance(mac, str):
        print(f"tv: {path.name} needs host/mac", flush=True)
        return None
    return TvConfig(host, mac)


def magic_packet(mac: str) -> bytes:
    """6 x 0xFF then the MAC repeated 16 times — the whole Wake-on-LAN format."""
    raw = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    if len(raw) != 6:
        raise ValueError(f"not a MAC address: {mac!r}")
    return b"\xff" * 6 + raw * 16


def wol_targets(mac: str) -> list[str]:
    """The paired MAC plus its immediate neighbours — the TV's other NIC.

    The TV reports one MAC over the remote protocol (…:97) while ARP shows the
    next one along carrying traffic (…:98): wired and wireless interfaces on
    one board, allocated consecutively. Which one listens in standby isn't
    observable from here, and two extra UDP packets cost nothing, so cover all
    three rather than make a silent wrong guess.
    """
    raw = bytearray(bytes.fromhex(mac.replace(":", "").replace("-", "")))
    targets = []
    for delta in (0, 1, -1):
        candidate = bytearray(raw)
        candidate[5] = (candidate[5] + delta) % 256
        targets.append(":".join(f"{byte:02X}" for byte in candidate))
    return targets


def broadcast_address(host: str) -> str:
    """The subnet broadcast for a /24 — the rig's LAN, and all it has to be.

    Some gear ignores 255.255.255.255 and only answers a directed broadcast,
    so both get a packet.
    """
    return host.rsplit(".", 1)[0] + ".255"


def send_wol(config: TvConfig) -> bool:
    """Fire magic packets at every plausible MAC, port and broadcast address."""
    targets = wol_targets(config.mac)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            for mac in targets:
                packet = magic_packet(mac)
                for address in (broadcast_address(config.host), "255.255.255.255"):
                    for port in WOL_PORTS:
                        sock.sendto(packet, (address, port))
    except (OSError, ValueError) as exc:
        print(f"tv: wake-on-lan failed ({exc})", flush=True)
        return False
    print(f"tv: wake-on-lan sent to {', '.join(targets)}", flush=True)
    return True


def port_open(host: str, port: int = PORT, timeout: float = 1.0) -> bool:
    """Is the TV answering on the remote API port? (i.e. out of standby)"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            return sock.connect_ex((host, port)) == 0
    except OSError:
        return False


def _normalise_mac(mac: str) -> str:
    """Lowercase, zero-padded — `arp` prints `4:6b:...`, configs write `04:6B`."""
    try:
        return ":".join(f"{int(part, 16):02x}" for part in mac.split(":"))
    except ValueError:
        return mac.lower()


def arp_host(macs: Sequence[str]) -> str | None:
    """Which address does the ARP cache say owns one of these MACs?"""
    try:
        out = subprocess.run(  # nosec B603: fixed argv, shell=False
            ["/usr/sbin/arp", "-an"], capture_output=True, text=True, timeout=2.0, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"tv: could not read the ARP cache ({exc})", flush=True)
        return None
    wanted = {_normalise_mac(mac) for mac in macs}
    for line in out.splitlines():
        # "? (192.168.4.29) at 64:6b:e7:fe:46:98 on en1 ifscope [ethernet]"
        found = re.search(r"\((\d+\.\d+\.\d+\.\d+)\) at ([0-9a-fA-F:]+)", line)
        if found and _normalise_mac(found.group(2)) in wanted:
            return found.group(1)
    return None


def resolve(config: TvConfig) -> tuple[TvConfig, bool]:
    """(the TV at the address it is on *now*, whether it is answering).

    `tv.json` pins an IP, and DHCP does not respect that. A power cycle moved
    the TV from .27 to .29 (2026-09-01) and every call for the rest of the
    session failed against the stale address: `ensure_on` fired nineteen magic
    packets at a TV that was already awake and never confirmed, then the stop's
    `power_off` timed out and left the wall lit. Nothing said why — a TV that
    has moved and a TV that is in standby look identical from a closed port.

    The MAC does not move, so that is what gets asked. Same three candidates
    `wol_targets` already covers, because the TV answers the remote protocol on
    one NIC and shows up in ARP on the other.

    ARP only knows hosts seen recently, so this is a repair for a TV that is
    awake at the wrong address — not discovery for one in standby. That case is
    already handled: the magic packet is a broadcast and needs no address.
    """
    if port_open(config.host):
        return config, True
    found = arp_host(wol_targets(config.mac))
    if found is None or found == config.host or not port_open(found):
        return config, False
    print(f"tv: {config.host} stopped answering — the TV is at {found} now (ARP)", flush=True)
    return replace(config, host=found), True


def _run[T](coro: Coroutine[Any, Any, T], what: str) -> T | None:
    """Run one short conversation with the TV; None on any failure."""
    try:
        return asyncio.run(coro)
    except Exception as exc:  # a control surface never dies because the TV did
        print(f"tv: {what} failed ({type(exc).__name__}: {exc})", flush=True)
        return None


async def _connect(config: TvConfig) -> AndroidTVRemote:
    remote = AndroidTVRemote(CLIENT_NAME, config.certfile, config.keyfile, config.host)
    await asyncio.wait_for(remote.async_connect(), CONNECT_TIMEOUT_S)
    return remote


type PowerState = Literal["on", "art", "off"]


def _state(remote: AndroidTVRemote) -> PowerState:
    if not remote.is_on:
        return "off"
    return "art" if remote.current_app == ART_MODE_APP else "on"


async def _wait(event: asyncio.Event, timeout: float) -> bool:
    try:
        await asyncio.wait_for(event.wait(), timeout)
    except TimeoutError:
        return False
    return True


async def _open(config: TvConfig) -> tuple[AndroidTVRemote, asyncio.Event]:
    """Connect, and return an event the TV's state pushes set."""
    remote = await _connect(config)
    pushed = asyncio.Event()
    remote.add_is_on_updated_callback(lambda _on: pushed.set())
    remote.add_current_app_updated_callback(lambda _app: pushed.set())
    if not remote.current_app:  # art mode or not is only known once it lands
        await _wait(pushed, APP_WAIT_S)
    return remote, pushed


async def _async_power_state(config: TvConfig) -> PowerState:
    remote, _ = await _open(config)
    try:
        return _state(remote)
    finally:
        remote.disconnect()


async def _async_tap_to_standby(config: TvConfig) -> bool:
    """Tap POWER, one checked step at a time, until the TV reports standby."""
    remote, pushed = await _open(config)
    try:
        for _ in range(MAX_TAPS):
            if _state(remote) == "off":
                return True
            pushed.clear()
            remote.send_key_command(POWER)
            if await _wait(pushed, TAP_ANSWER_S):
                await asyncio.sleep(SETTLE_S)  # the rest of the transition
        return _state(remote) == "off"
    finally:
        remote.disconnect()


def power_state(config: TvConfig | None = None) -> PowerState | None:
    """Is the TV on, in art mode, or in standby, by its own report?

    **None means unknown** — on Wi-Fi a TV in standby isn't on the network at
    all, so a failed connect is not the same as a confident "off", and a
    caller must not read it as one.
    """
    cfg = config or load_config()
    if cfg is None:
        return None
    return _run(_async_power_state(cfg), "power state")


def ensure_on(config: TvConfig | None = None) -> bool:
    """Drive the TV toward on. True only once it is confirmed on.

    Idempotent, and deliberately unwilling to block: the deck calls this from
    `start()` and then again on each poll until it returns True, exactly like
    the OBS setup beside it. When the TV is asleep this fires the magic packet
    and leaves rather than sitting on the deck loop waiting for a panel to
    light; only art mode costs a few seconds, tapping down to standby first.
    """
    cfg = config or load_config()
    if cfg is None:
        return False
    # Resolve first: an unanswered port means "asleep" only once we know we
    # asked the right address. `resolve` reports reachability so the port is
    # probed once, not twice — this runs on the deck's 2 s poll.
    cfg, reachable = resolve(cfg)
    state = power_state(cfg) if reachable else None
    if state == "on":
        return True
    if state == "art":
        # A tap from art mode goes to standby, not on, and art mode ignores the
        # magic packet, so the way up is down first.
        print("tv: in art mode — stepping down to standby to wake it", flush=True)
        _run(_async_tap_to_standby(cfg), "leave art mode")
    # Standby, or unknown: only the magic packet wakes it, and unlike POWER it
    # is no toggle, so sending it to a TV that is already awake costs nothing.
    send_wol(cfg)
    return False


def power_off(config: TvConfig | None = None) -> bool:
    """Tap POWER down to standby. True once the TV reports standby.

    Taps, never a long press: a long press steps an unpredictable number of
    times, and on this set it left the wall lit in art mode a third of the time.
    """
    cfg = config or load_config()
    if cfg is None:
        return False
    cfg, _ = resolve(cfg)  # the stop is the call that most needs to land
    reached = _run(_async_tap_to_standby(cfg), "power off")
    if reached:
        print("tv: standby confirmed", flush=True)
    elif reached is False:
        print(f"tv: power off not confirmed after {MAX_TAPS} taps", flush=True)
    return bool(reached)
