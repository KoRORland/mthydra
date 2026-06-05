"""Probe runner wheel — periodically run the three MVP probers (P-D6) for
every (live box × active vantage with SSH configured) pair, ingesting via
probe-record.

U-D2: per-vantage pre-flight reachability check. A dead vantage today would
produce one soft_fail probe row per (box × prober) combination — pure noise
with no operator signal that the *vantage* (not the boxes) is the problem.
The wheel now pings each vantage once at the start of a tick; if SSH fails,
it short-circuits all pair-probes for that vantage and raises a single
probe_vantage_unreachable::<vantage_id> anti-obligation. On the next tick
where SSH succeeds, the anti-obligation is cleared automatically.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from apscheduler.executors.pool import ThreadPoolExecutor as APSPoolExec
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from mthydra.controller.probe_runner import probers
from mthydra.controller.probe_runner.ssh import SshNotConfigured, ssh_cmd
from mthydra.controller.state.db import connect
from mthydra.controller.state.obligations import set_obligation

_log = logging.getLogger(__name__)
_PROBE_BIN = str(Path(sys.executable).parent / "mthydra-controller")

# Anti-obligation prefix for unreachable vantages (registered in
# observability.snapshot._ANTI_PREFIXES so obs-status classifies it).
_VANTAGE_UNREACHABLE_PREFIX = "probe_vantage_unreachable"


def _record_probe(*, db_path: str, box_id: str, vantage_id: str,
                  check_type: str, status: str, evidence: str,
                  cycle_at: str) -> None:
    subprocess.run([
        _PROBE_BIN, "probe-record",
        "--box-id", box_id, "--vantage", vantage_id,
        "--check", check_type, "--status", status,
        "--cycle-at", cycle_at,
        "--evidence", evidence[:4096],
        "--db-path", db_path,
    ], check=False, capture_output=True, text=True)


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _list_pairs(conn: sqlite3.Connection) -> list[dict]:
    conn.row_factory = sqlite3.Row
    boxes = conn.execute(
        "SELECT box_id, public_ip, sni FROM ru_boxes"
        " WHERE state='live' AND public_ip IS NOT NULL"
    ).fetchall()
    vantages = conn.execute(
        "SELECT vantage_id, ssh_host, ssh_port, ssh_user, ssh_key_path,"
        " ssh_known_hosts_path FROM probe_vantages"
        " WHERE state='active' AND ssh_host IS NOT NULL"
    ).fetchall()
    out = []
    for b in boxes:
        for v in vantages:
            out.append({
                "box_id": b["box_id"], "box_ip": b["public_ip"],
                "cover_sni": b["sni"],
                "vantage_id": v["vantage_id"],
                "vantage_ssh": dict(v),
            })
    return out


def _probe_one(pair: dict, db_path: str) -> None:
    cycle_at = _now_iso()
    v = pair["vantage_ssh"]
    def _ssh(*cmd_parts, timeout_s=30):
        return ssh_cmd(v, *cmd_parts, timeout_s=timeout_s)
    try:
        for check_type, fn in (
            ("tls_fall_through",
             lambda: probers.probe_tls_fall_through(_ssh, pair["box_ip"],
                                                     pair["cover_sni"])),
            ("cover_domain_consistency",
             lambda: probers.probe_cover_consistency(_ssh, pair["box_ip"],
                                                      pair["cover_sni"])),
            ("surface_scan",
             lambda: probers.probe_surface_scan(_ssh, pair["box_ip"])),
        ):
            try:
                status, evidence = fn()
            except SshNotConfigured:
                return
            except Exception as e:
                status, evidence = "soft_fail", f"prober raised: {e}"
            _record_probe(
                db_path=db_path, box_id=pair["box_id"],
                vantage_id=pair["vantage_id"],
                check_type=check_type, status=status, evidence=evidence,
                cycle_at=cycle_at)
    except Exception:
        _log.exception("probe runner: pair %r threw uncaught", pair)


def _check_vantage_reachable(vantage_ssh: dict, timeout_s: int = 10) -> tuple[bool, str]:
    """U-D2: cheap pre-flight `ssh ... echo OK` to avoid wasting per-pair
    probe attempts against a vantage whose SSH transport is broken.
    Returns (reachable, reason). reason is short enough for details_json."""
    try:
        res = ssh_cmd(vantage_ssh, "echo", "OK", timeout_s=timeout_s)
    except SshNotConfigured as e:
        return False, f"not-configured: {e}"
    except subprocess.TimeoutExpired:
        return False, "ssh-timeout"
    except Exception as e:
        return False, f"ssh-error: {type(e).__name__}: {e}"
    if res.returncode != 0:
        # Trim stderr to keep details_json compact; SSH errors are usually
        # one informative line ("Permission denied", "Host key verification
        # failed", etc.).
        err = (res.stderr or "").strip().splitlines()[:1]
        return False, f"ssh-rc={res.returncode}: {err[0] if err else ''}"
    return True, "ok"


def _flag_vantage_unreachable(db_path: str, vantage_id: str, reason: str,
                              now: str) -> None:
    conn = connect(db_path)
    try:
        set_obligation(
            conn,
            obligation_id=f"{_VANTAGE_UNREACHABLE_PREFIX}::{vantage_id}",
            last_proven_at=now, proven_by="probe_runner",
            next_due_at=now,  # anti-obligation semantics
            details=json.dumps({"reason": reason}),
        )
        conn.commit()
    finally:
        conn.close()


def _clear_vantage_unreachable(db_path: str, vantage_id: str) -> None:
    conn = connect(db_path)
    try:
        conn.execute(
            "DELETE FROM obligation_clocks WHERE obligation_id=?",
            (f"{_VANTAGE_UNREACHABLE_PREFIX}::{vantage_id}",),
        )
        conn.commit()
    finally:
        conn.close()


class ProbeRunnerWheel:
    def __init__(self, db_path: str, interval_seconds: int,
                 max_concurrent: int, mode: str = "active",
                 reach_check: bool = True,
                 ssh_dir: str = "/var/lib/mthydra/ssh") -> None:
        self.db_path = db_path
        self.interval_seconds = interval_seconds
        self.max_concurrent = max_concurrent
        self.mode = mode
        # reach_check=False is for tests that monkeypatch _probe_one only.
        self.reach_check = reach_check
        self.ssh_dir = ssh_dir
        self._scheduler: BackgroundScheduler | None = None

    def tick(self) -> None:
        with connect(self.db_path) as conn:
            pairs = _list_pairs(conn)
        if not pairs:
            return

        # U-D2: group pairs by vantage, pre-flight each vantage once.
        # Skip all pairs for unreachable vantages; emit a single vantage
        # anti-obligation instead of N pair-level soft_fails.
        live_pairs: list[dict] = pairs
        if self.reach_check:
            by_vantage: dict[str, list[dict]] = {}
            for p in pairs:
                by_vantage.setdefault(p["vantage_id"], []).append(p)
            live_pairs = []
            now = _now_iso()
            for vantage_id, vps in by_vantage.items():
                vantage_ssh = vps[0]["vantage_ssh"]
                reachable, reason = _check_vantage_reachable(vantage_ssh)
                if reachable:
                    _clear_vantage_unreachable(self.db_path, vantage_id)
                    live_pairs.extend(vps)
                else:
                    _flag_vantage_unreachable(
                        self.db_path, vantage_id, reason, now)
                    _log.info("probe runner: vantage %s unreachable (%s); "
                              "skipping %d pair(s)",
                              vantage_id, reason, len(vps))

        if not live_pairs:
            return

        with ThreadPoolExecutor(max_workers=self.max_concurrent) as pool:
            for p in live_pairs:
                pool.submit(_probe_one, p, self.db_path)

    def start(self) -> None:
        from mthydra.controller.probe_runner.key import ensure_probe_key
        with connect(self.db_path) as conn:
            ensure_probe_key(conn, self.ssh_dir)
        if self.mode == "offline":
            return
        self._scheduler = BackgroundScheduler(
            executors={"default": APSPoolExec(max_workers=1)})
        self._scheduler.add_job(
            self.tick, IntervalTrigger(seconds=self.interval_seconds),
            id="probe-runner", coalesce=True, max_instances=1)
        self._scheduler.start()

    def shutdown(self, wait: bool = False) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=wait)
            self._scheduler = None
