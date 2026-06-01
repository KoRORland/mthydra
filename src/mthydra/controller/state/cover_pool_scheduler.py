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
        freeze_threshold: int = 2,
    ) -> None:
        self.db_path = Path(db_path)
        self.sweep_interval_seconds = sweep_interval_seconds
        self.mode = mode
        self._clock = clock or _default_clock
        # Tests inject a fake check_fn to avoid real network calls.
        self._check = check_fn or auto_reverify_check
        # Auto-rotate gate: only burn drifted candidate_verified domains when
        # the pool stays at >= freeze_threshold healthy candidate_verified
        # rows AFTER the burn. Matches cfg.cover_pool.freeze_threshold.
        self.freeze_threshold = freeze_threshold
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
        """Returns {'passed': [...], 'failed': [...], 'auto_burned': [...]}.

        Failures get raised as drift anti-obligations UNLESS auto-rotate
        succeeds first (candidate_verified drift with pool slack > freeze
        threshold). Passes after prior failure clear the anti-obligation.
        """
        now = self._clock()
        conn = connect(self.db_path)
        try:
            # Capture state-keyed domain list once: we need both the domain
            # identity AND its state to decide eligibility for auto-rotate.
            state_for: dict[str, str] = {}
            for state in self._STATES_IN_SCOPE:
                for d in list_by_state(conn, state):
                    state_for[d.domain] = state
            passed: list[str] = []
            failed: list[tuple[str, str]] = []
            for domain in state_for:
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

            # Auto-rotate gate: for each drifted candidate_verified domain,
            # check whether burning it would still leave the pool at
            # >= freeze_threshold candidate_verified. If yes, burn it
            # (no anti-obligation, no operator triage). If no, fall
            # through to raise the anti-obligation as before.
            # in_use drift is NEVER auto-burned — burning an active SNI
            # orphans the boxes using it; that's a box-replacement flow,
            # not a sweep flow.
            candidate_verified_count = sum(
                1 for s in state_for.values() if s == "candidate_verified"
            )
            auto_burned: list[str] = []
            for domain, reason in failed:
                if state_for[domain] != "candidate_verified":
                    continue
                # candidate_verified_count tracks the live count as we burn.
                if candidate_verified_count - 1 < self.freeze_threshold:
                    continue  # pool too tight; leave for operator
                try:
                    _self_burn(conn, domain, reason=reason, at=now)
                except Exception as e:
                    # Burn failed (concurrent assign_to_box, integrity?).
                    # Leave the anti-obligation to flag it.
                    log_event(
                        conn, ts=now, actor="auto_reverify_sweep",
                        action="cover_auto_burn_failed", target=domain,
                        details_json=json.dumps({
                            "reason": reason, "error": str(e),
                        }),
                    )
                    continue
                auto_burned.append(domain)
                candidate_verified_count -= 1
                # Self-burn took care of the drift; also clear any pre-existing
                # anti-obligation row for this domain (recovery via burn).
                conn.execute(
                    "DELETE FROM obligation_clocks WHERE obligation_id=?",
                    (f"{self._DRIFT_PREFIX}::{domain}",),
                )

            # Per-domain drift anti-obligations for domains we did NOT
            # auto-burn (either in_use, or pool too tight, or burn failed).
            burned_set = set(auto_burned)
            for domain, reason in failed:
                if domain in burned_set:
                    continue
                ob_id = f"{self._DRIFT_PREFIX}::{domain}"
                set_obligation(
                    conn,
                    obligation_id=ob_id,
                    last_proven_at=now,
                    proven_by="auto_reverify_sweep",
                    next_due_at=now,  # anti-obligation: 'now' = currently failing
                    details=json.dumps({
                        "reason": reason,
                        "state": state_for[domain],
                    }),
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
                    "passed": passed,
                    "failed": [d for d, _ in failed],
                    "auto_burned": auto_burned,
                }),
            )
            conn.commit()
            return {
                "passed": passed,
                "failed": [d for d, _ in failed],
                "auto_burned": auto_burned,
            }
        finally:
            conn.close()


def _self_burn(conn, domain: str, *, reason: str, at: str) -> None:
    """V-Task 1: burn a drifted candidate_verified domain on the sweep's
    own authority — no operator action required. Uses the same mark_burned
    path the rotate-and-burn flow uses, with last_box_id=None and reason
    'auto_reverify_drift' so audit trail makes the source visible.

    Caller MUST have already confirmed:
      - domain is candidate_verified (not in_use — that's a box flow)
      - post-burn pool still meets freeze_threshold
    """
    from mthydra.controller.state.burned import mark_burned
    details = json.dumps({
        "trigger": "cover_auto_reverify_sweep",
        "reverify_reason": reason,
    })
    log_event(
        conn, ts=at, actor="auto_reverify_sweep",
        action="cover_auto_burned", target=domain,
        details_json=details,
    )
    mark_burned(
        conn, domain,
        reason="auto_reverify_drift",
        last_box_id=None,
        at=at,
        details=details,
    )
