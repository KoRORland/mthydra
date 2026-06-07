# Debug Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add operator-triggered verbose diagnostic logging to both node types — EU controller (CLI toggle + service restart, persistent rotating log, 24h auto-expire) and RU agent (tmpfs flag file polled live, no restart).

**Architecture:** A shared stdlib-only `mthydra/debuglog.py` holds the toggleable `logging` facility (no redaction; loud banner). EU wraps it with a flag file + `debug` CLI subcommand + a serve-time arm/auto-expire helper. RU wraps it with a tmpfs flag-file poller thread. Instrumentation calls (`debuglog.log(category, msg, **fields)`) are added at high-value flow points in each node.

**Tech Stack:** Python ≥3.12, stdlib `logging`/`logging.handlers`, `argparse`, `threading`; pytest. Spec: `docs/superpowers/specs/2026-06-07-debug-mode-design.md`.

---

## File Structure

- `src/mthydra/debuglog.py` (NEW) — shared toggleable logger; stdlib only.
- `src/mthydra/controller/debug_flag.py` (NEW) — EU flag persistence + expiry.
- `src/mthydra/controller/debug_runtime.py` (NEW) — EU serve-time arm + expiry watcher (imports `debuglog` + `debug_flag`).
- `src/mthydra/controller/cli.py` (MODIFY) — `debug` subparser, dispatch, `_cmd_debug`, serve wiring.
- `src/mthydra/ru_agent/debug_poll.py` (NEW) — RU flag poller; stdlib only.
- `src/mthydra/ru_agent/__main__.py` (MODIFY) — start poller thread.
- Instrumentation (MODIFY): `src/mthydra/controller/state/db.py`, `src/mthydra/controller/data_exit/exit_observer.py`, `src/mthydra/ru_agent/seed.py`, `src/mthydra/ru_agent/supervisor.py`, `src/mthydra/ru_agent/descriptor_refresh.py`.
- Docs (MODIFY): `doc/runbook.md`, `CHANGELOG.md`.
- Tests (NEW): `tests/unit/test_debuglog.py`, `tests/unit/controller/test_debug_flag.py`, `tests/unit/controller/test_debug_runtime.py`, `tests/unit/controller/test_cli_debug.py`, `tests/unit/ru_agent/test_debug_poll.py`.

Run all tests with: `python -m pytest -q` (from repo root). Lint changed files only (local ruff is newer than the pin — see project memory): `ruff check <path>`.

---

## Task 1: Shared toggleable logger `debuglog.py`

**Files:**
- Create: `src/mthydra/debuglog.py`
- Test: `tests/unit/test_debuglog.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_debuglog.py
import logging
from pathlib import Path

from mthydra import debuglog


def _reset():
    debuglog.disable()


def test_disabled_by_default_log_is_noop(caplog):
    _reset()
    with caplog.at_level(logging.DEBUG, logger=debuglog.LOGGER_NAME):
        debuglog.log("conn", "should not appear", ip="1.2.3.4")
    assert debuglog.is_enabled() is False
    assert "should not appear" not in caplog.text


def test_enable_emits_banner_and_logs_to_file(tmp_path):
    _reset()
    sink = tmp_path / "logs" / "debug.log"
    debuglog.enable(sink=sink, max_bytes=10 * 1024 * 1024, backups=5)
    try:
        assert debuglog.is_enabled() is True
        debuglog.log("conn", "incoming", ip="5.6.7.8", exit="eu-3")
        content = sink.read_text()
        assert "DEBUG MODE ON" in content
        assert "category=conn incoming" in content
        assert "ip=5.6.7.8" in content and "exit=eu-3" in content
    finally:
        _reset()


def test_disable_stops_logging_and_emits_off_banner(tmp_path):
    _reset()
    sink = tmp_path / "debug.log"
    debuglog.enable(sink=sink, max_bytes=1024, backups=1)
    debuglog.disable()
    after = sink.read_text()
    assert "DEBUG MODE OFF" in after
    debuglog.log("db", "must not appear")
    assert "must not appear" not in sink.read_text()
    assert debuglog.is_enabled() is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_debuglog.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'mthydra.debuglog'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/mthydra/debuglog.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_debuglog.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/debuglog.py tests/unit/test_debuglog.py
git commit -m "feat(debug): shared stdlib-only toggleable debug logger"
```

