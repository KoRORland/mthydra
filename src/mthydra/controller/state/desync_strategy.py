"""Spec V V-D6 / invariant #36 — staged vs live desync strategy with a
canary-proven gate. A fleet-wide (live) strategy can only be set from a staged
candidate whose hash has been marked canary-proven."""
from __future__ import annotations

import hashlib
import sqlite3


class CanaryGateError(RuntimeError):
    pass


def _h(strategy: str) -> str:
    return hashlib.sha256(strategy.encode("utf-8")).hexdigest()


def stage(conn: sqlite3.Connection, strategy: str, *, at: str) -> None:
    conn.execute("UPDATE desync_strategy SET staged=?, updated_at=? WHERE id=1", (strategy, at))
    conn.commit()


def staged(conn: sqlite3.Connection) -> str | None:
    return conn.execute("SELECT staged FROM desync_strategy WHERE id=1").fetchone()[0]


def live(conn: sqlite3.Connection) -> str | None:
    return conn.execute("SELECT live FROM desync_strategy WHERE id=1").fetchone()[0]


def mark_canary_proven(conn: sqlite3.Connection, strategy: str, *, at: str) -> None:
    conn.execute(
        "UPDATE desync_strategy SET canary_proven_hash=?, updated_at=? WHERE id=1",
        (_h(strategy), at),
    )
    conn.commit()


def promote(conn: sqlite3.Connection, *, at: str) -> None:
    row = conn.execute(
        "SELECT staged, canary_proven_hash FROM desync_strategy WHERE id=1"
    ).fetchone()
    cand, proven = row
    if cand is None:
        raise CanaryGateError("no staged strategy to promote")
    if proven != _h(cand):
        raise CanaryGateError(
            "invariant #36: staged strategy is not canary-proven "
            "(stage it on a canary shard, confirm V5 handshake-health holds, "
            "then mark it proven before promoting)"
        )
    conn.execute("UPDATE desync_strategy SET live=?, updated_at=? WHERE id=1", (cand, at))
    conn.commit()
