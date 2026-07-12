"""Unit tests for SceneService — use mock bridge."""

from __future__ import annotations

from unittest.mock import MagicMock
import pytest

from mmhue.services.scene_service import SceneService


def _make_bridge(scenes: list[dict]) -> MagicMock:
    bridge = MagicMock()
    mock_scenes = []
    for s in scenes:
        ms = MagicMock()
        ms.id = s["id"]
        ms.metadata.name = s["name"]
        ms.group.rid = s["group_id"]
        mock_scenes.append(ms)
    bridge.scenes.__iter__ = lambda self: iter(mock_scenes)
    bridge.scenes.get = lambda sid: next((m for m in mock_scenes if m.id == sid), None)
    bridge.rooms.__iter__ = lambda self: iter([])
    bridge.zones.__iter__ = lambda self: iter([])
    return bridge


def test_find_scene_case_insensitive():
    bridge = _make_bridge([{"id": "s1", "name": "Bright Morning", "group_id": "g1"}])
    svc = SceneService(bridge)
    scene = svc.find_scene("bright")
    assert scene is not None
    assert scene.id == "s1"


def test_find_scene_not_found():
    bridge = _make_bridge([{"id": "s1", "name": "Relax", "group_id": "g1"}])
    svc = SceneService(bridge)
    assert svc.find_scene("nonexistent") is None
