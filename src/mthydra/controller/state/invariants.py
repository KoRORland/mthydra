"""Startup self-check invariants — spec A §10 + spec B §11."""
from __future__ import annotations

import hashlib
import json
import sqlite3


class InvariantViolation(RuntimeError):
    """Raised by check_all when the DB is in a state that forbids startup."""


def _scalar(conn: sqlite3.Connection, sql: str, *params) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


def check_all(
    conn: sqlite3.Connection,
    *,
    expected_schema_version: int,
    mode: str = "production",
    now_iso: str | None = None,
) -> None:
    """Run every §10 (spec A) + §11 (spec B) invariant.

    Raise InvariantViolation on the first failure.
    mode and now_iso are used for spec B checks 13 and 16.
    """

    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise InvariantViolation(f"integrity_check: {integrity}")

    row = conn.execute("SELECT version FROM schema_version WHERE rowid=1").fetchone()
    if row is None:
        raise InvariantViolation("schema_version row missing")
    if row[0] != expected_schema_version:
        raise InvariantViolation(
            f"schema_version mismatch: db={row[0]} expected={expected_schema_version}"
        )

    overlap = _scalar(
        conn,
        "SELECT COUNT(*) FROM cover_domain_pool WHERE domain IN (SELECT domain FROM burned_domains)",
    )
    if overlap > 0:
        raise InvariantViolation(
            f"cover_domain_pool / burned_domains overlap: {overlap} row(s)"
        )

    # Read role early so authority/signing-key checks are scoped to active nodes.
    # (Check 21 later will enforce the singleton invariant formally.)
    _early_role_row = conn.execute("SELECT role FROM node_state WHERE rowid=1").fetchone()
    _early_role = _early_role_row[0] if _early_role_row else "active"

    if _early_role == "active":
        active_authorities = _scalar(
            conn, "SELECT COUNT(*) FROM credential_authority WHERE retired_at IS NULL"
        )
        if active_authorities != 1:
            raise InvariantViolation(
                f"check 22: credential_authority must have exactly 1 active row, "
                f"found {active_authorities}"
            )

        # Allow 1 or 2 active signing keys (spec B B-D7: current + outgoing during rotation)
        active_signing = _scalar(
            conn, "SELECT COUNT(*) FROM descriptor_signing_key WHERE retired_at IS NULL"
        )
        if active_signing < 1:
            raise InvariantViolation(
                f"check 22: descriptor_signing_key must have at least 1 active row, "
                f"found {active_signing}"
            )

    impossible = _scalar(
        conn,
        "SELECT COUNT(*) FROM backup_log WHERE pushed_at IS NULL AND index_updated_at IS NOT NULL",
    )
    if impossible > 0:
        raise InvariantViolation(
            f"impossible backup_log state (index without pushed): {impossible} row(s)"
        )

    # --- spec B checks ---

    # Check 13: at most 2 active signing keys (current + outgoing during rotation)
    _now_str = now_iso or "9999-12-31T23:59:59Z"
    active_keys = _scalar(
        conn,
        "SELECT COUNT(*) FROM descriptor_signing_key "
        "WHERE retired_at IS NULL OR retired_at > ?",
        _now_str,
    )
    if active_keys > 2:
        raise InvariantViolation(
            f"check 13: {active_keys} active descriptor_signing_key rows (max 2)"
        )

    # Check 14: every descriptor_history row FK points at a real signing key
    orphan = conn.execute(
        "SELECT dh.generation FROM descriptor_history dh "
        "LEFT JOIN descriptor_signing_key dsk ON dh.signing_key_generation=dsk.generation "
        "WHERE dsk.generation IS NULL LIMIT 1"
    ).fetchone()
    if orphan:
        raise InvariantViolation(
            f"check 14: descriptor_history.generation={orphan[0]} references missing signing_key"
        )

    # Check 15: chain integrity across descriptor_history
    rows = conn.execute(
        "SELECT generation, payload FROM descriptor_history ORDER BY generation"
    ).fetchall()
    prev_hash: str | None = None
    for gen, payload_text in rows:
        blob = payload_text.encode("utf-8")
        try:
            obj = json.loads(blob)
        except json.JSONDecodeError as e:
            raise InvariantViolation(
                f"check 15: descriptor_history.generation={gen} has invalid JSON: {e}"
            )
        ph = obj.get("previous_generation_hash")
        if gen == 1:
            if ph is not None:
                raise InvariantViolation(
                    f"check 15: generation 1 has non-null previous_generation_hash={ph!r}"
                )
        else:
            if ph != prev_hash:
                raise InvariantViolation(
                    f"check 15: chain break at generation {gen}: "
                    f"stored={ph!r} expected={prev_hash!r}"
                )
        prev_hash = hashlib.sha256(blob).hexdigest()

    # Check 16: no placeholder key in production mode
    if mode not in ("dryrun", "offline"):
        active_row = conn.execute(
            "SELECT privkey FROM descriptor_signing_key "
            "WHERE retired_at IS NULL ORDER BY generation DESC LIMIT 1"
        ).fetchone()
        if active_row is not None:
            priv = bytes(active_row[0])
            if priv.startswith(b"PRIV-DESC-"):
                raise InvariantViolation(
                    "check 16: active descriptor_signing_key is a spec A placeholder; "
                    "run: mthydra-controller descriptor-migrate-placeholder"
                )

    # --- spec C checks (#17–#20) ---

    # Check 17: structural triggers present (cover_pool_reject_burned + burned_domains_no_delete)
    trigs = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
    }
    for required in ("cover_pool_reject_burned", "burned_domains_no_delete"):
        if required not in trigs:
            raise InvariantViolation(f"check 17: trigger {required} is missing")

    # Check 18: entered_in_use_at IS NOT NULL iff state='in_use'
    row = conn.execute(
        "SELECT domain, state, entered_in_use_at FROM cover_domain_pool WHERE "
        "(state='in_use' AND entered_in_use_at IS NULL) OR "
        "(state!='in_use' AND entered_in_use_at IS NOT NULL) LIMIT 1"
    ).fetchone()
    if row is not None:
        raise InvariantViolation(
            f"check 18: cover_domain_pool row violates entered_in_use_at invariant: "
            f"domain={row[0]} state={row[1]} entered_in_use_at={row[2]!r}"
        )

    # Check 19: every in_use row has a live (non-terminated) box
    row = conn.execute(
        "SELECT cdp.domain, cdp.assigned_box_id, rb.state FROM cover_domain_pool cdp "
        "LEFT JOIN ru_boxes rb ON cdp.assigned_box_id = rb.box_id "
        "WHERE cdp.state='in_use' AND (rb.box_id IS NULL OR rb.state='terminated') LIMIT 1"
    ).fetchone()
    if row is not None:
        raise InvariantViolation(
            f"check 19: cover_domain_pool.domain={row[0]!r} in_use but "
            f"assigned_box_id={row[1]!r} is missing or terminated"
        )

    # Check 20: last_verified_at and verified_from_vantage populated for non-unverified rows
    row = conn.execute(
        "SELECT domain, state FROM cover_domain_pool "
        "WHERE state IN ('candidate_verified', 'in_use') "
        "AND (last_verified_at IS NULL OR verified_from_vantage IS NULL) LIMIT 1"
    ).fetchone()
    if row is not None:
        raise InvariantViolation(
            f"check 20: cover_domain_pool.domain={row[0]!r} state={row[1]!r} "
            "missing last_verified_at or verified_from_vantage"
        )

    # --- spec F checks (#21–#23) ---

    # Check 21: node_state singleton exists
    n = _scalar(conn, "SELECT COUNT(*) FROM node_state")
    if n != 1:
        raise InvariantViolation(f"check 21: node_state must have exactly 1 row, found {n}")

    role_row = conn.execute("SELECT role FROM node_state WHERE rowid=1").fetchone()
    role = role_row[0]

    # Check 22: active role requires non-retired authority + signing key
    if role == "active":
        a = _scalar(
            conn,
            "SELECT COUNT(*) FROM credential_authority WHERE retired_at IS NULL",
        )
        k = _scalar(
            conn,
            "SELECT COUNT(*) FROM descriptor_signing_key WHERE retired_at IS NULL",
        )
        if a < 1 or k < 1:
            raise InvariantViolation(
                f"check 22: active node requires authority + signing key "
                f"(authority={a}, signing_key={k})"
            )

    # Check 23: standby is skeleton (no live state, except B2 provider credential)
    if role == "standby":
        forbidden_tables = (
            "credential_authority",
            "descriptor_signing_key",
            "descriptor_history",
            "publishing_tokens",
            "cover_domain_pool",
            "burned_domains",
            "eu_exit_set",
        )
        for tbl in forbidden_tables:
            cnt = _scalar(conn, f"SELECT COUNT(*) FROM {tbl}")
            if cnt > 0:
                raise InvariantViolation(
                    f"check 23: standby DB must be skeleton; {tbl} has {cnt} row(s)"
                )
        # provider_api_credentials: B2 only carve-out
        non_b2 = _scalar(
            conn,
            "SELECT COUNT(*) FROM provider_api_credentials WHERE provider != 'b2'",
        )
        if non_b2 > 0:
            raise InvariantViolation(
                f"check 23: standby may hold only B2 provider credentials; "
                f"found {non_b2} non-B2 row(s)"
            )

    # --- spec D checks (#24–#25) ---

    # Check 24: at most one promoted image
    p = _scalar(conn, "SELECT COUNT(*) FROM ru_images WHERE state='promoted'")
    if p > 1:
        raise InvariantViolation(
            f"check 24: at most one promoted ru_image permitted, found {p}"
        )

    # Check 25: state timestamps consistent
    bad = conn.execute(
        "SELECT image_version, state, promoted_at, retired_at FROM ru_images WHERE "
        "(state='promoted'  AND promoted_at IS NULL) OR "
        "(state='retired'   AND retired_at  IS NULL) OR "
        "(state='candidate' AND (promoted_at IS NOT NULL OR retired_at IS NOT NULL)) "
        "LIMIT 1"
    ).fetchone()
    if bad is not None:
        raise InvariantViolation(
            f"check 25: ru_image {bad[0]!r} state={bad[1]!r} has inconsistent "
            f"timestamps (promoted_at={bad[2]!r}, retired_at={bad[3]!r})"
        )
