"""Tests for probe.evaluator — pure kill-decision logic."""
from __future__ import annotations

import pytest

from mthydra.controller.probe.evaluator import (
    EvaluationError,
    ProbeConfigView,
    evaluate_box,
)
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


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "state.sqlite")
    apply_schema(c)
    c.execute(
        "INSERT INTO ru_images (image_version, upstream_release, upstream_repo, "
        "binary_url, manifest_url, binary_sha256, binary_size_bytes, state, built_at) "
        "VALUES ('v1', 'r', 'r', 'u', 'm', 'sha', 1, 'candidate', '2026-05-25T00:00:00Z')"
    )
    c.execute(
        "INSERT INTO ru_boxes (box_id, provider, region, sni, state, image_version, created_at) "
        "VALUES ('b1', 'p', 'r', 'sni-b1', 'live', 'v1', '2026-05-25T00:00:00Z')"
    )
    pin(c, image_version="v1", profile_json='{}', recorded_by="op",
        at="2026-05-25T00:00:00Z")
    # Two active vantages so most tests can vary distinct counts.
    add_candidate(c, vantage_id="v1", label="kz1", source_kind="x",
                  at="2026-05-25T00:00:00Z")
    attest_active(c, "v1", at="2026-05-25T00:00:30Z")
    add_candidate(c, vantage_id="v2", label="by1", source_kind="x",
                  at="2026-05-25T00:00:00Z")
    attest_active(c, "v2", at="2026-05-25T00:00:30Z")
    yield c
    c.close()


def _rec(conn, vantage, status, ts, check="surface_scan"):
    record(conn, box_id="b1", vantage_id=vantage, cycle_at=ts,
           check_type=check, status=status, evidence_json=None,
           image_version="v1", recorded_at=ts)


def test_empty_history_is_healthy(conn):
    r = evaluate_box(conn, box_id="b1", cfg=CFG, now="2026-05-25T01:00:00Z")
    assert r.verdict == "healthy"
    assert r.offending_checks == ()
    assert r.distinct_vantages_consulted == 0


def test_one_hard_fail_kills(conn):
    _rec(conn, "v1", "hard_fail", "2026-05-25T01:00:00Z", check="tls_fall_through")
    r = evaluate_box(conn, box_id="b1", cfg=CFG, now="2026-05-25T01:01:00Z")
    assert r.verdict == "hard_kill"
    assert r.offending_checks == ("tls_fall_through",)
    assert len(r.evidence_pointer) == 1


def test_N_soft_across_distinct_vantages_reaches_threshold(conn):
    _rec(conn, "v1", "soft_fail", "2026-05-25T01:00:00Z", check="latency_loss")
    _rec(conn, "v2", "soft_fail", "2026-05-25T01:01:00Z", check="latency_loss")
    _rec(conn, "v1", "soft_fail", "2026-05-25T01:02:00Z", check="latency_loss")
    r = evaluate_box(conn, box_id="b1", cfg=CFG, now="2026-05-25T01:03:00Z")
    assert r.verdict == "soft_threshold_reached"
    assert r.distinct_vantages_consulted == 2


def test_N_soft_on_single_vantage_is_soft_pending(conn):
    """Three soft_fails but only one vantage — does not reach threshold."""
    for ts in ("2026-05-25T01:00:00Z", "2026-05-25T01:01:00Z", "2026-05-25T01:02:00Z"):
        _rec(conn, "v1", "soft_fail", ts, check="latency_loss")
    r = evaluate_box(conn, box_id="b1", cfg=CFG, now="2026-05-25T01:03:00Z")
    assert r.verdict == "soft_pending"
    assert r.distinct_vantages_consulted == 1


def test_under_N_is_soft_pending(conn):
    _rec(conn, "v1", "soft_fail", "2026-05-25T01:00:00Z")
    _rec(conn, "v2", "soft_fail", "2026-05-25T01:01:00Z")
    r = evaluate_box(conn, box_id="b1", cfg=CFG, now="2026-05-25T01:02:00Z")
    assert r.verdict == "soft_pending"


def test_passes_only_is_healthy(conn):
    for ts, v in [
        ("2026-05-25T01:00:00Z", "v1"),
        ("2026-05-25T01:01:00Z", "v2"),
        ("2026-05-25T01:02:00Z", "v1"),
    ]:
        _rec(conn, v, "pass", ts)
    r = evaluate_box(conn, box_id="b1", cfg=CFG, now="2026-05-25T01:03:00Z")
    assert r.verdict == "healthy"


def test_old_fails_outside_window_excluded(conn):
    """Window M=4 rows; recent passes push the old fail out of window."""
    _rec(conn, "v1", "soft_fail", "2026-05-25T00:00:00Z")
    for ts, v in [
        ("2026-05-25T01:00:00Z", "v1"),
        ("2026-05-25T01:01:00Z", "v2"),
        ("2026-05-25T01:02:00Z", "v1"),
        ("2026-05-25T01:03:00Z", "v2"),
    ]:
        _rec(conn, v, "pass", ts)
    r = evaluate_box(conn, box_id="b1", cfg=CFG, now="2026-05-25T01:04:00Z")
    assert r.verdict == "healthy"


def test_hard_kill_wins_over_soft_pending(conn):
    """A single hard_fail anywhere in the window dominates everything else."""
    _rec(conn, "v1", "soft_fail", "2026-05-25T01:00:00Z")
    _rec(conn, "v2", "hard_fail", "2026-05-25T01:01:00Z", check="cover_domain_consistency")
    r = evaluate_box(conn, box_id="b1", cfg=CFG, now="2026-05-25T01:02:00Z")
    assert r.verdict == "hard_kill"
    assert "cover_domain_consistency" in r.offending_checks


def test_missing_image_profile_raises(conn):
    """A box whose image_version lacks an image_profiles row -> EvaluationError."""
    # Provision a second box on a different image with no profile.
    conn.execute(
        "INSERT INTO ru_images (image_version, upstream_release, upstream_repo, "
        "binary_url, manifest_url, binary_sha256, binary_size_bytes, state, built_at) "
        "VALUES ('v2', 'r', 'r', 'u', 'm', 'sha2', 1, 'candidate', '2026-05-25T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO ru_boxes (box_id, provider, region, sni, state, image_version, created_at) "
        "VALUES ('b2', 'p', 'r', 'sni-b2', 'live', 'v2', '2026-05-25T00:00:00Z')"
    )
    conn.commit()
    with pytest.raises(EvaluationError, match="profile missing"):
        evaluate_box(conn, box_id="b2", cfg=CFG, now="2026-05-25T01:00:00Z")


def test_unknown_box_raises(conn):
    with pytest.raises(EvaluationError, match="unknown box"):
        evaluate_box(conn, box_id="nope", cfg=CFG, now="2026-05-25T01:00:00Z")
