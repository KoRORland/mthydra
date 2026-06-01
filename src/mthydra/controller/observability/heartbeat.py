"""Dead-man's-switch heartbeat publisher — spec J §6 + J-D4.

Periodically dispatches one summary email. Telegram is deliberately NOT
the heartbeat channel (silence on Telegram is confusable with takedown).
After heartbeat_breach_threshold consecutive failures, sets
obs_dead_mans_switch_breach anti-obligation row; the next successful
heartbeat clears it.
"""
from __future__ import annotations

import json
import os
import subprocess
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


def collect_identity(db_path: Path | str) -> dict[str, str]:
    """R-D8: identification fields embedded in every heartbeat. Lets an
    operator reading a heartbeat email tell what version is running on
    which host — silence then carries real information ('which version
    went silent') instead of just 'something is off'."""
    from importlib import metadata
    try:
        version = metadata.version("mthydra")
    except metadata.PackageNotFoundError:
        version = "unknown"
    hostname = os.uname().nodename
    try:
        conn = connect(db_path)
        try:
            row = conn.execute(
                "SELECT version FROM schema_version WHERE rowid=1"
            ).fetchone()
            schema_version = str(row[0]) if row else "unknown"
        finally:
            conn.close()
    except Exception:
        schema_version = "unknown"
    # HEAD SHA is best-effort — packaged installs (no .git) report 'unknown'.
    head_sha = "unknown"
    try:
        res = subprocess.run(
            ["git", "-C", "/opt/mthydra/src", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if res.returncode == 0:
            head_sha = res.stdout.strip()[:12] or "unknown"
    except (OSError, subprocess.SubprocessError):
        pass
    return {
        "version": version,
        "hostname": hostname,
        "schema_version": schema_version,
        "head_sha": head_sha,
    }


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
        identity: dict[str, str] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.email_sink = email_sink
        self.interval_seconds = interval_seconds
        self.breach_threshold = breach_threshold
        self.mode = mode
        self._clock = clock or _default_clock
        self._consecutive_failures = 0
        self._scheduler: BackgroundScheduler | None = None
        # R-D8: identity is fetched once at construction so per-tick heartbeats
        # don't shell out to git on every fire. Tests can inject a fixed dict.
        self._identity = identity if identity is not None else collect_identity(db_path)

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
            ident = self._identity
            subject = (
                f"mthydra heartbeat @ {now} — "
                f"{ident['hostname']} v{ident['version']}"
            )
            body = (
                f"version: {ident['version']}\n"
                f"hostname: {ident['hostname']}\n"
                f"schema: v{ident['schema_version']}\n"
                f"HEAD: {ident['head_sha']}\n"
                f"\n"
                f"{snap.summary_line}"
            )
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
