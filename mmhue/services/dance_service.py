from __future__ import annotations

import asyncio

from aiohue.v2 import HueBridgeV2
from loguru import logger

from mmhue.models import CommandResult
from mmhue.services import dance_state
from mmhue.services.dance_runner import run_dance
from mmhue.services.dances import REGISTRY, record_clean_state


class DanceService:
    """Runs dances, and can see and stop dances started anywhere else too.

    Dances also get launched from the CLI and from cron, in separate processes.
    Those are visible through the shared dance_state file, and stoppable through
    it: stop writes a request that every dance runner watches for. So a Stop
    here really does stop the dance, whoever started it.
    """

    def __init__(self, bridge: HueBridgeV2) -> None:
        self._bridge = bridge
        self._task: asyncio.Task | None = None
        self._name: str | None = None

    @property
    def running(self) -> str | None:
        """Name of the dance playing right now, ours or anyone else's."""
        if self._task and not self._task.done():
            return self._name
        return dance_state.running()

    @property
    def running_here(self) -> bool:
        return bool(self._task and not self._task.done())

    def registry_names(self) -> list[str]:
        return list(REGISTRY)

    async def start(self, name: str, light_ids: list[str], **kwargs) -> CommandResult:
        current = self.running
        if current:
            return CommandResult.error(f"'{current}' is already running — stop it first")
        if name not in REGISTRY:
            return CommandResult.error(f"Unknown dance '{name}'")

        # A stale stop flag would kill the dance we are about to start
        dance_state.clear_stop()

        self._name = name
        self._task = asyncio.create_task(run_dance(self._bridge, name, light_ids, **kwargs))
        self._task.add_done_callback(self._on_done)
        logger.info("dance '{}' started on {} lights", name, len(light_ids))
        return CommandResult.ok(f"▶ {name} started")

    async def stop(self) -> CommandResult:
        name = self.running
        if not name:
            return CommandResult.error("No dance running")

        # Tell every runner, in any process, to stop. Ours included.
        dance_state.request_stop()

        if self.running_here and self._task:
            await asyncio.gather(self._task, return_exceptions=True)
        else:
            # Someone else's process owns it; give its watcher a moment to react
            for _ in range(20):
                await asyncio.sleep(0.3)
                if not dance_state.running():
                    break
            else:
                return CommandResult.error(f"'{name}' did not stop — check the logs")

        return CommandResult.ok(f"⏹ {name} stopped")

    async def remember_state(self, light_ids: list[str]) -> None:
        """Record the current lights as a clean state a dance can return to."""
        await record_clean_state(self._bridge, light_ids)

    def _on_done(self, task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception():
            logger.error("dance '{}' crashed: {}", self._name, task.exception())
        self._name = None
