"""V5 Task 5: RealityHandshakeObserver — reality-handshake reckoning from a
RU vantage against the published EU exit set.

Mirrors K3's EuExitObserver shape (BackgroundScheduler + IntervalTrigger,
no-op in offline mode; tick() opens its own connection, sweeps, sets/clears
anti-obligations). Per tick:

  1. Read the latest signed descriptor; parse its EU exit set + tls
     fingerprints to test.
  2. Pick one active probe vantage and dial each exit's Reality endpoint
     with `mthydra-rh` (via probe_reality_handshake), once per fingerprint —
     this both exercises connectivity and captures the emitted JA3 per fp.
  3. Per exit: if any probe came back reset/timeout/tcp_fail/error, raise
     eu_exit_handshake_degraded::<exit_fingerprint>; else clear it.
  4. Once per tick: compare observed JA3s against the operator-maintained
     reference set and raise/clear tls_fingerprint_stale::<fp> accordingly.
     An empty/missing reference set means "nothing configured yet" — we
     skip staleness entirely rather than flag every fingerprint as stale.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from mthydra.controller.observability.fingerprint_staleness import (
    evaluate_fingerprint_staleness,
    load_reference_set,
)
from mthydra.controller.probe_runner.probers import (
    HandshakeProbeResult,
    probe_reality_handshake,
)
from mthydra.controller.probe_runner.ssh import ssh_cmd
from mthydra.controller.state.db import connect
from mthydra.controller.state.descriptor import latest_descriptor_with_signature
from mthydra.controller.state.obligations import set_obligation
from mthydra.descriptor.payload import DescriptorPayload

_DEGRADED_RESULTS = {"reset", "timeout", "tcp_fail", "error"}


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class RealityHandshakeObserver:
    POLL_INTERVAL_SECONDS = 30 * 60

    def __init__(
        self,
        *,
        db_path: Path | str,
        ja3_reference_path: str | None = None,
        ssh_cmd_fn: Callable | None = None,
        clock: Callable[[], str] | None = None,
        mode: str = "online",
    ) -> None:
        self._db_path = Path(db_path)
        self._ja3_reference_path = ja3_reference_path
        self._ssh_cmd_fn = ssh_cmd_fn or ssh_cmd
        self._clock = clock or _now_iso
        self._mode = mode
        self._scheduler: BackgroundScheduler | None = None

    def arm(self) -> None:
        if self._mode == "offline":
            return
        self._scheduler = BackgroundScheduler(daemon=True)
        self._scheduler.add_job(
            self.tick, trigger=IntervalTrigger(seconds=self.POLL_INTERVAL_SECONDS))
        self._scheduler.start()

    def disarm(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    def tick(self) -> None:
        now = self._clock()
        conn = connect(self._db_path)
        try:
            # (1) Parse the latest signed descriptor. A missing or malformed
            # descriptor is 'no observations this tick' — never an excuse to
            # crash the sweep and kill the scheduler job (mirror EuExitObserver).
            try:
                row = latest_descriptor_with_signature(conn)
                if row is None:
                    return
                payload = DescriptorPayload.from_canonical_bytes(row[1])
                exits = payload.eu_exit_set
                fps = [fp for fp, _w in (payload.tls_fingerprints or (("chrome", 0),))]
            except Exception:
                return

            conn.row_factory = sqlite3.Row
            vrow = conn.execute(
                "SELECT vantage_id, ssh_host, ssh_port, ssh_user, ssh_key_path,"
                " ssh_known_hosts_path FROM probe_vantages WHERE state='active'"
                " ORDER BY vantage_id LIMIT 1"
            ).fetchone()
            if vrow is None:
                return
            vantage_ssh = dict(vrow)

            def _ssh(*cmd_parts, timeout_s=30):
                return self._ssh_cmd_fn(vantage_ssh, *cmd_parts, timeout_s=timeout_s)

            observed_ja3_by_fp: dict[str, str | None] = {}
            for exit in exits:
                if not (exit.endpoint and exit.cover_sni and exit.reality_pubkey):
                    continue
                degraded: HandshakeProbeResult | None = None
                for fp in fps:
                    # A raised exception from the prober/ssh path is a degraded
                    # observation for this exit — not a reason to abort the whole
                    # sweep. Treat it as an error result and keep going.
                    try:
                        res = probe_reality_handshake(
                            _ssh, exit_endpoint=exit.endpoint, cover_sni=exit.cover_sni,
                            reality_pubkey=exit.reality_pubkey, fingerprint=fp)
                    except Exception as exc:  # noqa: BLE001
                        res = HandshakeProbeResult(
                            result="error", detail=str(exc), ja3=None, ttfb_ms=None)
                    # Keep the first NON-None ja3 per fp across exits, so an
                    # earlier exit's None doesn't shadow a healthy fingerprint.
                    if observed_ja3_by_fp.get(fp) is None and res.ja3 is not None:
                        observed_ja3_by_fp[fp] = res.ja3
                    else:
                        observed_ja3_by_fp.setdefault(fp, res.ja3)
                    if degraded is None and res.result in _DEGRADED_RESULTS:
                        degraded = res
                oid = f"eu_exit_handshake_degraded::{exit.fingerprint}"
                if degraded is not None:
                    set_obligation(
                        conn, obligation_id=oid, last_proven_at=now,
                        proven_by="reality_observer", next_due_at=now,
                        details=json.dumps({
                            "endpoint": exit.endpoint,
                            "verdict": f"{degraded.result}:{degraded.detail}",
                        }),
                    )
                else:
                    conn.execute(
                        "DELETE FROM obligation_clocks WHERE obligation_id=?",
                        (oid,))

            reference = (
                load_reference_set(self._ja3_reference_path)
                if self._ja3_reference_path is not None else {}
            )
            if reference:
                findings = evaluate_fingerprint_staleness(observed_ja3_by_fp, reference)
                stale_fps = {f.fingerprint for f in findings}
                for finding in findings:
                    set_obligation(
                        conn,
                        obligation_id=f"tls_fingerprint_stale::{finding.fingerprint}",
                        last_proven_at=now, proven_by="reality_observer",
                        next_due_at=now,
                        details=json.dumps({"observed_ja3": finding.observed_ja3}),
                    )
                for fp in observed_ja3_by_fp:
                    if fp not in stale_fps:
                        conn.execute(
                            "DELETE FROM obligation_clocks WHERE obligation_id=?",
                            (f"tls_fingerprint_stale::{fp}",))

            conn.commit()
        finally:
            conn.close()
