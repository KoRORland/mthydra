"""Tests for eu_exit_set repository (spec B §3.1)."""
import pytest

from mthydra.controller.state.db import connect
from mthydra.controller.state.eu_exit_set import add_exit, list_active, list_all, retire_exit
from mthydra.controller.state.schema import apply_schema


def _conn(tmp_db_path):
    conn = connect(tmp_db_path)
    apply_schema(conn)
    return conn


def test_add_and_list_active(tmp_db_path):
    conn = _conn(tmp_db_path)
    add_exit(conn, "fp1", "eu1.example.org:443", 1, "2026-05-19T00:00:00Z")
    add_exit(conn, "fp2", "eu2.example.org:443", 2, "2026-05-19T00:00:01Z")
    active = list_active(conn)
    assert len(active) == 2
    assert active[0].fingerprint == "fp1"  # sorted by fingerprint
    assert active[1].weight == 2


def test_retire_removes_from_active(tmp_db_path):
    conn = _conn(tmp_db_path)
    add_exit(conn, "fp1", "eu1.example.org:443", 1, "2026-05-19T00:00:00Z")
    retire_exit(conn, "fp1", at="2026-05-19T01:00:00Z")
    assert list_active(conn) == []
    all_rows = list_all(conn)
    assert all_rows[0].retired_at == "2026-05-19T01:00:00Z"


def test_retire_unknown_raises(tmp_db_path):
    conn = _conn(tmp_db_path)
    with pytest.raises(ValueError, match="not found or already retired"):
        retire_exit(conn, "nonexistent", at="2026-05-19T01:00:00Z")


def test_retire_already_retired_raises(tmp_db_path):
    conn = _conn(tmp_db_path)
    add_exit(conn, "fp1", "eu1.example.org:443", 1, "2026-05-19T00:00:00Z")
    retire_exit(conn, "fp1", at="2026-05-19T01:00:00Z")
    with pytest.raises(ValueError):
        retire_exit(conn, "fp1", at="2026-05-19T02:00:00Z")


def test_duplicate_fingerprint_raises(tmp_db_path):
    conn = _conn(tmp_db_path)
    add_exit(conn, "fp1", "eu1.example.org:443", 1, "2026-05-19T00:00:00Z")
    with pytest.raises(Exception):  # UNIQUE violation
        add_exit(conn, "fp1", "eu2.example.org:443", 1, "2026-05-19T00:00:01Z")


def test_weight_default_is_one(tmp_db_path):
    conn = _conn(tmp_db_path)
    add_exit(conn, "fp1", "eu1.example.org:443", 1, "2026-05-19T00:00:00Z")
    assert list_active(conn)[0].weight == 1


def test_ordering_by_fingerprint(tmp_db_path):
    conn = _conn(tmp_db_path)
    add_exit(conn, "zzz", "z.example.org:443", 1, "2026-05-19T00:00:00Z")
    add_exit(conn, "aaa", "a.example.org:443", 1, "2026-05-19T00:00:00Z")
    active = list_active(conn)
    assert [r.fingerprint for r in active] == ["aaa", "zzz"]
