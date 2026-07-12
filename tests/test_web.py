"""Web interface: auth, safe defaults, and cross-process dance stop."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from mmhue.interfaces.web.interface import WebInterface
from mmhue.models import CommandResult


def _hub() -> MagicMock:
    hub = MagicMock()
    hub.rooms.list_rooms.return_value = []
    hub.scenes.list_scenes.return_value = []

    light = MagicMock()
    light.id, light.name, light.room, light.room_id = "l1", "Lamp", "Den", "r1"
    light.state.on, light.state.brightness, light.state.color_temp = True, 0.5, 300
    light.supports_color = light.supports_color_temp = True
    hub.lights.list_lights.return_value = [light]

    hub.dances.registry_names.return_value = ["birthday"]
    hub.dances.running = "birthday"
    hub.dances.stop = AsyncMock(return_value=CommandResult.ok("⏹ birthday stopped"))
    hub.dances.remember_state = AsyncMock()
    hub.lights.toggle = AsyncMock()
    return hub


def test_binds_to_localhost_when_no_password(monkeypatch):
    """An unauthenticated light switch must not land on the home network."""
    monkeypatch.delenv("MMHUE_WEB_PASSWORD", raising=False)
    web = WebInterface(_hub(), host="0.0.0.0")
    assert web.host == "127.0.0.1"


def test_serves_on_lan_when_password_set(monkeypatch):
    monkeypatch.setenv("MMHUE_WEB_PASSWORD", "hunter2")
    web = WebInterface(_hub(), host="0.0.0.0")
    assert web.host == "0.0.0.0"


def test_api_requires_password(monkeypatch):
    monkeypatch.setenv("MMHUE_WEB_PASSWORD", "hunter2")
    client = TestClient(WebInterface(_hub()).build_app())

    assert client.get("/api/state").status_code == 401
    assert client.get("/api/state", auth=("x", "wrong")).status_code == 401
    assert client.get("/api/state", auth=("x", "hunter2")).status_code == 200


def test_state_reports_running_dance(monkeypatch):
    monkeypatch.setenv("MMHUE_WEB_PASSWORD", "hunter2")
    client = TestClient(WebInterface(_hub()).build_app())

    body = client.get("/api/state", auth=("x", "hunter2")).json()
    assert body["dance_running"] == "birthday"
    assert body["lights"][0]["brightness"] == 50


def test_stop_reaches_a_dance_it_did_not_start(monkeypatch):
    """The whole point: stop works on cron/CLI dances, not just its own."""
    monkeypatch.setenv("MMHUE_WEB_PASSWORD", "hunter2")
    hub = _hub()
    client = TestClient(WebInterface(hub).build_app())

    r = client.post("/api/dances/stop", auth=("x", "hunter2"))
    assert r.status_code == 200
    assert r.json()["ok"] is True
    hub.dances.stop.assert_awaited_once()


def test_light_change_records_a_clean_state(monkeypatch):
    """Ordinary changes feed the non-dance history a dance restores to."""
    monkeypatch.setenv("MMHUE_WEB_PASSWORD", "hunter2")
    hub = _hub()
    client = TestClient(WebInterface(hub).build_app())

    client.post("/api/lights/l1/toggle", auth=("x", "hunter2"))
    hub.lights.toggle.assert_awaited_once_with("l1")
    hub.dances.remember_state.assert_awaited_once()
