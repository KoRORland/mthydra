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
from mthydra.controller.distribution.render import RenderedMessage, render_user_message
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
        breach_threshold: int = 3,
        mode: str = "production",
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.telegram_sink = telegram_sink
        self.email_sink = email_sink
        self.sweep_interval_seconds = sweep_interval_seconds
        self.breach_threshold = breach_threshold
        self.mode = mode
        self._clock = clock or _default_clock
        # Per-user consecutive "could not reach this user at all this tick"
        # counter. In-memory (resets on restart, as the old heartbeat did). At
        # breach_threshold, raise dist_user_heartbeat_breach::<user_id> for the
        # operator. No synthetic pings — this rides on real content delivery.
        self._failures: dict[str, int] = {}
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

    def run_once(self, force_user_ids: set[str] | None = None) -> dict[str, int]:
        """Publish each assigned user's current subset.

        force_user_ids: users for whom delivery bypasses the unchanged-subset
        dedupe — used when the user explicitly asks (e.g. tapping /start), where
        "you already have this" is the wrong answer. The background sweep passes
        nothing, so it still dedupes and never spams."""
        force = force_user_ids or set()
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
            # Reconcile orphans: a per-user alert is only cleared for a user we
            # still iterate (assigned). If a flagged user was later deleted or
            # unassigned, their alert would orphan forever — so drop any whose
            # user is no longer assigned. Covers both the unregistered alert and
            # the unreachable/breach alert.
            assigned_set = set(assigned)
            for (oid,) in conn.execute(
                "SELECT obligation_id FROM obligation_clocks "
                "WHERE obligation_id LIKE 'dist_user_unregistered::%' "
                "OR obligation_id LIKE 'dist_user_heartbeat_breach::%'"
            ).fetchall():
                if oid.split("::", 1)[1] not in assigned_set:
                    conn.execute(
                        "DELETE FROM obligation_clocks WHERE obligation_id=?", (oid,)
                    )
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
                rendered = render_user_message(payload)

                attempted_user = 0
                succeeded_user = 0
                for channel_label, configured in (
                    ("telegram", channels.telegram_chat_id),
                    ("email", channels.email_addr),
                ):
                    if not configured:
                        continue
                    last_hash = _dl.last_subset_hash(conn, user_id, channel_label)
                    if last_hash == payload.subset_hash and user_id not in force:
                        deduped += 1
                        continue
                    success, err = self._dispatch(
                        channel_label, configured, rendered, payload,
                    )
                    attempted_user += 1
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
                        succeeded_user += 1
                log_event(
                    conn, ts=now, actor="dist_publisher",
                    action="dist_publish_decided",
                    target=user_id,
                    details_json=json.dumps({
                        "subset_hash": payload.subset_hash,
                        "boxes": len(payload.boxes),
                    }),
                )
                # Per-user reachability (spec K §7, now ride-along instead of a
                # synthetic heartbeat): only a tick where we actually attempted
                # a send carries a signal. All attempts failing -> the user is
                # unreachable this tick; a single success clears it. Deduped /
                # unregistered ticks (attempted_user == 0) carry no signal.
                if attempted_user > 0:
                    self._update_reachability(
                        conn, now, user_id,
                        reachable=succeeded_user > 0,
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
        rendered: RenderedMessage,
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
                res = sink(chat_id=configured, message=rendered.text)
                if getattr(res, "success", False):
                    # QR photos are best-effort: the link text is the real
                    # delivery. A failed/raised photo must NOT flip this
                    # delivery to failed (which would re-dispatch the text).
                    for caption, png in rendered.qr:
                        try:
                            sink.send_photo(chat_id=configured, png=png, caption=caption)
                        except Exception:
                            pass
            else:
                res = sink(
                    to_addr=configured,
                    subject=(
                        f"mthydra proxy update — {payload.user_id} "
                        f"({len(payload.boxes)} proxies)"
                    ),
                    body=rendered.text,
                )
        except Exception as e:
            return False, repr(e)
        return (bool(getattr(res, "success", False)),
                getattr(res, "error", None))

    def _update_reachability(
        self, conn, now: str, user_id: str, *, reachable: bool,
    ) -> None:
        oid = f"dist_user_heartbeat_breach::{user_id}"
        if reachable:
            if self._failures.get(user_id):
                self._failures[user_id] = 0
            conn.execute(
                "DELETE FROM obligation_clocks WHERE obligation_id=?", (oid,)
            )
            return
        self._failures[user_id] = self._failures.get(user_id, 0) + 1
        fails = self._failures[user_id]
        log_event(
            conn, ts=now, actor="dist_publisher",
            action="dist_delivery_failed", target=user_id,
            details_json=json.dumps({"consecutive_failures": fails}),
        )
        if fails >= self.breach_threshold:
            set_obligation(
                conn, obligation_id=oid,
                last_proven_at=now, proven_by="dist_publisher",
                next_due_at=now,
                details=json.dumps({"consecutive_failures": fails}),
            )

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
