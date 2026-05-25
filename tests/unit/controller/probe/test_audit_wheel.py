"""Tests for ProbeAuditWheel — spec I §7.1."""
from __future__ import annotations

import json

import pytest

from mthydra.controller.probe.audit_wheel import ProbeAuditWheel
from mthydra.controller.probe.evaluator import ProbeConfigView
from mthydra.controller.state.db import connect
from mthydra.controller.state.image_profiles import pin
from mthydra.controller.state.probe_results import record
from mthydra.controller.state.probe_vantages import add_candidate, attest_active
from mthydra.controller.state.schema import apply_schema


CFG = ProbeConfigView(
    soft_fail_window_M=4,
    soft_fail_threshold_N=3,
    min_distinct_vantages=2,
)


def _seed_box(conn, box_id="b1", image_version="v1", with_profile=True):
    conn.execute(
        "INSERT OR IGNORE INTO ru_images (image_version, upstream_release, upstream_repo, "
        "binary_url, manifest_url, binary_sha256, binary_size_bytes, state, built_at) "
        "VALUES (?, 'r', 'r', 'u', 'm', 'sha', 1, 'candidate', '2026-05-25T00:00:00Z')",
        (image_version,),
    )
    conn.execute(
        "INSERT INTO ru_boxes (box_id, provider, region, sni, state, image_version, created_at) "
        "VALUES (?, 'p', 'r', ?, 'live', ?, '2026-05-25T00:00:00Z')",
        (box_id, f"sni-{box_id}", image_version),
    )
    if with_profile:
        pin(conn, image_version=image_version, profile_json='{}',
            recorded_by="op", at="2026-05-25T00:00:00Z")
    conn.commit()


def _seed_vantage(conn, vid, label, *, attested_at="2026-05-25T00:00:30Z"):
    add_candidate(conn, vantage_id=vid, label=label, source_kind="x",
                  at="2026-05-25T00:00:00Z")
    attest_active(conn, vid, at=attested_at)


def _wheel(db, sweep_seconds=300, ttl_days=14, coverage=3600,
           clock="2026-05-25T01:00:00Z"):
    return ProbeAuditWheel(
        db, cfg=CFG, coverage_window_seconds=coverage,
        probe_vantage_ttl_days=ttl_days,
        sweep_interval_seconds=sweep_seconds,
        mode="offline", clock=lambda: clock,
    )


def test_run_once_emits_kill_pending_on_hard_fail(tmp_path):
    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    _seed_box(conn)
    _seed_vantage(conn, "v1", "kz1")
    record(conn, box_id="b1", vantage_id="v1",
           cycle_at="2026-05-25T01:00:00Z",
           check_type="tls_fall_through", status="hard_fail",
           evidence_json=None, image_version="v1",
           recorded_at="2026-05-25T01:00:05Z")
    conn.close()
    wheel = _wheel(db, clock="2026-05-25T01:01:00Z")
    res = wheel.run_once()
    assert res["kill_pending"] == ["b1"]
    conn2 = connect(db)
    row = conn2.execute(
        "SELECT details FROM obligation_clocks "
        "WHERE obligation_id='probe_kill_pending::b1'"
    ).fetchone()
    assert row is not None
    details = json.loads(row[0])
    assert details["verdict"] == "hard_kill"
    conn2.close()


def test_run_once_clears_kill_when_box_no_longer_live(tmp_path):
    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    _seed_box(conn)
    _seed_vantage(conn, "v1", "kz1")
    record(conn, box_id="b1", vantage_id="v1",
           cycle_at="2026-05-25T01:00:00Z",
           check_type="tls_fall_through", status="hard_fail",
           evidence_json=None, image_version="v1",
           recorded_at="2026-05-25T01:00:05Z")
    conn.close()
    wheel = _wheel(db, clock="2026-05-25T01:01:00Z")
    wheel.run_once()  # creates the kill_pending row
    # Terminate the box (preserve shard_id via the v7 trigger, so just NULL it
    # would refuse — we set state=terminated with shard_id still NULL, which
    # is allowed because OLD.shard_id IS NULL).
    conn2 = connect(db)
    conn2.execute("UPDATE ru_boxes SET state='terminated' WHERE box_id='b1'")
    conn2.commit()
    conn2.close()
    wheel.run_once()  # should clear the stale kill_pending row
    conn3 = connect(db)
    n = conn3.execute(
        "SELECT COUNT(*) FROM obligation_clocks "
        "WHERE obligation_id='probe_kill_pending::b1'"
    ).fetchone()[0]
    assert n == 0
    conn3.close()


