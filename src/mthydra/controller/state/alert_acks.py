"""Operator alert acknowledgments — spec J2.

Append-only table; while an ack is active (now < expires_at), the alerter
skips dispatch for the matching dedupe_key.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from mthydra.controller.state import audit


@dataclass(frozen=True)
class AlertAck:
    id: int
    dedupe_key: str
    acked_at: str
    acked_by: str
    expires_at: str
    evidence: str


def ack(
    conn: sqlite3.Connection,
    *,
    dedupe_key: str,
    acked_by: str,
    evidence: str,
    at: str,
    expires_at: str,
) -> int:
    if not evidence:
        raise ValueError("evidence must be non-empty")
    cur = conn.execute(
        "INSERT INTO alert_acks (dedupe_key, acked_at, acked_by, expires_at, evidence) "
        "VALUES (?, ?, ?, ?, ?)",
        (dedupe_key, at, acked_by, expires_at, evidence),
    )
    audit.log_event(
        conn, ts=at, actor=acked_by, action="alert_ack",
        target=dedupe_key,
        details_json=json.dumps({
            "expires_at": expires_at, "evidence": evidence,
        }),
    )
    conn.commit()
    return int(cur.lastrowid)


def is_acked(conn: sqlite3.Connection, dedupe_key: str, *, now: str) -> bool:
    """True iff there is an alert_acks row for this key with expires_at > now."""
    row = conn.execute(
        "SELECT 1 FROM alert_acks WHERE dedupe_key=? AND expires_at > ? LIMIT 1",
        (dedupe_key, now),
    ).fetchone()
    return row is not None


def list_active(conn: sqlite3.Connection, *, now: str) -> list[AlertAck]:
    rows = conn.execute(
        "SELECT id, dedupe_key, acked_at, acked_by, expires_at, evidence "
        "FROM alert_acks WHERE expires_at > ? ORDER BY id DESC",
        (now,),
    ).fetchall()
    return [AlertAck(*r) for r in rows]


def list_all(conn: sqlite3.Connection, *, limit: int = 50) -> list[AlertAck]:
    rows = conn.execute(
        "SELECT id, dedupe_key, acked_at, acked_by, expires_at, evidence "
        "FROM alert_acks ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [AlertAck(*r) for r in rows]
