"""Telegram handlers — 3-level navigation: Home → Room → Light."""

from __future__ import annotations

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from mmhue.models import LightInfo, RoomInfo, SceneInfo
from mmhue.services import ServiceHub
from mmhue.services.dances import REGISTRY as DANCE_REGISTRY
from .auth import restricted


# ---------------------------------------------------------------------------
# Room archetype → emoji
# ---------------------------------------------------------------------------

_ROOM_ICON: dict[str, str] = {
    "living":       "🛋",  "lounge":    "🛋",  "tv":           "📺",
    "kitchen":      "🍳",  "dining":    "🍽",
    "bedroom":      "🛏",  "kids_bedroom": "🧸", "guest_room": "🛏",
    "bathroom":     "🚿",  "nursery":   "🧸",
    "hallway":      "🚪",  "corridor":  "🚪",  "staircase":   "🪜",
    "office":       "💼",  "study":     "📚",
    "garage":       "🚗",  "gym":       "🏋",
    "garden":       "🌿",  "terrace":   "🌿",  "balcony":      "🌿",
    "laundry":      "🧺",  "storage":   "📦",  "attic":        "🏠",
    "recreation":   "🎮",  "man_cave":  "🎮",
    "other":        "🏠",
}

_DANCE_ICON: dict[str, str] = {
    "chromatic_drift": "🌈",
    "police":          "🚔",
    "ambulance":       "🚑",
    "thunderstorm":    "⛈",
    "bandari":         "🎶",
    "birthday":        "🎂",
}

_BRI_STEPS   = [20, 40, 60, 80, 100]
_CT_PRESETS  = [("🕯", 450), ("💛", 380), ("⬜", 300), ("🔵", 230), ("☀", 153)]
_COL_PRESETS = [("🔴", 0), ("🟠", 25), ("🟡", 60), ("🟢", 120),
                ("🩵", 190), ("🔵", 240), ("🟣", 280), ("🩷", 320)]


async def _edit(query, text: str, kb: InlineKeyboardMarkup) -> None:
    """edit_message_text, silently ignoring 'not modified' errors."""
    try:
        await query.edit_message_text(text, reply_markup=kb)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


def _btn(label: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=data)

def _noop(label: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data="noop")

def _room_icon(archetype: str) -> str:
    return _ROOM_ICON.get(archetype.lower(), "🏠")


# ---------------------------------------------------------------------------
# View builders
# ---------------------------------------------------------------------------

def _home_view(rooms: list[RoomInfo], total_on: int, total: int) -> tuple[str, InlineKeyboardMarkup]:
    text = f"mmhue\n{total_on} of {total} lights on"
    rows = []
    for room in rooms:
        icon  = _room_icon(room.archetype)
        state = "●" if room.on_count else "○"
        rows.append([_btn(f"{icon}  {room.name}  {state} {room.on_count}/{room.total}",
                          f"room:{room.id}")])
    rows.append([_btn("💡 All on", "all_on"),  _btn("🌑 All off", "all_off")])
    rows.append([_btn("🎬 Scenes", "scenes"),   _btn("✨ Dances", "dances")])
    rows.append([_btn("↻ Refresh", "home")])
    return text, InlineKeyboardMarkup(rows)


def _room_view(room: RoomInfo, lights: list[LightInfo],
               scenes: list[SceneInfo]) -> tuple[str, InlineKeyboardMarkup]:
    icon  = _room_icon(room.archetype)
    text  = f"{icon}  {room.name}\n{room.on_count} of {room.total} on"
    rows  = []
    rows.append([_btn("💡 All on",  f"room_on:{room.id}"),
                 _btn("🌑 All off", f"room_off:{room.id}")])
    if scenes:
        rows.append([_btn("🎬 Scenes", f"room_scenes:{room.id}")])

    if lights:
        for light in sorted(lights, key=lambda l: l.name):
            icon_l = "💡" if light.state.on else "○"
            bri    = f"  {light.state.brightness:.0%}" if light.state.on else ""
            rows.append([_btn(f"{icon_l}  {light.name}{bri}", f"lp:{light.id}")])

    rows.append([_btn("← Home", "home")])
    return text, InlineKeyboardMarkup(rows)


