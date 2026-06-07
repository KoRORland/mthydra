"""Operator-triggered verbose debug logging, shared by EU controller and RU agent.

stdlib-only by design: the RU agent imports this and the ru_agent AST guard
forbids any ``mthydra.controller`` import. Keep it dependency-free.

FULL-VERBOSE / UNREDACTED: when enabled, emitted records may contain IPs,
session identifiers and secrets (operator owns the risk — see spec decision 3).
Off by default; every enable writes a banner.
"""
from __future__ import annotations

import contextlib
import logging
import logging.handlers
from pathlib import Path

LOGGER_NAME = "mthydra.debug"
_BANNER_ON = "DEBUG MODE ON — verbose, UNREDACTED diagnostics (IPs/secrets may appear)"
_BANNER_OFF = "DEBUG MODE OFF"

_logger = logging.getLogger(LOGGER_NAME)
_logger.propagate = False
_logger.setLevel(logging.WARNING)


def _clear_handlers() -> None:
    for h in list(_logger.handlers):
        _logger.removeHandler(h)
        with contextlib.suppress(Exception):
            h.close()


def enable(*, sink: Path | str | None = None,
           max_bytes: int = 10 * 1024 * 1024, backups: int = 5) -> None:
    """Turn on DEBUG-level logging. Idempotent: re-enabling replaces handlers."""
    _clear_handlers()
    fmt = logging.Formatter("%(asctime)s %(message)s")
    stream = logging.StreamHandler()  # stderr -> journald
    stream.setFormatter(fmt)
    _logger.addHandler(stream)
    if sink is not None:
        p = Path(sink)
        p.parent.mkdir(parents=True, exist_ok=True)
        fileh = logging.handlers.RotatingFileHandler(
            p, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
        fileh.setFormatter(fmt)
        _logger.addHandler(fileh)
    _logger.setLevel(logging.DEBUG)
    _logger.debug(_BANNER_ON)


def disable() -> None:
    """Turn off debug logging and detach all handlers."""
    if _logger.isEnabledFor(logging.DEBUG):
        _logger.debug(_BANNER_OFF)
    _clear_handlers()
    _logger.setLevel(logging.WARNING)


def is_enabled() -> bool:
    return _logger.isEnabledFor(logging.DEBUG)


def log(category: str, msg: str, **fields: object) -> None:
    """Emit one debug record. No-op when debug is disabled (cheap guard)."""
    if not _logger.isEnabledFor(logging.DEBUG):
        return
    if fields:
        kv = " ".join(f"{k}={v}" for k, v in fields.items())
        _logger.debug("category=%s %s | %s", category, msg, kv)
    else:
        _logger.debug("category=%s %s", category, msg)
