import pytest

from mthydra.controller.state.burned import is_burned, mark_burned
from mthydra.controller.state.cover_pool import add_candidate, list_by_state, mark_verified, move_to_in_use
from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema


def _conn(tmp_db_path):
    conn = connect(tmp_db_path)
    apply_schema(conn)
    return conn


def _seed(conn, domain):
    add_candidate(conn, domain, added_at="2026-05-18T00:00:00Z")
    mark_verified(conn, domain, from_vantage="v1", at="2026-05-18T01:00:00Z")
    move_to_in_use(conn, domain, box_id="box-1")


def test_mark_burned_moves_domain_atomically(tmp_db_path):
    conn = _conn(tmp_db_path)
    _seed(conn, "example.org")
    mark_burned(
        conn,
        domain="example.org",
        reason="job2_kill",
        last_box_id="box-1",
        at="2026-05-18T02:00:00Z",
        details=None,
    )
    assert is_burned(conn, "example.org")
    assert list_by_state(conn, "in_use") == []


def test_mark_burned_refuses_already_burned(tmp_db_path):
    conn = _conn(tmp_db_path)
    _seed(conn, "example.org")
    mark_burned(conn, "example.org", "job2_kill", "box-1", "2026-05-18T02:00:00Z", None)
    with pytest.raises(ValueError, match="already burned"):
        mark_burned(conn, "example.org", "job2_kill", "box-1", "2026-05-18T03:00:00Z", None)


def test_mark_burned_refuses_unknown_domain(tmp_db_path):
    conn = _conn(tmp_db_path)
    with pytest.raises(ValueError, match="not present"):
        mark_burned(conn, "ghost.org", "job2_kill", "box-1", "2026-05-18T02:00:00Z", None)
