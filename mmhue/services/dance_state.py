"""Shared, cross-process dance state.

Dances can be started from more than one place — the Telegram bot, the CLI, a
cron job — and those run as *separate processes*. Without somewhere shared to
look, none of them knows about the others, which causes two real bugs:

  1. The bot cannot show a dance it did not start itself.
  2. A dance starting while another is already running snapshots the lights
     mid-strobe, and "restoring" afterwards leaves the room in a random
     confetti colour.

So we keep a small state directory (MMHUE_STATE_DIR, bind-mounted from the
host) holding the set of running dances, plus a rolling history of light states
we believe are *not* mid-dance. Restore aims for the newest of those; if we
have none, we turn the lights off rather than freeze them somewhere random.

Writes are atomic (tmp + rename) and guarded by a lock file, since several
processes can touch this at once.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from loguru import logger

STATE_DIR = Path(os.getenv("MMHUE_STATE_DIR", "/app/state"))
DANCE_FILE = STATE_DIR / "dance.json"
HISTORY_FILE = STATE_DIR / "history.json"
LOCK_FILE = STATE_DIR / ".lock"

MAX_HISTORY = 50

# A dance is presumed dead if it has not heartbeated for this long.
STALE_SECONDS = 15.0


def _ensure_dir() -> bool:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        return True
    except OSError as exc:
        logger.warning("dance state dir unavailable ({}): {}", STATE_DIR, exc)
        return False


@contextmanager
def _locked() -> Iterator[None]:
    """Cross-process lock. Never fatal: a dance must run even if state is broken."""
    if not _ensure_dir():
        yield
        return
    import fcntl

    fh = None
    try:
        fh = open(LOCK_FILE, "w")  # noqa: SIM115 - held across the yield by design
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    except OSError as exc:
        logger.warning("dance state lock failed: {}", exc)
        yield
    finally:
        if fh is not None:
            try:
                fcntl.flock(fh, fcntl.LOCK_UN)
            finally:
                fh.close()


def _read(path: Path, default: Any) -> Any:
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _write(path: Path, data: Any) -> None:
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)   # atomic
    except OSError as exc:
        logger.warning("could not write {}: {}", path.name, exc)


# ---------------------------------------------------------------------------
# Running dances
# ---------------------------------------------------------------------------

def _prune(entries: list[dict]) -> list[dict]:
    """Drop entries that stopped heartbeating (a crash or SIGKILL leaves them behind).

    Liveness is a heartbeat, not os.kill(pid, 0), because pids are namespaced:
    the bot and the web UI run in different containers, so the web cannot see
    the bot's pids and would wrongly declare a live dance dead — and prune it.
    """
    now = time.time()
    alive = []
    for e in entries:
        seen = e.get("last_seen", e.get("started_at", 0))
        if not isinstance(seen, (int, float)):
            continue
        if now - seen > STALE_SECONDS:
            logger.debug("clearing stale dance entry {} (silent {:.0f}s)",
                         e.get("name"), now - seen)
            continue
        alive.append(e)
    return alive


def heartbeat(token: str) -> None:
    """Say a dance is still alive. Runners call this on a timer."""
    with _locked():
        entries = _read(DANCE_FILE, [])
        found = False
        for e in entries:
            if e.get("token") == token:
                e["last_seen"] = time.time()
                found = True
        if found:
            _write(DANCE_FILE, _prune(entries))


def running() -> str | None:
    """Name of a currently running dance, from any process. None if idle."""
    with _locked():
        entries = _prune(_read(DANCE_FILE, []))
        _write(DANCE_FILE, entries)
    return entries[0]["name"] if entries else None


def running_all() -> list[dict]:
    with _locked():
        entries = _prune(_read(DANCE_FILE, []))
        _write(DANCE_FILE, entries)
    return entries


def begin(name: str, source: str = "unknown") -> str:
    """Register a dance as running. Returns a token to pass back to end()."""
    token = uuid.uuid4().hex
    with _locked():
        entries = _prune(_read(DANCE_FILE, []))
        now = time.time()
        entries.append({
            "token": token,
            "name": name,
            "source": source,
            "pid": os.getpid(),      # informational only; pids are namespaced
            "started_at": now,
            "last_seen": now,
        })
        _write(DANCE_FILE, entries)
    logger.info("dance '{}' registered ({}), token {}", name, source, token[:8])
    return token


def end(token: str) -> None:
    with _locked():
        entries = [e for e in _prune(_read(DANCE_FILE, [])) if e.get("token") != token]
        _write(DANCE_FILE, entries)


def others_running(token: str | None = None) -> bool:
    """Is some *other* dance already running? Our own token does not count."""
    return any(e.get("token") != token for e in running_all())


# ---------------------------------------------------------------------------
# Stop requests
# ---------------------------------------------------------------------------
#
# Any interface must be able to stop any dance, whoever started it. Signalling
# the pid only works inside a single container, so instead a stop request is
# written here and every dance runner watches for it. That holds however the
# processes are arranged.

STOP_FILE = STATE_DIR / "stop.json"


def request_stop() -> None:
    """Ask every running dance to stop. Runners see this and cancel cleanly."""
    with _locked():
        _write(STOP_FILE, {"at": time.time()})
    logger.info("stop requested for all dances")


def stop_requested_since(started_at: float) -> bool:
    """Has a stop been requested since this dance began?"""
    data = _read(STOP_FILE, None)
    if not isinstance(data, dict):
        return False
    at = data.get("at")
    return isinstance(at, (int, float)) and at >= started_at


def clear_stop() -> None:
    with _locked():
        _write(STOP_FILE, {"at": 0})


# ---------------------------------------------------------------------------
# Non-dance light-state history
# ---------------------------------------------------------------------------

def record_clean(snaps: list[dict]) -> None:
    """Remember a light state we believe is NOT mid-dance."""
    if not snaps:
        return
    with _locked():
        history = _read(HISTORY_FILE, [])
        history.append({"at": time.time(), "snaps": snaps})
        _write(HISTORY_FILE, history[-MAX_HISTORY:])


def last_clean(light_ids: list[str]) -> list[dict] | None:
    """Newest non-dance state covering these lights, or None if we have none."""
    history = _read(HISTORY_FILE, [])
    wanted = set(light_ids)
    for entry in reversed(history):
        snaps = entry.get("snaps") or []
        if wanted.issubset({s.get("id") for s in snaps}):
            return [s for s in snaps if s.get("id") in wanted]
    return None
