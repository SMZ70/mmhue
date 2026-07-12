from __future__ import annotations

import asyncio

from aiohue.v2 import HueBridgeV2
from loguru import logger

from mmhue.models import CommandResult
from mmhue.services import dance_state
from mmhue.services.dances import REGISTRY, record_clean_state


class DanceService:
    """Runs dances, and reports on dances started anywhere else too.

    Dances can also be launched from the CLI or a cron job, in separate
    processes. Those are visible through the shared dance_state file, so the
    bot can show what is playing even when it did not start it — though it
    cannot cancel a task living in another process.
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
        """True only if *this* process owns the running dance (so we can stop it)."""
        return bool(self._task and not self._task.done())

    async def start(self, name: str, light_ids: list[str], **kwargs) -> CommandResult:
        current = self.running
        if current:
            return CommandResult.error(f"'{current}' is already running — stop it first")
        fn = REGISTRY.get(name)
        if not fn:
            return CommandResult.error(f"Unknown dance '{name}'")
        self._name = name
        self._task = asyncio.create_task(fn(self._bridge, light_ids, **kwargs))
        self._task.add_done_callback(self._on_done)
        logger.info("dance '{}' started on {} lights", name, len(light_ids))
        return CommandResult.ok(f"▶ {name} started")

    async def stop(self) -> CommandResult:
        if self.running_here:
            name = self._name
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            return CommandResult.ok(f"⏹ {name} stopped")

        external = dance_state.running()
        if external:
            # Someone else's process owns it; we have no handle to cancel.
            return CommandResult.error(
                f"'{external}' was started outside the bot — stop it where it began"
            )
        return CommandResult.error("No dance running")

    async def remember_state(self, light_ids: list[str]) -> None:
        """Record the current lights as a clean state a dance can return to."""
        await record_clean_state(self._bridge, light_ids)

    def _on_done(self, task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception():
            logger.error("dance '{}' crashed: {}", self._name, task.exception())
        self._name = None
