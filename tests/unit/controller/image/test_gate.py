"""Tests for image.gate — pure validation gate evaluator."""
from __future__ import annotations

import pytest

from mthydra.controller.image.gate import (
    GateConfigView,
    evaluate_promotion_gate,
)
from mthydra.controller.state.db import connect
from mthydra.controller.state.image_profiles import pin
from mthydra.controller.state.probe_results import record
from mthydra.controller.state.probe_vantages import add_candidate, attest_active
from mthydra.controller.state.ru_boxes import insert_box, mark_live, mark_terminated
from mthydra.controller.state.schema import apply_schema


NOW = "2026-05-25T12:00:00Z"
CFG = GateConfigView(
    min_canary_boxes=1, min_cycles_per_box=4, min_distinct_vantages=2,
)


def _seed_image(conn, version="v_new", state="candidate"):
    conn.execute(
        "INSERT OR IGNORE INTO ru_images (image_version, upstream_release, upstream_repo, "
        "binary_url, manifest_url, binary_sha256, binary_size_bytes, state, built_at) "
        "VALUES (?, 'r', 'r', 'u', 'm', 'sha', 1, ?, ?)",
        (version, state, NOW),
    )
    conn.commit()


def _seed_vantage(conn, vid, label):
    add_candidate(conn, vantage_id=vid, label=label, source_kind="x", at=NOW)
    attest_active(conn, vid, at=NOW)


def _seed_canary(conn, box_id, image_version, *, state="live", canary=True):
    insert_box(conn, box_id, "p", "r", f"10.0.0.{ord(box_id[-1]) & 0xff}",
               f"sni-{box_id}", image_version, NOW, is_canary=canary)
    if state == "live":
        mark_live(conn, box_id, public_ip=f"10.0.0.{ord(box_id[-1]) & 0xff}", at=NOW)
    elif state == "terminated":
        mark_terminated(conn, box_id, reason="test", at=NOW)


def _record_cycles(conn, box_id, vid, image_version, count, *, status="pass"):
    for i in range(count):
        cycle = f"2026-05-25T1{i % 10}:00:00Z"
        record(conn, box_id=box_id, vantage_id=vid,
               cycle_at=cycle, check_type="surface_scan", status=status,
               evidence_json=None, image_version=image_version,
               recorded_at=cycle)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "state.sqlite")
    apply_schema(c)
    yield c
    c.close()


def test_no_profile_no_canary_fails(conn):
    _seed_image(conn)
    res = evaluate_promotion_gate(conn, "v_new", cfg=CFG)
    assert not res.passed
    # Two failures: profile missing + insufficient canary boxes.
    assert any("image_profiles row missing" in r for r in res.reasons)
    assert any("insufficient canary boxes" in r for r in res.reasons)


def test_profile_present_but_no_canary_fails(conn):
    _seed_image(conn)
    pin(conn, image_version="v_new", profile_json='{}', recorded_by="op", at=NOW)
    res = evaluate_promotion_gate(conn, "v_new", cfg=CFG)
    assert not res.passed
    assert all("image_profiles" not in r for r in res.reasons)
    assert any("insufficient canary boxes" in r for r in res.reasons)


def test_canary_with_too_few_cycles_fails(conn):
    _seed_image(conn)
    pin(conn, image_version="v_new", profile_json='{}', recorded_by="op", at=NOW)
    _seed_vantage(conn, "v1", "kz1")
    _seed_canary(conn, "b1", "v_new")
    _record_cycles(conn, "b1", "v1", "v_new", count=2)
    res = evaluate_promotion_gate(conn, "v_new", cfg=CFG)
    assert not res.passed
    assert any("below threshold" in r for r in res.reasons)


def test_canary_with_enough_cycles_one_vantage_fails(conn):
    """Cycles enough but only 1 vantage; need 2 distinct."""
    _seed_image(conn)
    pin(conn, image_version="v_new", profile_json='{}', recorded_by="op", at=NOW)
    _seed_vantage(conn, "v1", "kz1")
    _seed_canary(conn, "b1", "v_new")
    _record_cycles(conn, "b1", "v1", "v_new", count=4)
    res = evaluate_promotion_gate(conn, "v_new", cfg=CFG)
    assert not res.passed
    assert any("distinct_vantages=1" in r for r in res.reasons)


