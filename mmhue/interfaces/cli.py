"""CLI interface — run a dance from the command line, a script, or cron.

Unlike scripts/dance.py this lives inside the package, so it ships in the
Docker image and can be invoked in a running container:

    python -m mmhue.interfaces.cli birthday 300
    python -m mmhue.interfaces.cli thunderstorm 60 kitchen

Blocks until the dance finishes, so a scheduler can treat it as a job.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
from contextlib import suppress

from loguru import logger

from mmhue.core import HueBridge
from mmhue.services import ServiceHub
from mmhue.services.dances import REGISTRY


async def run_dance(name: str, duration: float, rooms: list[str]) -> int:
    bridge = HueBridge()
    async with bridge.connected() as b:
        hub = ServiceHub(b.raw)

        lights = hub.lights.list_lights()
        if rooms:
            wanted = [r.lower() for r in rooms]
            lights = [
                light for light in lights
                if light.room and any(w in light.room.lower() for w in wanted)
            ]

        light_ids = [light.id for light in lights]
        if not light_ids:
            logger.error("No lights matched {}", rooms or "any room")
            return 1

        logger.info("running '{}' on {} lights for {:.0f}s", name, len(light_ids), duration)
        task = asyncio.create_task(REGISTRY[name](b.raw, light_ids, duration=duration))

        # A scheduler or a "stop" command kills us with SIGTERM. Cancel the dance
        # rather than dying where we stand, so its cleanup restores the lights
        # instead of leaving the room frozen mid-strobe.
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with suppress(NotImplementedError):
                loop.add_signal_handler(sig, task.cancel)

        with suppress(asyncio.CancelledError):
            await task
        logger.info("done")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mmhue-dance", description=__doc__)
    parser.add_argument("dance", choices=sorted(REGISTRY))
    parser.add_argument("duration", nargs="?", type=float, default=60.0,
                        help="seconds (default: 60)")
    parser.add_argument("room", nargs="*",
                        help="room names to match; default is every room")
    args = parser.parse_args(argv)

    try:
        return asyncio.run(run_dance(args.dance, args.duration, args.room))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
