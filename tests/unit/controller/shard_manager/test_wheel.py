"""Tests for ShardReshuffleWheel — spec H §7.1."""
from __future__ import annotations

import json
import sqlite3

import pytest

from mthydra.controller.shard_manager.wheel import ShardReshuffleWheel
from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema


def _seed(tmp_path):
    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    return db, conn


def _ids_iter(prefix="g"):
    counter = iter(range(1000))

    def fn() -> str:
        return f"{prefix}{next(counter):03d}"

    return fn


def test_run_once_no_overdue_no_unassigned(tmp_path):
    db, conn = _seed(tmp_path)
    conn.close()
    wheel = ShardReshuffleWheel(
        db, target_size=2, max_size=3, reshuffle_interval_days=14,
        sweep_interval_seconds=3600, mode="offline",
        clock=lambda: "2026-05-24T00:00:00Z",
        shard_id_factory=_ids_iter(),
    )
    result = wheel.run_once()
    assert result == {"reshuffled": [], "folded_in": []}
    # Heartbeat row exists.
    conn2 = connect(db)
    row = conn2.execute(
        "SELECT obligation_id FROM obligation_clocks "
        "WHERE obligation_id='shard_reshuffle_sweep_ran'"
    ).fetchone()
    assert row is not None
    conn2.close()


def test_run_once_reshuffles_overdue_shard(tmp_path):
    db, conn = _seed(tmp_path)
    # Shard created 15d ago; TTL is 14d -> overdue.
    conn.execute(
        "INSERT INTO shards (shard_id, members_json, target_size, last_reshuffled_at, created_at) "
        "VALUES ('s1', ?, 2, '2026-05-01T00:00:00Z', '2026-05-01T00:00:00Z')",
        (json.dumps(["u1", "u2"]),),
    )
    for u in ["u1", "u2"]:
        conn.execute(
            "INSERT INTO users (user_id, display_name, out_of_band_channel, current_shard_id, added_at) "
            "VALUES (?, NULL, 'email', 's1', '2026-05-01T00:00:00Z')",
            (u,),
        )
    conn.commit()
    conn.close()

    wheel = ShardReshuffleWheel(
        db, target_size=2, max_size=3, reshuffle_interval_days=14,
        sweep_interval_seconds=3600, mode="offline",
        clock=lambda: "2026-05-21T00:00:00Z",
        shard_id_factory=_ids_iter(),
    )
    result = wheel.run_once()
    assert len(result["reshuffled"]) == 1
    new_sid = result["reshuffled"][0]

    conn2 = connect(db)
    # Old shard retired.
    old = conn2.execute(
        "SELECT retired_at FROM shards WHERE shard_id='s1'"
    ).fetchone()
    assert old[0] == "2026-05-21T00:00:00Z"
    # New shard active with same two users.
    members = json.loads(conn2.execute(
        "SELECT members_json FROM shards WHERE shard_id=?", (new_sid,)
    ).fetchone()[0])
    assert sorted(members) == ["u1", "u2"]
    # Users remapped.
    rows = conn2.execute(
        "SELECT user_id, current_shard_id FROM users ORDER BY user_id"
    ).fetchall()
    assert rows == [("u1", new_sid), ("u2", new_sid)]
    # shard_reshuffle_proven obligation set.
    proof = conn2.execute(
        "SELECT obligation_id FROM obligation_clocks "
        "WHERE obligation_id='shard_reshuffle_proven'"
    ).fetchone()
    assert proof is not None
    conn2.close()


def test_run_once_folds_unassigned(tmp_path):
    db, conn = _seed(tmp_path)
    for u in ["u1", "u2", "u3"]:
        conn.execute(
            "INSERT INTO users (user_id, display_name, out_of_band_channel, added_at) "
            "VALUES (?, NULL, 'email', '2026-05-24T00:00:00Z')",
            (u,),
        )
    conn.commit()
    conn.close()

    wheel = ShardReshuffleWheel(
        db, target_size=2, max_size=3, reshuffle_interval_days=14,
        sweep_interval_seconds=3600, mode="offline",
        clock=lambda: "2026-05-24T01:00:00Z",
        shard_id_factory=_ids_iter(),
    )
    result = wheel.run_once()
    assert len(result["folded_in"]) == 2  # 3 users / target 2 -> 2+1
    conn2 = connect(db)
    unassigned = conn2.execute(
        "SELECT COUNT(*) FROM users WHERE current_shard_id IS NULL"
    ).fetchone()[0]
    assert unassigned == 0
    conn2.close()