---

## Task 2: EU flag persistence `debug_flag.py`

**Files:**
- Create: `src/mthydra/controller/debug_flag.py`
- Test: `tests/unit/controller/test_debug_flag.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/controller/test_debug_flag.py
from mthydra.controller import debug_flag


def test_write_then_read_roundtrip(tmp_path):
    p = tmp_path / "debug.flag"
    f = debug_flag.write_flag(p, ttl_hours=24, now=1000.0)
    assert f.enabled_at == 1000.0
    assert f.expires_at == 1000.0 + 24 * 3600
    back = debug_flag.read_flag(p)
    assert back is not None
    assert back.expires_at == f.expires_at
    assert back.ttl_hours == 24


def test_is_expired_boundary(tmp_path):
    f = debug_flag.write_flag(tmp_path / "f", ttl_hours=1, now=0.0)
    assert f.is_expired(3599.0) is False
    assert f.is_expired(3600.0) is True  # now >= expires_at -> expired


def test_read_missing_returns_none(tmp_path):
    assert debug_flag.read_flag(tmp_path / "nope") is None


def test_read_corrupt_returns_none(tmp_path):
    p = tmp_path / "bad.flag"
    p.write_text("{not json")
    assert debug_flag.read_flag(p) is None


def test_clear_is_idempotent(tmp_path):
    p = tmp_path / "f"
    debug_flag.write_flag(p, now=0.0)
    debug_flag.clear_flag(p)
    assert not p.exists()
    debug_flag.clear_flag(p)  # second call must not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/controller/test_debug_flag.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'mthydra.controller.debug_flag'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/mthydra/controller/debug_flag.py
"""EU controller debug-flag persistence + expiry. Stdlib-only.

The flag lives at /var/lib/mthydra/debug.flag (persistent). `debug enable`
(run as root via sudo) writes it; `serve` (run as the mthydra user) reads it
and the auto-expire watcher removes it. The mthydra user can unlink a
root-written flag because it owns the containing directory.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_FLAG_PATH = "/var/lib/mthydra/debug.flag"
DEFAULT_TTL_HOURS = 24.0


@dataclass(frozen=True)
class DebugFlag:
    enabled_at: float
    expires_at: float
    ttl_hours: float

    def is_expired(self, now: float) -> bool:
        return now >= self.expires_at


def write_flag(path: Path | str, *, ttl_hours: float = DEFAULT_TTL_HOURS,
               now: float | None = None) -> DebugFlag:
    now = time.time() if now is None else now
    flag = DebugFlag(enabled_at=now, expires_at=now + ttl_hours * 3600,
                     ttl_hours=ttl_hours)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "enabled_at": flag.enabled_at,
        "expires_at": flag.expires_at,
        "ttl_hours": flag.ttl_hours,
    }))
    return flag


def read_flag(path: Path | str) -> DebugFlag | None:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    try:
        return DebugFlag(
            enabled_at=float(data["enabled_at"]),
            expires_at=float(data["expires_at"]),
            ttl_hours=float(data.get("ttl_hours", 0.0)),
        )
    except (KeyError, TypeError, ValueError):
        return None


def clear_flag(path: Path | str) -> None:
    Path(path).unlink(missing_ok=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/controller/test_debug_flag.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/debug_flag.py tests/unit/controller/test_debug_flag.py
git commit -m "feat(debug): EU debug-flag persistence + expiry"
```

---

## Task 3: EU serve-time arm + auto-expire `debug_runtime.py`

