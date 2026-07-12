from dataclasses import dataclass, field


@dataclass
class LightState:
    on: bool = False
    brightness: float = 1.0        # 0.0 – 1.0
    color_temp: int | None = None  # mirek (153–500)
    hue: float | None = None       # 0.0 – 360.0
    saturation: float | None = None  # 0.0 – 1.0
    color_xy: tuple[float, float] | None = None

    def is_color(self) -> bool:
        return self.hue is not None or self.color_xy is not None


@dataclass
class LightInfo:
    id: str
    name: str
    room: str | None
    room_id: str | None = None
    state: LightState = field(default_factory=LightState)
    is_reachable: bool = True
    supports_color: bool = False
    supports_color_temp: bool = False
