from __future__ import annotations

from aiohue.v2 import HueBridgeV2
from aiohue.v2.models.resource import ResourceTypes
from loguru import logger

from mmhue.services.bulk import set_lights_on

from mmhue.models import RoomInfo, CommandResult


class RoomService:
    def __init__(self, bridge: HueBridgeV2) -> None:
        self._bridge = bridge

    def list_rooms(self) -> list[RoomInfo]:
        light_by_id = {l.id: l for l in self._bridge.lights}
        dev_light: dict[str, str] = {}
        for dev in self._bridge.devices:
            for svc in dev.services:
                if svc.rid in light_by_id:
                    dev_light[dev.id] = svc.rid

        rooms = []
        for group in self._bridge.groups:
            if group.type != ResourceTypes.ROOM:
                continue
            ids = []
            for child in group.children:
                dev = self._bridge.devices.get(child.rid)
                if dev and dev.id in dev_light:
                    ids.append(dev_light[dev.id])
            on_count = sum(1 for lid in ids if light_by_id.get(lid) and light_by_id[lid].is_on)
            rooms.append(RoomInfo(
                id=group.id,
                name=group.metadata.name,
                archetype=group.metadata.archetype.value if group.metadata.archetype else "other",
                light_ids=ids,
                on_count=on_count,
                total=len(ids),
            ))
        return sorted(rooms, key=lambda r: r.name)

    def get_room(self, room_id: str) -> RoomInfo | None:
        return next((r for r in self.list_rooms() if r.id == room_id), None)

    async def set_on(self, room_id: str, on: bool) -> CommandResult:
        room = self.get_room(room_id)
        if not room:
            return CommandResult.error(f"Room not found")
        state = "on" if on else "off"
        stuck = await set_lights_on(self._bridge, list(room.light_ids), on)
        if stuck:
            return CommandResult.error(
                f"{room.name}: {len(stuck)} light(s) would not turn {state}"
            )
        logger.info("Room {} → {}", room.name, state)
        return CommandResult.ok(f"{room.name} {state}")
