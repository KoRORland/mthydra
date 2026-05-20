"""Spec F — node_state singleton repository.

The node_state table is a single-row description of *this* node's runtime
role. It is the DB-authoritative source for active/standby; controller.toml
carries only deploy-time hints.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from mthydra.controller.state.audit import log_event

_VALID_ROLES = {"active", "standby"}


@dataclass(frozen=True)
class NodeState:
    role: str
    promoted_at: str | None
    previous_role: str | None
    promotion_case: str | None
    promotion_backup_generation: int | None


def current_node_state(conn: sqlite3.Connection) -> NodeState:
    row = conn.execute(
        "SELECT role, promoted_at, previous_role, promotion_case, "
        "       promotion_backup_generation "
        "FROM node_state WHERE rowid=1"
    ).fetchone()
    if row is None:
        raise LookupError("node_state singleton missing — DB not initialised")
    return NodeState(*row)


def set_node_role(
    conn: sqlite3.Connection,
    *,
    role: str,
    promoted_at: str | None,
    previous_role: str | None,
    promotion_case: str | None,
    promotion_backup_generation: int | None,
    actor: str = "operator",
) -> None:
    """Update the singleton row. Emits one audit_log entry.

    The CHECK constraints on the table reject invalid role / promotion_case;
    this function pre-validates to surface a friendlier ValueError.
    """
    if role not in _VALID_ROLES:
        raise ValueError(f"role must be one of {_VALID_ROLES}, got {role!r}")
    if promotion_case is not None and promotion_case not in {"A", "B"}:
        raise ValueError(f"promotion_case must be 'A' or 'B', got {promotion_case!r}")

    conn.execute(
        "UPDATE node_state SET role=?, promoted_at=?, previous_role=?, "
        "       promotion_case=?, promotion_backup_generation=? "
        "WHERE rowid=1",
        (role, promoted_at, previous_role, promotion_case, promotion_backup_generation),
    )
    log_event(
        conn,
        ts=promoted_at or "1970-01-01T00:00:00Z",
        actor=actor,
        action="node_role_set",
        target=role,
        details_json=json.dumps({
            "role": role,
            "promoted_at": promoted_at,
            "previous_role": previous_role,
            "promotion_case": promotion_case,
            "promotion_backup_generation": promotion_backup_generation,
        }, separators=(",", ":")),
    )
    conn.commit()
