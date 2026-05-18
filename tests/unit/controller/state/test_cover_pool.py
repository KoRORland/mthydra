from mthydra.controller.state.cover_pool import (
    add_candidate,
    list_by_state,
    mark_verified,
    move_to_in_use,
)
from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema


def _conn(tmp_db_path):
    conn = connect(tmp_db_path)
    apply_schema(conn)
    return conn


def test_add_candidate_starts_unverified(tmp_db_path):
    conn = _conn(tmp_db_path)
    add_candidate(conn, "example.org", added_at="2026-05-18T00:00:00Z")
    rows = list_by_state(conn, "candidate_unverified")
    assert [r.domain for r in rows] == ["example.org"]


def test_mark_verified_transitions_state(tmp_db_path):
    conn = _conn(tmp_db_path)
    add_candidate(conn, "example.org", added_at="2026-05-18T00:00:00Z")
    mark_verified(conn, "example.org", from_vantage="ru-vantage-1", at="2026-05-18T01:00:00Z")
    assert list_by_state(conn, "candidate_unverified") == []
    rows = list_by_state(conn, "candidate_verified")
    assert rows[0].verified_from_vantage == "ru-vantage-1"


def test_move_to_in_use_requires_verified(tmp_db_path):
    conn = _conn(tmp_db_path)
    add_candidate(conn, "example.org", added_at="2026-05-18T00:00:00Z")
    try:
        move_to_in_use(conn, "example.org", box_id="box-1")
    except ValueError as e:
        assert "candidate_verified" in str(e)
    else:
        raise AssertionError("expected ValueError")
