# T2 Vantage SSH Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `mthydra-ops vantage-setup` open a vantage by any of three methods (root key, interactive password, operator-pre-populated pubkey), lock the vantage down to `probe`-key-only after verifying access, and back the probe keypair with `state.sqlite` so it survives standby promotion unattended.

**Architecture:** A single shared probe keypair becomes a one-row table in the controller DB (`controller_probe_key`); the file at `/var/lib/mthydra/ssh/probe.key` is a regenerable 0600 cache materialized from that row. `vantage-setup` is refactored to resolve the key from the DB, choose one of three entry methods to open a privileged session, provision the `probe` user, verify probe-key login on a fresh connection, then harden sshd (`AllowUsers probe` + no-password + no-root). The probe-runner wheel re-materializes the key file at startup, so a promoted standby restores the DB and resumes probing with no manual step.

**Tech Stack:** Python 3 stdlib (`subprocess`, `sqlite3`, `pathlib`), OpenSSH CLI (`ssh`, `ssh-keygen`, `ssh-keyscan`, remote `sshd`/`systemctl`), pytest. No new third-party deps.

**Spec:** `doc/specs/2026-06-05-T2-vantage-ssh-hardening.md`

**Resolved ambiguity (spec §6.1):** the `--print-pubkey` re-run always connects as a **root-capable** `--bootstrap-user` (default `root`), because hardening requires root. There is no "connect as probe if probe exists" shortcut — that path can't harden (probe has no sudo, T2-D8).

---

## File Structure

**Create:**
- `src/mthydra/controller/state/probe_key.py` — row accessors for `controller_probe_key` (`get`, `put`). One responsibility: persistence.
- `src/mthydra/controller/probe_runner/key.py` — `ensure_probe_key(conn, ssh_dir) -> (Path, str)`: keygen-on-empty, persist to DB, materialize the 0600 file cache. One responsibility: turning the DB row into a usable key file.
- `tests/unit/controller/state/test_schema_migration_v17.py`
- `tests/unit/controller/state/test_probe_key.py`
- `tests/unit/controller/probe_runner/test_key.py`

**Modify:**
- `src/mthydra/controller/state/schema.py` — bump `SCHEMA_VERSION` to 17, add CREATE TABLE to `_STATEMENTS`, add `migrate_v16_to_v17`, wire into `apply_schema`.
- `src/mthydra/ops/vantage_setup.py` — use the shared DB key; add entry methods; verify; harden.
- `src/mthydra/ops/main.py` — new args (`--password`, `--print-pubkey`, `--bootstrap-user`); `--root-key` no longer required.
- `src/mthydra/controller/probe_runner/wheel.py` — `ssh_dir` param + `ensure_probe_key` call in `start()`.
- `tests/unit/ops/test_vantage_setup.py` — update for the shared-key/entry-method flow.
- `doc/quickstart-mvp.md` §7.4 and `CHANGELOG.md`.

---

## Task 1: Schema — `controller_probe_key` table + v16→v17 migration

**Files:**
- Modify: `src/mthydra/controller/state/schema.py`
- Test: `tests/unit/controller/state/test_schema_migration_v17.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/controller/state/test_schema_migration_v17.py`:

```python
"""Tests for v16 → v17 schema migration — controller_probe_key table."""
from __future__ import annotations

import sqlite3

from mthydra.controller.state import schema


def test_fresh_schema_has_controller_probe_key():
    conn = sqlite3.connect(":memory:")
    schema.apply_schema(conn)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "controller_probe_key" in tables
    assert schema.SCHEMA_VERSION == 17


def test_single_row_check_rejects_second_row():
    conn = sqlite3.connect(":memory:")
    schema.apply_schema(conn)
    conn.execute(
        "INSERT INTO controller_probe_key (id, private_key, public_key, created_at)"
        " VALUES (1, 'priv', 'pub', '2026-06-05T00:00:00Z')")
    try:
        conn.execute(
            "INSERT INTO controller_probe_key (id, private_key, public_key, created_at)"
            " VALUES (2, 'priv2', 'pub2', '2026-06-05T00:00:00Z')")
        raised = False
    except sqlite3.IntegrityError:
        raised = True
    assert raised


def test_v16_to_v17_migrates_existing_db():
    conn = sqlite3.connect(":memory:")
    schema.apply_schema(conn)
    # Simulate a v16 DB: drop the table and rewind the version.
    conn.execute("DROP TABLE controller_probe_key")
    conn.execute("UPDATE schema_version SET version=16 WHERE rowid=1")
    schema.migrate_v16_to_v17(conn)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "controller_probe_key" in tables
    assert conn.execute(
        "SELECT version FROM schema_version WHERE rowid=1").fetchone()[0] == 17


def test_v17_migration_is_idempotent():
    conn = sqlite3.connect(":memory:")
    schema.apply_schema(conn)
    schema.migrate_v16_to_v17(conn)
    schema.migrate_v16_to_v17(conn)
    assert conn.execute(
        "SELECT version FROM schema_version WHERE rowid=1").fetchone()[0] >= 17
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/controller/state/test_schema_migration_v17.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'migrate_v16_to_v17'` / `SCHEMA_VERSION == 16`.

- [ ] **Step 3: Bump version + add the CREATE TABLE statement**

In `src/mthydra/controller/state/schema.py`, change line 7:

```python
SCHEMA_VERSION = 17
```

Add this DDL string to the `_STATEMENTS` list (insert it before the closing `]` at line ~582, alongside the other `CREATE TABLE` entries):

