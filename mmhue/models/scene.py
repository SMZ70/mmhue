from dataclasses import dataclass


@dataclass
class SceneInfo:
    id: str
    name: str
    group_id: str
    group_name: str
