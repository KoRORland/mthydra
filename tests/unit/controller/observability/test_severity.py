"""Tests for observability.severity — pure mapping."""
from __future__ import annotations

from mthydra.controller.observability.severity import (
    severity_for_anti,
    severity_for_eu_heartbeat,
    severity_for_obligation_staleness,
)


def test_anti_fixed_kinds():
    assert severity_for_anti("probe_kill_pending") == "crit"
    assert severity_for_anti("probe_evaluate_blocked") == "warn"
    assert severity_for_anti("probe_vantage_rotation_pending") == "info"
    assert severity_for_anti("cover_pool_rotation_pending") == "warn"
    assert severity_for_anti("cover_pool_rotation_frozen") == "crit"
    assert severity_for_anti("shard_overdue_pending") == "warn"
    assert severity_for_anti("shard_unassigned_pending") == "info"
    assert severity_for_anti("obs_dead_mans_switch_breach") == "crit"


def test_anti_unknown_defaults_warn():
    assert severity_for_anti("future_kind_we_have_not_added") == "warn"


def test_anti_probe_coverage_graduated():
    assert severity_for_anti("probe_coverage_pending", age_seconds=0) == "warn"
    assert severity_for_anti("probe_coverage_pending", age_seconds=3000) == "warn"
    assert severity_for_anti("probe_coverage_pending", age_seconds=6 * 3600) == "crit"
    assert severity_for_anti("probe_coverage_pending", age_seconds=24 * 3600) == "crit"


def test_obligation_staleness():
    assert severity_for_obligation_staleness(
        overdue_seconds=0, cadence_seconds=3600,
    ) == "info"
    assert severity_for_obligation_staleness(
        overdue_seconds=1800, cadence_seconds=3600,
    ) == "warn"
    assert severity_for_obligation_staleness(
        overdue_seconds=3600, cadence_seconds=3600,
    ) == "warn"
    assert severity_for_obligation_staleness(
        overdue_seconds=7200, cadence_seconds=3600,
    ) == "crit"


def test_eu_heartbeat_fresh_is_info():
    assert severity_for_eu_heartbeat(
        role="active", age_seconds=60, staleness_alert_seconds=600,
    ) == "info"


def test_eu_heartbeat_active_stale_is_crit():
    assert severity_for_eu_heartbeat(
        role="active", age_seconds=900, staleness_alert_seconds=600,
    ) == "crit"


def test_eu_heartbeat_standby_stale_is_warn():
    assert severity_for_eu_heartbeat(
        role="standby", age_seconds=900, staleness_alert_seconds=600,
    ) == "warn"
