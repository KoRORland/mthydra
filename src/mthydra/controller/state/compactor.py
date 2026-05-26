"""Append-only log compactor — spec M.

Three tables (alert_log, probe_results, distribution_log) are append-only
via triggers from specs J/I/K. Spec M adds a compactor_marker sentinel
table: when a row exists for the table being compacted, the trigger
short-circuits, allowing the DELETE. The marker is held only for the
duration of the compaction transaction.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from mthydra.controller.state.audit import log_event


@dataclass(frozen=True)
class CompactionResult:
    table_name: str
    cutoff: str
    dry_run: bool
    deleted: int


_CUTOFF_COL: dict[str, str] = {
    "alert_log": "attempted_at",
    "probe_results": "cycle_at",
    "distribution_log": "attempted_at",
    # Spec J2: alert_acks are forensic-relevant only while active; once
    # expired they may be compacted.
    "alert_acks": "expires_at",
}


def _compact(
    conn: sqlite3.Connection,
    table_name: str,
    *,
    before: str,
    dry_run: bool,
    actor: str,
) -> CompactionResult:
    if table_name not in _CUTOFF_COL:
        raise ValueError(f"unknown compactable table {table_name!r}")
    col = _CUTOFF_COL[table_name]
    count_sql = f"SELECT COUNT(*) FROM {table_name} WHERE {col} < ?"  # noqa: S608
    count = int(conn.execute(count_sql, (before,)).fetchone()[0])
    if dry_run:
        log_event(
            conn,
            ts=_now(),
            actor=actor,
            action="log_compact_dry_run",
            target=table_name,
            details_json=json.dumps({
                "cutoff": before,
                "would_delete": count,
            }),
        )
        return CompactionResult(
            table_name=table_name, cutoff=before,
            dry_run=True, deleted=count,
        )

    # Real delete: acquire the marker, delete, release in one transaction.
    try:
        conn.execute(
            "INSERT INTO compactor_marker (table_name, acquired_at, acquired_by) "
            "VALUES (?, ?, ?)",
            (table_name, _now(), actor),
        )
        delete_sql = f"DELETE FROM {table_name} WHERE {col} < ?"  # noqa: S608
        conn.execute(delete_sql, (before,))
        conn.execute(
            "DELETE FROM compactor_marker WHERE table_name=?", (table_name,),
        )
        log_event(
            conn,
            ts=_now(),
            actor=actor,
            action="log_compact",
            target=table_name,
            details_json=json.dumps({
                "cutoff": before,
                "deleted": count,
            }),
        )
        conn.commit()
    except sqlite3.IntegrityError as e:
        # PRIMARY KEY collision on compactor_marker -> another compactor is
        # running for the same table.
        raise RuntimeError(
            f"another compaction is in progress for {table_name!r}: {e}"
        ) from e
    except Exception:
        # On any other failure, release the marker so we don't leak privilege.
        conn.execute(
            "DELETE FROM compactor_marker WHERE table_name=?", (table_name,),
        )
        conn.commit()
        raise

    return CompactionResult(
        table_name=table_name, cutoff=before,
        dry_run=False, deleted=count,
    )


def compact_alert_log(
    conn: sqlite3.Connection, *, before: str, dry_run: bool, actor: str,
) -> CompactionResult:
    return _compact(conn, "alert_log", before=before, dry_run=dry_run, actor=actor)


def compact_probe_results(
    conn: sqlite3.Connection, *, before: str, dry_run: bool, actor: str,
) -> CompactionResult:
    return _compact(conn, "probe_results", before=before, dry_run=dry_run, actor=actor)


def compact_distribution_log(
    conn: sqlite3.Connection, *, before: str, dry_run: bool, actor: str,
) -> CompactionResult:
    return _compact(conn, "distribution_log", before=before,
                     dry_run=dry_run, actor=actor)


def compact_alert_acks(
    conn: sqlite3.Connection, *, before: str, dry_run: bool, actor: str,
) -> CompactionResult:
    return _compact(conn, "alert_acks", before=before, dry_run=dry_run, actor=actor)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
