"""Dead-man's-switch heartbeat publisher — spec J §6 + J-D4.

Periodically dispatches one summary email. Telegram is deliberately NOT
the heartbeat channel (silence on Telegram is confusable with takedown).
After heartbeat_breach_threshold consecutive failures, sets
obs_dead_mans_switch_breach anti-obligation row; the next successful
heartbeat clears it.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from mthydra.controller.observability.sinks import AlertPayload
from mthydra.controller.observability.snapshot import collect_snapshot
from mthydra.controller.state import alert_log as _al
from mthydra.controller.state.audit import log_event
from mthydra.controller.state.db import connect
from mthydra.controller.state.obligations import set_obligation


def _default_clock() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _add_seconds_iso(iso: str, seconds: float) -> str:
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (t + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


_BREACH_OBLIGATION_ID = "obs_dead_mans_switch_breach"


class ObsHeartbeatPublisher:
    """Email-only heartbeat. Failure streak -> breach anti-obligation."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        email_sink: Callable[[AlertPayload], object],
        interval_seconds: float,
        breach_threshold: int = 3,
        mode: str = "production",
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.email_sink = email_sink
        self.interval_seconds = interval_seconds
        self.breach_threshold = breach_threshold
        self.mode = mode
        self._clock = clock or _default_clock
        self._consecutive_failures = 0
        self._scheduler: BackgroundScheduler | None = None

    def arm(self) -> None:
        if self.mode == "offline":
            return
        executors = {"default": ThreadPoolExecutor(max_workers=1)}
        self._scheduler = BackgroundScheduler(executors=executors, daemon=True)
        self._scheduler.add_job(
            self.run_once,
            trigger=IntervalTrigger(seconds=self.interval_seconds),
        )
        self._scheduler.start()

    def disarm(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    def run_once(self) -> dict[str, object]:
        now = self._clock()
        conn = connect(self.db_path)
        try:
            snap = collect_snapshot(conn, now=now)
            subject = f"mthydra heartbeat @ {now}"
            body = snap.summary_line
            payload = AlertPayload(
                severity="heartbeat", kind="heartbeat", target=None,
                dedupe_key=f"heartbeat::{now}",
                subject=subject, body=body,
            )
            try:
                res = self.email_sink(payload)
                success = bool(getattr(res, "success", False))
                err = getattr(res, "error", None)
            except Exception as e:
                success = False
                err = repr(e)
            _al.append(
                conn, attempted_at=now,
                delivered_at=now if success else None,
                sink="email", severity="heartbeat",
                kind="heartbeat", target=None,
                dedupe_key=payload.dedupe_key,
                payload=f"{subject}\n\n{body}",
                error=err,
            )
            if success:
                self._consecutive_failures = 0
                self._clear_breach(conn)
                next_due = _add_seconds_iso(now, self.interval_seconds * 2)
                set_obligation(
                    conn,
                    obligation_id="obs_heartbeat_proven",
                    last_proven_at=now, proven_by="heartbeat",
                    next_due_at=next_due, details=None,
                )
                log_event(
                    conn, ts=now, actor="heartbeat",
                    action="heartbeat_delivered",
                    target=None, details_json=None,
                )
            else:
                self._consecutive_failures += 1
                log_event(
                    conn, ts=now, actor="heartbeat",
                    action="heartbeat_failed",
                    target=None,
                    details_json=json.dumps({
                        "error": err,
                        "consecutive_failures": self._consecutive_failures,
                    }),
                )
                if self._consecutive_failures >= self.breach_threshold:
                    set_obligation(
                        conn,
                        obligation_id=_BREACH_OBLIGATION_ID,
                        last_proven_at=now, proven_by="heartbeat",
                        next_due_at=now,
                        details=json.dumps({
                            "consecutive_failures": self._consecutive_failures,
                            "last_error": err,
                        }),
                    )
            return {
                "success": success,
                "consecutive_failures": self._consecutive_failures,
            }
        finally:
            conn.close()

    @staticmethod
    def _clear_breach(conn) -> None:
        conn.execute(
            "DELETE FROM obligation_clocks WHERE obligation_id=?",
            (_BREACH_OBLIGATION_ID,),
        )
        conn.commit()
