"""Publishing tokens and provider API credentials. Plaintext-on-disk per spec D1."""
from __future__ import annotations

import sqlite3


def set_publishing_token(conn: sqlite3.Connection, kind: str, value: str, at: str) -> None:
    conn.execute(
        "INSERT INTO publishing_tokens (kind, value, rotated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(kind) DO UPDATE SET value=excluded.value, rotated_at=excluded.rotated_at",
        (kind, value, at),
    )
    conn.commit()


def get_publishing_token(conn: sqlite3.Connection, kind: str) -> str:
    row = conn.execute("SELECT value FROM publishing_tokens WHERE kind=?", (kind,)).fetchone()
    if row is None:
        raise LookupError(f"no publishing token of kind {kind!r}")
    return row[0]


def set_provider_credential(conn: sqlite3.Connection, provider: str, credential: str, at: str) -> None:
    conn.execute(
        "INSERT INTO provider_api_credentials (provider, credential, rotated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(provider) DO UPDATE SET credential=excluded.credential, rotated_at=excluded.rotated_at",
        (provider, credential, at),
    )
    conn.commit()


def get_provider_credential(conn: sqlite3.Connection, provider: str) -> str:
    row = conn.execute(
        "SELECT credential FROM provider_api_credentials WHERE provider=?", (provider,)
    ).fetchone()
    if row is None:
        raise LookupError(f"no credential for provider {provider!r}")
    return row[0]
