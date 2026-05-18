import sqlite3

import pytest

from mthydra.controller.state.db import connect


def test_connect_enables_foreign_keys(tmp_db_path):
    conn = connect(tmp_db_path)
    fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert fk == 1


def test_connect_uses_wal_journal_mode(tmp_db_path):
    conn = connect(tmp_db_path)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_connect_creates_parent_dir(tmp_path):
    target = tmp_path / "deep" / "nested" / "state.sqlite"
    connect(target)
    assert target.exists()


def test_connect_rejects_missing_path_for_readonly(tmp_path):
    target = tmp_path / "missing.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        connect(target, read_only=True)
