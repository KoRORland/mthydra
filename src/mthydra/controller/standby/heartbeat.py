"""Standby heartbeat publisher + active-side poller (spec F §5).

The publisher runs on standby nodes and writes a small JSON ping to B2
on a timer. The poller runs on the active node and reads the same key,
stamps eu_nodes.last_heartbeat_at + the eu_standby_liveness_seen
obligation, and emits eu_standby_liveness_stale when staleness alerts.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from mthydra.controller.state.db import connect
from mthydra.controller.state.eu_nodes import list_eu_nodes, update_heartbeat
from mthydra.controller.state.obligations import set_obligation

log = logging.getLogger(__name__)

_HEARTBEAT_SCHEMA = "mthydra.standby_heartbeat.v1"


def _default_clock() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _add_seconds_iso(iso: str, seconds: float) -> str:
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (t + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _controller_version() -> str:
    try:
        from importlib.metadata import version
        return version("mthydra")
    except Exception:
        return "0.0.0"


def _age_seconds(now_iso: str, then_iso: str) -> float:
    now = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
    then = datetime.fromisoformat(then_iso.replace("Z", "+00:00"))
    return (now - then).total_seconds()


class StandbyHeartbeatPublisher:
    """Pushes a heartbeat JSON object to B2 at a regular cadence."""

    def __init__(
        self,
        *,
        node_id: str,
        b2_destination,
        interval_seconds: float,
        mode: str = "production",
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.node_id = node_id
        self.b2 = b2_destination
        self.interval_seconds = interval_seconds
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
            trigger=IntervalTrigger(seconds=self.interval_seconds),
        )
        self._scheduler.start()

    def disarm(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    def run_once(self) -> None:
        payload_dict = {
            "schema": _HEARTBEAT_SCHEMA,
            "node_id": self.node_id,
            "ts": self._clock(),
            "schema_version": 4,
            "controller_version": _controller_version(),
        }
        payload = json.dumps(payload_dict, separators=(",", ":")).encode("utf-8")
        self.b2.put_heartbeat(node_id=self.node_id, payload=payload)


class StandbyHeartbeatPoller:
    """Polls B2 for every active standby node's heartbeat.

    Each non-retired eu_nodes.role='standby' row is checked once per tick.
    Fresh heartbeats prove `eu_standby_liveness_seen::<node_id>` and clear
    `eu_standby_liveness_stale::<node_id>`. Missing or aged-past-staleness
    heartbeats set the stale anti-obligation.
    """

    def __init__(
        self,
        *,
        db_path: Path | str,
        b2_destination,
        poll_interval_seconds: float,
        staleness_alert_seconds: float,
        mode: str = "production",
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.b2 = b2_destination
        self.poll_interval_seconds = poll_interval_seconds
        self.staleness_alert_seconds = staleness_alert_seconds
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
            trigger=IntervalTrigger(seconds=self.poll_interval_seconds),
        )
        self._scheduler.start()

    def disarm(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    def run_once(self) -> list[str]:
        """Poll every non-retired standby. Returns list of stale node_ids."""
        now = self._clock()
        stale: list[str] = []
        conn = connect(self.db_path)
        try:
            standbys = list_eu_nodes(conn, role="standby")
            for n in standbys:
                hb = self.b2.head_heartbeat(node_id=n.node_id)
                if hb is None:
                    self._mark_stale(conn, n.node_id, now,
                                     reason="no heartbeat object in B2")
                    stale.append(n.node_id)
                    continue
                age_seconds = _age_seconds(now, hb["last_modified_iso"])
                if age_seconds > self.staleness_alert_seconds:
                    self._mark_stale(conn, n.node_id, now,
                                     reason=f"age={age_seconds:.0f}s")
                    stale.append(n.node_id)
                    continue
                # Fresh: update heartbeat, prove obligation, clear stale.
                update_heartbeat(conn, n.node_id, at=now, b2_etag=hb["etag"])
                next_due = _add_seconds_iso(now, self.staleness_alert_seconds)
                set_obligation(
                    conn,
                    obligation_id=f"eu_standby_liveness_seen::{n.node_id}",
                    last_proven_at=now,
                    proven_by="heartbeat_poller",
                    next_due_at=next_due,
                    details=json.dumps({"etag": hb["etag"]}),
                )
                conn.execute(
                    "DELETE FROM obligation_clocks WHERE obligation_id=?",
                    (f"eu_standby_liveness_stale::{n.node_id}",),
                )
                conn.commit()
            return stale
        finally:
            conn.close()

    def _mark_stale(self, conn, node_id: str, now: str, *, reason: str) -> None:
        set_obligation(
            conn,
            obligation_id=f"eu_standby_liveness_stale::{node_id}",
            last_proven_at=now,
            proven_by="heartbeat_poller",
            next_due_at=now,
            details=json.dumps({"reason": reason}),
        )
