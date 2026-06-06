# RU→EU End-to-End Connectivity Check (K3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect a broken RU→EU tunnel — the failure that lets an RU box pass `telnet :443` while Telegram cannot connect through it — via a box-side end-to-end self-check (local diagnostic) plus a controller-side corroboration alert derived from the co-located EU exit's live sessions.

**Architecture:** Two cooperating halves. (1) The RU agent, inside its existing 15-min recheck loop, opens a TCP connection to a Telegram-DC IP that its own iptables REDIRECT forces through sing-box → Reality → EU exit → Telegram, classifies upstream-established vs. broken, and writes the verdict to `/run/mthydra/health.json` + the journal. (2) On the active EU node (which IS the controller host), a wheel polls the local sing-box `clash_api` `/connections`, records which `box_id`s have live sessions into a new `eu_exit_observed` table, and raises the anti-obligation `box_eu_tunnel_unseen::<box_id>` for any `state='live'` box not seen within a threshold — surfaced through the existing alerter/remediation plumbing.

**Tech Stack:** Python 3, SQLite (forward-only schema migrations), sing-box (VLESS/Reality server + clash_api), iptables nat/REDIRECT, APScheduler wheels, pytest. No new third-party runtime dependency (uses stdlib `socket`, `urllib`, `json`).

**Spec:** `doc/specs/2026-06-06-K3-ru-eu-connectivity-check.md`

---

## File structure

**New files:**
- `src/mthydra/ru_agent/tunnel_check.py` — box-side probe + health.json writer. Pure logic + socket I/O; no DB/controller import.
- `src/mthydra/controller/state/eu_exit_observed.py` — repo for the `eu_exit_observed` table (`record_seen`, `last_seen`, `prune`).
- `src/mthydra/controller/data_exit/session_reader.py` — `poll_active_sessions(clash_api_url)` → `set[str]` of box_ids. HTTP-only, no DB.
- `src/mthydra/controller/data_exit/exit_observer.py` — `EuExitObserver` wheel: per tick, poll + record + sweep (raise/clear `box_eu_tunnel_unseen`).
- Tests mirroring each under `tests/unit/...` plus one integration test.

**Modified files:**
- `src/mthydra/controller/state/schema.py` — `SCHEMA_VERSION` 17→18, `migrate_v17_to_v18`, ladder entry, `eu_exit_observed` in `_STATEMENTS`.
- `src/mthydra/controller/config.py` — `DataExitConfig.clash_api_listen` field + parse.
- `src/mthydra/controller/data_exit/config_writer.py` — emit localhost `experimental.clash_api`.
- `src/mthydra/ru_agent/__main__.py` — call `tunnel_check` in `_periodic_recheck`.
- `src/mthydra/controller/observability/snapshot.py` — add `box_eu_tunnel_unseen` to `_ANTI_PREFIXES`.
- `src/mthydra/controller/observability/remediation.py` — add `box_eu_tunnel_unseen` remediation line.
- `src/mthydra/controller/cli.py` — arm `EuExitObserver` in `_cmd_serve`, active role only.
- `CHANGELOG.md`.

**Test command note:** local `ruff` is 0.15 (repo pins >=0.5); a blanket lint shows ~165 phantom errors. Scope lint to changed files: `ruff check --select I,F <file>`. Run tests with `python -m pytest`.

---

## Task 1: `eu_exit_observed` table + migration + repo

**Files:**
- Modify: `src/mthydra/controller/state/schema.py`
- Create: `src/mthydra/controller/state/eu_exit_observed.py`
- Test: `tests/unit/controller/state/test_eu_exit_observed.py`

- [ ] **Step 1: Write the failing repo test**

Create `tests/unit/controller/state/test_eu_exit_observed.py`:

```python
from __future__ import annotations

from mthydra.controller.state import eu_exit_observed as obs
from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema


def _db(tmp_path):
    c = connect(tmp_path / "s.sqlite")
    apply_schema(c)
    return c


def test_record_then_last_seen_roundtrip(tmp_path):
    c = _db(tmp_path)
    assert obs.last_seen(c, "box-1") is None
    obs.record_seen(c, "box-1", "2026-06-06T10:00:00Z")
    assert obs.last_seen(c, "box-1") == "2026-06-06T10:00:00Z"


def test_record_seen_is_upsert_keeps_latest(tmp_path):
    c = _db(tmp_path)
    obs.record_seen(c, "box-1", "2026-06-06T10:00:00Z")
    obs.record_seen(c, "box-1", "2026-06-06T10:05:00Z")
    assert obs.last_seen(c, "box-1") == "2026-06-06T10:05:00Z"
    # exactly one row per box
    n = c.execute("SELECT COUNT(*) FROM eu_exit_observed WHERE box_id='box-1'").fetchone()[0]
    assert n == 1
```

- [ ] **Step 2: Run it; expect failure**

Run: `python -m pytest tests/unit/controller/state/test_eu_exit_observed.py -v`
Expected: FAIL — `no such table: eu_exit_observed` / `ModuleNotFoundError`.

- [ ] **Step 3: Add the table to schema + migration**

In `src/mthydra/controller/state/schema.py`: bump `SCHEMA_VERSION = 18`.

Add the `CREATE TABLE` to the `_STATEMENTS` list (so fresh installs get it) — place alongside the other `CREATE TABLE IF NOT EXISTS` statements:

```python
        """
        CREATE TABLE IF NOT EXISTS eu_exit_observed (
            box_id       TEXT PRIMARY KEY,
            last_seen_at TEXT NOT NULL
        )
        """,
```

Add the migration function near the other `migrate_vN_to_vN+1` defs:

```python
def migrate_v17_to_v18(conn: sqlite3.Connection) -> None:
    """K3: eu_exit_observed — last time each box had a live session at the EU exit."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS eu_exit_observed ("
        "  box_id TEXT PRIMARY KEY,"
        "  last_seen_at TEXT NOT NULL"
        ")"
    )
    conn.execute(
        "UPDATE schema_version SET version=?, applied_at=? WHERE rowid=1",
        (18, _now()),
    )
    conn.commit()
```

