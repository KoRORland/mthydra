"""Systematic catalogue of the spec H v7 disjointness triggers.

Schema-level smoke coverage lives in test_schema.py; this file exercises
every state combination the two triggers care about.
"""
from __future__ import annotations

import sqlite3

import pytest

from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema


def _setup(tmp_path):
    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    conn.execute(
        "INSERT INTO shards (shard_id, members_json, target_size, last_reshuffled_at, created_at) "
        "VALUES ('s1', '[]', 2, '2026-05-24T00:00:00Z', '2026-05-24T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO shards (shard_id, members_json, target_size, last_reshuffled_at, created_at) "
        "VALUES ('s2', '[]', 2, '2026-05-24T00:00:00Z', '2026-05-24T00:00:00Z')"
    )
    conn.commit()
    return conn


def _insert_box(conn, box_id, *, state, shard_id=None):
    conn.execute(
        "INSERT INTO ru_boxes (box_id, provider, region, sni, shard_id, state, image_version, created_at) "
        "VALUES (?, 'p', 'r', ?, ?, ?, 'v1', '2026-05-24T00:00:00Z')",
        (box_id, f"sni-{box_id}.example", shard_id, state),
    )
    conn.commit()


# --- ru_boxes_no_cross_shard_reassign ---

def test_assign_null_to_provisioning_allowed(tmp_path):
    conn = _setup(tmp_path)
    _insert_box(conn, "b1", state="provisioning", shard_id=None)
    conn.execute("UPDATE ru_boxes SET shard_id='s1' WHERE box_id='b1'")
    conn.commit()  # OK — OLD.shard_id IS NULL


def test_reassign_provisioning_to_different_shard_allowed(tmp_path):
    """Provisioning is the explicit escape hatch (operator may change mind)."""
    conn = _setup(tmp_path)
    _insert_box(conn, "b1", state="provisioning", shard_id="s1")
    conn.execute("UPDATE ru_boxes SET shard_id='s2' WHERE box_id='b1'")
    conn.commit()
    row = conn.execute("SELECT shard_id FROM ru_boxes WHERE box_id='b1'").fetchone()
    assert row[0] == "s2"


def test_reassign_live_to_different_shard_blocked(tmp_path):
    conn = _setup(tmp_path)
    _insert_box(conn, "b1", state="live", shard_id="s1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE ru_boxes SET shard_id='s2' WHERE box_id='b1'")
        conn.commit()


def test_reassign_terminated_to_different_shard_blocked(tmp_path):
    conn = _setup(tmp_path)
    _insert_box(conn, "b1", state="terminated", shard_id="s1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE ru_boxes SET shard_id='s2' WHERE box_id='b1'")
        conn.commit()


def test_setting_same_shard_id_on_live_is_a_noop(tmp_path):
    """OLD.shard_id IS NOT NEW.shard_id is false -> trigger does not fire."""
    conn = _setup(tmp_path)
    _insert_box(conn, "b1", state="live", shard_id="s1")
    conn.execute("UPDATE ru_boxes SET shard_id='s1' WHERE box_id='b1'")
    conn.commit()  # idempotent


# --- ru_boxes_terminated_keeps_shard ---

def test_terminate_with_shard_retained_allowed(tmp_path):
    conn = _setup(tmp_path)
    _insert_box(conn, "b1", state="live", shard_id="s1")
    conn.execute(
        "UPDATE ru_boxes SET state='terminated', terminated_at='2026-05-24T01:00:00Z' "
        "WHERE box_id='b1'"
    )
    conn.commit()
    row = conn.execute(
        "SELECT state, shard_id FROM ru_boxes WHERE box_id='b1'"
    ).fetchone()
    assert row == ("terminated", "s1")


def test_terminate_clearing_shard_blocked(tmp_path):
    conn = _setup(tmp_path)
    _insert_box(conn, "b1", state="live", shard_id="s1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE ru_boxes SET state='terminated', shard_id=NULL "
            "WHERE box_id='b1'"
        )
        conn.commit()


def test_terminate_unsharded_box_allowed(tmp_path):
    """Boxes that never got a shard can terminate as-is."""
    conn = _setup(tmp_path)
    _insert_box(conn, "b1", state="provisioning", shard_id=None)
    conn.execute(
        "UPDATE ru_boxes SET state='terminated' WHERE box_id='b1'"
    )
    conn.commit()
    row = conn.execute(
        "SELECT state, shard_id FROM ru_boxes WHERE box_id='b1'"
    ).fetchone()
    assert row == ("terminated", None)
