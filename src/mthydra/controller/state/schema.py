"""SQLite schema for the controller's runtime state."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

SCHEMA_VERSION = 2

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
    """
    CREATE TABLE IF NOT EXISTS publishing_tokens (
      kind       TEXT PRIMARY KEY,
      value      TEXT NOT NULL,
      rotated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS provider_api_credentials (
      provider   TEXT PRIMARY KEY,
      credential TEXT NOT NULL,
      rotated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS obligation_clocks (
      obligation_id  TEXT PRIMARY KEY,
      last_proven_at TEXT NOT NULL,
      proven_by      TEXT NOT NULL,
      details        TEXT,
      next_due_at    TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS backup_log (
      generation       INTEGER PRIMARY KEY,
      created_at       TEXT NOT NULL,
      size_bytes       INTEGER NOT NULL DEFAULT 0,
      sha256           TEXT NOT NULL DEFAULT '',
      pushed_at        TEXT,
      index_updated_at TEXT,
      trigger          TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_log (
      id           INTEGER PRIMARY KEY AUTOINCREMENT,
      ts           TEXT NOT NULL,
      actor        TEXT NOT NULL,
      action       TEXT NOT NULL,
      target       TEXT,
      details_json TEXT
    )
    """,
    # --- spec B additions ---
    """
    CREATE TABLE IF NOT EXISTS eu_exit_set (
      fingerprint  TEXT PRIMARY KEY,
      endpoint     TEXT NOT NULL,
      weight       INTEGER NOT NULL DEFAULT 1,
      added_at     TEXT NOT NULL,
      retired_at   TEXT
    )
    """,
]

# Spec B migration statements (applied by migrate_v1_to_v2)
_V2_MIGRATION: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS eu_exit_set (
      fingerprint  TEXT PRIMARY KEY,
      endpoint     TEXT NOT NULL,
      weight       INTEGER NOT NULL DEFAULT 1,
      added_at     TEXT NOT NULL,
      retired_at   TEXT
    )
    """,
    # ALTER TABLE is handled separately (may fail if column already exists)
]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """Idempotent v1 → v2 migration: add eu_exit_set and descriptor_history.signature."""
    for stmt in _V2_MIGRATION:
        conn.execute(stmt)
    # ALTER TABLE fails if column already exists — catch and ignore
    cols = [r[1] for r in conn.execute("PRAGMA table_info(descriptor_history)").fetchall()]
    if "signature" not in cols:
        conn.execute(
            "ALTER TABLE descriptor_history ADD COLUMN signature BLOB NOT NULL DEFAULT X''"
        )
    conn.execute(
        "UPDATE schema_version SET version=?, applied_at=? WHERE rowid=1",
        (2, _now()),
    )
    conn.commit()


def apply_schema(conn: sqlite3.Connection) -> None:
    """Create tables if missing; insert or migrate schema_version row."""
    for stmt in _STATEMENTS:
        conn.execute(stmt)
    # Ensure descriptor_history.signature column exists (spec B)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(descriptor_history)").fetchall()]
    if "signature" not in cols:
        conn.execute(
            "ALTER TABLE descriptor_history ADD COLUMN signature BLOB NOT NULL DEFAULT X''"
        )
    existing = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
    if existing == 0:
        conn.execute(
            "INSERT INTO schema_version (rowid, version, applied_at) VALUES (1, ?, ?)",
            (SCHEMA_VERSION, _now()),
        )
    else:
        current = conn.execute("SELECT version FROM schema_version WHERE rowid=1").fetchone()[0]
        if current < 2:
            migrate_v1_to_v2(conn)
    conn.commit()
