"""Web interface — FastAPI JSON API + a single-page control panel.

Mirrors the Telegram bot (rooms, lights, scenes, dances) and adds a Stop that
works on *any* dance, including ones started by cron or the CLI, because
stopping goes through the shared dance-state flag rather than a local task.

Auth: set MMHUE_WEB_PASSWORD and the panel asks for it (HTTP basic). Without a
password set, the server refuses to listen on anything but localhost — an
unauthenticated light switch on an open home network is not a good default.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from loguru import logger

from mmhue.interfaces.base import BaseInterface
from mmhue.services import ServiceHub

_STATIC = Path(__file__).parent / "static"


class WebInterface(BaseInterface):
    def __init__(self, hub: ServiceHub, host: str = "0.0.0.0", port: int = 8080) -> None:
        super().__init__(hub)
        self.password = os.getenv("MMHUE_WEB_PASSWORD", "").strip()
        if not self.password and host != "127.0.0.1":
            logger.warning(
                "MMHUE_WEB_PASSWORD is not set — refusing to serve on {}; "
                "binding to localhost only", host,
            )
            host = "127.0.0.1"
        self.host = host
        self.port = port
        self._server = None

    # ── App ──────────────────────────────────────────────────────────────────

    def build_app(self):
        from fastapi import Depends, FastAPI, HTTPException, status
        from fastapi.responses import FileResponse
        from fastapi.security import HTTPBasic, HTTPBasicCredentials

        app = FastAPI(title="mmhue", docs_url=None, redoc_url=None)
        security = HTTPBasic(auto_error=bool(self.password))

        def auth(creds: HTTPBasicCredentials | None = Depends(security)) -> None:  # noqa: B008
            if not self.password:
                return
            # compare_digest: constant time, so the password cannot be guessed
            # character by character from response timing
            if not creds or not secrets.compare_digest(creds.password, self.password):
                raise HTTPException(
                    status.HTTP_401_UNAUTHORIZED,
                    "Wrong password",
                    headers={"WWW-Authenticate": "Basic"},
                )

        hub = self.hub

        @app.get("/")
        async def index() -> FileResponse:
            return FileResponse(_STATIC / "index.html")

        @app.get("/api/state")
        async def state(_: None = Depends(auth)) -> dict:
            lights = hub.lights.list_lights()
            return {
                "rooms": [
                    {
                        "id": r.id, "name": r.name, "archetype": r.archetype,
                        "on_count": r.on_count, "total": r.total,
                    }
                    for r in hub.rooms.list_rooms()
                ],
                "lights": [
                    {
                        "id": light.id, "name": light.name,
                        "room": light.room, "room_id": light.room_id,
                        "on": light.state.on,
                        "brightness": round(light.state.brightness * 100),
                        "color_temp": light.state.color_temp,
                        "supports_color": light.supports_color,
                        "supports_color_temp": light.supports_color_temp,
                    }
                    for light in sorted(lights, key=lambda x: x.name)
                ],
                "scenes": [
                    {"id": s.id, "name": s.name, "group_id": s.group_id,
                     "group_name": s.group_name}
                    for s in hub.scenes.list_scenes()
                ],
                "dances": sorted(hub.dances.registry_names()),
                "dance_running": hub.dances.running,
                "on_count": sum(1 for x in lights if x.state.on),
                "total": len(lights),
            }

        # ── Lights ───────────────────────────────────────────────────────────

        @app.post("/api/lights/{light_id}/toggle")
        async def light_toggle(light_id: str, _: None = Depends(auth)) -> dict:
            await hub.lights.toggle(light_id)
            await self._remember()
            return {"ok": True}

        @app.post("/api/lights/{light_id}/brightness/{pct}")
        async def light_brightness(light_id: str, pct: int, _: None = Depends(auth)) -> dict:
            await hub.lights.set_brightness(light_id, max(0, min(100, pct)) / 100.0)
            await self._remember()
            return {"ok": True}

        @app.post("/api/lights/{light_id}/color/{hue}")
        async def light_color(light_id: str, hue: float, _: None = Depends(auth)) -> dict:
            await hub.lights.set_color(light_id, hue)
            await self._remember()
            return {"ok": True}

        @app.post("/api/lights/{light_id}/ct/{mirek}")
        async def light_ct(light_id: str, mirek: int, _: None = Depends(auth)) -> dict:
            await hub.lights.set_color_temp(light_id, mirek)
            await self._remember()
            return {"ok": True}

        # ── Rooms / all ──────────────────────────────────────────────────────

        @app.post("/api/rooms/{room_id}/{onoff}")
        async def room_onoff(room_id: str, onoff: str, _: None = Depends(auth)) -> dict:
            await hub.rooms.set_on(room_id, onoff == "on")
            await self._remember()
            return {"ok": True}

        @app.post("/api/all/{onoff}")
        async def all_onoff(onoff: str, _: None = Depends(auth)) -> dict:
            await hub.lights.set_all_on(onoff == "on")
            await self._remember()
            return {"ok": True}

        # ── Scenes ───────────────────────────────────────────────────────────

        @app.post("/api/scenes/{scene_id}/activate")
        async def scene_activate(scene_id: str, _: None = Depends(auth)) -> dict:
            result = await hub.scenes.activate(scene_id)
            await self._remember()
            return {"ok": result.success, "message": result.message}

        # ── Dances ───────────────────────────────────────────────────────────

        @app.post("/api/dances/{name}/start")
        async def dance_start(name: str, _: None = Depends(auth)) -> dict:
            ids = [light.id for light in hub.lights.danceable_lights()]
            result = await hub.dances.start(name, ids)
            return {"ok": result.success, "message": result.message}

        @app.post("/api/dances/stop")
        async def dance_stop(_: None = Depends(auth)) -> dict:
            # Works even for dances started by cron or the CLI
            result = await hub.dances.stop()
            return {"ok": result.success, "message": result.message}

        return app

    async def _remember(self) -> None:
        await self.hub.dances.remember_state(
            [light.id for light in self.hub.lights.list_lights()]
        )

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        import uvicorn

        config = uvicorn.Config(
            self.build_app(), host=self.host, port=self.port,
            log_level="info", access_log=False,
        )
        self._server = uvicorn.Server(config)
        logger.info("web interface on http://{}:{}  (auth: {})",
                    self.host, self.port, "on" if self.password else "OFF")
        await self._server.serve()

    async def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
