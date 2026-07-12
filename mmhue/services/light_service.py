"""High-level light operations over the Hue bridge."""

from __future__ import annotations

from aiohue.v2 import HueBridgeV2
from aiohue.v2.models.resource import ResourceTypes
from loguru import logger

from mmhue.models import LightInfo, LightState, CommandResult


class LightService:
    def __init__(self, bridge: HueBridgeV2) -> None:
        self._bridge = bridge

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def list_lights(self) -> list[LightInfo]:
        lights = []
        for light in self._bridge.lights:
            device = self._bridge.lights.get_device(light.id)
            name = device.metadata.name if device else light.id
            room_name, room_id = self._room_for_light(light.id)
            state = LightState(
                on=light.is_on,
                brightness=light.brightness / 100.0,
                color_temp=light.color_temperature.mirek if light.color_temperature else None,
            )
            lights.append(
                LightInfo(
                    id=light.id,
                    name=name,
                    room=room_name,
                    room_id=room_id,
                    state=state,
                    supports_color=light.supports_color,
                    supports_color_temp=light.supports_color_temperature,
                )
            )
        return lights

    def get_light(self, light_id: str) -> LightInfo | None:
        return next((l for l in self.list_lights() if l.id == light_id), None)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def set_on(self, light_id: str, on: bool) -> CommandResult:
        if not self._bridge.lights.get(light_id):
            return CommandResult.error(f"Light {light_id!r} not found")
        if on:
            await self._bridge.lights.turn_on(light_id)
        else:
            await self._bridge.lights.turn_off(light_id)
        logger.debug("Light {} -> on={}", light_id, on)
        return CommandResult.ok(f"Light {'on' if on else 'off'}")

    async def toggle(self, light_id: str) -> CommandResult:
        info = self.get_light(light_id)
        if not info:
            return CommandResult.error(f"Light {light_id!r} not found")
        return await self.set_on(light_id, not info.state.on)

    async def set_brightness(self, light_id: str, brightness: float) -> CommandResult:
        """brightness: 0.0–1.0"""
        if not self._bridge.lights.get(light_id):
            return CommandResult.error(f"Light {light_id!r} not found")
        await self._bridge.lights.set_brightness(light_id, brightness * 100.0)
        return CommandResult.ok(f"Brightness set to {brightness:.0%}")

    async def set_color_temp(self, light_id: str, mirek: int) -> CommandResult:
        """mirek: 153 (cool/daylight) – 500 (warm/candle)"""
        light = self._bridge.lights.get(light_id)
        if not light:
            return CommandResult.error(f"Light {light_id!r} not found")
        if not light.supports_color_temperature:
            return CommandResult.error("Light does not support color temperature")
        await self._bridge.lights.set_color_temperature(light_id, mirek)
        return CommandResult.ok(f"Color temperature set")

    async def set_color(self, light_id: str, hue_deg: float) -> CommandResult:
        """hue_deg: 0–360"""
        from colorsys import hsv_to_rgb
        light = self._bridge.lights.get(light_id)
        if not light:
            return CommandResult.error(f"Light {light_id!r} not found")
        if not light.supports_color:
            return CommandResult.error("Light does not support color")
        r, g, b = hsv_to_rgb(hue_deg / 360.0, 1.0, 1.0)
        X = r * 0.4124 + g * 0.3576 + b * 0.1805
        Y = r * 0.2126 + g * 0.7152 + b * 0.0722
        Z = r * 0.0193 + g * 0.1192 + b * 0.9505
        total = X + Y + Z or 1.0
        await self._bridge.lights.set_color(light_id, X / total, Y / total)
        return CommandResult.ok("Color set")

    async def set_all_on(self, on: bool) -> CommandResult:
        for light in self._bridge.lights:
            if on:
                await self._bridge.lights.turn_on(light.id)
            else:
                await self._bridge.lights.turn_off(light.id)
        state = "on" if on else "off"
        logger.info("All lights turned {}", state)
        return CommandResult.ok(f"All lights {state}")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _room_for_light(self, light_id: str) -> tuple[str, str] | tuple[None, None]:
        for group in self._bridge.groups:
            if group.type != ResourceTypes.ROOM:
                continue
            for child in group.children:
                device = self._bridge.devices.get(child.rid)
                if device is None:
                    continue
                for svc in device.services:
                    if svc.rid == light_id:
                        return group.metadata.name, group.id
        return None, None
