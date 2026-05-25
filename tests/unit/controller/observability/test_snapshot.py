"""Tests for observability.snapshot — pure aggregator."""
from __future__ import annotations

import pytest

from mthydra.controller.observability.snapshot import (
    AntiObligationRow,
    EuNodeView,
    Snapshot,
    collect_snapshot,
)
from mthydra.controller.state.db import connect
from mthydra.controller.state.obligations import set_obligation
from mthydra.controller.state.schema import apply_schema


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "state.sqlite")
    apply_schema(c)
    yield c
    c.close()


NOW = "2026-05-25T12:00:00Z"


def test_collect_empty_database(conn):
    snap = collect_snapshot(conn, now=NOW)
    assert isinstance(snap, Snapshot)
    assert snap.obligations_healthy == ()
    assert snap.obligations_overdue == ()
    assert snap.anti_obligations == ()
    assert snap.eu_nodes == ()
    assert snap.counts.boxes_live == 0


def test_anti_per_target_classification(conn):
    set_obligation(conn,
                   obligation_id="probe_kill_pending::b1",
                   last_proven_at=NOW, proven_by="x", next_due_at=NOW,
                   details='{"verdict":"hard_kill"}')
    snap = collect_snapshot(conn, now=NOW)
    assert len(snap.anti_obligations) == 1
    a = snap.anti_obligations[0]
    assert isinstance(a, AntiObligationRow)
    assert a.kind == "probe_kill_pending"
    assert a.target == "b1"
    assert a.severity == "crit"


def test_anti_singleton_classification(conn):
    set_obligation(conn,
                   obligation_id="cover_pool_rotation_frozen",
                   last_proven_at=NOW, proven_by="x", next_due_at=NOW,
                   details=None)
    snap = collect_snapshot(conn, now=NOW)
    a = snap.anti_obligations[0]
    assert a.kind == "cover_pool_rotation_frozen"
    assert a.target is None
    assert a.severity == "crit"


def test_overdue_obligation_classified(conn):
    # Cadence-by-id map for probe_audit_sweep_ran is 3600.
    set_obligation(conn,
                   obligation_id="probe_audit_sweep_ran",
                   last_proven_at="2026-05-25T08:00:00Z",
                   proven_by="x",
                   next_due_at="2026-05-25T09:00:00Z",
                   details=None)
    snap = collect_snapshot(conn, now="2026-05-25T11:00:00Z")
    assert len(snap.obligations_overdue) == 1
    o = snap.obligations_overdue[0]
    assert o.obligation_id == "probe_audit_sweep_ran"
    # 2h overdue against 1h cadence -> crit
    assert o.severity == "crit"


def test_healthy_obligation_classified(conn):
    set_obligation(conn,
                   obligation_id="probe_audit_sweep_ran",
                   last_proven_at="2026-05-25T11:55:00Z",
                   proven_by="x",
                   next_due_at="2026-05-25T13:00:00Z",
                   details=None)
    snap = collect_snapshot(conn, now=NOW)
    assert [o.obligation_id for o in snap.obligations_healthy] == [
        "probe_audit_sweep_ran"
    ]
    assert snap.obligations_overdue == ()


def test_eu_node_fresh_heartbeat_info(conn):
    conn.execute(
        "INSERT INTO eu_nodes (node_id, hostname, provider, region, public_ip, "
        "role, added_at, last_heartbeat_at) "
        "VALUES ('eu1', 'h', 'p', 'r', '1.2.3.4', 'active', ?, ?)",
        (NOW, "2026-05-25T11:59:00Z"),
    )
    conn.commit()
    snap = collect_snapshot(conn, now=NOW, staleness_alert_seconds=600)
    assert len(snap.eu_nodes) == 1
    n = snap.eu_nodes[0]
    assert isinstance(n, EuNodeView)
    assert n.severity == "info"
    assert n.heartbeat_age_seconds == 60


def test_eu_node_stale_active_is_crit(conn):
    conn.execute(
        "INSERT INTO eu_nodes (node_id, hostname, provider, region, public_ip, "
        "role, added_at, last_heartbeat_at) "
        "VALUES ('eu1', 'h', 'p', 'r', '1.2.3.4', 'active', ?, ?)",
        (NOW, "2026-05-25T10:00:00Z"),
    )
    conn.commit()
    snap = collect_snapshot(conn, now=NOW, staleness_alert_seconds=600)
    assert snap.eu_nodes[0].severity == "crit"


