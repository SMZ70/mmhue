"""Run a named dance on the TV light and all kitchen lights.

Usage:
    uv run python scripts/dance.py [dance_name] [duration_seconds]

Available dances: chromatic_drift  police  ambulance  thunderstorm  bandari  birthday
"""

import asyncio
import sys

from loguru import logger

from mmhue.core import HueBridge
from mmhue.services.dances import REGISTRY
from aiohue.v2.models.resource import ResourceTypes


async def find_lights(bridge, room_names: list[str]) -> dict[str, list[str]]:
    dev_by_id = {dev.id: dev for dev in bridge.devices}
    light_by_id = {light.id: light for light in bridge.lights}

    light_id_by_dev: dict[str, str] = {}
    for dev in bridge.devices:
        for svc in dev.services:
            if svc.rid in light_by_id:
                light_id_by_dev[dev.id] = svc.rid

    result: dict[str, list[str]] = {}
    for group in bridge.groups:
        if group.type != ResourceTypes.ROOM:
            continue
        for target in room_names:
            if target.lower() not in group.metadata.name.lower():
                continue
            ids = []
            for child in group.children:
                dev = dev_by_id.get(child.rid)
                if dev and dev.id in light_id_by_dev:
                    ids.append(light_id_by_dev[dev.id])
            if ids:
                result[group.metadata.name] = ids
    return result


async def main(dance_name: str, duration: float) -> None:
    dance_fn = REGISTRY.get(dance_name)
    if not dance_fn:
        logger.error("Unknown dance '{}'. Available: {}", dance_name, ", ".join(REGISTRY))
        return

    bridge = HueBridge()
    async with bridge.connected() as b:
        raw = b.raw
        rooms = await find_lights(raw, ["Living room", "Kitchen"])
        if not rooms:
            logger.error("No matching lights found")
            return

        all_ids: list[str] = []
        for room, ids in rooms.items():
            logger.info("  {}: {} light(s)", room, len(ids))
            all_ids.extend(ids)

        logger.info("Running '{}' on {} lights for {:.0f}s  (Ctrl+C to stop)",
                    dance_name, len(all_ids), duration)
        try:
            await dance_fn(raw, all_ids, duration=duration)
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass

        logger.info("Done.")


if __name__ == "__main__":
    name     = sys.argv[1] if len(sys.argv) > 1 else "chromatic_drift"
    duration = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0
    try:
        asyncio.run(main(name, duration))
    except KeyboardInterrupt:
        pass