```python
    """
    CREATE TABLE IF NOT EXISTS controller_probe_key (
      id           INTEGER PRIMARY KEY CHECK (id = 1),
      private_key  TEXT NOT NULL,
      public_key   TEXT NOT NULL,
      created_at   TEXT NOT NULL,
      comment      TEXT
    );
    """,
```

- [ ] **Step 4: Add the migration function**

In `src/mthydra/controller/state/schema.py`, after `migrate_v15_to_v16` (ends ~line 822):

```python
def migrate_v16_to_v17(conn: sqlite3.Connection) -> None:
    """Idempotent v16 → v17: add controller_probe_key (single-row table).

    Holds the one shared probe-runner SSH keypair so it rides the encrypted
    DB backup and survives standby promotion (spec T2-D2)."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS controller_probe_key ("
        "id INTEGER PRIMARY KEY CHECK (id = 1), "
        "private_key TEXT NOT NULL, public_key TEXT NOT NULL, "
        "created_at TEXT NOT NULL, comment TEXT)"
    )
    conn.execute(
        "UPDATE schema_version SET version=?, applied_at=? WHERE rowid=1",
        (17, _now()),
    )
    conn.commit()
```

- [ ] **Step 5: Wire the migration into `apply_schema`**

In `apply_schema` (the migration ladder, after the `if current < 16:` block at line ~1093):

```python
        if current < 17:
            migrate_v16_to_v17(conn)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/unit/controller/state/test_schema_migration_v17.py tests/unit/controller/state/test_schema.py tests/unit/controller/state/test_schema_migration_v15.py -v`
Expected: PASS (all). The pre-existing schema tests confirm no regression.

- [ ] **Step 7: Commit**

```bash
git add src/mthydra/controller/state/schema.py tests/unit/controller/state/test_schema_migration_v17.py
git commit -m "feat(schema): v17 controller_probe_key single-row table

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: State accessors — `state/probe_key.py`

**Files:**
- Create: `src/mthydra/controller/state/probe_key.py`
- Test: `tests/unit/controller/state/test_probe_key.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/controller/state/test_probe_key.py`:

```python
"""Tests for state.probe_key — controller_probe_key row accessors."""
from __future__ import annotations

import pytest

from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema
from mthydra.controller.state import probe_key


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "state.sqlite")
    apply_schema(c)
    yield c
    c.close()


def test_get_returns_none_when_empty(conn):
    assert probe_key.get(conn) is None


def test_put_then_get_round_trips(conn):
    probe_key.put(conn, private_key="PRIV", public_key="ssh-ed25519 PUB x",
                  comment="mthydra-probe-runner", at="2026-06-05T00:00:00Z")
    row = probe_key.get(conn)
    assert row.private_key == "PRIV"
    assert row.public_key == "ssh-ed25519 PUB x"
    assert row.comment == "mthydra-probe-runner"


def test_put_is_single_row_upsert(conn):
    probe_key.put(conn, private_key="A", public_key="pa",
                  comment=None, at="2026-06-05T00:00:00Z")
    probe_key.put(conn, private_key="B", public_key="pb",
                  comment=None, at="2026-06-05T00:00:01Z")
    row = probe_key.get(conn)
    assert row.private_key == "B"
    n = conn.execute("SELECT COUNT(*) FROM controller_probe_key").fetchone()[0]
    assert n == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/controller/state/test_probe_key.py -v`
Expected: FAIL — `ModuleNotFoundError: ... state.probe_key`.

- [ ] **Step 3: Write the module**

Create `src/mthydra/controller/state/probe_key.py`:

```python
"""controller_probe_key accessors — the one shared probe SSH keypair (spec T2).

Single-row table (CHECK id=1). The DB is the source of truth; the file at
/var/lib/mthydra/ssh/probe.key is a regenerable cache (see probe_runner.key).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class ProbeKey:
    private_key: str
    public_key: str
    created_at: str
    comment: str | None


def get(conn: sqlite3.Connection) -> ProbeKey | None:
    r = conn.execute(
        "SELECT private_key, public_key, created_at, comment "
        "FROM controller_probe_key WHERE id=1"
    ).fetchone()
    return ProbeKey(*r) if r else None


def put(conn: sqlite3.Connection, *, private_key: str, public_key: str,
        comment: str | None, at: str) -> None:
    conn.execute(
        "INSERT INTO controller_probe_key (id, private_key, public_key, created_at, comment) "
        "VALUES (1, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET private_key=excluded.private_key, "
        "public_key=excluded.public_key, created_at=excluded.created_at, "
        "comment=excluded.comment",
        (private_key, public_key, at, comment),
    )
    conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/controller/state/test_probe_key.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/state/probe_key.py tests/unit/controller/state/test_probe_key.py
git commit -m "feat(state): controller_probe_key accessors (get/put upsert)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Materialization — `probe_runner/key.py`

**Files:**
- Create: `src/mthydra/controller/probe_runner/key.py`
- Test: `tests/unit/controller/probe_runner/test_key.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/controller/probe_runner/test_key.py`:

```python
"""Tests for probe_runner.key.ensure_probe_key — keygen + DB persist + file cache."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema
from mthydra.controller.state import probe_key as pk
from mthydra.controller.probe_runner import key as keymod


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "state.sqlite")
    apply_schema(c)
    yield c
    c.close()


