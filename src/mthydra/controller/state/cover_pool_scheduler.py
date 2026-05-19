"""Cover-pool sweep schedulers (spec C §7).

Two APScheduler-driven sweeps:
  * CoverPoolReverifySweep — TTL downgrade of stale candidate_verified rows
  * CoverPoolRotationSweep  — flags due-for-rotation in_use domains

Both follow the same all-synchronous + BackgroundScheduler model as
mthydra.descriptor.scheduler.DescriptorRotator. Offline mode disables
the timer entirely; tests use run_once() with a frozen clock.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from mthydra.controller.state.audit import log_event
from mthydra.controller.state.cover_pool import (
    downgrade_stale_verified,
    list_due_for_rotation,
    pool_health,
)
from mthydra.controller.state.db import connect
from mthydra.controller.state.obligations import set_obligation


def _default_clock() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _add_seconds_iso(iso: str, seconds: float) -> str:
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (t + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


class CoverPoolReverifySweep:
    """Periodic downgrade of stale candidate_verified rows (spec C §7.1)."""

    def __init__(
        self,
        db_path: Path | str,
        reverify_after_days: int,
        sweep_interval_seconds: float,
        mode: str = "production",
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.reverify_after_days = reverify_after_days
        self.sweep_interval_seconds = sweep_interval_seconds
        self.mode = mode
        self._clock = clock or _default_clock
        self._scheduler: BackgroundScheduler | None = None

    def arm(self) -> None:
        if self.mode == "offline":
            return
        executors = {"default": ThreadPoolExecutor(max_workers=1)}
        self._scheduler = BackgroundScheduler(executors=executors, daemon=True)
        self._scheduler.add_job(
            self.run_once,
            trigger=IntervalTrigger(seconds=self.sweep_interval_seconds),
        )
        self._scheduler.start()

    def disarm(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    def run_once(self) -> list[str]:
        now = self._clock()
        conn = connect(self.db_path)
        try:
            downgraded = downgrade_stale_verified(
                conn, now=now, reverify_after_days=self.reverify_after_days,
            )
            log_event(
                conn, ts=now, actor="reverify_sweep", action="cover_reverify_sweep",
                target=None, details_json=json.dumps({"downgraded": len(downgraded)}),
            )
            next_due = _add_seconds_iso(now, self.sweep_interval_seconds * 2)
            set_obligation(
                conn,
                obligation_id="cover_pool_reverify_sweep_ran",
                last_proven_at=now,
                proven_by="reverify_sweep",
                next_due_at=next_due,
                details=json.dumps({"downgraded": len(downgraded)}),
            )
            return downgraded
        finally:
            conn.close()


class CoverPoolRotationSweep:
    """Periodic detection of due-for-rotation in_use domains (spec C §7.2)."""

    def __init__(
        self,
        db_path: Path | str,
        rotation_ttl_days: int,
        freeze_threshold: int,
        sweep_interval_seconds: float,
        mode: str = "production",
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.rotation_ttl_days = rotation_ttl_days
        self.freeze_threshold = freeze_threshold
        self.sweep_interval_seconds = sweep_interval_seconds
        self.mode = mode
        self._clock = clock or _default_clock
        self._scheduler: BackgroundScheduler | None = None

    def arm(self) -> None:
        if self.mode == "offline":
            return
        executors = {"default": ThreadPoolExecutor(max_workers=1)}
        self._scheduler = BackgroundScheduler(executors=executors, daemon=True)
        self._scheduler.add_job(
            self.run_once,
            trigger=IntervalTrigger(seconds=self.sweep_interval_seconds),
        )
        self._scheduler.start()

    def disarm(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    def run_once(self) -> list[str]:
        """Returns the list of domains flagged due-for-rotation (empty if frozen)."""
        now = self._clock()
        conn = connect(self.db_path)
        try:
            h = pool_health(conn, freeze_threshold=self.freeze_threshold)
            if h.rotation_frozen:
                set_obligation(
                    conn,
                    obligation_id="cover_pool_rotation_frozen",
                    last_proven_at=now,
                    proven_by="rotation_sweep",
                    next_due_at=now,
                    details=json.dumps({
                        "candidate_verified": h.candidate_verified,
                        "freeze_threshold": self.freeze_threshold,
                    }),
                )
                self._heartbeat(conn, now, flagged=0, frozen=True)
                return []

            # Pool healthy → ensure the freeze obligation row is cleared (if it exists)
            conn.execute(
                "DELETE FROM obligation_clocks WHERE obligation_id='cover_pool_rotation_frozen'"
            )
            conn.commit()

            due = list_due_for_rotation(
                conn, now=now, rotation_ttl_days=self.rotation_ttl_days,
            )
            flagged = [d.domain for d in due]
            for domain in flagged:
                set_obligation(
                    conn,
                    obligation_id=f"cover_pool_rotation_pending::{domain}",
                    last_proven_at=now,
                    proven_by="rotation_sweep",
                    next_due_at=now,
                    details=json.dumps({"domain": domain}),
                )
            self._heartbeat(conn, now, flagged=len(flagged), frozen=False)
            return flagged
        finally:
            conn.close()

    def _heartbeat(self, conn, now: str, *, flagged: int, frozen: bool) -> None:
        next_due = _add_seconds_iso(now, self.sweep_interval_seconds * 2)
        set_obligation(
            conn,
            obligation_id="cover_pool_rotation_sweep_ran",
            last_proven_at=now,
            proven_by="rotation_sweep",
            next_due_at=next_due,
            details=json.dumps({"flagged": flagged, "frozen": frozen}),
        )
        log_event(
            conn, ts=now, actor="rotation_sweep", action="cover_rotation_sweep",
            target=None, details_json=json.dumps({"flagged": flagged, "frozen": frozen}),
        )
