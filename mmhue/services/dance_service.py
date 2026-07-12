from __future__ import annotations

import asyncio

from aiohue.v2 import HueBridgeV2
from loguru import logger

from mmhue.models import CommandResult
from mmhue.services.dances import REGISTRY


class DanceService:
    def __init__(self, bridge: HueBridgeV2) -> None:
        self._bridge = bridge
        self._task: asyncio.Task | None = None
        self._name: str | None = None

    @property
    def running(self) -> str | None:
        if self._task and not self._task.done():
            return self._name
        return None

    async def start(self, name: str, light_ids: list[str], **kwargs) -> CommandResult:
        if self.running:
            return CommandResult.error(f"'{self._name}' is already running — stop it first")
        fn = REGISTRY.get(name)
        if not fn:
            return CommandResult.error(f"Unknown dance '{name}'")
        self._name = name
        self._task = asyncio.create_task(fn(self._bridge, light_ids, **kwargs))
        self._task.add_done_callback(self._on_done)
        logger.info("dance '{}' started on {} lights", name, len(light_ids))
        return CommandResult.ok(f"▶ {name} started")

    async def stop(self) -> CommandResult:
        if not self.running:
            return CommandResult.error("No dance running")
        name = self._name
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        return CommandResult.ok(f"⏹ {name} stopped")

    def _on_done(self, task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception():
            logger.error("dance '{}' crashed: {}", self._name, task.exception())
        self._name = None
