"""Audit log — append-only by convention."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class AuditEvent:
    id: int
    ts: str
    actor: str
    action: str
    target: str | None
    details_json: str | None


def log_event(
    conn: sqlite3.Connection,
    ts: str,
    actor: str,
    action: str,
    target: str | None,
    details_json: str | None,
) -> None:
    conn.execute(
        "INSERT INTO audit_log (ts, actor, action, target, details_json) VALUES (?, ?, ?, ?, ?)",
        (ts, actor, action, target, details_json),
    )
    conn.commit()


def recent_events(conn: sqlite3.Connection, limit: int = 100) -> list[AuditEvent]:
    rows = conn.execute(
        "SELECT id, ts, actor, action, target, details_json FROM audit_log ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [AuditEvent(*r) for r in rows]
