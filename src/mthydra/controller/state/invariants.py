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

    active_authorities = _scalar(
        conn, "SELECT COUNT(*) FROM credential_authority WHERE retired_at IS NULL"
    )
    if active_authorities != 1:
        raise InvariantViolation(
            f"credential_authority must have exactly 1 active row, found {active_authorities}"
        )

    # Allow 1 or 2 active signing keys (spec B B-D7: current + outgoing during rotation)
    active_signing = _scalar(
        conn, "SELECT COUNT(*) FROM descriptor_signing_key WHERE retired_at IS NULL"
    )
    if active_signing < 1:
        raise InvariantViolation(
            f"descriptor_signing_key must have at least 1 active row, found {active_signing}"
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
