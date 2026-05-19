"""Spec C — cover-domain pool state machine + audit emission."""
import pytest

from mthydra.controller.state.audit import recent_events
from mthydra.controller.state.burned import is_burned, mark_burned
from mthydra.controller.state.cover_pool import (
    add_candidate,
    assign_to_box,
    attest_verified,
    downgrade_stale_verified,
    list_by_state,
    list_due_for_rotation,
    pool_health,
    rotate_and_burn,
)
from mthydra.controller.state.db import connect
from mthydra.controller.state.ru_boxes import insert_box, mark_live
from mthydra.controller.state.schema import apply_schema

NOW = "2026-05-19T00:00:00Z"


@pytest.fixture
def conn(tmp_db_path):
    c = connect(tmp_db_path)
    apply_schema(c)
    return c


def _seed_live_box(c, box_id: str = "box-1", sni: str = "box-sni.invalid") -> None:
    insert_box(c, box_id, "aws", "eu-west-1", "10.0.0.1", sni, "img-v1", NOW)
    mark_live(c, box_id, public_ip="10.0.0.1", at=NOW)


def test_add_candidate_emits_audit(conn):
    add_candidate(conn, "example.org", added_at=NOW)
    rows = list_by_state(conn, "candidate_unverified")
    assert [r.domain for r in rows] == ["example.org"]
    ev = recent_events(conn, limit=1)
    assert ev[0].action == "cover_added"
    assert ev[0].target == "example.org"


def test_attest_verified_records_vantage_and_evidence(conn):
    add_candidate(conn, "example.org", added_at=NOW)
    attest_verified(
        conn,
        "example.org",
        from_vantage="ru-vps-01",
        at="2026-05-19T01:00:00Z",
        evidence="curl from RU vps: 200 OK + matching cert chain",
    )
    rows = list_by_state(conn, "candidate_verified")
    assert rows[0].verified_from_vantage == "ru-vps-01"
    assert rows[0].last_verified_at == "2026-05-19T01:00:00Z"
    ev = recent_events(conn, limit=1)
    assert ev[0].action == "cover_attest_verified"
    assert ev[0].target == "example.org"
    assert "curl from RU vps" in (ev[0].details_json or "")


def test_attest_verified_rejects_non_unverified(conn):
    add_candidate(conn, "example.org", added_at=NOW)
    attest_verified(conn, "example.org", from_vantage="ru-vps-01", at=NOW)
    with pytest.raises(ValueError, match="candidate_unverified"):
        attest_verified(conn, "example.org", from_vantage="ru-vps-01", at=NOW)


def test_assign_to_box_sets_entered_in_use_at(conn):
    _seed_live_box(conn, "box-1")
    add_candidate(conn, "example.org", added_at=NOW)
    attest_verified(conn, "example.org", from_vantage="ru-vps-01", at=NOW)
    assign_to_box(conn, "example.org", box_id="box-1", at="2026-05-20T00:00:00Z")
    rows = list_by_state(conn, "in_use")
    assert rows[0].entered_in_use_at == "2026-05-20T00:00:00Z"
    assert rows[0].assigned_box_id == "box-1"


def test_assign_to_box_refuses_non_verified(conn):
    _seed_live_box(conn, "box-1")
    add_candidate(conn, "example.org", added_at=NOW)
    with pytest.raises(ValueError, match="candidate_verified"):
        assign_to_box(conn, "example.org", box_id="box-1", at=NOW)


def test_assign_to_box_refuses_stale_after_downgrade(conn):
    _seed_live_box(conn, "box-1")
    add_candidate(conn, "example.org", added_at="2026-04-01T00:00:00Z")
    attest_verified(
        conn, "example.org", from_vantage="ru-vps-01",
        at="2026-04-01T01:00:00Z",
    )
    downgrade_stale_verified(
        conn, now="2026-05-19T00:00:00Z", reverify_after_days=30,
    )
    with pytest.raises(ValueError, match="candidate_verified"):
        assign_to_box(conn, "example.org", box_id="box-1", at=NOW)
