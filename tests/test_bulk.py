"""Bulk on/off must actually switch every light, even on a flaky bridge."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from mmhue.services import bulk


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(bulk, "PACE_SECONDS", 0.0)
    monkeypatch.setattr(bulk, "SETTLE_SECONDS", 0.0)


def _bridge(light_ids: list[str], drop_first: set[str] | None = None,
            raise_on: set[str] | None = None) -> MagicMock:
    """A bridge that mimics the real one: silently ignores some commands."""
    drop_first = drop_first or set()
    raise_on = raise_on or set()
    seen: dict[str, int] = {}

    lights = {}
    for lid in light_ids:
        light = MagicMock()
        light.id = lid
        light.is_on = True
        lights[lid] = light

    bridge = MagicMock()
    bridge.lights.get = lights.get

    async def turn_off(lid):
        if lid in raise_on:
            raise Exception("device has communication issues")
        seen[lid] = seen.get(lid, 0) + 1
        # A dropped command: the bridge accepts it but nothing happens
        if lid in drop_first and seen[lid] == 1:
            return
        lights[lid].is_on = False

    async def turn_on(lid):
        lights[lid].is_on = True

    bridge.lights.turn_off = turn_off
    bridge.lights.turn_on = turn_on
    return bridge


async def test_retries_lights_the_bridge_dropped():
    """The actual bug: 'All off' left Dining and two kitchen lights on."""
    ids = ["a", "b", "c", "d", "e", "f", "g"]
    bridge = _bridge(ids, drop_first={"b", "d", "f"})

    stuck = await bulk.set_lights_on(bridge, ids, on=False)

    assert stuck == []
    assert all(bridge.lights.get(i).is_on is False for i in ids)


async def test_one_broken_light_does_not_strand_the_others():
    """A raising light used to abort the loop, leaving later lights untouched."""
    ids = ["a", "broken", "c"]
    bridge = _bridge(ids, raise_on={"broken"})

    stuck = await bulk.set_lights_on(bridge, ids, on=False)

    assert stuck == ["broken"]                        # reported, not hidden
    assert bridge.lights.get("a").is_on is False      # and the rest still switched
    assert bridge.lights.get("c").is_on is False
