"""Wire the EU debug flag into a running controller: arm on start, auto-expire.

Imports both ``debuglog`` (the facility) and ``debug_flag`` (persistence).
EU-only — never imported by ru_agent.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from mthydra import debuglog
from mthydra.controller import debug_flag

DEBUG_LOG_PATH = "/var/log/mthydra/debug.log"
WATCH_POLL_SECONDS = 60
_MAX_BYTES = 10 * 1024 * 1024
_BACKUPS = 5


def arm_from_flag(flag_path: Path | str, *, log_path: Path | str = DEBUG_LOG_PATH,
                  now: float | None = None, spawn_watcher: bool = True) -> bool:
    """If a non-expired flag exists, enable debug + start the expiry watcher.

    Returns True iff debug was enabled. A stale/expired flag is removed.
    """
    now = time.time() if now is None else now
    f = debug_flag.read_flag(flag_path)
    if f is None:
        return False
    if f.is_expired(now):
        debug_flag.clear_flag(flag_path)
        return False
    debuglog.enable(sink=log_path, max_bytes=_MAX_BYTES, backups=_BACKUPS)
    if spawn_watcher:
        threading.Thread(target=_watch, args=(flag_path,), daemon=True,
                         name="debug-expiry").start()
    return True


def expiry_tick(flag_path: Path | str, *, now: float | None = None) -> bool:
    """One watcher iteration. Returns True iff debug was turned off."""
    now = time.time() if now is None else now
    f = debug_flag.read_flag(flag_path)
    if f is None or f.is_expired(now):
        debuglog.disable()
        debug_flag.clear_flag(flag_path)
        return True
    return False


def _watch(flag_path: Path | str) -> None:
    while True:
        time.sleep(WATCH_POLL_SECONDS)
        if expiry_tick(flag_path):
            return