In `apply_schema`, add to the migration ladder after the `if current < 17:` block (match the existing pattern):

```python
        if current < 18:
            migrate_v17_to_v18(conn)
```

- [ ] **Step 4: Write the repo module**

Create `src/mthydra/controller/state/eu_exit_observed.py`:

```python
"""K3: per-box record of the last live session observed at the EU exit.

One row per box_id, upserted each time the exit's clash_api reports a live
VLESS session for that box. The alerter sweep compares last_seen_at against a
freshness threshold to flag boxes that should be tunnelling but are not.
"""
from __future__ import annotations

import sqlite3


def record_seen(conn: sqlite3.Connection, box_id: str, at: str) -> None:
    """Upsert the box's last-seen timestamp (monotonic-ish: callers pass 'now')."""
    conn.execute(
        "INSERT INTO eu_exit_observed (box_id, last_seen_at) VALUES (?, ?) "
        "ON CONFLICT(box_id) DO UPDATE SET last_seen_at=excluded.last_seen_at",
        (box_id, at),
    )


def last_seen(conn: sqlite3.Connection, box_id: str) -> str | None:
    row = conn.execute(
        "SELECT last_seen_at FROM eu_exit_observed WHERE box_id=?", (box_id,)
    ).fetchone()
    return row[0] if row else None
```

- [ ] **Step 5: Run tests; expect pass**

Run: `python -m pytest tests/unit/controller/state/test_eu_exit_observed.py -v`
Expected: PASS (2 tests).

- [ ] **Step 6: Verify the migration applies forward**

Run: `python -m pytest tests/unit/controller/state -k "schema or migrat" -v`
Expected: PASS — existing schema/migration tests still green at version 18. If a test asserts the exact `SCHEMA_VERSION`, update it to 18.

- [ ] **Step 7: Commit**

```bash
git add src/mthydra/controller/state/schema.py src/mthydra/controller/state/eu_exit_observed.py tests/unit/controller/state/test_eu_exit_observed.py
git commit -m "feat(K3): eu_exit_observed table + repo (schema v18)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: clash_api `/connections` parser (`session_reader.py`)

**Files:**
- Create: `src/mthydra/controller/data_exit/session_reader.py`
- Test: `tests/unit/controller/data_exit/test_session_reader.py`

Background: sing-box `clash_api` serves `GET /connections` returning JSON shaped
`{"connections": [{"metadata": {...}}, ...]}`. The per-box user name appears in
the connection metadata. sing-box versions have used both `inboundUser` and
`user`; the parser checks both so it survives a version skew (verify against the
running sing-box during integration). The EU exit names its VLESS users by
`box_id` (`data_exit/config_writer._live_users`), so the user field IS the box id.

- [ ] **Step 1: Write the failing parser test**

Create `tests/unit/controller/data_exit/test_session_reader.py`:

```python
from __future__ import annotations

import json

from mthydra.controller.data_exit import session_reader as sr


def test_parses_box_ids_from_inbounduser():
    body = json.dumps({"connections": [
        {"metadata": {"inboundUser": "box-1"}},
        {"metadata": {"inboundUser": "box-2"}},
        {"metadata": {"inboundUser": "box-1"}},  # duplicate -> set dedupes
    ]})
    assert sr.parse_connections(body) == {"box-1", "box-2"}


def test_parses_user_fallback_key():
    body = json.dumps({"connections": [{"metadata": {"user": "box-9"}}]})
    assert sr.parse_connections(body) == {"box-9"}


def test_empty_and_userless_connections_ignored():
    body = json.dumps({"connections": [
        {"metadata": {}},                 # no user -> skipped
        {"metadata": {"inboundUser": ""}},  # empty -> skipped
    ]})
    assert sr.parse_connections(body) == set()


def test_missing_connections_key_is_empty():
    assert sr.parse_connections("{}") == set()
```

- [ ] **Step 2: Run it; expect failure**

Run: `python -m pytest tests/unit/controller/data_exit/test_session_reader.py -v`
Expected: FAIL — `ModuleNotFoundError: ...session_reader`.

- [ ] **Step 3: Implement the parser + HTTP poll**

Create `src/mthydra/controller/data_exit/session_reader.py`:

```python
"""K3: read live VLESS sessions from the local sing-box clash_api.

Pure parse (parse_connections) is separated from I/O (poll_active_sessions) so
the parse is unit-tested without a network. The EU exit names its VLESS users by
box_id, so each live connection's user field is the box id with a live tunnel.
"""
from __future__ import annotations

import json
import urllib.request


def parse_connections(body: str) -> set[str]:
    """Extract the set of box_ids from a clash_api /connections JSON body."""
    try:
        doc = json.loads(body)
    except (ValueError, TypeError):
        return set()
    out: set[str] = set()
    for c in doc.get("connections") or []:
        meta = c.get("metadata") or {}
        user = meta.get("inboundUser") or meta.get("user") or ""
        if user:
            out.add(user)
    return out


def poll_active_sessions(clash_api_url: str, *, timeout: float = 5.0) -> set[str]:
    """GET <clash_api_url>/connections and return the box_ids with live sessions.

    Raises on connection/HTTP error — the caller decides how to treat an
    unreadable API (K3: treat as 'no observations this tick', never as
    'all boxes broken')."""
    url = clash_api_url.rstrip("/") + "/connections"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    return parse_connections(body)
```

- [ ] **Step 4: Run tests; expect pass**

Run: `python -m pytest tests/unit/controller/data_exit/test_session_reader.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/data_exit/session_reader.py tests/unit/controller/data_exit/test_session_reader.py
git commit -m "feat(K3): clash_api /connections session reader

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Enable localhost clash_api on the EU exit config

