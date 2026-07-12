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
# Dance 4 — Bandari  (Iranian Persian Gulf coastal dance)
# ---------------------------------------------------------------------------

async def bandari(
    bridge: HueBridgeV2,
    light_ids: list[str],
    *,
    duration: float = 60.0,
    bpm: float = 118.0,
) -> None:
    """Warm rhythmic dance inspired by Iranian Bandari music.

    Visual design:
      - Colour zones: warm reds/golds (home), hot pink, teal, deep purple —
        each light independently roams its zone then jumps to a new one
      - Hard rhythmic shimmy at bpm, staggered across lights (ripple effect)
      - Per-light random blackouts: a light snaps off for 0.5–1.5 beats
      - Full-room blackout every ~16 beats — brief dramatic drop, then explosion
      - Accent gold flash every 4 beats (the dohol downbeat)
      - Rotating call light; energy builds over first 65% of duration
    """
    # Colour zones (hue lo, hue hi) — Persian-inspired palette
    ZONES: list[tuple[float, float]] = [
        (5,   55),   # warm: deep red → orange → gold  (home zone, 3× weight)
        (5,   55),
        (5,   55),
        (315, 355),  # hot pink / magenta — Persian textiles
        (168, 192),  # teal / turquoise  — Persian tilework
        (265, 292),  # deep purple       — Persian carpets
    ]

    def _zone_hue(zone: int) -> float:
        lo, hi = ZONES[zone]
        return random.uniform(lo, hi)

    saved = await _capture_state(bridge, light_ids, "bandari")
    try:
        beat     = 60.0 / bpm   # ~0.508 s
        fps      = 6
        interval = 1.0 / fps    # ~167 ms
        snap_ms  = 95

        n = len(light_ids)
        configs = [
            {
                "lid":          lid,
                "hue":          _zone_hue(0),
                "hue_vel":      random.uniform(18, 32) * random.choice([-1, 1]),
                "shimmy_phase": (i / max(n - 1, 1)) * math.pi,  # 0 → π ripple spread
                "zone":         0,
                "next_zone_at": random.uniform(4, 10) * beat,
                "dark_until":   -1.0,   # seconds; -1 = not dark
            }
            for i, lid in enumerate(light_ids)
        ]

        dead: set[str] = set()

        # Prime lights
        for cfg in configs:
            x, y = _hue_to_xy(cfg["hue"])
            await _set(bridge, cfg["lid"], dead, on=True, color_xy=(x, y),
                       brightness=65, transition_time=1000)
        await asyncio.sleep(1.1)

        logger.info("bandari  {} lights  {:.0f}s  {:.0f}bpm", n, duration, bpm)

        elapsed          = 0.0
        prev_beat_num    = -1
        call_light_idx   = 0
        call_start_beat  = 0
        blackout_until   = -1.0

        while elapsed < duration:
            t0 = asyncio.get_event_loop().time()

            energy      = min(1.0, elapsed / (duration * 0.65))
            beat_num    = int(elapsed / beat)
            beat_phase  = (elapsed % beat) / beat * 2 * math.pi
            is_new_beat = beat_num != prev_beat_num

            if is_new_beat:
                prev_beat_num = beat_num

                if beat_num - call_start_beat >= 8:
                    call_light_idx = (call_light_idx + 1) % n
                    call_start_beat = beat_num

                for i, cfg in enumerate(configs):
                    if elapsed >= cfg["next_zone_at"]:
                        cfg["zone"]        = random.randrange(len(ZONES))
                        cfg["hue"]         = _zone_hue(cfg["zone"])
                        cfg["hue_vel"]     = random.uniform(18, 32) * random.choice([-1, 1])
                        cfg["next_zone_at"] = elapsed + random.uniform(3, 9) * beat
                        logger.debug("zone jump  light {}  →  zone {}", i, cfg["zone"])

                    if (i != call_light_idx
                            and elapsed >= cfg["dark_until"]
                            and random.random() < 0.20 * energy):
                        cfg["dark_until"] = elapsed + beat * random.uniform(0.5, 1.5)

                if beat_num > 0 and beat_num % 16 == 0 and energy > 0.35:
                    blackout_until = elapsed + beat * random.uniform(0.4, 0.8)
                    logger.debug("full blackout  beat {}", beat_num)

            # ── Full-room blackout ────────────────────────────────────────────
            if elapsed < blackout_until:
                for cfg in configs:
                    await _set(bridge, cfg["lid"], dead, brightness=3, transition_time=55)

            # ── Accent flash: every 4 beats ───────────────────────────────────
            elif is_new_beat and beat_num % 4 == 0:
                gold_xy = _hue_to_xy(38)
                for cfg in configs:
                    cfg["dark_until"] = -1.0
                    await _set(bridge, cfg["lid"], dead, color_xy=gold_xy,
                               brightness=int(88 + 12 * energy), transition_time=snap_ms)

            # ── Normal shimmy frame ───────────────────────────────────────────
            else:
                for i, cfg in enumerate(configs):
                    if cfg["lid"] in dead:
                        continue

                    if elapsed < cfg["dark_until"]:
                        await _set(bridge, cfg["lid"], dead, brightness=4, transition_time=snap_ms)
                        continue

                    lo, hi = ZONES[cfg["zone"]]
                    cfg["hue"] = (cfg["hue"] + cfg["hue_vel"] * interval) % 360
                    h = cfg["hue"]
                    if not (lo <= h <= hi):
                        cfg["hue_vel"] *= -1
                        cfg["hue"] = max(lo, min(hi, h))

                    shimmy_depth = 0.16 + 0.38 * energy
                    shimmy_val   = 0.5 + 0.5 * math.cos(beat_phase - cfg["shimmy_phase"])
                    base_bri     = 50 + 32 * energy
                    bri          = base_bri * (1.0 - shimmy_depth * (1.0 - shimmy_val))

                    if i == call_light_idx:
                        bri = min(100, bri + 15 * energy * shimmy_val)

                    x, y = _hue_to_xy(cfg["hue"])
                    await _set(bridge, cfg["lid"], dead, color_xy=(x, y),
                               brightness=max(5, bri), transition_time=snap_ms)

            elapsed += interval
            await asyncio.sleep(max(0.0, interval - (asyncio.get_event_loop().time() - t0)))

        logger.info("bandari finished")

    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        await _restore_state(bridge, saved)


# ---------------------------------------------------------------------------
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
