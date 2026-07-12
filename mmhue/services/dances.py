"""Named light dances.

Each dance is an async coroutine:  dance(bridge, light_ids, **kwargs) -> None

The REGISTRY dict maps name → coroutine so any interface can list and invoke
dances without knowing their internals.
"""

from __future__ import annotations

import asyncio
import math
import random
from colorsys import hsv_to_rgb
from dataclasses import dataclass
from typing import Awaitable, Callable

from aiohue.v2 import HueBridgeV2
from loguru import logger

from mmhue.services import dance_state


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _hue_to_xy(hue_deg: float) -> tuple[float, float]:
    """Full-saturation HSV hue → CIE xy (sRGB gamut)."""
    r, g, b = hsv_to_rgb(hue_deg / 360.0, 1.0, 1.0)
    X = r * 0.4124 + g * 0.3576 + b * 0.1805
    Y = r * 0.2126 + g * 0.7152 + b * 0.0722
    Z = r * 0.0193 + g * 0.1192 + b * 0.9505
    total = X + Y + Z or 1.0
    return X / total, Y / total


_WHITE_XY = (0.3127, 0.3290)  # CIE D65 white point

# A snapshot is a plain dict so it crosses any serialisation boundary easily.
type Snapshot = dict


# White-only lights reject colour commands. Capability never changes, so
# remember them across dances and just send them brightness instead of
# dropping them from the show entirely.
_MONO: set[str] = set()


async def _set(bridge: HueBridgeV2, lid: str, dead: set[str], **kwargs) -> None:
    """set_state wrapper that skips unreachable lights and de-colours white-only ones."""
    if lid in dead:
        return
    if lid in _MONO:
        kwargs.pop("color_xy", None)
        if not kwargs:
            return
    try:
        await bridge.lights.set_state(lid, **kwargs)
    except Exception as exc:
        if "color" in str(exc).lower() and "color_xy" in kwargs:
            _MONO.add(lid)
            kwargs.pop("color_xy")
            logger.info("light {} is white-only; sending brightness only", lid[:8])
            if not kwargs:
                return
            try:
                await bridge.lights.set_state(lid, **kwargs)
                return
            except Exception as retry_exc:
                exc = retry_exc
        logger.warning("light {} unreachable, skipping for this dance: {}", lid[:8], exc)
        dead.add(lid)


@dataclass
class RestorePlan:
    """What a dance should put the lights back to when it ends."""
    light_ids: list[str]
    snaps: list[Snapshot]   # empty => no trustworthy state; turn the lights off
    token: str


async def _read_lights(bridge: HueBridgeV2, light_ids: list[str]) -> list[Snapshot]:
    """Read the current state of each light."""
    snaps: list[Snapshot] = []
    for lid in light_ids:
        light = bridge.lights.get(lid)
        if light is None:
            snaps.append({"id": lid, "found": False})
            continue
        snap: Snapshot = {"id": lid, "found": True,
                          "on": light.is_on, "brightness": light.brightness}
        # Determine active colour mode: CT wins if its value is currently valid
        if light.color_temperature and light.color_temperature.mirek_valid:
            snap["color_temp"] = light.color_temperature.mirek
        elif light.color:
            snap["color_xy"] = (light.color.xy.x, light.color.xy.y)
        snaps.append(snap)
    return snaps


async def record_clean_state(bridge: HueBridgeV2, light_ids: list[str]) -> None:
    """Remember the current lights as a known-good, non-dance state.

    Call this after an ordinary light change (a scene, a toggle, a dim). It is
    what a dance later restores *to*, so a dance never has to guess.
    """
    if dance_state.running():
        return  # mid-dance: the lights are strobing, this is not a clean state
    snaps = await _read_lights(bridge, light_ids)
    dance_state.record_clean(snaps)


