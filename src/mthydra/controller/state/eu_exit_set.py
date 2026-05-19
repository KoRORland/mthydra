"""EU exit-set repository — consumed by spec B (descriptor signing) and spec F (EU node setup)."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class EUExitRow:
    fingerprint: str
    endpoint: str
    weight: int
    added_at: str
    retired_at: str | None


def add_exit(
    conn: sqlite3.Connection,
    fingerprint: str,
    endpoint: str,
    weight: int,
    added_at: str,
) -> None:
    conn.execute(
        "INSERT INTO eu_exit_set (fingerprint, endpoint, weight, added_at) VALUES (?,?,?,?)",
        (fingerprint, endpoint, weight, added_at),
    )
    conn.commit()


def retire_exit(conn: sqlite3.Connection, fingerprint: str, *, at: str) -> None:
    cur = conn.execute(
        "UPDATE eu_exit_set SET retired_at=? WHERE fingerprint=? AND retired_at IS NULL",
        (at, fingerprint),
    )
    if cur.rowcount == 0:
        raise ValueError(f"fingerprint {fingerprint!r} not found or already retired")
    conn.commit()


def list_active(conn: sqlite3.Connection) -> list[EUExitRow]:
    """Return active (non-retired) exits sorted by fingerprint for deterministic ordering."""
    rows = conn.execute(
        "SELECT fingerprint, endpoint, weight, added_at, retired_at "
        "FROM eu_exit_set WHERE retired_at IS NULL ORDER BY fingerprint"
    ).fetchall()
    return [EUExitRow(*r) for r in rows]


def list_all(conn: sqlite3.Connection) -> list[EUExitRow]:
    rows = conn.execute(
        "SELECT fingerprint, endpoint, weight, added_at, retired_at "
        "FROM eu_exit_set ORDER BY fingerprint"
    ).fetchall()
    return [EUExitRow(*r) for r in rows]