**Files:**
- Modify: `src/mthydra/controller/config.py` (DataExitConfig)
- Modify: `src/mthydra/controller/data_exit/config_writer.py`
- Test: `tests/unit/controller/data_exit/test_config_writer.py` (existing — extend)

- [ ] **Step 1: Add the config field (failing-config test first)**

Add to `tests/unit/controller/data_exit/test_config_writer.py` (new test; keep existing ones):

```python
def test_config_enables_localhost_clash_api():
    import json
    from mthydra.controller.config import DataExitConfig
    from mthydra.controller.data_exit.config_writer import render_sing_box_config
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema

    cfg = DataExitConfig(
        listen_port=443, config_path="/tmp/x.json",
        reality_key_path="/tmp/k", telegram_dcs_v4=(), telegram_dcs_v6=(),
        cover_sni_default="example.com", clash_api_listen="127.0.0.1:9090",
    )
    c = connect(":memory:"); apply_schema(c)
    doc = json.loads(render_sing_box_config(
        c, cfg, node_id="eu-1", cover_sni="example.com",
        reality_private_key="priv"))
    assert doc["experimental"]["clash_api"]["external_controller"] == "127.0.0.1:9090"
```

- [ ] **Step 2: Run it; expect failure**

Run: `python -m pytest tests/unit/controller/data_exit/test_config_writer.py::test_config_enables_localhost_clash_api -v`
Expected: FAIL — `DataExitConfig` has no `clash_api_listen` (TypeError) or no `experimental` key (KeyError).

- [ ] **Step 3: Add the field to DataExitConfig + parse**

In `src/mthydra/controller/config.py`, add to the `DataExitConfig` dataclass (after `cover_sni_per_node`):

```python
    clash_api_listen: str = "127.0.0.1:9090"
```

In the loader (where `DataExitConfig(...)` is constructed, ~line 295), add the kwarg:

```python
            clash_api_listen=str(de_raw.get("clash_api_listen", "127.0.0.1:9090")),
```

- [ ] **Step 4: Emit clash_api in the rendered config**

In `src/mthydra/controller/data_exit/config_writer.py`, the `render_sing_box_config`
`payload` dict gains an `experimental` block. Add it after the `route` key:

```python
        "experimental": {
            "clash_api": {
                # localhost only — read by the co-located controller's
                # EuExitObserver (K3). MUST NOT bind a public interface.
                "external_controller": cfg.clash_api_listen,
            }
        },
```

- [ ] **Step 5: Run tests; expect pass**

Run: `python -m pytest tests/unit/controller/data_exit/test_config_writer.py -v`
Expected: PASS — new test passes; existing config_writer tests still pass (they
assert other keys; `experimental` is additive). If an existing test does a
whole-dict equality, update its expected dict to include the `experimental` block.

- [ ] **Step 6: Commit**

```bash
git add src/mthydra/controller/config.py src/mthydra/controller/data_exit/config_writer.py tests/unit/controller/data_exit/test_config_writer.py
git commit -m "feat(K3): EU exit serves localhost-bound clash_api

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Box-side tunnel self-check (`tunnel_check.py`)

**Files:**
- Create: `src/mthydra/ru_agent/tunnel_check.py`
- Test: `tests/unit/ru_agent/test_tunnel_check.py`

Design of the success predicate (spec K3-D2): a bare `connect()` returns on
sing-box's *local* accept and proves nothing. So the probe connects, sends a few
bytes, then attempts a short bounded read. A broken upstream makes sing-box close
the local socket promptly → the read returns empty (EOF) or errors quickly →
**FAIL**. A healthy tunnel reaches Telegram, which (for a non-MTProto probe)
either holds the connection open past the short read window or returns bytes →
**OK**. To keep this unit-testable without a network, the socket layer is injected
via a `connect_fn` returning an object with `sendall`, `recv`, `close`.

- [ ] **Step 1: Write the failing test**

Create `tests/unit/ru_agent/test_tunnel_check.py`:

```python
from __future__ import annotations

import json

from mthydra.ru_agent import tunnel_check as tc


class FakeSock:
    """Models a redirected socket. `held` True = upstream alive (recv times out
    -> raises timeout, meaning peer is holding the conn). `held` False = broken
    upstream (sing-box closed it) -> recv returns b'' (EOF)."""
    def __init__(self, *, held: bool):
        self._held = held
        self.closed = False

    def sendall(self, data): pass

    def recv(self, n):
        if self._held:
            raise TimeoutError("peer holding connection open")  # healthy
        return b""  # EOF -> upstream dead

    def close(self): self.closed = True


def test_held_connection_is_ok():
    v = tc.check_eu_tunnel(
        dc_ips=["149.154.167.51"],
        connect_fn=lambda ip, port, timeout: FakeSock(held=True),
        clock=lambda: "2026-06-06T10:00:00Z",
    )
    assert v.verdict == "ok"
    assert v.telegram_dc_tried == "149.154.167.51"


def test_eof_connection_is_fail():
    v = tc.check_eu_tunnel(
        dc_ips=["149.154.167.51"],
        connect_fn=lambda ip, port, timeout: FakeSock(held=False),
        clock=lambda: "2026-06-06T10:00:00Z",
    )
    assert v.verdict == "fail"
    assert "eof" in v.detail.lower()


def test_connect_error_is_fail_not_crash():
    def boom(ip, port, timeout):
        raise OSError("no route to host")
    v = tc.check_eu_tunnel(
        dc_ips=["149.154.167.51"], connect_fn=boom,
        clock=lambda: "2026-06-06T10:00:00Z",
    )
    assert v.verdict == "fail"
    assert "no route" in v.detail.lower()


def test_no_dc_ips_is_fail():
    v = tc.check_eu_tunnel(
        dc_ips=[], connect_fn=lambda *a, **k: FakeSock(held=True),
        clock=lambda: "2026-06-06T10:00:00Z",
    )
    assert v.verdict == "fail"


