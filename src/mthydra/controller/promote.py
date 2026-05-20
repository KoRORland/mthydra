"""Spec F — promote-active atomic state replacement.

promote_active() performs steps 1–9 of spec F §8: decrypt the backup blob,
swap it for the live skeleton DB atomically, write the node_state UPDATE,
run startup-check, and roll back via .bak rename on failure. It does NOT
invoke systemctl — the operator stops/starts the service.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from mthydra.controller.restore.decrypt import decrypt_blob
from mthydra.controller.state.audit import log_event
from mthydra.controller.state.db import connect
from mthydra.controller.state.eu_nodes import get_eu_node
from mthydra.controller.state.invariants import InvariantViolation, check_all
from mthydra.controller.state.node_state import current_node_state, set_node_role
from mthydra.controller.state.schema import SCHEMA_VERSION, apply_schema


class PromotionError(RuntimeError):
    """Raised when promote_active cannot complete safely."""


_CASE_B_CHECKLIST = """\
CASE B — SUSPECTED COMPROMISE. Run the following in order on the now-active node:

  1. Re-key credential authority:
       mthydra-controller authority-rotate

  2. Rotate descriptor signing key:
       mthydra-controller signing-key-rotate

  3. Rotate the B2 provider credential (revoke old in B2 console, mint new, push):
       mthydra-controller rotate-provider-credential --provider b2 --credential <NEW>

  4. Mint fresh publishing tokens — spec K, when shipped.

  5. Trigger immediate descriptor sign:
       mthydra-controller descriptor-sign-now

  6. Rotate the published subset forward — spec K. For MVP, accelerate cover-rotate.

  7. Verify recovery:
       - probe from a Russia-vantage shows green
       - backup-monitor dead-man's-switch clears
       - at least one end-to-end user path confirmed

  8. Record the drill:
       mthydra-controller standby-drill-proven --node-id <previous-standby-id> \\
           --case B --notes "promoted <date>, reason <text>"
"""


def promote_active(
    *,
    db_path: Path | str,
    backup_blob: Path | str,
    age_identity: Path | str,
    case: str,
    node_id: str,
    now: str,
) -> str | None:
    """Atomic state replacement. Returns the Case-B checklist or None for Case A.

    Refuses with PromotionError if:
      - case not in {'A','B'}
      - any input path is missing
      - current node_state.role != 'standby'
      - decryption fails
      - decrypted schema_version is newer than local SCHEMA_VERSION
      - startup-check fails on the new DB (rolls back via .bak rename first)
    """
    if case not in ("A", "B"):
        raise PromotionError(f"case must be 'A' or 'B', got {case!r}")

    db_path = Path(db_path)
    backup_blob = Path(backup_blob)
    age_identity = Path(age_identity)

    if not db_path.exists():
        raise PromotionError(f"db_path {db_path} does not exist")
    if not backup_blob.exists():
        raise PromotionError(f"backup_blob {backup_blob} does not exist")
    if not age_identity.exists():
        raise PromotionError(f"age_identity {age_identity} does not exist")

    # 1. Verify current role is standby.
    conn = connect(db_path)
    try:
        ns = current_node_state(conn)
        if ns.role != "standby":
            raise PromotionError(
                f"refused: current node_state.role={ns.role!r}, expected 'standby'"
            )
    finally:
        conn.close()

    # 2. Decrypt to a temp path in the same dir (atomic rename requires same FS).
    tmp_dir = db_path.parent / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    temp_db = tmp_dir / f"promote-{timestamp}.sqlite"

    try:
        decrypt_blob(backup_blob, identity_path=age_identity, out=temp_db)
    except Exception as e:
        raise PromotionError(f"decryption failed: {e}") from e

    # 3. apply_schema on the temp DB; read backup generation.
    conn = connect(temp_db)
    try:
        apply_schema(conn)
        v_row = conn.execute("SELECT version FROM schema_version WHERE rowid=1").fetchone()
        if v_row is None or v_row[0] > SCHEMA_VERSION:
            raise PromotionError(
                f"backup schema_version {v_row[0] if v_row else None} "
                f"is newer than local {SCHEMA_VERSION}"
            )
        gen_row = conn.execute(
            "SELECT MAX(generation) FROM backup_log WHERE pushed_at IS NOT NULL"
        ).fetchone()
        backup_generation = gen_row[0] if gen_row and gen_row[0] is not None else 0
    finally:
        conn.close()

    # 4. Atomic file swap.
    bak_path = db_path.with_suffix(db_path.suffix + f".preskel-{timestamp}.bak")
    db_path.rename(bak_path)
    try:
        temp_db.rename(db_path)
    except Exception:
        bak_path.rename(db_path)
        raise
    db_path.chmod(0o600)

    # 5. Write the node_state UPDATE on the new DB.
    conn = connect(db_path)
    try:
        set_node_role(
            conn,
            role="active",
            promoted_at=now,
            previous_role="standby",
            promotion_case=case,
            promotion_backup_generation=backup_generation,
        )
        # 6. Update local eu_nodes row if the operator pre-registered it.
        try:
            get_eu_node(conn, node_id)
            conn.execute(
                "UPDATE eu_nodes SET role='active', promoted_at=? WHERE node_id=?",
                (now, node_id),
            )
            conn.commit()
        except LookupError:
            pass

        # 7. Emit promotion audit.
        log_event(
            conn, ts=now, actor="operator", action="eu_node_promoted",
            target=node_id,
            details_json=json.dumps({
                "case": case, "backup_generation": backup_generation,
                "previous_role": "standby",
            }, separators=(",", ":")),
        )

        # 8. Run startup-check. Rollback on failure.
        try:
            check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=now)
        except InvariantViolation as e:
            conn.close()
            failed_path = db_path.with_suffix(db_path.suffix + f".failed-{timestamp}")
            db_path.rename(failed_path)
            bak_path.rename(db_path)
            raise PromotionError(f"invariant check failed after promotion: {e}") from e
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return _CASE_B_CHECKLIST if case == "B" else None
