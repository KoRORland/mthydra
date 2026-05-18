"""Cover-domain pool repository (consumed in detail by spec C)."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class CoverDomain:
    domain: str
    state: str
    last_verified_at: str | None
    verified_from_vantage: str | None
    assigned_box_id: str | None
    added_at: str
    notes: str | None


def add_candidate(conn: sqlite3.Connection, domain: str, *, added_at: str, notes: str | None = None) -> None:
    conn.execute(
        "INSERT INTO cover_domain_pool (domain, state, added_at, notes) VALUES (?, 'candidate_unverified', ?, ?)",
        (domain, added_at, notes),
    )
    conn.commit()


def mark_verified(conn: sqlite3.Connection, domain: str, *, from_vantage: str, at: str) -> None:
    cur = conn.execute(
        "UPDATE cover_domain_pool SET state='candidate_verified', verified_from_vantage=?, last_verified_at=? "
        "WHERE domain=? AND state='candidate_unverified'",
        (from_vantage, at, domain),
    )
    if cur.rowcount == 0:
        raise ValueError(f"domain {domain!r} is not in candidate_unverified state")
    conn.commit()


def move_to_in_use(conn: sqlite3.Connection, domain: str, *, box_id: str) -> None:
    cur = conn.execute(
        "UPDATE cover_domain_pool SET state='in_use', assigned_box_id=? "
        "WHERE domain=? AND state='candidate_verified'",
        (box_id, domain),
    )
    if cur.rowcount == 0:
        raise ValueError(f"domain {domain!r} is not in candidate_verified state")
    conn.commit()


def list_by_state(conn: sqlite3.Connection, state: str) -> list[CoverDomain]:
    rows = conn.execute(
        "SELECT domain, state, last_verified_at, verified_from_vantage, assigned_box_id, added_at, notes "
        "FROM cover_domain_pool WHERE state=? ORDER BY domain",
        (state,),
    ).fetchall()
    return [CoverDomain(*r) for r in rows]
