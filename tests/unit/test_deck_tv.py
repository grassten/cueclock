"""Wall-TV power (deck phase 2) — every branch, with no TV on the network.

Two kinds of thing live here: packet arithmetic, which is pure, and the small
decision table `ensure_on` walks. The two socket calls and the coroutine runner
are patched out, so the whole file runs offline and in milliseconds.

The constants these lock down were measured against the real TV on 2026-08-02
(see the module docstring in `tv.py`); a change that breaks one of these tests
is a change that would put the wall TV in the wrong state.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import pytest

from cueclock.deck import tv

CONFIG = tv.TvConfig("192.168.4.27", "64:6B:E7:FE:46:97")


def record(log: list[str]) -> Callable[[Coroutine[Any, Any, Any], str], bool]:
    """Stand in for `_run`, closing the coroutine it was handed.

    Closing matters: an un-awaited coroutine leaks a RuntimeWarning that would
    make every other test in the run look noisy.
    """

    def fake(coro: Coroutine[Any, Any, Any], what: str) -> bool:
        coro.close()
        log.append(what)
        return True

    return fake


def test_magic_packet_is_six_ff_bytes_then_the_mac_sixteen_times() -> None:
    packet = tv.magic_packet("64:6B:E7:FE:46:97")
    mac = bytes.fromhex("646BE7FE4697")
    assert len(packet) == 102
    assert packet == b"\xff" * 6 + mac * 16


def test_a_short_mac_is_refused_rather_than_padded() -> None:
    with pytest.raises(ValueError, match="not a MAC address"):
        tv.magic_packet("64:6B:E7:FE:46")


def test_the_packet_reaches_the_neighbouring_nic_too() -> None:
    # The TV reports …:97 over the remote protocol while ARP shows …:98
    # carrying the traffic — one board, two consecutively allocated NICs.
    # Which of them listens in standby is not observable from here, so all
    # three get a packet rather than a silent wrong guess.
    assert tv.wol_targets("64:6B:E7:FE:46:97") == [
        "64:6B:E7:FE:46:97",
        "64:6B:E7:FE:46:98",
        "64:6B:E7:FE:46:96",
    ]


def test_neighbouring_macs_wrap_inside_the_last_octet() -> None:
    assert tv.wol_targets("00:11:22:33:44:FF")[1] == "00:11:22:33:44:00"
    assert tv.wol_targets("00:11:22:33:44:00")[2] == "00:11:22:33:44:FF"


def test_the_broadcast_address_is_the_subnet_not_the_host() -> None:
    assert tv.broadcast_address("192.168.4.27") == "192.168.4.255"


def test_a_missing_config_is_reported_not_raised(tmp_path: Path) -> None:
    assert tv.load_config(tmp_path / "absent.json") is None


def test_a_config_missing_the_mac_is_refused(tmp_path: Path) -> None:
    # Without a MAC there is no way to wake the TV, and a half-working power
    # key is worse than one that says up front that it cannot work.
    path = tmp_path / "tv.json"
    path.write_text(json.dumps({"host": "192.168.4.27"}))
    assert tv.load_config(path) is None


def test_a_good_config_defaults_to_the_paired_certificate(tmp_path: Path) -> None:
    path = tmp_path / "tv.json"
    path.write_text(json.dumps({"host": "192.168.4.27", "mac": "64:6B:E7:FE:46:97"}))
    config = tv.load_config(path)
    assert config is not None
    assert (config.host, config.mac) == ("192.168.4.27", "64:6B:E7:FE:46:97")
    assert config.certfile.endswith("tv_cert.pem")
    assert config.keyfile.endswith("tv_key.pem")


def test_a_sleeping_tv_gets_a_packet_and_is_not_waited_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[str] = []
    monkeypatch.setattr(tv, "port_open", lambda *_a, **_k: False)
    monkeypatch.setattr(tv, "send_wol", lambda config: bool(sent.append(config.mac)) or True)

    # False, not True: in standby the TV is off the network entirely, so there
    # is nothing to confirm against. It wakes on its own clock and the deck
    # checks back on the next poll rather than blocking the loop.
    assert tv.ensure_on(CONFIG) is False
    assert sent == [CONFIG.mac]


def test_a_tv_already_on_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    # POWER is a toggle: pressing it here would switch the TV *off*, which is
    # exactly the bug that made the first working version look broken.
    log: list[str] = []
    monkeypatch.setattr(tv, "port_open", lambda *_a, **_k: True)
    monkeypatch.setattr(tv, "is_on", lambda _config=None: True)
    monkeypatch.setattr(tv, "_run", record(log))

    assert tv.ensure_on(CONFIG) is True
    assert log == []


def test_a_tv_awake_but_reporting_off_gets_a_short_press(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Art mode, or a magic packet that revived only the NIC. A *short* press is
    # what leaves art mode; a long one would drive it to standby — backwards.
    log: list[str] = []
    monkeypatch.setattr(tv, "port_open", lambda *_a, **_k: True)
    monkeypatch.setattr(tv, "is_on", lambda _config=None: False)
    monkeypatch.setattr(tv, "_run", record(log))

    assert tv.ensure_on(CONFIG) is False  # confirmed on a later poll
    assert log == ["power on"]


def test_a_tv_that_cannot_be_asked_is_unknown_not_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log: list[str] = []
    monkeypatch.setattr(tv, "port_open", lambda *_a, **_k: True)
    monkeypatch.setattr(tv, "is_on", lambda _config=None: None)
    monkeypatch.setattr(tv, "_run", record(log))

    # Unknown must never be read as "off" and pressed against.
    assert tv.ensure_on(CONFIG) is False
    assert log == []


def test_power_off_holds_the_key_because_a_tap_only_reaches_art_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holds: list[float] = []

    async def fake_press(config: tv.TvConfig, hold_s: float) -> bool:
        holds.append(hold_s)
        return True

    monkeypatch.setattr(tv, "_async_press_power", fake_press)

    assert tv.power_off(CONFIG) is True
    # A short press would land in art mode: panel lit, TV still on the network,
    # and every check downstream reporting success while the wall glows.
    assert holds == [tv.HOLD_S]
    assert tv.HOLD_S >= 2.0


def test_a_failed_press_is_reported_rather_than_assumed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tv, "_run", lambda coro, _what: coro.close())
    assert tv.power_off(CONFIG) is False


def test_nothing_is_attempted_without_a_config(monkeypatch: pytest.MonkeyPatch) -> None:
    # No `config/tv.json` (a fresh clone) must degrade to a session that simply
    # never touches the TV — never to an exception inside the deck loop.
    monkeypatch.setattr(tv, "load_config", lambda _path=tv.CONFIG_PATH: None)
    assert tv.ensure_on() is False
    assert tv.power_off() is False
    assert tv.is_on() is None
