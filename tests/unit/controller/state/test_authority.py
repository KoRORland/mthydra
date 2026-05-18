import pytest

from mthydra.controller.state.authority import (
    current_authority,
    insert_authority,
    list_authorities,
    retire_authority,
)
from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema


def _conn(tmp_db_path):
    conn = connect(tmp_db_path)
    apply_schema(conn)
    return conn


def test_insert_first_authority(tmp_db_path):
    conn = _conn(tmp_db_path)
    insert_authority(conn, generation=1, privkey_pem="PRIV", pubkey_pem="PUB", created_at="2026-05-18T00:00:00Z")
    cur = current_authority(conn)
    assert cur.generation == 1
    assert cur.retired_at is None


def test_retire_then_insert_next(tmp_db_path):
    conn = _conn(tmp_db_path)
    insert_authority(conn, 1, "P1", "K1", "2026-05-18T00:00:00Z")
    retire_authority(conn, 1, at="2026-05-19T00:00:00Z")
    insert_authority(conn, 2, "P2", "K2", "2026-05-19T00:00:01Z")
    cur = current_authority(conn)
    assert cur.generation == 2
    assert len(list_authorities(conn)) == 2


def test_current_authority_raises_when_none_active(tmp_db_path):
    conn = _conn(tmp_db_path)
    insert_authority(conn, 1, "P", "K", "2026-05-18T00:00:00Z")
    retire_authority(conn, 1, at="2026-05-19T00:00:00Z")
    with pytest.raises(LookupError):
        current_authority(conn)
