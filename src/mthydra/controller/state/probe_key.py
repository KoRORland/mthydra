"""controller_probe_key accessors — the one shared probe SSH keypair (spec T2).

Single-row table (CHECK id=1). The DB is the source of truth; the file at
/var/lib/mthydra/ssh/probe.key is a regenerable cache (see probe_runner.key).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class ProbeKey:
    private_key: str
    public_key: str
    created_at: str
    comment: str | None


def get(conn: sqlite3.Connection) -> ProbeKey | None:
    r = conn.execute(
        "SELECT private_key, public_key, created_at, comment "
        "FROM controller_probe_key WHERE id=1"
    ).fetchone()
    return ProbeKey(*r) if r else None


def put(conn: sqlite3.Connection, *, private_key: str, public_key: str,
        comment: str | None, at: str) -> None:
    conn.execute(
        "INSERT INTO controller_probe_key (id, private_key, public_key, created_at, comment) "
        "VALUES (1, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET private_key=excluded.private_key, "
        "public_key=excluded.public_key, created_at=excluded.created_at, "
        "comment=excluded.comment",
        (private_key, public_key, at, comment),
    )
    conn.commit()
