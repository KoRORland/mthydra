import sqlite3

from mthydra.controller.state.schema import SCHEMA_VERSION, apply_schema, migrate_v1_to_v2


def test_apply_schema_creates_version_row(tmp_db_path):
    conn = sqlite3.connect(tmp_db_path)
    apply_schema(conn)
    row = conn.execute("SELECT version FROM schema_version WHERE rowid = 1").fetchone()
    assert row == (SCHEMA_VERSION,)


def test_apply_schema_is_idempotent(tmp_db_path):
    conn = sqlite3.connect(tmp_db_path)
    apply_schema(conn)
    apply_schema(conn)
    count = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
    assert count == 1


def test_fresh_schema_is_v2(tmp_db_path):
    conn = sqlite3.connect(tmp_db_path)
    apply_schema(conn)
    version = conn.execute("SELECT version FROM schema_version WHERE rowid=1").fetchone()[0]
    assert version == 2


def test_fresh_schema_has_eu_exit_set(tmp_db_path):
    conn = sqlite3.connect(tmp_db_path)
    apply_schema(conn)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "eu_exit_set" in tables


def test_fresh_schema_has_descriptor_history_signature(tmp_db_path):
    conn = sqlite3.connect(tmp_db_path)
    apply_schema(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(descriptor_history)").fetchall()]
    assert "signature" in cols


def test_migrate_v1_to_v2_is_idempotent(tmp_db_path):
    """Calling migrate_v1_to_v2 twice must not raise."""
    conn = sqlite3.connect(tmp_db_path)
    apply_schema(conn)
    migrate_v1_to_v2(conn)  # second time — should be a no-op
    version = conn.execute("SELECT version FROM schema_version WHERE rowid=1").fetchone()[0]
    assert version == 2


def test_migration_from_v1_preserves_data(tmp_db_path):
    """Simulate a v1 DB: apply schema but manually set version=1, then migrate."""
    conn = sqlite3.connect(tmp_db_path)
    apply_schema(conn)
    # Force version back to 1 and drop eu_exit_set to simulate v1
    conn.execute("UPDATE schema_version SET version=1 WHERE rowid=1")
    conn.execute("DROP TABLE IF EXISTS eu_exit_set")
    conn.execute(
        "INSERT INTO descriptor_signing_key (generation, privkey, pubkey, created_at) "
        "VALUES (1, X'aa', X'bb', '2026-05-19T00:00:00Z')"
    )
    conn.commit()
    migrate_v1_to_v2(conn)
    # Data preserved
    row = conn.execute("SELECT generation FROM descriptor_signing_key").fetchone()
    assert row[0] == 1
    # eu_exit_set now exists
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "eu_exit_set" in tables
