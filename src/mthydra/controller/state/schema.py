"""SQLite schema for the controller's runtime state."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

SCHEMA_VERSION = 1

_STATEMENTS: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS schema_version (
      version    INTEGER NOT NULL,
      applied_at TEXT    NOT NULL,
      CHECK (rowid = 1)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cover_domain_pool (
      domain                TEXT PRIMARY KEY,
      state                 TEXT NOT NULL CHECK (state IN ('candidate_unverified','candidate_verified','in_use')),
      last_verified_at      TEXT,
      verified_from_vantage TEXT,
      assigned_box_id       TEXT,
      added_at              TEXT NOT NULL,
      notes                 TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS burned_domains (
      domain      TEXT PRIMARY KEY,
      burned_at   TEXT NOT NULL,
      reason      TEXT NOT NULL,
      last_box_id TEXT,
      details     TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS credential_authority (
      generation  INTEGER PRIMARY KEY,
      privkey_pem TEXT NOT NULL,
      pubkey_pem  TEXT NOT NULL,
      created_at  TEXT NOT NULL,
      retired_at  TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS descriptor_signing_key (
      generation  INTEGER PRIMARY KEY,
      privkey     BLOB NOT NULL,
      pubkey      BLOB NOT NULL,
      created_at  TEXT NOT NULL,
      retired_at  TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS descriptor_history (
      generation             INTEGER PRIMARY KEY,
      payload                TEXT NOT NULL,
      signed_at              TEXT NOT NULL,
      valid_until            TEXT NOT NULL,
      signing_key_generation INTEGER NOT NULL,
      FOREIGN KEY (signing_key_generation) REFERENCES descriptor_signing_key(generation)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS shards (
      shard_id           TEXT PRIMARY KEY,
      members_json       TEXT NOT NULL,
      last_reshuffled_at TEXT NOT NULL,
      created_at         TEXT NOT NULL,
      retired_at         TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ru_boxes (
      box_id             TEXT PRIMARY KEY,
      provider           TEXT NOT NULL,
      region             TEXT NOT NULL,
      public_ip          TEXT,
      sni                TEXT UNIQUE NOT NULL,
      shard_id           TEXT,
      state              TEXT NOT NULL CHECK (state IN ('provisioning','live','terminated')),
      image_version      TEXT NOT NULL,
      created_at         TEXT NOT NULL,
      went_live_at       TEXT,
      terminated_at      TEXT,
      termination_reason TEXT,
      FOREIGN KEY (shard_id) REFERENCES shards(shard_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS onward_credentials (
      cred_id              TEXT PRIMARY KEY,
      box_id               TEXT NOT NULL,
      credential           BLOB NOT NULL,
      issued_at            TEXT NOT NULL,
      revoked_at           TEXT,
      authority_generation INTEGER NOT NULL,
      FOREIGN KEY (box_id) REFERENCES ru_boxes(box_id),
      FOREIGN KEY (authority_generation) REFERENCES credential_authority(generation)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS users (
      user_id              TEXT PRIMARY KEY,
      display_name         TEXT,
      out_of_band_channel  TEXT NOT NULL,
      current_shard_id     TEXT,
      added_at             TEXT NOT NULL,
      FOREIGN KEY (current_shard_id) REFERENCES shards(shard_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS published_subsets (
      publish_gen  INTEGER PRIMARY KEY AUTOINCREMENT,
      payload_json TEXT NOT NULL,
      published_at TEXT NOT NULL,
      channel      TEXT NOT NULL
    )
    """,
]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def apply_schema(conn: sqlite3.Connection) -> None:
    """Create tables if missing; insert the schema_version row exactly once."""
    for stmt in _STATEMENTS:
        conn.execute(stmt)
    existing = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
    if existing == 0:
        conn.execute(
            "INSERT INTO schema_version (rowid, version, applied_at) VALUES (1, ?, ?)",
            (SCHEMA_VERSION, _now()),
        )
    conn.commit()