async def _capture_state(bridge: HueBridgeV2, light_ids: list[str],
                         name: str = "dance", source: str = "lib") -> RestorePlan:
    """Register the dance and work out what to restore to when it finishes.

    If another dance is already running, whatever the lights are doing right now
    is mid-strobe — snapshotting it would mean "restoring" the room to a random
    confetti colour later. In that case fall back to the newest known non-dance
    state, and if we have none, to darkness.
    """
    if dance_state.others_running():
        target = dance_state.last_clean(light_ids)
        if target:
            logger.info("another dance is running; will restore to last clean state")
        else:
            logger.warning("another dance is running and no clean state known; "
                           "will restore to lights-off")
        token = dance_state.begin(name, source)
        return RestorePlan(light_ids, target or [], token)

    snaps = await _read_lights(bridge, light_ids)
    dance_state.record_clean(snaps)
    logger.debug("captured clean state for {} lights", len(snaps))
    token = dance_state.begin(name, source)
    return RestorePlan(light_ids, snaps, token)


async def _restore_state(bridge: HueBridgeV2, plan: RestorePlan,
                         transition_ms: int = 1500) -> None:
    """Put the lights back. Skips unreachable lights.

    With no trustworthy state to return to, turn the lights off: darkness is a
    sane, intentional-looking end state, whereas leaving them wherever the
    strobe happened to stop is not.
    """
    dead: set[str] = set()
    try:
        if not plan.snaps:
            logger.warning("no clean state to restore; turning {} lights off",
                           len(plan.light_ids))
            for lid in plan.light_ids:
                await _set(bridge, lid, dead, on=False, transition_time=transition_ms)
            return

        count = sum(1 for s in plan.snaps if s.get("found"))
        logger.info("restoring {} lights to pre-dance state…", count)
        for snap in plan.snaps:
            if not snap.get("found"):
                continue
            lid = snap["id"]
            if not snap["on"]:
                await _set(bridge, lid, dead, on=False, transition_time=transition_ms)
                continue
            kwargs: dict = {"on": True, "brightness": snap["brightness"],
                            "transition_time": transition_ms}
            if "color_temp" in snap:
                kwargs["color_temp"] = snap["color_temp"]
            elif "color_xy" in snap:
                kwargs["color_xy"] = snap["color_xy"]
            await _set(bridge, lid, dead, **kwargs)
        logger.info("state restored")
    finally:
        dance_state.end(plan.token)


# ---------------------------------------------------------------------------
# Dance 1 — Chromatic Drift
# ---------------------------------------------------------------------------

@dataclass
class _DriftCtx:
    hue: float
    hue_vel: float
    bri_phase: float
    bri_freq: float
    burst_in: float


def _rand_drift() -> _DriftCtx:
    return _DriftCtx(
        hue=random.uniform(0, 360),
        hue_vel=random.uniform(20, 55) * random.choice([-1, 1]),
        bri_phase=random.uniform(0, 2 * math.pi),
        bri_freq=random.uniform(0.08, 0.22),
        burst_in=random.uniform(6, 18),
    )


async def chromatic_drift(
    bridge: HueBridgeV2,
    light_ids: list[str],
    *,
    duration: float = 60.0,
    fps: int = 3,
    min_bri: float = 0.28,
    max_bri: float = 0.92,
) -> None:
    """Each light independently drifts through the colour wheel.

    Randomness: starting hue, drift speed/direction, brightness pulse rate
    and phase, plus periodic bursts that jump to a new random hue.
    """
    saved = await _capture_state(bridge, light_ids, "chromatic_drift")
    try:
        interval = 1.0 / fps
        transition_ms = int(interval * 850)
        state = {lid: _rand_drift() for lid in light_ids}
        dead: set[str] = set()

        for lid, ctx in state.items():
            x, y = _hue_to_xy(ctx.hue)
            await _set(bridge, lid, dead, on=True, color_xy=(x, y),
                       brightness=70, transition_time=800)
        await asyncio.sleep(0.9)

        logger.info("chromatic_drift  {} lights  {:.0f}s", len(light_ids), duration)
        elapsed = 0.0

        while elapsed < duration:
            t0 = asyncio.get_event_loop().time()
            for lid, ctx in state.items():
                if lid in dead:
                    continue
                ctx.hue = (ctx.hue + ctx.hue_vel * interval) % 360
                ctx.burst_in -= interval
                if ctx.burst_in <= 0:
                    ctx.hue = random.uniform(0, 360)
                    ctx.hue_vel = random.uniform(20, 55) * random.choice([-1, 1])
                    ctx.burst_in = random.uniform(6, 20)
                    logger.debug("burst → {} hue={:.0f}° vel={:+.0f}°/s",
                                 lid[:8], ctx.hue, ctx.hue_vel)
                bri_norm = 0.5 + 0.5 * math.sin(
                    2 * math.pi * ctx.bri_freq * elapsed + ctx.bri_phase)
                bri = (min_bri + (max_bri - min_bri) * bri_norm) * 100
                x, y = _hue_to_xy(ctx.hue)
                await _set(bridge, lid, dead, color_xy=(x, y),
                           brightness=bri, transition_time=transition_ms)
            elapsed += interval
            await asyncio.sleep(max(0.0, interval - (asyncio.get_event_loop().time() - t0)))

        logger.info("chromatic_drift finished")

    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        await _restore_state(bridge, saved)


