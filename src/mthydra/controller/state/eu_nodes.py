"""Spec F — eu_nodes inventory repository."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from mthydra.controller.state.audit import log_event


@dataclass(frozen=True)
class EUNode:
    node_id: str
    hostname: str
    provider: str
    region: str
    public_ip: str | None
    role: str
    added_at: str
    promoted_at: str | None
    retired_at: str | None
    last_heartbeat_at: str | None
    last_heartbeat_b2_etag: str | None
    notes: str | None


_COLS = (
    "node_id, hostname, provider, region, public_ip, role, added_at, "
    "promoted_at, retired_at, last_heartbeat_at, last_heartbeat_b2_etag, notes"
)


def add_eu_node(
    conn: sqlite3.Connection,
    *,
    node_id: str,
    hostname: str,
    provider: str,
    region: str,
    added_at: str,
    role: str = "standby",
    public_ip: str | None = None,
    notes: str | None = None,
    actor: str = "operator",
) -> None:
    """Insert a new EU node row.

    Refuses to insert a second role='active' row (split-brain by definition).
    """
    if role == "active":
        existing = conn.execute(
            "SELECT node_id FROM eu_nodes WHERE role='active'"
        ).fetchone()
        if existing is not None:
            raise ValueError(
                f"only one active EU node permitted (existing: {existing[0]!r})"
            )
    conn.execute(
        "INSERT INTO eu_nodes (node_id, hostname, provider, region, public_ip, "
        "                      role, added_at, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (node_id, hostname, provider, region, public_ip, role, added_at, notes),
    )
    log_event(
        conn, ts=added_at, actor=actor, action="eu_node_added",
        target=node_id,
        details_json=json.dumps({
            "hostname": hostname, "provider": provider, "region": region,
            "role": role,
        }, separators=(",", ":")),
    )
    conn.commit()


def retire_eu_node(
    conn: sqlite3.Connection,
    node_id: str,
    *,
    at: str,
    actor: str = "operator",
) -> None:
    cur = conn.execute(
        "UPDATE eu_nodes SET role='retired', retired_at=? WHERE node_id=? AND role != 'retired'",
        (at, node_id),
    )
    if cur.rowcount == 0:
        raise ValueError(f"eu node {node_id!r} not found or already retired")
    log_event(
        conn, ts=at, actor=actor, action="eu_node_retired",
        target=node_id, details_json=None,
    )
    conn.commit()


def update_heartbeat(
    conn: sqlite3.Connection,
    node_id: str,
    *,
    at: str,
    b2_etag: str,
) -> None:
    """Update last_heartbeat_at + etag. Idempotent on identical etag (no audit churn)."""
    row = conn.execute(
        "SELECT last_heartbeat_b2_etag FROM eu_nodes WHERE node_id=?", (node_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"eu node {node_id!r} not in inventory")
    if row[0] == b2_etag:
        return  # no change; suppress audit
    conn.execute(
        "UPDATE eu_nodes SET last_heartbeat_at=?, last_heartbeat_b2_etag=? WHERE node_id=?",
        (at, b2_etag, node_id),
    )
    conn.commit()


def get_eu_node(conn: sqlite3.Connection, node_id: str) -> EUNode:
    row = conn.execute(
        f"SELECT {_COLS} FROM eu_nodes WHERE node_id=?", (node_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"eu node {node_id!r} not found")
    return EUNode(*row)


def set_data_exit_identity(
    conn: sqlite3.Connection,
    node_id: str,
    *,
    cover_sni: str,
    reality_pubkey: str,
) -> None:
    """Set eu_nodes.cover_sni + reality_pubkey. Raises KeyError if node absent."""
    n = conn.execute(
        "UPDATE eu_nodes SET cover_sni=?, reality_pubkey=? WHERE node_id=?",
        (cover_sni, reality_pubkey, node_id),
    ).rowcount
    if n == 0:
        raise KeyError(f"eu_node {node_id!r} not found")
    conn.commit()


def set_data_exit_state(
    conn: sqlite3.Connection,
    node_id: str,
    *,
    state: str,
    started_at: str | None = None,
) -> None:
    """Set data_exit_state (and optionally started_at). Validates the enum."""
    if state not in ("healthy", "degraded", "stopped"):
        raise ValueError(f"invalid data_exit_state: {state!r}")
    if started_at is None:
        conn.execute(
            "UPDATE eu_nodes SET data_exit_state=? WHERE node_id=?",
            (state, node_id),
        )
    else:
        conn.execute(
            "UPDATE eu_nodes SET data_exit_state=?, data_exit_started_at=? "
            "WHERE node_id=?",
            (state, started_at, node_id),
        )
    conn.commit()


def get_node(conn: sqlite3.Connection, node_id: str) -> dict | None:
    """Fetch an eu_node row as a dict including v6 data-exit columns.

    Returns None if the node does not exist.
    """
    row = conn.execute(
        "SELECT node_id, hostname, provider, region, public_ip, role, "
        "added_at, promoted_at, retired_at, last_heartbeat_at, "
        "last_heartbeat_b2_etag, notes, cover_sni, reality_pubkey, "
        "data_exit_state, data_exit_started_at "
        "FROM eu_nodes WHERE node_id=?",
        (node_id,),
    ).fetchone()
    if row is None:
        return None
    cols = (
        "node_id", "hostname", "provider", "region", "public_ip", "role",
        "added_at", "promoted_at", "retired_at", "last_heartbeat_at",
        "last_heartbeat_b2_etag", "notes", "cover_sni", "reality_pubkey",
        "data_exit_state", "data_exit_started_at",
    )
    return dict(zip(cols, row))


def list_eu_nodes(
    conn: sqlite3.Connection,
    *,
    role: str | None = None,
) -> list[EUNode]:
    if role is None:
        rows = conn.execute(
            f"SELECT {_COLS} FROM eu_nodes ORDER BY node_id"
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {_COLS} FROM eu_nodes WHERE role=? ORDER BY node_id", (role,)
        ).fetchall()
    return [EUNode(*r) for r in rows]
