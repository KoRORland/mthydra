"""Burned-domain repository — append-only, monotonic, never deleted from."""
from __future__ import annotations

import json
import sqlite3

from mthydra.controller.state.audit import log_event


def is_burned(conn: sqlite3.Connection, domain: str) -> bool:
    row = conn.execute("SELECT 1 FROM burned_domains WHERE domain=?", (domain,)).fetchone()
    return row is not None


def mark_burned(
    conn: sqlite3.Connection,
    domain: str,
    reason: str,
    last_box_id: str | None,
    at: str,
    details: str | None,
) -> None:
    """Move a domain from cover_domain_pool to burned_domains in a single transaction.

    This is the only path by which a row may be inserted into burned_domains.
    """
    if is_burned(conn, domain):
        raise ValueError(f"domain {domain!r} is already burned")
    try:
        conn.execute("BEGIN")
        cur = conn.execute("DELETE FROM cover_domain_pool WHERE domain=?", (domain,))
        if cur.rowcount == 0:
            raise ValueError(f"domain {domain!r} is not present in cover_domain_pool")
        conn.execute(
            "INSERT INTO burned_domains (domain, burned_at, reason, last_box_id, details) VALUES (?, ?, ?, ?, ?)",
            (domain, at, reason, last_box_id, details),
        )
        # Audit row must commit in the SAME transaction as the burn — otherwise a
        # crash between commit-the-burn and write-the-audit drops the audit trail
        # for the single most security-sensitive cover-pool operation (spec audit
        # 2026-05-30 M11). log_event() ends with conn.commit(), so we DELETE +
        # INSERT-burned + INSERT-audit + commit are all one atomic txn here.
        log_event(
            conn,
            ts=at,
            actor="controller",
            action="cover_burned",
            target=domain,
            details_json=json.dumps({"reason": reason, "last_box_id": last_box_id}),
        )
    except Exception:
        conn.execute("ROLLBACK")
        raise