# ---------------------------------------------------------------------------
# Dance 2 — Emergency Flash  (police / ambulance)
# ---------------------------------------------------------------------------

async def emergency_flash(
    bridge: HueBridgeV2,
    light_ids: list[str],
    *,
    mode: str = "police",
    duration: float = 30.0,
    flash_hz: float = 3.0,
) -> None:
    """Hard alternating flashes.

    police    — round-robin split into two groups, red vs blue, alternating.
    ambulance — all lights alternate saturated red and cool white.
    """
    saved = await _capture_state(bridge, light_ids, mode)
    try:
        half = 1.0 / (flash_hz * 2)
        snap = max(30, int(half * 180))
        dead: set[str] = set()

        for lid in light_ids:
            await _set(bridge, lid, dead, on=True, brightness=100, transition_time=100)
        await asyncio.sleep(0.15)

        elapsed = 0.0
        phase = 0

        if mode == "police":
            group_a = light_ids[0::2]
            group_b = light_ids[1::2] or light_ids
            red, blue = _hue_to_xy(0), _hue_to_xy(240)
            logger.info("emergency_flash  police  a={} b={}  {:.0f}s",
                        len(group_a), len(group_b), duration)
            while elapsed < duration:
                t0 = asyncio.get_event_loop().time()
                a_xy, a_bri = (red, 100) if phase % 2 == 0 else (blue, 85)
                b_xy, b_bri = (blue, 85) if phase % 2 == 0 else (red, 100)
                for lid in group_a:
                    await _set(bridge, lid, dead, color_xy=a_xy, brightness=a_bri, transition_time=snap)
                for lid in group_b:
                    await _set(bridge, lid, dead, color_xy=b_xy, brightness=b_bri, transition_time=snap)
                phase += 1
                elapsed += half
                await asyncio.sleep(max(0.0, half - (asyncio.get_event_loop().time() - t0)))

        elif mode == "ambulance":
            red, white = _hue_to_xy(0), _WHITE_XY
            logger.info("emergency_flash  ambulance  {} lights  {:.0f}s",
                        len(light_ids), duration)
            while elapsed < duration:
                t0 = asyncio.get_event_loop().time()
                xy  = red if phase % 2 == 0 else white
                bri = 100 if phase % 2 == 0 else 55
                for lid in light_ids:
                    await _set(bridge, lid, dead, color_xy=xy, brightness=bri, transition_time=snap)
                phase += 1
                elapsed += half
                await asyncio.sleep(max(0.0, half - (asyncio.get_event_loop().time() - t0)))

        logger.info("emergency_flash finished")

    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        await _restore_state(bridge, saved)


# ---------------------------------------------------------------------------
# Dance 3 — Thunderstorm
# ---------------------------------------------------------------------------

@dataclass
class _StormCtx:
    hue: float
    hue_vel: float
    bri: float
    bri_phase: float
    bri_freq: float
    struck: bool


