"""Summarize a restored SQLite DB. Read-only — never modifies the file."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from mthydra.controller.state.db import connect


def summarize_db(db_path: Path | str) -> dict[str, Any]:
    """Return a dict with key metrics from the restored state DB (spec A §7.1 step 6)."""
    conn = connect(db_path, read_only=True)
    try:
        schema_row = conn.execute(
            "SELECT version FROM schema_version WHERE rowid=1"
        ).fetchone()
        latest_backup_row = conn.execute(
            "SELECT MAX(generation) FROM backup_log"
        ).fetchone()
        burned_count = conn.execute("SELECT COUNT(*) FROM burned_domains").fetchone()[0]
        live_boxes = conn.execute(
            "SELECT COUNT(*) FROM ru_boxes WHERE state='live'"
        ).fetchone()[0]
        pool_counts = dict(
            conn.execute(
                "SELECT state, COUNT(*) FROM cover_domain_pool GROUP BY state"
            ).fetchall()
        )
        latest_descriptor = conn.execute(
            "SELECT generation, valid_until FROM descriptor_history "
            "ORDER BY generation DESC LIMIT 1"
        ).fetchone()
        oldest_obligation = conn.execute(
            "SELECT obligation_id, last_proven_at FROM obligation_clocks "
            "ORDER BY last_proven_at ASC LIMIT 1"
        ).fetchone()
        return {
            "schema_version": schema_row[0] if schema_row else None,
            "latest_backup_generation": (
                latest_backup_row[0] if latest_backup_row and latest_backup_row[0] else None
            ),
            "burned_domains_count": burned_count,
            "ru_boxes_live": live_boxes,
            "cover_pool_candidate_unverified": pool_counts.get("candidate_unverified", 0),
            "cover_pool_candidate_verified": pool_counts.get("candidate_verified", 0),
            "cover_pool_in_use": pool_counts.get("in_use", 0),
            "latest_descriptor_generation": (
                latest_descriptor[0] if latest_descriptor else None
            ),
            "latest_descriptor_valid_until": (
                latest_descriptor[1] if latest_descriptor else None
            ),
            "oldest_obligation": oldest_obligation,
        }
    finally:
        conn.close()
