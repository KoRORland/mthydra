"""Spec D — ru_images catalog repository.

The ru_images table tracks every binary we have built (`candidate`),
promoted to the provisioning default (`promoted`), or retired. State
transitions emit audit_log rows; `promote` is atomic within a single
transaction and re-stamps the t4 obligations.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from mthydra.controller.state.audit import log_event
from mthydra.controller.state.obligations import set_obligation


@dataclass(frozen=True)
class RUImage:
    image_version: str
    upstream_release: str
    upstream_repo: str
    binary_url: str
    manifest_url: str
    binary_sha256: str
    binary_size_bytes: int
    state: str
    built_at: str
    promoted_at: str | None
    retired_at: str | None
    notes: str | None


_COLS = (
    "image_version, upstream_release, upstream_repo, binary_url, manifest_url, "
    "binary_sha256, binary_size_bytes, state, built_at, "
    "promoted_at, retired_at, notes"
)


def insert_candidate(
    conn: sqlite3.Connection,
    *,
    image_version: str,
    upstream_release: str,
    upstream_repo: str,
    binary_url: str,
    manifest_url: str,
    binary_sha256: str,
    binary_size_bytes: int,
    built_at: str,
    notes: str | None = None,
    actor: str = "operator",
) -> None:
    """Insert a new candidate. Emits one audit row."""
    conn.execute(
        "INSERT INTO ru_images "
        "(image_version, upstream_release, upstream_repo, binary_url, manifest_url, "
        " binary_sha256, binary_size_bytes, state, built_at, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'candidate', ?, ?)",
        (image_version, upstream_release, upstream_repo, binary_url, manifest_url,
         binary_sha256, binary_size_bytes, built_at, notes),
    )
    log_event(
        conn, ts=built_at, actor=actor, action="image_built",
        target=image_version,
        details_json=json.dumps({
            "upstream_release": upstream_release,
            "upstream_repo": upstream_repo,
            "binary_sha256": binary_sha256,
        }, separators=(",", ":")),
    )
    conn.commit()


def promote(
    conn: sqlite3.Connection,
    image_version: str,
    *,
    at: str,
    evidence: str,
    actor: str = "operator",
) -> None:
    """Atomic candidate → promoted; prior promoted (if any) → retired.

    Re-stamps t4_image_promoted; clears t4_upstream_release_available::<tag>.
    """
    row = conn.execute(
        "SELECT state, upstream_release FROM ru_images WHERE image_version=?",
        (image_version,),
    ).fetchone()
    if row is None:
        raise LookupError(f"ru_image {image_version!r} not found")
    if row[0] != "candidate":
        raise ValueError(
            f"ru_image {image_version!r} is not a candidate (state={row[0]})"
        )
    upstream_release = row[1]

    prior = conn.execute(
        "SELECT image_version FROM ru_images WHERE state='promoted'"
    ).fetchone()
    prior_iv = prior[0] if prior is not None else None

    try:
        conn.execute("BEGIN")
        if prior_iv is not None:
            conn.execute(
                "UPDATE ru_images SET state='retired', retired_at=? "
                "WHERE image_version=?",
                (at, prior_iv),
            )
        conn.execute(
            "UPDATE ru_images SET state='promoted', promoted_at=? "
            "WHERE image_version=?",
            (at, image_version),
        )
        log_event(
            conn, ts=at, actor=actor, action="image_promoted",
            target=image_version,
            details_json=json.dumps({
                "evidence": evidence,
                "retired_predecessor": prior_iv,
            }, separators=(",", ":")),
        )
        next_due = _add_days_iso(at, 30)
        set_obligation(
            conn,
            obligation_id="t4_image_promoted",
            last_proven_at=at,
            proven_by="operator",
            next_due_at=next_due,
            details=image_version,
        )
        conn.execute(
            "DELETE FROM obligation_clocks WHERE obligation_id=?",
            (f"t4_upstream_release_available::{upstream_release}",),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def retire(
    conn: sqlite3.Connection,
    image_version: str,
    *,
    at: str,
    reason: str,
    actor: str = "operator",
) -> None:
    """state -> 'retired'. Legal from candidate or promoted. Re-retiring raises."""
    row = conn.execute(
        "SELECT state FROM ru_images WHERE image_version=?", (image_version,)
    ).fetchone()
    if row is None:
        raise LookupError(f"ru_image {image_version!r} not found")
    prior_state = row[0]
    if prior_state == "retired":
        raise ValueError(f"ru_image {image_version!r} is already retired")
    conn.execute(
        "UPDATE ru_images SET state='retired', retired_at=? WHERE image_version=?",
        (at, image_version),
    )
    log_event(
        conn, ts=at, actor=actor, action="image_retired",
        target=image_version,
        details_json=json.dumps({"reason": reason, "prior_state": prior_state},
                                separators=(",", ":")),
    )
    conn.commit()


def current_promoted(conn: sqlite3.Connection) -> RUImage | None:
    row = conn.execute(
        f"SELECT {_COLS} FROM ru_images WHERE state='promoted' LIMIT 1"
    ).fetchone()
    return RUImage(*row) if row is not None else None


def list_images(
    conn: sqlite3.Connection,
    *,
    state: str | None = None,
) -> list[RUImage]:
    if state is None:
        rows = conn.execute(
            f"SELECT {_COLS} FROM ru_images ORDER BY built_at DESC"
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {_COLS} FROM ru_images WHERE state=? ORDER BY built_at DESC",
            (state,),
        ).fetchall()
    return [RUImage(*r) for r in rows]


def get_image(conn: sqlite3.Connection, image_version: str) -> RUImage:
    row = conn.execute(
        f"SELECT {_COLS} FROM ru_images WHERE image_version=?", (image_version,)
    ).fetchone()
    if row is None:
        raise LookupError(f"ru_image {image_version!r} not found")
    return RUImage(*row)


def _add_days_iso(iso: str, days: int) -> str:
    from datetime import datetime, timedelta, timezone
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (t + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
