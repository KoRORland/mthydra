"""Spec D — UpstreamReleaseTracker.

Polls GitHub for the latest release of the configured upstream repo on a
configurable interval. Emits anti-obligations when a newer release is seen
that has not been built; stamps t4_upstream_check on success only (never
lies about checking when the check failed).
"""
from __future__ import annotations

import json
import logging
import urllib.request
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from mthydra.controller.state.audit import log_event
from mthydra.controller.state.db import connect
from mthydra.controller.state.obligations import set_obligation

log = logging.getLogger(__name__)


def _default_clock() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _add_seconds_iso(iso: str, seconds: float) -> str:
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (t + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_http_get(url: str):
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    resp = urllib.request.urlopen(req, timeout=30)
    class _R:
        def __init__(self, r):
            self.status = r.getcode()
            self._r = r
        def read(self):
            return self._r.read()
    return _R(resp)


class UpstreamReleaseTracker:
    """Periodic GitHub-releases poll. Active-only."""

    def __init__(
        self,
        *,
        db_path: Path | str,
        upstream_repo: str,
        github_api_url: str,
        poll_interval_seconds: float,
        mode: str = "production",
        clock: Callable[[], str] | None = None,
        http_client: Callable | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.upstream_repo = upstream_repo
        self.github_api_url = github_api_url
        self.poll_interval_seconds = poll_interval_seconds
        self.mode = mode
        self._clock = clock or _default_clock
        self._http = http_client or _default_http_get
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

    def run_once(self) -> str | None:
        """Returns the latest tag observed, or None if the check failed.

        Side effects on success:
          - stamps t4_upstream_check
          - sets t4_upstream_release_available::<tag> if not yet in ru_images
          - emits audit row action='upstream_release_seen'
        On failure: returns None, no obligation stamped.
        """
        url = f"{self.github_api_url}/repos/{self.upstream_repo}/releases/latest"
        try:
            resp = self._http(url)
            if resp.status != 200:
                log.warning("upstream-check: GET %s -> %s", url, resp.status)
                return None
            data = json.loads(resp.read())
            tag = data.get("tag_name")
            if not tag:
                log.warning("upstream-check: response missing tag_name")
                return None
        except Exception as e:
            log.warning("upstream-check: request failed: %s", e)
            return None

        now = self._clock()
        conn = connect(self.db_path)
        try:
            set_obligation(
                conn,
                obligation_id="t4_upstream_check",
                last_proven_at=now,
                proven_by="upstream_tracker",
                next_due_at=_add_seconds_iso(now, self.poll_interval_seconds * 2),
                details=tag,
            )
            log_event(
                conn, ts=now, actor="upstream_tracker", action="upstream_release_seen",
                target=tag, details_json=None,
            )
            already = conn.execute(
                "SELECT 1 FROM ru_images WHERE upstream_release=? LIMIT 1", (tag,)
            ).fetchone()
            if already is None:
                set_obligation(
                    conn,
                    obligation_id=f"t4_upstream_release_available::{tag}",
                    last_proven_at=now,
                    proven_by="upstream_tracker",
                    next_due_at=now,
                    details=tag,
                )
            return tag
        finally:
            conn.close()
