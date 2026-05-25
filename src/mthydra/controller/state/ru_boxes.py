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
    *,
    is_canary: bool = False,
) -> None:
    conn.execute(
        "INSERT INTO ru_boxes (box_id, provider, region, public_ip, sni, state, "
        "image_version, created_at, is_canary) "
        "VALUES (?, ?, ?, ?, ?, 'provisioning', ?, ?, ?)",
        (box_id, provider, region, public_ip, sni, image_version,
         created_at, 1 if is_canary else 0),
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


def set_reality_uuid(conn: sqlite3.Connection, box_id: str, uuid: str) -> None:
    """Set ru_boxes.reality_uuid for a box.

    Raises KeyError if the box does not exist. Raises sqlite3.IntegrityError
    if the uuid collides with an existing reality_uuid (idx is UNIQUE WHERE
    reality_uuid IS NOT NULL).
    """
    n = conn.execute(
        "UPDATE ru_boxes SET reality_uuid=? WHERE box_id=?",
        (uuid, box_id),
    ).rowcount
    if n == 0:
        raise KeyError(f"ru_box {box_id!r} not found")
    conn.commit()


def list_live(conn: sqlite3.Connection) -> list[Box]:
    rows = conn.execute(
        "SELECT box_id, provider, region, public_ip, sni, shard_id, state, image_version, "
        "created_at, went_live_at, terminated_at, termination_reason "
        "FROM ru_boxes WHERE state='live' ORDER BY box_id"
    ).fetchall()
    return [Box(*r) for r in rows]


def list_canary_boxes(
    conn: sqlite3.Connection,
    *,
    image_version: str | None = None,
    state_filter: tuple[str, ...] | None = None,
) -> list[str]:
    """Return box_ids of canary boxes, optionally filtered by image_version + state."""
    sql = "SELECT box_id FROM ru_boxes WHERE is_canary=1"
    params: list[object] = []
    if image_version is not None:
        sql += " AND image_version=?"
        params.append(image_version)
    if state_filter is not None:
        placeholders = ",".join("?" * len(state_filter))
        sql += f" AND state IN ({placeholders})"
        params.extend(state_filter)
    sql += " ORDER BY box_id"
    return [r[0] for r in conn.execute(sql, params).fetchall()]


def clear_canary_flag(
    conn: sqlite3.Connection,
    box_id: str,
    *,
    at: str,
    reason: str,
) -> None:
    """Demote a canary box to regular fleet. Writes an audit row."""
    import json as _json
    from mthydra.controller.state import audit
    row = conn.execute(
        "SELECT is_canary FROM ru_boxes WHERE box_id=?", (box_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"no box {box_id!r}")
    if row[0] == 0:
        raise ValueError(f"box {box_id!r} is not a canary")
    conn.execute(
        "UPDATE ru_boxes SET is_canary=0 WHERE box_id=?",
        (box_id,),
    )
    audit.log_event(
        conn, ts=at, actor="operator", action="ru_box_canary_clear",
        target=box_id, details_json=_json.dumps({"reason": reason}),
    )
