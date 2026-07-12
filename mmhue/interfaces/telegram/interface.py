"""Telegram interface — wraps python-telegram-bot Application."""

from __future__ import annotations

import asyncio

from telegram.ext import Application
from loguru import logger

from mmhue.config import settings
from mmhue.interfaces.base import BaseInterface
from mmhue.services import ServiceHub
from .handlers import register_handlers


class TelegramInterface(BaseInterface):
    def __init__(self, hub: ServiceHub) -> None:
        super().__init__(hub)
        self._app: Application | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        self._app = Application.builder().token(settings.telegram_bot_token).build()
        register_handlers(self._app, self.hub)
        logger.info("Telegram interface starting (polling)…")
        async with self._app:
            await self._app.start()
            await self._app.updater.start_polling(drop_pending_updates=True)
            await self._stop_event.wait()
            await self._app.updater.stop()
            await self._app.stop()

    async def stop(self) -> None:
        logger.info("Telegram interface stopping…")
        self._stop_event.set()
