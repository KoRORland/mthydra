"""Cover-pool sweep schedulers (spec C §7 + spec U U-D1).

Three APScheduler-driven sweeps:
  * CoverPoolReverifySweep        — TTL downgrade of stale candidate_verified rows
  * CoverPoolRotationSweep        — flags due-for-rotation in_use domains
  * CoverPoolAutoReverifySweep    — controller-side reverification (spec U U-D1):
      runs the smell tests (TCP+TLS) that an operator used to do manually
      via cover-attest-verified, on a short cadence (default hourly).
      On any pass, stamps the cover_pool_reverify_pass_proven obligation;
      on per-domain fail, raises cover_pool_reverify_drift_pending::<domain>.

All three follow the same all-synchronous + BackgroundScheduler model as
mthydra.descriptor.scheduler.DescriptorRotator. Offline mode disables
the timer entirely; tests use run_once() with a frozen clock.
"""
from __future__ import annotations

import json
import socket
import ssl
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from mthydra.controller.state.audit import log_event
from mthydra.controller.state.cover_pool import (
    downgrade_stale_verified,
    list_by_state,
    list_due_for_rotation,
    pool_health,
)
from mthydra.controller.state.db import connect
from mthydra.controller.state.obligations import set_obligation


def _default_clock() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _add_seconds_iso(iso: str, seconds: float) -> str:
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (t + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


class CoverPoolReverifySweep:
    """Periodic downgrade of stale candidate_verified rows (spec C §7.1)."""

    def __init__(
        self,
        db_path: Path | str,
        reverify_after_days: int,
        sweep_interval_seconds: float,
        mode: str = "production",
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.reverify_after_days = reverify_after_days
        self.sweep_interval_seconds = sweep_interval_seconds
        self.mode = mode
        self._clock = clock or _default_clock
        self._scheduler: BackgroundScheduler | None = None

    def arm(self) -> None:
        if self.mode == "offline":
            return
        executors = {"default": ThreadPoolExecutor(max_workers=1)}
        self._scheduler = BackgroundScheduler(executors=executors, daemon=True)
        self._scheduler.add_job(
            self.run_once,
            trigger=IntervalTrigger(seconds=self.sweep_interval_seconds),
        )
        self._scheduler.start()

    def disarm(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    def run_once(self) -> list[str]:
        now = self._clock()
        conn = connect(self.db_path)
        try:
            downgraded = downgrade_stale_verified(
                conn, now=now, reverify_after_days=self.reverify_after_days,
            )
            log_event(
                conn, ts=now, actor="reverify_sweep", action="cover_reverify_sweep",
                target=None, details_json=json.dumps({"downgraded": len(downgraded)}),
            )
            next_due = _add_seconds_iso(now, self.sweep_interval_seconds * 2)
            set_obligation(
                conn,
                obligation_id="cover_pool_reverify_sweep_ran",
                last_proven_at=now,
                proven_by="reverify_sweep",
                next_due_at=next_due,
                details=json.dumps({"downgraded": len(downgraded)}),
            )
            return downgraded
        finally:
            conn.close()


class CoverPoolRotationSweep:
    """Periodic detection of due-for-rotation in_use domains (spec C §7.2)."""

    def __init__(
        self,
        db_path: Path | str,
        rotation_ttl_days: int,
        freeze_threshold: int,
        sweep_interval_seconds: float,
        mode: str = "production",
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.rotation_ttl_days = rotation_ttl_days
        self.freeze_threshold = freeze_threshold
        self.sweep_interval_seconds = sweep_interval_seconds
        self.mode = mode
        self._clock = clock or _default_clock
        self._scheduler: BackgroundScheduler | None = None

    def arm(self) -> None:
        if self.mode == "offline":
            return
        executors = {"default": ThreadPoolExecutor(max_workers=1)}
        self._scheduler = BackgroundScheduler(executors=executors, daemon=True)
        self._scheduler.add_job(
            self.run_once,
            trigger=IntervalTrigger(seconds=self.sweep_interval_seconds),
        )
        self._scheduler.start()

    def disarm(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    def run_once(self) -> list[str]:
        """Returns the list of domains flagged due-for-rotation (empty if frozen)."""
        now = self._clock()
        conn = connect(self.db_path)
        try:
            h = pool_health(conn, freeze_threshold=self.freeze_threshold)
            if h.rotation_frozen:
                set_obligation(
                    conn,
                    obligation_id="cover_pool_rotation_frozen",
                    last_proven_at=now,
                    proven_by="rotation_sweep",
                    next_due_at=now,
                    details=json.dumps({
                        "candidate_verified": h.candidate_verified,
                        "freeze_threshold": self.freeze_threshold,
                    }),
                )
                self._heartbeat(conn, now, flagged=0, frozen=True)
                return []

            # Pool healthy → ensure the freeze obligation row is cleared (if it exists)
            conn.execute(
                "DELETE FROM obligation_clocks WHERE obligation_id='cover_pool_rotation_frozen'"
            )
            conn.commit()

            due = list_due_for_rotation(
                conn, now=now, rotation_ttl_days=self.rotation_ttl_days,
            )
            flagged = [d.domain for d in due]
            for domain in flagged:
                set_obligation(
                    conn,
                    obligation_id=f"cover_pool_rotation_pending::{domain}",
                    last_proven_at=now,
                    proven_by="rotation_sweep",
                    next_due_at=now,
                    details=json.dumps({"domain": domain}),
                )
            self._heartbeat(conn, now, flagged=len(flagged), frozen=False)
            return flagged
        finally:
            conn.close()

    def _heartbeat(self, conn, now: str, *, flagged: int, frozen: bool) -> None:
        next_due = _add_seconds_iso(now, self.sweep_interval_seconds * 2)
        set_obligation(
            conn,
            obligation_id="cover_pool_rotation_sweep_ran",
            last_proven_at=now,
            proven_by="rotation_sweep",
            next_due_at=next_due,
            details=json.dumps({"flagged": flagged, "frozen": frozen}),
        )
        log_event(
            conn, ts=now, actor="rotation_sweep", action="cover_rotation_sweep",
            target=None, details_json=json.dumps({"flagged": flagged, "frozen": frozen}),
        )


# ---------------------------------------------------------------------------
# U-D1 — cover-domain auto-reverify
# ---------------------------------------------------------------------------


def auto_reverify_check(domain: str, *, port: int = 443, timeout_s: float = 5.0
                        ) -> tuple[bool, str]:
    """Smell test: TCP-connect :443 + TLS handshake completes.

    Returns (passed, reason). The reason string is short enough to fit in
    a details_json field — operators don't need a stack trace, just enough
    to disambiguate the failure mode.

    This is the MVP version of the test. Baseline-drift detection (cipher
    + extension fingerprint comparison against a per-domain baseline
    captured at cover-add time) is a follow-up.
    """
    try:
        ctx = ssl.create_default_context()
        # SNI must match the domain. cover-domain reverify only cares
        # that the domain serves SOMETHING credible on :443; we don't
        # verify chain-trust because legitimate corporate domains often
        # use private CAs at the edge.
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection((domain, port), timeout=timeout_s) as sock:
            with ctx.wrap_socket(sock, server_hostname=domain) as tls:
                # Peer cert isn't validated, but pulling it forces handshake completion.
                tls.getpeercert(binary_form=True)
        return True, "tls-handshake-ok"
    except socket.timeout:
        return False, "timeout"
    except OSError as e:
        return False, f"connect-error: {type(e).__name__}: {e}"
    except ssl.SSLError as e:
        return False, f"tls-error: {type(e).__name__}: {e}"
    except Exception as e:
        return False, f"unexpected: {type(e).__name__}: {e}"


class CoverPoolAutoReverifySweep:
    """U-D1: periodic auto-reverification of cover-domain liveness.

    Targets domains in (candidate_verified, in_use) — i.e. domains the
    operator has previously attested. Verified-but-stale rows are also
    in scope via the existing TTL downgrade sweep; this sweep is the
    positive-evidence side.

    On any-pass: stamps cover_pool_reverify_pass_proven (singleton, since
      the pool is fundamentally OK if any verified domain works).
    On per-domain fail: raises cover_pool_reverify_drift_pending::<domain>
      anti-obligation; the row's last_proven_at is the sweep timestamp
      and details_json carries the failure reason.
    On per-domain pass after prior fail: clears any matching drift
      anti-obligation row.
    """

    _STATES_IN_SCOPE = ("candidate_verified", "in_use")
    _PROOF_OBLIGATION = "cover_pool_reverify_pass_proven"
    _DRIFT_PREFIX = "cover_pool_reverify_drift_pending"

    def __init__(
        self,
        db_path: Path | str,
        sweep_interval_seconds: float,
        mode: str = "production",
        clock: Callable[[], str] | None = None,
        check_fn: Callable[[str], tuple[bool, str]] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.sweep_interval_seconds = sweep_interval_seconds
        self.mode = mode
        self._clock = clock or _default_clock
        # Tests inject a fake check_fn to avoid real network calls.
        self._check = check_fn or auto_reverify_check
        self._scheduler: BackgroundScheduler | None = None

    def arm(self) -> None:
        if self.mode == "offline":
            return
        executors = {"default": ThreadPoolExecutor(max_workers=1)}
        self._scheduler = BackgroundScheduler(executors=executors, daemon=True)
        self._scheduler.add_job(
            self.run_once,
            trigger=IntervalTrigger(seconds=self.sweep_interval_seconds),
        )
        self._scheduler.start()

    def disarm(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    def run_once(self) -> dict[str, list[str]]:
        """Returns {'passed': [...], 'failed': [...]} for the sweep run."""
        now = self._clock()
        conn = connect(self.db_path)
        try:
            domains: list[str] = []
            for state in self._STATES_IN_SCOPE:
                domains.extend(d.domain for d in list_by_state(conn, state))
            passed: list[str] = []
            failed: list[tuple[str, str]] = []
            for domain in domains:
                ok, reason = self._check(domain)
                if ok:
                    passed.append(domain)
                else:
                    failed.append((domain, reason))

            # Stamp the singleton proof obligation if ANY domain passed.
            if passed:
                next_due = _add_seconds_iso(now, self.sweep_interval_seconds * 2)
                set_obligation(
                    conn,
                    obligation_id=self._PROOF_OBLIGATION,
                    last_proven_at=now,
                    proven_by="auto_reverify_sweep",
                    next_due_at=next_due,
                    details=json.dumps({"passed": len(passed), "failed": len(failed)}),
                )

            # Per-domain drift anti-obligations. Failures get raised;
            # passes after prior failure get cleared (the operator's only
            # action is investigation, so a self-cleared drift is a win).
            for domain, reason in failed:
                ob_id = f"{self._DRIFT_PREFIX}::{domain}"
                set_obligation(
                    conn,
                    obligation_id=ob_id,
                    last_proven_at=now,
                    proven_by="auto_reverify_sweep",
                    next_due_at=now,  # anti-obligation: 'now' = currently failing
                    details=json.dumps({"reason": reason}),
                )
            for domain in passed:
                ob_id = f"{self._DRIFT_PREFIX}::{domain}"
                conn.execute(
                    "DELETE FROM obligation_clocks WHERE obligation_id=?",
                    (ob_id,),
                )

            log_event(
                conn, ts=now, actor="auto_reverify_sweep",
                action="cover_auto_reverify_sweep", target=None,
                details_json=json.dumps({
                    "passed": passed, "failed": [d for d, _ in failed],
                }),
            )
            conn.commit()
            return {"passed": passed, "failed": [d for d, _ in failed]}
        finally:
            conn.close()
