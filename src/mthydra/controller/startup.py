"""Startup self-check runner — spec A §10 (with mode extensions from plan §16.2).

Structured pass/fail results allow the CLI layer to decide whether to refuse
startup (production mode) or log a warning (dryrun mode for step 10).
"""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from mthydra.controller.backup.age_crypt import AgeError, validate_recipient
from mthydra.controller.backup.reconcile import reconcile_pending
from mthydra.controller.state.backup_log import abandon_zombie_starts
from mthydra.controller.state.db import connect
from mthydra.controller.state.invariants import InvariantViolation, check_all
from mthydra.controller.state.schema import SCHEMA_VERSION


@dataclass(frozen=True)
class StartupCheckResult:
    ok: bool
    failed_check: str | None
    message: str | None


def _ok() -> StartupCheckResult:
    return StartupCheckResult(ok=True, failed_check=None, message=None)


def _fail(name: str, msg: str) -> StartupCheckResult:
    return StartupCheckResult(ok=False, failed_check=name, message=msg)


def run_startup_checks(
    db_path: Path | str,
    age_recipient: str,
    mode: str = "production",
    bucket_override: str | None = None,
    prod_bucket: str | None = None,
    destination=None,
) -> StartupCheckResult:
    """Run spec A §10 self-checks and return a structured result.

    Modes (plan §16.2):
    - production: all 12 checks run.
    - dryrun: network checks (step 10) run against bucket_override, not prod.
              Rejects startup if bucket_override is unset or matches prod_bucket.
    - offline: network checks (steps 10–11) skipped entirely.

    The caller (CLI) is responsible for logging the DRYRUN MODE banner and
    for calling sys.exit on a failed result.
    """
    db_path = Path(db_path)

    # Check 1: DB file exists
    if not db_path.exists():
        return _fail("db_present", f"state file not found: {db_path}")

    # Check 3: age binary present
    if shutil.which("age") is None:
        return _fail("age_binary", "age binary not on PATH")

    # Check 2: age recipient valid
    try:
        validate_recipient(age_recipient)
    except AgeError as e:
        return _fail("age_recipient", str(e))

    # Checks 4–9, 12: invariants (pure SQLite, all modes)
    conn = connect(db_path)
    try:
        try:
            check_all(conn, expected_schema_version=SCHEMA_VERSION)
        except InvariantViolation as e:
            return _fail("invariant", str(e))

        # §9 zombie cleanup: tag pre-push rows older than 1h as abandoned (all modes)
        from datetime import datetime, timezone
        _now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        abandon_zombie_starts(conn, now_iso=_now_iso, max_age_hours=1)
    finally:
        conn.close()

    # Check 10: network reachability
    if mode == "offline":
        pass  # skipped in offline mode
    elif mode == "dryrun":
        if not bucket_override:
            return _fail(
                "dryrun_bucket_override",
                "dryrun mode requires MTHYDRA_BUCKET_OVERRIDE to be set",
            )
        if prod_bucket and bucket_override == prod_bucket:
            return _fail(
                "dryrun_bucket_override",
                f"dryrun bucket_override must differ from prod bucket ({prod_bucket!r})",
            )
        # In dryrun, the caller passes a destination configured for the override bucket.
        if destination is not None:
            result = _check_destination(destination)
            if result is not None:
                return result
    else:
        # production
        if destination is not None:
            result = _check_destination(destination)
            if result is not None:
                return result

    # Check 11: crash-recovery reconciliation (not offline)
    if mode != "offline" and destination is not None:
        from datetime import datetime, timezone

        def clock() -> str:
            return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        reconcile_pending(db_path, destination, clock=clock)

    return _ok()


def _check_destination(destination) -> StartupCheckResult | None:
    """HEAD the bucket to verify reachability and credentials. Returns failure or None."""
    try:
        destination.head_index()  # returns None if index absent — that's fine
        return None
    except Exception as e:
        return _fail("b2_reachable", f"B2/S3 destination unreachable: {e}")


def reconcile_after_startup(db_path: Path | str, destination) -> int:
    """Run §9 crash-recovery after a clean startup check has passed."""
    from datetime import datetime, timezone

    def clock() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return reconcile_pending(Path(db_path), destination, clock=clock)
