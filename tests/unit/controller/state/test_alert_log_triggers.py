"""Catalogue tests for the spec J v9 alert_log triggers."""
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
        "INSERT INTO alert_log (attempted_at, delivered_at, sink, severity, "
        "kind, target, dedupe_key, payload) "
        "VALUES (?, ?, 'telegram', 'warn', 'x', NULL, 'k', 'p')",
        ("2026-05-25T00:00:00Z", "2026-05-25T00:00:01Z"),
    )
    c.commit()
    yield c
    c.close()


def test_insert_succeeds(conn):
    n = conn.execute("SELECT COUNT(*) FROM alert_log").fetchone()[0]
    assert n == 1


def test_update_refused(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE alert_log SET payload='changed'")
        conn.commit()


def test_delete_refused(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM alert_log")
        conn.commit()


def test_update_with_where_still_refused(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE alert_log SET error='x' WHERE id=1")
        conn.commit()
