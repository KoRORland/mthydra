"""Append-only alert log — spec J §3.1.

Every alerter emission attempt (success or failure) appends a row.
Schema v9 triggers refuse UPDATE / DELETE. Read helpers feed obs CLI
and dedupe decisions inside the alerter.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


_SEVERITIES = {"info", "warn", "crit", "heartbeat"}


@dataclass(frozen=True)
class AlertLogEntry:
    id: int
    attempted_at: str
    delivered_at: str | None
    sink: str
    severity: str
    kind: str
    target: str | None
    dedupe_key: str
    payload: str
    error: str | None


def append(
    conn: sqlite3.Connection,
    *,
    attempted_at: str,
    delivered_at: str | None,
    sink: str,
    severity: str,
    kind: str,
    target: str | None,
    dedupe_key: str,
    payload: str,
    error: str | None,
) -> int:
    if severity not in _SEVERITIES:
        raise ValueError(f"unknown severity {severity!r}")
    cur = conn.execute(
        "INSERT INTO alert_log (attempted_at, delivered_at, sink, severity, "
        "kind, target, dedupe_key, payload, error) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (attempted_at, delivered_at, sink, severity, kind,
         target, dedupe_key, payload, error),
    )
    conn.commit()
    return int(cur.lastrowid)


def recent(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    severity: str | None = None,
) -> list[AlertLogEntry]:
    if severity is None:
        rows = conn.execute(
            "SELECT id, attempted_at, delivered_at, sink, severity, kind, target, "
            "dedupe_key, payload, error FROM alert_log "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, attempted_at, delivered_at, sink, severity, kind, target, "
            "dedupe_key, payload, error FROM alert_log "
            "WHERE severity=? ORDER BY id DESC LIMIT ?",
            (severity, limit),
        ).fetchall()
    return [AlertLogEntry(*r) for r in rows]


def last_for_key(
    conn: sqlite3.Connection, dedupe_key: str,
) -> AlertLogEntry | None:
    r = conn.execute(
        "SELECT id, attempted_at, delivered_at, sink, severity, kind, target, "
        "dedupe_key, payload, error FROM alert_log "
        "WHERE dedupe_key=? ORDER BY id DESC LIMIT 1",
        (dedupe_key,),
    ).fetchone()
    return AlertLogEntry(*r) if r else None


def last_successful_heartbeat(
    conn: sqlite3.Connection,
) -> AlertLogEntry | None:
    """Most-recent alert_log row with severity='heartbeat' AND delivered_at IS NOT NULL."""
    r = conn.execute(
        "SELECT id, attempted_at, delivered_at, sink, severity, kind, target, "
        "dedupe_key, payload, error FROM alert_log "
        "WHERE severity='heartbeat' AND delivered_at IS NOT NULL "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return AlertLogEntry(*r) if r else None