def test_write_health_writes_json(tmp_path):
    v = tc.Verdict(checked_at="2026-06-06T10:00:00Z", verdict="ok",
                   detail="held", telegram_dc_tried="149.154.167.51")
    p = tmp_path / "health.json"
    tc.write_health(str(p), v)
    doc = json.loads(p.read_text())
    assert doc["verdict"] == "ok"
    assert doc["telegram_dc_tried"] == "149.154.167.51"
    assert doc["checked_at"] == "2026-06-06T10:00:00Z"
```

- [ ] **Step 2: Run it; expect failure**

Run: `python -m pytest tests/unit/ru_agent/test_tunnel_check.py -v`
Expected: FAIL — `ModuleNotFoundError: ...tunnel_check`.

- [ ] **Step 3: Implement the module**

Create `src/mthydra/ru_agent/tunnel_check.py`:

```python
"""K3: box-side end-to-end RU->EU tunnel self-check.

Opens a TCP connection to a Telegram-DC IP:443. The box's own MTHYDRA_DCS
iptables REDIRECT pushes that connection into sing-box -> Reality tunnel -> EU
exit -> Telegram, so this exercises the exact real path. The success predicate
proves the UPSTREAM established (not just sing-box's local accept): a broken
tunnel makes sing-box close the local socket promptly (recv -> EOF), a healthy
tunnel reaches Telegram which holds the connection (recv -> timeout) or answers.

Verdict is written to /run/mthydra/health.json and logged by the caller.
"""
from __future__ import annotations

import json
import os
import socket
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Callable

DC_PORT = 443
PROBE_BYTES = b"\x00" * 8          # minimal nudge so the upstream must engage
READ_TIMEOUT_SECONDS = 3.0
CONNECT_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class Verdict:
    checked_at: str
    verdict: str              # "ok" | "fail"
    detail: str
    telegram_dc_tried: str | None


def _default_clock() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _real_connect(ip: str, port: int, timeout: float):
    s = socket.create_connection((ip, port), timeout=timeout)
    s.settimeout(READ_TIMEOUT_SECONDS)
    return s


def check_eu_tunnel(
    *,
    dc_ips: list[str],
    connect_fn: Callable[[str, int, float], object] | None = None,
    clock: Callable[[], str] | None = None,
) -> Verdict:
    """Probe the first reachable Telegram DC through the tunnel. OK if the
    upstream holds the connection (timeout on read) or returns data; FAIL on
    EOF (sing-box closed it -> upstream dead) or any connect error."""
    now = (clock or _default_clock)()
    connect_fn = connect_fn or _real_connect
    if not dc_ips:
        return Verdict(now, "fail", "no telegram DC IPs in seed", None)

    last_detail = "no DC attempted"
    for ip in dc_ips:
        sock = None
        try:
            sock = connect_fn(ip, DC_PORT, CONNECT_TIMEOUT_SECONDS)
            sock.sendall(PROBE_BYTES)
            try:
                data = sock.recv(64)
            except (TimeoutError, socket.timeout):
                # Peer is holding the connection open -> upstream is alive.
                return Verdict(now, "ok", "upstream held connection", ip)
            if data:
                return Verdict(now, "ok", "upstream returned data", ip)
            last_detail = f"{ip}: EOF on read (sing-box closed; upstream dead)"
        except OSError as e:
            last_detail = f"{ip}: {e}"
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
    return Verdict(now, "fail", last_detail, dc_ips[0])


def write_health(path: str, verdict: Verdict) -> None:
    """Atomically write the verdict JSON to `path` (/run is tmpfs)."""
    data = json.dumps(asdict(verdict), sort_keys=True).encode("utf-8")
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d)
    try:
        os.write(fd, data)
        os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        with contextlib_suppress():
            os.unlink(tmp)
        raise


import contextlib  # noqa: E402  (kept near sole use)


def contextlib_suppress():
    return contextlib.suppress(OSError)
```

Note: if the `contextlib` shim reads awkwardly in review, replace `write_health`'s
cleanup with a direct `with contextlib.suppress(OSError): os.unlink(tmp)` and move
`import contextlib` to the top. The behavior is identical; pick whichever the
reviewer prefers. Tests do not depend on the cleanup path.

- [ ] **Step 4: Run tests; expect pass**

Run: `python -m pytest tests/unit/ru_agent/test_tunnel_check.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Lint the new file**

Run: `ruff check --select I,F src/mthydra/ru_agent/tunnel_check.py`
Expected: clean (fix imports if flagged; prefer the top-level `import contextlib`
form noted above to avoid E402).

- [ ] **Step 6: Commit**

```bash
git add src/mthydra/ru_agent/tunnel_check.py tests/unit/ru_agent/test_tunnel_check.py
git commit -m "feat(K3): box-side end-to-end EU tunnel self-check

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Wire the self-check into the agent recheck loop

**Files:**
- Modify: `src/mthydra/ru_agent/__main__.py` (`_periodic_recheck`, constants)
- Test: `tests/unit/ru_agent/test_main_recheck_tunnel.py`

The existing `_periodic_recheck` loop (in `main()`) sleeps 15 min, re-verifies
hardening + iptables. We add the tunnel check each tick. The check must never
raise out of the loop (the loop also guards hardening/iptables and must keep
running). Refactor the per-tick body into a small testable helper.

- [ ] **Step 1: Read the current loop**

Run: `grep -n "_periodic_recheck\|HEALTH\|/run/mthydra" src/mthydra/ru_agent/__main__.py`
Note the loop body and the `/run/mthydra` path constants (`MTG_CONFIG_PATH` etc.).

- [ ] **Step 2: Add a HEALTH_PATH constant**

In `src/mthydra/ru_agent/__main__.py`, beside the other `/run/mthydra/...`
constants (`SEED_PATH`, `MTG_PATH`, ...):

```python
HEALTH_PATH = "/run/mthydra/health.json"
```

- [ ] **Step 3: Write the failing helper test**

Create `tests/unit/ru_agent/test_main_recheck_tunnel.py`:

```python
from __future__ import annotations

