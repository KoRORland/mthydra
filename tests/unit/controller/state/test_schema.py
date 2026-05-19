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


def test_fresh_schema_is_v3(tmp_db_path):
    conn = sqlite3.connect(tmp_db_path)
    apply_schema(conn)
    version = conn.execute("SELECT version FROM schema_version WHERE rowid=1").fetchone()[0]
    assert version == 3


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


def test_schema_version_is_3(tmp_db_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import SCHEMA_VERSION, apply_schema
    assert SCHEMA_VERSION == 3
    conn = connect(tmp_db_path)
    apply_schema(conn)
    row = conn.execute("SELECT version FROM schema_version WHERE rowid=1").fetchone()
    assert row[0] == 3


def test_cover_pool_has_entered_in_use_at(tmp_db_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    conn = connect(tmp_db_path)
    apply_schema(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(cover_domain_pool)").fetchall()]
    assert "entered_in_use_at" in cols


def test_triggers_present(tmp_db_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    conn = connect(tmp_db_path)
    apply_schema(conn)
    triggers = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
    }
    assert "cover_pool_reject_burned" in triggers
    assert "burned_domains_no_delete" in triggers


def test_v2_to_v3_migration_adds_column_and_triggers(tmp_db_path):
    import sqlite3
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema, migrate_v2_to_v3
    # Manually construct a v2 DB (no entered_in_use_at, no triggers)
    conn = connect(tmp_db_path)
    conn.executescript(
        "CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL, CHECK (rowid=1));"
        "INSERT INTO schema_version (rowid, version, applied_at) VALUES (1, 2, '2026-05-19T00:00:00Z');"
        "CREATE TABLE cover_domain_pool ("
        "  domain TEXT PRIMARY KEY, state TEXT NOT NULL, last_verified_at TEXT,"
        "  verified_from_vantage TEXT, assigned_box_id TEXT, added_at TEXT NOT NULL, notes TEXT);"
        "CREATE TABLE burned_domains ("
        "  domain TEXT PRIMARY KEY, burned_at TEXT NOT NULL, reason TEXT NOT NULL,"
        "  last_box_id TEXT, details TEXT);"
    )
    conn.commit()
    migrate_v2_to_v3(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(cover_domain_pool)").fetchall()]
    assert "entered_in_use_at" in cols
    v = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    assert v == 3
    # Trigger refuses INSERT of burned domain
    conn.execute(
        "INSERT INTO burned_domains (domain, burned_at, reason) VALUES ('x.org', '2026-05-19T00:00:00Z', 'manual')"
    )
    conn.commit()
    try:
        conn.execute(
            "INSERT INTO cover_domain_pool (domain, state, added_at) "
            "VALUES ('x.org', 'candidate_unverified', '2026-05-19T01:00:00Z')"
        )
    except sqlite3.IntegrityError as e:
        assert "burned_domains" in str(e)
    else:
        raise AssertionError("expected IntegrityError")
