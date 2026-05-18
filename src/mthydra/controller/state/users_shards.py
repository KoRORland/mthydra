"""Users, shards, and published-subset history."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class User:
    user_id: str
    display_name: str | None
    out_of_band_channel: str
    current_shard_id: str | None
    added_at: str


@dataclass(frozen=True)
class Shard:
    shard_id: str
    members_json: str
    last_reshuffled_at: str
    created_at: str
    retired_at: str | None


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


def create_shard(conn: sqlite3.Connection, shard_id: str, members: list[str], at: str) -> None:
    conn.execute(
        "INSERT INTO shards (shard_id, members_json, last_reshuffled_at, created_at) VALUES (?, ?, ?, ?)",
        (shard_id, json.dumps(members), at, at),
    )
    conn.commit()


def set_user_shard(conn: sqlite3.Connection, user_id: str, shard_id: str | None) -> None:
    conn.execute("UPDATE users SET current_shard_id=? WHERE user_id=?", (shard_id, user_id))
    conn.commit()


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
