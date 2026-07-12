from __future__ import annotations

from aiohue.v2 import HueBridgeV2

from .light_service import LightService
from .room_service import RoomService
from .scene_service import SceneService
from .animation_service import AnimationService
from .dance_service import DanceService


class ServiceHub:
    """Single entry point that all interfaces talk to."""

    def __init__(self, bridge: HueBridgeV2) -> None:
        self.lights     = LightService(bridge)
        self.rooms      = RoomService(bridge)
        self.scenes     = SceneService(bridge)
        self.animations = AnimationService(bridge)
        self.dances     = DanceService(bridge)