def _light_view(light: LightInfo) -> tuple[str, InlineKeyboardMarkup]:
    icon   = "💡" if light.state.on else "○"
    bri_pct = round(light.state.brightness * 100)
    parts  = ["ON" if light.state.on else "OFF", f"{bri_pct}%"]
    if light.state.color_temp:
        parts.append(_ct_name(light.state.color_temp))
    room_icon = _room_icon("other")  # fallback; room archetype not in LightInfo
    text = (f"{icon}  {light.name}\n"
            f"{light.room or '—'}  ·  {'  ·  '.join(parts)}")

    lid  = light.id
    rows = []

    # On/off
    label = "⏻  Turn OFF" if light.state.on else "⏻  Turn ON"
    rows.append([_btn(label, f"lt:{lid}")])

    # Brightness
    bri_row = []
    for step in _BRI_STEPS:
        active = abs(step - bri_pct) <= 11
        bri_row.append(_btn(f"{'●' if active else ''}{step}%", f"lb:{lid}:{step}"))
    rows.append(bri_row)

    # Color temperature
    if light.supports_color_temp:
        ct_row = []
        for emoji, mirek in _CT_PRESETS:
            active = light.state.color_temp and abs(mirek - light.state.color_temp) < 40
            ct_row.append(_btn(f"{'●' if active else ''}{emoji}", f"lct:{lid}:{mirek}"))
        rows.append(ct_row)

    # Color
    if light.supports_color:
        rows.append([_btn(e, f"lc:{lid}:{h}") for e, h in _COL_PRESETS])

    # Back — to room if we know it, else home
    back = f"room:{light.room_id}" if light.room_id else "home"
    back_label = f"← {light.room}" if light.room else "← Home"
    rows.append([_btn(back_label, back)])
    return text, InlineKeyboardMarkup(rows)


def _scenes_view(scenes: list[SceneInfo],
                 room: RoomInfo | None = None) -> tuple[str, InlineKeyboardMarkup]:
    if room:
        icon = _room_icon(room.archetype)
        text = f"🎬 Scenes\n{icon} {room.name}"
        back = f"room:{room.id}"
        back_label = f"← {room.name}"
        rows = [[_btn(s.name, f"scene_run:{s.id}")] for s in scenes]
    else:
        text = "🎬 Scenes"
        back, back_label = "home", "← Home"
        by_group: dict[str, list[SceneInfo]] = {}
        for s in scenes:
            by_group.setdefault(s.group_name, []).append(s)
        rows = []
        for group_name, group_scenes in sorted(by_group.items()):
            rows.append([_noop(group_name)])
            # 2 scene buttons per row
            chunk = group_scenes
            for i in range(0, len(chunk), 2):
                rows.append([_btn(s.name, f"scene_run:{s.id}")
                              for s in chunk[i:i + 2]])
    rows.append([_btn(back_label, back)])
    return text, InlineKeyboardMarkup(rows)


def _dances_view(running: str | None) -> tuple[str, InlineKeyboardMarkup]:
    status = f"▶ {running}" if running else "nothing playing"
    text   = f"✨ Dances\n{status}"
    rows   = []
    for name in DANCE_REGISTRY:
        icon  = _DANCE_ICON.get(name, "🎵")
        label = f"{'▶ ' if name == running else ''}{icon} {name.replace('_', ' ').title()}"
        rows.append([_btn(label, f"dance_run:{name}")])
    if running:
        rows.append([_btn("⏹ Stop", "dance_stop")])
    rows.append([_btn("← Home", "home")])
    return text, InlineKeyboardMarkup(rows)


def _ct_name(mirek: int) -> str:
    for _, preset, label in [(e, m, e) for e, m in _CT_PRESETS]:
        if abs(preset - mirek) < 40:
            return label
    return f"{mirek}K"


# ---------------------------------------------------------------------------
# Handler registration
# ---------------------------------------------------------------------------

def register_handlers(app, hub: ServiceHub) -> None:
    from telegram.ext import CommandHandler, MessageHandler, CallbackQueryHandler, filters
    h = _Handlers(hub)
    app.add_handler(CommandHandler("start",  h.cmd_home))
    app.add_handler(CommandHandler("help",   h.cmd_home))
    app.add_handler(CommandHandler("lights", h.cmd_home))
    app.add_handler(CallbackQueryHandler(h.callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, h.unknown))


# ---------------------------------------------------------------------------
# Handler class
# ---------------------------------------------------------------------------