def _fake_keygen(monkeypatch):
    """ssh-keygen shim: writes a private file + .pub at the -f path."""
    def _run(argv, capture_output=True, text=True, timeout=None, input=None):
        if argv[0] == "ssh-keygen":
            for i, tok in enumerate(argv):
                if tok == "-f":
                    Path(argv[i + 1]).write_text("PRIVATE-KEY-BODY\n")
                    Path(argv[i + 1] + ".pub").write_text("ssh-ed25519 PUBKEY mthydra\n")
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(keymod.subprocess, "run", _run)


def test_generates_and_persists_on_empty_db(tmp_path, conn, monkeypatch):
    _fake_keygen(monkeypatch)
    ssh_dir = tmp_path / "ssh"
    key_path, pubkey = keymod.ensure_probe_key(conn, ssh_dir)
    assert key_path == ssh_dir / "probe.key"
    assert key_path.read_text() == "PRIVATE-KEY-BODY\n"
    assert (ssh_dir / "probe.key.pub").read_text().strip() == pubkey
    row = pk.get(conn)
    assert row.private_key == "PRIVATE-KEY-BODY\n"
    assert row.public_key == "ssh-ed25519 PUBKEY mthydra"
    assert oct(key_path.stat().st_mode)[-3:] == "600"


def test_materializes_file_from_existing_db_row_without_keygen(tmp_path, conn, monkeypatch):
    pk.put(conn, private_key="DB-PRIV\n", public_key="ssh-ed25519 DBPUB x",
           comment=None, at="2026-06-05T00:00:00Z")
    calls = []
    def _run(argv, **kw):
        calls.append(argv[0])
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(keymod.subprocess, "run", _run)
    ssh_dir = tmp_path / "ssh"
    key_path, pubkey = keymod.ensure_probe_key(conn, ssh_dir)
    assert "ssh-keygen" not in calls          # no regeneration
    assert key_path.read_text() == "DB-PRIV\n"
    assert pubkey == "ssh-ed25519 DBPUB x"


def test_rewrites_file_when_cache_is_stale(tmp_path, conn, monkeypatch):
    pk.put(conn, private_key="CORRECT\n", public_key="ssh-ed25519 P x",
           comment=None, at="2026-06-05T00:00:00Z")
    monkeypatch.setattr(keymod.subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""))
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir()
    (ssh_dir / "probe.key").write_text("STALE\n")     # wrong contents
    key_path, _ = keymod.ensure_probe_key(conn, ssh_dir)
    assert key_path.read_text() == "CORRECT\n"        # rewritten from DB
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/controller/probe_runner/test_key.py -v`
Expected: FAIL — `ModuleNotFoundError: ... probe_runner.key`.

- [ ] **Step 3: Write the module**

Create `src/mthydra/controller/probe_runner/key.py`:

```python
"""Probe key materialization (spec T2-D2 / §5).

DB row (controller_probe_key) is the source of truth; the file at
<ssh_dir>/probe.key is a 0600 regenerable cache. ensure_probe_key is called by
vantage-setup and at probe-wheel startup, so a promoted standby that restores
the DB rematerializes the identical key file with no manual step.
"""
from __future__ import annotations

import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from mthydra.controller.state import probe_key as pk

_COMMENT = "mthydra-probe-runner"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _generate_keypair() -> tuple[str, str]:
    """ssh-keygen an ed25519 keypair in a temp dir; return (private, public)."""
    with tempfile.TemporaryDirectory() as td:
        kp = Path(td) / "k"
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(kp), "-C", _COMMENT],
            capture_output=True, text=True, timeout=30, check=True,
        )
        return kp.read_text(), (Path(str(kp) + ".pub")).read_text().strip()


def ensure_probe_key(conn: sqlite3.Connection, ssh_dir: Path | str) -> tuple[Path, str]:
    """Resolve the shared probe key. Generate+persist on first call; always
    materialize the 0600 file cache from the DB row. Returns (key_path, pubkey)."""
    ssh_dir = Path(ssh_dir)
    ssh_dir.mkdir(parents=True, exist_ok=True)
    try:
        ssh_dir.chmod(0o700)
    except PermissionError:
        pass

    row = pk.get(conn)
    if row is None:
        priv, pub = _generate_keypair()
        pk.put(conn, private_key=priv, public_key=pub, comment=_COMMENT, at=_now())
        row = pk.get(conn)

    key_path = ssh_dir / "probe.key"
    pub_path = ssh_dir / "probe.key.pub"
    if not key_path.exists() or key_path.read_text() != row.private_key:
        key_path.write_text(row.private_key)
        key_path.chmod(0o600)
    if not pub_path.exists() or pub_path.read_text().strip() != row.public_key:
        pub_path.write_text(row.public_key + "\n")
        pub_path.chmod(0o644)
    return key_path, row.public_key
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/controller/probe_runner/test_key.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/probe_runner/key.py tests/unit/controller/probe_runner/test_key.py
git commit -m "feat(probe-runner): ensure_probe_key materializes shared key from DB

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Wheel re-materializes the key at startup (failover path)

