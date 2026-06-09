"""Alert sweep scheduler — spec J §6.

Per tick:
  * collect_snapshot
  * for each anti-obligation + each overdue obligation + each stale EU
    heartbeat: decide severity, build AlertPayload, check dedupe against
    alert_log, dispatch to the per-severity sinks (crit -> both;
    warn -> Telegram; info -> none), append alert_log row per attempt
  * heartbeat obligation 'obs_alerter_sweep_ran' proven each tick
"""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from mthydra.controller.observability.alert_text import (
    human_age,
    kind_title,
    render_details,
    severity_word,
)
from mthydra.controller.observability.remediation import remediation_for
from mthydra.controller.observability.sinks import AlertPayload, DryRunSink
from mthydra.controller.observability.snapshot import (
    Snapshot,
    collect_snapshot,
)
from mthydra.controller.state import alert_acks as _aa
from mthydra.controller.state import alert_log as _al
from mthydra.controller.state.audit import log_event
from mthydra.controller.state.db import connect
from mthydra.controller.state.obligations import set_obligation


def _default_clock() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _add_seconds_iso(iso: str, seconds: float) -> str:
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (t + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(ts: str) -> int:
    return int(
        datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
        .replace(tzinfo=UTC).timestamp()
    )


# Severity → list of sink labels routed to.
_ROUTING: dict[str, tuple[str, ...]] = {
    "crit": ("telegram", "email"),
    "warn": ("telegram",),
    "info": (),
}


class AlertSweep:
    """Periodic alerter; pluggable per-severity sinks."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        telegram_sink: Callable[[AlertPayload], object],
        email_sink: Callable[[AlertPayload], object],
        sweep_interval_seconds: float,
        dedupe_window_seconds: dict[str, int],
        staleness_alert_seconds: int = 600,
        mode: str = "production",
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.telegram_sink = telegram_sink
        self.email_sink = email_sink
        self.sweep_interval_seconds = sweep_interval_seconds
        self.dedupe_window_seconds = dedupe_window_seconds
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
            snap = collect_snapshot(
                conn, now=now,
                staleness_alert_seconds=self.staleness_alert_seconds,
            )
            decisions = _build_decisions(snap, self.staleness_alert_seconds)
            dispatched = 0
            deduped = 0
            acked = 0
            for sev, dedupe_key, kind, target, subject, body in decisions:
                sinks = _ROUTING.get(sev, ())
                if not sinks:
                    continue
                # Spec J2: operator-acked alerts skip dispatch (and skip
                # alert_log too — no attempt was made).
                if _aa.is_acked(conn, dedupe_key, now=now):
                    acked += 1
                    log_event(
                        conn, ts=now, actor="alerter",
                        action="alert_acked",
                        target=dedupe_key, details_json=json.dumps({
                            "severity": sev, "kind": kind,
                        }),
                    )
                    continue
                if self._is_deduped(conn, dedupe_key, sev, now):
                    deduped += 1
                    log_event(
                        conn, ts=now, actor="alerter",
                        action="alert_deduped",
                        target=dedupe_key, details_json=json.dumps({
                            "severity": sev, "kind": kind,
                        }),
                    )
                    continue
                payload = AlertPayload(
                    severity=sev, kind=kind, target=target,
                    dedupe_key=dedupe_key, subject=subject, body=body,
                )
                for sink_label in sinks:
                    sink = self._sink(sink_label)
                    try:
                        res = sink(payload)
                        success = bool(getattr(res, "success", False))
                        err = getattr(res, "error", None)
                    except Exception as e:
                        success = False
                        err = repr(e)
                    _al.append(
                        conn,
                        attempted_at=now,
                        delivered_at=now if success else None,
                        sink=sink_label,
                        severity=sev,
                        kind=kind,
                        target=target,
                        dedupe_key=dedupe_key,
                        payload=f"{subject}\n\n{body}",
                        error=err,
                    )
                    if success:
                        dispatched += 1
                log_event(
                    conn, ts=now, actor="alerter", action="alert_dispatched",
                    target=dedupe_key, details_json=json.dumps({
                        "severity": sev, "kind": kind,
                    }),
                )
            self._heartbeat(conn, now, dispatched, deduped)
            return {"dispatched": dispatched, "deduped": deduped,
                    "acked": acked,
                    "decisions": len(decisions)}
        finally:
            conn.close()

    def _sink(self, label: str):
        if self.mode == "offline":
            return _OFFLINE_SINK
        if label == "telegram":
            return self.telegram_sink
        return self.email_sink

    def _is_deduped(
        self, conn, dedupe_key: str, severity: str, now: str,
    ) -> bool:
        last = _al.last_for_key(conn, dedupe_key)
        if last is None:
            return False
        window = self.dedupe_window_seconds.get(severity, 0)
        if window <= 0:
            return False
        age = _parse_iso(now) - _parse_iso(last.attempted_at)
        return age < window

    def _heartbeat(self, conn, now: str, dispatched: int, deduped: int) -> None:
        next_due = _add_seconds_iso(now, self.sweep_interval_seconds * 2)
        set_obligation(
            conn,
            obligation_id="obs_alerter_sweep_ran",
            last_proven_at=now,
            proven_by="alerter",
            next_due_at=next_due,
            details=json.dumps({
                "dispatched": dispatched, "deduped": deduped,
            }),
        )


_OFFLINE_SINK = DryRunSink(label="offline")


def _subject(severity: str, kind: str, target: str | None) -> str:
    s = f"{severity_word(severity)}: {kind_title(kind)}"
    if target:
        s += f" — {target}"
    return s


def _action(obligation_id: str) -> str:
    """The operator's 'What to do' line for an alert. Falls back to a generic
    pointer rather than leaving the line blank or crashing on unknown ids."""
    return remediation_for(obligation_id) or (
        "no specific action recorded — look this up in the runbook."
    )


def _build_decisions(
    snap: Snapshot, staleness_alert_seconds: int,
) -> list[tuple[str, str, str, str | None, str, str]]:
    """Translate a Snapshot into a flat list of (severity, dedupe_key, kind,
    target, subject, body) tuples ready for dispatch.

    Subjects + bodies are plain language with a "What to do" line (spec J §6
    presentation). The machine identifiers (dedupe_key, kind) are unchanged so
    routing, dedupe, and acks keep working.
    """
    out: list[tuple[str, str, str, str | None, str, str]] = []

    for a in snap.anti_obligations:
        subject = _subject(a.severity, a.kind, a.target)
        lines = [f"Flagged since {a.last_proven_at}."]
        details = render_details(a.details)
        if details:
            lines.append("Details:\n" + details)
        lines.append("\nWhat to do:\n  " + _action(a.obligation_id))
        body = "\n".join(lines)
        out.append((a.severity, a.obligation_id, a.kind, a.target, subject, body))

    for o in snap.obligations_overdue:
        if o.severity == "info":
            continue
        subject = _subject(o.severity, "obligation_overdue", o.obligation_id)
        body = (
            f"The scheduled task '{o.obligation_id}' is overdue.\n"
            f"Last completed: {o.last_proven_at}.\n"
            f"Was due by: {o.next_due_at}.\n"
            f"Overdue by: {human_age(o.overdue_seconds)}.\n"
            f"\nWhat to do:\n  {_action(o.obligation_id)}"
        )
        out.append((o.severity, f"obligation_overdue::{o.obligation_id}",
                     "obligation_overdue", o.obligation_id, subject, body))

    for n in snap.eu_nodes:
        if n.severity == "info":
            continue
        subject = _subject(n.severity, "eu_heartbeat_stale", n.node_id)
        last_seen = n.last_heartbeat_at or "never"
        de_state = n.data_exit_state or "not reported"
        body = (
            f"EU node {n.node_id} (role: {n.role}) hasn't been seen recently.\n"
            f"Last heartbeat: {last_seen}.\n"
            f"Heartbeat age: {human_age(n.heartbeat_age_seconds)}.\n"
            f"Data-exit state: {de_state}.\n"
            f"\nWhat to do:\n  {_action('eu_heartbeat_stale')}"
        )
        out.append((n.severity, f"eu_heartbeat_stale::{n.node_id}",
                     "eu_heartbeat_stale", n.node_id, subject, body))

    return out
