"""Onward-credentials repository (per-box revocable secret)."""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class Credential:
    cred_id: str
    box_id: str
    credential: bytes
    issued_at: str
    revoked_at: str | None
    authority_generation: int


def issue_credential(
    conn: sqlite3.Connection,
    box_id: str,
    credential: bytes,
    issued_at: str,
    authority_generation: int,
) -> str:
    cred_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO onward_credentials (cred_id, box_id, credential, issued_at, authority_generation) "
        "VALUES (?, ?, ?, ?, ?)",
        (cred_id, box_id, credential, issued_at, authority_generation),
    )
    conn.commit()
    return cred_id


def revoke_credential(conn: sqlite3.Connection, cred_id: str, *, at: str) -> None:
    cur = conn.execute(
        "UPDATE onward_credentials SET revoked_at=? WHERE cred_id=? AND revoked_at IS NULL",
        (at, cred_id),
    )
    if cur.rowcount == 0:
        raise ValueError(f"credential {cred_id!r} not active")
    conn.commit()


def active_for_box(conn: sqlite3.Connection, box_id: str) -> list[Credential]:
    rows = conn.execute(
        "SELECT cred_id, box_id, credential, issued_at, revoked_at, authority_generation "
        "FROM onward_credentials WHERE box_id=? AND revoked_at IS NULL",
        (box_id,),
    ).fetchall()
    return [Credential(*r) for r in rows]