async def thunderstorm(
    bridge: HueBridgeV2,
    light_ids: list[str],
    *,
    duration: float = 60.0,
    fps: int = 2,
) -> None:
    """Dark indigo atmosphere with slow drift and random lightning strikes.

    Lightning events run as separate asyncio tasks so the fast flash sequence
    (bright white → flicker → fade back) doesn't block the drift loop.
    """
    saved = await _capture_state(bridge, light_ids, "thunderstorm")
    pending: set[asyncio.Task] = set()

    try:
        interval = 1.0 / fps
        transition_ms = int(interval * 800)

        state: dict[str, _StormCtx] = {
            lid: _StormCtx(
                hue=random.uniform(210, 280),
                hue_vel=random.uniform(2, 7) * random.choice([-1, 1]),
                bri=random.uniform(18, 32),
                bri_phase=random.uniform(0, 2 * math.pi),
                bri_freq=random.uniform(0.04, 0.12),
                struck=False,
            )
            for lid in light_ids
        }
        dead: set[str] = set()

        for lid, ctx in state.items():
            x, y = _hue_to_xy(ctx.hue)
            await _set(bridge, lid, dead, on=True, color_xy=(x, y),
                       brightness=int(ctx.bri), transition_time=1200)
        await asyncio.sleep(1.4)

        logger.info("thunderstorm  {} lights  {:.0f}s", len(light_ids), duration)

        async def lightning_strike(targets: list[str]) -> None:
            alive = [lid for lid in targets if lid not in dead]
            for lid in alive:
                state[lid].struck = True
            try:
                for lid in alive:
                    await _set(bridge, lid, dead, color_xy=_WHITE_XY, brightness=100, transition_time=25)
                await asyncio.sleep(0.07)
                for lid in alive:
                    await _set(bridge, lid, dead, brightness=30, transition_time=40)
                await asyncio.sleep(0.05)
                for lid in alive:
                    await _set(bridge, lid, dead, brightness=90, transition_time=30)
                await asyncio.sleep(0.09)
                for lid in alive:
                    ctx = state[lid]
                    x, y = _hue_to_xy(ctx.hue)
                    await _set(bridge, lid, dead, color_xy=(x, y),
                               brightness=int(ctx.bri), transition_time=700)
                await asyncio.sleep(0.75)
            except asyncio.CancelledError:
                pass
            finally:
                for lid in alive:
                    state[lid].struck = False

        elapsed = 0.0
        next_strike_at = random.uniform(2.0, 6.0)

        while elapsed < duration:
            t0 = asyncio.get_event_loop().time()

            for lid, ctx in state.items():
                if ctx.struck or lid in dead:
                    continue
                ctx.hue = (ctx.hue + ctx.hue_vel * interval) % 360
                if ctx.hue < 200 or ctx.hue > 290:
                    ctx.hue_vel *= -1
                    ctx.hue = max(200.0, min(290.0, ctx.hue))
                ctx.bri = 15 + 17 * (0.5 + 0.5 * math.sin(
                    2 * math.pi * ctx.bri_freq * elapsed + ctx.bri_phase))
                x, y = _hue_to_xy(ctx.hue)
                await _set(bridge, lid, dead, color_xy=(x, y),
                           brightness=int(ctx.bri), transition_time=transition_ms)

            if elapsed >= next_strike_at:
                free = [lid for lid in light_ids if not state[lid].struck]
                if free:
                    n = random.randint(1, min(3, len(free)))
                    targets = random.sample(free, n)
                    task = asyncio.create_task(lightning_strike(targets))
                    pending.add(task)
                    task.add_done_callback(pending.discard)
                    logger.debug("lightning → {} lights", n)
                next_strike_at = elapsed + random.uniform(3.0, 10.0)

            elapsed += interval
            await asyncio.sleep(max(0.0, interval - (asyncio.get_event_loop().time() - t0)))

        logger.info("thunderstorm finished")

    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        # Cancel any in-flight lightning tasks before restoring lights
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await _restore_state(bridge, saved)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Dance 4 — Bandari  (Iranian Persian Gulf coastal dance)
# ---------------------------------------------------------------------------

_BANDARI_ZONES: list[tuple[float, float]] = [
    (8, 40),      # deep red -> orange -> gold  (home)
    (318, 350),   # hot pink / magenta
    (168, 195),   # teal / turquoise
    (265, 295),   # deep purple
    (95, 130),    # lime
]

_DOHOL_GOLD = 38.0