**Files:**
- Create: `src/mthydra/controller/debug_runtime.py`
- Test: `tests/unit/controller/test_debug_runtime.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/controller/test_debug_runtime.py
from mthydra import debuglog
from mthydra.controller import debug_flag, debug_runtime


def _reset():
    debuglog.disable()


def test_arm_no_flag_returns_false(tmp_path):
    _reset()
    assert debug_runtime.arm_from_flag(tmp_path / "absent", spawn_watcher=False) is False
    assert debuglog.is_enabled() is False


def test_arm_valid_flag_enables(tmp_path):
    _reset()
    p = tmp_path / "debug.flag"
    debug_flag.write_flag(p, ttl_hours=24, now=1000.0)
    armed = debug_runtime.arm_from_flag(
        p, log_path=tmp_path / "debug.log", now=1000.0, spawn_watcher=False)
    try:
        assert armed is True
        assert debuglog.is_enabled() is True
    finally:
        _reset()


def test_arm_expired_flag_removes_and_stays_off(tmp_path):
    _reset()
    p = tmp_path / "debug.flag"
    debug_flag.write_flag(p, ttl_hours=1, now=0.0)
    armed = debug_runtime.arm_from_flag(p, now=10_000.0, spawn_watcher=False)
    assert armed is False
    assert debuglog.is_enabled() is False
    assert not p.exists()


def test_expiry_tick_turns_off_when_expired(tmp_path):
    _reset()
    p = tmp_path / "debug.flag"
    debug_flag.write_flag(p, ttl_hours=1, now=0.0)
    debuglog.enable(sink=tmp_path / "debug.log")
    turned_off = debug_runtime.expiry_tick(p, now=3600.0)
    assert turned_off is True
    assert debuglog.is_enabled() is False
    assert not p.exists()


def test_expiry_tick_keeps_on_when_valid(tmp_path):
    _reset()
    p = tmp_path / "debug.flag"
    debug_flag.write_flag(p, ttl_hours=24, now=0.0)
    debuglog.enable(sink=tmp_path / "debug.log")
    try:
        assert debug_runtime.expiry_tick(p, now=10.0) is False
        assert debuglog.is_enabled() is True
        assert p.exists()
    finally:
        _reset()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/controller/test_debug_runtime.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'mthydra.controller.debug_runtime'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/mthydra/controller/debug_runtime.py
"""Wire the EU debug flag into a running controller: arm on start, auto-expire.

Imports both ``debuglog`` (the facility) and ``debug_flag`` (persistence).
EU-only — never imported by ru_agent.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from mthydra import debuglog
from mthydra.controller import debug_flag

DEBUG_LOG_PATH = "/var/log/mthydra/debug.log"
WATCH_POLL_SECONDS = 60
_MAX_BYTES = 10 * 1024 * 1024
_BACKUPS = 5


def arm_from_flag(flag_path: Path | str, *, log_path: Path | str = DEBUG_LOG_PATH,
                  now: float | None = None, spawn_watcher: bool = True) -> bool:
    """If a non-expired flag exists, enable debug + start the expiry watcher.

    Returns True iff debug was enabled. A stale/expired flag is removed.
    """
    now = time.time() if now is None else now
    f = debug_flag.read_flag(flag_path)
    if f is None:
        return False
    if f.is_expired(now):
        debug_flag.clear_flag(flag_path)
        return False
    debuglog.enable(sink=log_path, max_bytes=_MAX_BYTES, backups=_BACKUPS)
    if spawn_watcher:
        threading.Thread(target=_watch, args=(flag_path,), daemon=True,
                         name="debug-expiry").start()
    return True


def expiry_tick(flag_path: Path | str, *, now: float | None = None) -> bool:
    """One watcher iteration. Returns True iff debug was turned off."""
    now = time.time() if now is None else now
    f = debug_flag.read_flag(flag_path)
    if f is None or f.is_expired(now):
        debuglog.disable()
        debug_flag.clear_flag(flag_path)
        return True
    return False


def _watch(flag_path: Path | str) -> None:
    while True:
        time.sleep(WATCH_POLL_SECONDS)
        if expiry_tick(flag_path):
            return
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/controller/test_debug_runtime.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/debug_runtime.py tests/unit/controller/test_debug_runtime.py
git commit -m "feat(debug): EU serve-time arm + auto-expire watcher"
```

---

## Task 4: EU `debug` CLI subcommand

