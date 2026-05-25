"""Probe audit scheduler — spec I §7.1.

Periodically:
  * runs evaluate_box(...) on every live box
  * emits per-box `probe_kill_pending` anti-obligation rows on hard_kill /
    soft_threshold_reached verdicts; clears them when verdict returns to
    healthy (or the box is no longer live)
  * emits per-box `probe_coverage_pending` anti-obligation rows when the
    last probe is older than coverage_window_seconds; clears on fresh probes
  * emits per-vantage `probe_vantage_rotation_pending` anti-obligation rows
    for active vantages past TTL; clears on retire/burn
  * heartbeats `probe_audit_sweep_ran`
  * emits `probe_evaluate_blocked::<box_id>` when EvaluationError fires
    (missing image profile)
"""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from mthydra.controller.probe.evaluator import (
    EvaluationError,
    ProbeConfigView,
    evaluate_box,
)
from mthydra.controller.state import probe_results as _pr
from mthydra.controller.state import probe_vantages as _pv
from mthydra.controller.state.audit import log_event
from mthydra.controller.state.db import connect
from mthydra.controller.state.obligations import set_obligation


def _default_clock() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(ts: str) -> int:
    return int(
        datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
        .replace(tzinfo=timezone.utc).timestamp()
    )


def _add_seconds_iso(iso: str, seconds: float) -> str:
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (t + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


class ProbeAuditWheel:
    """Per-tick: evaluate every live box; emit/clear obligations."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        cfg: ProbeConfigView,
        coverage_window_seconds: int,
        probe_vantage_ttl_days: int,
        sweep_interval_seconds: float,
        mode: str = "production",
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.cfg = cfg
        self.coverage_window_seconds = coverage_window_seconds
        self.probe_vantage_ttl_days = probe_vantage_ttl_days
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

    def run_once(self) -> dict[str, list[str]]:
        now = self._clock()
        conn = connect(self.db_path)
        try:
            kill_pending: list[str] = []
            coverage_pending: list[str] = []
            blocked: list[str] = []
            rotation_pending: list[str] = []

            live_boxes = [
                r[0] for r in conn.execute(
                    "SELECT box_id FROM ru_boxes WHERE state='live'"
                ).fetchall()
            ]
            for box_id in live_boxes:
                # Evaluation verdict.
                try:
                    res = evaluate_box(
                        conn, box_id=box_id, cfg=self.cfg, now=now,
                    )
                except EvaluationError as e:
                    set_obligation(
                        conn,
                        obligation_id=f"probe_evaluate_blocked::{box_id}",
                        last_proven_at=now,
                        proven_by="probe_audit_sweep",
                        next_due_at=now,
                        details=json.dumps({"reason": str(e)}),
                    )
                    blocked.append(box_id)
                    # Coverage check still applies even when evaluator is blocked.
                else:
                    if res.verdict in ("hard_kill", "soft_threshold_reached"):
                        set_obligation(
                            conn,
                            obligation_id=f"probe_kill_pending::{box_id}",
                            last_proven_at=now,
                            proven_by="probe_audit_sweep",
                            next_due_at=now,
                            details=json.dumps({
                                "verdict": res.verdict,
                                "offending_checks": list(res.offending_checks),
                                "evidence_pointer": list(res.evidence_pointer),
                            }),
                        )
                        kill_pending.append(box_id)
                    else:
                        conn.execute(
                            "DELETE FROM obligation_clocks WHERE obligation_id=?",
                            (f"probe_kill_pending::{box_id}",),
                        )
                        conn.execute(
                            "DELETE FROM obligation_clocks WHERE obligation_id=?",
                            (f"probe_evaluate_blocked::{box_id}",),
                        )

                # Coverage check.
                last = _pr.last_cycle_at(conn, box_id)
                if last is None or (
                    _parse_iso(now) - _parse_iso(last) > self.coverage_window_seconds
                ):
                    set_obligation(
                        conn,
                        obligation_id=f"probe_coverage_pending::{box_id}",
                        last_proven_at=now,
                        proven_by="probe_audit_sweep",
                        next_due_at=now,
                        details=json.dumps({"last_cycle_at": last}),
                    )
                    coverage_pending.append(box_id)
                else:
                    conn.execute(
                        "DELETE FROM obligation_clocks WHERE obligation_id=?",
                        (f"probe_coverage_pending::{box_id}",),
                    )

            # Clean kill_pending rows for boxes that are no longer live.
            stale = conn.execute(
                "SELECT obligation_id FROM obligation_clocks "
                "WHERE obligation_id LIKE 'probe_kill_pending::%' "
                "AND substr(obligation_id, length('probe_kill_pending::') + 1) NOT IN "
                "(SELECT box_id FROM ru_boxes WHERE state='live')"
            ).fetchall()
            for (oid,) in stale:
                conn.execute(
                    "DELETE FROM obligation_clocks WHERE obligation_id=?", (oid,)
                )

            # Vantage rotation.
            overdue_vantages = _pv.list_due_for_rotation(
                conn, now=now, ttl_days=self.probe_vantage_ttl_days,
            )
            for vid in overdue_vantages:
                set_obligation(
                    conn,
                    obligation_id=f"probe_vantage_rotation_pending::{vid}",
                    last_proven_at=now,
                    proven_by="probe_audit_sweep",
                    next_due_at=now,
                    details=json.dumps({"vantage_id": vid}),
                )
                rotation_pending.append(vid)
            # Clear rotation rows for vantages that are no longer active.
            stale_rot = conn.execute(
                "SELECT obligation_id FROM obligation_clocks "
                "WHERE obligation_id LIKE 'probe_vantage_rotation_pending::%' "
                "AND substr(obligation_id, length('probe_vantage_rotation_pending::') + 1) NOT IN "
                "(SELECT vantage_id FROM probe_vantages WHERE state='active')"
            ).fetchall()
            for (oid,) in stale_rot:
                conn.execute(
                    "DELETE FROM obligation_clocks WHERE obligation_id=?", (oid,)
                )

            self._heartbeat(
                conn, now,
                kill=len(kill_pending), coverage=len(coverage_pending),
                blocked=len(blocked), rotation=len(rotation_pending),
            )
            conn.commit()
            return {
                "kill_pending": kill_pending,
                "coverage_pending": coverage_pending,
                "blocked": blocked,
                "rotation_pending": rotation_pending,
            }
        finally:
            conn.close()

    def _heartbeat(
        self,
        conn,
        now: str,
        *,
        kill: int,
        coverage: int,
        blocked: int,
        rotation: int,
    ) -> None:
        next_due = _add_seconds_iso(now, self.sweep_interval_seconds * 2)
        set_obligation(
            conn,
            obligation_id="probe_audit_sweep_ran",
            last_proven_at=now,
            proven_by="probe_audit_sweep",
            next_due_at=next_due,
            details=json.dumps({
                "kill": kill, "coverage": coverage,
                "blocked": blocked, "rotation": rotation,
            }),
        )
        log_event(
            conn, ts=now, actor="probe_audit_sweep",
            action="probe_audit_sweep",
            target=None,
            details_json=json.dumps({
                "kill": kill, "coverage": coverage,
                "blocked": blocked, "rotation": rotation,
            }),
        )
