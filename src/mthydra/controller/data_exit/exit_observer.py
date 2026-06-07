"""K3: EuExitObserver — corroborate RU->EU connectivity from the EU exit side.

Runs on the ACTIVE EU node (co-located with the controller and the exit's
sing-box). Per tick: poll the localhost clash_api for live box sessions, record
last-seen per box, and raise/clear the box_eu_tunnel_unseen anti-obligation for
live boxes that have not been seen within the freshness threshold.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from mthydra import debuglog
from mthydra.controller.data_exit.session_reader import poll_active_sessions
from mthydra.controller.state import eu_exit_observed as _obs
from mthydra.controller.state.audit import log_event
from mthydra.controller.state.db import connect
from mthydra.controller.state.obligations import set_obligation


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _age_seconds(now: str, then: str) -> float:
    a = datetime.fromisoformat(now.replace("Z", "+00:00"))
    b = datetime.fromisoformat(then.replace("Z", "+00:00"))
    return (a - b).total_seconds()


class EuExitObserver:
    POLL_INTERVAL_SECONDS = 5 * 60
    DEFAULT_UNSEEN_THRESHOLD_SECONDS = 45 * 60  # ~3x the box self-check cadence

    def __init__(
        self,
        *,
        db_path: Path | str,
        clash_api_url: str,
        poll_fn: Callable[..., set[str]] | None = None,
        clock: Callable[[], str] | None = None,
        unseen_threshold_seconds: int | None = None,
        mode: str = "online",
    ) -> None:
        self._db_path = Path(db_path)
        self._clash_api_url = clash_api_url
        self._poll_fn = poll_fn or poll_active_sessions
        self._clock = clock or _now_iso
        self._threshold = (
            unseen_threshold_seconds
            if unseen_threshold_seconds is not None
            else self.DEFAULT_UNSEEN_THRESHOLD_SECONDS
        )
        self._mode = mode
        self._scheduler: BackgroundScheduler | None = None

    def arm(self) -> None:
        if self._mode == "offline":
            return
        self._scheduler = BackgroundScheduler(daemon=True)
        self._scheduler.add_job(
            self.tick, trigger=IntervalTrigger(seconds=self.POLL_INTERVAL_SECONDS))
        self._scheduler.start()

    def disarm(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    def tick(self) -> None:
        now = self._clock()
        # (1) Poll. An unreadable API is 'no observations this tick' — never an
        # excuse to flag every box (K3 §6).
        try:
            seen = self._poll_fn(self._clash_api_url, timeout=5.0)
        except Exception:
            seen = set()
        debuglog.log("conn", "observed live sessions", now=now,
                     count=len(seen), boxes=",".join(sorted(seen)))
        conn = connect(self._db_path)
        try:
            for box_id in seen:
                _obs.record_seen(conn, box_id, now)
            # (2) Sweep live boxes.
            live = [
                r[0] for r in conn.execute(
                    "SELECT box_id FROM ru_boxes WHERE state='live' "
                    "AND reality_uuid IS NOT NULL ORDER BY box_id"
                ).fetchall()
            ]
            for box_id in live:
                last = _obs.last_seen(conn, box_id)
                stale = last is None or _age_seconds(now, last) > self._threshold
                oid = f"box_eu_tunnel_unseen::{box_id}"
                if stale:
                    set_obligation(
                        conn, obligation_id=oid, last_proven_at=now,
                        proven_by="eu_exit_observer", next_due_at=now,
                        details=json.dumps(
                            {"box_id": box_id, "last_seen_at": last}),
                    )
                    log_event(
                        conn, ts=now, actor="eu_exit_observer",
                        action="box_eu_tunnel_unseen", target=box_id,
                        details_json=None)
                else:
                    conn.execute(
                        "DELETE FROM obligation_clocks WHERE obligation_id=?",
                        (oid,))
            conn.commit()
        finally:
            conn.close()
