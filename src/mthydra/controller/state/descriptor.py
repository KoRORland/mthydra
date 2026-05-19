"""Descriptor-signing key + signed-descriptor history (consumed by spec B)."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class SigningKey:
    generation: int
    privkey: bytes
    pubkey: bytes
    created_at: str
    retired_at: str | None


@dataclass(frozen=True)
class Descriptor:
    generation: int
    payload: str
    signed_at: str
    valid_until: str
    signing_key_generation: int


def insert_signing_key(
    conn: sqlite3.Connection, generation: int, privkey: bytes, pubkey: bytes, created_at: str
) -> None:
    conn.execute(
        "INSERT INTO descriptor_signing_key (generation, privkey, pubkey, created_at) VALUES (?, ?, ?, ?)",
        (generation, privkey, pubkey, created_at),
    )
    conn.commit()


def retire_signing_key(conn: sqlite3.Connection, generation: int, *, at: str) -> None:
    conn.execute(
        "UPDATE descriptor_signing_key SET retired_at=? WHERE generation=? AND retired_at IS NULL",
        (at, generation),
    )
    conn.commit()


def current_signing_key(conn: sqlite3.Connection) -> SigningKey:
    row = conn.execute(
        "SELECT generation, privkey, pubkey, created_at, retired_at FROM descriptor_signing_key "
        "WHERE retired_at IS NULL ORDER BY generation DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise LookupError("no active descriptor_signing_key")
    return SigningKey(*row)


def insert_descriptor(
    conn: sqlite3.Connection,
    generation: int,
    payload: str,
    signed_at: str,
    valid_until: str,
    signing_key_generation: int,
    signature: bytes = b"",
) -> None:
    conn.execute(
        "INSERT INTO descriptor_history "
        "(generation, payload, signed_at, valid_until, signing_key_generation, signature) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (generation, payload, signed_at, valid_until, signing_key_generation, signature),
    )
    conn.commit()


def latest_descriptor(conn: sqlite3.Connection) -> Descriptor | None:
    """Return the most recent descriptor, or None if none exist."""
    row = conn.execute(
        "SELECT generation, payload, signed_at, valid_until, signing_key_generation "
        "FROM descriptor_history ORDER BY generation DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return Descriptor(*row)


def latest_descriptor_with_signature(
    conn: sqlite3.Connection,
) -> tuple[int, bytes, bytes] | None:
    """Return (generation, payload_bytes, signature) for the latest descriptor, or None."""
    row = conn.execute(
        "SELECT generation, payload, signature FROM descriptor_history "
        "ORDER BY generation DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    gen, payload_text, sig = row
    return gen, payload_text.encode("utf-8"), bytes(sig)


def next_descriptor_generation(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(generation), 0) FROM descriptor_history"
    ).fetchone()
    return int(row[0]) + 1
