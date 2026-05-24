import json

import pytest

from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema
from mthydra.controller.state.shards import create_shard
from mthydra.controller.state.users_shards import (
    add_user,
    assign_user_to_shard,
    latest_published_subset,
    list_users,
    publish_subset,
    reshuffle_unassigned,
    set_user_shard,
    unassigned_users,
)


def _conn(tmp_db_path):
    conn = connect(tmp_db_path)
    apply_schema(conn)
    return conn


def test_add_user_and_list(tmp_db_path):
    conn = _conn(tmp_db_path)
    add_user(conn, user_id="u1", display_name="Alice", out_of_band_channel="signal:+1", at="2026-05-18T00:00:00Z")
    users = list_users(conn)
    assert [u.user_id for u in users] == ["u1"]


def test_set_user_shard(tmp_db_path):
    conn = _conn(tmp_db_path)
    add_user(conn, "u1", "Alice", "signal", "2026-05-18T00:00:00Z")
    create_shard(conn, shard_id="s1", members=["u1"], target_size=2,
                 at="2026-05-18T01:00:00Z")
    set_user_shard(conn, user_id="u1", shard_id="s1")
    users = list_users(conn)
    assert users[0].current_shard_id == "s1"


def test_publish_subset_appends(tmp_db_path):
    conn = _conn(tmp_db_path)
    publish_subset(conn, payload={"boxes": ["b1"]}, channel="telegram", at="2026-05-18T02:00:00Z")
    publish_subset(conn, payload={"boxes": ["b2"]}, channel="telegram", at="2026-05-18T03:00:00Z")
    latest = latest_published_subset(conn)
    assert json.loads(latest.payload_json) == {"boxes": ["b2"]}
    assert latest.publish_gen == 2


# --- spec H: assignment cap + unassigned roster + fold-in ---

def test_assign_user_to_shard_writes_audit(tmp_db_path):
    conn = _conn(tmp_db_path)
    add_user(conn, "u1", None, "email", "2026-05-24T00:00:00Z")
    create_shard(conn, shard_id="s1", members=[], target_size=2,
                 at="2026-05-24T00:00:00Z")
    assign_user_to_shard(conn, "u1", "s1", at="2026-05-24T00:01:00Z", max_size=3)
    row = conn.execute("SELECT current_shard_id FROM users WHERE user_id='u1'").fetchone()
    assert row[0] == "s1"
    members = json.loads(conn.execute(
        "SELECT members_json FROM shards WHERE shard_id='s1'"
    ).fetchone()[0])
    assert members == ["u1"]
    audits = conn.execute(
        "SELECT action, target FROM audit_log WHERE action='shard_assign_user'"
    ).fetchall()
    assert audits == [("shard_assign_user", "s1")]


def test_assign_user_to_shard_refuses_at_max(tmp_db_path):
    conn = _conn(tmp_db_path)
    add_user(conn, "u1", None, "email", "2026-05-24T00:00:00Z")
    add_user(conn, "u2", None, "email", "2026-05-24T00:00:00Z")
    add_user(conn, "u3", None, "email", "2026-05-24T00:00:00Z")
    add_user(conn, "u4", None, "email", "2026-05-24T00:00:00Z")
    create_shard(conn, shard_id="s1", members=["u1", "u2", "u3"], target_size=3,
                 at="2026-05-24T00:00:00Z")
    with pytest.raises(ValueError):
        assign_user_to_shard(conn, "u4", "s1", at="2026-05-24T00:01:00Z", max_size=3)


def test_assign_user_to_shard_refuses_retired(tmp_db_path):
    conn = _conn(tmp_db_path)
    add_user(conn, "u1", None, "email", "2026-05-24T00:00:00Z")
    create_shard(conn, shard_id="s1", members=[], target_size=2,
                 at="2026-05-24T00:00:00Z")
    conn.execute("UPDATE shards SET retired_at='2026-05-24T00:30:00Z' WHERE shard_id='s1'")
    conn.commit()
    with pytest.raises(LookupError):
        assign_user_to_shard(conn, "u1", "s1", at="2026-05-24T00:31:00Z", max_size=3)


def test_assign_user_to_shard_idempotent_for_existing_member(tmp_db_path):
    conn = _conn(tmp_db_path)
    add_user(conn, "u1", None, "email", "2026-05-24T00:00:00Z")
    create_shard(conn, shard_id="s1", members=["u1"], target_size=2,
                 at="2026-05-24T00:00:00Z")
    set_user_shard(conn, "u1", "s1")
    assign_user_to_shard(conn, "u1", "s1", at="2026-05-24T00:01:00Z", max_size=3)
    # No new audit row written (idempotent path).
    n = conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action='shard_assign_user'"
    ).fetchone()[0]
    assert n == 0


def test_unassigned_users_lists_null_shard(tmp_db_path):
    conn = _conn(tmp_db_path)
    add_user(conn, "u1", None, "email", "2026-05-24T00:00:00Z")
    add_user(conn, "u2", None, "email", "2026-05-24T00:00:00Z")
    add_user(conn, "u3", None, "email", "2026-05-24T00:00:00Z")
    create_shard(conn, shard_id="s1", members=["u1"], target_size=2,
                 at="2026-05-24T00:00:00Z")
    set_user_shard(conn, "u1", "s1")
    assert unassigned_users(conn) == ["u2", "u3"]


def test_reshuffle_unassigned_folds_into_chunks(tmp_db_path):
    conn = _conn(tmp_db_path)
    for u in ["u1", "u2", "u3", "u4", "u5"]:
        add_user(conn, u, None, "email", "2026-05-24T00:00:00Z")
    ids = iter(["sA", "sB", "sC"])
    new_sids = reshuffle_unassigned(
        conn,
        now="2026-05-24T01:00:00Z",
        target_size=2,
        shard_id_factory=lambda: next(ids),
    )
    assert new_sids == ["sA", "sB", "sC"]
    shards = conn.execute(
        "SELECT shard_id, members_json FROM shards ORDER BY shard_id"
    ).fetchall()
    by_id = {sid: json.loads(mj) for sid, mj in shards}
    assert by_id["sA"] == ["u1", "u2"]
    assert by_id["sB"] == ["u3", "u4"]
    assert by_id["sC"] == ["u5"]
    # All users assigned now.
    assert unassigned_users(conn) == []


def test_reshuffle_unassigned_noop_when_empty(tmp_db_path):
    conn = _conn(tmp_db_path)
    new_sids = reshuffle_unassigned(
        conn, now="2026-05-24T00:00:00Z",
        target_size=2, shard_id_factory=lambda: "should-not-be-called",
    )
    assert new_sids == []
