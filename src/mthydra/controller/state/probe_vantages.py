"""Probe vantage registry — spec I §5.

State machine: candidate -> active -> retired (clean) | burned (irrecoverable).
Burned is monotonic (trigger enforces); labels of burned rows cannot be
reused (trigger enforces). Every transition writes an audit_log row.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from mthydra.controller.state import audit


@dataclass(frozen=True)
class ProbeVantage:
    vantage_id: str
    label: str
    source_kind: str
    region_hint: str | None
    state: str
    added_at: str
    attested_at: str | None
    last_used_at: str | None
    retired_at: str | None
    burned_at: str | None
    burn_reason: str | None
    notes: str | None


def _row(conn: sqlite3.Connection, vantage_id: str) -> ProbeVantage | None:
    r = conn.execute(
        "SELECT vantage_id, label, source_kind, region_hint, state, added_at, "
        "attested_at, last_used_at, retired_at, burned_at, burn_reason, notes "
        "FROM probe_vantages WHERE vantage_id=?",
        (vantage_id,),
    ).fetchone()
    return ProbeVantage(*r) if r else None


def set_ssh(conn, vantage_id: str, *, host: str, port: int, user: str,
            key_path: str, known_hosts_path: str) -> None:
    cur = conn.execute(
        "UPDATE probe_vantages SET ssh_host=?, ssh_port=?, ssh_user=?,"
        " ssh_key_path=?, ssh_known_hosts_path=? WHERE vantage_id=?",
        (host, port, user, key_path, known_hosts_path, vantage_id),
    )
    if cur.rowcount == 0:
        raise ValueError(f"no probe_vantages row for {vantage_id!r}")
    conn.commit()


def add_candidate(
    conn: sqlite3.Connection,
    *,
    vantage_id: str,
    label: str,
    source_kind: str,
    at: str,
    region_hint: str | None = None,
    notes: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO probe_vantages (vantage_id, label, source_kind, region_hint, "
        "state, added_at, notes) VALUES (?, ?, ?, ?, 'candidate', ?, ?)",
        (vantage_id, label, source_kind, region_hint, at, notes),
    )
    audit.log_event(
        conn, ts=at, actor="probe_harness", action="vantage_add",
        target=vantage_id,
        details_json=json.dumps({
            "label": label, "source_kind": source_kind,
            "region_hint": region_hint,
        }),
    )


def attest_active(
    conn: sqlite3.Connection,
    vantage_id: str,
    *,
    at: str,
    evidence: str | None = None,
) -> None:
    v = _row(conn, vantage_id)
    if v is None:
        raise LookupError(f"no vantage {vantage_id!r}")
    if v.state != "candidate":
        raise ValueError(
            f"vantage {vantage_id!r} cannot be attested from state={v.state!r}"
        )
    conn.execute(
        "UPDATE probe_vantages SET state='active', attested_at=? WHERE vantage_id=?",
        (at, vantage_id),
    )
    audit.log_event(
        conn, ts=at, actor="probe_harness", action="vantage_attest_active",
        target=vantage_id,
        details_json=json.dumps({"evidence": evidence}),
    )


def retire(
    conn: sqlite3.Connection,
    vantage_id: str,
    *,
    at: str,
    reason: str | None = None,
) -> None:
    v = _row(conn, vantage_id)
    if v is None:
        raise LookupError(f"no vantage {vantage_id!r}")
    if v.state not in ("candidate", "active"):
        raise ValueError(
            f"vantage {vantage_id!r} cannot be retired from state={v.state!r}"
        )
    conn.execute(
        "UPDATE probe_vantages SET state='retired', retired_at=? WHERE vantage_id=?",
        (at, vantage_id),
    )
    audit.log_event(
        conn, ts=at, actor="probe_harness", action="vantage_retire",
        target=vantage_id,
        details_json=json.dumps({"reason": reason}),
    )


def burn(
    conn: sqlite3.Connection,
    vantage_id: str,
    *,
    at: str,
    reason: str,
) -> None:
    v = _row(conn, vantage_id)
    if v is None:
        raise LookupError(f"no vantage {vantage_id!r}")
    if v.state == "burned":
        raise ValueError(f"vantage {vantage_id!r} already burned")
    conn.execute(
        "UPDATE probe_vantages SET state='burned', burned_at=?, burn_reason=? "
        "WHERE vantage_id=?",
        (at, reason, vantage_id),
    )
    audit.log_event(
        conn, ts=at, actor="probe_harness", action="vantage_burn",
        target=vantage_id,
        details_json=json.dumps({"reason": reason}),
    )


def list_by_state(
    conn: sqlite3.Connection, state: str | None = None,
) -> list[ProbeVantage]:
    if state is None:
        rows = conn.execute(
            "SELECT vantage_id, label, source_kind, region_hint, state, added_at, "
            "attested_at, last_used_at, retired_at, burned_at, burn_reason, notes "
            "FROM probe_vantages ORDER BY added_at, vantage_id"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT vantage_id, label, source_kind, region_hint, state, added_at, "
            "attested_at, last_used_at, retired_at, burned_at, burn_reason, notes "
            "FROM probe_vantages WHERE state=? ORDER BY added_at, vantage_id",
            (state,),
        ).fetchall()
    return [ProbeVantage(*r) for r in rows]


def get_vantage(conn: sqlite3.Connection, vantage_id: str) -> ProbeVantage:
    v = _row(conn, vantage_id)
    if v is None:
        raise LookupError(f"no vantage {vantage_id!r}")
    return v


def list_due_for_rotation(
    conn: sqlite3.Connection, *, now: str, ttl_days: int,
) -> list[str]:
    """Active vantages whose attestation is older than ttl_days."""
    from datetime import datetime, timezone

    def _to_s(iso: str) -> int:
        return int(
            datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=timezone.utc).timestamp()
        )

    now_s = _to_s(now)
    ttl = ttl_days * 86400
    rows = conn.execute(
        "SELECT vantage_id, attested_at FROM probe_vantages WHERE state='active'"
    ).fetchall()
    overdue: list[str] = []
    for vid, attested in rows:
        if attested is None:
            continue
        age = now_s - _to_s(attested)
        if age > ttl:
            overdue.append(vid)
    return sorted(overdue)
