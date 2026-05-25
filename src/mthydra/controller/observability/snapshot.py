"""Pure snapshot aggregator — spec J §4.

Walks obligation_clocks, eu_nodes, ru_boxes, cover_domain_pool, shards,
probe_vantages. Returns a structured immutable view. No I/O outside the
read transaction.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from mthydra.controller.observability.severity import (
    severity_for_anti,
    severity_for_eu_heartbeat,
    severity_for_obligation_staleness,
)


# Anti-obligation single-row keys (no "::" suffix).
_SINGLETON_ANTI_KEYS = frozenset({
    "cover_pool_rotation_frozen",
    "obs_dead_mans_switch_breach",
})

# Prefixes used to identify anti-obligation per-target rows: "<prefix>::<target>".
_ANTI_PREFIXES = frozenset({
    "probe_kill_pending",
    "probe_evaluate_blocked",
    "probe_coverage_pending",
    "probe_vantage_rotation_pending",
    "cover_pool_rotation_pending",
    "shard_overdue_pending",
    "shard_unassigned_pending",
    # Spec K — distribution channel anti-obligations (J snapshot amendment).
    "dist_user_unregistered",
    "dist_user_heartbeat_breach",
})


@dataclass(frozen=True)
class ObligationStatus:
    obligation_id: str
    last_proven_at: str
    next_due_at: str
    overdue_seconds: int            # 0 if not overdue
    severity: str                   # 'info' | 'warn' | 'crit'


@dataclass(frozen=True)
class AntiObligationRow:
    obligation_id: str
    last_proven_at: str
    details: str | None
    kind: str
    target: str | None
    severity: str


@dataclass(frozen=True)
class EuNodeView:
    node_id: str
    role: str
    last_heartbeat_at: str | None
    heartbeat_age_seconds: int | None
    data_exit_state: str | None
    severity: str


@dataclass(frozen=True)
class FleetCounts:
    boxes_provisioning: int
    boxes_live: int
    boxes_terminated: int
    cover_domains_in_use: int
    cover_domains_burned: int
    active_vantages: int
    active_shards: int


@dataclass(frozen=True)
class Snapshot:
    collected_at: str
    obligations_healthy: tuple[ObligationStatus, ...]
    obligations_overdue: tuple[ObligationStatus, ...]
    anti_obligations: tuple[AntiObligationRow, ...]
    eu_nodes: tuple[EuNodeView, ...]
    counts: FleetCounts
    summary_line: str


def _parse_iso(ts: str) -> int:
    return int(
        datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
        .replace(tzinfo=timezone.utc).timestamp()
    )


def _classify_obligation(obligation_id: str) -> tuple[str, str, str | None] | None:
    """Return (kind, role_in_layout, target) for an anti-obligation row, or None.

    Layout 'singleton' -> the whole key IS the kind, no target.
    Layout 'per_target' -> prefix '::' target.
    """
    if obligation_id in _SINGLETON_ANTI_KEYS:
        return obligation_id, "singleton", None
    if "::" in obligation_id:
        prefix, _, target = obligation_id.partition("::")
        if prefix in _ANTI_PREFIXES:
            return prefix, "per_target", target
    return None


# Cadence (seconds) for the regular *_proven / *_sweep_ran obligations.
# Used to derive staleness severity (J severity §7).
_DEFAULT_CADENCE: dict[str, int] = {
    "probe_audit_sweep_ran": 3600,
    "shard_reshuffle_sweep_ran": 3600,
    "cover_pool_reverify_sweep_ran": 3600,
    "cover_pool_rotation_sweep_ran": 3600,
    "obs_alerter_sweep_ran": 3600,
    "obs_heartbeat_proven": 7200,
    "shard_reshuffle_proven": 28 * 86400,
    "shard_disjointness_check_proven": 86400,
    "probe_coverage_proven": 7200,
    "probe_vantage_rotation_proven": 28 * 86400,
    "cover_pool_reverify_pass_proven": 60 * 86400,
    "cover_pool_replenishment_proven": 90 * 86400,
}


def _cadence_for(obligation_id: str, next_due_at: str, last_proven_at: str) -> int:
    """Use the known cadence map; fall back to (next_due - last_proven)."""
    if obligation_id in _DEFAULT_CADENCE:
        return _DEFAULT_CADENCE[obligation_id]
    try:
        return max(60, _parse_iso(next_due_at) - _parse_iso(last_proven_at))
    except Exception:
        return 3600


def collect_snapshot(
    conn: sqlite3.Connection, *, now: str,
    staleness_alert_seconds: int = 600,
) -> Snapshot:
    now_s = _parse_iso(now)

    healthy: list[ObligationStatus] = []
    overdue: list[ObligationStatus] = []
    antis: list[AntiObligationRow] = []

    rows = conn.execute(
        "SELECT obligation_id, last_proven_at, next_due_at, details "
        "FROM obligation_clocks"
    ).fetchall()
    for ob_id, last_proven_at, next_due_at, details in rows:
        cls = _classify_obligation(ob_id)
        if cls is not None:
            kind, layout, target = cls
            age = max(0, now_s - _parse_iso(last_proven_at))
            sev = severity_for_anti(kind, age_seconds=age)
            antis.append(AntiObligationRow(
                obligation_id=ob_id,
                last_proven_at=last_proven_at,
                details=details,
                kind=kind, target=target, severity=sev,
            ))
            continue
        try:
            due_s = _parse_iso(next_due_at)
        except ValueError:
            due_s = now_s
        overdue_seconds = max(0, now_s - due_s)
        cadence = _cadence_for(ob_id, next_due_at, last_proven_at)
        sev = severity_for_obligation_staleness(
            overdue_seconds=overdue_seconds, cadence_seconds=cadence,
        )
        status = ObligationStatus(
            obligation_id=ob_id,
            last_proven_at=last_proven_at,
            next_due_at=next_due_at,
            overdue_seconds=overdue_seconds,
            severity=sev,
        )
        if overdue_seconds > 0:
            overdue.append(status)
        else:
            healthy.append(status)

    eu_views: list[EuNodeView] = []
    eu_rows = conn.execute(
        "SELECT node_id, role, last_heartbeat_at, data_exit_state FROM eu_nodes "
        "WHERE role IN ('active','standby')"
    ).fetchall()
    for node_id, role, last_hb, de_state in eu_rows:
        age: int | None = None
        if last_hb is not None:
            age = max(0, now_s - _parse_iso(last_hb))
        sev = severity_for_eu_heartbeat(
            role=role,
            age_seconds=age if age is not None else 10 ** 9,
            staleness_alert_seconds=staleness_alert_seconds,
        )
        eu_views.append(EuNodeView(
            node_id=node_id, role=role,
            last_heartbeat_at=last_hb,
            heartbeat_age_seconds=age,
            data_exit_state=de_state, severity=sev,
        ))

    def _count(sql: str, *params) -> int:
        return int(conn.execute(sql, params).fetchone()[0])

    counts = FleetCounts(
        boxes_provisioning=_count(
            "SELECT COUNT(*) FROM ru_boxes WHERE state='provisioning'"
        ),
        boxes_live=_count("SELECT COUNT(*) FROM ru_boxes WHERE state='live'"),
        boxes_terminated=_count(
            "SELECT COUNT(*) FROM ru_boxes WHERE state='terminated'"
        ),
        cover_domains_in_use=_count(
            "SELECT COUNT(*) FROM cover_domain_pool WHERE state='in_use'"
        ),
        cover_domains_burned=_count("SELECT COUNT(*) FROM burned_domains"),
        active_vantages=_count(
            "SELECT COUNT(*) FROM probe_vantages WHERE state='active'"
        ),
        active_shards=_count("SELECT COUNT(*) FROM shards WHERE retired_at IS NULL"),
    )

    summary_line = (
        f"obligations: {len(healthy)} green, {len(overdue)} overdue; "
        f"anti: {len(antis)}; eu: {len(eu_views)} nodes; "
        f"live boxes: {counts.boxes_live}"
    )

    return Snapshot(
        collected_at=now,
        obligations_healthy=tuple(healthy),
        obligations_overdue=tuple(overdue),
        anti_obligations=tuple(antis),
        eu_nodes=tuple(eu_views),
        counts=counts,
        summary_line=summary_line,
    )
