"""Shard reshuffle scheduler — spec H §7.1.

Periodically:
  * reshuffles every shard past `reshuffle_interval_days`
  * folds unassigned users into new shards
  * heartbeats `shard_reshuffle_sweep_ran` each tick
  * emits per-shard `shard_overdue_pending` anti-obligation rows
    that disappear once the shard is reshuffled

U-D3 hardening: per-shard reshuffle attempts are isolated in a
try/except so one failing shard doesn't crash the entire sweep. A
shard whose attempt raises gets `shard_overdue_pending::<sid>` with
the exception details in details_json — operator triages from there.
Same isolation for the unassigned fold-in (singleton anti-obligation
`shard_unassigned_pending` if that step fails, e.g. DB constraint).

Same all-synchronous + BackgroundScheduler model as
`mthydra.controller.state.cover_pool_scheduler`. Offline mode disables
the timer entirely; tests use run_once() with a frozen clock.
"""
from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
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
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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
            failed_shards: list[tuple[str, str]] = []  # (sid, reason)
            for old_sid in h.overdue_for_reshuffle:
                # U-D3: per-shard isolation. A pick/reshuffle that raises
                # mid-loop used to crash the whole tick (no other shards
                # got a chance). Now: catch and raise the per-shard anti
                # obligation, keep sweeping the rest.
                try:
                    new_sids = self._reshuffle_one_shard(conn, old_sid, now)
                    reshuffled.extend(new_sids)
                except Exception as e:
                    failed_shards.append(
                        (old_sid, f"{type(e).__name__}: {e}"))
                    # Rollback the partial transaction for this shard;
                    # the next shard starts fresh.
                    conn.rollback()

            # U-D3: same isolation for the unassigned fold-in. If it fails
            # (DB error, picker constraint), raise the singleton
            # shard_unassigned_pending and proceed to the heartbeat so
            # we still record this tick happened.
            folded_in: list[str] = []
            fold_in_error: str | None = None
            try:
                folded_in = reshuffle_unassigned(
                    conn,
                    now=now,
                    target_size=self.target_size,
                    shard_id_factory=self._shard_id_factory,
                )
            except Exception as e:
                fold_in_error = f"{type(e).__name__}: {e}"
                conn.rollback()

            # Re-check after reshuffle. Anything still overdue (e.g. because it was
            # newly created and somehow already past TTL — shouldn't happen but we
            # don't pre-judge) gets a fresh anti-obligation row.
            h2 = _shards.health(
                conn, now=now,
                reshuffle_interval_seconds=self.reshuffle_interval_days * 86400,
            )
            still_overdue = set(h2.overdue_for_reshuffle)
            for sid in still_overdue:
                set_obligation(
                    conn,
                    obligation_id=f"shard_overdue_pending::{sid}",
                    last_proven_at=now,
                    proven_by="shard_reshuffle_sweep",
                    next_due_at=now,
                    details=json.dumps({"shard_id": sid}),
                )
            # U-D3: shards whose attempt threw also get the anti-obligation,
            # with the exception class + message in details_json so the
            # operator triages from the alert body, not from logs.
            for sid, reason in failed_shards:
                if sid in still_overdue:
                    # Already raised above; just enrich the details.
                    set_obligation(
                        conn,
                        obligation_id=f"shard_overdue_pending::{sid}",
                        last_proven_at=now,
                        proven_by="shard_reshuffle_sweep",
                        next_due_at=now,
                        details=json.dumps({"shard_id": sid, "error": reason}),
                    )
                else:
                    set_obligation(
                        conn,
                        obligation_id=f"shard_overdue_pending::{sid}",
                        last_proven_at=now,
                        proven_by="shard_reshuffle_sweep",
                        next_due_at=now,
                        details=json.dumps({"shard_id": sid, "error": reason}),
                    )
            # U-D3: singleton anti-obligation when the unassigned fold-in
            # failed; cleared on next tick where fold-in succeeds.
            if fold_in_error is not None:
                set_obligation(
                    conn,
                    obligation_id="shard_unassigned_pending",
                    last_proven_at=now,
                    proven_by="shard_reshuffle_sweep",
                    next_due_at=now,
                    details=json.dumps({"error": fold_in_error}),
                )
            else:
                conn.execute(
                    "DELETE FROM obligation_clocks WHERE obligation_id=?",
                    ("shard_unassigned_pending",),
                )
            self._heartbeat(
                conn, now,
                reshuffled=len(reshuffled),
                folded_in=len(folded_in),
            )
            conn.commit()
            return {"reshuffled": reshuffled, "folded_in": folded_in}
        finally:
            conn.close()

    def _reshuffle_one_shard(self, conn, old_sid: str, now: str) -> list[str]:
        """Reshuffle one overdue shard. Returns the list of new shard_ids
        created (primary + leftovers). Caller wraps in try/except for
        per-shard isolation (U-D3)."""
        new_sids: list[str] = []
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
            return []
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
        new_sids.append(new_sid)
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
            new_sids.append(extra_sid)
        conn.commit()
        _clear_overdue_obligation(conn, old_sid)
        return new_sids

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
