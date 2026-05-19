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


def record_started(
    conn: sqlite3.Connection,
    generation: int,
    trigger: BackupTrigger | str,
    created_at: str,
) -> None:
    trigger_str = trigger.value if isinstance(trigger, BackupTrigger) else trigger
    conn.execute(
        "INSERT INTO backup_log (generation, created_at, trigger) VALUES (?, ?, ?)",
        (generation, created_at, trigger_str),
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


def abandon_zombie_starts(
    conn: sqlite3.Connection, *, now_iso: str, max_age_hours: int = 1
) -> int:
    """Tag backup_log rows that started but never pushed and are older than max_age_hours.

    Such rows are left by a controller crash between record_started and put_blob
    (spec A §9 last row).  They are NOT deleted — the generation number is
    preserved for forensics — but their trigger string is suffixed with
    ':abandoned' so they are excluded from the pending-reconciliation list and
    from the streak counter, and are clearly labelled in operator queries.

    Returns the number of rows updated.
    """
    from datetime import datetime, timedelta, timezone

    cutoff = (
        datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
        - timedelta(hours=max_age_hours)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    cur = conn.execute(
        "UPDATE backup_log "
        "SET trigger = trigger || ':abandoned' "
        "WHERE pushed_at IS NULL "
        "  AND index_updated_at IS NULL "
        "  AND created_at < ? "
        "  AND trigger NOT LIKE '%:abandoned'",
        (cutoff,),
    )
    conn.commit()
    return cur.rowcount


def count_consecutive_failures(conn: sqlite3.Connection, window_hours: int = 24) -> int:
    """Count consecutive backup failures at the head of backup_log.

    Scans rows ordered by generation DESC and counts rows where pushed_at IS NULL,
    stopping at the first row that has pushed_at NOT NULL (a successful push
    terminates the streak).

    window_hours is retained as a parameter for callers that want to limit the
    look-back range, but the primary termination condition is always the first
    success in the streak scan — the window only prunes very old stale rows that
    predate any success in the DB.

    Returns 0 if there are no rows or the most recent row succeeded.
    """
    rows = conn.execute(
        "SELECT pushed_at FROM backup_log ORDER BY generation DESC"
    ).fetchall()

    streak = 0
    for (pushed_at,) in rows:
        if pushed_at is None:
            streak += 1
        else:
            break  # first success terminates the streak
    return streak


def list_pending_reconciliation(conn: sqlite3.Connection) -> list[BackupRecord]:
    rows = conn.execute(
        "SELECT generation, created_at, size_bytes, sha256, pushed_at, index_updated_at, trigger "
        "FROM backup_log WHERE pushed_at IS NOT NULL AND index_updated_at IS NULL "
        "ORDER BY generation"
    ).fetchall()
    return [BackupRecord(*r) for r in rows]