import json

from mthydra.ru_agent import __main__ as agent_main


def test_run_tunnel_check_writes_health(tmp_path, monkeypatch):
    health = tmp_path / "health.json"
    monkeypatch.setattr(agent_main, "HEALTH_PATH", str(health))
    logged = []
    agent_main._run_tunnel_check(
        dc_ips=["149.154.167.51"],
        connect_fn=lambda ip, port, timeout: _Held(),
        log=logged.append,
        clock=lambda: "2026-06-06T10:00:00Z",
    )
    assert json.loads(health.read_text())["verdict"] == "ok"


def test_run_tunnel_check_logs_loudly_on_fail(tmp_path, monkeypatch):
    health = tmp_path / "health.json"
    monkeypatch.setattr(agent_main, "HEALTH_PATH", str(health))
    logged = []

    def boom(ip, port, timeout):
        raise OSError("dead")

    agent_main._run_tunnel_check(
        dc_ips=["149.154.167.51"], connect_fn=boom, log=logged.append,
        clock=lambda: "2026-06-06T10:00:00Z",
    )
    assert json.loads(health.read_text())["verdict"] == "fail"
    assert any("EU tunnel check FAILED" in m for m in logged)


def test_run_tunnel_check_never_raises(tmp_path, monkeypatch):
    # Even if writing health throws, the loop must not crash.
    monkeypatch.setattr(agent_main, "HEALTH_PATH", "/nonexistent-dir/health.json")
    agent_main._run_tunnel_check(
        dc_ips=["149.154.167.51"],
        connect_fn=lambda ip, port, timeout: _Held(),
        log=lambda m: None, clock=lambda: "2026-06-06T10:00:00Z",
    )  # must return without raising


class _Held:
    def sendall(self, d): pass
    def recv(self, n): raise TimeoutError()
    def close(self): pass
```

- [ ] **Step 3b: Run it; expect failure**

Run: `python -m pytest tests/unit/ru_agent/test_main_recheck_tunnel.py -v`
Expected: FAIL — `_run_tunnel_check` not defined.

- [ ] **Step 4: Implement the helper + call it in the loop**

In `src/mthydra/ru_agent/__main__.py`, add near the top imports:

```python
from mthydra.ru_agent import tunnel_check
```

Add the helper (module level):

```python
def _run_tunnel_check(*, dc_ips, connect_fn=None, log=None, clock=None) -> None:
    """Run the EU tunnel self-check, write health.json, log the verdict.

    Never raises: this runs inside the periodic recheck loop, which must keep
    re-verifying hardening + iptables regardless of the probe outcome."""
    log = log or (lambda m: print(m, file=sys.stderr, flush=True))
    try:
        v = tunnel_check.check_eu_tunnel(
            dc_ips=dc_ips, connect_fn=connect_fn, clock=clock)
        try:
            tunnel_check.write_health(HEALTH_PATH, v)
        except OSError as e:
            log(f"agent: could not write {HEALTH_PATH}: {e}")
        if v.verdict == "ok":
            log(f"agent: EU tunnel check ok via {v.telegram_dc_tried}")
        else:
            log(f"agent: EU tunnel check FAILED — {v.detail}")
    except Exception as e:  # defensive: probe must never kill the loop
        log(f"agent: EU tunnel check raised (ignored): {e!r}")
```

In `_periodic_recheck`, inside the `while True:` body (after the hardening +
iptables re-verification, before/after the sleep is fine — keep it inside the
loop), add the DC IPs source from the seed `s` already in scope:

```python
            dc_ips = list(s.telegram_dcs.get("v4", [])) + list(s.telegram_dcs.get("v6", []))
            _run_tunnel_check(dc_ips=dc_ips)
```

- [ ] **Step 5: Run tests; expect pass**

Run: `python -m pytest tests/unit/ru_agent/test_main_recheck_tunnel.py -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Run the full ru_agent suite (no regressions)**

Run: `python -m pytest tests/unit/ru_agent -v`
Expected: PASS — existing agent tests unaffected.

- [ ] **Step 7: Commit**

```bash
git add src/mthydra/ru_agent/__main__.py tests/unit/ru_agent/test_main_recheck_tunnel.py
git commit -m "feat(K3): run EU tunnel self-check in agent recheck loop

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Corroboration wheel (`EuExitObserver`)

**Files:**
- Create: `src/mthydra/controller/data_exit/exit_observer.py`
- Test: `tests/unit/controller/data_exit/test_exit_observer.py`

Per tick: (1) poll the clash_api for live box sessions; record each into
`eu_exit_observed`. (2) sweep all `state='live'` boxes: if `last_seen_at` is
older than `UNSEEN_THRESHOLD_SECONDS` (or never seen), raise anti-obligation
`box_eu_tunnel_unseen::<box_id>`; otherwise clear it. A clash_api read error is
"no observations this tick" — it must NOT itself flag every box (K3 §6).

- [ ] **Step 1: Write the failing wheel test**

Create `tests/unit/controller/data_exit/test_exit_observer.py`:

```python
from __future__ import annotations

from mthydra.controller.data_exit.exit_observer import EuExitObserver
from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema


def _db(tmp_path):
    c = connect(tmp_path / "s.sqlite")
    apply_schema(c)
    return c


def _add_live_box(c, box_id):
    c.execute(
        "INSERT INTO ru_boxes (box_id, state, reality_uuid) VALUES (?, 'live', ?)",
        (box_id, f"uuid-{box_id}"),
    )
    c.commit()


def _has_unseen(c, box_id):
    return c.execute(
        "SELECT COUNT(*) FROM obligation_clocks WHERE obligation_id=?",
        (f"box_eu_tunnel_unseen::{box_id}",),
    ).fetchone()[0] == 1