def test_canary_passes_full_gate(conn):
    _seed_image(conn)
    pin(conn, image_version="v_new", profile_json='{}', recorded_by="op", at=NOW)
    _seed_vantage(conn, "v1", "kz1")
    _seed_vantage(conn, "v2", "by1")
    _seed_canary(conn, "b1", "v_new")
    _record_cycles(conn, "b1", "v1", "v_new", count=2)
    _record_cycles(conn, "b1", "v2", "v_new", count=2)
    res = evaluate_promotion_gate(conn, "v_new", cfg=CFG)
    assert res.passed
    assert res.reasons == ()
    assert res.canary_probe_rows == 4
    assert res.canary_distinct_vantages == 2


def test_canary_with_kill_pending_fails(conn):
    _seed_image(conn)
    pin(conn, image_version="v_new", profile_json='{}', recorded_by="op", at=NOW)
    _seed_vantage(conn, "v1", "kz1")
    _seed_vantage(conn, "v2", "by1")
    _seed_canary(conn, "b1", "v_new")
    _record_cycles(conn, "b1", "v1", "v_new", count=2)
    _record_cycles(conn, "b1", "v2", "v_new", count=2)
    # Inject a probe_kill_pending row for this canary.
    conn.execute(
        "INSERT INTO obligation_clocks (obligation_id, last_proven_at, "
        "proven_by, next_due_at) "
        "VALUES ('probe_kill_pending::b1', ?, 'probe_audit_sweep', ?)",
        (NOW, NOW),
    )
    conn.commit()
    res = evaluate_promotion_gate(conn, "v_new", cfg=CFG)
    assert not res.passed
    assert any("pending kill" in r for r in res.reasons)
    assert "b1" in res.pending_kills


def test_terminated_canary_counts_in_cohort(conn):
    """A canary that died during soak still counts towards min_canary_boxes."""
    _seed_image(conn)
    pin(conn, image_version="v_new", profile_json='{}', recorded_by="op", at=NOW)
    _seed_canary(conn, "b1", "v_new", state="terminated")
    res = evaluate_promotion_gate(conn, "v_new", cfg=CFG)
    assert "b1" in res.canary_box_ids
    # Still fails on probe-cycle thresholds (no probes recorded), but the
    # cohort count passes.
    assert all("insufficient canary boxes" not in r for r in res.reasons)


def test_terminated_canary_kill_pending_ignored(conn):
    """A terminated canary with stale kill_pending should not block promotion
    — only live canaries' kill_pending counts."""
    _seed_image(conn)
    pin(conn, image_version="v_new", profile_json='{}', recorded_by="op", at=NOW)
    _seed_vantage(conn, "v1", "kz1")
    _seed_vantage(conn, "v2", "by1")
    _seed_canary(conn, "b1", "v_new", state="terminated")
    _record_cycles(conn, "b1", "v1", "v_new", count=2)
    _record_cycles(conn, "b1", "v2", "v_new", count=2)
    conn.execute(
        "INSERT INTO obligation_clocks (obligation_id, last_proven_at, "
        "proven_by, next_due_at) "
        "VALUES ('probe_kill_pending::b1', ?, 'probe_audit_sweep', ?)",
        (NOW, NOW),
    )
    conn.commit()
    res = evaluate_promotion_gate(conn, "v_new", cfg=CFG)
    # Cycle/vantage thresholds met, kill_pending on terminated box ignored.
    assert res.passed
    assert res.pending_kills == ()


def test_non_canary_box_with_image_version_ignored(conn):
    """A non-canary box from the same image_version must not be counted."""
    _seed_image(conn)
    pin(conn, image_version="v_new", profile_json='{}', recorded_by="op", at=NOW)
    _seed_canary(conn, "b1", "v_new", canary=False)
    res = evaluate_promotion_gate(conn, "v_new", cfg=CFG)
    assert res.canary_box_ids == ()
    assert any("insufficient canary boxes" in r for r in res.reasons)
