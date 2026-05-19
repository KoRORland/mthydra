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


def test_downgrade_stale_verified_returns_empty_when_no_stale(conn):
    add_candidate(conn, "fresh.org", added_at="2026-05-19T00:00:00Z")
    attest_verified(conn, "fresh.org", from_vantage="ru-vps-01", at="2026-05-19T01:00:00Z")
    downgraded = downgrade_stale_verified(
        conn, now="2026-05-20T00:00:00Z", reverify_after_days=30,
    )
    assert downgraded == []
    assert [r.domain for r in list_by_state(conn, "candidate_verified")] == ["fresh.org"]


def test_downgrade_stale_verified_returns_stale_only(conn):
    add_candidate(conn, "stale.org", added_at="2026-04-01T00:00:00Z")
    attest_verified(conn, "stale.org", from_vantage="ru-vps-01", at="2026-04-01T01:00:00Z")
    add_candidate(conn, "fresh.org", added_at="2026-05-15T00:00:00Z")
    attest_verified(conn, "fresh.org", from_vantage="ru-vps-01", at="2026-05-15T01:00:00Z")
    downgraded = downgrade_stale_verified(
        conn, now="2026-05-19T00:00:00Z", reverify_after_days=30,
    )
    assert downgraded == ["stale.org"]
    assert {r.domain for r in list_by_state(conn, "candidate_verified")} == {"fresh.org"}
    assert {r.domain for r in list_by_state(conn, "candidate_unverified")} == {"stale.org"}


def test_downgrade_stale_verified_emits_one_audit_per_row(conn):
    add_candidate(conn, "a.org", added_at="2026-04-01T00:00:00Z")
    attest_verified(conn, "a.org", from_vantage="ru-vps-01", at="2026-04-01T01:00:00Z")
    add_candidate(conn, "b.org", added_at="2026-04-01T00:00:00Z")
    attest_verified(conn, "b.org", from_vantage="ru-vps-01", at="2026-04-01T01:00:00Z")
    downgrade_stale_verified(
        conn, now="2026-05-19T00:00:00Z", reverify_after_days=30,
    )
    actions = [e.action for e in recent_events(conn, limit=10)]
    assert actions.count("cover_downgraded_stale") == 2


def test_list_due_for_rotation_empty_when_no_in_use(conn):
    add_candidate(conn, "fresh.org", added_at=NOW)
    attest_verified(conn, "fresh.org", from_vantage="ru-vps-01", at=NOW)
    due = list_due_for_rotation(conn, now=NOW, rotation_ttl_days=14)
    assert due == []


def test_list_due_for_rotation_returns_overdue_only(conn):
    _seed_live_box(conn, "box-old")
    _seed_live_box(conn, "box-new", sni="new-sni.invalid")
    add_candidate(conn, "old.org", added_at="2026-04-01T00:00:00Z")
    attest_verified(conn, "old.org", from_vantage="ru-vps-01", at="2026-04-01T01:00:00Z")
    assign_to_box(conn, "old.org", box_id="box-old", at="2026-04-01T02:00:00Z")
    add_candidate(conn, "new.org", added_at="2026-05-15T00:00:00Z")
    attest_verified(conn, "new.org", from_vantage="ru-vps-01", at="2026-05-15T01:00:00Z")
    assign_to_box(conn, "new.org", box_id="box-new", at="2026-05-15T02:00:00Z")

    due = list_due_for_rotation(conn, now="2026-05-19T00:00:00Z", rotation_ttl_days=14)
    assert [r.domain for r in due] == ["old.org"]


def test_pool_health_counts_and_freeze_flag(conn):
    _seed_live_box(conn, "box-1")
    add_candidate(conn, "a.org", added_at=NOW)
    attest_verified(conn, "a.org", from_vantage="ru-vps-01", at=NOW)
    add_candidate(conn, "b.org", added_at=NOW)            # unverified
    add_candidate(conn, "c.org", added_at=NOW)
    attest_verified(conn, "c.org", from_vantage="ru-vps-01", at=NOW)
    assign_to_box(conn, "c.org", box_id="box-1", at=NOW)  # in_use

    h = pool_health(conn, freeze_threshold=2)
    assert h.candidate_unverified == 1
    assert h.candidate_verified == 1
    assert h.in_use == 1
    assert h.burned == 0
    assert h.rotation_frozen is True


def test_pool_health_not_frozen_when_above_threshold(conn):
    add_candidate(conn, "a.org", added_at=NOW)
    attest_verified(conn, "a.org", from_vantage="ru-vps-01", at=NOW)
    add_candidate(conn, "b.org", added_at=NOW)
    attest_verified(conn, "b.org", from_vantage="ru-vps-01", at=NOW)
    h = pool_health(conn, freeze_threshold=2)
    assert h.candidate_verified == 2
    assert h.rotation_frozen is False
