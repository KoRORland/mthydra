"""SQLite schema for the controller's runtime state."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

SCHEMA_VERSION = 1

_STATEMENTS: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS schema_version (
      version    INTEGER NOT NULL,
      applied_at TEXT    NOT NULL,
      CHECK (rowid = 1)
    )
    """,
]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def apply_schema(conn: sqlite3.Connection) -> None:
    """Create tables if missing; insert the schema_version row exactly once."""
    for stmt in _STATEMENTS:
        conn.execute(stmt)
    existing = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
    if existing == 0:
        conn.execute(
            "INSERT INTO schema_version (rowid, version, applied_at) VALUES (1, ?, ?)",
            (SCHEMA_VERSION, _now()),
        )
    conn.commit()
