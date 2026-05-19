import pytest

from mthydra.controller.state.db import connect
from mthydra.controller.state.descriptor import (
    current_signing_key,
    insert_descriptor,
    insert_signing_key,
    latest_descriptor,
    retire_signing_key,
)
from mthydra.controller.state.schema import apply_schema


def _conn(tmp_db_path):
    conn = connect(tmp_db_path)
    apply_schema(conn)
    return conn


def test_insert_signing_key(tmp_db_path):
    conn = _conn(tmp_db_path)
    insert_signing_key(conn, generation=1, privkey=b"P", pubkey=b"K", created_at="2026-05-18T00:00:00Z")
    cur = current_signing_key(conn)
    assert cur.generation == 1


def test_insert_descriptor_references_active_key(tmp_db_path):
    conn = _conn(tmp_db_path)
    insert_signing_key(conn, 1, b"P", b"K", "2026-05-18T00:00:00Z")
    insert_descriptor(
        conn,
        generation=1,
        payload='{"exit_set":[]}',
        signed_at="2026-05-18T01:00:00Z",
        valid_until="2026-05-18T13:00:00Z",
        signing_key_generation=1,
    )
    d = latest_descriptor(conn)
    assert d is not None
    assert d.generation == 1
    assert d.signing_key_generation == 1


def test_insert_descriptor_rejects_unknown_signing_key(tmp_db_path):
    conn = _conn(tmp_db_path)
    with pytest.raises(Exception):  # FK violation surfaces as IntegrityError
        insert_descriptor(
            conn, 1, '{"exit_set":[]}', "2026-05-18T01:00:00Z", "2026-05-18T13:00:00Z", 99
        )
