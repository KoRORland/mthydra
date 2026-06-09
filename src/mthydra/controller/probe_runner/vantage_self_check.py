"""Vantage self-check sweep — controller self-attests t3_vantage_revalidation.

When the controller has at least one active probe vantage it can reach, it runs
a *meaningful* liveness probe from that vantage (SSH control-plane reachable AND
an outbound TLS handshake to a reference HTTPS host verifies cleanly) and
self-proves t3_vantage_revalidation. The operator is only bothered when NO
vantage passes — t3 then simply goes overdue (and per-vantage SSH failures
already surface via probe_vantage_unreachable from the probe runner wheel).

proven_by is recorded as 'vantage_self_check' (vs 'operator') so the provenance
of every self-attestation is auditable and distinct from a human vouch.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from mthydra.controller.probe_runner.ssh import SshNotConfigured, ssh_cmd
from mthydra.controller.state.audit import log_event
from mthydra.controller.state.db import connect
from mthydra.controller.state.obligations import set_obligation

_PROOF_OBLIGATION = "t3_vantage_revalidation"
# t3's cadence window; one successful sweep keeps it green for this long.
_PROOF_WINDOW_SECONDS = 168 * 3600

_ACTIVE_VANTAGE_SQL = (
    "SELECT vantage_id, ssh_host, ssh_port, ssh_user, ssh_key_path, "
    "ssh_known_hosts_path FROM probe_vantages "
    "WHERE state='active' AND ssh_host IS NOT NULL"
)


def _default_clock() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _add_seconds_iso(iso: str, seconds: float) -> str:
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (t + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_vantage_check(
    vantage_ssh: dict, cover_sni_ref: str | None, *,
    ssh_cmd_fn: Callable = ssh_cmd,
) -> tuple[bool, str]:
    """A meaningful liveness probe of one vantage. Returns (ok, reason).

    Pass = SSH control-plane reachable AND (when a reference host is given) an
    outbound TLS handshake to it verifies cleanly. With no reference host, SSH
    reachability alone is the best honest signal. Reuses the same openssl
    s_client pattern the probe-runner probers rely on (so vantage tooling
    requirements do not change)."""
    try:
        if cover_sni_ref:
            res = ssh_cmd_fn(
                vantage_ssh, "sh", "-c",
                f"openssl s_client -connect {cover_sni_ref}:443 "
                f"-servername {cover_sni_ref} </dev/null 2>&1 | head -60",
                timeout_s=20,
            )
            out = getattr(res, "stdout", "") or ""
            if getattr(res, "returncode", 1) == 0 and "Verify return code: 0" in out:
                return True, "tls_ok"
            return False, "tls_fail"
        res = ssh_cmd_fn(vantage_ssh, "echo", "OK", timeout_s=10)
        if getattr(res, "returncode", 1) == 0 and "OK" in (getattr(res, "stdout", "") or ""):
            return True, "ssh_ok"
        return False, "ssh_fail"
    except SshNotConfigured as e:
        return False, f"ssh_not_configured: {e}"
    except Exception as e:  # subprocess timeout / transport error — never raise
        return False, f"error: {type(e).__name__}"


class VantageSelfCheckSweep:
    def __init__(
        self,
        db_path: Path | str,
        *,
        sweep_interval_seconds: float,
        cover_sni_ref: str | None,
        mode: str = "production",
        clock: Callable[[], str] | None = None,
        check_fn: Callable[[dict, str | None], tuple[bool, str]] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.sweep_interval_seconds = sweep_interval_seconds
        self.cover_sni_ref = cover_sni_ref
        self.mode = mode
        self._clock = clock or _default_clock
        self._check = check_fn or default_vantage_check
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

    def run_once(self) -> dict[str, int]:
        now = self._clock()
        conn = connect(self.db_path)
        try:
            rows = conn.execute(_ACTIVE_VANTAGE_SQL).fetchall()
            if not rows:
                # No usable vantage -> nothing to self-attest from. Leave t3 to
                # the operator (its overdue warn correctly means "no vantage").
                return {"checked": 0, "passed": 0}
            passed = 0
            for r in rows:
                vantage_ssh = {
                    "ssh_host": r[1], "ssh_port": r[2], "ssh_user": r[3],
                    "ssh_key_path": r[4], "ssh_known_hosts_path": r[5],
                }
                ok, reason = self._check(vantage_ssh, self.cover_sni_ref)
                log_event(
                    conn, ts=now, actor="vantage_self_check",
                    action="vantage_self_check_pass" if ok else "vantage_self_check_fail",
                    target=r[0], details_json=json.dumps({"reason": reason}),
                )
                if ok:
                    passed += 1
            if passed:
                set_obligation(
                    conn,
                    obligation_id=_PROOF_OBLIGATION,
                    last_proven_at=now,
                    proven_by="vantage_self_check",
                    next_due_at=_add_seconds_iso(now, _PROOF_WINDOW_SECONDS),
                    details=json.dumps({"vantages_passed": passed,
                                        "vantages_checked": len(rows)}),
                )
            conn.commit()
            return {"checked": len(rows), "passed": passed}
        finally:
            conn.close()
