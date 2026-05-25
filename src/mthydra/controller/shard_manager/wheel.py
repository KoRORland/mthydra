"""Shard reshuffle scheduler — spec H §7.1.

Periodically:
  * reshuffles every shard past `reshuffle_interval_days`
  * folds unassigned users into new shards
  * heartbeats `shard_reshuffle_sweep_ran` each tick
  * emits per-shard `shard_overdue_pending` anti-obligation rows
    that disappear once the shard is reshuffled

Same all-synchronous + BackgroundScheduler model as
`mthydra.controller.state.cover_pool_scheduler`. Offline mode disables
the timer entirely; tests use run_once() with a frozen clock.
"""
from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from mthydra.controller.shard_manager.picker import pick_new_rosters
from mthydra.controller.state import shards as _shards
from mthydra.controller.state.audit import log_event
from mthydra.controller.state.db import connect
from mthydra.controller.state.obligations import set_obligation
from mthydra.controller.state.users_shards import reshuffle_unassigned


def _default_clock() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _add_seconds_iso(iso: str, seconds: float) -> str:
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (t + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_shard_id() -> str:
    return str(uuid.uuid4())


class ShardReshuffleWheel:
    """Periodic shard reshuffle + unassigned fold-in (spec H §7.1)."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        target_size: int,
        max_size: int,
        reshuffle_interval_days: int,
        sweep_interval_seconds: float,
        mode: str = "production",
        clock: Callable[[], str] | None = None,
        shard_id_factory: Callable[[], str] = _default_shard_id,
    ) -> None:
        self.db_path = Path(db_path)
        self.target_size = target_size
        self.max_size = max_size
        self.reshuffle_interval_days = reshuffle_interval_days
        self.sweep_interval_seconds = sweep_interval_seconds
        self.mode = mode
        self._clock = clock or _default_clock
        self._shard_id_factory = shard_id_factory
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
        """One sweep. Returns {"reshuffled": [...new_sids], "folded_in": [...new_sids]}."""
        now = self._clock()
        conn = connect(self.db_path)
        try:
            h = _shards.health(
                conn, now=now,
                reshuffle_interval_seconds=self.reshuffle_interval_days * 86400,
            )

            reshuffled: list[str] = []
            for old_sid in h.overdue_for_reshuffle:
                old_shard = _shards.get_shard(conn, old_sid)
                rosters = pick_new_rosters(
                    current_members=json.loads(old_shard.members_json),
                    unassigned=[],
                    target_size=self.target_size,
                )
                if not rosters:
                    # Nothing to reshuffle (empty shard — invariant #36 would
                    # have caught it; just retire and move on).
                    _shards.retire_shard(conn, old_sid, at=now)
                    continue
                primary = rosters[0]
                new_sid = self._shard_id_factory()
                _shards.reshuffle(
                    conn, old_sid,
                    now=now,
                    target_size=self.target_size,
                    new_shard_id=new_sid,
                    new_members=primary,
                    reason="ttl",
                )
                reshuffled.append(new_sid)
                # Leftover chunks (rare with small members): each becomes its own shard.
                for leftover in rosters[1:]:
                    extra_sid = self._shard_id_factory()
                    _shards.create_shard(
                        conn, shard_id=extra_sid, members=leftover,
                        target_size=self.target_size, at=now,
                    )
                    for u in leftover:
                        conn.execute(
                            "UPDATE users SET current_shard_id=? WHERE user_id=?",
                            (extra_sid, u),
                        )
                    reshuffled.append(extra_sid)
                conn.commit()
                _clear_overdue_obligation(conn, old_sid)

            folded_in = reshuffle_unassigned(
                conn,
                now=now,
                target_size=self.target_size,
                shard_id_factory=self._shard_id_factory,
            )

            # Re-check after reshuffle. Anything still overdue (e.g. because it was
            # newly created and somehow already past TTL — shouldn't happen but we
            # don't pre-judge) gets a fresh anti-obligation row.
            h2 = _shards.health(
                conn, now=now,
                reshuffle_interval_seconds=self.reshuffle_interval_days * 86400,
            )
            for sid in h2.overdue_for_reshuffle:
                set_obligation(
                    conn,
                    obligation_id=f"shard_overdue_pending::{sid}",
                    last_proven_at=now,
                    proven_by="shard_reshuffle_sweep",
                    next_due_at=now,
                    details=json.dumps({"shard_id": sid}),
                )
            self._heartbeat(
                conn, now,
                reshuffled=len(reshuffled),
                folded_in=len(folded_in),
            )
            return {"reshuffled": reshuffled, "folded_in": folded_in}
        finally:
            conn.close()

    def _heartbeat(
        self, conn, now: str, *, reshuffled: int, folded_in: int,
    ) -> None:
        next_due = _add_seconds_iso(now, self.sweep_interval_seconds * 2)
        set_obligation(
            conn,
            obligation_id="shard_reshuffle_sweep_ran",
            last_proven_at=now,
            proven_by="shard_reshuffle_sweep",
            next_due_at=next_due,
            details=json.dumps({
                "reshuffled": reshuffled, "folded_in": folded_in,
            }),
        )
        if reshuffled > 0:
            # Spec H §12: shard_reshuffle_proven is proven on every successful
            # reshuffle (TTL or compromise). Cadence = reshuffle_interval_days x 2.
            next_proof_due = _add_seconds_iso(
                now, self.reshuffle_interval_days * 86400 * 2,
            )
            set_obligation(
                conn,
                obligation_id="shard_reshuffle_proven",
                last_proven_at=now,
                proven_by="shard_reshuffle_sweep",
                next_due_at=next_proof_due,
                details=json.dumps({"reshuffled": reshuffled}),
            )
        log_event(
            conn, ts=now, actor="shard_reshuffle_sweep",
            action="shard_reshuffle_sweep",
            target=None,
            details_json=json.dumps({
                "reshuffled": reshuffled, "folded_in": folded_in,
            }),
        )


def _clear_overdue_obligation(conn, shard_id: str) -> None:
    """Remove the anti-obligation row that flagged the now-reshuffled shard."""
    conn.execute(
        "DELETE FROM obligation_clocks WHERE obligation_id=?",
        (f"shard_overdue_pending::{shard_id}",),
    )
    conn.commit()
