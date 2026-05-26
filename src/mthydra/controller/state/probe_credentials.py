"""Per-(box, vantage) probe credentials — spec I2.

Mirrors `onward_credentials` but lives separately so user credentials
and probe credentials are statically discriminable. UNIQUE-on-active
index enforces "one credential at a time per (box, vantage, authority)".
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from mthydra.controller.state import audit


@dataclass(frozen=True)
class ProbeCredential:
    cred_id: str
    box_id: str
    vantage_id: str
    credential: bytes
    issued_at: str
    revoked_at: str | None
    authority_generation: int


def issue(
    conn: sqlite3.Connection,
    *,
    cred_id: str,
    box_id: str,
    vantage_id: str,
    authority_generation: int,
    credential: bytes,
    issued_at: str,
    actor: str = "operator",
    evidence: str | None = None,
) -> str:
    conn.execute(
        "INSERT INTO probe_credentials (cred_id, box_id, vantage_id, credential, "
        "issued_at, authority_generation) VALUES (?, ?, ?, ?, ?, ?)",
        (cred_id, box_id, vantage_id, credential, issued_at, authority_generation),
    )
    audit.log_event(
        conn, ts=issued_at, actor=actor, action="probe_credential_issue",
        target=cred_id,
        details_json=json.dumps({
            "box_id": box_id, "vantage_id": vantage_id,
            "authority_generation": authority_generation,
            "evidence": evidence,
        }),
    )
    conn.commit()
    return cred_id


def revoke(
    conn: sqlite3.Connection,
    cred_id: str,
    *,
    at: str,
    reason: str,
    actor: str = "operator",
) -> None:
    cur = conn.execute(
        "UPDATE probe_credentials SET revoked_at=? "
        "WHERE cred_id=? AND revoked_at IS NULL",
        (at, cred_id),
    )
    if cur.rowcount == 0:
        # Either missing or already revoked.
        present = conn.execute(
            "SELECT revoked_at FROM probe_credentials WHERE cred_id=?",
            (cred_id,),
        ).fetchone()
        if present is None:
            raise LookupError(f"no probe credential {cred_id!r}")
        raise ValueError(f"probe credential {cred_id!r} already revoked")
    audit.log_event(
        conn, ts=at, actor=actor, action="probe_credential_revoke",
        target=cred_id,
        details_json=json.dumps({"reason": reason}),
    )
    conn.commit()


def list_active_for_box(
    conn: sqlite3.Connection, box_id: str,
) -> list[ProbeCredential]:
    rows = conn.execute(
        "SELECT cred_id, box_id, vantage_id, credential, issued_at, revoked_at, "
        "authority_generation FROM probe_credentials "
        "WHERE box_id=? AND revoked_at IS NULL ORDER BY issued_at DESC",
        (box_id,),
    ).fetchall()
    return [ProbeCredential(*r) for r in rows]


def list_active_for_vantage(
    conn: sqlite3.Connection, vantage_id: str,
) -> list[ProbeCredential]:
    rows = conn.execute(
        "SELECT cred_id, box_id, vantage_id, credential, issued_at, revoked_at, "
        "authority_generation FROM probe_credentials "
        "WHERE vantage_id=? AND revoked_at IS NULL ORDER BY issued_at DESC",
        (vantage_id,),
    ).fetchall()
    return [ProbeCredential(*r) for r in rows]


def list_all(
    conn: sqlite3.Connection,
    *,
    box_id: str | None = None,
    vantage_id: str | None = None,
    include_revoked: bool = False,
) -> list[ProbeCredential]:
    sql = (
        "SELECT cred_id, box_id, vantage_id, credential, issued_at, revoked_at, "
        "authority_generation FROM probe_credentials WHERE 1=1"
    )
    params: list[object] = []
    if box_id is not None:
        sql += " AND box_id=?"
        params.append(box_id)
    if vantage_id is not None:
        sql += " AND vantage_id=?"
        params.append(vantage_id)
    if not include_revoked:
        sql += " AND revoked_at IS NULL"
    sql += " ORDER BY issued_at DESC, cred_id"
    rows = conn.execute(sql, params).fetchall()
    return [ProbeCredential(*r) for r in rows]