def test_eu_node_stale_standby_is_warn(conn):
    conn.execute(
        "INSERT INTO eu_nodes (node_id, hostname, provider, region, public_ip, "
        "role, added_at, last_heartbeat_at) "
        "VALUES ('eu1', 'h', 'p', 'r', '1.2.3.4', 'standby', ?, ?)",
        (NOW, "2026-05-25T10:00:00Z"),
    )
    conn.commit()
    snap = collect_snapshot(conn, now=NOW, staleness_alert_seconds=600)
    assert snap.eu_nodes[0].severity == "warn"


def test_eu_node_null_heartbeat_treated_as_stale(conn):
    conn.execute(
        "INSERT INTO eu_nodes (node_id, hostname, provider, region, public_ip, "
        "role, added_at, last_heartbeat_at) "
        "VALUES ('eu1', 'h', 'p', 'r', '1.2.3.4', 'active', ?, NULL)",
        (NOW,),
    )
    conn.commit()
    snap = collect_snapshot(conn, now=NOW, staleness_alert_seconds=600)
    n = snap.eu_nodes[0]
    assert n.last_heartbeat_at is None
    assert n.heartbeat_age_seconds is None
    assert n.severity == "crit"


def test_fleet_counts_populated(conn):
    # Some boxes.
    conn.execute(
        "INSERT INTO ru_boxes (box_id, provider, region, sni, state, image_version, created_at) "
        "VALUES ('b1', 'p', 'r', 's1', 'live', 'v1', ?)", (NOW,),
    )
    conn.execute(
        "INSERT INTO ru_boxes (box_id, provider, region, sni, state, image_version, created_at) "
        "VALUES ('b2', 'p', 'r', 's2', 'provisioning', 'v1', ?)", (NOW,),
    )
    # An active vantage.
    conn.execute(
        "INSERT INTO probe_vantages (vantage_id, label, source_kind, state, added_at) "
        "VALUES ('vk', 'kz', 'cloud-cis', 'active', ?)", (NOW,),
    )
    # An active shard.
    conn.execute(
        "INSERT INTO shards (shard_id, members_json, target_size, last_reshuffled_at, created_at) "
        "VALUES ('s1', '[]', 2, ?, ?)", (NOW, NOW),
    )
    conn.commit()
    snap = collect_snapshot(conn, now=NOW)
    assert snap.counts.boxes_live == 1
    assert snap.counts.boxes_provisioning == 1
    assert snap.counts.active_vantages == 1
    assert snap.counts.active_shards == 1


def test_summary_line_present(conn):
    snap = collect_snapshot(conn, now=NOW)
    assert "obligations" in snap.summary_line
    assert "live boxes" in snap.summary_line


def test_dist_user_unregistered_classified_as_anti(conn):
    """Spec K amendment: dist_user_unregistered::* must be classified as anti."""
    from mthydra.controller.state.obligations import set_obligation
    set_obligation(conn,
                   obligation_id="dist_user_unregistered::u1",
                   last_proven_at=NOW, proven_by="x",
                   next_due_at=NOW, details=None)
    snap = collect_snapshot(conn, now=NOW)
    assert len(snap.anti_obligations) == 1
    a = snap.anti_obligations[0]
    assert a.kind == "dist_user_unregistered"
    assert a.target == "u1"
    assert a.severity == "warn"


def test_dist_user_heartbeat_breach_classified_as_crit(conn):
    from mthydra.controller.state.obligations import set_obligation
    set_obligation(conn,
                   obligation_id="dist_user_heartbeat_breach::u1",
                   last_proven_at=NOW, proven_by="x",
                   next_due_at=NOW, details=None)
    snap = collect_snapshot(conn, now=NOW)
    assert snap.anti_obligations[0].severity == "crit"


def test_probe_coverage_pending_graduates_to_crit(conn):
    # Insert a probe_coverage_pending whose last_proven_at is > 6h ago.
    set_obligation(conn,
                   obligation_id="probe_coverage_pending::b1",
                   last_proven_at="2026-05-25T04:00:00Z",
                   proven_by="x",
                   next_due_at="2026-05-25T04:00:00Z",
                   details=None)
    snap = collect_snapshot(conn, now=NOW)
    a = snap.anti_obligations[0]
    assert a.kind == "probe_coverage_pending"
    assert a.severity == "crit"
