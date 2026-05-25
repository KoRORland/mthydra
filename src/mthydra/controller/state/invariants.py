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

    # --- spec G checks (#26–#28) ---

    # Check 26: authority is real Ed25519 (production mode only)
    if mode not in ("offline", "dryrun"):
        placeholder = _scalar(
            conn,
            "SELECT COUNT(*) FROM credential_authority "
            "WHERE retired_at IS NULL AND privkey_pem LIKE 'PRIV-BOOTSTRAP-%'",
        )
        if placeholder > 0:
            raise InvariantViolation(
                f"check 26: {placeholder} non-retired credential_authority row(s) "
                f"still use PRIV-BOOTSTRAP- placeholder; "
                f"run: mthydra-controller authority-migrate-placeholder"
            )

    # Check 27: every live/provisioning box has an active onward credential
    orphan = conn.execute(
        "SELECT rb.box_id FROM ru_boxes rb "
        "LEFT JOIN onward_credentials oc ON oc.box_id = rb.box_id "
        "                                  AND oc.revoked_at IS NULL "
        "WHERE rb.state IN ('provisioning','live') AND oc.cred_id IS NULL "
        "LIMIT 1"
    ).fetchone()
    if orphan is not None:
        raise InvariantViolation(
            f"check 27: ru_boxes.box_id={orphan[0]!r} is live/provisioning "
            f"but has no active onward_credentials row"
        )

    # Check 28: no two non-terminated boxes share an SNI (defence-in-depth on UNIQUE)
    dup = conn.execute(
        "SELECT sni, COUNT(*) FROM ru_boxes "
        "WHERE state != 'terminated' "
        "GROUP BY sni HAVING COUNT(*) > 1 LIMIT 1"
    ).fetchone()
    if dup is not None:
        raise InvariantViolation(
            f"check 28: SNI {dup[0]!r} shared by {dup[1]} non-terminated boxes"
        )

    # --- spec E checks (#29–#32) — gated on schema v6+ ---

    if expected_schema_version >= 6:
        _check_29_reality_uuid_unique(conn)
        _check_30_live_box_has_reality_uuid_with_credential(conn)
        _check_31_eu_node_active_has_cover_sni(conn)
        _check_32_descriptor_cover_sni_matches_eu_nodes(conn)

    # --- spec H checks (#33–#36) — gated on schema v7+ ---

    if expected_schema_version >= 7:
        _check_33_live_box_has_shard(conn)
        _check_34_user_not_on_retired_shard(conn)
        _check_35_no_cross_shard_user(conn)
        _check_36_no_empty_active_shard(conn)

    # --- spec I checks (#37–#40) — gated on schema v8+ ---

    if expected_schema_version >= 8:
        _check_37_vantage_label_unique(conn)
        _check_38_probe_results_no_orphan(conn)
        _check_39_promoted_image_has_profile(conn)
        _check_40_active_vantage_distinct_region(conn)


def _check_29_reality_uuid_unique(conn: sqlite3.Connection) -> None:
    """No two ru_boxes share a reality_uuid (defence against accidental double-assign)."""
    dup = conn.execute(
        "SELECT reality_uuid, COUNT(*) FROM ru_boxes "
        "WHERE reality_uuid IS NOT NULL "
        "GROUP BY reality_uuid HAVING COUNT(*) > 1 LIMIT 1"
    ).fetchone()
    if dup is not None:
        raise InvariantViolation(
            f"check 29: reality_uuid {dup[0]!r} shared by {dup[1]} ru_boxes rows"
        )


def _check_30_live_box_has_reality_uuid_with_credential(conn: sqlite3.Connection) -> None:
    """Every live ru_box with a non-revoked credential MUST have a non-NULL reality_uuid."""
    row = conn.execute(
        "SELECT rb.box_id FROM ru_boxes rb "
        "JOIN onward_credentials oc ON oc.box_id = rb.box_id "
        "                              AND oc.revoked_at IS NULL "
        "WHERE rb.state = 'live' AND rb.reality_uuid IS NULL LIMIT 1"
    ).fetchone()
    if row is not None:
        raise InvariantViolation(
            f"check 30: live ru_boxes.box_id={row[0]!r} has active credential "
            f"but reality_uuid IS NULL"
        )


def _check_31_eu_node_active_has_cover_sni(conn: sqlite3.Connection) -> None:
    """eu_nodes.role IN ('active','standby') => non-NULL cover_sni AND reality_pubkey."""
    row = conn.execute(
        "SELECT node_id, role, cover_sni, reality_pubkey FROM eu_nodes "
        "WHERE role IN ('active','standby') "
        "AND (cover_sni IS NULL OR reality_pubkey IS NULL) LIMIT 1"
    ).fetchone()
    if row is not None:
        raise InvariantViolation(
            f"check 31: eu_nodes.node_id={row[0]!r} role={row[1]!r} "
            f"missing cover_sni={row[2]!r} or reality_pubkey={row[3]!r}"
        )


def _check_37_vantage_label_unique(conn: sqlite3.Connection) -> None:
    """Defence-in-depth on the UNIQUE constraint."""
    dup = conn.execute(
        "SELECT label, COUNT(*) FROM probe_vantages "
        "GROUP BY label HAVING COUNT(*) > 1 LIMIT 1"
    ).fetchone()
    if dup is not None:
        raise InvariantViolation(
            f"check 37: probe_vantages.label={dup[0]!r} shared by {dup[1]} rows"
        )


