"""Probe runner wheel — periodically run the three MVP probers (P-D6) for
every (live box × active vantage with SSH configured) pair, ingesting via
probe-record."""
from __future__ import annotations

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

_log = logging.getLogger(__name__)
_PROBE_BIN = str(Path(sys.executable).parent / "mthydra-controller")


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


class ProbeRunnerWheel:
    def __init__(self, db_path: str, interval_seconds: int,
                 max_concurrent: int, mode: str = "active") -> None:
        self.db_path = db_path
        self.interval_seconds = interval_seconds
        self.max_concurrent = max_concurrent
        self.mode = mode
        self._scheduler: BackgroundScheduler | None = None

    def tick(self) -> None:
        with connect(self.db_path) as conn:
            pairs = _list_pairs(conn)
        if not pairs:
            return
        with ThreadPoolExecutor(max_workers=self.max_concurrent) as pool:
            for p in pairs:
                pool.submit(_probe_one, p, self.db_path)

    def start(self) -> None:
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
