"""Tests for alerter._build_decisions — human-readable subject/body output."""
from __future__ import annotations

from mthydra.controller.observability.alerter import _build_decisions
from mthydra.controller.observability.snapshot import (
    AntiObligationRow,
    EuNodeView,
    FleetCounts,
    ObligationStatus,
    Snapshot,
)

_EMPTY_COUNTS = FleetCounts(0, 0, 0, 0, 0, 0, 0)


def _snap(*, antis=(), overdue=(), eu=()) -> Snapshot:
    return Snapshot(
        collected_at="2026-06-03T16:00:00Z",
        obligations_healthy=(),
        obligations_overdue=tuple(overdue),
        anti_obligations=tuple(antis),
        eu_nodes=tuple(eu),
        counts=_EMPTY_COUNTS,
        summary_line="",
    )


def test_frozen_anti_is_human_readable():
    a = AntiObligationRow(
        obligation_id="cover_pool_rotation_frozen",
        last_proven_at="2026-06-03T15:01:44Z",
        details='{"candidate_verified": 0, "freeze_threshold": 1}',
        kind="cover_pool_rotation_frozen", target=None, severity="crit",
    )
    (sev, dedupe, kind, target, subject, body), = _build_decisions(_snap(antis=[a]), 600)
    # Subject is plain language, no raw kind/underscores leaking.
    assert subject == "CRITICAL: Cover-domain rotation is paused"
    # dedupe_key / kind unchanged (routing + dedupe rely on them).
    assert dedupe == "cover_pool_rotation_frozen"
    assert kind == "cover_pool_rotation_frozen"
    # Body: no raw column names; humanised detail keys; actionable line.
    assert "obligation_id:" not in body
    assert "Candidate verified: 0" in body
    assert "Freeze threshold: 1" in body
    assert "What to do:" in body
    assert "cover-attest-verified" in body


def test_overdue_is_human_readable():
    o = ObligationStatus(
        obligation_id="backup_integrity_proven",
        last_proven_at="2026-05-20T00:00:00Z",
        next_due_at="2026-05-27T00:00:00Z",
        overdue_seconds=2 * 86400,
        severity="warn",
    )
    (sev, dedupe, kind, target, subject, body), = _build_decisions(
        _snap(overdue=[o]), 600)
    assert subject.startswith("Warning: Scheduled task is overdue")
    assert "Overdue by: 2 days." in body
    assert "What to do:" in body
    assert "backup-integrity-now" in body  # from the existing remediation map


def test_eu_heartbeat_body_reads_never_and_not_reported():
    n = EuNodeView(
        node_id="eu-standby-de-1", role="standby",
        last_heartbeat_at=None, heartbeat_age_seconds=None,
        data_exit_state=None, severity="warn",
    )
    (sev, dedupe, kind, target, subject, body), = _build_decisions(_snap(eu=[n]), 600)
    assert subject == "Warning: EU node heartbeat is stale — eu-standby-de-1"
    assert "Last heartbeat: never." in body
    assert "Data-exit state: not reported." in body
    # Reads as a sentence, not a raw column dump.
    assert "(role: standby) hasn't been seen recently." in body
    assert "last_heartbeat_at:" not in body
    assert "What to do:" in body
