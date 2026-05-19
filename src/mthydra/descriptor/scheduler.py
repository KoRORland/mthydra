"""Routine descriptor rotation via APScheduler (spec B §8 R1, all-synchronous per plan §16.3)."""
from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger


class DescriptorRotator:
    """Fires sign_new_descriptor on a configurable interval (spec B §8 R1).

    Uses the same all-synchronous + BackgroundScheduler model as BackupOrchestrator
    (plan §16.3).  Offline mode disables the scheduler entirely.
    """

    def __init__(
        self,
        db_path: Path | str,
        rotation_interval_seconds: float,
        validity_window_seconds: float,
        mode: str = "production",
        clock: Callable[[], str] | None = None,
        timer_factory: Callable | None = None,  # unused; kept for interface parity
    ) -> None:
        self.db_path = Path(db_path)
        self.rotation_interval_seconds = rotation_interval_seconds
        self.validity_window_seconds = validity_window_seconds
        self.mode = mode
        self._clock = clock
        self._scheduler: BackgroundScheduler | None = None

    def arm(self) -> None:
        """Start the rotation scheduler.  No-op in offline mode."""
        if self.mode == "offline":
            return
        executors = {"default": ThreadPoolExecutor(max_workers=1)}
        self._scheduler = BackgroundScheduler(executors=executors, daemon=True)
        self._scheduler.add_job(
            self._rotate,
            trigger=IntervalTrigger(seconds=self.rotation_interval_seconds),
        )
        self._scheduler.start()

    def disarm(self) -> None:
        """Stop the scheduler and cancel any pending job."""
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    def sign_now(self) -> int:
        """Force an immediate descriptor sign and return the new generation number."""
        return self._rotate() or -1

    def _rotate(self) -> int | None:
        from datetime import datetime, timedelta, timezone

        from mthydra.controller.state.db import connect
        from mthydra.descriptor.sign import SignError, sign_new_descriptor

        def _now() -> str:
            if self._clock:
                return self._clock()
            return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        now = _now()
        now_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
        valid_until = (
            now_dt + timedelta(seconds=self.validity_window_seconds)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        conn = connect(self.db_path)
        try:
            gen, _, _ = sign_new_descriptor(conn, now_iso=now, valid_until_iso=valid_until)
            return gen
        except (SignError, Exception):
            # Logged via audit_log in sign_new_descriptor; scheduler loop continues.
            return None
        finally:
            conn.close()