**Files:**
- Modify: `src/mthydra/controller/cli.py` (imports + `DEFAULT_DEBUG_FLAG` constant near `DEFAULT_DB` at ~line 40; new `debug` subparser in `build_parser`; dispatch in `run`; new `_cmd_debug`)
- Test: `tests/unit/controller/test_cli_debug.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/controller/test_cli_debug.py
from unittest import mock

from mthydra.controller import cli, debug_flag


def test_debug_enable_writes_flag_and_restarts(tmp_path):
    flag = tmp_path / "debug.flag"
    with mock.patch.object(cli, "DEFAULT_DEBUG_FLAG", str(flag)), \
         mock.patch("subprocess.run") as run:
        rc = cli.run(["debug", "enable", "--ttl-hours", "6"])
    assert rc == 0
    f = debug_flag.read_flag(flag)
    assert f is not None and f.ttl_hours == 6.0
    run.assert_called_once()
    assert run.call_args.args[0] == ["systemctl", "restart", "mthydra-controller"]


def test_debug_enable_no_restart_skips_systemctl(tmp_path):
    flag = tmp_path / "debug.flag"
    with mock.patch.object(cli, "DEFAULT_DEBUG_FLAG", str(flag)), \
         mock.patch("subprocess.run") as run:
        rc = cli.run(["debug", "enable", "--no-restart"])
    assert rc == 0
    assert debug_flag.read_flag(flag) is not None
    run.assert_not_called()


def test_debug_disable_clears_flag(tmp_path):
    flag = tmp_path / "debug.flag"
    debug_flag.write_flag(flag, now=0.0)
    with mock.patch.object(cli, "DEFAULT_DEBUG_FLAG", str(flag)), \
         mock.patch("subprocess.run"):
        rc = cli.run(["debug", "disable", "--no-restart"])
    assert rc == 0
    assert not flag.exists()


def test_debug_status_reports_off_then_on(tmp_path, capsys):
    flag = tmp_path / "debug.flag"
    with mock.patch.object(cli, "DEFAULT_DEBUG_FLAG", str(flag)):
        assert cli.run(["debug", "status"]) == 0
        assert "OFF" in capsys.readouterr().out
        debug_flag.write_flag(flag, ttl_hours=24)
        assert cli.run(["debug", "status"]) == 0
        assert "ON" in capsys.readouterr().out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/controller/test_cli_debug.py -q`
Expected: FAIL — argparse rejects `debug` as an invalid choice (or `AttributeError` on `DEFAULT_DEBUG_FLAG`).

- [ ] **Step 3a: Add imports + constant**

In `src/mthydra/controller/cli.py`, add to the import block (near the other `from mthydra.controller import ...` lines, top of file):

```python
from mthydra.controller import debug_flag, debug_runtime
```

Add next to `DEFAULT_DB = "/var/lib/mthydra/state.sqlite"` (~line 40):

```python
DEFAULT_DEBUG_FLAG = "/var/lib/mthydra/debug.flag"
```

- [ ] **Step 3b: Register the subparser**

In `build_parser`, after `sub = p.add_subparsers(dest="cmd", required=True)` and alongside the other `sub.add_parser(...)` calls, add:

```python
    dbg_p = sub.add_parser(
        "debug",
        help="toggle verbose UNREDACTED debug logging (restarts the controller)",
    )
    dbg_p.add_argument("action", choices=["enable", "disable", "status"])
    dbg_p.add_argument(
        "--ttl-hours", type=float, default=debug_flag.DEFAULT_TTL_HOURS,
        help="auto-disable after this many hours (default 24; enable only)",
    )
    dbg_p.add_argument(
        "--no-restart", action="store_true",
        help="update the flag but do not restart the service",
    )
```

- [ ] **Step 3c: Add dispatch**

In `run`, next to the other `if args.cmd == "...":` blocks (e.g. just before `if args.cmd == "serve":`), add:

```python
    if args.cmd == "debug":
        return _cmd_debug(args)
```

- [ ] **Step 3d: Add the handler**

Add `_cmd_debug` (e.g. just above `_cmd_serve`):

```python
def _cmd_debug(args) -> int:
    """Toggle verbose debug logging via the persistent flag + service restart."""
    import subprocess
    import time as _time

    flag_path = DEFAULT_DEBUG_FLAG

    if args.action == "status":
        f = debug_flag.read_flag(flag_path)
        if f is None:
            print("debug: OFF")
            return 0
        now = _time.time()
        remaining = max(0, int(f.expires_at - now))
        state = "EXPIRED" if f.is_expired(now) else "ON"
        print(f"debug: {state} (ttl={f.ttl_hours}h, {remaining}s remaining) "
              f"-> {debug_runtime.DEBUG_LOG_PATH}")
        return 0

    if args.action == "enable":
        debug_flag.write_flag(flag_path, ttl_hours=args.ttl_hours)
        print(f"debug: enabled (ttl={args.ttl_hours}h) "
              f"-> {debug_runtime.DEBUG_LOG_PATH}")
    else:  # disable
        debug_flag.clear_flag(flag_path)
        print("debug: disabled")

    if args.no_restart:
        print("debug: --no-restart set; restart mthydra-controller to apply")
        return 0
    try:
        subprocess.run(["systemctl", "restart", "mthydra-controller"], check=True)
        print("debug: restarted mthydra-controller")
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"debug: flag updated but restart failed ({e}); "
              f"run 'sudo systemctl restart mthydra-controller' manually",
              file=sys.stderr)
        return 1
    return 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/controller/test_cli_debug.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/cli.py tests/unit/controller/test_cli_debug.py
git commit -m "feat(debug): mthydra-controller debug enable/disable/status"
```

