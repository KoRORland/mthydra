"""Catalogue tests for the spec K v10 distribution_log triggers."""
from __future__ import annotations

import sqlite3

import pytest

from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "state.sqlite")
    apply_schema(c)
    c.execute(
        "INSERT INTO users (user_id, display_name, out_of_band_channel, added_at) "
        "VALUES ('u1', NULL, 'email', '2026-05-25T00:00:00Z')"
    )
    c.execute(
        "INSERT INTO distribution_log (user_id, channel, kind, attempted_at, "
        "delivered_at, subset_hash, payload_json) "
        "VALUES ('u1', 'telegram', 'subset_delta', ?, ?, 'h', '[]')",
        ("2026-05-25T00:00:00Z", "2026-05-25T00:00:01Z"),
    )
    c.commit()
    yield c
    c.close()


def test_update_refused(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE distribution_log SET payload_json='changed'")
        conn.commit()


def test_delete_refused(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM distribution_log")
        conn.commit()


def test_update_with_where_refused(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE distribution_log SET error='x' WHERE id=1")
        conn.commit()


def test_insert_still_succeeds(conn):
    conn.execute(
        "INSERT INTO distribution_log (user_id, channel, kind, attempted_at, "
        "delivered_at, subset_hash, payload_json) "
        "VALUES ('u1', 'email', 'heartbeat', ?, ?, NULL, 'ping')",
        ("2026-05-25T01:00:00Z", "2026-05-25T01:00:01Z"),
    )
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM distribution_log").fetchone()[0]
    assert n == 2
