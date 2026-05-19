"""Crash-recovery reconciliation — spec A §9 + §10.11."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from mthydra.controller.backup.s3_dest import S3Destination
from mthydra.controller.state.backup_log import list_pending_reconciliation, record_index_updated
from mthydra.controller.state.db import connect


def reconcile_pending(db_path: Path, destination: S3Destination, clock: Callable[[], str]) -> int:
    """Resolve backup_log rows with pushed_at NOT NULL and index_updated_at NULL.

    For each such row:
      - HEAD the matching gen-NNN.age object in S3; if missing, leave the row
        (the next backup cycle will retry as a fresh generation).
      - HEAD index.json; if it already references a generation >= ours, just
        stamp our row as index-updated (idempotent).  Otherwise re-PUT index.json
        with our generation, then stamp.

    Returns the count of rows reconciled.
    """
    conn = connect(db_path)
    try:
        pending = list_pending_reconciliation(conn)
        resolved = 0
        for row in pending:
            if not destination.head_blob(row.generation):
                continue  # blob never made it; skip, will be a fresh gen next time
            index = destination.head_index()
            if index is not None and index.get("highest_gen", 0) >= row.generation:
                # index already reflects this generation (or a later one)
                record_index_updated(conn, row.generation, at=clock())
                resolved += 1
                continue
            # index is stale or absent — re-PUT with our generation
            destination.put_index(
                highest_gen=row.generation,
                sha256=row.sha256,
                size_bytes=row.size_bytes,
                ts=clock(),
            )
            record_index_updated(conn, row.generation, at=clock())
            resolved += 1
        return resolved
    finally:
        conn.close()
