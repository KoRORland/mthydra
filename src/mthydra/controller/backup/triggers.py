"""Backup triggers: floor timer + burned_domains-change debouncer + manual.

All-synchronous per plan §16.3: BackgroundScheduler, threading.Timer debouncer.
No asyncio in this module.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Protocol

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from mthydra.controller.state.backup_log import BackupTrigger

_log = logging.getLogger(__name__)


class _PipelineLike(Protocol):
    def do_backup(self, trigger: BackupTrigger) -> int: ...


class BackupOrchestrator:
    """Orchestrates all three trigger paths (spec A §6.1, synchronous model).

    - Floor timer: APScheduler BackgroundScheduler fires every floor_interval_seconds.
    - On-change debouncer: threading.Timer reset on each notify_burned_change() call;
      fires do_backup after debounce_seconds of silence.
    - Manual: run_manual() calls do_backup directly on the calling thread.

    In offline mode, arm() is a no-op and run_manual() is disabled.
    """

    def __init__(
        self,
        pipeline: _PipelineLike,
        debounce_seconds: float,
        floor_interval_seconds: float,
        mode: str = "production",
        timer_factory: Callable[[float, Callable], threading.Timer] | None = None,
    ) -> None:
        self.pipeline = pipeline
        self.debounce_seconds = debounce_seconds
        self.floor_interval_seconds = floor_interval_seconds
        self.mode = mode
        self._timer_factory = timer_factory or (lambda delay, fn: threading.Timer(delay, fn))
        self._debounce_timer: threading.Timer | None = None
        self._debounce_lock = threading.Lock()
        self._scheduler: BackgroundScheduler | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def arm(self) -> None:
        """Start the floor timer. No-op in offline mode."""
        if self.mode == "offline":
            return
        executors = {"default": ThreadPoolExecutor(max_workers=1)}
        self._scheduler = BackgroundScheduler(executors=executors, daemon=True)
        self._scheduler.add_job(
            self._fire_floor_timer,
            trigger=IntervalTrigger(seconds=self.floor_interval_seconds),
        )
        self._scheduler.start()

    def disarm(self) -> None:
        """Stop the floor timer and cancel any pending debounce."""
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
        self._cancel_debounce()

    # ------------------------------------------------------------------
    # Trigger paths
    # ------------------------------------------------------------------

    def notify_burned_change(self) -> None:
        """Reset the debounce timer on each burned_domains change (spec §6.1)."""
        if self.mode == "offline":
            return
        with self._debounce_lock:
            self._cancel_debounce()
            t = self._timer_factory(self.debounce_seconds, self._fire_burned_change)
            self._debounce_timer = t
            t.start()

    def run_manual(self) -> int:
        """Run a backup immediately on the calling thread. Refused in offline mode."""
        if self.mode == "offline":
            raise RuntimeError("run_manual refused: controller is in offline mode")
        return self.pipeline.do_backup(BackupTrigger.MANUAL)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _fire_floor_timer(self) -> None:
        try:
            self.pipeline.do_backup(BackupTrigger.FLOOR_TIMER)
        except Exception:
            # pipeline records failures; loop must continue, but never silently.
            _log.exception("floor-timer backup tick failed")

    def _fire_burned_change(self) -> None:
        try:
            self.pipeline.do_backup(BackupTrigger.BURNED_DOMAINS_CHANGE)
        except Exception:
            _log.exception("burned-change backup tick failed")

    def _cancel_debounce(self) -> None:
        if self._debounce_timer is not None:
            self._debounce_timer.cancel()
            self._debounce_timer = None
