"""Users + published-subset history. Shards live in `state.shards` (spec H)."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

from mthydra.controller.state import audit


@dataclass(frozen=True)
class User:
    user_id: str
    display_name: str | None
    out_of_band_channel: str
    current_shard_id: str | None
    added_at: str


@dataclass(frozen=True)
class PublishedSubset:
    publish_gen: int
    payload_json: str
    published_at: str
    channel: str


def add_user(
    conn: sqlite3.Connection,
    user_id: str,
    display_name: str | None,
    out_of_band_channel: str,
    at: str,
) -> None:
    conn.execute(
        "INSERT INTO users (user_id, display_name, out_of_band_channel, added_at) VALUES (?, ?, ?, ?)",
        (user_id, display_name, out_of_band_channel, at),
    )
    conn.commit()


def list_users(conn: sqlite3.Connection) -> list[User]:
    rows = conn.execute(
        "SELECT user_id, display_name, out_of_band_channel, current_shard_id, added_at FROM users ORDER BY user_id"
    ).fetchall()
    return [User(*r) for r in rows]


def set_user_shard(conn: sqlite3.Connection, user_id: str, shard_id: str | None) -> None:
    conn.execute("UPDATE users SET current_shard_id=? WHERE user_id=?", (shard_id, user_id))
    conn.commit()


def assign_user_to_shard(
    conn: sqlite3.Connection,
    user_id: str,
    shard_id: str,
    *,
    at: str,
    max_size: int,
) -> None:
    """Add user to shard's members_json and update current_shard_id.

    Refuses if shard is retired (LookupError), missing (LookupError), or at
    `max_size` (ValueError). Idempotent: re-adding an existing member is a
    no-op (no audit row written).
    """
    row = conn.execute(
        "SELECT members_json, retired_at FROM shards WHERE shard_id=?",
        (shard_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"no shard {shard_id!r}")
    if row[1] is not None:
        raise LookupError(f"shard {shard_id!r} is retired")
    members = json.loads(row[0])
    if user_id in members:
        return
    if len(members) >= max_size:
        raise ValueError(f"shard {shard_id!r} at max_size={max_size}")
    members.append(user_id)
    conn.execute(
        "UPDATE shards SET members_json=? WHERE shard_id=?",
        (json.dumps(members), shard_id),
    )
    conn.execute(
        "UPDATE users SET current_shard_id=? WHERE user_id=?",
        (shard_id, user_id),
    )
    audit.log_event(
        conn, ts=at, actor="shard_manager", action="shard_assign_user",
        target=shard_id,
        details_json=json.dumps({"user_id": user_id}),
    )


def unassigned_users(conn: sqlite3.Connection) -> list[str]:
    return [
        r[0] for r in conn.execute(
            "SELECT user_id FROM users WHERE current_shard_id IS NULL ORDER BY user_id"
        ).fetchall()
    ]


def reshuffle_unassigned(
    conn: sqlite3.Connection,
    *,
    now: str,
    target_size: int,
    shard_id_factory: Callable[[], str],
) -> list[str]:
    """Group currently-unassigned users into shards of size <= target_size.

    Creates new shards via `state.shards.create_shard` (kept lazy-imported to
    avoid a circular import; `state.shards` does not depend on this module).
    Returns the list of new shard_ids.
    """
    from mthydra.controller.state import shards as _shards

    unassigned = unassigned_users(conn)
    if not unassigned:
        return []
    new_ids: list[str] = []
    for i in range(0, len(unassigned), target_size):
        chunk = unassigned[i : i + target_size]
        sid = shard_id_factory()
        _shards.create_shard(
            conn, shard_id=sid, members=chunk,
            target_size=target_size, at=now,
        )
        for u in chunk:
            conn.execute(
                "UPDATE users SET current_shard_id=? WHERE user_id=?",
                (sid, u),
            )
        new_ids.append(sid)
    conn.commit()
    return new_ids


def publish_subset(conn: sqlite3.Connection, payload: dict[str, Any], channel: str, at: str) -> int:
    cur = conn.execute(
        "INSERT INTO published_subsets (payload_json, published_at, channel) VALUES (?, ?, ?)",
        (json.dumps(payload), at, channel),
    )
    conn.commit()
    return int(cur.lastrowid)


def latest_published_subset(conn: sqlite3.Connection) -> PublishedSubset:
    row = conn.execute(
        "SELECT publish_gen, payload_json, published_at, channel "
        "FROM published_subsets ORDER BY publish_gen DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise LookupError("no published subsets")
    return PublishedSubset(*row)
