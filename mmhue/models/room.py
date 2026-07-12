from dataclasses import dataclass, field


@dataclass
class RoomInfo:
    id: str
    name: str
    archetype: str          # raw string from Hue, e.g. "living", "kitchen"
    light_ids: list[str] = field(default_factory=list)
    on_count: int = 0
    total: int = 0
