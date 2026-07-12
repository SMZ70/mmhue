"""Abstract base for all interfaces (Telegram, Web, CLI, …)."""

from __future__ import annotations

from abc import ABC, abstractmethod

from mmhue.services import ServiceHub


class BaseInterface(ABC):
    """
    An interface receives a ServiceHub and runs until stopped.
    Concrete implementations (Telegram, FastAPI, CLI) inherit from this.
    """

    def __init__(self, hub: ServiceHub) -> None:
        self.hub = hub

    @abstractmethod
    async def start(self) -> None:
        """Start the interface; should block until stopped."""

    @abstractmethod
    async def stop(self) -> None:
        """Graceful shutdown."""
