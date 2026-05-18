import json

from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema
from mthydra.controller.state.users_shards import (
    add_user,
    create_shard,
    latest_published_subset,
    list_users,
    publish_subset,
    set_user_shard,
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


def test_create_shard_and_assign_user(tmp_db_path):
    conn = _conn(tmp_db_path)
    add_user(conn, "u1", "Alice", "signal", "2026-05-18T00:00:00Z")
    create_shard(conn, shard_id="s1", members=["u1"], at="2026-05-18T01:00:00Z")
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