def _obs(tmp_path, sessions, *, now, threshold=900):
    return EuExitObserver(
        db_path=tmp_path / "s.sqlite",
        clash_api_url="http://127.0.0.1:9090",
        poll_fn=lambda url, timeout=5.0: set(sessions),
        clock=lambda: now,
        unseen_threshold_seconds=threshold,
        mode="offline",
    )


def test_seen_box_is_recorded_and_not_flagged(tmp_path):
    c = _db(tmp_path); _add_live_box(c, "box-1"); c.close()
    _obs(tmp_path, {"box-1"}, now="2026-06-06T10:00:00Z").tick()
    c = connect(tmp_path / "s.sqlite")
    assert not _has_unseen(c, "box-1")


def test_never_seen_live_box_is_flagged(tmp_path):
    c = _db(tmp_path); _add_live_box(c, "box-1"); c.close()
    _obs(tmp_path, set(), now="2026-06-06T10:00:00Z").tick()
    c = connect(tmp_path / "s.sqlite")
    assert _has_unseen(c, "box-1")


def test_stale_then_seen_clears_flag(tmp_path):
    c = _db(tmp_path); _add_live_box(c, "box-1"); c.close()
    # tick 1: not seen -> flagged
    _obs(tmp_path, set(), now="2026-06-06T10:00:00Z").tick()
    c = connect(tmp_path / "s.sqlite"); assert _has_unseen(c, "box-1"); c.close()
    # tick 2: now seen -> cleared
    _obs(tmp_path, {"box-1"}, now="2026-06-06T10:10:00Z").tick()
    c = connect(tmp_path / "s.sqlite"); assert not _has_unseen(c, "box-1")


def test_poll_error_does_not_flag_everything(tmp_path):
    c = _db(tmp_path); _add_live_box(c, "box-1"); c.close()
    # box was seen recently...
    _obs(tmp_path, {"box-1"}, now="2026-06-06T10:00:00Z").tick()
    # ...then the clash_api goes unreadable on the next tick (within threshold)
    def boom(url, timeout=5.0):
        raise OSError("connection refused")
    obs = EuExitObserver(
        db_path=tmp_path / "s.sqlite", clash_api_url="http://127.0.0.1:9090",
        poll_fn=boom, clock=lambda: "2026-06-06T10:05:00Z",
        unseen_threshold_seconds=900, mode="offline")
    obs.tick()  # must not raise
    c = connect(tmp_path / "s.sqlite")
    assert not _has_unseen(c, "box-1")  # still fresh, not flagged
