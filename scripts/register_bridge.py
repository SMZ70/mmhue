"""
Run this once to register mmhue with your Hue bridge and get an app key.

Usage:
    python scripts/register_bridge.py <bridge-ip>

Press the physical button on the bridge before running.
"""

import asyncio
import sys
from aiohue.util import create_app_key


async def main(host: str) -> None:
    print(f"Registering with bridge at {host}…")
    print("Press the button on the bridge now, then press Enter.")
    input()
    try:
        key = await create_app_key(host, "mmhue")
        print(f"\nSuccess! Add to your .env:\n")
        print(f"HUE_BRIDGE_HOST={host}")
        print(f"HUE_BRIDGE_APP_KEY={key}")
    except Exception as e:
        print(f"Failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <bridge-ip>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
