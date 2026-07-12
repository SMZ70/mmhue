from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class CommandStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    NOT_FOUND = "not_found"
    UNAUTHORIZED = "unauthorized"


@dataclass
class Command:
    """Serializable intent from any interface."""

    action: str                          # e.g. "lights.set_state", "scenes.activate"
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class CommandResult:
    status: CommandStatus
    message: str
    data: Any = None

    @classmethod
    def ok(cls, message: str = "Done", data: Any = None) -> "CommandResult":
        return cls(status=CommandStatus.OK, message=message, data=data)

    @classmethod
    def error(cls, message: str, data: Any = None) -> "CommandResult":
        return cls(status=CommandStatus.ERROR, message=message, data=data)

    @property
    def success(self) -> bool:
        return self.status == CommandStatus.OK
