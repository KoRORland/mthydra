"""Tests for mthydra.controller.state.shards — repository, lifecycle, reshuffle."""
from __future__ import annotations

import json
import sqlite3

import pytest

from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema
from mthydra.controller.state.shards import (
    Shard,
    ShardHealth,
    assign_box_to_shard,
    create_shard,
    get_shard,
    health,
    list_active,
    list_shard_boxes,
    reshuffle,
    retire_shard,
)


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "state.sqlite"
    c = connect(db)
    apply_schema(c)
    yield c
    c.close()


def _insert_user(conn, user_id: str, shard_id: str | None = None) -> None:
    conn.execute(
        "INSERT INTO users (user_id, display_name, out_of_band_channel, current_shard_id, added_at) "
        "VALUES (?, NULL, 'email', ?, '2026-05-24T00:00:00Z')",
        (user_id, shard_id),
    )
    conn.commit()


def _insert_box(conn, box_id: str, *, state: str = "provisioning", shard_id: str | None = None) -> None:
    conn.execute(
        "INSERT INTO ru_boxes (box_id, provider, region, sni, shard_id, state, image_version, created_at) "
        "VALUES (?, 'p', 'r', ?, ?, ?, 'v1', '2026-05-24T00:00:00Z')",
        (box_id, f"sni-{box_id}.example", shard_id, state),
    )
    conn.commit()


def test_create_shard_inserts_row_and_audit(conn):
    create_shard(conn, shard_id="s1", members=["u1", "u2"], target_size=2,
                 at="2026-05-24T00:00:00Z")
    row = conn.execute(
        "SELECT shard_id, members_json, target_size, retired_at FROM shards WHERE shard_id='s1'"
    ).fetchone()
    assert row == ("s1", '["u1", "u2"]', 2, None)
    audits = conn.execute(
        "SELECT action, target FROM audit_log WHERE action='shard_create'"
    ).fetchall()
    assert audits == [("shard_create", "s1")]


def test_create_shard_refuses_duplicate(conn):
    create_shard(conn, shard_id="s1", members=[], target_size=2,
                 at="2026-05-24T00:00:00Z")
    with pytest.raises(sqlite3.IntegrityError):
        create_shard(conn, shard_id="s1", members=[], target_size=2,
                     at="2026-05-24T00:00:00Z")


def test_retire_shard_sets_retired_at_and_audits(conn):
    create_shard(conn, shard_id="s1", members=[], target_size=2,
                 at="2026-05-24T00:00:00Z")
    retire_shard(conn, "s1", at="2026-05-24T01:00:00Z")
    row = conn.execute("SELECT retired_at FROM shards WHERE shard_id='s1'").fetchone()
    assert row[0] == "2026-05-24T01:00:00Z"
    audits = conn.execute(
        "SELECT action, target FROM audit_log WHERE action='shard_retire'"
    ).fetchall()
    assert audits == [("shard_retire", "s1")]


def test_retire_shard_idempotent_or_raises(conn):
    create_shard(conn, shard_id="s1", members=[], target_size=2,
                 at="2026-05-24T00:00:00Z")
    retire_shard(conn, "s1", at="2026-05-24T01:00:00Z")
    # second retire raises (already retired)
    with pytest.raises(LookupError):
        retire_shard(conn, "s1", at="2026-05-24T02:00:00Z")


def test_list_active_excludes_retired(conn):
    create_shard(conn, shard_id="s1", members=[], target_size=2,
                 at="2026-05-24T00:00:00Z")
    create_shard(conn, shard_id="s2", members=[], target_size=2,
                 at="2026-05-24T00:00:00Z")
    retire_shard(conn, "s1", at="2026-05-24T01:00:00Z")
    active = list_active(conn)
    assert [s.shard_id for s in active] == ["s2"]


def test_get_shard_returns_record(conn):
    create_shard(conn, shard_id="s1", members=["u1"], target_size=2,
                 at="2026-05-24T00:00:00Z")
    s = get_shard(conn, "s1")
    assert isinstance(s, Shard)
    assert s.shard_id == "s1"
    assert json.loads(s.members_json) == ["u1"]
    assert s.target_size == 2


def test_get_shard_raises_when_missing(conn):
    with pytest.raises(LookupError):
        get_shard(conn, "nonexistent")


def test_assign_box_to_shard_succeeds_for_provisioning(conn):
    create_shard(conn, shard_id="s1", members=[], target_size=2,
                 at="2026-05-24T00:00:00Z")
    _insert_box(conn, "b1", state="provisioning")
    assign_box_to_shard(conn, box_id="b1", shard_id="s1", at="2026-05-24T00:01:00Z")
    row = conn.execute("SELECT shard_id FROM ru_boxes WHERE box_id='b1'").fetchone()
    assert row[0] == "s1"
    audits = conn.execute(
        "SELECT action, target FROM audit_log WHERE action='shard_assign_box'"
    ).fetchall()
    assert audits == [("shard_assign_box", "s1")]


def test_assign_box_to_shard_refused_when_live_cross_shard(conn):
    create_shard(conn, shard_id="s1", members=[], target_size=2,
                 at="2026-05-24T00:00:00Z")
    create_shard(conn, shard_id="s2", members=[], target_size=2,
                 at="2026-05-24T00:00:00Z")
    _insert_box(conn, "b1", state="live", shard_id="s1")
    with pytest.raises(sqlite3.IntegrityError):
        assign_box_to_shard(conn, box_id="b1", shard_id="s2",
                            at="2026-05-24T00:01:00Z")