**Files:**
- Modify: `src/mthydra/controller/probe_runner/wheel.py:162-217`
- Test: `tests/unit/controller/probe_runner/test_key.py` (add a wheel-start test)

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/controller/probe_runner/test_key.py`:

```python
def test_wheel_start_materializes_key_from_db(tmp_path, monkeypatch):
    """A promoted standby restores the DB; wheel.start() must rematerialize
    the probe.key file before scheduling ticks."""
    from mthydra.controller.probe_runner.wheel import ProbeRunnerWheel
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    from mthydra.controller.state import probe_key as pk

    db = tmp_path / "state.sqlite"
    c = connect(db)
    apply_schema(c)
    pk.put(c, private_key="RESTORED\n", public_key="ssh-ed25519 R x",
           comment=None, at="2026-06-05T00:00:00Z")
    c.close()

    ssh_dir = tmp_path / "ssh"
    # mode='offline' so start() does materialization but schedules nothing.
    wheel = ProbeRunnerWheel(db_path=str(db), interval_seconds=1800,
                             max_concurrent=2, mode="offline",
                             ssh_dir=str(ssh_dir))
    wheel.start()
    assert (ssh_dir / "probe.key").read_text() == "RESTORED\n"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/controller/probe_runner/test_key.py::test_wheel_start_materializes_key_from_db -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'ssh_dir'`.

- [ ] **Step 3: Add `ssh_dir` to the wheel and materialize in `start()`**

In `src/mthydra/controller/probe_runner/wheel.py`, update `__init__` (line 162):

```python
    def __init__(self, db_path: str, interval_seconds: int,
                 max_concurrent: int, mode: str = "active",
                 reach_check: bool = True,
                 ssh_dir: str = "/var/lib/mthydra/ssh") -> None:
        self.db_path = db_path
        self.interval_seconds = interval_seconds
        self.max_concurrent = max_concurrent
        self.mode = mode
        # reach_check=False is for tests that monkeypatch _probe_one only.
        self.reach_check = reach_check
        self.ssh_dir = ssh_dir
        self._scheduler: BackgroundScheduler | None = None
```

Update `start()` (line 209) to materialize the key first — note this runs even in
`offline` mode so a standby's file is ready before it is promoted to active:

```python
    def start(self) -> None:
        from mthydra.controller.probe_runner.key import ensure_probe_key
        with connect(self.db_path) as conn:
            ensure_probe_key(conn, self.ssh_dir)
        if self.mode == "offline":
            return
        self._scheduler = BackgroundScheduler(
            executors={"default": APSPoolExec(max_workers=1)})
        self._scheduler.add_job(
            self.tick, IntervalTrigger(seconds=self.interval_seconds),
            id="probe-runner", coalesce=True, max_instances=1)
        self._scheduler.start()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/controller/probe_runner/ -v`
Expected: PASS. (Existing wheel tests still pass — `ssh_dir` has a default; `start()` only adds a key-materialization step.)

- [ ] **Step 5: Check the existing serve wiring still constructs the wheel correctly**

Run: `grep -rn "ProbeRunnerWheel(" src/`
If `_cmd_serve` passes positional/keyword args, the new defaulted `ssh_dir` keeps it compatible. If a config value for the ssh dir exists (`grep -rn "ssh_dir\|probe.*ssh" src/mthydra/controller/config*`), pass it through; otherwise the default is correct. No code change required unless serve hard-codes a different dir.

- [ ] **Step 6: Commit**

```bash
git add src/mthydra/controller/probe_runner/wheel.py tests/unit/controller/probe_runner/test_key.py
git commit -m "feat(probe-runner): wheel.start materializes shared probe key (failover)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: `vantage-setup` uses the shared DB key

**Files:**
- Modify: `src/mthydra/ops/vantage_setup.py`
- Test: `tests/unit/ops/test_vantage_setup.py`

This task swaps the per-vantage `_ensure_probe_key(ssh_dir, vantage_id)` for the
shared `ensure_probe_key(conn, ssh_dir)` and registers the shared key path. Entry
methods and hardening come in Tasks 6–7.

- [ ] **Step 1: Update the happy-path test to expect the shared key**

In `tests/unit/ops/test_vantage_setup.py`, replace `test_ensure_probe_key_is_idempotent`
(lines 78-87) with a shared-key test, and update `test_cmd_vantage_setup_happy_path`
to assert the registered key path is `probe.key`:

```python
def test_cmd_vantage_setup_registers_shared_key(tmp_path, monkeypatch):
    """vantage-setup resolves the shared probe key from the DB and registers
    that path (not a per-vantage <id>.key)."""
    (tmp_path / "root.pem").write_text("-----BEGIN PRIVATE KEY-----\nfake\n")
    history: list[dict] = []
    monkeypatch.setattr(vantage_setup.subprocess, "run",
                        _fake_run_factory(history))

    rc = vantage_setup.cmd_vantage_setup(_args(tmp_path))
    assert rc == 0
    controller_calls = [h for h in history if "mthydra-controller" in h["argv"][0]]
    assert len(controller_calls) == 1
    argv = controller_calls[0]["argv"]
    assert "vantage-set-ssh" in argv
    kp = argv[argv.index("--key-path") + 1]
    assert kp.endswith("probe.key")
```

Update the `_fake_run_factory` `ssh-keygen` branch so it writes BOTH the private
file and the `.pub` (the real `ensure_probe_key` reads the private back):

```python
        if argv[0] == "ssh-keygen":
            for i, tok in enumerate(argv):
                if tok == "-f" and i + 1 < len(argv):
                    Path(argv[i + 1]).write_text("PRIV\n")
                    Path(argv[i + 1] + ".pub").write_text(
                        "ssh-ed25519 AAAAFAKEKEY mthydra-probe-runner\n")
                    break
            return subprocess.CompletedProcess(argv, 0, "", "")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/ops/test_vantage_setup.py::test_cmd_vantage_setup_registers_shared_key -v`
Expected: FAIL — current code registers `<ssh_dir>/ru-msk-1.key`, not `probe.key`.

- [ ] **Step 3: Refactor `cmd_vantage_setup` to use the shared key**

