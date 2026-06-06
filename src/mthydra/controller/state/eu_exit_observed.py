"""K3: per-box record of the last live session observed at the EU exit.

One row per box_id, upserted each time the exit's clash_api reports a live
VLESS session for that box. The alerter sweep compares last_seen_at against a
freshness threshold to flag boxes that should be tunnelling but are not.
"""
from __future__ import annotations

import sqlite3


def record_seen(conn: sqlite3.Connection, box_id: str, at: str) -> None:
    """Upsert the box's last-seen timestamp (callers pass 'now')."""
    conn.execute(
        "INSERT INTO eu_exit_observed (box_id, last_seen_at) VALUES (?, ?) "
        "ON CONFLICT(box_id) DO UPDATE SET last_seen_at=excluded.last_seen_at",
        (box_id, at),
    )


def last_seen(conn: sqlite3.Connection, box_id: str) -> str | None:
    row = conn.execute(
        "SELECT last_seen_at FROM eu_exit_observed WHERE box_id=?", (box_id,)
    ).fetchone()
    return row[0] if row else None
