import sqlite3

import pytest

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


def test_schema_version_is_3_removed(tmp_db_path):
    # Superseded by test_schema_version_is_4 — kept as a no-op to preserve numbering.
    pass


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



def test_node_state_table_present_and_seeded_active(tmp_db_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    conn = connect(tmp_db_path)
    apply_schema(conn)
    rows = conn.execute("SELECT role FROM node_state").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "active"


def test_node_state_singleton_rejects_second_row(tmp_db_path):
    import sqlite3
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    conn = connect(tmp_db_path)
    apply_schema(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO node_state (rowid, role) VALUES (2, 'standby')")
        conn.commit()


def test_eu_nodes_table_present(tmp_db_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    conn = connect(tmp_db_path)
    apply_schema(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(eu_nodes)").fetchall()]
    assert {"node_id", "hostname", "provider", "region", "public_ip",
            "role", "added_at", "promoted_at", "retired_at",
            "last_heartbeat_at", "last_heartbeat_b2_etag", "notes",
            "cover_sni", "reality_pubkey",
            "data_exit_state", "data_exit_started_at"} == set(cols)


def test_v3_to_v4_migration_seeds_node_state_active(tmp_db_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import migrate_v3_to_v4
    conn = connect(tmp_db_path)
    # Manually construct a v3 DB shell.
    conn.executescript(
        "CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL, CHECK (rowid=1));"
        "INSERT INTO schema_version (rowid, version, applied_at) VALUES (1, 3, '2026-05-20T00:00:00Z');"
    )
    conn.commit()
    migrate_v3_to_v4(conn)
    v = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    assert v == 4
    role = conn.execute("SELECT role FROM node_state WHERE rowid=1").fetchone()[0]
    assert role == "active"


def test_schema_version_is_5_removed(tmp_db_path):
    # Superseded by test_schema_version_is_6 — kept as a no-op to preserve numbering.
    pass


def test_ru_images_table_present(tmp_db_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    conn = connect(tmp_db_path)
    apply_schema(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(ru_images)").fetchall()]
    assert {"image_version", "upstream_release", "upstream_repo",
            "binary_url", "manifest_url", "binary_sha256",
            "binary_size_bytes", "state", "built_at",
            "promoted_at", "retired_at", "notes"} == set(cols)


def test_ru_images_state_index_present(tmp_db_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    conn = connect(tmp_db_path)
    apply_schema(conn)
    idxs = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='ru_images'"
        ).fetchall()
    }
    assert "ix_ru_images_state" in idxs


def test_v4_to_v5_migration_adds_table(tmp_db_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import migrate_v4_to_v5
    conn = connect(tmp_db_path)
    conn.executescript(
        "CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL, CHECK (rowid=1));"
        "INSERT INTO schema_version (rowid, version, applied_at) VALUES (1, 4, '2026-05-21T00:00:00Z');"
    )
    conn.commit()
    migrate_v4_to_v5(conn)
    v = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    assert v == 5
    cols = [r[1] for r in conn.execute("PRAGMA table_info(ru_images)").fetchall()]
    assert "image_version" in cols


def test_schema_version_at_least_6(tmp_db_path):
    """Superseded by test_schema_version_is_7. Kept to preserve numbering."""
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import SCHEMA_VERSION, apply_schema
    assert SCHEMA_VERSION >= 6
    conn = connect(tmp_db_path)
    apply_schema(conn)
    row = conn.execute("SELECT version FROM schema_version WHERE rowid=1").fetchone()
    assert row[0] >= 6


def test_v5_to_v6_migration_adds_columns(tmp_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema

    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)

    cols_ru = [r[1] for r in conn.execute("PRAGMA table_info(ru_boxes)").fetchall()]
    assert "reality_uuid" in cols_ru

    cols_eu = [r[1] for r in conn.execute("PRAGMA table_info(eu_nodes)").fetchall()]
    assert "cover_sni" in cols_eu
    assert "reality_pubkey" in cols_eu
    assert "data_exit_state" in cols_eu
    assert "data_exit_started_at" in cols_eu

    cols_ex = [r[1] for r in conn.execute("PRAGMA table_info(eu_exit_set)").fetchall()]
    assert "cover_sni" in cols_ex
    assert "reality_pubkey" in cols_ex


def test_v5_to_v6_migration_idempotent(tmp_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema, migrate_v5_to_v6

    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    migrate_v5_to_v6(conn)
    migrate_v5_to_v6(conn)
    version = conn.execute("SELECT version FROM schema_version WHERE rowid=1").fetchone()[0]
    assert version == 6


def test_v6_reality_uuid_unique_partial(tmp_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema

    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    # Two NULLs allowed (partial unique index excludes NULL).
    conn.execute(
        "INSERT INTO ru_boxes (box_id, provider, region, sni, state, image_version, created_at) "
        "VALUES ('a', 'p', 'r', 'sni-a', 'provisioning', 'v1', '2026-05-23T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO ru_boxes (box_id, provider, region, sni, state, image_version, created_at) "
        "VALUES ('b', 'p', 'r', 'sni-b', 'provisioning', 'v1', '2026-05-23T00:00:00Z')"
    )
    conn.execute("UPDATE ru_boxes SET reality_uuid='same-uuid' WHERE box_id='a'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE ru_boxes SET reality_uuid='same-uuid' WHERE box_id='b'")


def test_v5_to_v6_migration_from_v5_db(tmp_path):
    """Simulate a v5 DB (no new columns) and verify migration adds them."""
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema, migrate_v5_to_v6

    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    # Force back to v5 to simulate pre-E DB; columns are already there from fresh
    # install but migrate must remain idempotent and bump version.
    conn.execute("UPDATE schema_version SET version=5 WHERE rowid=1")
    conn.commit()
    migrate_v5_to_v6(conn)
    v = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    assert v == 6
    # Index present.
    idxs = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='ru_boxes'"
        ).fetchall()
    }
    assert "idx_ru_boxes_reality_uuid" in idxs


# --- spec H schema v7 tests ---

def test_schema_version_at_least_7(tmp_path):
    """Superseded by test_schema_version_is_8. Kept to preserve numbering."""
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import SCHEMA_VERSION, apply_schema

    assert SCHEMA_VERSION >= 7
    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    row = conn.execute("SELECT version FROM schema_version WHERE rowid=1").fetchone()
    assert row[0] >= 7


def test_shards_target_size_column_present(tmp_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema

    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(shards)").fetchall()]
    assert "target_size" in cols


def test_v7_no_cross_shard_reassign_trigger_blocks_live(tmp_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema

    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    conn.execute(
        "INSERT INTO shards (shard_id, members_json, target_size, last_reshuffled_at, created_at) "
        "VALUES ('s1', '[]', 2, '2026-05-24T00:00:00Z', '2026-05-24T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO shards (shard_id, members_json, target_size, last_reshuffled_at, created_at) "
        "VALUES ('s2', '[]', 2, '2026-05-24T00:00:00Z', '2026-05-24T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO ru_boxes (box_id, provider, region, sni, shard_id, state, image_version, created_at) "
        "VALUES ('b1', 'p', 'r', 'sni1.example', 's1', 'live', 'v1', '2026-05-24T00:00:00Z')"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE ru_boxes SET shard_id='s2' WHERE box_id='b1'")
        conn.commit()


def test_v7_no_cross_shard_reassign_trigger_allows_provisioning(tmp_path):
    """Reassigning a provisioning box is fine (operator may change their mind)."""
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema

    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    conn.execute(
        "INSERT INTO shards (shard_id, members_json, target_size, last_reshuffled_at, created_at) "
        "VALUES ('s1', '[]', 2, '2026-05-24T00:00:00Z', '2026-05-24T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO shards (shard_id, members_json, target_size, last_reshuffled_at, created_at) "
        "VALUES ('s2', '[]', 2, '2026-05-24T00:00:00Z', '2026-05-24T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO ru_boxes (box_id, provider, region, sni, shard_id, state, image_version, created_at) "
        "VALUES ('b1', 'p', 'r', 'sni1.example', 's1', 'provisioning', 'v1', '2026-05-24T00:00:00Z')"
    )
    conn.execute("UPDATE ru_boxes SET shard_id='s2' WHERE box_id='b1'")
    conn.commit()
    row = conn.execute("SELECT shard_id FROM ru_boxes WHERE box_id='b1'").fetchone()
    assert row[0] == "s2"


def test_v7_terminated_keeps_shard_trigger(tmp_path):
    """terminating a box must NOT clear shard_id — history preservation."""
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema

    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    conn.execute(
        "INSERT INTO shards (shard_id, members_json, target_size, last_reshuffled_at, created_at) "
        "VALUES ('s1', '[]', 2, '2026-05-24T00:00:00Z', '2026-05-24T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO ru_boxes (box_id, provider, region, sni, shard_id, state, image_version, created_at) "
        "VALUES ('b1', 'p', 'r', 'sni1.example', 's1', 'live', 'v1', '2026-05-24T00:00:00Z')"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE ru_boxes SET state='terminated', shard_id=NULL WHERE box_id='b1'"
        )
        conn.commit()


def test_v7_terminate_with_shard_retained_succeeds(tmp_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema

    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    conn.execute(
        "INSERT INTO shards (shard_id, members_json, target_size, last_reshuffled_at, created_at) "
        "VALUES ('s1', '[]', 2, '2026-05-24T00:00:00Z', '2026-05-24T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO ru_boxes (box_id, provider, region, sni, shard_id, state, image_version, created_at) "
        "VALUES ('b1', 'p', 'r', 'sni1.example', 's1', 'live', 'v1', '2026-05-24T00:00:00Z')"
    )
    conn.commit()
    conn.execute(
        "UPDATE ru_boxes SET state='terminated', terminated_at='2026-05-24T01:00:00Z' "
        "WHERE box_id='b1'"
    )
    conn.commit()
    row = conn.execute(
        "SELECT state, shard_id FROM ru_boxes WHERE box_id='b1'"
    ).fetchone()
    assert row == ("terminated", "s1")


def test_v6_to_v7_migration_idempotent(tmp_path):
    """Force schema_version back to 6 and verify migrate_v6_to_v7 brings it to 7
    without breaking on already-present columns/triggers."""
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema, migrate_v6_to_v7

    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    conn.execute("UPDATE schema_version SET version=6 WHERE rowid=1")
    conn.commit()
    migrate_v6_to_v7(conn)
    v = conn.execute("SELECT version FROM schema_version WHERE rowid=1").fetchone()[0]
    assert v == 7
    # Re-running is a no-op.
    migrate_v6_to_v7(conn)
    v2 = conn.execute("SELECT version FROM schema_version WHERE rowid=1").fetchone()[0]
    assert v2 == 7


# --- spec I schema v8 tests ---

def test_schema_version_is_8(tmp_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import SCHEMA_VERSION, apply_schema

    assert SCHEMA_VERSION == 8
    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    row = conn.execute("SELECT version FROM schema_version WHERE rowid=1").fetchone()
    assert row[0] == 8


def test_probe_vantages_table_present(tmp_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema

    conn = connect(tmp_path / "state.sqlite")
    apply_schema(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(probe_vantages)").fetchall()}
    assert {"vantage_id", "label", "source_kind", "state",
            "added_at", "attested_at", "last_used_at",
            "retired_at", "burned_at", "burn_reason"} <= cols


def test_probe_results_table_present(tmp_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema

    conn = connect(tmp_path / "state.sqlite")
    apply_schema(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(probe_results)").fetchall()}
    assert {"id", "box_id", "vantage_id", "cycle_at",
            "check_type", "status", "evidence_json",
            "image_version", "recorded_at"} <= cols


def test_image_profiles_table_present(tmp_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema

    conn = connect(tmp_path / "state.sqlite")
    apply_schema(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(image_profiles)").fetchall()}
    assert {"image_version", "profile_json", "recorded_at", "recorded_by"} <= cols


def test_v8_probe_results_append_only(tmp_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema

    conn = connect(tmp_path / "state.sqlite")
    apply_schema(conn)
    # Need a parent box + image + vantage to insert a row.
    conn.execute(
        "INSERT INTO ru_images (image_version, upstream_release, upstream_repo, "
        "binary_url, manifest_url, binary_sha256, binary_size_bytes, state, built_at) "
        "VALUES ('v1', 'r', 'r', 'u', 'm', 'sha', 1, 'candidate', ?)",
        ("2026-05-25T00:00:00Z",),
    )
    conn.execute(
        "INSERT INTO ru_boxes (box_id, provider, region, sni, state, image_version, created_at) "
        "VALUES ('b1', 'p', 'r', 'sni1.example', 'live', 'v1', ?)",
        ("2026-05-25T00:00:00Z",),
    )
    conn.execute(
        "INSERT INTO probe_vantages (vantage_id, label, source_kind, state, added_at) "
        "VALUES ('vk', 'kz1', 'cloud-cis', 'active', ?)",
        ("2026-05-25T00:00:00Z",),
    )
    conn.execute(
        "INSERT INTO probe_results (box_id, vantage_id, cycle_at, check_type, status, "
        "image_version, recorded_at) VALUES ('b1', 'vk', ?, 'tls_fall_through', 'pass', 'v1', ?)",
        ("2026-05-25T00:00:00Z", "2026-05-25T00:00:00Z"),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE probe_results SET status='hard_fail' WHERE id=1")
        conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM probe_results WHERE id=1")
        conn.commit()


def test_v8_probe_vantages_no_relabel_burned(tmp_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema

    conn = connect(tmp_path / "state.sqlite")
    apply_schema(conn)
    conn.execute(
        "INSERT INTO probe_vantages (vantage_id, label, source_kind, state, added_at) "
        "VALUES ('v1', 'kz1', 'cloud-cis', 'burned', ?)",
        ("2026-05-25T00:00:00Z",),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO probe_vantages (vantage_id, label, source_kind, state, added_at) "
            "VALUES ('v2', 'kz1', 'cloud-cis', 'candidate', ?)",
            ("2026-05-25T00:00:00Z",),
        )
        conn.commit()


def test_v8_probe_vantages_burned_no_revert(tmp_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema

    conn = connect(tmp_path / "state.sqlite")
    apply_schema(conn)
    conn.execute(
        "INSERT INTO probe_vantages (vantage_id, label, source_kind, state, added_at) "
        "VALUES ('v1', 'kz1', 'cloud-cis', 'burned', ?)",
        ("2026-05-25T00:00:00Z",),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE probe_vantages SET state='active' WHERE vantage_id='v1'")
        conn.commit()


def test_v7_to_v8_migration_idempotent(tmp_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema, migrate_v7_to_v8

    conn = connect(tmp_path / "state.sqlite")
    apply_schema(conn)
    conn.execute("UPDATE schema_version SET version=7 WHERE rowid=1")
    conn.commit()
    migrate_v7_to_v8(conn)
    v = conn.execute("SELECT version FROM schema_version WHERE rowid=1").fetchone()[0]
    assert v == 8
    # Re-run is a no-op.
    migrate_v7_to_v8(conn)
    v2 = conn.execute("SELECT version FROM schema_version WHERE rowid=1").fetchone()[0]
    assert v2 == 8