In `src/mthydra/ops/vantage_setup.py`, replace the `_ensure_probe_key` usage. Add
imports at the top:

```python
from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema
from mthydra.controller.probe_runner.key import ensure_probe_key
```

Delete the local `_ensure_probe_key` function (lines 56-69). In `cmd_vantage_setup`,
replace the key-resolution block:

```python
    ssh_dir = Path(args.ssh_dir)
    root_key = Path(args.root_key).expanduser()
    if not root_key.exists():
        _err(f"--root-key not found: {root_key}")
        return 2
    try:
        _ensure_ssh_dir(ssh_dir)
        key_path = _ensure_probe_key(ssh_dir, args.vantage_id)
        pubkey_path = Path(str(key_path) + ".pub")
        probe_pubkey = pubkey_path.read_text().strip()
```

with:

```python
    ssh_dir = Path(args.ssh_dir)
    root_key = Path(args.root_key).expanduser()
    if not root_key.exists():
        _err(f"--root-key not found: {root_key}")
        return 2
    try:
        _ensure_ssh_dir(ssh_dir)
        with connect(args.db_path) as conn:
            apply_schema(conn)
            key_path, probe_pubkey = ensure_probe_key(conn, ssh_dir)
```

(Entry-method branching replaces the `--root-key` hard requirement in Task 6;
for this task `--root-key` stays required.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/ops/test_vantage_setup.py -v`
Expected: PASS. Remove the now-obsolete `test_ensure_probe_key_is_idempotent`
if it still references the deleted function.

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/ops/vantage_setup.py tests/unit/ops/test_vantage_setup.py
git commit -m "refactor(vantage-setup): use shared DB-backed probe key

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Three entry methods (root-key / password / print-pubkey)

**Files:**
- Modify: `src/mthydra/ops/vantage_setup.py`, `src/mthydra/ops/main.py`
- Test: `tests/unit/ops/test_vantage_setup.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/ops/test_vantage_setup.py`:

```python
def test_print_pubkey_emits_key_and_exits_without_ssh(tmp_path, monkeypatch, capsys):
    history: list[dict] = []
    monkeypatch.setattr(vantage_setup.subprocess, "run",
                        _fake_run_factory(history))
    args = _args(tmp_path, root_key=None, password=False, print_pubkey=True,
                 bootstrap_user="root")
    rc = vantage_setup.cmd_vantage_setup(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "ssh-ed25519" in out                       # pubkey printed
    assert not any(h["argv"][0] == "ssh" for h in history)  # no connection made


def test_password_method_omits_batchmode(tmp_path, monkeypatch):
    """Password entry must NOT set BatchMode=yes (that disables password auth)
    and must NOT use sshpass."""
    captured = {}
    def _fake(argv, capture_output=True, text=True, timeout=None, input=None):
        if argv[0] == "ssh":
            captured.setdefault("ssh_argvs", []).append(argv)
            return subprocess.CompletedProcess(argv, 0, "OK\n", "")
        if argv[0] == "ssh-keyscan":
            return subprocess.CompletedProcess(argv, 0, "h x\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(vantage_setup.subprocess, "run", _fake)
    # Skip verify+harden indirection for this unit test:
    monkeypatch.setattr(vantage_setup, "_verify_probe_login", lambda **kw: None)
    monkeypatch.setattr(vantage_setup, "_harden_sshd", lambda **kw: None)

    args = _args(tmp_path, root_key=None, password=True, print_pubkey=False,
                 bootstrap_user="root")
    rc = vantage_setup.cmd_vantage_setup(args)
    assert rc == 0
    prov = captured["ssh_argvs"][0]
    assert "sshpass" not in " ".join(prov)
    assert "BatchMode=yes" not in prov
    assert prov[prov.index("-o") if "-o" in prov else 0] != "BatchMode=yes"


def test_root_key_method_uses_batchmode(tmp_path, monkeypatch):
    (tmp_path / "root.pem").write_text("k")
    captured = {}
    def _fake(argv, capture_output=True, text=True, timeout=None, input=None):
        if argv[0] == "ssh":
            captured.setdefault("ssh_argvs", []).append(argv)
            return subprocess.CompletedProcess(argv, 0, "OK\n", "")
        return subprocess.CompletedProcess(argv, 0, "h x\n", "")
    monkeypatch.setattr(vantage_setup.subprocess, "run", _fake)
    monkeypatch.setattr(vantage_setup, "_verify_probe_login", lambda **kw: None)
    monkeypatch.setattr(vantage_setup, "_harden_sshd", lambda **kw: None)
    args = _args(tmp_path, password=False, print_pubkey=False, bootstrap_user="root")
    rc = vantage_setup.cmd_vantage_setup(args)
    assert rc == 0
    prov = captured["ssh_argvs"][0]
    assert "BatchMode=yes" in prov
    assert f"root@{args.vantage_host}" in prov
```

Update `_args` to carry the new fields:

```python
def _args(tmp_path, **over):
    base = dict(
        vantage_id="ru-msk-1",
        vantage_host="203.0.113.5",
        vantage_port=22,
        root_key=str(tmp_path / "root.pem"),
        password=False,
        print_pubkey=False,
        bootstrap_user="root",
        ssh_dir=str(tmp_path / "ssh"),
        db_path=str(tmp_path / "db.sqlite"),
    )
    base.update(over)
    return argparse.Namespace(**base)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/ops/test_vantage_setup.py -k "print_pubkey or password_method or root_key_method" -v`
Expected: FAIL — these args/branches don't exist yet.

- [ ] **Step 3: Add the entry-method resolver + branching**

In `src/mthydra/ops/vantage_setup.py`, add a resolver and generalize
`_ssh_provision_vantage` to take a connection spec. Replace the
`_ssh_provision_vantage` signature and add `_entry_ssh_opts`:

```python
def _entry_ssh_opts(args) -> tuple[str, str, list[str]]:
    """Return (ssh_user, identity_opt_or_empty, extra_opts) for the privileged
    bootstrap session, per the chosen entry method.

    - --root-key  : user 'root', -i <key>, BatchMode=yes
    - --password  : user 'root', no identity, NO BatchMode (ssh prompts on the
                    controlling TTY; the password is never captured — spec T2-D4)
    - re-run after --print-pubkey (no flag): user <bootstrap_user> (root-capable),
                    -i <shared key>, BatchMode=yes
    """
    if args.password:
        return "root", "", ["-o", "StrictHostKeyChecking=accept-new",
                            "-o", "ConnectTimeout=15"]
    if args.root_key:
        return "root", str(Path(args.root_key).expanduser()), [
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
    # print-pubkey re-run: connect as the root-capable bootstrap user with the
    # shared key the operator installed out of band.
    return args.bootstrap_user, "SHARED", [
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
```

Replace `_ssh_provision_vantage` to accept the resolved connection:

```python
def _ssh_provision_vantage(
    *,
    vantage_host: str,
    vantage_port: int,
    ssh_user: str,
    identity: str,        # path, "SHARED" (substituted by caller), or "" (password)
    extra_opts: list[str],
    probe_pubkey: str,
    timeout: int = 120,
) -> None:
    """SSH to the vantage with a root-capable session and provision the probe
    user + deps in one round trip. Idempotent."""
    remote_script = f"""
set -u
id probe >/dev/null 2>&1 || adduser --disabled-password --gecos '' probe
mkdir -p /home/probe/.ssh
chmod 700 /home/probe/.ssh
grep -qxF {probe_pubkey!r} /home/probe/.ssh/authorized_keys 2>/dev/null \\
    || echo {probe_pubkey!r} >> /home/probe/.ssh/authorized_keys
chown -R probe:probe /home/probe/.ssh
chmod 600 /home/probe/.ssh/authorized_keys
DEBIAN_FRONTEND=noninteractive apt-get update -y >/dev/null
DEBIAN_FRONTEND=noninteractive apt-get install -y openssl ncat >/dev/null
echo OK
"""
    argv = ["ssh"]
    if identity:
        argv += ["-i", identity]
    argv += ["-p", str(vantage_port), *extra_opts, f"{ssh_user}@{vantage_host}",
             "bash", "-s"]
    _say(f"provisioning vantage {vantage_host}:{vantage_port} as {ssh_user}")
    res = subprocess.run(
        argv, input=remote_script, capture_output=True, text=True, timeout=timeout,
    )
    if res.returncode != 0 or "OK" not in res.stdout:
        raise VantageSetupError(
            f"remote provisioning failed (rc={res.returncode}): "
            f"{res.stderr.strip() or res.stdout.strip()}"
        )
```

Rewrite `cmd_vantage_setup` body (entry validation + dispatch):

```python
def cmd_vantage_setup(args) -> int:
    ssh_dir = Path(args.ssh_dir)
    methods = [bool(args.root_key), bool(args.password), bool(args.print_pubkey)]
    if sum(methods) > 1:
        _err("choose at most one of --root-key / --password / --print-pubkey")
        return 2
    if args.root_key and not Path(args.root_key).expanduser().exists():
        _err(f"--root-key not found: {args.root_key}")
        return 2
    try:
        _ensure_ssh_dir(ssh_dir)
        with connect(args.db_path) as conn:
            apply_schema(conn)
            key_path, probe_pubkey = ensure_probe_key(conn, ssh_dir)

        if args.print_pubkey:
            print(probe_pubkey)
            _say("install the line above into the authorized_keys of a "
                 "root-capable user on the vantage, then re-run vantage-setup "
                 "WITHOUT --print-pubkey (uses --bootstrap-user, default root).")
            return 0

        ssh_user, identity, extra_opts = _entry_ssh_opts(args)
        if identity == "SHARED":
            identity = str(key_path)
        _ssh_provision_vantage(
            vantage_host=args.vantage_host, vantage_port=args.vantage_port,
            ssh_user=ssh_user, identity=identity, extra_opts=extra_opts,
            probe_pubkey=probe_pubkey)
        _verify_probe_login(
            vantage_host=args.vantage_host, vantage_port=args.vantage_port,
            key_path=key_path)
        _harden_sshd(
            vantage_host=args.vantage_host, vantage_port=args.vantage_port,
            ssh_user=ssh_user, identity=identity, extra_opts=extra_opts)
        known_hosts = ssh_dir / "known_hosts"
        _ssh_keyscan(args.vantage_host, args.vantage_port, known_hosts)
        _register_with_controller(
            vantage_id=args.vantage_id, vantage_host=args.vantage_host,
            vantage_port=args.vantage_port, key_path=key_path,
            known_hosts=known_hosts, db_path=args.db_path)
    except VantageSetupError as e:
        _err(str(e))
        return 3
    _say(f"vantage {args.vantage_id} ready for probes")
    return 0
```

`_verify_probe_login` and `_harden_sshd` are added in Task 7; for this task's
tests they are monkeypatched out, so define minimal stubs now so the module
imports:

```python
def _verify_probe_login(**kwargs) -> None:  # real body in Task 7
    pass


def _harden_sshd(**kwargs) -> None:         # real body in Task 7
    pass
```

- [ ] **Step 4: Update the CLI args in `main.py`**

In `src/mthydra/ops/main.py` (lines 1196-1206), make `--root-key` optional and add
the new flags:

```python
    vs.add_argument("--vantage-id", required=True,
                    help="must match a vantage-add row in the controller DB")
    vs.add_argument("--vantage-host", required=True,
                    help="public IPv4 / hostname of the vantage VPS")
    vs.add_argument("--vantage-port", type=int, default=22,
                    help="vantage SSH port (default 22)")
    g = vs.add_mutually_exclusive_group()
    g.add_argument("--root-key",
                   help="SSH private key with root access on the vantage")
    g.add_argument("--password", action="store_true",
                   help="authenticate the setup session by password (you will be "
                        "prompted on this terminal; never stored — spec T2-D4)")
    g.add_argument("--print-pubkey", action="store_true", dest="print_pubkey",
                   help="print the shared probe pubkey and exit, for providers "
                        "that forbid password auth; install it on a root-capable "
                        "user, then re-run without this flag")
    vs.add_argument("--bootstrap-user", default="root",
                    help="root-capable user to connect as on the --print-pubkey "
                         "re-run (default root)")
    vs.add_argument("--ssh-dir", default="/var/lib/mthydra/ssh",
                    help="where to cache the shared probe key + known_hosts")
    vs.add_argument("--db-path", default=_DEFAULT_DB)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/unit/ops/test_vantage_setup.py -v`
Expected: PASS (all, including the Task-5 shared-key test).

- [ ] **Step 6: Commit**

```bash
git add src/mthydra/ops/vantage_setup.py src/mthydra/ops/main.py tests/unit/ops/test_vantage_setup.py
git commit -m "feat(vantage-setup): root-key / password / print-pubkey entry methods

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: Verify-before-harden + full lockdown

**Files:**
- Modify: `src/mthydra/ops/vantage_setup.py`
- Test: `tests/unit/ops/test_vantage_setup.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/ops/test_vantage_setup.py`:

```python
def test_verify_probe_login_uses_probe_user_and_key(tmp_path, monkeypatch):
    captured = {}
    def _fake(argv, capture_output=True, text=True, timeout=None, input=None):
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, "VERIFY-OK\n", "")
    monkeypatch.setattr(vantage_setup.subprocess, "run", _fake)
    vantage_setup._verify_probe_login(
        vantage_host="h", vantage_port=22, key_path=Path("/k/probe.key"))
    argv = captured["argv"]
    assert "probe@h" in argv
    assert "BatchMode=yes" in argv
    assert argv[argv.index("-i") + 1] == "/k/probe.key"


def test_verify_probe_login_raises_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(vantage_setup.subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 255, "", "denied"))
    with pytest.raises(vantage_setup.VantageSetupError, match="probe login verification failed"):
        vantage_setup._verify_probe_login(
            vantage_host="h", vantage_port=22, key_path=Path("/k/probe.key"))


def test_harden_writes_lockdown_dropin_and_validates(tmp_path, monkeypatch):
    captured = {}
    def _fake(argv, capture_output=True, text=True, timeout=None, input=None):
        captured["argv"] = argv
        captured["input"] = input
        return subprocess.CompletedProcess(argv, 0, "HARDENED\n", "")
    monkeypatch.setattr(vantage_setup.subprocess, "run", _fake)
    vantage_setup._harden_sshd(
        vantage_host="h", vantage_port=22, ssh_user="root",
        identity="/k/root.pem", extra_opts=["-o", "BatchMode=yes"])
    script = captured["input"]
    assert "AllowUsers probe" in script
    assert "PasswordAuthentication no" in script
    assert "PermitRootLogin no" in script
    assert "sshd -t" in script                 # validate before reload
    assert "reload" in script                  # reload, not a hard restart


def test_harden_raises_when_remote_omits_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(vantage_setup.subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "config test failed\n", ""))
    with pytest.raises(vantage_setup.VantageSetupError, match="hardening failed"):
        vantage_setup._harden_sshd(
            vantage_host="h", vantage_port=22, ssh_user="root",
            identity="/k/root.pem", extra_opts=[])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/unit/ops/test_vantage_setup.py -k "verify_probe or harden" -v`
Expected: FAIL — the stub functions accept `**kwargs` and do nothing / wrong signature.

- [ ] **Step 3: Replace the stubs with real implementations**

In `src/mthydra/ops/vantage_setup.py`, replace the Task-6 stub `_verify_probe_login`
and `_harden_sshd` with:

```python
def _verify_probe_login(*, vantage_host: str, vantage_port: int,
                        key_path: Path, timeout: int = 30) -> None:
    """Open a FRESH connection as probe with the shared key and confirm it
    works. Must pass BEFORE hardening — a bad key here would otherwise lock us
    out (spec T2-D6)."""
    _say("verifying probe-key login before hardening")
    argv = [
        "ssh", "-i", str(key_path), "-p", str(vantage_port),
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
        f"probe@{vantage_host}", "echo", "VERIFY-OK",
    ]
    res = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0 or "VERIFY-OK" not in res.stdout:
        raise VantageSetupError(
            f"probe login verification failed (rc={res.returncode}); NOT "
            f"hardening. {res.stderr.strip() or res.stdout.strip()}")


def _harden_sshd(*, vantage_host: str, vantage_port: int, ssh_user: str,
                 identity: str, extra_opts: list[str], timeout: int = 60) -> None:
    """Lock the vantage to probe-key-only (spec T2-D7). Writes a sshd_config
    drop-in, validates with `sshd -t`, then RELOADS (existing sessions survive,
    so this same root session stays alive to report success)."""
    remote_script = """
set -e
cat > /etc/ssh/sshd_config.d/60-mthydra-probe.conf <<'EOF'
AllowUsers probe
PasswordAuthentication no
PermitRootLogin no
EOF
sshd -t
systemctl reload ssh 2>/dev/null || systemctl reload sshd
echo HARDENED
"""
    argv = ["ssh"]
    if identity:
        argv += ["-i", identity]
    argv += ["-p", str(vantage_port), *extra_opts, f"{ssh_user}@{vantage_host}",
             "bash", "-s"]
    _say("hardening vantage sshd to probe-key-only (AllowUsers probe)")
    res = subprocess.run(argv, input=remote_script, capture_output=True,
                         text=True, timeout=timeout)
    if res.returncode != 0 or "HARDENED" not in res.stdout:
        raise VantageSetupError(
            f"hardening failed (rc={res.returncode}): "
            f"{res.stderr.strip() or res.stdout.strip()}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/ops/test_vantage_setup.py -v`
Expected: PASS (all). The Task-6 tests that monkeypatched `_verify_probe_login`
/`_harden_sshd` still pass — they patch by name and don't care about the body.

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/ops/vantage_setup.py tests/unit/ops/test_vantage_setup.py
git commit -m "feat(vantage-setup): verify probe login then lock sshd to probe-only

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 8: Full-suite regression + docs

**Files:**
- Modify: `doc/quickstart-mvp.md:512-538`, `CHANGELOG.md`

- [ ] **Step 1: Run the full test suite**

Run: `python -m pytest -q`
Expected: PASS. Investigate and fix any failure caused by the wheel/vantage-setup
changes before proceeding (do not edit unrelated failing tests — if a failure is
pre-existing on `main`, note it and move on).

- [ ] **Step 2: Run linters as the repo does**

Run: `ruff check src/ tests/` (and `ruff format --check` if the repo uses it —
check `pyproject.toml`/`Makefile` for the exact command first with
`grep -nE "ruff|lint" Makefile pyproject.toml`).
Expected: clean. Fix any new lint in the files this plan touched.

- [ ] **Step 3: Update quickstart §7.4**

In `doc/quickstart-mvp.md`, replace the §7.4 command + wizard description (lines
512-538) to document: the shared DB-backed key (not per-vantage), the three entry
methods (`--root-key` / `--password` / `--print-pubkey --bootstrap-user`), the
verify-before-harden step, and the post-setup lockdown (after `vantage-setup`, the
only way into the vantage is `probe` + the controller key — root and password are
gone; future root access is via the provider console only). State that failover is
automatic: a promoted standby restores the DB and resumes probing with no
re-provisioning. Note the password method may prompt twice (provision + harden).

- [ ] **Step 4: Update CHANGELOG**

Add an entry under the current unreleased section of `CHANGELOG.md`:

```markdown
- **vantage-setup hardening (spec T2):** `mthydra-ops vantage-setup` now opens a
  vantage by `--root-key`, `--password` (interactive, never stored), or
  `--print-pubkey` (operator-installed key); verifies probe-key login, then locks
  sshd to `probe`-key-only (`AllowUsers probe`, no password, no root). The probe
  keypair now lives in `state.sqlite` (table `controller_probe_key`, schema v17),
  so it rides the encrypted backup and a promoted standby resumes probing with no
  manual re-provisioning.
```

- [ ] **Step 5: Commit + push**

```bash
git add doc/quickstart-mvp.md CHANGELOG.md
git commit -m "docs: quickstart 7.4 + CHANGELOG for T2 vantage SSH hardening

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
git push origin main
```

---

## Self-Review (completed during planning)

**Spec coverage:**
- T2-D1 shared key → Tasks 3, 5. T2-D2 key-in-DB + file cache → Tasks 1–3.
- T2-D3 three entry methods → Task 6. T2-D4 no-sshpass/TTY passthrough → Task 6
  (`_entry_ssh_opts` password branch omits BatchMode; test asserts no sshpass).
- T2-D5 one-time password acceptable → realized by Task 6 + Task 7 lockdown.
- T2-D6 verify-before-harden → Task 7 (ordering in `cmd_vantage_setup`; failure test).
- T2-D7 full lockdown → Task 7 `_harden_sshd`. T2-D8 no-sudo probe → unchanged
  provision script (no sudoers line; covered by existing provision test).
- T2-D9 shared not per-controller → Tasks 1–3 (single-row table) + Task 4 failover.
- §5 materialization → Task 3. §7 failover → Task 4. Migration §8 → Task 1.
- §6.1 ambiguity resolved (root-capable bootstrap user) → stated in header + Task 6.

**Placeholder scan:** none — every code step shows complete code; every run step
shows the command + expected result.

**Type consistency:** `ensure_probe_key(conn, ssh_dir) -> (Path, str)` used
identically in Tasks 3, 4, 5. `_entry_ssh_opts -> (user, identity, opts)` feeds
`_ssh_provision_vantage` and `_harden_sshd` with matching kwargs. `probe_key.get`
returns `ProbeKey` (dataclass) used by `ensure_probe_key`. `_verify_probe_login`
/`_harden_sshd` signatures match their call sites and tests.