def test_list_shard_boxes_filters_state(conn):
    create_shard(conn, shard_id="s1", members=[], target_size=2,
                 at="2026-05-24T00:00:00Z")
    _insert_box(conn, "b1", state="live", shard_id="s1")
    _insert_box(conn, "b2", state="provisioning", shard_id="s1")
    _insert_box(conn, "b3", state="terminated", shard_id="s1")
    boxes = list_shard_boxes(conn, "s1")
    assert sorted(boxes) == ["b1", "b2"]
    with_term = list_shard_boxes(conn, "s1", include_terminated=True)
    assert sorted(with_term) == ["b1", "b2", "b3"]


def test_reshuffle_atomic_retire_create_remap(conn):
    create_shard(conn, shard_id="s1", members=["u1", "u2"], target_size=2,
                 at="2026-05-24T00:00:00Z")
    _insert_user(conn, "u1", shard_id="s1")
    _insert_user(conn, "u2", shard_id="s1")
    returned = reshuffle(
        conn, "s1",
        now="2026-05-24T01:00:00Z",
        target_size=2,
        new_shard_id="s2",
        new_members=["u1", "u2"],
        reason="ttl",
    )
    assert returned == "s2"
    # Old shard retired.
    old = conn.execute("SELECT retired_at FROM shards WHERE shard_id='s1'").fetchone()
    assert old[0] == "2026-05-24T01:00:00Z"
    # New shard created.
    new = conn.execute(
        "SELECT members_json, target_size, retired_at FROM shards WHERE shard_id='s2'"
    ).fetchone()
    assert json.loads(new[0]) == ["u1", "u2"]
    assert new[1] == 2
    assert new[2] is None
    # Users remapped.
    users = conn.execute(
        "SELECT user_id, current_shard_id FROM users ORDER BY user_id"
    ).fetchall()
    assert users == [("u1", "s2"), ("u2", "s2")]
    # Audit row exists with reason.
    audits = conn.execute(
        "SELECT details_json FROM audit_log WHERE action='shard_reshuffle'"
    ).fetchall()
    assert len(audits) == 1
    details = json.loads(audits[0][0])
    assert details["from"] == "s1"
    assert details["to"] == "s2"
    assert details["reason"] == "ttl"


def test_reshuffle_refuses_retired_shard(conn):
    create_shard(conn, shard_id="s1", members=[], target_size=2,
                 at="2026-05-24T00:00:00Z")
    retire_shard(conn, "s1", at="2026-05-24T01:00:00Z")
    with pytest.raises(LookupError):
        reshuffle(conn, "s1", now="2026-05-24T02:00:00Z",
                  target_size=2, new_shard_id="s2",
                  new_members=[], reason="ttl")


def test_reshuffle_refuses_reused_shard_id(conn):
    create_shard(conn, shard_id="s1", members=[], target_size=2,
                 at="2026-05-24T00:00:00Z")
    # Attempt to reshuffle s1 -> s1 should refuse (new_shard_id must be fresh).
    with pytest.raises(ValueError):
        reshuffle(conn, "s1", now="2026-05-24T01:00:00Z",
                  target_size=2, new_shard_id="s1",
                  new_members=[], reason="ttl")


def test_health_lists_overdue_and_unassigned(conn):
    # s1 created at T0 with TTL 14d; "now" T = 15d -> overdue.
    create_shard(conn, shard_id="s1", members=["u1"], target_size=2,
                 at="2026-05-01T00:00:00Z")
    # s2 created at T0 with same TTL but freshly reshuffled later.
    create_shard(conn, shard_id="s2", members=["u2"], target_size=2,
                 at="2026-05-01T00:00:00Z")
    conn.execute("UPDATE shards SET last_reshuffled_at='2026-05-20T00:00:00Z' WHERE shard_id='s2'")
    _insert_user(conn, "u1", shard_id="s1")
    _insert_user(conn, "u2", shard_id="s2")
    _insert_user(conn, "u3", shard_id=None)  # unassigned
    conn.commit()
    h = health(conn, now="2026-05-21T00:00:00Z",
               reshuffle_interval_seconds=14 * 86400)
    assert isinstance(h, ShardHealth)
    assert h.overdue_for_reshuffle == ["s1"]
    assert h.unassigned_users == ["u3"]
    assert h.total_active == 2
    assert h.total_retired == 0


def test_health_handles_empty_fleet(conn):
    h = health(conn, now="2026-05-21T00:00:00Z",
               reshuffle_interval_seconds=14 * 86400)
    assert h.overdue_for_reshuffle == []
    assert h.unassigned_users == []
    assert h.total_active == 0
    assert h.total_retired == 0


def test_list_all_includes_retired(conn):
    from mthydra.controller.state.shards import list_all

    create_shard(conn, shard_id="s1", members=[], target_size=2,
                 at="2026-05-24T00:00:00Z")
    create_shard(conn, shard_id="s2", members=[], target_size=2,
                 at="2026-05-24T00:00:00Z")
    retire_shard(conn, "s1", at="2026-05-24T01:00:00Z")
    everything = list_all(conn)
    assert sorted(s.shard_id for s in everything) == ["s1", "s2"]


def test_assign_box_to_shard_refuses_missing_box(conn):
    create_shard(conn, shard_id="s1", members=[], target_size=2,
                 at="2026-05-24T00:00:00Z")
    with pytest.raises(LookupError):
        assign_box_to_shard(conn, box_id="nope", shard_id="s1",
                            at="2026-05-24T00:01:00Z")


def test_reshuffle_refuses_missing_old_shard(conn):
    with pytest.raises(LookupError):
        reshuffle(conn, "nope", now="2026-05-24T00:00:00Z",
                  target_size=2, new_shard_id="s-new",
                  new_members=[], reason="ttl")
