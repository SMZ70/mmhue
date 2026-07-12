"""Scene activation and listing."""

from __future__ import annotations

from aiohue.v2 import HueBridgeV2
from aiohue.v2.models.resource import ResourceTypes
from loguru import logger

from mmhue.models import SceneInfo, CommandResult


class SceneService:
    def __init__(self, bridge: HueBridgeV2) -> None:
        self._bridge = bridge

    def list_scenes(self) -> list[SceneInfo]:
        scenes = []
        for scene in self._bridge.scenes:
            group = self._bridge.scenes.get_group(scene.id) if scene.group else None
            group_name = group.metadata.name if group else "?"
            scenes.append(
                SceneInfo(
                    id=scene.id,
                    name=scene.metadata.name,
                    group_id=scene.group.rid if scene.group else "",
                    group_name=group_name,
                )
            )
        return sorted(scenes, key=lambda s: (s.group_name, s.name))

    def find_scene(self, name: str, group: str | None = None) -> SceneInfo | None:
        name_lower = name.lower()
        candidates = [s for s in self.list_scenes() if name_lower in s.name.lower()]
        if group:
            candidates = [s for s in candidates if group.lower() in s.group_name.lower()]
        return candidates[0] if candidates else None

    async def activate(self, scene_id: str) -> CommandResult:
        scene = self._bridge.scenes.get(scene_id)
        if not scene:
            return CommandResult.error(f"Scene {scene_id!r} not found")
        await self._bridge.scenes.recall(scene_id)
        logger.info("Scene {} activated", scene.metadata.name)
        return CommandResult.ok(f"Scene '{scene.metadata.name}' activated")

    async def activate_by_name(self, name: str, group: str | None = None) -> CommandResult:
        scene = self.find_scene(name, group)
        if not scene:
            q = f"{name!r}" + (f" in {group!r}" if group else "")
            return CommandResult.error(f"No scene matching {q}")
        return await self.activate(scene.id)
