"""Supervise mtg + sing-box. Restart on transient failure; self-terminate
on persistent crash-loop (>=4 crashes in 5min)."""
from __future__ import annotations

import subprocess
import time
from typing import Callable


class Supervisor:
    CRASH_WINDOW_SECONDS = 5 * 60
    CRASH_MAX_IN_WINDOW = 4

    def __init__(
        self,
        *,
        mtg_cmd: list[str],
        sing_box_cmd: list[str],
        clock: Callable[[], float] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        on_persistent_failure: Callable[[str], None] | None = None,
    ):
        self._mtg_cmd = mtg_cmd
        self._sing_box_cmd = sing_box_cmd
        self._clock = clock or time.monotonic
        self._sleep_fn = sleep_fn or time.sleep
        self._on_failure = on_persistent_failure or (lambda r: None)
        self._mtg_proc: subprocess.Popen | None = None
        self._sing_box_proc: subprocess.Popen | None = None
        self._mtg_crashes: list[float] = []
        self._sing_box_crashes: list[float] = []

    def launch_all(self) -> None:
        self._mtg_proc = subprocess.Popen(self._mtg_cmd)
        self._sing_box_proc = subprocess.Popen(self._sing_box_cmd)

    def check_children_once(self) -> None:
        now = self._clock()
        for name, proc_attr, cmd, crashes in (
            ("mtg", "_mtg_proc", self._mtg_cmd, self._mtg_crashes),
            ("sing-box", "_sing_box_proc", self._sing_box_cmd, self._sing_box_crashes),
        ):
            proc = getattr(self, proc_attr)
            if proc is None:
                continue
            rc = proc.poll()
            if rc is None:
                continue  # still running
            # Crashed.
            crashes.append(now)
            crashes[:] = [t for t in crashes if now - t < self.CRASH_WINDOW_SECONDS]
            if len(crashes) >= self.CRASH_MAX_IN_WINDOW:
                self._on_failure(
                    f"{name} crashed {len(crashes)} times in "
                    f"{self.CRASH_WINDOW_SECONDS}s"
                )
                return
            # Backoff: 2^(n-1) seconds, capped at 8.
            backoff = min(8.0, 2.0 ** (len(crashes) - 1))
            self._sleep_fn(backoff)
            setattr(self, proc_attr, subprocess.Popen(cmd))

    def shutdown_children(self) -> None:
        for proc in (self._mtg_proc, self._sing_box_proc):
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()

    def run_forever(self) -> None:
        try:
            while True:
                self.check_children_once()
                self._sleep_fn(1.0)
        except KeyboardInterrupt:
            self.shutdown_children()