def test_run_once_clears_overdue_obligation(tmp_path):
    db, conn = _seed(tmp_path)
    conn.execute(
        "INSERT INTO shards (shard_id, members_json, target_size, last_reshuffled_at, created_at) "
        "VALUES ('s1', ?, 2, '2026-05-01T00:00:00Z', '2026-05-01T00:00:00Z')",
        (json.dumps(["u1"]),),
    )
    conn.execute(
        "INSERT INTO users (user_id, display_name, out_of_band_channel, current_shard_id, added_at) "
        "VALUES ('u1', NULL, 'email', 's1', '2026-05-01T00:00:00Z')"
    )
    # Pre-seed an overdue anti-obligation row from a prior sweep.
    conn.execute(
        "INSERT INTO obligation_clocks (obligation_id, last_proven_at, proven_by, next_due_at) "
        "VALUES ('shard_overdue_pending::s1', '2026-05-20T00:00:00Z', 'shard_reshuffle_sweep', "
        "'2026-05-20T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    wheel = ShardReshuffleWheel(
        db, target_size=2, max_size=3, reshuffle_interval_days=14,
        sweep_interval_seconds=3600, mode="offline",
        clock=lambda: "2026-05-21T00:00:00Z",
        shard_id_factory=_ids_iter(),
    )
    wheel.run_once()
    conn2 = connect(db)
    n = conn2.execute(
        "SELECT COUNT(*) FROM obligation_clocks "
        "WHERE obligation_id='shard_overdue_pending::s1'"
    ).fetchone()[0]
    assert n == 0
    conn2.close()


def test_offline_mode_does_not_arm(tmp_path):
    db, conn = _seed(tmp_path)
    conn.close()
    wheel = ShardReshuffleWheel(
        db, target_size=2, max_size=3, reshuffle_interval_days=14,
        sweep_interval_seconds=3600, mode="offline",
        clock=lambda: "2026-05-24T00:00:00Z",
    )
    wheel.arm()
    assert wheel._scheduler is None
    wheel.disarm()  # no-op


def test_arm_and_disarm_in_production_mode(tmp_path):
    db, conn = _seed(tmp_path)
    conn.close()
    wheel = ShardReshuffleWheel(
        db, target_size=2, max_size=3, reshuffle_interval_days=14,
        # Very large interval so the sweep never actually fires before disarm.
        sweep_interval_seconds=86400, mode="production",
        clock=lambda: "2026-05-24T00:00:00Z",
    )
    wheel.arm()
    assert wheel._scheduler is not None
    wheel.disarm()
    assert wheel._scheduler is None


def test_default_clock_and_default_shard_id_factory(tmp_path):
    """Smoke-cover the default callables used when tests don't override."""
    from mthydra.controller.shard_manager.wheel import (
        _default_clock,
        _default_shard_id,
    )
    ts = _default_clock()
    assert ts.endswith("Z") and "T" in ts
    sid = _default_shard_id()
    # uuid4 yields 36-char hex form.
    assert len(sid) == 36 and sid.count("-") == 4


def test_run_once_retires_empty_overdue_shard(tmp_path):
    """Edge case: an overdue shard with no members. Spec H §7.1 step retire-and-skip."""
    db, conn = _seed(tmp_path)
    conn.execute(
        "INSERT INTO shards (shard_id, members_json, target_size, last_reshuffled_at, created_at) "
        "VALUES ('s1', '[]', 2, '2026-05-01T00:00:00Z', '2026-05-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()
    wheel = ShardReshuffleWheel(
        db, target_size=2, max_size=3, reshuffle_interval_days=14,
        sweep_interval_seconds=3600, mode="offline",
        clock=lambda: "2026-05-21T00:00:00Z",
        shard_id_factory=_ids_iter(),
    )
    result = wheel.run_once()
    # No primary reshuffle row (empty shard short-circuits to retire).
    assert result["reshuffled"] == []
    conn2 = connect(db)
    retired_at = conn2.execute(
        "SELECT retired_at FROM shards WHERE shard_id='s1'"
    ).fetchone()[0]
    assert retired_at == "2026-05-21T00:00:00Z"
    conn2.close()


def test_run_once_handles_leftover_chunks(tmp_path):
    """Shard members > target_size: picker produces leftover chunks; each becomes its own shard."""
    import json as _json
    db, conn = _seed(tmp_path)
    # Shard with 4 members at target_size 2 — reshuffle yields 2 chunks (one
    # primary + one leftover).
    conn.execute(
        "INSERT INTO shards (shard_id, members_json, target_size, last_reshuffled_at, created_at) "
        "VALUES ('s1', ?, 2, '2026-05-01T00:00:00Z', '2026-05-01T00:00:00Z')",
        (_json.dumps(["u1", "u2", "u3", "u4"]),),
    )
    for u in ["u1", "u2", "u3", "u4"]:
        conn.execute(
            "INSERT INTO users (user_id, display_name, out_of_band_channel, current_shard_id, added_at) "
            "VALUES (?, NULL, 'email', 's1', '2026-05-01T00:00:00Z')",
            (u,),
        )
    conn.commit()
    conn.close()
    wheel = ShardReshuffleWheel(
        db, target_size=2, max_size=3, reshuffle_interval_days=14,
        sweep_interval_seconds=3600, mode="offline",
        clock=lambda: "2026-05-21T00:00:00Z",
        shard_id_factory=_ids_iter(),
    )
    result = wheel.run_once()
    # primary + leftover = 2 new shards from this reshuffle.
    assert len(result["reshuffled"]) == 2
    conn2 = connect(db)
    n_active = conn2.execute(
        "SELECT COUNT(*) FROM shards WHERE retired_at IS NULL"
    ).fetchone()[0]
    assert n_active == 2
    conn2.close()
