"""Enrollment poller (spec O O-D2): long-poll the distribution bot for
/start <token> and capture the user's chat_id. Active-only scheduler.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from mthydra.controller.distribution import enrollment
from mthydra.controller.state import user_channels as _uc
from mthydra.controller.state.audit import log_event
from mthydra.controller.state.db import connect

log = logging.getLogger(__name__)

_BOT_PURPOSE = "distribution"


def _default_clock() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class EnrollmentPoller:
    def __init__(
        self,
        *,
        db_path: Path | str,
        receive_client,
        poll_interval_seconds: float,
        on_enrolled: Callable[[str], None] | None = None,
        mode: str = "production",
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.recv = receive_client
        self.poll_interval_seconds = poll_interval_seconds
        self.on_enrolled = on_enrolled
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
            trigger=IntervalTrigger(seconds=self.poll_interval_seconds),
        )
        self._scheduler.start()

    def disarm(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    def _offset(self, conn) -> int:
        row = conn.execute(
            "SELECT last_offset FROM bot_offsets WHERE bot_purpose=?",
            (_BOT_PURPOSE,),
        ).fetchone()
        return int(row[0]) if row else 0

    def _save_offset(self, conn, offset: int, now: str) -> None:
        conn.execute(
            "INSERT INTO bot_offsets (bot_purpose, last_offset, updated_at) "
            "VALUES (?, ?, ?) ON CONFLICT(bot_purpose) DO UPDATE SET "
            "last_offset=excluded.last_offset, updated_at=excluded.updated_at",
            (_BOT_PURPOSE, offset, now),
        )

    def run_once(self) -> list[str]:
        """Process one batch of updates. Returns user_ids newly enrolled."""
        now = self._clock()
        conn = connect(self.db_path)
        enrolled: list[str] = []
        try:
            offset = self._offset(conn)
            updates = self.recv.get_updates(offset=offset)
            max_update_id = offset - 1
            for u in updates:
                max_update_id = max(max_update_id, int(u["update_id"]))
                text = u.get("text", "") or ""
                if not text.startswith("/start "):
                    continue
                token = text.split(maxsplit=1)[1].strip()
                user_id = enrollment.match(conn, token, now=now)
                if user_id is None:
                    log_event(conn, ts=now, actor="enroll_poller",
                              action="enrollment_rejected", target=None,
                              details_json=None)
                    continue
                existing = _uc.get_channels(conn, user_id)
                email = existing.email_addr if existing else None
                _uc.set_channels(conn, user_id,
                                 telegram_chat_id=u["chat_id"],
                                 email_addr=email, at=now)
                enrolled.append(user_id)
            if updates:
                self._save_offset(conn, max_update_id + 1, now)
            conn.commit()
        finally:
            conn.close()
        if self.on_enrolled:
            for uid in enrolled:
                try:
                    self.on_enrolled(uid)
                except Exception:
                    log.exception("on_enrolled callback failed for %s", uid)
        return enrolled