# Measured off a real bandari track (Sasy, "Bandari"): a hit every ~163 ms,
# i.e. a 6/8 bar of ~0.98 s.
_BANDARI_PULSE = 0.163
_BANDARI_BED = 22.0       # the room glows between strikes; it never goes black


async def _gset(bridge: HueBridgeV2, gid: str, dead: set[str], **kwargs) -> None:
    """Drive a whole ROOM with one command.

    This is the difference between a light show and a disco. Striking six lights
    individually costs six commands, and releasing them six more — the entire
    budget the bridge will accept in a second. One grouped_light command hits
    every light in the room at once, so the room can slam in unison on the beat,
    which is the thing that actually makes people move.
    """
    if gid in dead:
        return
    try:
        await bridge.groups.grouped_light.set_state(gid, **kwargs)
    except Exception as exc:
        logger.warning("room {} unreachable: {}", gid[:8], exc)
        dead.add(gid)


def _dance_rooms(bridge: HueBridgeV2, light_ids: list[str]) -> list[str]:
    """grouped_light ids for the rooms we are allowed to dance in."""
    allowed = set(light_ids)
    rooms: list[str] = []
    for gl in bridge.groups.grouped_light:
        members = {light.id for light in bridge.groups.grouped_light.get_lights(gl.id)}
        if members and members <= allowed:
            rooms.append(gl.id)
    return rooms


