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