```

- [ ] **Step 2: Run it; expect failure**

Run: `python -m pytest tests/unit/controller/data_exit/test_exit_observer.py -v`
Expected: FAIL — `ModuleNotFoundError: ...exit_observer`.

- [ ] **Step 3: Implement the wheel**

Create `src/mthydra/controller/data_exit/exit_observer.py`:

```python
"""K3: EuExitObserver — corroborate RU->EU connectivity from the EU exit side.

Runs on the ACTIVE EU node (co-located with the controller and the exit's
sing-box). Per tick: poll the localhost clash_api for live box sessions, record
last-seen per box, and raise/clear the box_eu_tunnel_unseen anti-obligation for
live boxes that have not been seen within the freshness threshold.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from mthydra.controller.data_exit.session_reader import poll_active_sessions
from mthydra.controller.state import eu_exit_observed as _obs
from mthydra.controller.state.audit import log_event
from mthydra.controller.state.db import connect
from mthydra.controller.state.obligations import set_obligation


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _age_seconds(now: str, then: str) -> float:
    a = datetime.fromisoformat(now.replace("Z", "+00:00"))
    b = datetime.fromisoformat(then.replace("Z", "+00:00"))
    return (a - b).total_seconds()


class EuExitObserver:
    POLL_INTERVAL_SECONDS = 5 * 60
    DEFAULT_UNSEEN_THRESHOLD_SECONDS = 45 * 60  # ~3x the box self-check cadence

    def __init__(
        self,
        *,
        db_path: Path | str,
        clash_api_url: str,
        poll_fn: Callable[..., set[str]] | None = None,
        clock: Callable[[], str] | None = None,
        unseen_threshold_seconds: int | None = None,
        mode: str = "online",
    ) -> None:
        self._db_path = Path(db_path)
        self._clash_api_url = clash_api_url
        self._poll_fn = poll_fn or poll_active_sessions
        self._clock = clock or _now_iso
        self._threshold = (
            unseen_threshold_seconds
            if unseen_threshold_seconds is not None
            else self.DEFAULT_UNSEEN_THRESHOLD_SECONDS
        )
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
        # (1) Poll. An unreadable API is 'no observations this tick' — never an
        # excuse to flag every box (K3 §6).
        try:
            seen = self._poll_fn(self._clash_api_url, timeout=5.0)
        except Exception:
            seen = set()
        conn = connect(self._db_path)
        try:
            for box_id in seen:
                _obs.record_seen(conn, box_id, now)
            # (2) Sweep live boxes.
            live = [
                r[0] for r in conn.execute(
                    "SELECT box_id FROM ru_boxes WHERE state='live' "
                    "AND reality_uuid IS NOT NULL ORDER BY box_id"
                ).fetchall()
            ]
            for box_id in live:
                last = _obs.last_seen(conn, box_id)
                stale = last is None or _age_seconds(now, last) > self._threshold
                oid = f"box_eu_tunnel_unseen::{box_id}"
                if stale:
                    set_obligation(
                        conn, obligation_id=oid, last_proven_at=now,
                        proven_by="eu_exit_observer", next_due_at=now,
                        details=json.dumps(
                            {"box_id": box_id, "last_seen_at": last}),
                    )
                    log_event(conn, ts=now, actor="eu_exit_observer",
                              action="box_eu_tunnel_unseen", target=box_id,
                              details_json=None)
                else:
                    conn.execute(
                        "DELETE FROM obligation_clocks WHERE obligation_id=?",
                        (oid,))
            conn.commit()
        finally:
            conn.close()
```

- [ ] **Step 4: Run tests; expect pass**

Run: `python -m pytest tests/unit/controller/data_exit/test_exit_observer.py -v`
Expected: PASS (4 tests). If `set_obligation`'s signature differs, align the
kwargs with the existing call in `distribution/publisher.py:118` (the canonical
example).

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/data_exit/exit_observer.py tests/unit/controller/data_exit/test_exit_observer.py
git commit -m "feat(K3): EuExitObserver wheel — corroborate tunnels, flag unseen boxes

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: Surface the anti-obligation (snapshot + remediation)

**Files:**
- Modify: `src/mthydra/controller/observability/snapshot.py` (`_ANTI_PREFIXES`)
- Modify: `src/mthydra/controller/observability/remediation.py` (`_REMEDIATIONS`)
- Test: `tests/unit/controller/observability/test_snapshot.py` (extend) and
  `tests/unit/controller/observability/test_remediation.py` (extend)

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/controller/observability/test_remediation.py`:

```python
def test_box_eu_tunnel_unseen_has_remediation():
    from mthydra.controller.observability.remediation import remediation_for
    text = remediation_for("box_eu_tunnel_unseen::box-1")
    assert text is not None
    assert "tunnel" in text.lower() or "exit" in text.lower()
```

Add to `tests/unit/controller/observability/test_snapshot.py` (a test that an
anti-obligation row with this prefix is surfaced). Mirror an existing
anti-obligation test in that file; set the obligation then assert it appears in
`snapshot(...).anti_obligations` with `kind == "box_eu_tunnel_unseen"` and
`target == "box-1"`:

```python
def test_box_eu_tunnel_unseen_surfaced(tmp_path):
    from mthydra.controller.observability.snapshot import snapshot
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    from mthydra.controller.state.obligations import set_obligation

    c = connect(tmp_path / "s.sqlite"); apply_schema(c)
    set_obligation(c, obligation_id="box_eu_tunnel_unseen::box-1",
                   last_proven_at="2026-06-06T10:00:00Z",
                   proven_by="eu_exit_observer",
                   next_due_at="2026-06-06T10:00:00Z", details=None)
    c.commit()
    snap = snapshot(c, now="2026-06-06T10:01:00Z")
    rows = [a for a in snap.anti_obligations if a.kind == "box_eu_tunnel_unseen"]
    assert len(rows) == 1 and rows[0].target == "box-1"
```

Adjust the `snapshot(...)` call signature to match the existing tests in that
file (the `now=` kwarg name may differ — copy it from a neighbouring test).

- [ ] **Step 2: Run them; expect failure**

Run: `python -m pytest tests/unit/controller/observability/test_remediation.py -k tunnel tests/unit/controller/observability/test_snapshot.py -k tunnel -v`
Expected: FAIL — remediation returns None; snapshot yields no such row.

- [ ] **Step 3: Add the snapshot prefix**

In `src/mthydra/controller/observability/snapshot.py`, add to `_ANTI_PREFIXES`:

```python
    # Spec K3 — RU->EU tunnel unseen at the EU exit.
    "box_eu_tunnel_unseen",
```

- [ ] **Step 4: Add the remediation line**

In `src/mthydra/controller/observability/remediation.py`, add to `_REMEDIATIONS`:

```python
    "box_eu_tunnel_unseen": (
        "this RU box hasn't established a working tunnel to the EU exit "
        "recently, so Telegram traffic can't flow through it (TCP :443 may "
        "still answer — that's not enough). SSH in and check "
        "'cat /run/mthydra/health.json' + 'journalctl -u mthydra-agent' for "
        "the EU tunnel check verdict; confirm sing-box is up on the box and "
        "the EU exit IP:port is reachable from it. If the box is genuinely "
        "dead, replace it ('mthydra-ops ru-bringup ...')."
    ),
```

- [ ] **Step 5: Run tests; expect pass**

Run: `python -m pytest tests/unit/controller/observability/test_remediation.py tests/unit/controller/observability/test_snapshot.py -v`
Expected: PASS — new tests pass, existing observability tests unaffected.

- [ ] **Step 6: Commit**

```bash
git add src/mthydra/controller/observability/snapshot.py src/mthydra/controller/observability/remediation.py tests/unit/controller/observability/test_snapshot.py tests/unit/controller/observability/test_remediation.py
git commit -m "feat(K3): surface box_eu_tunnel_unseen via snapshot + remediation

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 8: Arm the observer in serve (active role only)

**Files:**
- Modify: `src/mthydra/controller/cli.py` (`_cmd_serve`)
- Test: manual smoke (the wheel is offline-mode in tests; arming is config wiring)

The clash_api exists only on the **active** EU node. On standby the poll would
fail every tick (handled as no-observations) but the sweep would then flag every
live box — so the observer must only run on the active role.

- [ ] **Step 1: Read how serve determines role + where wheels are armed**

Run: `grep -n "role\|\.arm()\|node_state\|data_exit\|cfg.data_exit" src/mthydra/controller/cli.py | sed -n '1,40p'`
Note: serve already branches on role (line ~1719) and arms a block of wheels
(~1987–2002). Find how the active role is read at serve time (node_state / args).

- [ ] **Step 2: Construct + arm the observer (active only)**

In `_cmd_serve`, where the other wheels are instantiated (near `shard_wheel = ...`),
add — guarded so it only arms when this node is active AND `cfg.data_exit` is set:

```python
    exit_observer = None
    if cfg.data_exit is not None and _node_is_active(conn):
        from mthydra.controller.data_exit.exit_observer import EuExitObserver
        exit_observer = EuExitObserver(
            db_path=args.db_path,
            clash_api_url="http://" + cfg.data_exit.clash_api_listen,
        )
```

Use the existing active-role predicate. There is already
`_require_active_role(conn, ...)` in this file (line ~2579) and a standby check at
~1719; reuse whichever reads `node_state.role == 'active'`. If only
`_require_active_role` exists (which returns an int/None for CLI gating), add a
tiny boolean helper beside it:

```python
def _node_is_active(conn) -> bool:
    row = conn.execute("SELECT role FROM node_state WHERE rowid=1").fetchone()
    return bool(row) and row[0] == "active"
```

In the `.arm()` block (~1987), add:

```python
        if exit_observer is not None:
            exit_observer.arm()
```

In the `.disarm()`/shutdown block (~2022), add:

```python
        if exit_observer is not None:
            exit_observer.disarm()
```

Update the serve startup `print(...)` summary string to mention "+ eu exit
observer" when armed.

- [ ] **Step 3: Smoke-check serve imports + wiring**

Run: `python -c "import mthydra.controller.cli"`
Expected: no ImportError.

Run: `python -m pytest tests/unit/controller -k "serve or cli" -v`
Expected: PASS (or no such tests — then rely on the import smoke).

- [ ] **Step 4: Lint changed files**

Run: `ruff check --select I,F src/mthydra/controller/cli.py src/mthydra/controller/data_exit/exit_observer.py`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/cli.py
git commit -m "feat(K3): arm EuExitObserver in serve (active EU node only)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 9: Integration test + CHANGELOG

**Files:**
- Create: `tests/integration/test_k3_connectivity.py`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Write the end-to-end integration test**

Create `tests/integration/test_k3_connectivity.py`:

```python
from __future__ import annotations

from mthydra.controller.data_exit.exit_observer import EuExitObserver
from mthydra.controller.observability.snapshot import snapshot
from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema
from mthydra.ru_agent import tunnel_check as tc


def test_broken_tunnel_box_verdict_is_fail():
    """Box-side: a broken upstream (sing-box closes -> EOF) yields FAIL."""
    class Dead:
        def sendall(self, d): pass
        def recv(self, n): return b""      # EOF
        def close(self): pass
    v = tc.check_eu_tunnel(
        dc_ips=["149.154.167.51"],
        connect_fn=lambda *a, **k: Dead(),
        clock=lambda: "2026-06-06T10:00:00Z")
    assert v.verdict == "fail"


def test_unseen_live_box_surfaces_alert_end_to_end(tmp_path):
    """Controller-side: a live box never seen at the exit surfaces the
    box_eu_tunnel_unseen anti-obligation through the snapshot."""
    db = tmp_path / "s.sqlite"
    c = connect(db); apply_schema(c)
    c.execute("INSERT INTO ru_boxes (box_id, state, reality_uuid) "
              "VALUES ('box-1', 'live', 'uuid-1')")
    c.commit(); c.close()

    EuExitObserver(
        db_path=db, clash_api_url="http://127.0.0.1:9090",
        poll_fn=lambda url, timeout=5.0: set(),   # exit sees nobody
        clock=lambda: "2026-06-06T10:00:00Z",
        unseen_threshold_seconds=900, mode="offline").tick()

    c = connect(db)
    snap = snapshot(c, now="2026-06-06T10:01:00Z")
    kinds = {a.kind for a in snap.anti_obligations}
    assert "box_eu_tunnel_unseen" in kinds
```

Match the `snapshot(...)` call signature to the unit tests (the `now=` kwarg).

- [ ] **Step 2: Run it; expect pass**

Run: `python -m pytest tests/integration/test_k3_connectivity.py -v`
Expected: PASS (2 tests).

- [ ] **Step 3: Run the affected suites together (no regressions)**

Run: `python -m pytest tests/unit/ru_agent tests/unit/controller/data_exit tests/unit/controller/state tests/unit/controller/observability tests/integration/test_k3_connectivity.py -q`
Expected: PASS. Pre-existing unrelated failures (3 "box has no shard" tests +
gap_monitor collection error) are known and independent of this work — do not
attempt to fix them here; note them if they appear.

- [ ] **Step 4: Update CHANGELOG**

Add an entry under the current unreleased section of `CHANGELOG.md`:

```markdown
### Added
- **K3 RU→EU connectivity check.** RU boxes now run an end-to-end tunnel
  self-check (TCP to a Telegram DC forced through the box's own
  iptables→sing-box→EU-exit path), writing the verdict to
  `/run/mthydra/health.json` + the journal. The active EU node polls its local
  sing-box `clash_api` and raises a plain-language `box_eu_tunnel_unseen` alert
  for any live box not seen tunnelling — closing the blind spot where a box
  passed `telnet :443` while Telegram could not connect through it. No new RU
  credential or outbound. Schema → v18.
```

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_k3_connectivity.py CHANGELOG.md
git commit -m "test(K3): integration coverage + CHANGELOG

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

- [ ] **Step 6: Push**

```bash
git push origin main
```

---

## Self-review notes (against the spec)

- **K3-D1** (check on box, targets Telegram DC) → Task 4 + Task 5.
- **K3-D2** (upstream-established predicate, not bare connect) → Task 4 Step 3 (recv/EOF logic) + Task 9 broken-tunnel test.
- **K3-D3** (existing 15-min loop, health.json + loud journal) → Task 5.
- **K3-D4** (localhost clash_api + poll → eu_exit_observed) → Task 3 + Task 2 + Task 6.
- **K3-D5** (alerter flags box_eu_tunnel_unseen, plain remediation) → Task 6 + Task 7.
- **K3-D6** (no new RU cred/outbound/fingerprint) → satisfied by construction; box only reads, all new I/O is controller/exit-side. Standby-role guard in Task 8 prevents false flags where clash_api is absent.
- **K3-D7** (out of scope items) → not implemented, by design.
- **§6 error handling** (probe never crashes loop; unreadable API ≠ flag-all) → Task 4 (`OSError`/broad catch), Task 5 (`_run_tunnel_check` never raises), Task 6 (`test_poll_error_does_not_flag_everything`).
- **§7 testing** → every task is TDD; integration in Task 9.
- **§8 upgrade path** (forward-only migration, exit picks up via wheel) → Task 1 (v18) + Task 3.
