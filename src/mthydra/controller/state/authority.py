"""Credential-authority key material repository."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Authority:
    generation: int
    privkey_pem: str
    pubkey_pem: str
    created_at: str
    retired_at: str | None


def insert_authority(
    conn: sqlite3.Connection, generation: int, privkey_pem: str, pubkey_pem: str, created_at: str
) -> None:
    conn.execute(
        "INSERT INTO credential_authority (generation, privkey_pem, pubkey_pem, created_at) VALUES (?, ?, ?, ?)",
        (generation, privkey_pem, pubkey_pem, created_at),
    )
    conn.commit()


def retire_authority(conn: sqlite3.Connection, generation: int, *, at: str) -> None:
    conn.execute(
        "UPDATE credential_authority SET retired_at=? WHERE generation=? AND retired_at IS NULL",
        (at, generation),
    )
    conn.commit()


def current_authority(conn: sqlite3.Connection) -> Authority:
    row = conn.execute(
        "SELECT generation, privkey_pem, pubkey_pem, created_at, retired_at "
        "FROM credential_authority WHERE retired_at IS NULL "
        "ORDER BY generation DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise LookupError("no active credential_authority")
    return Authority(*row)


def list_authorities(conn: sqlite3.Connection) -> list[Authority]:
    rows = conn.execute(
        "SELECT generation, privkey_pem, pubkey_pem, created_at, retired_at "
        "FROM credential_authority ORDER BY generation"
    ).fetchall()
    return [Authority(*r) for r in rows]
