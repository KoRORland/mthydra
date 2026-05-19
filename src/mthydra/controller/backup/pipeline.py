"""do_backup orchestration — spec A §6.2 (all-synchronous per plan §16.3)."""
from __future__ import annotations

import hashlib
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path

from mthydra.controller.backup.age_crypt import encrypt_file
from mthydra.controller.backup.s3_dest import S3Destination
from mthydra.controller.state.audit import log_event
from mthydra.controller.state.backup_log import (
    BackupTrigger,
    next_generation,
    record_index_updated,
    record_pushed,
    record_started,
)
from mthydra.controller.state.db import connect


class BackupPipeline:
    """Orchestrates the full do_backup procedure (spec A §6.2).

    Threading model (plan §16.3): all-synchronous; callers may invoke
    do_backup from any thread.  The internal threading.Lock serializes
    concurrent invocations so only one backup runs at a time.
    """

    def __init__(
        self,
        db_path: Path,
        tmp_dir: Path,
        recipient: str,
        destination: S3Destination,
        clock: Callable[[], str],
        mode: str = "production",
        bucket_override: str | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.tmp_dir = Path(tmp_dir)
        self.recipient = recipient
        self.destination = destination
        self.clock = clock
        self.mode = mode
        self.bucket_override = bucket_override
        self._mutex = threading.Lock()
        self._consecutive_failures = 0

    def do_backup(self, trigger: BackupTrigger) -> int:
        """Run one backup cycle.  Returns the generation number produced.

        In offline mode, refuses to run (spec §16.3 G4).
        Trigger is tagged with mode prefix when not production (e.g. 'dryrun:floor_timer').
        """
        if self.mode == "offline":
            raise RuntimeError("do_backup refused: controller is in offline mode")

        effective_trigger: BackupTrigger | str = (
            trigger if self.mode == "production" else f"dryrun:{trigger.value}"
        )

        with self._mutex:
            try:
                result = self._do_backup_locked(effective_trigger)
                self._consecutive_failures = 0
                return result
            except Exception:
                self._consecutive_failures += 1
                if self._consecutive_failures >= 3:
                    self._record_self_alarm_unreachable()
                raise

    def _do_backup_locked(self, trigger: BackupTrigger) -> int:
        """Core backup steps — must be called with self._mutex held."""
        conn = connect(self.db_path)
        try:
            gen = next_generation(conn)
            now = self.clock()
            record_started(conn, gen, trigger, now)
        finally:
            conn.close()

        snap = self.tmp_dir / f"snap-{gen}.db"
        enc = self.tmp_dir / f"snap-{gen}.age"
        try:
            self._sqlite_backup(self.db_path, snap)
            encrypt_file(snap, recipient=self.recipient, out=enc)

            sha = hashlib.sha256(enc.read_bytes()).hexdigest()
            size = enc.stat().st_size

            self.destination.put_blob(generation=gen, blob_path=enc)

            conn = connect(self.db_path)
            try:
                record_pushed(conn, gen, sha256=sha, size_bytes=size, pushed_at=self.clock())
            finally:
                conn.close()

            self.destination.put_index(
                highest_gen=gen, sha256=sha, size_bytes=size, ts=self.clock()
            )

            conn = connect(self.db_path)
            try:
                record_index_updated(conn, gen, at=self.clock())
            finally:
                conn.close()

            return gen
        finally:
            if snap.exists():
                snap.unlink()
            if enc.exists():
                enc.unlink()

    def _record_self_alarm_unreachable(self) -> None:
        """Write audit_log row on 3rd consecutive failure (spec §16.4 G9)."""
        import json

        try:
            conn = connect(self.db_path)
            try:
                log_event(
                    conn,
                    ts=self.clock(),
                    actor="controller",
                    action="self_alarm_unreachable",
                    target=None,
                    details_json=json.dumps({"consecutive_failures": self._consecutive_failures}),
                )
            finally:
                conn.close()
        except Exception:
            pass  # don't mask the original failure

    @staticmethod
    def _sqlite_backup(src: Path, dest: Path) -> None:
        """Online SQLite backup via the sqlite3 backup API (atomic, WAL-safe)."""
        with sqlite3.connect(str(src)) as src_conn, sqlite3.connect(str(dest)) as dst_conn:
            src_conn.backup(dst_conn)
