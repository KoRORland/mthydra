"""Append-only distribution log — spec K §5.

Every per-user send attempt records a row here. Schema v10 triggers
refuse UPDATE / DELETE.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


_CHANNELS = {"telegram", "email", "dryrun"}


@dataclass(frozen=True)
class DistributionLogEntry:
    id: int
    user_id: str
    channel: str
    kind: str
    attempted_at: str
    delivered_at: str | None
    subset_hash: str | None
    payload_json: str
    error: str | None


def append(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    channel: str,
    kind: str,
    attempted_at: str,
    delivered_at: str | None,
    subset_hash: str | None,
    payload_json: str,
    error: str | None,
) -> int:
    if channel not in _CHANNELS:
        raise ValueError(f"unknown channel {channel!r}")
    cur = conn.execute(
        "INSERT INTO distribution_log (user_id, channel, kind, attempted_at, "
        "delivered_at, subset_hash, payload_json, error) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, channel, kind, attempted_at, delivered_at,
         subset_hash, payload_json, error),
    )
    conn.commit()
    return int(cur.lastrowid)


def last_subset_hash(
    conn: sqlite3.Connection, user_id: str, channel: str,
) -> str | None:
    """Most recent delivered subset_hash for (user_id, channel). NULL otherwise."""
    r = conn.execute(
        "SELECT subset_hash FROM distribution_log "
        "WHERE user_id=? AND channel=? AND kind='subset_delta' "
        "AND delivered_at IS NOT NULL "
        "ORDER BY id DESC LIMIT 1",
        (user_id, channel),
    ).fetchone()
    return r[0] if r else None


def recent(
    conn: sqlite3.Connection,
    *,
    user_id: str | None = None,
    limit: int = 50,
) -> list[DistributionLogEntry]:
    if user_id is None:
        rows = conn.execute(
            "SELECT id, user_id, channel, kind, attempted_at, delivered_at, "
            "subset_hash, payload_json, error FROM distribution_log "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, user_id, channel, kind, attempted_at, delivered_at, "
            "subset_hash, payload_json, error FROM distribution_log "
            "WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [DistributionLogEntry(*r) for r in rows]
