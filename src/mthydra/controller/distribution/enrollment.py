"""One-time enrollment tokens for deep-link user onboarding (spec O O-D3).

Operator-issued, single-use, expiring, stored hashed. A token authenticates an
incoming Telegram /start so the controller can bind a chat_id to a user without
open self-service (preserves spec K K-D4).
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta

from mthydra.controller.state import audit


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _add_seconds_iso(iso: str, seconds: int) -> str:
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (t + timedelta(seconds=seconds)).astimezone(
        UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def mint(conn: sqlite3.Connection, user_id: str, *, ttl_seconds: int,
         now: str) -> str:
    """Mint (or reissue) a token for user_id. Returns the plaintext token once."""
    token = secrets.token_urlsafe(16)  # 128 bits entropy; fits Telegram's 64-char start payload
    expires_at = _add_seconds_iso(now, ttl_seconds)
    conn.execute(
        "INSERT INTO pending_enrollments "
        "(user_id, token_hash, created_at, expires_at, consumed_at) "
        "VALUES (?, ?, ?, ?, NULL) "
        "ON CONFLICT(user_id) DO UPDATE SET "
        "token_hash=excluded.token_hash, created_at=excluded.created_at, "
        "expires_at=excluded.expires_at, consumed_at=NULL",
        (user_id, _hash(token), now, expires_at),
    )
    audit.log_event(conn, ts=now, actor="operator", action="enrollment_mint",
                    target=user_id, details_json=None)
    conn.commit()
    return token


def match(conn: sqlite3.Connection, token: str, *, now: str) -> str | None:
    """Return the user_id for a valid, unexpired, unconsumed token; else None.

    On a hit, marks the token consumed (single-use). ISO 'YYYY-MM-DDTHH:MM:SSZ'
    timestamps compare correctly lexicographically.
    """
    row = conn.execute(
        "SELECT user_id FROM pending_enrollments "
        "WHERE token_hash=? AND consumed_at IS NULL AND expires_at > ?",
        (_hash(token), now),
    ).fetchone()
    if row is None:
        return None
    user_id = row[0]
    conn.execute(
        "UPDATE pending_enrollments SET consumed_at=? WHERE user_id=?",
        (now, user_id),
    )
    audit.log_event(conn, ts=now, actor="enroll_poller",
                    action="enrollment_consumed", target=user_id,
                    details_json=None)
    # audit.log_event already committed; explicit commit keeps the contract clear
    conn.commit()
    return user_id


def deep_link(bot_username: str, token: str) -> str:
    return f"https://t.me/{bot_username}?start={token}"
