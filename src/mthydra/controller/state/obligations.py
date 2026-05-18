"""§12 obligation clocks repository."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Obligation:
    obligation_id: str
    last_proven_at: str
    proven_by: str
    details: str | None
    next_due_at: str


def set_obligation(
    conn: sqlite3.Connection,
    obligation_id: str,
    last_proven_at: str,
    proven_by: str,
    next_due_at: str,
    details: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO obligation_clocks (obligation_id, last_proven_at, proven_by, details, next_due_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(obligation_id) DO UPDATE SET "
        "  last_proven_at=excluded.last_proven_at, "
        "  proven_by=excluded.proven_by, "
        "  details=excluded.details, "
        "  next_due_at=excluded.next_due_at",
        (obligation_id, last_proven_at, proven_by, details, next_due_at),
    )
    conn.commit()


def prove(
    conn: sqlite3.Connection,
    obligation_id: str,
    proven_by: str,
    at: str,
    next_due_at: str,
    details: str | None,
) -> None:
    cur = conn.execute(
        "UPDATE obligation_clocks SET last_proven_at=?, proven_by=?, details=?, next_due_at=? WHERE obligation_id=?",
        (at, proven_by, details, next_due_at, obligation_id),
    )
    if cur.rowcount == 0:
        raise KeyError(f"unknown obligation {obligation_id!r}")
    conn.commit()


def list_obligations(conn: sqlite3.Connection) -> list[Obligation]:
    rows = conn.execute(
        "SELECT obligation_id, last_proven_at, proven_by, details, next_due_at "
        "FROM obligation_clocks ORDER BY obligation_id"
    ).fetchall()
    return [Obligation(*r) for r in rows]
