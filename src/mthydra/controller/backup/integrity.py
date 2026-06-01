"""V-Task 2: backup integrity smoke sweep.

Picks a random recent backup generation, downloads the encrypted blob from
S3, re-hashes it, and compares to the sha256 we recorded in backup_log at
write time. Catches a class of failures nothing else in the controller
surfaces today:

  - silent S3 corruption / bit-rot
  - wrong-bucket reads (config drift across hosts)
  - integrity-failed PUTs that S3 nevertheless accepted
  - operator-applied tooling that mutated the blob after upload

Hashes the ENCRYPTED blob; does NOT need the age key. The sha256 recorded
in backup_log is also of the encrypted blob, so the comparison is
end-to-end without any decryption surface.

Heartbeat obligation: backup_integrity_proven (singleton, default weekly
cadence). Failure: backup_integrity_failed::<generation> per-target anti
obligation with the mismatch details.
"""
from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from mthydra.controller.state.audit import log_event
from mthydra.controller.state.backup_log import list_recent_pushed
from mthydra.controller.state.db import connect
from mthydra.controller.state.obligations import set_obligation


def _default_clock() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _add_seconds_iso(iso: str, seconds: float) -> str:
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (t + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


class BackupIntegritySweep:
    _PROOF_OBLIGATION = "backup_integrity_proven"
    _FAIL_PREFIX = "backup_integrity_failed"

    def __init__(
        self,
        db_path: Path | str,
        *,
        destination,  # S3Destination
        sweep_interval_seconds: float = 7 * 86400,
        recent_window: int = 10,
        mode: str = "production",
        clock: Callable[[], str] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.destination = destination
        self.sweep_interval_seconds = sweep_interval_seconds
        # Pool size from which to pick the target gen. Picking from the
        # most-recent N (not just the very latest) gives us coverage of
        # older blobs too — a corruption that lands ~3 weeks back would
        # otherwise stay invisible until the next restore drill.
        self.recent_window = recent_window
        self.mode = mode
        self._clock = clock or _default_clock
        # Tests inject a seeded RNG for determinism.
        self._rng = rng or random.Random()
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

    def run_once(self) -> dict[str, object]:
        """Returns {'checked': gen|None, 'ok': bool, 'reason': str}."""
        now = self._clock()
        conn = connect(self.db_path)
        try:
            recent = list_recent_pushed(conn, limit=self.recent_window)
            if not recent:
                # Nothing to check yet — fresh install, no backups pushed.
                # Don't stamp the proof obligation; an empty fleet isn't
                # proven, it's just untestable.
                return {"checked": None, "ok": False, "reason": "no-backups"}
            target = self._rng.choice(recent)

            try:
                blob = self.destination.get_blob(target.generation)
            except Exception as e:
                # Download failure is a real signal — recorded but not a
                # "corruption" verdict per se. Raise the per-gen anti
                # obligation so operator investigates (could be IAM,
                # network, or actual blob deletion).
                reason = f"download-failed: {type(e).__name__}: {e}"
                self._raise_failure(conn, target.generation, reason, now)
                return {"checked": target.generation, "ok": False,
                        "reason": reason}

            actual = hashlib.sha256(blob).hexdigest()
            expected = target.sha256
            if actual != expected:
                reason = (f"sha256 mismatch: expected {expected[:12]}…, "
                          f"got {actual[:12]}…")
                self._raise_failure(conn, target.generation, reason, now)
                return {"checked": target.generation, "ok": False,
                        "reason": reason}

            # Pass — stamp the singleton proof + clear any prior fail for
            # this gen (the same gen could have a stale fail row from a
            # transient earlier mismatch).
            next_due = _add_seconds_iso(now, self.sweep_interval_seconds * 2)
            set_obligation(
                conn,
                obligation_id=self._PROOF_OBLIGATION,
                last_proven_at=now,
                proven_by="backup_integrity_sweep",
                next_due_at=next_due,
                details=json.dumps({
                    "checked_generation": target.generation,
                    "size_bytes": target.size_bytes,
                }),
            )
            conn.execute(
                "DELETE FROM obligation_clocks WHERE obligation_id=?",
                (f"{self._FAIL_PREFIX}::{target.generation}",),
            )
            log_event(
                conn, ts=now, actor="backup_integrity_sweep",
                action="backup_integrity_check",
                target=str(target.generation),
                details_json=json.dumps({"ok": True}),
            )
            conn.commit()
            return {"checked": target.generation, "ok": True, "reason": "ok"}
        finally:
            conn.close()

    def _raise_failure(self, conn, generation: int, reason: str, now: str) -> None:
        set_obligation(
            conn,
            obligation_id=f"{self._FAIL_PREFIX}::{generation}",
            last_proven_at=now,
            proven_by="backup_integrity_sweep",
            next_due_at=now,
            details=json.dumps({"reason": reason}),
        )
        log_event(
            conn, ts=now, actor="backup_integrity_sweep",
            action="backup_integrity_check",
            target=str(generation),
            details_json=json.dumps({"ok": False, "reason": reason}),
        )
        conn.commit()
