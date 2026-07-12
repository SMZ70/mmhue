"""Manages the aiohue Bridge connection and exposes typed sub-controllers."""

from __future__ import annotations

import ssl
from typing import AsyncIterator
from contextlib import asynccontextmanager

from aiohue import HueBridgeV2
from aiohue.v2.models.resource import ResourceTypes
from loguru import logger

from mmhue.config import settings


class HueBridge:
    """Thin wrapper around aiohue.HueBridgeV2 with connection lifecycle management."""

    def __init__(self) -> None:
        self._bridge = HueBridgeV2(settings.hue_bridge_host, settings.hue_bridge_app_key)

    @property
    def raw(self) -> HueBridgeV2:
        """Direct access to the aiohue bridge for advanced use."""
        return self._bridge

    async def connect(self) -> None:
        await self._bridge.initialize()
        logger.info(
            "Connected to Hue bridge at {}  (config: {})",
            settings.hue_bridge_host,
            self._bridge.config.name,
        )

    async def disconnect(self) -> None:
        await self._bridge.close()
        logger.info("Disconnected from Hue bridge")

    @asynccontextmanager
    async def connected(self) -> AsyncIterator["HueBridge"]:
        await self.connect()
        try:
            yield self
        finally:
            await self.disconnect()
