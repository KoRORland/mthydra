"""Shard repository — spec H. Lifecycle, reshuffle, disjointness audit.

The shard schema (`shards`, `users.current_shard_id`, `ru_boxes.shard_id`)
was laid by spec A. Spec H adds the lifecycle helpers, the reshuffle
transaction, and the read-side helpers spec K will later consume.

Disjointness (one box -> one shard, non-provisioning boxes never change
shard_id) is enforced by SQLite triggers from schema v7; this module
relies on those triggers and does NOT re-check them.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from mthydra.controller.state import audit


@dataclass(frozen=True)
class Shard:
    shard_id: str
    members_json: str
    target_size: int | None
    last_reshuffled_at: str
    created_at: str
    retired_at: str | None


@dataclass(frozen=True)
class ShardHealth:
    total_active: int
    total_retired: int
    oldest_active_age_seconds: int
    overdue_for_reshuffle: list[str]
    unassigned_users: list[str]


def _parse_iso(ts: str) -> int:
    """ISO-8601 'Z' timestamp -> POSIX seconds. Lightweight, no tz lib."""
    from datetime import datetime, timezone

    return int(
        datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
        .replace(tzinfo=timezone.utc)
        .timestamp()
    )


def create_shard(
    conn: sqlite3.Connection,
    *,
    shard_id: str,
    members: list[str],
    target_size: int,
    at: str,
) -> None:
    conn.execute(
        "INSERT INTO shards (shard_id, members_json, target_size, last_reshuffled_at, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (shard_id, json.dumps(members), target_size, at, at),
    )
    audit.log_event(
        conn, ts=at, actor="shard_manager", action="shard_create",
        target=shard_id,
        details_json=json.dumps({"members": members, "target_size": target_size}),
    )


def retire_shard(conn: sqlite3.Connection, shard_id: str, *, at: str) -> None:
    cur = conn.execute(
        "UPDATE shards SET retired_at=? WHERE shard_id=? AND retired_at IS NULL",
        (at, shard_id),
    )
    if cur.rowcount == 0:
        raise LookupError(f"no active shard {shard_id!r}")
    audit.log_event(
        conn, ts=at, actor="shard_manager", action="shard_retire",
        target=shard_id, details_json=None,
    )


def list_active(conn: sqlite3.Connection) -> list[Shard]:
    rows = conn.execute(
        "SELECT shard_id, members_json, target_size, last_reshuffled_at, created_at, retired_at "
        "FROM shards WHERE retired_at IS NULL ORDER BY created_at, shard_id"
    ).fetchall()
    return [Shard(*r) for r in rows]


def list_all(conn: sqlite3.Connection) -> list[Shard]:
    rows = conn.execute(
        "SELECT shard_id, members_json, target_size, last_reshuffled_at, created_at, retired_at "
        "FROM shards ORDER BY created_at, shard_id"
    ).fetchall()
    return [Shard(*r) for r in rows]


def get_shard(conn: sqlite3.Connection, shard_id: str) -> Shard:
    row = conn.execute(
        "SELECT shard_id, members_json, target_size, last_reshuffled_at, created_at, retired_at "
        "FROM shards WHERE shard_id=?",
        (shard_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"no shard {shard_id!r}")
    return Shard(*row)


def list_shard_boxes(
    conn: sqlite3.Connection,
    shard_id: str,
    *,
    include_terminated: bool = False,
) -> list[str]:
    if include_terminated:
        rows = conn.execute(
            "SELECT box_id FROM ru_boxes WHERE shard_id=? ORDER BY box_id",
            (shard_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT box_id FROM ru_boxes "
            "WHERE shard_id=? AND state IN ('provisioning','live') ORDER BY box_id",
            (shard_id,),
        ).fetchall()
    return [r[0] for r in rows]


def assign_box_to_shard(
    conn: sqlite3.Connection,
    *,
    box_id: str,
    shard_id: str,
    at: str,
) -> None:
    """Set ru_boxes.shard_id. Spec H-D2 trigger enforces 'provisioning only'."""
    cur = conn.execute(
        "UPDATE ru_boxes SET shard_id=? WHERE box_id=?",
        (shard_id, box_id),
    )
    if cur.rowcount == 0:
        raise LookupError(f"no box {box_id!r}")
    audit.log_event(
        conn, ts=at, actor="shard_manager", action="shard_assign_box",
        target=shard_id, details_json=json.dumps({"box_id": box_id}),
    )


def reshuffle(
    conn: sqlite3.Connection,
    shard_id: str,
    *,
    now: str,
    target_size: int,
    new_shard_id: str,
    new_members: list[str],
    reason: str,
) -> str:
    """Atomic. Retire old shard, create new, migrate users' current_shard_id.

    Caller is responsible for picking `new_shard_id` (must be fresh; H-D6) and
    `new_members` (output of the picker). Returns `new_shard_id` on success.
    """
    if new_shard_id == shard_id:
        raise ValueError(f"new_shard_id must differ from {shard_id!r}")
    row = conn.execute(
        "SELECT retired_at FROM shards WHERE shard_id=?", (shard_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"no shard {shard_id!r}")
    if row[0] is not None:
        raise LookupError(f"shard {shard_id!r} already retired")
    # Refuse re-using a previous shard_id even if it was retired (H-D6).
    existed = conn.execute(
        "SELECT 1 FROM shards WHERE shard_id=?", (new_shard_id,)
    ).fetchone()
    if existed is not None:
        raise ValueError(f"new_shard_id {new_shard_id!r} already used")

    # Retire old.
    conn.execute("UPDATE shards SET retired_at=? WHERE shard_id=?", (now, shard_id))
    # Create new.
    conn.execute(
        "INSERT INTO shards (shard_id, members_json, target_size, last_reshuffled_at, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (new_shard_id, json.dumps(new_members), target_size, now, now),
    )
    # Remap users that pointed to old shard.
    conn.execute(
        "UPDATE users SET current_shard_id=? WHERE current_shard_id=?",
        (new_shard_id, shard_id),
    )
    # Members may also include users who were unassigned — set them too.
    for user_id in new_members:
        conn.execute(
            "UPDATE users SET current_shard_id=? WHERE user_id=?",
            (new_shard_id, user_id),
        )
    audit.log_event(
        conn, ts=now, actor="shard_manager", action="shard_reshuffle",
        target=new_shard_id,
        details_json=json.dumps({
            "from": shard_id, "to": new_shard_id,
            "new_members": new_members, "reason": reason,
        }),
    )
    return new_shard_id


def health(
    conn: sqlite3.Connection,
    *,
    now: str,
    reshuffle_interval_seconds: int,
) -> ShardHealth:
    now_s = _parse_iso(now)
    active_rows = conn.execute(
        "SELECT shard_id, last_reshuffled_at FROM shards WHERE retired_at IS NULL"
    ).fetchall()
    retired_count = conn.execute(
        "SELECT COUNT(*) FROM shards WHERE retired_at IS NOT NULL"
    ).fetchone()[0]
    overdue: list[str] = []
    oldest_age = 0
    for sid, last in active_rows:
        age = now_s - _parse_iso(last)
        if age > reshuffle_interval_seconds:
            overdue.append(sid)
        if age > oldest_age:
            oldest_age = age
    unassigned = [
        r[0] for r in conn.execute(
            "SELECT user_id FROM users WHERE current_shard_id IS NULL ORDER BY user_id"
        ).fetchall()
    ]
    return ShardHealth(
        total_active=len(active_rows),
        total_retired=int(retired_count),
        oldest_active_age_seconds=oldest_age,
        overdue_for_reshuffle=sorted(overdue),
        unassigned_users=unassigned,
    )
