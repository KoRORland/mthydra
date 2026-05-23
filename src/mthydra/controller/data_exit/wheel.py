"""APScheduler-driven ticker that renders sing-box config + SIGHUPs on change."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler

from mthydra.controller.config import DataExitConfig
from mthydra.controller.data_exit.config_writer import (
    render_sing_box_config,
    write_atomic,
)
from mthydra.controller.data_exit.exit_set import register_started
from mthydra.controller.data_exit.signals import sighup_sing_box_unit
from mthydra.controller.state.audit import log_event
from mthydra.controller.state.db import connect


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class DataExitWheel:
    """One tick = render config from DB, write if changed, SIGHUP if written,
    register eu_exit_set on first successful write."""

    def __init__(
        self,
        *,
        db_path: Path | str,
        cfg: DataExitConfig,
        node_id: str,
        unit_name: str = "sing-box.service",
        sighup_fn: Callable[[str], None] | None = None,
        now_fn: Callable[[], str] | None = None,
        mode: str = "online",  # "online" | "offline" (no-op for tests)
    ):
        self._db_path = Path(db_path)
        self._cfg = cfg
        self._node_id = node_id
        self._unit_name = unit_name
        self._sighup_fn = sighup_fn or sighup_sing_box_unit
        self._now_fn = now_fn or _now_iso
        self._mode = mode
        self._last_hash: str | None = None
        self._registered_exit_set = False
        self._scheduler: BackgroundScheduler | None = None

    def tick(self) -> None:
        """One iteration. Idempotent; SIGHUPs only if rendered config hash changed."""
        try:
            reality_private_key = Path(self._cfg.reality_key_path).read_text().strip()
        except FileNotFoundError:
            self._audit("data_exit.no_reality_key", details=None)
            return
        cover_sni = self._cfg.cover_sni_for(self._node_id)

        conn = connect(self._db_path)
        try:
            content = render_sing_box_config(
                conn, self._cfg, node_id=self._node_id,
                cover_sni=cover_sni, reality_private_key=reality_private_key,
            )
            new_hash = hashlib.sha256(content).hexdigest()
            if new_hash == self._last_hash:
                return  # no change
            write_atomic(Path(self._cfg.config_path), content)
            if self._last_hash is not None:
                # Subsequent re-render: SIGHUP.
                try:
                    self._sighup_fn(self._unit_name)
                except Exception as e:
                    self._audit("data_exit.sighup_failed", details=str(e))
                    raise
            else:
                # First render: also register in eu_exit_set.
                try:
                    register_started(
                        conn, node_id=self._node_id,
                        listen_port=self._cfg.listen_port,
                        at=self._now_fn(),
                    )
                    self._registered_exit_set = True
                except (KeyError, ValueError) as e:
                    self._audit(
                        "data_exit.exit_set_register_failed", details=str(e),
                    )
                # Also SIGHUP on first render (the unit should be running already).
                try:
                    self._sighup_fn(self._unit_name)
                except Exception as e:
                    self._audit("data_exit.sighup_failed", details=str(e))
            self._audit(
                "data_exit.config_rewritten",
                details=f"hash={new_hash[:12]}",
            )
            self._last_hash = new_hash
        finally:
            conn.close()

    def _audit(self, action: str, *, details: str | None) -> None:
        conn = connect(self._db_path)
        try:
            log_event(
                conn,
                ts=self._now_fn(),
                actor="data_exit_wheel",
                action=action,
                target=self._node_id,
                details_json=None if details is None else f'{{"info":{details!r}}}',
            )
        finally:
            conn.close()

    def start(self) -> None:
        """Start the background scheduler. No-op in offline mode."""
        if self._mode == "offline":
            return
        self._scheduler = BackgroundScheduler()
        self._scheduler.add_job(self.tick, "interval", seconds=60, max_instances=1)
        self._scheduler.start()

    def stop(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None
