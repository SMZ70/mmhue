"""Run a dance so that anyone can stop it.

A dance may be started from the bot, the web UI, the CLI or cron — different
processes, possibly different containers. Whoever presses stop must be able to
stop it, so every dance is run alongside a watcher that polls the shared stop
flag and cancels the dance when one appears.

Cancelling (rather than killing) matters: it lets the dance's own cleanup run,
which restores the lights instead of stranding them mid-strobe.
"""

from __future__ import annotations

import asyncio
import time

from aiohue.v2 import HueBridgeV2
from loguru import logger

from mmhue.services import dance_state
from mmhue.services.dances import REGISTRY

POLL_SECONDS = 0.4


async def _watch(task: asyncio.Task, started_at: float, token: str) -> None:
    """Poll for a stop request, and heartbeat so others know we are alive."""
    while not task.done():
        if dance_state.stop_requested_since(started_at):
            logger.info("stop requested — cancelling dance")
            task.cancel()
            return
        dance_state.heartbeat(token)
        await asyncio.sleep(POLL_SECONDS)


async def run_dance(bridge: HueBridgeV2, name: str, light_ids: list[str], **kwargs) -> None:
    """Run a dance to completion, or until someone asks it to stop."""
    started_at = time.time()
    task = asyncio.create_task(REGISTRY[name](bridge, light_ids, **kwargs))

    # The dance registers itself from inside; give it a moment, then track it
    await asyncio.sleep(0.05)
    token = next((e["token"] for e in dance_state.running_all() if e["name"] == name), "")
    watcher = asyncio.create_task(_watch(task, started_at, token))
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