def test_run_once_emits_coverage_pending_when_stale(tmp_path):
    """Live box with no recent probes -> coverage_pending."""
    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    _seed_box(conn)
    conn.close()
    wheel = _wheel(db, coverage=3600, clock="2026-05-25T03:00:00Z")
    res = wheel.run_once()
    assert res["coverage_pending"] == ["b1"]


def test_run_once_clears_coverage_after_fresh_probe(tmp_path):
    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    _seed_box(conn)
    _seed_vantage(conn, "v1", "kz1")
    conn.close()
    wheel = _wheel(db, clock="2026-05-25T01:00:00Z")
    wheel.run_once()  # emits coverage_pending
    conn2 = connect(db)
    n_before = conn2.execute(
        "SELECT COUNT(*) FROM obligation_clocks "
        "WHERE obligation_id='probe_coverage_pending::b1'"
    ).fetchone()[0]
    assert n_before == 1
    record(conn2, box_id="b1", vantage_id="v1",
           cycle_at="2026-05-25T01:01:00Z",
           check_type="surface_scan", status="pass",
           evidence_json=None, image_version="v1",
           recorded_at="2026-05-25T01:01:05Z")
    conn2.close()
    # Re-run with a "now" close to the probe time so it's within window.
    wheel2 = _wheel(db, clock="2026-05-25T01:02:00Z")
    wheel2.run_once()
    conn3 = connect(db)
    n_after = conn3.execute(
        "SELECT COUNT(*) FROM obligation_clocks "
        "WHERE obligation_id='probe_coverage_pending::b1'"
    ).fetchone()[0]
    assert n_after == 0
    conn3.close()


def test_run_once_emits_evaluate_blocked_when_profile_missing(tmp_path):
    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    _seed_box(conn, with_profile=False)
    conn.close()
    wheel = _wheel(db, clock="2026-05-25T01:00:00Z")
    res = wheel.run_once()
    assert res["blocked"] == ["b1"]
    conn2 = connect(db)
    row = conn2.execute(
        "SELECT details FROM obligation_clocks "
        "WHERE obligation_id='probe_evaluate_blocked::b1'"
    ).fetchone()
    assert row is not None
    conn2.close()


def test_run_once_flags_vantage_past_ttl(tmp_path):
    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    _seed_vantage(conn, "vk", "kz1", attested_at="2026-05-01T00:00:00Z")
    conn.close()
    wheel = _wheel(db, ttl_days=14, clock="2026-05-25T00:00:00Z")
    res = wheel.run_once()
    assert res["rotation_pending"] == ["vk"]


def test_heartbeat_proven_each_tick(tmp_path):
    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    conn.close()
    wheel = _wheel(db, clock="2026-05-25T01:00:00Z")
    wheel.run_once()
    conn2 = connect(db)
    row = conn2.execute(
        "SELECT last_proven_at FROM obligation_clocks "
        "WHERE obligation_id='probe_audit_sweep_ran'"
    ).fetchone()
    assert row[0] == "2026-05-25T01:00:00Z"
    conn2.close()


def test_offline_mode_does_not_arm(tmp_path):
    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    conn.close()
    wheel = _wheel(db, sweep_seconds=300)
    wheel.arm()
    assert wheel._scheduler is None
    wheel.disarm()


def test_arm_and_disarm_in_production_mode(tmp_path):
    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    conn.close()
    wheel = ProbeAuditWheel(
        db, cfg=CFG, coverage_window_seconds=3600,
        probe_vantage_ttl_days=14,
        sweep_interval_seconds=86400, mode="production",
        clock=lambda: "2026-05-25T00:00:00Z",
    )
    wheel.arm()
    assert wheel._scheduler is not None
    wheel.disarm()
    assert wheel._scheduler is None
