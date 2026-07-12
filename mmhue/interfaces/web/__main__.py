"""Run the web interface:  python -m mmhue.interfaces.web"""

from __future__ import annotations

import asyncio
import os

from loguru import logger

from mmhue.core import HueBridge
from mmhue.interfaces.web.interface import WebInterface
from mmhue.services import ServiceHub


async def _run() -> None:
    bridge = HueBridge()
    async with bridge.connected() as b:
        hub = ServiceHub(b.raw)
        web = WebInterface(
            hub,
            host=os.getenv("MMHUE_WEB_HOST", "0.0.0.0"),
            port=int(os.getenv("MMHUE_WEB_PORT", "8080")),
        )
        try:
            await web.start()
        finally:
            await web.stop()


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("web interface stopped")


if __name__ == "__main__":
    main()
