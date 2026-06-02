"""Reclaim a never-live provisioning box without burning its cover domain.

provision_box() commits the ru_boxes row AND flips the cover domain
candidate_verified -> in_use before any VM exists. If provisioning crashes after
that commit (e.g. the caller dies), the box is stranded in state='provisioning'
holding a cover domain that ru-box-terminate would BURN.

A box in state='provisioning' never went live — only ru_boxes.mark_live flips it
to 'live' and records public_ip — so its SNI was never exposed on a real VM.
reclaim_box() therefore returns the cover domain in_use -> candidate_verified
(safe to reuse) and marks the box terminated, all in one transaction. It refuses
boxes that are 'live' (the SNI was exposed; the operator must ru-box-terminate,
which burns it).
"""
from __future__ import annotations

import json
import sqlite3


class ReclaimError(RuntimeError):
    """Raised when a box cannot be reclaimed."""


def reclaim_box(
    conn: sqlite3.Connection,
    box_id: str,
    *,
    now: str,
    reason: str = "stale_provisioning",
    actor: str = "operator",
) -> str:
    """Reclaim a never-live provisioning box. Returns the reclaimed cover domain.

    Raises ReclaimError if the box does not exist or is not in 'provisioning'
    state (live boxes exposed their SNI; use ru-box-terminate, which burns it).
    """
    row = conn.execute(
        "SELECT state, sni FROM ru_boxes WHERE box_id=?", (box_id,)
    ).fetchone()
    if row is None:
        raise ReclaimError(f"box {box_id!r} not found")
    state, sni = row[0], row[1]
    if state != "provisioning":
        raise ReclaimError(
            f"box {box_id!r} is in state={state!r}, not 'provisioning'; "
            "only never-live boxes can be reclaimed. A box that went live "
            "exposed its SNI — use mthydra-controller ru-box-terminate "
            "(which burns the cover domain) instead."
        )

    # Inlined audit inserts (not state.audit.log_event, which commits and would
    # break this transaction boundary — same reason provision_box inlines them).
    def _audit(action: str, target: str, details: dict) -> None:
        conn.execute(
            "INSERT INTO audit_log (ts, actor, action, target, details_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (now, actor, action, target, json.dumps(details, separators=(",", ":"))),
        )

    try:
        conn.execute("BEGIN")
        conn.execute(
            "UPDATE ru_boxes SET state='terminated', terminated_at=?, "
            "termination_reason=? WHERE box_id=? AND state='provisioning'",
            (now, f"reclaimed: {reason}", box_id),
        )
        # Return the cover domain to the reusable pool. The VM never came up so
        # the SNI was never exposed — return it to candidate_verified, NOT burned.
        # Keep last_verified_at/verified_from_vantage (the attestation still holds).
        cur = conn.execute(
            "UPDATE cover_domain_pool SET state='candidate_verified', "
            "assigned_box_id=NULL, entered_in_use_at=NULL "
            "WHERE domain=? AND state='in_use' AND assigned_box_id=?",
            (sni, box_id),
        )
        if cur.rowcount == 1:
            _audit("cover_reclaimed", sni, {"box_id": box_id})
        # Revoke the onward credential — the box is gone.
        conn.execute(
            "UPDATE onward_credentials SET revoked_at=? "
            "WHERE box_id=? AND revoked_at IS NULL",
            (now, box_id),
        )
        _audit("box_reclaimed", box_id, {"sni": sni, "reason": reason})
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return sni
