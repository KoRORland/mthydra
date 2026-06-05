"""Tests for v16 → v17 schema migration — controller_probe_key table."""
from __future__ import annotations

import sqlite3

from mthydra.controller.state import schema


def test_fresh_schema_has_controller_probe_key():
    conn = sqlite3.connect(":memory:")
    schema.apply_schema(conn)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "controller_probe_key" in tables
    assert schema.SCHEMA_VERSION == 17


def test_single_row_check_rejects_second_row():
    conn = sqlite3.connect(":memory:")
    schema.apply_schema(conn)
    conn.execute(
        "INSERT INTO controller_probe_key (id, private_key, public_key, created_at)"
        " VALUES (1, 'priv', 'pub', '2026-06-05T00:00:00Z')")
    try:
        conn.execute(
            "INSERT INTO controller_probe_key (id, private_key, public_key, created_at)"
            " VALUES (2, 'priv2', 'pub2', '2026-06-05T00:00:00Z')")
        raised = False
    except sqlite3.IntegrityError:
        raised = True
    assert raised


def test_v16_to_v17_migrates_existing_db():
    conn = sqlite3.connect(":memory:")
    schema.apply_schema(conn)
    # Simulate a v16 DB: drop the table and rewind the version.
    conn.execute("DROP TABLE controller_probe_key")
    conn.execute("UPDATE schema_version SET version=16 WHERE rowid=1")
    schema.migrate_v16_to_v17(conn)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "controller_probe_key" in tables
    assert conn.execute(
        "SELECT version FROM schema_version WHERE rowid=1").fetchone()[0] == 17


def test_v17_migration_is_idempotent():
    conn = sqlite3.connect(":memory:")
    schema.apply_schema(conn)
    schema.migrate_v16_to_v17(conn)
    schema.migrate_v16_to_v17(conn)
    assert conn.execute(
        "SELECT version FROM schema_version WHERE rowid=1").fetchone()[0] >= 17