def _check_38_probe_results_no_orphan(conn: sqlite3.Connection) -> None:
    """Every probe_results row references a real box + vantage + image."""
    row = conn.execute(
        "SELECT pr.id FROM probe_results pr "
        "LEFT JOIN ru_boxes rb ON rb.box_id = pr.box_id "
        "LEFT JOIN probe_vantages pv ON pv.vantage_id = pr.vantage_id "
        "LEFT JOIN ru_images ri ON ri.image_version = pr.image_version "
        "WHERE rb.box_id IS NULL OR pv.vantage_id IS NULL OR ri.image_version IS NULL "
        "LIMIT 1"
    ).fetchone()
    if row is not None:
        raise InvariantViolation(
            f"check 38: probe_results.id={row[0]} orphans (box/vantage/image missing)"
        )


def _check_39_promoted_image_has_profile(conn: sqlite3.Connection) -> None:
    """Every 'promoted' ru_image row has an image_profiles row."""
    row = conn.execute(
        "SELECT ri.image_version FROM ru_images ri "
        "LEFT JOIN image_profiles ip ON ip.image_version = ri.image_version "
        "WHERE ri.state='promoted' AND ip.image_version IS NULL LIMIT 1"
    ).fetchone()
    if row is not None:
        raise InvariantViolation(
            f"check 39: ru_images.image_version={row[0]!r} is promoted "
            "but has no image_profiles row (T3 stale-profile risk)"
        )


def _check_40_active_vantage_distinct_region(conn: sqlite3.Connection) -> None:
    """Warn (not raise) when two active vantages share (source_kind, region_hint).

    Cosmetic rotation: two vantages with the same source_kind + region_hint
    are not independent for fingerprint purposes. The operator may have a
    legitimate reason (two separate VMs); spec I residual #6 documents the
    decision to warn rather than raise.
    """
    import warnings

    row = conn.execute(
        "SELECT source_kind, region_hint, COUNT(*) FROM probe_vantages "
        "WHERE state='active' AND region_hint IS NOT NULL "
        "GROUP BY source_kind, region_hint HAVING COUNT(*) > 1 LIMIT 1"
    ).fetchone()
    if row is not None:
        warnings.warn(
            f"check 40: {row[2]} active vantages share source_kind={row[0]!r} + "
            f"region_hint={row[1]!r}; rotation may be cosmetic",
            RuntimeWarning,
            stacklevel=2,
        )


def _check_33_live_box_has_shard(conn: sqlite3.Connection) -> None:
    """Every ru_boxes row with state='live' has shard_id IS NOT NULL."""
    row = conn.execute(
        "SELECT box_id FROM ru_boxes WHERE state='live' AND shard_id IS NULL LIMIT 1"
    ).fetchone()
    if row is not None:
        raise InvariantViolation(
            f"check 33: live ru_boxes.box_id={row[0]!r} has no shard_id"
        )


def _check_34_user_not_on_retired_shard(conn: sqlite3.Connection) -> None:
    """No users row references a shards row whose retired_at IS NOT NULL."""
    row = conn.execute(
        "SELECT u.user_id, s.shard_id FROM users u "
        "JOIN shards s ON s.shard_id = u.current_shard_id "
        "WHERE s.retired_at IS NOT NULL LIMIT 1"
    ).fetchone()
    if row is not None:
        raise InvariantViolation(
            f"check 34: users.user_id={row[0]!r} references retired shard {row[1]!r}"
        )


def _check_35_no_cross_shard_user(conn: sqlite3.Connection) -> None:
    """No two active shards share any user_id in members_json."""
    rows = conn.execute(
        "SELECT shard_id, members_json FROM shards WHERE retired_at IS NULL"
    ).fetchall()
    seen: dict[str, str] = {}
    for sid, mj in rows:
        try:
            members = json.loads(mj)
        except json.JSONDecodeError as e:
            raise InvariantViolation(
                f"check 35: shards.shard_id={sid!r} has invalid members_json: {e}"
            ) from e
        for u in members:
            if u in seen and seen[u] != sid:
                raise InvariantViolation(
                    f"check 35: user {u!r} appears in both "
                    f"shards {seen[u]!r} and {sid!r}"
                )
            seen[u] = sid


def _check_36_no_empty_active_shard(conn: sqlite3.Connection) -> None:
    """Every active shard has a non-empty members_json list."""
    rows = conn.execute(
        "SELECT shard_id, members_json FROM shards WHERE retired_at IS NULL"
    ).fetchall()
    for sid, mj in rows:
        try:
            members = json.loads(mj)
        except json.JSONDecodeError as e:
            raise InvariantViolation(
                f"check 36: shards.shard_id={sid!r} has invalid members_json: {e}"
            ) from e
        if not members:
            raise InvariantViolation(f"check 36: empty active shard {sid!r}")


def _check_32_descriptor_cover_sni_matches_eu_nodes(conn: sqlite3.Connection) -> None:
    """eu_exit_set rows matched to eu_nodes by public_ip must have identical cover_sni."""
    row = conn.execute(
        "SELECT exs.fingerprint, exs.cover_sni, en.cover_sni "
        "FROM eu_exit_set exs "
        "JOIN eu_nodes en "
        "  ON SUBSTR(exs.endpoint, 1, INSTR(exs.endpoint, ':') - 1) = en.public_ip "
        "WHERE exs.retired_at IS NULL "
        "  AND exs.cover_sni IS NOT NULL "
        "  AND en.cover_sni IS NOT NULL "
        "  AND exs.cover_sni != en.cover_sni "
        "LIMIT 1"
    ).fetchone()
    if row is not None:
        raise InvariantViolation(
            f"check 32: eu_exit_set.fingerprint={row[0]!r} cover_sni={row[1]!r} "
            f"differs from matching eu_nodes.cover_sni={row[2]!r}"
        )