---

## Task 5: Wire debug arm into `serve`

**Files:**
- Modify: `src/mthydra/controller/cli.py` (`_cmd_serve`, just after `cfg = load_config(args.config)`)
- Test: covered by Task 3 (`debug_runtime`) — this step adds one integration line; verify the full suite stays green.

- [ ] **Step 1: Add the arm call**

In `_cmd_serve`, immediately after `cfg = load_config(args.config)` and before the standby-role branch, add:

```python
    # Debug mode: honour a non-expired flag set by `mthydra-controller debug enable`.
    # Applies to both active and standby serve paths. Starts an expiry watcher
    # that downgrades the live process at TTL (no restart needed to turn off).
    debug_runtime.arm_from_flag(DEFAULT_DEBUG_FLAG)
```

- [ ] **Step 2: Verify the suite still passes**

Run: `python -m pytest tests/unit/controller -q`
Expected: PASS (no regressions; existing serve tests unaffected because no flag file exists in their temp env).

- [ ] **Step 3: Commit**

```bash
git add src/mthydra/controller/cli.py
git commit -m "feat(debug): arm debug logging at controller serve startup"
```

---

## Task 6: RU flag poller `debug_poll.py`

**Files:**
- Create: `src/mthydra/ru_agent/debug_poll.py`
- Test: `tests/unit/ru_agent/test_debug_poll.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/ru_agent/test_debug_poll.py
from mthydra.ru_agent import debug_poll


def test_tick_enables_on_flag_appear_and_disables_on_remove(tmp_path):
    flag = tmp_path / "debug.flag"
    calls = []
    poller = debug_poll.DebugPoller(
        flag_path=flag,
        enable_fn=lambda: calls.append("on"),
        disable_fn=lambda: calls.append("off"),
    )
    poller.tick()                 # no flag -> nothing
    assert calls == []
    flag.write_text("")           # flag appears
    poller.tick()
    assert calls == ["on"]
    poller.tick()                 # still present -> no repeat
    assert calls == ["on"]
    flag.unlink()                 # flag removed
    poller.tick()
    assert calls == ["on", "off"]


def test_tick_swallows_enable_errors(tmp_path):
    flag = tmp_path / "debug.flag"
    flag.write_text("")

    def boom():
        raise OSError("tmpfs full")

    poller = debug_poll.DebugPoller(flag_path=flag, enable_fn=boom,
                                    disable_fn=lambda: None)
    poller.tick()  # must not raise; stays disarmed so it can retry next tick
    assert poller.enabled is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/ru_agent/test_debug_poll.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'mthydra.ru_agent.debug_poll'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/mthydra/ru_agent/debug_poll.py
"""RU-side debug-flag poller. stdlib-only (ru_agent AST guard forbids
mthydra.controller imports; mthydra.debuglog is allowed).

The agent cannot restart (tmpfs seed + once-per-instance cloud-init), so debug
is toggled on the LIVE process by a flag file on tmpfs. Output goes to tmpfs
only and dies on reboot — consistent with the no-persistent-storage invariant.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from mthydra import debuglog

DEBUG_FLAG_PATH = "/run/mthydra/debug.flag"
DEBUG_LOG_PATH = "/run/mthydra/debug/agent-debug.log"
POLL_SECONDS = 5
_MAX_BYTES = 2 * 1024 * 1024
_BACKUPS = 2


class DebugPoller:
    def __init__(self, *, flag_path: Path | str = DEBUG_FLAG_PATH,
                 log_path: Path | str = DEBUG_LOG_PATH,
                 enable_fn: Callable[[], None] | None = None,
                 disable_fn: Callable[[], None] | None = None) -> None:
        self._flag = Path(flag_path)
        self._log_path = Path(log_path)
        self._enabled = False
        self._enable = enable_fn or self._default_enable
        self._disable = disable_fn or debuglog.disable

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _default_enable(self) -> None:
        debuglog.enable(sink=self._log_path, max_bytes=_MAX_BYTES,
                        backups=_BACKUPS)

    def tick(self) -> None:
        """One poll iteration. Never raises (must not take down the agent)."""
        try:
            present = self._flag.exists()
            if present and not self._enabled:
                self._enable()
                self._enabled = True
            elif not present and self._enabled:
                self._disable()
                self._enabled = False
        except Exception:
            # Leave _enabled unchanged so the next tick retries the transition.
            pass

    def run_forever(self, *, sleep_fn: Callable[[float], None] = time.sleep,
                    poll_seconds: float = POLL_SECONDS) -> None:
        while True:
            self.tick()
            sleep_fn(poll_seconds)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/ru_agent/test_debug_poll.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Verify the AST guard still passes**

Run: `python -m pytest tests/unit/ru_agent/test_ast_no_controller_imports.py -q`
Expected: PASS (debug_poll imports only `mthydra.debuglog`, not `mthydra.controller`).

- [ ] **Step 6: Commit**

```bash
git add src/mthydra/ru_agent/debug_poll.py tests/unit/ru_agent/test_debug_poll.py
git commit -m "feat(debug): RU live debug-flag poller (tmpfs, no restart)"
```

---

## Task 7: Start the RU poller thread in the agent

**Files:**
- Modify: `src/mthydra/ru_agent/__main__.py` (import + start a daemon thread alongside the descriptor-refresh / periodic-recheck threads, after `sup.launch_all()`)
- Test: smoke import (the module is excluded from coverage; behaviour is covered by Task 6).

- [ ] **Step 1: Add the import**

In `src/mthydra/ru_agent/__main__.py`, add to the `from mthydra.ru_agent import (...)` group:

```python
from mthydra.ru_agent import debug_poll
```

- [ ] **Step 2: Start the poller thread**

In `main()`, after the `refresh`/`_periodic_recheck` threads are started and before `sup.run_forever()`, add:

```python
    # Live debug toggle: poll /run/mthydra/debug.flag (tmpfs). touch -> verbose
    # debug to /run/mthydra/debug/; rm -> off. No restart (would kill the box).
    threading.Thread(
        target=debug_poll.DebugPoller().run_forever,
        daemon=True, name="debug-poll",
    ).start()
