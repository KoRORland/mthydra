"""Backup-log repository — records each generation's lifecycle."""
from __future__ import annotations

import enum
import sqlite3
from dataclasses import dataclass


class BackupTrigger(str, enum.Enum):
    FLOOR_TIMER = "floor_timer"
    BURNED_DOMAINS_CHANGE = "burned_domains_change"
    MANUAL = "manual"
    BOOTSTRAP = "bootstrap"


@dataclass(frozen=True)
class BackupRecord:
    generation: int
    created_at: str
    size_bytes: int
    sha256: str
    pushed_at: str | None
    index_updated_at: str | None
    trigger: str


def next_generation(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(generation), 0) FROM backup_log").fetchone()
    return int(row[0]) + 1


def record_started(conn: sqlite3.Connection, generation: int, trigger: BackupTrigger, created_at: str) -> None:
    conn.execute(
        "INSERT INTO backup_log (generation, created_at, trigger) VALUES (?, ?, ?)",
        (generation, created_at, trigger.value),
    )
    conn.commit()


def record_pushed(conn: sqlite3.Connection, generation: int, sha256: str, size_bytes: int, pushed_at: str) -> None:
    conn.execute(
        "UPDATE backup_log SET sha256=?, size_bytes=?, pushed_at=? WHERE generation=?",
        (sha256, size_bytes, pushed_at, generation),
    )
    conn.commit()


def record_index_updated(conn: sqlite3.Connection, generation: int, at: str) -> None:
    conn.execute("UPDATE backup_log SET index_updated_at=? WHERE generation=?", (at, generation))
    conn.commit()


def list_pending_reconciliation(conn: sqlite3.Connection) -> list[BackupRecord]:
    rows = conn.execute(
        "SELECT generation, created_at, size_bytes, sha256, pushed_at, index_updated_at, trigger "
        "FROM backup_log WHERE pushed_at IS NOT NULL AND index_updated_at IS NULL "
        "ORDER BY generation"
    ).fetchall()
    return [BackupRecord(*r) for r in rows]