async def bandari(
    bridge: HueBridgeV2,
    light_ids: list[str],
    *,
    duration: float = 60.0,
    pulse: float = _BANDARI_PULSE,
) -> None:
    """A 6/8 bandari built out of sections, so it goes somewhere.

    Two things were wrong before. It looped a single bar for a minute, which is
    monotonous however correct the rhythm is; and every strike hit one lonely
    light, because striking the whole room individually costs more commands than
    the bridge will take. Rooms are driven as groups now — one command each — so
    the house can slam together on the dohol.

    Sections are shuffled and played 3-5 bars at a time, the way the birthday
    dance moves between acts:

      dohol   — the whole house slams gold in unison on 1 and 4. The hook.
      chase   — rooms fire one after another: a wave running around the flat.
      shimmy  — individual lights, quick teks scattered over the beat
      call    — one room calls, the others answer

    Every 6/8 bar is ~0.98 s, so a section is a few seconds — long enough to
    lock into, short enough that it never gets boring.
    """
    if not light_ids:
        return

    saved = await _capture_state(bridge, light_ids, "bandari")
    try:
        dead: set[str] = set()
        rooms = _dance_rooms(bridge, light_ids)
        n = len(light_ids)

        # Fall back to per-light strikes if the bridge exposes no usable rooms
        use_rooms = len(rooms) >= 2

        for lid in light_ids:
            await _set(bridge, lid, dead, on=True, brightness=_BANDARI_BED,
                       color_xy=_hue_to_xy(random.uniform(*_BANDARI_ZONES[0])),
                       transition_time=400)
        await asyncio.sleep(0.6)

        logger.info("bandari  {} lights in {} rooms  {:.0f}s  6/8 @ {:.0f}ms",
                    n, len(rooms), duration, pulse * 1000)

        order = light_ids[:]
        random.shuffle(order)
        idx = 0
        zone = 0

        started = asyncio.get_event_loop().time()
        elapsed = 0.0

        def over() -> bool:
            """Sections run whole bars, so check the clock between them; without
            this a 30s dance could keep going for another five."""
            return asyncio.get_event_loop().time() - started >= duration

        def gold() -> tuple[float, float]:
            return _hue_to_xy(_DOHOL_GOLD + random.uniform(-7, 7))

        def colour() -> tuple[float, float]:
            return _hue_to_xy(random.uniform(*_BANDARI_ZONES[zone]))

        async def beat(ops, swing: float = 1.0) -> None:
            """Run one pulse worth of commands, then wait out the pulse."""
            t0 = asyncio.get_event_loop().time()
            for fn in ops:
                await fn()
            wait = pulse * swing
            await asyncio.sleep(max(0.0, wait - (asyncio.get_event_loop().time() - t0)))

        # ── Sections ─────────────────────────────────────────────────────────

        async def dohol(bars: int) -> None:
            """The hook: the whole house hits together on 1 and 4."""
            for _ in range(bars):
                if over():
                    return
                g = gold()
                await beat([lambda r=r, c=g: _gset(bridge, r, dead, color_xy=c, brightness=100,
                                                    transition_time=50) for r in rooms])
                await beat([lambda r=r: _gset(bridge, r, dead, brightness=_BANDARI_BED,
                                              transition_time=90) for r in rooms], 0.94)
                await beat([])                                        # 3 — space
                half = rooms[: max(1, len(rooms) - 1)]
                g2 = gold()
                await beat([lambda r=r, c=g2: _gset(bridge, r, dead, color_xy=c, brightness=90,
                                                     transition_time=50) for r in half])
                await beat([lambda r=r: _gset(bridge, r, dead, brightness=_BANDARI_BED,
                                              transition_time=90) for r in half], 0.94)
                await beat([])                                        # 6 — space

        async def chase(bars: int) -> None:
            """A wave running around the flat, room to room."""
            i = 0
            for _ in range(bars):
                if over():
                    return
                for hit in (True, False, True, True, False, True):
                    if not hit:
                        await beat([], 0.94)
                        continue
                    cur, prev = rooms[i % len(rooms)], rooms[(i - 1) % len(rooms)]
                    c = colour()
                    await beat([
                        lambda r=cur, x=c: _gset(bridge, r, dead, color_xy=x, brightness=95,
                                                 transition_time=50),
                        lambda r=prev: _gset(bridge, r, dead, brightness=_BANDARI_BED,
                                             transition_time=110),
                    ], 1.04)
                    i += 1

        async def shimmy(bars: int) -> None:
            """Individual lights: quick teks scattered across the beat."""
            nonlocal idx
            lit: list[str] = []
            for _ in range(bars):
                if over():
                    return
                for count, bri in ((2, 100.0), (0, 0.0), (1, 78.0),
                                   (1, 95.0), (0, 0.0), (1, 88.0)):
                    ops = [lambda x=x: _set(bridge, x, dead, brightness=_BANDARI_BED,
                                            transition_time=110) for x in lit]
                    lit = []
                    for _k in range(count):
                        lid = order[idx % n]
                        idx += 1
                        xy = gold() if bri >= 95 else colour()
                        ops.append(lambda x=lid, c=xy, b=bri:
                                   _set(bridge, x, dead, color_xy=c, brightness=b,
                                        transition_time=50))
                        lit.append(lid)
                    await beat(ops, 1.0 if count else 0.94)

        async def call(bars: int) -> None:
            """One room calls; the others answer."""
            for b in range(bars):
                if over():
                    return
                lead = rooms[b % len(rooms)]
                rest = [r for r in rooms if r != lead]
                g = gold()
                await beat([lambda r=lead, c=g: _gset(bridge, r, dead, color_xy=c,
                                                      brightness=100, transition_time=50)])
                await beat([lambda r=lead: _gset(bridge, r, dead, brightness=_BANDARI_BED,
                                                 transition_time=90)], 0.94)
                c = colour()
                await beat([lambda r=r, x=c: _gset(bridge, r, dead, color_xy=x, brightness=92,
                                                   transition_time=50) for r in rest], 1.06)
                await beat([lambda r=r: _gset(bridge, r, dead, brightness=_BANDARI_BED,
                                              transition_time=90) for r in rest])
                await beat([], 0.94)
                c2 = colour()
                await beat([lambda r=r, x=c2: _gset(bridge, r, dead, color_xy=x, brightness=96,
                                                    transition_time=50) for r in rooms], 1.06)

        sections = [dohol, chase, call, shimmy] if use_rooms else [shimmy]
        bag: list = []

        while elapsed < duration:
            if not bag:
                bag = sections.copy()
                random.shuffle(bag)
            section = bag.pop()
            zone = random.randrange(len(_BANDARI_ZONES))
            bars = random.randint(3, 5)
            logger.debug("bandari → {} for {} bars", section.__name__, bars)
            await section(bars)
            elapsed = asyncio.get_event_loop().time() - started

        logger.info("bandari finished")

    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        await _restore_state(bridge, saved)


