"""Web interface stub — FastAPI + SSE for real-time light state updates."""

from __future__ import annotations

from mmhue.interfaces.base import BaseInterface
from mmhue.services import ServiceHub


class WebInterface(BaseInterface):
    """
    FastAPI-based HTTP interface.

    Routes to implement:
        GET  /lights              → list lights
        POST /lights/{id}/on      → turn on
        POST /lights/{id}/off     → turn off
        POST /lights/{id}/bri     → set brightness  {brightness: 0.0–1.0}
        GET  /scenes              → list scenes
        POST /scenes/{id}/activate
        POST /animations/breathe  → start breathe
        DELETE /animations        → stop all
        GET  /events              → SSE stream of bridge events
    """

    def __init__(self, hub: ServiceHub, host: str = "0.0.0.0", port: int = 8080) -> None:
        super().__init__(hub)
        self.host = host
        self.port = port

    async def start(self) -> None:
        raise NotImplementedError("Install mmhue[web] and implement WebInterface.start()")

    async def stop(self) -> None:
        pass