class _Handlers:
    def __init__(self, hub: ServiceHub) -> None:
        self.hub = hub

    # ── Commands ─────────────────────────────────────────────────────────────

    @restricted
    async def cmd_home(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        rooms  = self.hub.rooms.list_rooms()
        lights = self.hub.lights.list_lights()
        text, kb = _home_view(rooms, sum(l.state.on for l in lights), len(lights))
        await update.message.reply_text(text, reply_markup=kb)

    @restricted
    async def unknown(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text("Send /lights to open the control panel.")

    # ── Callbacks ─────────────────────────────────────────────────────────────

    @restricted
    async def callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        data  = query.data or ""

        if data in ("noop", ""):
            return

        # ── Home ─────────────────────────────────────────────────────────────
        if data == "home":
            rooms  = self.hub.rooms.list_rooms()
            lights = self.hub.lights.list_lights()
            text, kb = _home_view(rooms, sum(l.state.on for l in lights), len(lights))
            await _edit(query, text, kb)
            return

        # ── All on / off ──────────────────────────────────────────────────────
        if data == "all_on":
            await self.hub.lights.set_all_on(True)
            await self._remember()
            return await self._refresh_home(query)
        if data == "all_off":
            await self.hub.lights.set_all_on(False)
            await self._remember()
            return await self._refresh_home(query)

        action, _, rest = data.partition(":")

        # ── Room panel ────────────────────────────────────────────────────────
        if action == "room":
            return await self._show_room(query, rest)

        if action == "room_on":
            await self.hub.rooms.set_on(rest, True)
            await self._remember()
            return await self._show_room(query, rest)

        if action == "room_off":
            await self.hub.rooms.set_on(rest, False)
            await self._remember()
            return await self._show_room(query, rest)

        if action == "room_scenes":
            room   = self.hub.rooms.get_room(rest)
            scenes = [s for s in self.hub.scenes.list_scenes() if s.group_id == rest]
            text, kb = _scenes_view(scenes, room)
            await _edit(query, text, kb)
            return

        # ── Global scenes ─────────────────────────────────────────────────────
        if data == "scenes":
            text, kb = _scenes_view(self.hub.scenes.list_scenes())
            await _edit(query, text, kb)
            return

        if action == "scene_run":
            result = await self.hub.scenes.activate(rest)
            await self._remember()
            await query.answer(result.message, show_alert=False)
            return

        # ── Dances ────────────────────────────────────────────────────────────
        if data == "dances":
            text, kb = _dances_view(self.hub.dances.running)
            await _edit(query, text, kb)
            return

        if action == "dance_run":
            if self.hub.dances.running:
                result = await self.hub.dances.stop()
                if not result.success:
                    # Started elsewhere (cron/CLI) — we have no handle to cancel it
                    await query.answer(result.message, show_alert=True)
                    text, kb = _dances_view(self.hub.dances.running)
                    await _edit(query, text, kb)
                    return
            light_ids = [l.id for l in self.hub.lights.danceable_lights()]
            await self.hub.dances.start(rest, light_ids)
            text, kb = _dances_view(self.hub.dances.running)
            await _edit(query, text, kb)
            return

        if data == "dance_stop":
            result = await self.hub.dances.stop()
            if not result.success:
                await query.answer(result.message, show_alert=True)
            text, kb = _dances_view(self.hub.dances.running)
            await _edit(query, text, kb)
            return

        # ── Light panel ───────────────────────────────────────────────────────
        if action == "lp":
            light = self.hub.lights.get_light(rest)
            if light:
                text, kb = _light_view(light)
                await _edit(query, text, kb)
            return

        if action == "lt":
            await self.hub.lights.toggle(rest)
            await self._remember()
            light = self.hub.lights.get_light(rest)
            if light:
                text, kb = _light_view(light)
                await _edit(query, text, kb)
            return

        if action == "lb":
            lid, _, bri_s = rest.partition(":")
            await self.hub.lights.set_brightness(lid, int(bri_s) / 100.0)
            await self._remember()
            light = self.hub.lights.get_light(lid)
            if light:
                text, kb = _light_view(light)
                await _edit(query, text, kb)
            return

        if action == "lct":
            lid, _, mirek_s = rest.partition(":")
            await self.hub.lights.set_color_temp(lid, int(mirek_s))
            await self._remember()
            light = self.hub.lights.get_light(lid)
            if light:
                text, kb = _light_view(light)
                await _edit(query, text, kb)
            return

        if action == "lc":
            lid, _, hue_s = rest.partition(":")
            await self.hub.lights.set_color(lid, float(hue_s))
            await self._remember()
            light = self.hub.lights.get_light(lid)
            if light:
                text, kb = _light_view(light)
                await _edit(query, text, kb)
            return

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _remember(self) -> None:
        """Record the lights as a clean, non-dance state (no-op mid-dance)."""
        await self.hub.dances.remember_state([l.id for l in self.hub.lights.list_lights()])

    async def _refresh_home(self, query) -> None:
        rooms  = self.hub.rooms.list_rooms()
        lights = self.hub.lights.list_lights()
        text, kb = _home_view(rooms, sum(l.state.on for l in lights), len(lights))
        await _edit(query, text, kb)

    async def _show_room(self, query, room_id: str) -> None:
        room   = self.hub.rooms.get_room(room_id)
        if not room:
            await query.edit_message_text("Room not found.")
            return
        lights = [l for l in self.hub.lights.list_lights() if l.room_id == room_id]
        scenes = [s for s in self.hub.scenes.list_scenes() if s.group_id == room_id]
        text, kb = _room_view(room, lights, scenes)
        await _edit(query, text, kb)
