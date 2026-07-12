"""Custom light animations (looping sequences not natively in Hue)."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable, Awaitable
from loguru import logger

from aiohue.v2 import HueBridgeV2

from mmhue.models import CommandResult


# Type alias for animation steps: async callable that takes progress 0.0–1.0
AnimationStep = Callable[[float], Awaitable[None]]


class AnimationService:
    def __init__(self, bridge: HueBridgeV2) -> None:
        self._bridge = bridge
        self._running: dict[str, asyncio.Task] = {}  # name → active task

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self, name: str, coro: AnimationStep, *, duration: float, fps: int = 10) -> CommandResult:
        """Run an animation loop for `duration` seconds at `fps` frames/second."""
        if name in self._running:
            return CommandResult.error(f"Animation {name!r} already running")

        task = asyncio.create_task(self._run_loop(name, coro, duration=duration, fps=fps))
        self._running[name] = task
        logger.info("Animation {!r} started ({:.0f}s @ {}fps)", name, duration, fps)
        return CommandResult.ok(f"Animation '{name}' started")

    async def stop(self, name: str) -> CommandResult:
        task = self._running.pop(name, None)
        if not task:
            return CommandResult.error(f"No running animation {name!r}")
        task.cancel()
        logger.info("Animation {!r} stopped", name)
        return CommandResult.ok(f"Animation '{name}' stopped")

    async def stop_all(self) -> CommandResult:
        names = list(self._running.keys())
        for name in names:
            await self.stop(name)
        return CommandResult.ok(f"Stopped {len(names)} animation(s)")

    def list_running(self) -> list[str]:
        return list(self._running.keys())

    # ------------------------------------------------------------------
    # Built-in animations
    # ------------------------------------------------------------------

    async def breathe(self, light_ids: list[str], *, duration: float = 10.0, min_bri: float = 0.1, max_bri: float = 1.0) -> CommandResult:
        """Slow sine-wave brightness pulse."""

        async def step(t: float) -> None:
            bri = min_bri + (max_bri - min_bri) * (0.5 + 0.5 * math.sin(t * 2 * math.pi))
            for lid in light_ids:
                await self._bridge.lights.set_state(lid, brightness=bri * 100.0)

        return await self.start("breathe", step, duration=duration, fps=8)

    async def color_cycle(self, light_ids: list[str], *, duration: float = 30.0) -> CommandResult:
        """Cycle through the full hue wheel."""

        async def step(t: float) -> None:
            hue = t * 360.0
            for lid in light_ids:
                light = self._bridge.lights.get(lid)
                if light and light.color:
                    # Convert hue degrees to xy — simplified; for full accuracy use a color lib
                    xy = _hue_to_xy(hue)
                    await self._bridge.lights.set_state(lid, color_xy=xy)

        return await self.start("color_cycle", step, duration=duration, fps=5)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _run_loop(self, name: str, step: AnimationStep, *, duration: float, fps: int) -> None:
        interval = 1.0 / fps
        elapsed = 0.0
        try:
            while elapsed < duration:
                progress = elapsed / duration
                await step(progress)
                await asyncio.sleep(interval)
                elapsed += interval
        except asyncio.CancelledError:
            pass
        finally:
            self._running.pop(name, None)
            logger.debug("Animation {!r} finished", name)


def _hue_to_xy(hue_deg: float) -> tuple[float, float]:
    """Very rough hue→CIE xy conversion for color cycling (not perceptually accurate)."""
    import colorsys
    h = hue_deg / 360.0
    r, g, b = colorsys.hsv_to_rgb(h, 1.0, 1.0)
    # sRGB to XYZ (wide gamut approximation)
    X = r * 0.4124 + g * 0.3576 + b * 0.1805
    Y = r * 0.2126 + g * 0.7152 + b * 0.0722
    Z = r * 0.0193 + g * 0.1192 + b * 0.9505
    total = X + Y + Z
    if total == 0:
        return (0.3127, 0.3290)
    return (X / total, Y / total)