```

- [ ] **Step 3: Write the smoke test**

```python
# Append to tests/unit/ru_agent/test_debug_poll.py
import importlib


def test_agent_main_module_imports():
    # __main__ wires debug_poll; importing it must not raise.
    importlib.import_module("mthydra.ru_agent.__main__")
```

- [ ] **Step 4: Run the smoke test**

Run: `python -m pytest tests/unit/ru_agent/test_debug_poll.py::test_agent_main_module_imports -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/ru_agent/__main__.py tests/unit/ru_agent/test_debug_poll.py
git commit -m "feat(debug): start RU debug-flag poller thread in agent"
```

---

## Task 8: EU instrumentation (incoming connections + DB)

**Files:**
- Modify: `src/mthydra/controller/state/db.py` (log in `connect`)
- Modify: `src/mthydra/controller/data_exit/exit_observer.py` (log observed sessions/exits)
- Test: `tests/unit/controller/test_debug_instrumentation_eu.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/controller/test_debug_instrumentation_eu.py
import logging

from mthydra import debuglog
from mthydra.controller.state.db import connect


def test_db_connect_emits_debug_when_enabled(tmp_path, caplog):
    debuglog.disable()
    db = tmp_path / "state.sqlite"
    # Disabled: no debug line.
    with caplog.at_level(logging.DEBUG, logger=debuglog.LOGGER_NAME):
        connect(db).close()
    assert "category=db" not in caplog.text

    debuglog.enable()
    try:
        with caplog.at_level(logging.DEBUG, logger=debuglog.LOGGER_NAME):
            connect(db).close()
        assert "category=db" in caplog.text
    finally:
        debuglog.disable()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/controller/test_debug_instrumentation_eu.py -q`
Expected: FAIL (`category=db` not found — no instrumentation yet).

- [ ] **Step 3: Instrument `db.connect`**

In `src/mthydra/controller/state/db.py`, add `from mthydra import debuglog` to the imports, and at the end of `connect(...)` just before `return conn`, add:

```python
    debuglog.log("db", "connect", path=str(path))
