import pytest

from mthydra.controller.state.authority import insert_authority, retire_authority
from mthydra.controller.state.db import connect
from mthydra.controller.state.descriptor import insert_signing_key
from mthydra.controller.state.invariants import InvariantViolation, check_all
from mthydra.controller.state.schema import SCHEMA_VERSION, apply_schema


def _seeded(tmp_db_path):
    conn = connect(tmp_db_path)
    apply_schema(conn)
    insert_authority(conn, 1, "P", "K", "2026-05-18T00:00:00Z")
    insert_signing_key(conn, 1, b"P", b"K", "2026-05-18T00:00:00Z")
    return conn


def test_check_all_passes_on_clean_seeded_db(tmp_db_path):
    conn = _seeded(tmp_db_path)
    check_all(conn, expected_schema_version=SCHEMA_VERSION)


def test_check_all_rejects_unknown_schema_version(tmp_db_path):
    conn = _seeded(tmp_db_path)
    with pytest.raises(InvariantViolation, match="schema_version"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION + 99)


def test_check_all_rejects_overlap_pool_and_burned(tmp_db_path):
    conn = _seeded(tmp_db_path)
    conn.execute(
        "INSERT INTO cover_domain_pool (domain, state, added_at) VALUES ('x.org', 'in_use', '2026-05-18T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO burned_domains (domain, burned_at, reason) VALUES ('x.org', '2026-05-18T01:00:00Z', 'job2_kill')"
    )
    conn.commit()
    with pytest.raises(InvariantViolation, match="overlap"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION)


def test_check_all_rejects_no_active_authority(tmp_db_path):
    conn = _seeded(tmp_db_path)
    retire_authority(conn, 1, at="2026-05-19T00:00:00Z")
    with pytest.raises(InvariantViolation, match="credential_authority"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION)


def test_check_all_rejects_truly_impossible_state(tmp_db_path):
    conn = _seeded(tmp_db_path)
    conn.execute(
        "INSERT INTO backup_log (generation, created_at, size_bytes, sha256, pushed_at, index_updated_at, trigger) "
        "VALUES (1, '2026-05-18T00:00:00Z', 4096, 'abc', NULL, '2026-05-18T00:00:11Z', 'floor_timer')"
    )
    conn.commit()
    with pytest.raises(InvariantViolation, match="impossible"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION)
