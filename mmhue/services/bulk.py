"""Switching several lights at once, reliably.

The naive version — fire turn_off() at every light back to back — does not work.
The Hue bridge accepts roughly ten commands a second and silently drops the
rest, so "All off" would reliably leave a few lights on. An exception on one
light also aborted the whole loop, leaving the remainder untouched.

So: pace the commands, never let one light's failure stop the others, then read
the lights back and retry any that did not actually switch.
"""

from __future__ import annotations

import asyncio

from aiohue.v2 import HueBridgeV2
from loguru import logger

# The bridge tolerates ~10 commands/sec; leave headroom.
PACE_SECONDS = 0.12

# Time for the bridge to apply the commands and push state back to us.
SETTLE_SECONDS = 0.8

MAX_ATTEMPTS = 3


async def set_lights_on(bridge: HueBridgeV2, light_ids: list[str], on: bool) -> list[str]:
    """Switch lights on/off, verifying they took. Returns the ids that would not."""
    remaining = list(light_ids)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        for lid in remaining:
            try:
                if on:
                    await bridge.lights.turn_on(lid)
                else:
                    await bridge.lights.turn_off(lid)
            except Exception as exc:
                # Keep going: one dead bulb must not strand the whole room
                logger.warning("light {} did not accept the command: {}", lid[:8], exc)
            await asyncio.sleep(PACE_SECONDS)

        await asyncio.sleep(SETTLE_SECONDS)

        stuck = []
        for lid in remaining:
            light = bridge.lights.get(lid)
            if light is not None and light.is_on != on:
                stuck.append(lid)

        if not stuck:
            return []

        remaining = stuck
        if attempt < MAX_ATTEMPTS:
            logger.info("{} light(s) ignored '{}'; retrying (attempt {}/{})",
                        len(remaining), "on" if on else "off", attempt + 1, MAX_ATTEMPTS)

    logger.warning("{} light(s) would not switch {}", len(remaining), "on" if on else "off")
    return remaining