```

(Use the parameter name actually present in `connect`'s signature for `path`; if it is named `db_path`, use `db_path` instead.)

- [ ] **Step 4: Instrument the exit observer**

In `src/mthydra/controller/data_exit/exit_observer.py`, add `from mthydra import debuglog` to the imports, and at the point where a session/exit is observed (inside the observe/poll method that processes a session record), add a gated line, e.g.:

```python
        debuglog.log("conn", "observed session", src=str(src_ip), exit=str(exit_id))
```

Match the real local variable names for the source address and exit identifier in that method.

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/unit/controller/test_debug_instrumentation_eu.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/mthydra/controller/state/db.py \
        src/mthydra/controller/data_exit/exit_observer.py \
        tests/unit/controller/test_debug_instrumentation_eu.py
git commit -m "feat(debug): instrument EU DB + incoming-connection paths"
```

---

## Task 9: RU instrumentation (seed verify + child restart + refresh tick)

**Files:**
- Modify: `src/mthydra/ru_agent/seed.py` (log after successful verify)
- Modify: `src/mthydra/ru_agent/supervisor.py` (log child launch/restart)
- Modify: `src/mthydra/ru_agent/descriptor_refresh.py` (log each tick)
- Test: `tests/unit/ru_agent/test_debug_instrumentation_ru.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/ru_agent/test_debug_instrumentation_ru.py
import logging

from mthydra import debuglog
from mthydra.ru_agent import supervisor


def test_supervisor_launch_emits_debug_when_enabled(caplog, monkeypatch):
    # Avoid spawning real processes: stub Popen.
    class _FakeProc:
        def poll(self):
            return None

    monkeypatch.setattr(supervisor.subprocess, "Popen",
                        lambda *a, **k: _FakeProc())
    sup = supervisor.Supervisor(mtg_cmd=["true"], sing_box_cmd=["true"])

    debuglog.disable()
    with caplog.at_level(logging.DEBUG, logger=debuglog.LOGGER_NAME):
        sup.launch_all()
    assert "category=child" not in caplog.text

    debuglog.enable()
    try:
        with caplog.at_level(logging.DEBUG, logger=debuglog.LOGGER_NAME):
            sup.launch_all()
        assert "category=child" in caplog.text
    finally:
        debuglog.disable()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/ru_agent/test_debug_instrumentation_ru.py -q`
Expected: FAIL (`category=child` not found).

- [ ] **Step 3: Instrument the modules**

`src/mthydra/ru_agent/supervisor.py` — add `from mthydra import debuglog` to imports; in the method that starts a child (`launch_all` and/or the per-child start helper), after the process is created, add:

```python
        debuglog.log("child", "launched", name=name, cmd=" ".join(cmd))
```

Use the local variable names actually present (child name + argv list). If `launch_all` starts mtg and sing-box directly, emit one line per child with its label and command.

`src/mthydra/ru_agent/seed.py` — add `from mthydra import debuglog`; at the end of the successful verify path (just before returning the `Seed`), add:

```python
    debuglog.log("seed", "verified", box_id=seed.box_id, schema=seed.schema)
```

Match the parsed object's actual attribute names.

`src/mthydra/ru_agent/descriptor_refresh.py` — add `from mthydra import debuglog`; inside `run_forever`'s loop body (each tick), add:

```python
            debuglog.log("refresh", "tick")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/ru_agent/test_debug_instrumentation_ru.py -q`
Expected: PASS.

- [ ] **Step 5: Verify AST guard still passes**

Run: `python -m pytest tests/unit/ru_agent/test_ast_no_controller_imports.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/mthydra/ru_agent/seed.py src/mthydra/ru_agent/supervisor.py \
        src/mthydra/ru_agent/descriptor_refresh.py \
        tests/unit/ru_agent/test_debug_instrumentation_ru.py
git commit -m "feat(debug): instrument RU seed/supervisor/refresh paths"
```

---

## Task 10: Operator docs + CHANGELOG

**Files:**
- Modify: `doc/runbook.md` (new "Debug mode" section)
- Modify: `CHANGELOG.md` (feature entry)

