"""HTTP/WS surface — the Stream Deck contract from docs/cv-app-rewrite.md §2.1."""

import asyncio

from fastapi.testclient import TestClient

from cueclock.clock import State
from cueclock.server import create_app
from cueclock.server.hub import Hub


def make_client() -> TestClient:
    return TestClient(create_app())


def test_state_snapshot_shape() -> None:
    with make_client() as client:
        snap = client.get("/api/state").json()
        assert snap["state"] == "IDLE"
        assert snap["display"] == 30
        assert set(snap) == {
            "state",
            "time",
            "display",
            "ruleset",
            "extensions_used",
            "extensions_allowed",
            "cue",
        }


def test_start_pause_reset() -> None:
    with make_client() as client:
        assert client.post("/api/start").json() == {"ok": True}
        assert client.get("/api/state").json()["state"] == "RUNNING"
        client.post("/api/pause")
        assert client.get("/api/state").json()["state"] == "IDLE"
        client.post("/api/reset")
        assert client.get("/api/state").json()["display"] == 30


def test_extension_accepted_then_rejected() -> None:
    with make_client() as client:
        client.post("/api/start")
        assert client.post("/api/extension").json() == {"ok": True, "accepted": True}
        assert client.post("/api/extension").json()["accepted"] is False


def test_ruleset_switch_alias_and_unknown() -> None:
    with make_client() as client:
        listing = client.get("/api/rulesets").json()
        assert listing["active"] == "ultimate_pool_8ball"
        assert "wnt_9ball" in listing["available"]
        assert client.post("/api/ruleset/ultimate_9ball").json() == {"ok": True}  # alias
        assert client.get("/api/state").json()["ruleset"] == "wnt_9ball"
        assert client.post("/api/ruleset/no_such_ruleset").status_code == 404


def test_websocket_snapshot_on_connect() -> None:
    with make_client() as client, client.websocket_connect("/ws") as ws:
        snap = ws.receive_json()
        assert snap["state"] == "IDLE"
        assert snap["display"] == 30


def test_html_pages_served() -> None:
    with make_client() as client:
        assert "clock-square" in client.get("/shotclock").text
        assert "Shot Clock Control" in client.get("/control").text


def test_vision_events_drive_clock() -> None:
    """The `cueclock run` wiring: settle (re)starts the countdown, a shot freezes it."""
    hub = Hub()
    asyncio.run(hub.balls_settled())  # session's first shot settles → clock starts
    assert hub.clock.state is State.RUNNING
    asyncio.run(hub.shot_taken())
    assert hub.clock.state is State.MOTION
    asyncio.run(hub.balls_settled())
    assert hub.clock.state is State.RUNNING
    assert hub.clock.remaining == float(hub.clock.ruleset.base_seconds)