# Dance 5 — Birthday  (candles → wish → blow-out → confetti party)
# ---------------------------------------------------------------------------

# Saturated party palette — balloons, confetti, streamers
_PARTY_HUES: tuple[float, ...] = (
    0, 15, 30, 45, 60,        # red → orange → gold
    90, 110, 140,             # lime → green → jade
    170, 190, 210,            # turquoise → cyan → sky
    230, 250, 270,            # blue → indigo → violet
    290, 310, 330, 345,       # purple → magenta → hot pink → rose
)

_CANDLE_HUE = (18.0, 45.0)  # deep amber → gold


async def birthday(
    bridge: HueBridgeV2,
    light_ids: list[str],
    *,
    duration: float = 90.0,
) -> None:
    """Straight into a party that never repeats itself.

    Opening (~5s, plays once):
      fuse — candles flare, gutter twice, snap dark
      pop  — confetti cannon: every light bursts a random party colour

    Party (shuffled, drawn without replacement, 4–8 s each until time runs out):
      confetti     — rapid random-colour pops on random lights, 8 fps
      rainbow_spin — the hue wheel chasing around the room
      sparklers    — white-hot twinkles over a deep coloured bed
      cheer        — full-room strobe alternating two party colours

    Energy opens at 0.75 and tops out over the first quarter, so it is loud from
    the first second: bright, fast, deep strobes, many lights popping per frame.
    Colours are drawn from an 18-hue palette spanning the whole wheel.
    """
    if not light_ids:
        logger.warning("birthday: no lights to dance with")
        return

    saved = await _capture_state(bridge, light_ids, "birthday")
    dead: set[str] = set()
    n = len(light_ids)
    started = asyncio.get_event_loop().time()

    def _elapsed() -> float:
        return asyncio.get_event_loop().time() - started

    def _energy() -> float:
        """Opens at 0.75 and tops out fast — this party has no warm-up."""
        return min(1.0, 0.75 + 0.25 * (_elapsed() / max(duration * 0.25, 1.0)))

    def _party_xy() -> tuple[float, float]:
        return _hue_to_xy(random.choice(_PARTY_HUES) + random.uniform(-12, 12))

    async def _pace(t0: float, interval: float) -> None:
        await asyncio.sleep(max(0.0, interval - (asyncio.get_event_loop().time() - t0)))

    # ── Opening ──────────────────────────────────────────────────────────────

    async def fuse() -> None:
        """A ~4s fuse, not a slow burn: candles flare, snap out, room detonates.

        The old opening ran a 5 fps flicker loop over every light, which meant
        ~35 bridge commands a second against a bridge that takes about 10 — so
        it crawled for half a minute before the party started. This spends a
        fixed, small number of commands instead.
        """
        for lid in light_ids:
            await _set(bridge, lid, dead, on=True, brightness=30, transition_time=300,
                       color_xy=_hue_to_xy(random.uniform(*_CANDLE_HUE)))
        await asyncio.sleep(0.45)

        for _ in range(2):  # two quick guttering flickers
            for lid in light_ids:
                await _set(bridge, lid, dead, brightness=random.uniform(14.0, 36.0),
                           transition_time=120,
                           color_xy=_hue_to_xy(random.uniform(*_CANDLE_HUE)))
            await asyncio.sleep(0.25)

        for lid in light_ids:  # blown out
            await _set(bridge, lid, dead, brightness=1, transition_time=170)
        await asyncio.sleep(0.7)

    async def pop() -> None:
        """Confetti cannon — the whole room detonates at once."""
        logger.debug("birthday: 🎉 pop")
        for lid in light_ids:
            await _set(bridge, lid, dead, on=True, color_xy=_party_xy(),
                       brightness=100, transition_time=60)
        await asyncio.sleep(0.5)
        for _ in range(2):  # double-flash the cannon
            for lid in light_ids:
                await _set(bridge, lid, dead, brightness=25, transition_time=70)
            await asyncio.sleep(0.12)
            for lid in light_ids:
                await _set(bridge, lid, dead, color_xy=_party_xy(), brightness=100,
                           transition_time=70)
            await asyncio.sleep(0.16)

    # ── Party acts ───────────────────────────────────────────────────────────

    async def confetti(secs: float) -> None:
        """Only a handful of lights change each frame — snappy, never in sync."""
        interval = 1 / 8
        t = 0.0
        while t < secs:
            t0 = asyncio.get_event_loop().time()
            hits = min(n, 1 + int(round(random.uniform(0, 1.6 + 1.4 * _energy()))))
            for lid in random.sample(light_ids, hits):
                await _set(bridge, lid, dead, color_xy=_party_xy(),
                           brightness=random.uniform(55, 100), transition_time=70)
            t += interval
            await _pace(t0, interval)

    async def rainbow_spin(secs: float) -> None:
        """A hue wheel chasing around the room, faster as the party heats up."""
        interval = 1 / 6
        spread = 360.0 / n
        base = random.uniform(0, 360)
        t = 0.0
        while t < secs:
            t0 = asyncio.get_event_loop().time()
            base = (base + (110 + 150 * _energy()) * interval) % 360
            for i, lid in enumerate(light_ids):
                await _set(bridge, lid, dead, color_xy=_hue_to_xy((base + i * spread) % 360),
                           brightness=70 + 25 * _energy(), transition_time=int(interval * 850))
            t += interval
            await _pace(t0, interval)

    async def sparklers(secs: float) -> None:
        """Deep coloured bed, white-hot twinkles skittering across it."""
        bed = _hue_to_xy(random.choice([275, 300, 225]))
        for lid in light_ids:
            await _set(bridge, lid, dead, color_xy=bed, brightness=30, transition_time=500)
        await asyncio.sleep(0.6)

        interval = 1 / 8
        t = 0.0
        while t < secs:
            t0 = asyncio.get_event_loop().time()
            for lid in random.sample(light_ids, min(n, random.randint(1, 2))):
                await _set(bridge, lid, dead, color_xy=_WHITE_XY, brightness=100,
                           transition_time=40)
            for lid in random.sample(light_ids, min(n, random.randint(1, 2))):
                await _set(bridge, lid, dead, color_xy=bed, brightness=random.uniform(20, 38),
                           transition_time=200)
            t += interval
            await _pace(t0, interval)

    async def cheer(secs: float) -> None:
        """Full-room strobe — two colours, hard alternation, everyone together."""
        hz = 4.0 + 2.0 * _energy()
        half = 1.0 / (hz * 2)
        a, b = _party_xy(), _party_xy()
        t = 0.0
        phase = 0
        while t < secs:
            t0 = asyncio.get_event_loop().time()
            xy = a if phase % 2 == 0 else b
            bri = 100 if phase % 2 == 0 else 45 + 25 * _energy()
            for lid in light_ids:
                await _set(bridge, lid, dead, color_xy=xy, brightness=bri,
                           transition_time=max(30, int(half * 160)))
            phase += 1
            t += half
            await _pace(t0, half)

    # ── Run the show ─────────────────────────────────────────────────────────

    try:
        logger.info("birthday  {} lights  {:.0f}s", n, duration)

        await fuse()
        await pop()

        party = [confetti, rainbow_spin, sparklers, cheer]
        bag: list[Callable[[float], Awaitable[None]]] = []

        while True:
            remaining = duration - _elapsed()
            if remaining <= 0.5:
                break
            if not bag:
                bag = party.copy()
                random.shuffle(bag)
            act = bag.pop()
            # Short blocks: the room should keep changing character
            secs = min(remaining, random.uniform(4.0, 8.0))
            logger.debug("birthday act → {}  {:.0f}s  energy {:.2f}",
                         act.__name__, secs, _energy())
            await act(secs)

        logger.info("birthday finished")

    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        await _restore_state(bridge, saved)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

DanceFn = Callable[..., Awaitable[None]]

REGISTRY: dict[str, DanceFn] = {
    "chromatic_drift": chromatic_drift,
    "police":          lambda b, ids, **kw: emergency_flash(b, ids, mode="police",    **kw),
    "ambulance":       lambda b, ids, **kw: emergency_flash(b, ids, mode="ambulance", **kw),
    "thunderstorm":    thunderstorm,
    "bandari":         bandari,
    "birthday":        birthday,
}
