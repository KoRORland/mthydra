"""Per-user dead-man's switch heartbeat — spec K §7 + K-D7.

Daily "still here" pulse via Telegram only (K-D7: email per-day is
unkind to users). Per-user in-memory failure counter; at threshold,
emit dist_user_heartbeat_breach::<user_id> anti-obligation which spec J
escalates as crit to the operator.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from mthydra.controller.state import distribution_log as _dl
from mthydra.controller.state import user_channels as _uc
from mthydra.controller.state.audit import log_event
from mthydra.controller.state.db import connect
from mthydra.controller.state.obligations import set_obligation


def _default_clock() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _add_seconds_iso(iso: str, seconds: float) -> str:
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (t + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


class DistUserHeartbeatPublisher:
    """Telegram-only per-user heartbeat scheduler."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        telegram_sink: Callable,
        interval_seconds: float,
        breach_threshold: int = 3,
        mode: str = "production",
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.telegram_sink = telegram_sink
        self.interval_seconds = interval_seconds
        self.breach_threshold = breach_threshold
        self.mode = mode
        self._clock = clock or _default_clock
        self._failures: dict[str, int] = {}
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

    def run_once(self) -> dict[str, int]:
        now = self._clock()
        conn = connect(self.db_path)
        try:
            sent = 0
            failed = 0
            for row in _uc.list_channels(conn):
                if not row.telegram_chat_id:
                    continue
                try:
                    res = self.telegram_sink(
                        chat_id=row.telegram_chat_id,
                        message=f"mthydra heartbeat @ {now}",
                    )
                    success = bool(getattr(res, "success", False))
                    err = getattr(res, "error", None)
                except Exception as e:
                    success = False
                    err = repr(e)
                _dl.append(
                    conn, user_id=row.user_id, channel="telegram",
                    kind="heartbeat",
                    attempted_at=now,
                    delivered_at=now if success else None,
                    subset_hash=None,
                    payload_json=f"heartbeat @ {now}",
                    error=err,
                )
                if success:
                    sent += 1
                    self._failures[row.user_id] = 0
                    next_due = _add_seconds_iso(now, self.interval_seconds * 2)
                    set_obligation(
                        conn,
                        obligation_id=f"dist_user_heartbeat_proven::{row.user_id}",
                        last_proven_at=now, proven_by="dist_user_heartbeat",
                        next_due_at=next_due, details=None,
                    )
                    conn.execute(
                        "DELETE FROM obligation_clocks WHERE obligation_id=?",
                        (f"dist_user_heartbeat_breach::{row.user_id}",),
                    )
                    conn.commit()
                else:
                    failed += 1
                    self._failures[row.user_id] = self._failures.get(row.user_id, 0) + 1
                    log_event(
                        conn, ts=now, actor="dist_user_heartbeat",
                        action="user_heartbeat_failed",
                        target=row.user_id,
                        details_json=json.dumps({
                            "consecutive_failures": self._failures[row.user_id],
                            "error": err,
                        }),
                    )
                    if self._failures[row.user_id] >= self.breach_threshold:
                        set_obligation(
                            conn,
                            obligation_id=f"dist_user_heartbeat_breach::{row.user_id}",
                            last_proven_at=now, proven_by="dist_user_heartbeat",
                            next_due_at=now,
                            details=json.dumps({
                                "consecutive_failures": self._failures[row.user_id],
                                "last_error": err,
                            }),
                        )
            return {"sent": sent, "failed": failed}
        finally:
            conn.close()
