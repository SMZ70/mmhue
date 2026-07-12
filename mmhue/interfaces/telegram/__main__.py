"""Entry point: python -m mmhue.interfaces.telegram  or  mmhue-telegram"""

import asyncio
import sys

from loguru import logger

from mmhue.config import settings
from mmhue.core import HueBridge
from mmhue.services import ServiceHub
from mmhue.interfaces.telegram import TelegramInterface


def main() -> None:
    logger.remove()
    logger.add(sys.stderr, level=settings.log_level)

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


async def _run() -> None:
    bridge = HueBridge()
    async with bridge.connected() as b:
        hub = ServiceHub(b.raw)
        iface = TelegramInterface(hub)
        try:
            await iface.start()
        except (KeyboardInterrupt, SystemExit):
            await iface.stop()


if __name__ == "__main__":
    main()
