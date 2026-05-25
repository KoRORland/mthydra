"""Probe result ingest — spec I §5.

Append-only by trigger. record() also refreshes probe_vantages.last_used_at
and refuses non-active vantages (CLI-level guard against operator typos).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class ProbeResult:
    id: int
    box_id: str
    vantage_id: str
    cycle_at: str
    check_type: str
    status: str
    evidence_json: str | None
    image_version: str
    recorded_at: str


_CHECK_TYPES = {
    "tls_fall_through",
    "cover_domain_consistency",
    "surface_scan",
    "valid_path_liveness",
    "latency_loss",
    "behavioural_identity",
}
_STATUSES = {"pass", "soft_fail", "hard_fail"}


def record(
    conn: sqlite3.Connection,
    *,
    box_id: str,
    vantage_id: str,
    cycle_at: str,
    check_type: str,
    status: str,
    evidence_json: str | None,
    image_version: str,
    recorded_at: str,
) -> int:
    """Append one probe_results row. Refuses if vantage state != 'active'."""
    if check_type not in _CHECK_TYPES:
        raise ValueError(f"unknown check_type {check_type!r}")
    if status not in _STATUSES:
        raise ValueError(f"unknown status {status!r}")
    row = conn.execute(
        "SELECT state FROM probe_vantages WHERE vantage_id=?", (vantage_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"no vantage {vantage_id!r}")
    if row[0] != "active":
        raise ValueError(
            f"vantage {vantage_id!r} state={row[0]!r}; only active vantages may record"
        )
    cur = conn.execute(
        "INSERT INTO probe_results (box_id, vantage_id, cycle_at, check_type, status, "
        "evidence_json, image_version, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (box_id, vantage_id, cycle_at, check_type, status,
         evidence_json, image_version, recorded_at),
    )
    conn.execute(
        "UPDATE probe_vantages SET last_used_at=? WHERE vantage_id=?",
        (recorded_at, vantage_id),
    )
    conn.commit()
    return int(cur.lastrowid)


def recent_for_box(
    conn: sqlite3.Connection, box_id: str, *, limit: int = 50,
) -> list[ProbeResult]:
    rows = conn.execute(
        "SELECT id, box_id, vantage_id, cycle_at, check_type, status, "
        "evidence_json, image_version, recorded_at FROM probe_results "
        "WHERE box_id=? ORDER BY cycle_at DESC, id DESC LIMIT ?",
        (box_id, limit),
    ).fetchall()
    return [ProbeResult(*r) for r in rows]


def last_cycle_at(conn: sqlite3.Connection, box_id: str) -> str | None:
    r = conn.execute(
        "SELECT MAX(cycle_at) FROM probe_results WHERE box_id=?", (box_id,)
    ).fetchone()
    return r[0] if r and r[0] else None


def distinct_vantages_in_window(
    conn: sqlite3.Connection, box_id: str, *, window_seconds: int, now: str,
) -> list[str]:
    """Return vantages that submitted at least one row in (now-window, now]."""
    from datetime import datetime, timedelta, timezone

    now_dt = datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    start = (now_dt - timedelta(seconds=window_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        "SELECT DISTINCT vantage_id FROM probe_results "
        "WHERE box_id=? AND cycle_at >= ? AND cycle_at <= ? ORDER BY vantage_id",
        (box_id, start, now),
    ).fetchall()
    return [r[0] for r in rows]
