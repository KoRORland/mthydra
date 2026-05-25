"""Per-user delta-only subset publisher — spec K §7.

Per tick: for each user with current_shard_id IS NOT NULL, build subset,
check user_channels, hash-dedupe against last delivered, dispatch to
configured sinks, append distribution_log row per attempt.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from mthydra.controller.distribution.payload import (
    build_subset,
    payload_to_json,
)
from mthydra.controller.distribution.sinks import DryRunDistributionSink
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


class DistributionPublisher:
    """Per-tick delta-only per-user subset publisher."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        telegram_sink: Callable,
        email_sink: Callable,
        sweep_interval_seconds: float,
        mode: str = "production",
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.telegram_sink = telegram_sink
        self.email_sink = email_sink
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

    def run_once(self) -> dict[str, int]:
        now = self._clock()
        conn = connect(self.db_path)
        try:
            dispatched = 0
            deduped = 0
            unregistered = 0
            assigned = [
                r[0] for r in conn.execute(
                    "SELECT user_id FROM users WHERE current_shard_id IS NOT NULL "
                    "ORDER BY user_id"
                ).fetchall()
            ]
            for user_id in assigned:
                payload = build_subset(conn, user_id, now=now)
                if payload is None:
                    continue
                channels = _uc.get_channels(conn, user_id)
                if channels is None or (
                    not channels.telegram_chat_id and not channels.email_addr
                ):
                    set_obligation(
                        conn,
                        obligation_id=f"dist_user_unregistered::{user_id}",
                        last_proven_at=now, proven_by="dist_publisher",
                        next_due_at=now,
                        details=json.dumps({"user_id": user_id}),
                    )
                    unregistered += 1
                    log_event(
                        conn, ts=now, actor="dist_publisher",
                        action="dist_unregistered_skip",
                        target=user_id, details_json=None,
                    )
                    continue
                # Channels exist — clear the anti-obligation if present.
                conn.execute(
                    "DELETE FROM obligation_clocks WHERE obligation_id=?",
                    (f"dist_user_unregistered::{user_id}",),
                )
                payload_body = payload_to_json(payload)

                for channel_label, configured in (
                    ("telegram", channels.telegram_chat_id),
                    ("email", channels.email_addr),
                ):
                    if not configured:
                        continue
                    last_hash = _dl.last_subset_hash(conn, user_id, channel_label)
                    if last_hash == payload.subset_hash:
                        deduped += 1
                        continue
                    success, err = self._dispatch(
                        channel_label, configured, payload_body, payload,
                    )
                    _dl.append(
                        conn,
                        user_id=user_id, channel=channel_label,
                        kind="subset_delta",
                        attempted_at=now,
                        delivered_at=now if success else None,
                        subset_hash=payload.subset_hash,
                        payload_json=payload_body,
                        error=err,
                    )
                    if success:
                        dispatched += 1
                log_event(
                    conn, ts=now, actor="dist_publisher",
                    action="dist_publish_decided",
                    target=user_id,
                    details_json=json.dumps({
                        "subset_hash": payload.subset_hash,
                        "boxes": len(payload.boxes),
                    }),
                )
            self._heartbeat(conn, now, dispatched, deduped, unregistered)
            conn.commit()
            return {
                "dispatched": dispatched, "deduped": deduped,
                "unregistered": unregistered,
            }
        finally:
            conn.close()

    def _dispatch(
        self,
        channel_label: str,
        configured: str,
        payload_body: str,
        payload,
    ) -> tuple[bool, str | None]:
        sink = (
            self.telegram_sink if channel_label == "telegram"
            else self.email_sink
        )
        if self.mode == "offline":
            sink = _OFFLINE_SINK
        try:
            if channel_label == "telegram":
                res = sink(chat_id=configured, message=payload_body)
            else:
                res = sink(
                    to_addr=configured,
                    subject=(
                        f"mthydra subset update — {payload.user_id} "
                        f"({len(payload.boxes)} boxes)"
                    ),
                    body=payload_body,
                )
        except Exception as e:
            return False, repr(e)
        return (bool(getattr(res, "success", False)),
                getattr(res, "error", None))

    def _heartbeat(
        self, conn, now: str,
        dispatched: int, deduped: int, unregistered: int,
    ) -> None:
        next_due = _add_seconds_iso(now, self.sweep_interval_seconds * 2)
        set_obligation(
            conn,
            obligation_id="dist_publish_sweep_ran",
            last_proven_at=now, proven_by="dist_publisher",
            next_due_at=next_due,
            details=json.dumps({
                "dispatched": dispatched, "deduped": deduped,
                "unregistered": unregistered,
            }),
        )


_OFFLINE_SINK = DryRunDistributionSink(label="offline")