- [ ] **Step 1: Add the runbook section**

Append a "Debug mode" section to `doc/runbook.md` (match the file's existing heading style/numbering):

```markdown
## Debug mode

Verbose, **UNREDACTED** diagnostics (incoming connections, DB activity, network
talks). Output may contain user IPs, session identifiers and secrets — enable
only while actively debugging, and prefer disabling it as soon as you are done.

### EU controller (persistent host, restart-safe)

    sudo mthydra-controller debug enable            # 24h auto-expire, restarts service
    sudo mthydra-controller debug enable --ttl-hours 2
    sudo mthydra-controller debug status            # ON/OFF/EXPIRED + remaining TTL
    sudo mthydra-controller debug disable           # restarts service

Logs land in `/var/log/mthydra/debug.log` (rotated, 10 MB × 5). Debug
auto-reverts to normal after the TTL without a restart; `disable` also restarts
to drop verbosity immediately. `--no-restart` updates the flag only.

### RU box (tmpfs only, MUST NOT restart)

The box cannot be restarted (tmpfs seed + once-per-instance cloud-init). Toggle
on the live agent via a flag file on tmpfs:

    touch /run/mthydra/debug.flag    # debug ON within ~5s
    rm    /run/mthydra/debug.flag    # debug OFF

Output goes to `/run/mthydra/debug/agent-debug.log` (tmpfs) and journald; it is
wiped on reboot and never touches persistent storage. **Seizure warning:** a
powered-on box exposes `/run`. Do not leave RU debug enabled on a box you
cannot promptly wipe.
```

- [ ] **Step 2: Add the CHANGELOG entry**

Add under the top/unreleased section of `CHANGELOG.md` (match existing style):

```markdown
- feat(debug): operator debug mode — `mthydra-controller debug enable/disable/status`
  (rotating `/var/log/mthydra/debug.log`, 24h auto-expire) on EU; live tmpfs
  flag file `/run/mthydra/debug.flag` on RU (no restart). Verbose/unredacted;
  off by default.
```

- [ ] **Step 3: Commit**

```bash
git add doc/runbook.md CHANGELOG.md
git commit -m "docs(debug): runbook debug-mode section + CHANGELOG"
```

---

## Task 11: Full verification

- [ ] **Step 1: Run the whole suite**

Run: `python -m pytest -q`
Expected: PASS — all new tests plus the existing suite. (Per project memory, 3 pre-existing "box has no shard" failures + 1 gap_monitor collection error may already fail on `main` independent of this work; confirm the count matches `main` and that no *new* failures were introduced.)

- [ ] **Step 2: Lint the changed files only**

Run: `ruff check src/mthydra/debuglog.py src/mthydra/controller/debug_flag.py src/mthydra/controller/debug_runtime.py src/mthydra/ru_agent/debug_poll.py`
Expected: no errors. (Lint scoped to changed files — local ruff is far newer than the pinned `>=0.5`, so a repo-wide run reports phantom issues; see project memory.)

- [ ] **Step 3: Confirm the RU boundary holds**

Run: `python -m pytest tests/unit/ru_agent/test_ast_no_controller_imports.py -q`
Expected: PASS — no `mthydra.controller` import leaked into the RU agent.

- [ ] **Step 4: Final commit (if anything outstanding) + push**

```bash
git push origin main
```

---

## Self-Review notes

- **Spec coverage:** shared core (T1), EU flag (T2), EU arm+expire (T3), EU CLI (T4), serve wiring (T5), RU poller (T6) + thread (T7), instrumentation EU (T8) + RU (T9), security posture (banner in T1, tmpfs-only RU in T6, rotate+TTL in T1/T3), docs (T10), tests + AST guard + lint (every task + T11). The deferred `[debug]` config section is intentionally out of scope (spec "Defaults" + "Out of scope").
- **Types:** `DebugFlag.is_expired(now)`, `debug_flag.{read,write,clear}_flag`, `debug_runtime.{arm_from_flag,expiry_tick}`, `debuglog.{enable,disable,is_enabled,log}`, `DebugPoller.tick/run_forever/enabled` are used consistently across tasks.
- **Instrumentation caveat:** Tasks 8–9 reference local variable / attribute names (`path`/`db_path`, exit-observer session fields, seed attributes, supervisor child labels) that the implementer must match to the real code at edit time — each is flagged inline.
