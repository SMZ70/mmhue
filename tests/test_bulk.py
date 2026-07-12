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


# ---------------------------------------------------------------------------
# Excluded rooms never dance
# ---------------------------------------------------------------------------

def _light(lid: str, room: str) -> MagicMock:
    light = MagicMock()
    light.id, light.room = lid, room
    return light


def test_excluded_rooms_are_kept_out_of_dances(monkeypatch):
    """A strobing hallway at midnight is a hazard, not a party."""
    from mmhue.config import settings
    from mmhue.services.light_service import LightService

    monkeypatch.setattr(settings, "dance_exclude_rooms", ["Hallway"])

    svc = LightService(MagicMock())
    monkeypatch.setattr(svc, "list_lights", lambda: [
        _light("l1", "Living room"),
        _light("l2", "Kitchen"),
        _light("l3", "Hallway"),      # must sit the dance out
        _light("l4", None),           # no room: still allowed
    ])

    ids = [x.id for x in svc.danceable_lights()]
    assert ids == ["l1", "l2", "l4"]
    assert [x.id for x in svc.list_lights()] == ["l1", "l2", "l3", "l4"]  # still controllable


def test_no_exclusions_means_every_light_dances(monkeypatch):
    from mmhue.config import settings
    from mmhue.services.light_service import LightService

    monkeypatch.setattr(settings, "dance_exclude_rooms", [])
    svc = LightService(MagicMock())
    monkeypatch.setattr(svc, "list_lights", lambda: [_light("l1", "Hallway")])
    assert [x.id for x in svc.danceable_lights()] == ["l1"]
