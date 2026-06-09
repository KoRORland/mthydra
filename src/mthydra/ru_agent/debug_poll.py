"""RU-side debug-flag poller. stdlib-only (ru_agent AST guard forbids
mthydra.controller imports; mthydra.debuglog is allowed).

The agent cannot restart (tmpfs seed + once-per-instance cloud-init), so debug
is toggled on the LIVE process by a flag file on tmpfs. Output goes to tmpfs
only and dies on reboot — consistent with the no-persistent-storage invariant.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from mthydra import debuglog

DEBUG_FLAG_PATH = "/run/mthydra/debug.flag"
DEBUG_LOG_PATH = "/run/mthydra/debug/agent-debug.log"
POLL_SECONDS = 5
_MAX_BYTES = 2 * 1024 * 1024
_BACKUPS = 2


class DebugPoller:
    def __init__(self, *, flag_path: Path | str = DEBUG_FLAG_PATH,
                 log_path: Path | str = DEBUG_LOG_PATH,
                 enable_fn: Callable[[], None] | None = None,
                 disable_fn: Callable[[], None] | None = None) -> None:
        self._flag = Path(flag_path)
        self._log_path = Path(log_path)
        self._enabled = False
        self._enable = enable_fn or self._default_enable
        self._disable = disable_fn or debuglog.disable

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _default_enable(self) -> None:
        debuglog.enable(sink=self._log_path, max_bytes=_MAX_BYTES,
                        backups=_BACKUPS)

    def tick(self) -> None:
        """One poll iteration. Never raises (must not take down the agent)."""
        try:
            present = self._flag.exists()
            if present and not self._enabled:
                self._enable()
                self._enabled = True
            elif not present and self._enabled:
                self._disable()
                self._enabled = False
        except Exception:
            # Leave _enabled unchanged so the next tick retries the transition.
            pass

    def run_forever(self, *, sleep_fn: Callable[[float], None] = time.sleep,
                    poll_seconds: float = POLL_SECONDS) -> None:
        while True:
            self.tick()
            sleep_fn(poll_seconds)
