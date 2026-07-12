"""Unit tests for the birthday dance — use a mock bridge, no real lights."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from mmhue.services.dances import REGISTRY, birthday


def _make_bridge(light_ids: list[str]) -> MagicMock:
    bridge = MagicMock()

    lights = {}
    for lid in light_ids:
        light = MagicMock()
        light.id = lid
        light.is_on = True
        light.brightness = 42.0
        light.color_temperature.mirek_valid = False
        light.color.xy.x = 0.3
        light.color.xy.y = 0.3
        lights[lid] = light

    bridge.lights.get = lights.get
    bridge.lights.set_state = AsyncMock()
    return bridge


def test_birthday_is_registered():
    assert REGISTRY["birthday"] is birthday


async def test_birthday_runs_and_restores_state():
    ids = ["l1", "l2", "l3"]
    bridge = _make_bridge(ids)

    await birthday(bridge, ids, duration=6.0)

    calls = bridge.lights.set_state.await_args_list
    assert calls, "dance issued no commands"

    # Every light was driven, and none outside the given set
    touched = {c.args[0] for c in calls}
    assert touched == set(ids)

    # Brightness stays inside the Hue 0-100 range
    for c in calls:
        if "brightness" in c.kwargs:
            assert 0 <= c.kwargs["brightness"] <= 100

    # The final command per light restores its captured state
    for lid in ids:
        last = [c for c in calls if c.args[0] == lid][-1]
        assert last.kwargs["on"] is True
        assert last.kwargs["brightness"] == 42.0


async def test_birthday_restores_state_when_cancelled():
    ids = ["l1", "l2"]
    bridge = _make_bridge(ids)

    task = asyncio.create_task(birthday(bridge, ids, duration=60.0))
    await asyncio.sleep(1.0)
    task.cancel()
    await task  # must swallow CancelledError and restore, not propagate

    for lid in ids:
        last = [c for c in bridge.lights.set_state.await_args_list if c.args[0] == lid][-1]
        assert last.kwargs["brightness"] == 42.0


async def test_birthday_with_no_lights_is_a_noop():
    bridge = _make_bridge([])
    await birthday(bridge, [], duration=5.0)
    bridge.lights.set_state.assert_not_awaited()


async def test_white_only_light_still_dances():
    """A bulb that rejects colour must keep getting brightness, not be dropped."""
    from mmhue.services import dances

    ids = ["colour", "white"]
    bridge = _make_bridge(ids)
    dances._MONO.discard("white")

    async def set_state(lid, **kwargs):
        if lid == "white" and "color_xy" in kwargs:
            raise Exception("attribute (.color.xy) is not supported by resource white")

    bridge.lights.set_state = AsyncMock(side_effect=set_state)

    await birthday(bridge, ids, duration=6.0)

    calls = bridge.lights.set_state.await_args_list
    white = [c for c in calls if c.args[0] == "white"]

    # It kept being driven all the way through, on brightness alone
    assert len(white) > 5
    assert all("color_xy" not in c.kwargs for c in white[-3:])
    assert any("brightness" in c.kwargs for c in white)


async def test_sigterm_restores_lights():
    """A stop command must not leave the room frozen mid-strobe."""
    ids = ["l1", "l2"]
    bridge = _make_bridge(ids)

    task = asyncio.create_task(birthday(bridge, ids, duration=3600.0))
    await asyncio.sleep(1.0)

    # This is what the signal handler in interfaces/cli.py does
    task.cancel()
    await task

    for lid in ids:
        last = [c for c in bridge.lights.set_state.await_args_list if c.args[0] == lid][-1]
        assert last.kwargs["on"] is True
        assert last.kwargs["brightness"] == 42.0


# ---------------------------------------------------------------------------
# Shared dance state: never "restore" the room to a mid-strobe colour
# ---------------------------------------------------------------------------

async def test_second_dance_restores_to_clean_state_not_midstrobe(tmp_path, monkeypatch):
    """A dance starting mid-party must not snapshot the strobe and restore to it."""
    from mmhue.services import dance_state

    monkeypatch.setattr(dance_state, "STATE_DIR", tmp_path)
    monkeypatch.setattr(dance_state, "DANCE_FILE", tmp_path / "dance.json")
    monkeypatch.setattr(dance_state, "HISTORY_FILE", tmp_path / "history.json")
    monkeypatch.setattr(dance_state, "LOCK_FILE", tmp_path / ".lock")

    ids = ["l1", "l2"]
    bridge = _make_bridge(ids)

    # The room as the user left it: warm, dim. This is the clean state.
    dance_state.record_clean([
        {"id": "l1", "found": True, "on": True, "brightness": 20.0, "color_temp": 400},
        {"id": "l2", "found": True, "on": True, "brightness": 20.0, "color_temp": 400},
    ])

    # Dance A is already running (say, launched by cron)
    dance_state.begin("birthday", "cron")

    # The bridge now reports a garish mid-strobe state
    for lid in ids:
        bridge.lights.get(lid).brightness = 100.0

    # Dance B starts and finishes
    await birthday(bridge, ids, duration=5.0)

    # It must restore the warm dim state, NOT brightness 100 from the strobe
    for lid in ids:
        last = [c for c in bridge.lights.set_state.await_args_list if c.args[0] == lid][-1]
        assert last.kwargs["brightness"] == 20.0
        assert last.kwargs.get("color_temp") == 400


async def test_no_clean_state_turns_lights_off(tmp_path, monkeypatch):
    """With nothing safe to restore to, go dark rather than freeze mid-colour."""
    from mmhue.services import dance_state

    monkeypatch.setattr(dance_state, "STATE_DIR", tmp_path)
    monkeypatch.setattr(dance_state, "DANCE_FILE", tmp_path / "dance.json")
    monkeypatch.setattr(dance_state, "HISTORY_FILE", tmp_path / "history.json")
    monkeypatch.setattr(dance_state, "LOCK_FILE", tmp_path / ".lock")

    ids = ["l1", "l2"]
    bridge = _make_bridge(ids)

    dance_state.begin("birthday", "cron")   # another dance running, no history at all

    await birthday(bridge, ids, duration=5.0)

    for lid in ids:
        last = [c for c in bridge.lights.set_state.await_args_list if c.args[0] == lid][-1]
        assert last.kwargs["on"] is False
