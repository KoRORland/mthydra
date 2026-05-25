"""User distribution channel registry — spec K §5.

Per-user contact points for the distribution publisher: Telegram chat_id
and/or email address. Operator-managed via CLI; every upsert writes
audit_log. At least one of (telegram_chat_id, email_addr) must be
present at insert time.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from mthydra.controller.state import audit


@dataclass(frozen=True)
class UserChannelRow:
    user_id: str
    telegram_chat_id: str | None
    email_addr: str | None
    registered_at: str
    updated_at: str


def set_channels(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    telegram_chat_id: str | None,
    email_addr: str | None,
    at: str,
) -> None:
    if not telegram_chat_id and not email_addr:
        raise ValueError(
            "user-channels: at least one of telegram_chat_id or email_addr required"
        )
    existing = conn.execute(
        "SELECT registered_at FROM user_channels WHERE user_id=?", (user_id,)
    ).fetchone()
    if existing is None:
        conn.execute(
            "INSERT INTO user_channels (user_id, telegram_chat_id, email_addr, "
            "registered_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, telegram_chat_id, email_addr, at, at),
        )
        action = "user_channels_register"
    else:
        conn.execute(
            "UPDATE user_channels SET telegram_chat_id=?, email_addr=?, updated_at=? "
            "WHERE user_id=?",
            (telegram_chat_id, email_addr, at, user_id),
        )
        action = "user_channels_update"
    audit.log_event(
        conn, ts=at, actor="operator", action=action, target=user_id,
        details_json=json.dumps({
            "telegram_chat_id": telegram_chat_id,
            "email_addr": email_addr,
        }),
    )


def get_channels(conn: sqlite3.Connection, user_id: str) -> UserChannelRow | None:
    r = conn.execute(
        "SELECT user_id, telegram_chat_id, email_addr, registered_at, updated_at "
        "FROM user_channels WHERE user_id=?",
        (user_id,),
    ).fetchone()
    return UserChannelRow(*r) if r else None


def list_channels(conn: sqlite3.Connection) -> list[UserChannelRow]:
    rows = conn.execute(
        "SELECT user_id, telegram_chat_id, email_addr, registered_at, updated_at "
        "FROM user_channels ORDER BY user_id"
    ).fetchall()
    return [UserChannelRow(*r) for r in rows]
