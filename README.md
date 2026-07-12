# mmhue

A modular Philips Hue controller with pluggable interfaces. Drive your lights
from Telegram (inline keyboard, 3-level Home → Room → Light navigation), from a
web interface, or from a script — the interfaces are thin, and all the logic
lives in the services layer.

## Light dances

Beyond on/off and scenes, mmhue ships animated "dances" — long-running async
coroutines that drive the lights and restore their previous state when they end
or are cancelled.

| Dance | What it does |
| --- | --- |
| `chromatic_drift` | Each light drifts independently around the colour wheel, with random bursts |
| `police` | Lights split into two groups, alternating red and blue |
| `ambulance` | All lights alternate saturated red and cool white |
| `thunderstorm` | Dark indigo atmosphere, with lightning strikes flashing across random lights |
| `bandari` | Warm rhythmic shimmy on a beat, inspired by Iranian Bandari music |
| `birthday` | Candles, a wish, a blow-out, then a confetti party that escalates |

Adding one is a single coroutine plus a `REGISTRY` entry in
`mmhue/services/dances.py` — every interface picks it up automatically.

## Setup

Requires Python 3.12+ and [uv](https://github.com/astral-sh/uv).

```bash
uv sync
cp .env.example .env
```

Register an app key against your bridge (press the link button first):

```bash
uv run python scripts/register_bridge.py <bridge-ip>
```

Put the resulting host and key in `.env`, along with a Telegram bot token from
[@BotFather](https://t.me/BotFather) and the user IDs allowed to talk to it.

## Running

```bash
# Telegram bot
uv run python -m mmhue.interfaces.telegram

# One-off dance from the CLI: dance, seconds, then optional room names
uv run python scripts/dance.py birthday 90
uv run python scripts/dance.py thunderstorm 60 kitchen
```

With Docker:

```bash
docker compose up -d --build
```

## Tests

```bash
uv run pytest
```

Tests run against a mock bridge — no real lights required.
