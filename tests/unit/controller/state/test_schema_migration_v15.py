"""Tests for v14 → v15 schema migration — SSH columns on probe_vantages."""
from __future__ import annotations

import sqlite3

from mthydra.controller.state import schema


def test_v14_to_v15_adds_ssh_columns():
    conn = sqlite3.connect(":memory:")
    schema.apply_schema(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(probe_vantages)")}
    for c in ("ssh_host", "ssh_port", "ssh_user", "ssh_key_path",
              "ssh_known_hosts_path"):
        assert c in cols


def test_v15_migration_is_idempotent():
    conn = sqlite3.connect(":memory:")
    schema.apply_schema(conn)
    schema.migrate_v14_to_v15(conn)
    schema.migrate_v14_to_v15(conn)
    row = conn.execute(
        "SELECT version FROM schema_version WHERE rowid=1"
    ).fetchone()
    assert row[0] == schema.SCHEMA_VERSION
