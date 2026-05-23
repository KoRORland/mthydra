"""EU exit-set repository — consumed by spec B (descriptor signing) and spec F (EU node setup).

Spec E Task 5 adds per-exit cover_sni and reality_pubkey columns
(propagated into v2 descriptor payloads).
"""
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
    cover_sni: str | None = None
    reality_pubkey: str | None = None


def add_exit(
    conn: sqlite3.Connection,
    fingerprint: str,
    endpoint: str,
    weight: int,
    added_at: str,
    *,
    cover_sni: str | None = None,
    reality_pubkey: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO eu_exit_set "
        "(fingerprint, endpoint, weight, added_at, cover_sni, reality_pubkey) "
        "VALUES (?,?,?,?,?,?)",
        (fingerprint, endpoint, weight, added_at, cover_sni, reality_pubkey),
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
        "SELECT fingerprint, endpoint, weight, added_at, retired_at, "
        "cover_sni, reality_pubkey "
        "FROM eu_exit_set WHERE retired_at IS NULL ORDER BY fingerprint"
    ).fetchall()
    return [EUExitRow(*r) for r in rows]


def list_all(conn: sqlite3.Connection) -> list[EUExitRow]:
    rows = conn.execute(
        "SELECT fingerprint, endpoint, weight, added_at, retired_at, "
        "cover_sni, reality_pubkey "
        "FROM eu_exit_set ORDER BY fingerprint"
    ).fetchall()
    return [EUExitRow(*r) for r in rows]
