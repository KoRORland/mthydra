"""Severity mapping — spec J §7.

Pure function: given the kind of obligation row (or the obligation_id),
the staleness, and the EU node role for EU-node sources, return the
severity bucket: 'info' | 'warn' | 'crit'.
"""
from __future__ import annotations


# Anti-obligation prefixes that always map to a fixed severity regardless of age.
_FIXED: dict[str, str] = {
    "probe_kill_pending":              "crit",
    "probe_evaluate_blocked":          "warn",
    "probe_vantage_rotation_pending":  "info",
    "cover_pool_rotation_pending":     "warn",
    "cover_pool_rotation_frozen":      "crit",
    "shard_overdue_pending":           "warn",
    "shard_unassigned_pending":        "info",
    "obs_dead_mans_switch_breach":     "crit",
    # Spec K — distribution channel anti-obligations (J severity table amendment).
    "dist_user_unregistered":          "warn",
    "dist_user_heartbeat_breach":      "crit",
}

# Age-graduated thresholds (seconds) for probe coverage gaps.
_PROBE_COVERAGE_CRIT_SECONDS = 6 * 3600


def severity_for_anti(kind: str, *, age_seconds: int = 0) -> str:
    """Severity for an anti-obligation row, keyed by its kind prefix."""
    if kind == "probe_coverage_pending":
        return "crit" if age_seconds >= _PROBE_COVERAGE_CRIT_SECONDS else "warn"
    return _FIXED.get(kind, "warn")


def severity_for_obligation_staleness(
    *, overdue_seconds: int, cadence_seconds: int,
) -> str:
    """For a regular *_proven / *_sweep_ran obligation that is overdue.

    Inside the cadence: 'info' (caller should not alert at all — the obligation
    is not yet overdue). Less than 2x: 'warn'. >= 2x: 'crit'.
    """
    if overdue_seconds <= 0:
        return "info"
    if overdue_seconds >= 2 * cadence_seconds:
        return "crit"
    return "warn"


def severity_for_eu_heartbeat(*, role: str, age_seconds: int,
                                staleness_alert_seconds: int) -> str:
    """EU node heartbeat staleness severity. Active nodes are crit; standby warn."""
    if age_seconds < staleness_alert_seconds:
        return "info"
    if role == "active":
        return "crit"
    return "warn"
