"""Startup self-check invariants — spec A §10."""
from __future__ import annotations

import sqlite3


class InvariantViolation(RuntimeError):
    """Raised by check_all when the DB is in a state that forbids startup."""


def _scalar(conn: sqlite3.Connection, sql: str, *params) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


def check_all(conn: sqlite3.Connection, *, expected_schema_version: int) -> None:
    """Run every §10 invariant. Raise InvariantViolation on the first failure."""

    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise InvariantViolation(f"integrity_check: {integrity}")

    row = conn.execute("SELECT version FROM schema_version WHERE rowid=1").fetchone()
    if row is None:
        raise InvariantViolation("schema_version row missing")
    if row[0] != expected_schema_version:
        raise InvariantViolation(
            f"schema_version mismatch: db={row[0]} expected={expected_schema_version}"
        )

    overlap = _scalar(
        conn,
        "SELECT COUNT(*) FROM cover_domain_pool WHERE domain IN (SELECT domain FROM burned_domains)",
    )
    if overlap > 0:
        raise InvariantViolation(
            f"cover_domain_pool / burned_domains overlap: {overlap} row(s)"
        )

    active_authorities = _scalar(
        conn, "SELECT COUNT(*) FROM credential_authority WHERE retired_at IS NULL"
    )
    if active_authorities != 1:
        raise InvariantViolation(
            f"credential_authority must have exactly 1 active row, found {active_authorities}"
        )

    active_signing = _scalar(
        conn, "SELECT COUNT(*) FROM descriptor_signing_key WHERE retired_at IS NULL"
    )
    if active_signing != 1:
        raise InvariantViolation(
            f"descriptor_signing_key must have exactly 1 active row, found {active_signing}"
        )

    impossible = _scalar(
        conn,
        "SELECT COUNT(*) FROM backup_log WHERE pushed_at IS NULL AND index_updated_at IS NOT NULL",
    )
    if impossible > 0:
        raise InvariantViolation(
            f"impossible backup_log state (index without pushed): {impossible} row(s)"
        )
