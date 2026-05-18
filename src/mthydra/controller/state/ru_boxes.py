"""RU box inventory repository."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Box:
    box_id: str
    provider: str
    region: str
    public_ip: str | None
    sni: str
    shard_id: str | None
    state: str
    image_version: str
    created_at: str
    went_live_at: str | None
    terminated_at: str | None
    termination_reason: str | None


def insert_box(
    conn: sqlite3.Connection,
    box_id: str,
    provider: str,
    region: str,
    public_ip: str | None,
    sni: str,
    image_version: str,
    created_at: str,
) -> None:
    conn.execute(
        "INSERT INTO ru_boxes (box_id, provider, region, public_ip, sni, state, image_version, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'provisioning', ?, ?)",
        (box_id, provider, region, public_ip, sni, image_version, created_at),
    )
    conn.commit()


def mark_live(conn: sqlite3.Connection, box_id: str, *, public_ip: str, at: str) -> None:
    cur = conn.execute(
        "UPDATE ru_boxes SET state='live', public_ip=?, went_live_at=? "
        "WHERE box_id=? AND state='provisioning'",
        (public_ip, at, box_id),
    )
    if cur.rowcount == 0:
        raise ValueError(f"box {box_id!r} is not in provisioning state")
    conn.commit()


def mark_terminated(conn: sqlite3.Connection, box_id: str, *, reason: str, at: str) -> None:
    cur = conn.execute(
        "UPDATE ru_boxes SET state='terminated', terminated_at=?, termination_reason=? "
        "WHERE box_id=? AND state IN ('provisioning','live')",
        (at, reason, box_id),
    )
    if cur.rowcount == 0:
        raise ValueError(f"box {box_id!r} is not in a terminable state")
    conn.commit()


def list_live(conn: sqlite3.Connection) -> list[Box]:
    rows = conn.execute(
        "SELECT box_id, provider, region, public_ip, sni, shard_id, state, image_version, "
        "created_at, went_live_at, terminated_at, termination_reason "
        "FROM ru_boxes WHERE state='live' ORDER BY box_id"
    ).fetchall()
    return [Box(*r) for r in rows]
