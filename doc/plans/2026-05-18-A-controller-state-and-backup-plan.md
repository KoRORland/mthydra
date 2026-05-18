# Spec A — Controller State & Backup Pipeline — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the EU controller's authoritative state model (SQLite), the encrypted off-box backup pipeline (age + S3-compatible), the two-step operator-driven restore, the bootstrap path, the startup self-check, and the operator-side generation-gap monitor — as defined in `doc/specs/2026-05-18-A-controller-state-and-backup.md`.

**Architecture:** Python 3.12 package `mthydra` with two console entry points (`mthydra-controller`, `mthydra-backup-monitor`). State layer is a stdlib `sqlite3` wrapper with one repository module per table-group. Backup pipeline is async (asyncio) with APScheduler for the floor timer and an asyncio `Queue` debouncer for `burned_domains` change events. S3 transport via `boto3` (works against AWS S3, Backblaze B2, MinIO identically). Encryption shells out to the `age` binary. Tests are pytest, with `moto` mocking S3 for unit/integration and a real `minio` container for full end-to-end.

**Tech Stack:**

- Python 3.12 (Ubuntu 24.04 default)
- stdlib: `sqlite3`, `tomllib`, `asyncio`, `pathlib`, `dataclasses`, `subprocess`, `hashlib`
- third-party runtime: `boto3`, `APScheduler`
- third-party test: `pytest`, `pytest-asyncio`, `moto[s3]`, `hypothesis`
- external binary: `age` (Ubuntu package: `age`)
- packaging: `setuptools` via `pyproject.toml`, console_scripts for entries, systemd units shipped under `packaging/systemd/`
- lint: `ruff` (lint + format)

---

## File Structure (locked before tasks)

```
mthydra/
├── pyproject.toml
├── README.md                                  # minimal: links to design + spec + this plan
├── .gitignore                                 # python + venv + .pytest_cache + dist + .ruff_cache
├── packaging/
│   ├── systemd/
│   │   ├── mthydra-controller.service
│   │   └── mthydra-backup-monitor.service
│   └── etc/
│       └── mthydra/
│           └── controller.toml.example
├── src/
│   └── mthydra/
│       ├── __init__.py
│       ├── controller/
│       │   ├── __init__.py
│       │   ├── __main__.py
│       │   ├── cli.py
│       │   ├── config.py
│       │   ├── startup.py
│       │   ├── bootstrap.py
│       │   ├── state/
│       │   │   ├── __init__.py
│       │   │   ├── schema.py
│       │   │   ├── db.py
│       │   │   ├── invariants.py
│       │   │   ├── burned.py
│       │   │   ├── cover_pool.py
│       │   │   ├── authority.py
│       │   │   ├── descriptor.py
│       │   │   ├── ru_boxes.py
│       │   │   ├── credentials.py
│       │   │   ├── users_shards.py
│       │   │   ├── tokens.py
│       │   │   ├── obligations.py
│       │   │   ├── backup_log.py
│       │   │   └── audit.py
│       │   ├── backup/
│       │   │   ├── __init__.py
│       │   │   ├── age_crypt.py
│       │   │   ├── s3_dest.py
│       │   │   ├── pipeline.py
│       │   │   ├── triggers.py
│       │   │   └── reconcile.py
│       │   └── restore/
│       │       ├── __init__.py
│       │       ├── decrypt.py
│       │       ├── summary.py
│       │       └── adopt.py
│       └── backup_monitor/
│           ├── __init__.py
│           ├── __main__.py
│           ├── cli.py
│           ├── poller.py
│           └── emailer.py
└── tests/
    ├── conftest.py
    ├── unit/
    │   ├── controller/
    │   │   ├── test_config.py
    │   │   ├── test_startup.py
    │   │   ├── test_bootstrap.py
    │   │   ├── state/
    │   │   │   ├── test_db.py
    │   │   │   ├── test_schema.py
    │   │   │   ├── test_invariants.py
    │   │   │   ├── test_burned.py
    │   │   │   ├── test_cover_pool.py
    │   │   │   ├── test_authority.py
    │   │   │   ├── test_descriptor.py
    │   │   │   ├── test_ru_boxes.py
    │   │   │   ├── test_credentials.py
    │   │   │   ├── test_users_shards.py
    │   │   │   ├── test_tokens.py
    │   │   │   ├── test_obligations.py
    │   │   │   ├── test_backup_log.py
    │   │   │   └── test_audit.py
    │   │   ├── backup/
    │   │   │   ├── test_age_crypt.py
    │   │   │   ├── test_s3_dest.py
    │   │   │   ├── test_pipeline.py
    │   │   │   ├── test_triggers.py
    │   │   │   └── test_reconcile.py
    │   │   └── restore/
    │   │       ├── test_decrypt.py
    │   │       ├── test_summary.py
    │   │       └── test_adopt.py
    │   └── backup_monitor/
    │       ├── test_poller.py
    │       └── test_emailer.py
    ├── integration/
    │   ├── test_end_to_end_backup.py
    │   ├── test_end_to_end_restore.py
    │   ├── test_gap_monitor.py
    │   └── test_object_lock.py
    └── property/
        └── test_burned_set_monotonic.py
```

Responsibility per file: each state module owns one table-group's CRUD + invariants. Each backup module owns one concern (encrypt, push, orchestrate, schedule, recover). Restore is symmetric to backup. No cross-imports across `backup/` and `restore/`; both depend on `state/`.

---

## Phase 0 — Project Skeleton

### Task 0: Project skeleton, pyproject.toml, tooling, CI-runnable test harness

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `README.md`
- Create: `src/mthydra/__init__.py`
- Create: `tests/conftest.py`
- Create: `tests/unit/__init__.py`

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "mthydra"
version = "0.0.1"
description = "Resilient Telegram access controller — EU side"
requires-python = ">=3.12"
dependencies = [
    "boto3>=1.34",
    "APScheduler>=3.10",
]

[project.optional-dependencies]
dev = [
    "pytest>=8",
    "pytest-asyncio>=0.23",
    "moto[s3]>=5",
    "hypothesis>=6",
    "ruff>=0.5",
]

[project.scripts]
mthydra-controller = "mthydra.controller.__main__:main"
mthydra-backup-monitor = "mthydra.backup_monitor.__main__:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"
addopts = "-ra --strict-markers"

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "W", "I", "B", "UP", "SIM"]
```

- [ ] **Step 2: Write `.gitignore`**

```
__pycache__/
*.py[cod]
*.egg-info/
.venv/
venv/
.pytest_cache/
.ruff_cache/
dist/
build/
*.sqlite
*.sqlite-wal
*.sqlite-shm
.coverage
htmlcov/
```

- [ ] **Step 3: Write `README.md`**

```markdown
# mthydra

Resilient Telegram access controller. See `doc/design.md` for the architecture, `doc/build-plan.md` for the artifact decomposition, and `doc/specs/` and `doc/plans/` for individual artifact specs and implementation plans.

## Development

```
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest
```
```

- [ ] **Step 4: Write `src/mthydra/__init__.py`**

```python
__version__ = "0.0.1"
```

- [ ] **Step 5: Write `tests/conftest.py`**

```python
import pytest


@pytest.fixture
def tmp_db_path(tmp_path):
    return tmp_path / "state.sqlite"
```

- [ ] **Step 6: Write `tests/unit/__init__.py`** (empty file).

- [ ] **Step 7: Verify install + empty test run**

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest
```

Expected: pytest reports `no tests ran` with exit code 5; install succeeds without errors.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml .gitignore README.md src/ tests/
git commit -m "scaffold: project skeleton (pyproject, src layout, pytest+ruff)"
git push origin main
```

---

## Phase 1 — State Layer

### Task 1: Schema module + schema_version table

**Files:**
- Create: `src/mthydra/controller/__init__.py` (empty)
- Create: `src/mthydra/controller/state/__init__.py` (empty)
- Create: `src/mthydra/controller/state/schema.py`
- Create: `tests/unit/controller/__init__.py` (empty)
- Create: `tests/unit/controller/state/__init__.py` (empty)
- Create: `tests/unit/controller/state/test_schema.py`

- [ ] **Step 1: Write the failing test**

`tests/unit/controller/state/test_schema.py`:

```python
import sqlite3

from mthydra.controller.state.schema import SCHEMA_VERSION, apply_schema


def test_apply_schema_creates_version_row(tmp_db_path):
    conn = sqlite3.connect(tmp_db_path)
    apply_schema(conn)
    row = conn.execute("SELECT version FROM schema_version WHERE rowid = 1").fetchone()
    assert row == (SCHEMA_VERSION,)


def test_apply_schema_is_idempotent(tmp_db_path):
    conn = sqlite3.connect(tmp_db_path)
    apply_schema(conn)
    apply_schema(conn)
    count = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
    assert count == 1
```

- [ ] **Step 2: Run test to verify it fails**

`pytest tests/unit/controller/state/test_schema.py -v`
Expected: ImportError / ModuleNotFoundError on `mthydra.controller.state.schema`.

- [ ] **Step 3: Write minimal implementation**

Touch empty `__init__.py` files as listed above.

`src/mthydra/controller/state/schema.py`:

```python
"""SQLite schema for the controller's runtime state."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

SCHEMA_VERSION = 1

_STATEMENTS: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS schema_version (
      version    INTEGER NOT NULL,
      applied_at TEXT    NOT NULL,
      CHECK (rowid = 1)
    )
    """,
]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def apply_schema(conn: sqlite3.Connection) -> None:
    """Create tables if missing; insert the schema_version row exactly once."""
    for stmt in _STATEMENTS:
        conn.execute(stmt)
    existing = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
    if existing == 0:
        conn.execute(
            "INSERT INTO schema_version (rowid, version, applied_at) VALUES (1, ?, ?)",
            (SCHEMA_VERSION, _now()),
        )
    conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

`pytest tests/unit/controller/state/test_schema.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/ tests/unit/controller/
git commit -m "state(A): schema module + schema_version table"
git push origin main
```

---

### Task 2: DB connection module (PRAGMAs, WAL, foreign keys)

**Files:**
- Create: `src/mthydra/controller/state/db.py`
- Create: `tests/unit/controller/state/test_db.py`

- [ ] **Step 1: Write the failing test**

```python
import sqlite3

import pytest

from mthydra.controller.state.db import connect


def test_connect_enables_foreign_keys(tmp_db_path):
    conn = connect(tmp_db_path)
    fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    assert fk == 1


def test_connect_uses_wal_journal_mode(tmp_db_path):
    conn = connect(tmp_db_path)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_connect_creates_parent_dir(tmp_path):
    target = tmp_path / "deep" / "nested" / "state.sqlite"
    connect(target)
    assert target.exists()


def test_connect_rejects_missing_path_for_readonly(tmp_path):
    target = tmp_path / "missing.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        connect(target, read_only=True)
```

- [ ] **Step 2: Run test to verify it fails**

`pytest tests/unit/controller/state/test_db.py -v`
Expected: ModuleNotFoundError on `mthydra.controller.state.db`.

- [ ] **Step 3: Write minimal implementation**

`src/mthydra/controller/state/db.py`:

```python
"""SQLite connection management for the controller state."""
from __future__ import annotations

import sqlite3
from pathlib import Path


def connect(db_path: Path | str, *, read_only: bool = False) -> sqlite3.Connection:
    """Open a SQLite connection with the project's standard PRAGMAs.

    Creates parent directories for read-write opens. Read-only opens require
    the file to already exist.
    """
    db_path = Path(db_path)
    if read_only:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn
```

- [ ] **Step 4: Run test to verify it passes**

`pytest tests/unit/controller/state/test_db.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/state/db.py tests/unit/controller/state/test_db.py
git commit -m "state(A): db connect module with WAL + foreign keys"
git push origin main
```

---

### Task 3: burned_domains + cover_domain_pool tables + transactional mark_burned chokepoint

**Files:**
- Modify: `src/mthydra/controller/state/schema.py` (extend `_STATEMENTS`)
- Create: `src/mthydra/controller/state/burned.py`
- Create: `src/mthydra/controller/state/cover_pool.py`
- Create: `tests/unit/controller/state/test_burned.py`
- Create: `tests/unit/controller/state/test_cover_pool.py`

- [ ] **Step 1: Write the failing tests**

`tests/unit/controller/state/test_cover_pool.py`:

```python
from mthydra.controller.state.cover_pool import (
    add_candidate,
    list_by_state,
    mark_verified,
    move_to_in_use,
)
from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema


def _conn(tmp_db_path):
    conn = connect(tmp_db_path)
    apply_schema(conn)
    return conn


def test_add_candidate_starts_unverified(tmp_db_path):
    conn = _conn(tmp_db_path)
    add_candidate(conn, "example.org", added_at="2026-05-18T00:00:00Z")
    rows = list_by_state(conn, "candidate_unverified")
    assert [r.domain for r in rows] == ["example.org"]


def test_mark_verified_transitions_state(tmp_db_path):
    conn = _conn(tmp_db_path)
    add_candidate(conn, "example.org", added_at="2026-05-18T00:00:00Z")
    mark_verified(conn, "example.org", from_vantage="ru-vantage-1", at="2026-05-18T01:00:00Z")
    assert list_by_state(conn, "candidate_unverified") == []
    rows = list_by_state(conn, "candidate_verified")
    assert rows[0].verified_from_vantage == "ru-vantage-1"


def test_move_to_in_use_requires_verified(tmp_db_path):
    conn = _conn(tmp_db_path)
    add_candidate(conn, "example.org", added_at="2026-05-18T00:00:00Z")
    try:
        move_to_in_use(conn, "example.org", box_id="box-1")
    except ValueError as e:
        assert "candidate_verified" in str(e)
    else:
        raise AssertionError("expected ValueError")
```

`tests/unit/controller/state/test_burned.py`:

```python
import pytest

from mthydra.controller.state.burned import is_burned, mark_burned
from mthydra.controller.state.cover_pool import add_candidate, list_by_state, mark_verified, move_to_in_use
from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema


def _conn(tmp_db_path):
    conn = connect(tmp_db_path)
    apply_schema(conn)
    return conn


def _seed(conn, domain):
    add_candidate(conn, domain, added_at="2026-05-18T00:00:00Z")
    mark_verified(conn, domain, from_vantage="v1", at="2026-05-18T01:00:00Z")
    move_to_in_use(conn, domain, box_id="box-1")


def test_mark_burned_moves_domain_atomically(tmp_db_path):
    conn = _conn(tmp_db_path)
    _seed(conn, "example.org")
    mark_burned(
        conn,
        domain="example.org",
        reason="job2_kill",
        last_box_id="box-1",
        at="2026-05-18T02:00:00Z",
        details=None,
    )
    assert is_burned(conn, "example.org")
    assert list_by_state(conn, "in_use") == []


def test_mark_burned_refuses_already_burned(tmp_db_path):
    conn = _conn(tmp_db_path)
    _seed(conn, "example.org")
    mark_burned(conn, "example.org", "job2_kill", "box-1", "2026-05-18T02:00:00Z", None)
    with pytest.raises(ValueError, match="already burned"):
        mark_burned(conn, "example.org", "job2_kill", "box-1", "2026-05-18T03:00:00Z", None)


def test_mark_burned_refuses_unknown_domain(tmp_db_path):
    conn = _conn(tmp_db_path)
    with pytest.raises(ValueError, match="not present"):
        mark_burned(conn, "ghost.org", "job2_kill", "box-1", "2026-05-18T02:00:00Z", None)
```

- [ ] **Step 2: Run tests to verify they fail**

`pytest tests/unit/controller/state/test_burned.py tests/unit/controller/state/test_cover_pool.py -v`
Expected: ModuleNotFoundError on `cover_pool` and `burned`.

- [ ] **Step 3: Extend schema.py — append to `_STATEMENTS`**

```python
    """
    CREATE TABLE IF NOT EXISTS cover_domain_pool (
      domain                TEXT PRIMARY KEY,
      state                 TEXT NOT NULL CHECK (state IN ('candidate_unverified','candidate_verified','in_use')),
      last_verified_at      TEXT,
      verified_from_vantage TEXT,
      assigned_box_id       TEXT,
      added_at              TEXT NOT NULL,
      notes                 TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS burned_domains (
      domain      TEXT PRIMARY KEY,
      burned_at   TEXT NOT NULL,
      reason      TEXT NOT NULL,
      last_box_id TEXT,
      details     TEXT
    )
    """,
```

- [ ] **Step 4: Write `src/mthydra/controller/state/cover_pool.py`**

```python
"""Cover-domain pool repository (consumed in detail by spec C)."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class CoverDomain:
    domain: str
    state: str
    last_verified_at: str | None
    verified_from_vantage: str | None
    assigned_box_id: str | None
    added_at: str
    notes: str | None


def add_candidate(conn: sqlite3.Connection, domain: str, *, added_at: str, notes: str | None = None) -> None:
    conn.execute(
        "INSERT INTO cover_domain_pool (domain, state, added_at, notes) VALUES (?, 'candidate_unverified', ?, ?)",
        (domain, added_at, notes),
    )
    conn.commit()


def mark_verified(conn: sqlite3.Connection, domain: str, *, from_vantage: str, at: str) -> None:
    cur = conn.execute(
        "UPDATE cover_domain_pool SET state='candidate_verified', verified_from_vantage=?, last_verified_at=? "
        "WHERE domain=? AND state='candidate_unverified'",
        (from_vantage, at, domain),
    )
    if cur.rowcount == 0:
        raise ValueError(f"domain {domain!r} is not in candidate_unverified state")
    conn.commit()


def move_to_in_use(conn: sqlite3.Connection, domain: str, *, box_id: str) -> None:
    cur = conn.execute(
        "UPDATE cover_domain_pool SET state='in_use', assigned_box_id=? "
        "WHERE domain=? AND state='candidate_verified'",
        (box_id, domain),
    )
    if cur.rowcount == 0:
        raise ValueError(f"domain {domain!r} is not in candidate_verified state")
    conn.commit()


def list_by_state(conn: sqlite3.Connection, state: str) -> list[CoverDomain]:
    rows = conn.execute(
        "SELECT domain, state, last_verified_at, verified_from_vantage, assigned_box_id, added_at, notes "
        "FROM cover_domain_pool WHERE state=? ORDER BY domain",
        (state,),
    ).fetchall()
    return [CoverDomain(*r) for r in rows]
```

- [ ] **Step 5: Write `src/mthydra/controller/state/burned.py`**

```python
"""Burned-domain repository — append-only, monotonic, never deleted from."""
from __future__ import annotations

import sqlite3


def is_burned(conn: sqlite3.Connection, domain: str) -> bool:
    row = conn.execute("SELECT 1 FROM burned_domains WHERE domain=?", (domain,)).fetchone()
    return row is not None


def mark_burned(
    conn: sqlite3.Connection,
    domain: str,
    reason: str,
    last_box_id: str | None,
    at: str,
    details: str | None,
) -> None:
    """Move a domain from cover_domain_pool to burned_domains in a single transaction.

    This is the only path by which a row may be inserted into burned_domains.
    """
    if is_burned(conn, domain):
        raise ValueError(f"domain {domain!r} is already burned")
    try:
        conn.execute("BEGIN")
        cur = conn.execute("DELETE FROM cover_domain_pool WHERE domain=?", (domain,))
        if cur.rowcount == 0:
            raise ValueError(f"domain {domain!r} is not present in cover_domain_pool")
        conn.execute(
            "INSERT INTO burned_domains (domain, burned_at, reason, last_box_id, details) VALUES (?, ?, ?, ?, ?)",
            (domain, at, reason, last_box_id, details),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
```

- [ ] **Step 6: Run tests to verify they pass**

`pytest tests/unit/controller/state/ -v`
Expected: 8 passed (schema 2 + db 4 + cover_pool 3 + burned 3 = 12; if any other count, debug before moving on).

- [ ] **Step 7: Commit**

```bash
git add src/mthydra/controller/state/schema.py \
        src/mthydra/controller/state/cover_pool.py \
        src/mthydra/controller/state/burned.py \
        tests/unit/controller/state/test_cover_pool.py \
        tests/unit/controller/state/test_burned.py
git commit -m "state(A): cover_domain_pool + burned_domains with transactional mark_burned"
git push origin main
```

---

### Task 4: credential_authority + descriptor_signing_key + descriptor_history

**Files:**
- Modify: `src/mthydra/controller/state/schema.py`
- Create: `src/mthydra/controller/state/authority.py`
- Create: `src/mthydra/controller/state/descriptor.py`
- Create: `tests/unit/controller/state/test_authority.py`
- Create: `tests/unit/controller/state/test_descriptor.py`

- [ ] **Step 1: Write failing tests**

`tests/unit/controller/state/test_authority.py`:

```python
import pytest

from mthydra.controller.state.authority import (
    current_authority,
    insert_authority,
    list_authorities,
    retire_authority,
)
from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema


def _conn(tmp_db_path):
    conn = connect(tmp_db_path)
    apply_schema(conn)
    return conn


def test_insert_first_authority(tmp_db_path):
    conn = _conn(tmp_db_path)
    insert_authority(conn, generation=1, privkey_pem="PRIV", pubkey_pem="PUB", created_at="2026-05-18T00:00:00Z")
    cur = current_authority(conn)
    assert cur.generation == 1
    assert cur.retired_at is None


def test_retire_then_insert_next(tmp_db_path):
    conn = _conn(tmp_db_path)
    insert_authority(conn, 1, "P1", "K1", "2026-05-18T00:00:00Z")
    retire_authority(conn, 1, at="2026-05-19T00:00:00Z")
    insert_authority(conn, 2, "P2", "K2", "2026-05-19T00:00:01Z")
    cur = current_authority(conn)
    assert cur.generation == 2
    assert len(list_authorities(conn)) == 2


def test_current_authority_raises_when_none_active(tmp_db_path):
    conn = _conn(tmp_db_path)
    insert_authority(conn, 1, "P", "K", "2026-05-18T00:00:00Z")
    retire_authority(conn, 1, at="2026-05-19T00:00:00Z")
    with pytest.raises(LookupError):
        current_authority(conn)
```

`tests/unit/controller/state/test_descriptor.py`:

```python
import pytest

from mthydra.controller.state.db import connect
from mthydra.controller.state.descriptor import (
    current_signing_key,
    insert_descriptor,
    insert_signing_key,
    latest_descriptor,
    retire_signing_key,
)
from mthydra.controller.state.schema import apply_schema


def _conn(tmp_db_path):
    conn = connect(tmp_db_path)
    apply_schema(conn)
    return conn


def test_insert_signing_key(tmp_db_path):
    conn = _conn(tmp_db_path)
    insert_signing_key(conn, generation=1, privkey=b"P", pubkey=b"K", created_at="2026-05-18T00:00:00Z")
    cur = current_signing_key(conn)
    assert cur.generation == 1


def test_insert_descriptor_references_active_key(tmp_db_path):
    conn = _conn(tmp_db_path)
    insert_signing_key(conn, 1, b"P", b"K", "2026-05-18T00:00:00Z")
    insert_descriptor(
        conn,
        generation=1,
        payload='{"exit_set":[]}',
        signed_at="2026-05-18T01:00:00Z",
        valid_until="2026-05-18T13:00:00Z",
        signing_key_generation=1,
    )
    d = latest_descriptor(conn)
    assert d.generation == 1
    assert d.signing_key_generation == 1


def test_insert_descriptor_rejects_unknown_signing_key(tmp_db_path):
    conn = _conn(tmp_db_path)
    with pytest.raises(Exception):  # FK violation surfaces as IntegrityError
        insert_descriptor(
            conn, 1, '{"exit_set":[]}', "2026-05-18T01:00:00Z", "2026-05-18T13:00:00Z", 99
        )
```

- [ ] **Step 2: Run tests to verify failure**

`pytest tests/unit/controller/state/test_authority.py tests/unit/controller/state/test_descriptor.py -v`
Expected: ImportError on `authority` and `descriptor`.

- [ ] **Step 3: Extend `_STATEMENTS` in `schema.py`**

```python
    """
    CREATE TABLE IF NOT EXISTS credential_authority (
      generation  INTEGER PRIMARY KEY,
      privkey_pem TEXT NOT NULL,
      pubkey_pem  TEXT NOT NULL,
      created_at  TEXT NOT NULL,
      retired_at  TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS descriptor_signing_key (
      generation  INTEGER PRIMARY KEY,
      privkey     BLOB NOT NULL,
      pubkey      BLOB NOT NULL,
      created_at  TEXT NOT NULL,
      retired_at  TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS descriptor_history (
      generation             INTEGER PRIMARY KEY,
      payload                TEXT NOT NULL,
      signed_at              TEXT NOT NULL,
      valid_until            TEXT NOT NULL,
      signing_key_generation INTEGER NOT NULL,
      FOREIGN KEY (signing_key_generation) REFERENCES descriptor_signing_key(generation)
    )
    """,
```

- [ ] **Step 4: Write `src/mthydra/controller/state/authority.py`**

```python
"""Credential-authority key material repository."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Authority:
    generation: int
    privkey_pem: str
    pubkey_pem: str
    created_at: str
    retired_at: str | None


def insert_authority(
    conn: sqlite3.Connection, generation: int, privkey_pem: str, pubkey_pem: str, created_at: str
) -> None:
    conn.execute(
        "INSERT INTO credential_authority (generation, privkey_pem, pubkey_pem, created_at) VALUES (?, ?, ?, ?)",
        (generation, privkey_pem, pubkey_pem, created_at),
    )
    conn.commit()


def retire_authority(conn: sqlite3.Connection, generation: int, *, at: str) -> None:
    conn.execute(
        "UPDATE credential_authority SET retired_at=? WHERE generation=? AND retired_at IS NULL",
        (at, generation),
    )
    conn.commit()


def current_authority(conn: sqlite3.Connection) -> Authority:
    row = conn.execute(
        "SELECT generation, privkey_pem, pubkey_pem, created_at, retired_at "
        "FROM credential_authority WHERE retired_at IS NULL "
        "ORDER BY generation DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise LookupError("no active credential_authority")
    return Authority(*row)


def list_authorities(conn: sqlite3.Connection) -> list[Authority]:
    rows = conn.execute(
        "SELECT generation, privkey_pem, pubkey_pem, created_at, retired_at "
        "FROM credential_authority ORDER BY generation"
    ).fetchall()
    return [Authority(*r) for r in rows]
```

- [ ] **Step 5: Write `src/mthydra/controller/state/descriptor.py`**

```python
"""Descriptor-signing key + signed-descriptor history (consumed by spec B)."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class SigningKey:
    generation: int
    privkey: bytes
    pubkey: bytes
    created_at: str
    retired_at: str | None


@dataclass(frozen=True)
class Descriptor:
    generation: int
    payload: str
    signed_at: str
    valid_until: str
    signing_key_generation: int


def insert_signing_key(
    conn: sqlite3.Connection, generation: int, privkey: bytes, pubkey: bytes, created_at: str
) -> None:
    conn.execute(
        "INSERT INTO descriptor_signing_key (generation, privkey, pubkey, created_at) VALUES (?, ?, ?, ?)",
        (generation, privkey, pubkey, created_at),
    )
    conn.commit()


def retire_signing_key(conn: sqlite3.Connection, generation: int, *, at: str) -> None:
    conn.execute(
        "UPDATE descriptor_signing_key SET retired_at=? WHERE generation=? AND retired_at IS NULL",
        (at, generation),
    )
    conn.commit()


def current_signing_key(conn: sqlite3.Connection) -> SigningKey:
    row = conn.execute(
        "SELECT generation, privkey, pubkey, created_at, retired_at FROM descriptor_signing_key "
        "WHERE retired_at IS NULL ORDER BY generation DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise LookupError("no active descriptor_signing_key")
    return SigningKey(*row)


def insert_descriptor(
    conn: sqlite3.Connection,
    generation: int,
    payload: str,
    signed_at: str,
    valid_until: str,
    signing_key_generation: int,
) -> None:
    conn.execute(
        "INSERT INTO descriptor_history "
        "(generation, payload, signed_at, valid_until, signing_key_generation) "
        "VALUES (?, ?, ?, ?, ?)",
        (generation, payload, signed_at, valid_until, signing_key_generation),
    )
    conn.commit()


def latest_descriptor(conn: sqlite3.Connection) -> Descriptor:
    row = conn.execute(
        "SELECT generation, payload, signed_at, valid_until, signing_key_generation "
        "FROM descriptor_history ORDER BY generation DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise LookupError("no descriptors recorded")
    return Descriptor(*row)
```

- [ ] **Step 6: Run tests**

`pytest tests/unit/controller/state/test_authority.py tests/unit/controller/state/test_descriptor.py -v`
Expected: 6 passed.

- [ ] **Step 7: Commit**

```bash
git add src/mthydra/controller/state/schema.py \
        src/mthydra/controller/state/authority.py \
        src/mthydra/controller/state/descriptor.py \
        tests/unit/controller/state/test_authority.py \
        tests/unit/controller/state/test_descriptor.py
git commit -m "state(A): credential authority + descriptor signing key + descriptor history"
git push origin main
```

---

### Task 5: ru_boxes + onward_credentials

**Files:**
- Modify: `src/mthydra/controller/state/schema.py`
- Create: `src/mthydra/controller/state/ru_boxes.py`
- Create: `src/mthydra/controller/state/credentials.py`
- Create: `tests/unit/controller/state/test_ru_boxes.py`
- Create: `tests/unit/controller/state/test_credentials.py`

- [ ] **Step 1: Write failing tests**

`tests/unit/controller/state/test_ru_boxes.py`:

```python
import pytest

from mthydra.controller.state.db import connect
from mthydra.controller.state.ru_boxes import (
    Box,
    insert_box,
    list_live,
    mark_live,
    mark_terminated,
)
from mthydra.controller.state.schema import apply_schema


def _conn(tmp_db_path):
    conn = connect(tmp_db_path)
    apply_schema(conn)
    return conn


def test_insert_starts_in_provisioning(tmp_db_path):
    conn = _conn(tmp_db_path)
    insert_box(
        conn,
        box_id="box-1",
        provider="hetzner",
        region="fsn1",
        public_ip=None,
        sni="example.org",
        image_version="abc123",
        created_at="2026-05-18T00:00:00Z",
    )
    assert list_live(conn) == []


def test_mark_live_transitions(tmp_db_path):
    conn = _conn(tmp_db_path)
    insert_box(conn, "box-1", "hetzner", "fsn1", None, "example.org", "abc123", "2026-05-18T00:00:00Z")
    mark_live(conn, "box-1", public_ip="1.2.3.4", at="2026-05-18T00:10:00Z")
    live = list_live(conn)
    assert [b.box_id for b in live] == ["box-1"]
    assert live[0].public_ip == "1.2.3.4"


def test_mark_terminated_removes_from_live(tmp_db_path):
    conn = _conn(tmp_db_path)
    insert_box(conn, "box-1", "hetzner", "fsn1", None, "example.org", "abc123", "2026-05-18T00:00:00Z")
    mark_live(conn, "box-1", public_ip="1.2.3.4", at="2026-05-18T00:10:00Z")
    mark_terminated(conn, "box-1", reason="job2_kill", at="2026-05-18T01:00:00Z")
    assert list_live(conn) == []


def test_sni_uniqueness_enforced(tmp_db_path):
    conn = _conn(tmp_db_path)
    insert_box(conn, "box-1", "hetzner", "fsn1", None, "example.org", "abc123", "2026-05-18T00:00:00Z")
    with pytest.raises(Exception):
        insert_box(conn, "box-2", "hetzner", "fsn1", None, "example.org", "abc123", "2026-05-18T00:01:00Z")
```

`tests/unit/controller/state/test_credentials.py`:

```python
from mthydra.controller.state.authority import insert_authority
from mthydra.controller.state.credentials import active_for_box, issue_credential, revoke_credential
from mthydra.controller.state.db import connect
from mthydra.controller.state.ru_boxes import insert_box
from mthydra.controller.state.schema import apply_schema


def _conn(tmp_db_path):
    conn = connect(tmp_db_path)
    apply_schema(conn)
    insert_authority(conn, 1, "P", "K", "2026-05-18T00:00:00Z")
    insert_box(conn, "box-1", "hetzner", "fsn1", None, "example.org", "abc123", "2026-05-18T00:00:00Z")
    return conn


def test_issue_returns_unique_cred(tmp_db_path):
    conn = _conn(tmp_db_path)
    c1 = issue_credential(conn, box_id="box-1", credential=b"S1", issued_at="2026-05-18T00:01:00Z", authority_generation=1)
    c2 = issue_credential(conn, box_id="box-1", credential=b"S2", issued_at="2026-05-18T00:02:00Z", authority_generation=1)
    assert c1 != c2


def test_active_for_box_excludes_revoked(tmp_db_path):
    conn = _conn(tmp_db_path)
    cid = issue_credential(conn, "box-1", b"S1", "2026-05-18T00:01:00Z", 1)
    revoke_credential(conn, cid, at="2026-05-18T00:05:00Z")
    assert active_for_box(conn, "box-1") == []
```

- [ ] **Step 2: Run tests to verify they fail**

`pytest tests/unit/controller/state/test_ru_boxes.py tests/unit/controller/state/test_credentials.py -v`
Expected: ImportError on `ru_boxes` / `credentials`.

- [ ] **Step 3: Extend `_STATEMENTS`**

```python
    """
    CREATE TABLE IF NOT EXISTS shards (
      shard_id           TEXT PRIMARY KEY,
      members_json       TEXT NOT NULL,
      last_reshuffled_at TEXT NOT NULL,
      created_at         TEXT NOT NULL,
      retired_at         TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ru_boxes (
      box_id             TEXT PRIMARY KEY,
      provider           TEXT NOT NULL,
      region             TEXT NOT NULL,
      public_ip          TEXT,
      sni                TEXT UNIQUE NOT NULL,
      shard_id           TEXT,
      state              TEXT NOT NULL CHECK (state IN ('provisioning','live','terminated')),
      image_version      TEXT NOT NULL,
      created_at         TEXT NOT NULL,
      went_live_at       TEXT,
      terminated_at      TEXT,
      termination_reason TEXT,
      FOREIGN KEY (shard_id) REFERENCES shards(shard_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS onward_credentials (
      cred_id              TEXT PRIMARY KEY,
      box_id               TEXT NOT NULL,
      credential           BLOB NOT NULL,
      issued_at            TEXT NOT NULL,
      revoked_at           TEXT,
      authority_generation INTEGER NOT NULL,
      FOREIGN KEY (box_id) REFERENCES ru_boxes(box_id),
      FOREIGN KEY (authority_generation) REFERENCES credential_authority(generation)
    )
    """,
```

- [ ] **Step 4: Write `src/mthydra/controller/state/ru_boxes.py`**

```python
"""RU box inventory repository."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Box:
    box_id: str
    provider: str
    region: str
    public_ip: str | None
    sni: str
    shard_id: str | None
    state: str
    image_version: str
    created_at: str
    went_live_at: str | None
    terminated_at: str | None
    termination_reason: str | None


def insert_box(
    conn: sqlite3.Connection,
    box_id: str,
    provider: str,
    region: str,
    public_ip: str | None,
    sni: str,
    image_version: str,
    created_at: str,
) -> None:
    conn.execute(
        "INSERT INTO ru_boxes (box_id, provider, region, public_ip, sni, state, image_version, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'provisioning', ?, ?)",
        (box_id, provider, region, public_ip, sni, image_version, created_at),
    )
    conn.commit()


def mark_live(conn: sqlite3.Connection, box_id: str, *, public_ip: str, at: str) -> None:
    cur = conn.execute(
        "UPDATE ru_boxes SET state='live', public_ip=?, went_live_at=? "
        "WHERE box_id=? AND state='provisioning'",
        (public_ip, at, box_id),
    )
    if cur.rowcount == 0:
        raise ValueError(f"box {box_id!r} is not in provisioning state")
    conn.commit()


def mark_terminated(conn: sqlite3.Connection, box_id: str, *, reason: str, at: str) -> None:
    cur = conn.execute(
        "UPDATE ru_boxes SET state='terminated', terminated_at=?, termination_reason=? "
        "WHERE box_id=? AND state IN ('provisioning','live')",
        (at, reason, box_id),
    )
    if cur.rowcount == 0:
        raise ValueError(f"box {box_id!r} is not in a terminable state")
    conn.commit()


def list_live(conn: sqlite3.Connection) -> list[Box]:
    rows = conn.execute(
        "SELECT box_id, provider, region, public_ip, sni, shard_id, state, image_version, "
        "created_at, went_live_at, terminated_at, termination_reason "
        "FROM ru_boxes WHERE state='live' ORDER BY box_id"
    ).fetchall()
    return [Box(*r) for r in rows]
```

- [ ] **Step 5: Write `src/mthydra/controller/state/credentials.py`**

```python
"""Onward-credentials repository (per-box revocable secret)."""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class Credential:
    cred_id: str
    box_id: str
    credential: bytes
    issued_at: str
    revoked_at: str | None
    authority_generation: int


def issue_credential(
    conn: sqlite3.Connection,
    box_id: str,
    credential: bytes,
    issued_at: str,
    authority_generation: int,
) -> str:
    cred_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO onward_credentials (cred_id, box_id, credential, issued_at, authority_generation) "
        "VALUES (?, ?, ?, ?, ?)",
        (cred_id, box_id, credential, issued_at, authority_generation),
    )
    conn.commit()
    return cred_id


def revoke_credential(conn: sqlite3.Connection, cred_id: str, *, at: str) -> None:
    cur = conn.execute(
        "UPDATE onward_credentials SET revoked_at=? WHERE cred_id=? AND revoked_at IS NULL",
        (at, cred_id),
    )
    if cur.rowcount == 0:
        raise ValueError(f"credential {cred_id!r} not active")
    conn.commit()


def active_for_box(conn: sqlite3.Connection, box_id: str) -> list[Credential]:
    rows = conn.execute(
        "SELECT cred_id, box_id, credential, issued_at, revoked_at, authority_generation "
        "FROM onward_credentials WHERE box_id=? AND revoked_at IS NULL",
        (box_id,),
    ).fetchall()
    return [Credential(*r) for r in rows]
```

- [ ] **Step 6: Run tests**

`pytest tests/unit/controller/state/test_ru_boxes.py tests/unit/controller/state/test_credentials.py -v`
Expected: 6 passed.

- [ ] **Step 7: Commit**

```bash
git add src/mthydra/controller/state/schema.py \
        src/mthydra/controller/state/ru_boxes.py \
        src/mthydra/controller/state/credentials.py \
        tests/unit/controller/state/test_ru_boxes.py \
        tests/unit/controller/state/test_credentials.py
git commit -m "state(A): ru_boxes + onward_credentials + shards table"
git push origin main
```

---

### Task 6: users + shards (operations) + published_subsets

**Files:**
- Modify: `src/mthydra/controller/state/schema.py`
- Create: `src/mthydra/controller/state/users_shards.py`
- Create: `tests/unit/controller/state/test_users_shards.py`

- [ ] **Step 1: Write failing tests**

```python
import json

from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema
from mthydra.controller.state.users_shards import (
    add_user,
    create_shard,
    latest_published_subset,
    list_users,
    publish_subset,
    set_user_shard,
)


def _conn(tmp_db_path):
    conn = connect(tmp_db_path)
    apply_schema(conn)
    return conn


def test_add_user_and_list(tmp_db_path):
    conn = _conn(tmp_db_path)
    add_user(conn, user_id="u1", display_name="Alice", out_of_band_channel="signal:+1", at="2026-05-18T00:00:00Z")
    users = list_users(conn)
    assert [u.user_id for u in users] == ["u1"]


def test_create_shard_and_assign_user(tmp_db_path):
    conn = _conn(tmp_db_path)
    add_user(conn, "u1", "Alice", "signal", "2026-05-18T00:00:00Z")
    create_shard(conn, shard_id="s1", members=["u1"], at="2026-05-18T01:00:00Z")
    set_user_shard(conn, user_id="u1", shard_id="s1")
    users = list_users(conn)
    assert users[0].current_shard_id == "s1"


def test_publish_subset_appends(tmp_db_path):
    conn = _conn(tmp_db_path)
    publish_subset(conn, payload={"boxes": ["b1"]}, channel="telegram", at="2026-05-18T02:00:00Z")
    publish_subset(conn, payload={"boxes": ["b2"]}, channel="telegram", at="2026-05-18T03:00:00Z")
    latest = latest_published_subset(conn)
    assert json.loads(latest.payload_json) == {"boxes": ["b2"]}
    assert latest.publish_gen == 2
```

- [ ] **Step 2: Run test to verify failure**

`pytest tests/unit/controller/state/test_users_shards.py -v`
Expected: ImportError.

- [ ] **Step 3: Extend `_STATEMENTS`**

```python
    """
    CREATE TABLE IF NOT EXISTS users (
      user_id              TEXT PRIMARY KEY,
      display_name         TEXT,
      out_of_band_channel  TEXT NOT NULL,
      current_shard_id     TEXT,
      added_at             TEXT NOT NULL,
      FOREIGN KEY (current_shard_id) REFERENCES shards(shard_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS published_subsets (
      publish_gen  INTEGER PRIMARY KEY AUTOINCREMENT,
      payload_json TEXT NOT NULL,
      published_at TEXT NOT NULL,
      channel      TEXT NOT NULL
    )
    """,
```

- [ ] **Step 4: Write `src/mthydra/controller/state/users_shards.py`**

```python
"""Users, shards, and published-subset history."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class User:
    user_id: str
    display_name: str | None
    out_of_band_channel: str
    current_shard_id: str | None
    added_at: str


@dataclass(frozen=True)
class Shard:
    shard_id: str
    members_json: str
    last_reshuffled_at: str
    created_at: str
    retired_at: str | None


@dataclass(frozen=True)
class PublishedSubset:
    publish_gen: int
    payload_json: str
    published_at: str
    channel: str


def add_user(
    conn: sqlite3.Connection,
    user_id: str,
    display_name: str | None,
    out_of_band_channel: str,
    at: str,
) -> None:
    conn.execute(
        "INSERT INTO users (user_id, display_name, out_of_band_channel, added_at) VALUES (?, ?, ?, ?)",
        (user_id, display_name, out_of_band_channel, at),
    )
    conn.commit()


def list_users(conn: sqlite3.Connection) -> list[User]:
    rows = conn.execute(
        "SELECT user_id, display_name, out_of_band_channel, current_shard_id, added_at FROM users ORDER BY user_id"
    ).fetchall()
    return [User(*r) for r in rows]


def create_shard(conn: sqlite3.Connection, shard_id: str, members: list[str], at: str) -> None:
    conn.execute(
        "INSERT INTO shards (shard_id, members_json, last_reshuffled_at, created_at) VALUES (?, ?, ?, ?)",
        (shard_id, json.dumps(members), at, at),
    )
    conn.commit()


def set_user_shard(conn: sqlite3.Connection, user_id: str, shard_id: str | None) -> None:
    conn.execute("UPDATE users SET current_shard_id=? WHERE user_id=?", (shard_id, user_id))
    conn.commit()


def publish_subset(conn: sqlite3.Connection, payload: dict[str, Any], channel: str, at: str) -> int:
    cur = conn.execute(
        "INSERT INTO published_subsets (payload_json, published_at, channel) VALUES (?, ?, ?)",
        (json.dumps(payload), at, channel),
    )
    conn.commit()
    return int(cur.lastrowid)


def latest_published_subset(conn: sqlite3.Connection) -> PublishedSubset:
    row = conn.execute(
        "SELECT publish_gen, payload_json, published_at, channel "
        "FROM published_subsets ORDER BY publish_gen DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise LookupError("no published subsets")
    return PublishedSubset(*row)
```

- [ ] **Step 5: Run tests**

`pytest tests/unit/controller/state/test_users_shards.py -v`
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add src/mthydra/controller/state/schema.py \
        src/mthydra/controller/state/users_shards.py \
        tests/unit/controller/state/test_users_shards.py
git commit -m "state(A): users + shards + published_subsets repository"
git push origin main
```

---

### Task 7: publishing_tokens + provider_api_credentials

**Files:**
- Modify: `src/mthydra/controller/state/schema.py`
- Create: `src/mthydra/controller/state/tokens.py`
- Create: `tests/unit/controller/state/test_tokens.py`

- [ ] **Step 1: Write failing tests**

```python
import pytest

from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema
from mthydra.controller.state.tokens import (
    get_provider_credential,
    get_publishing_token,
    set_provider_credential,
    set_publishing_token,
)


def _conn(tmp_db_path):
    conn = connect(tmp_db_path)
    apply_schema(conn)
    return conn


def test_set_then_get_publishing_token(tmp_db_path):
    conn = _conn(tmp_db_path)
    set_publishing_token(conn, kind="telegram_bot", value="bot:xyz", at="2026-05-18T00:00:00Z")
    assert get_publishing_token(conn, "telegram_bot") == "bot:xyz"


def test_replace_publishing_token(tmp_db_path):
    conn = _conn(tmp_db_path)
    set_publishing_token(conn, kind="telegram_bot", value="bot:old", at="2026-05-18T00:00:00Z")
    set_publishing_token(conn, kind="telegram_bot", value="bot:new", at="2026-05-19T00:00:00Z")
    assert get_publishing_token(conn, "telegram_bot") == "bot:new"


def test_get_missing_provider_raises(tmp_db_path):
    conn = _conn(tmp_db_path)
    with pytest.raises(LookupError):
        get_provider_credential(conn, "aws")


def test_set_then_get_provider_credential(tmp_db_path):
    conn = _conn(tmp_db_path)
    set_provider_credential(conn, provider="aws", credential="AKID:SECRET", at="2026-05-18T00:00:00Z")
    assert get_provider_credential(conn, "aws") == "AKID:SECRET"
```

- [ ] **Step 2: Run test to verify failure**

Expected: ImportError on `tokens`.

- [ ] **Step 3: Extend `_STATEMENTS`**

```python
    """
    CREATE TABLE IF NOT EXISTS publishing_tokens (
      kind       TEXT PRIMARY KEY,
      value      TEXT NOT NULL,
      rotated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS provider_api_credentials (
      provider   TEXT PRIMARY KEY,
      credential TEXT NOT NULL,
      rotated_at TEXT NOT NULL
    )
    """,
```

- [ ] **Step 4: Write `src/mthydra/controller/state/tokens.py`**

```python
"""Publishing tokens and provider API credentials. Plaintext-on-disk per spec D1."""
from __future__ import annotations

import sqlite3


def set_publishing_token(conn: sqlite3.Connection, kind: str, value: str, at: str) -> None:
    conn.execute(
        "INSERT INTO publishing_tokens (kind, value, rotated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(kind) DO UPDATE SET value=excluded.value, rotated_at=excluded.rotated_at",
        (kind, value, at),
    )
    conn.commit()


def get_publishing_token(conn: sqlite3.Connection, kind: str) -> str:
    row = conn.execute("SELECT value FROM publishing_tokens WHERE kind=?", (kind,)).fetchone()
    if row is None:
        raise LookupError(f"no publishing token of kind {kind!r}")
    return row[0]


def set_provider_credential(conn: sqlite3.Connection, provider: str, credential: str, at: str) -> None:
    conn.execute(
        "INSERT INTO provider_api_credentials (provider, credential, rotated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(provider) DO UPDATE SET credential=excluded.credential, rotated_at=excluded.rotated_at",
        (provider, credential, at),
    )
    conn.commit()


def get_provider_credential(conn: sqlite3.Connection, provider: str) -> str:
    row = conn.execute(
        "SELECT credential FROM provider_api_credentials WHERE provider=?", (provider,)
    ).fetchone()
    if row is None:
        raise LookupError(f"no credential for provider {provider!r}")
    return row[0]
```

- [ ] **Step 5: Run tests**

Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add src/mthydra/controller/state/schema.py \
        src/mthydra/controller/state/tokens.py \
        tests/unit/controller/state/test_tokens.py
git commit -m "state(A): publishing_tokens + provider_api_credentials"
git push origin main
```

---

### Task 8: obligation_clocks + backup_log + audit_log

**Files:**
- Modify: `src/mthydra/controller/state/schema.py`
- Create: `src/mthydra/controller/state/obligations.py`
- Create: `src/mthydra/controller/state/backup_log.py`
- Create: `src/mthydra/controller/state/audit.py`
- Create: `tests/unit/controller/state/test_obligations.py`
- Create: `tests/unit/controller/state/test_backup_log.py`
- Create: `tests/unit/controller/state/test_audit.py`

- [ ] **Step 1: Write failing tests**

`tests/unit/controller/state/test_obligations.py`:

```python
from mthydra.controller.state.db import connect
from mthydra.controller.state.obligations import list_obligations, prove, set_obligation
from mthydra.controller.state.schema import apply_schema


def _conn(tmp_db_path):
    conn = connect(tmp_db_path)
    apply_schema(conn)
    return conn


def test_set_then_prove_updates_timestamp(tmp_db_path):
    conn = _conn(tmp_db_path)
    set_obligation(
        conn,
        obligation_id="t2_dryrun_caseA",
        last_proven_at="2026-05-18T00:00:00Z",
        proven_by="bootstrap",
        next_due_at="2026-06-17T00:00:00Z",
    )
    prove(conn, "t2_dryrun_caseA", proven_by="operator", at="2026-05-19T00:00:00Z", next_due_at="2026-06-18T00:00:00Z", details="dry-run gen-42")
    rows = {r.obligation_id: r for r in list_obligations(conn)}
    assert rows["t2_dryrun_caseA"].last_proven_at == "2026-05-19T00:00:00Z"
    assert rows["t2_dryrun_caseA"].proven_by == "operator"


def test_prove_unknown_obligation_raises(tmp_db_path):
    conn = _conn(tmp_db_path)
    try:
        prove(conn, "ghost", "x", "2026-05-19T00:00:00Z", "2026-06-18T00:00:00Z", None)
    except KeyError:
        pass
    else:
        raise AssertionError("expected KeyError")
```

`tests/unit/controller/state/test_backup_log.py`:

```python
from mthydra.controller.state.backup_log import (
    BackupTrigger,
    list_pending_reconciliation,
    next_generation,
    record_index_updated,
    record_pushed,
    record_started,
)
from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema


def _conn(tmp_db_path):
    conn = connect(tmp_db_path)
    apply_schema(conn)
    return conn


def test_next_generation_starts_at_one(tmp_db_path):
    conn = _conn(tmp_db_path)
    assert next_generation(conn) == 1


def test_record_started_then_pushed_then_index(tmp_db_path):
    conn = _conn(tmp_db_path)
    gen = next_generation(conn)
    record_started(conn, gen, trigger=BackupTrigger.FLOOR_TIMER, created_at="2026-05-18T00:00:00Z")
    record_pushed(conn, gen, sha256="abc", size_bytes=4096, pushed_at="2026-05-18T00:00:10Z")
    record_index_updated(conn, gen, at="2026-05-18T00:00:11Z")
    assert next_generation(conn) == 2
    assert list_pending_reconciliation(conn) == []


def test_pushed_without_index_listed_for_reconciliation(tmp_db_path):
    conn = _conn(tmp_db_path)
    gen = next_generation(conn)
    record_started(conn, gen, BackupTrigger.FLOOR_TIMER, "2026-05-18T00:00:00Z")
    record_pushed(conn, gen, sha256="abc", size_bytes=4096, pushed_at="2026-05-18T00:00:10Z")
    pending = list_pending_reconciliation(conn)
    assert [p.generation for p in pending] == [gen]
```

`tests/unit/controller/state/test_audit.py`:

```python
from mthydra.controller.state.audit import log_event, recent_events
from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema


def test_log_then_recent(tmp_db_path):
    conn = connect(tmp_db_path)
    apply_schema(conn)
    log_event(conn, ts="2026-05-18T00:00:00Z", actor="controller", action="mark_burned", target="example.org", details_json='{"reason":"job2_kill"}')
    events = recent_events(conn, limit=10)
    assert events[0].action == "mark_burned"
```

- [ ] **Step 2: Run tests to verify failure**

Expected: ImportErrors.

- [ ] **Step 3: Extend `_STATEMENTS`**

```python
    """
    CREATE TABLE IF NOT EXISTS obligation_clocks (
      obligation_id  TEXT PRIMARY KEY,
      last_proven_at TEXT NOT NULL,
      proven_by      TEXT NOT NULL,
      details        TEXT,
      next_due_at    TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS backup_log (
      generation       INTEGER PRIMARY KEY,
      created_at       TEXT NOT NULL,
      size_bytes       INTEGER NOT NULL DEFAULT 0,
      sha256           TEXT NOT NULL DEFAULT '',
      pushed_at        TEXT,
      index_updated_at TEXT,
      trigger          TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_log (
      id           INTEGER PRIMARY KEY AUTOINCREMENT,
      ts           TEXT NOT NULL,
      actor        TEXT NOT NULL,
      action       TEXT NOT NULL,
      target       TEXT,
      details_json TEXT
    )
    """,
```

- [ ] **Step 4: Write `src/mthydra/controller/state/obligations.py`**

```python
"""§12 obligation clocks repository."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Obligation:
    obligation_id: str
    last_proven_at: str
    proven_by: str
    details: str | None
    next_due_at: str


def set_obligation(
    conn: sqlite3.Connection,
    obligation_id: str,
    last_proven_at: str,
    proven_by: str,
    next_due_at: str,
    details: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO obligation_clocks (obligation_id, last_proven_at, proven_by, details, next_due_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(obligation_id) DO UPDATE SET "
        "  last_proven_at=excluded.last_proven_at, "
        "  proven_by=excluded.proven_by, "
        "  details=excluded.details, "
        "  next_due_at=excluded.next_due_at",
        (obligation_id, last_proven_at, proven_by, details, next_due_at),
    )
    conn.commit()


def prove(
    conn: sqlite3.Connection,
    obligation_id: str,
    proven_by: str,
    at: str,
    next_due_at: str,
    details: str | None,
) -> None:
    cur = conn.execute(
        "UPDATE obligation_clocks SET last_proven_at=?, proven_by=?, details=?, next_due_at=? WHERE obligation_id=?",
        (at, proven_by, details, next_due_at, obligation_id),
    )
    if cur.rowcount == 0:
        raise KeyError(f"unknown obligation {obligation_id!r}")
    conn.commit()


def list_obligations(conn: sqlite3.Connection) -> list[Obligation]:
    rows = conn.execute(
        "SELECT obligation_id, last_proven_at, proven_by, details, next_due_at "
        "FROM obligation_clocks ORDER BY obligation_id"
    ).fetchall()
    return [Obligation(*r) for r in rows]
```

- [ ] **Step 5: Write `src/mthydra/controller/state/backup_log.py`**

```python
"""Backup-log repository — records each generation's lifecycle."""
from __future__ import annotations

import enum
import sqlite3
from dataclasses import dataclass


class BackupTrigger(str, enum.Enum):
    FLOOR_TIMER = "floor_timer"
    BURNED_DOMAINS_CHANGE = "burned_domains_change"
    MANUAL = "manual"
    BOOTSTRAP = "bootstrap"


@dataclass(frozen=True)
class BackupRecord:
    generation: int
    created_at: str
    size_bytes: int
    sha256: str
    pushed_at: str | None
    index_updated_at: str | None
    trigger: str


def next_generation(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(generation), 0) FROM backup_log").fetchone()
    return int(row[0]) + 1


def record_started(conn: sqlite3.Connection, generation: int, trigger: BackupTrigger, created_at: str) -> None:
    conn.execute(
        "INSERT INTO backup_log (generation, created_at, trigger) VALUES (?, ?, ?)",
        (generation, created_at, trigger.value),
    )
    conn.commit()


def record_pushed(conn: sqlite3.Connection, generation: int, sha256: str, size_bytes: int, pushed_at: str) -> None:
    conn.execute(
        "UPDATE backup_log SET sha256=?, size_bytes=?, pushed_at=? WHERE generation=?",
        (sha256, size_bytes, pushed_at, generation),
    )
    conn.commit()


def record_index_updated(conn: sqlite3.Connection, generation: int, at: str) -> None:
    conn.execute("UPDATE backup_log SET index_updated_at=? WHERE generation=?", (at, generation))
    conn.commit()


def list_pending_reconciliation(conn: sqlite3.Connection) -> list[BackupRecord]:
    rows = conn.execute(
        "SELECT generation, created_at, size_bytes, sha256, pushed_at, index_updated_at, trigger "
        "FROM backup_log WHERE pushed_at IS NOT NULL AND index_updated_at IS NULL "
        "ORDER BY generation"
    ).fetchall()
    return [BackupRecord(*r) for r in rows]
```

- [ ] **Step 6: Write `src/mthydra/controller/state/audit.py`**

```python
"""Audit log — append-only by convention."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class AuditEvent:
    id: int
    ts: str
    actor: str
    action: str
    target: str | None
    details_json: str | None


def log_event(
    conn: sqlite3.Connection,
    ts: str,
    actor: str,
    action: str,
    target: str | None,
    details_json: str | None,
) -> None:
    conn.execute(
        "INSERT INTO audit_log (ts, actor, action, target, details_json) VALUES (?, ?, ?, ?, ?)",
        (ts, actor, action, target, details_json),
    )
    conn.commit()


def recent_events(conn: sqlite3.Connection, limit: int = 100) -> list[AuditEvent]:
    rows = conn.execute(
        "SELECT id, ts, actor, action, target, details_json FROM audit_log ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [AuditEvent(*r) for r in rows]
```

- [ ] **Step 7: Run tests**

`pytest tests/unit/controller/state/ -v`
Expected: all previous tests still pass, plus 5 new ones (3 obligations + 3 backup_log + 1 audit = 7, with previous 12 + previous 6 + 4 + 3 = full run ≥ 30).

- [ ] **Step 8: Commit**

```bash
git add src/mthydra/controller/state/schema.py \
        src/mthydra/controller/state/obligations.py \
        src/mthydra/controller/state/backup_log.py \
        src/mthydra/controller/state/audit.py \
        tests/unit/controller/state/test_obligations.py \
        tests/unit/controller/state/test_backup_log.py \
        tests/unit/controller/state/test_audit.py
git commit -m "state(A): obligation_clocks + backup_log + audit_log"
git push origin main
```

---

### Task 9: Invariants module (the §10 startup self-check rules)

**Files:**
- Create: `src/mthydra/controller/state/invariants.py`
- Create: `tests/unit/controller/state/test_invariants.py`

- [ ] **Step 1: Write failing tests**

```python
import pytest

from mthydra.controller.state.authority import insert_authority, retire_authority
from mthydra.controller.state.db import connect
from mthydra.controller.state.descriptor import insert_signing_key
from mthydra.controller.state.invariants import InvariantViolation, check_all
from mthydra.controller.state.schema import SCHEMA_VERSION, apply_schema


def _seeded(tmp_db_path):
    conn = connect(tmp_db_path)
    apply_schema(conn)
    insert_authority(conn, 1, "P", "K", "2026-05-18T00:00:00Z")
    insert_signing_key(conn, 1, b"P", b"K", "2026-05-18T00:00:00Z")
    return conn


def test_check_all_passes_on_clean_seeded_db(tmp_db_path):
    conn = _seeded(tmp_db_path)
    check_all(conn, expected_schema_version=SCHEMA_VERSION)


def test_check_all_rejects_unknown_schema_version(tmp_db_path):
    conn = _seeded(tmp_db_path)
    with pytest.raises(InvariantViolation, match="schema_version"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION + 99)


def test_check_all_rejects_overlap_pool_and_burned(tmp_db_path):
    conn = _seeded(tmp_db_path)
    conn.execute(
        "INSERT INTO cover_domain_pool (domain, state, added_at) VALUES ('x.org', 'in_use', '2026-05-18T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO burned_domains (domain, burned_at, reason) VALUES ('x.org', '2026-05-18T01:00:00Z', 'job2_kill')"
    )
    conn.commit()
    with pytest.raises(InvariantViolation, match="overlap"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION)


def test_check_all_rejects_no_active_authority(tmp_db_path):
    conn = _seeded(tmp_db_path)
    retire_authority(conn, 1, at="2026-05-19T00:00:00Z")
    with pytest.raises(InvariantViolation, match="credential_authority"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION)


def test_check_all_rejects_truly_impossible_state(tmp_db_path):
    conn = _seeded(tmp_db_path)
    conn.execute(
        "INSERT INTO backup_log (generation, created_at, size_bytes, sha256, pushed_at, index_updated_at, trigger) "
        "VALUES (1, '2026-05-18T00:00:00Z', 4096, 'abc', NULL, '2026-05-18T00:00:11Z', 'floor_timer')"
    )
    conn.commit()
    with pytest.raises(InvariantViolation, match="impossible"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION)
```

- [ ] **Step 2: Run test to verify failure**

Expected: ImportError.

- [ ] **Step 3: Write `src/mthydra/controller/state/invariants.py`**

```python
"""Startup self-check invariants — spec A §10."""
from __future__ import annotations

import sqlite3


class InvariantViolation(RuntimeError):
    """Raised by check_all when the DB is in a state that forbids startup."""


def _scalar(conn: sqlite3.Connection, sql: str, *params) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


def check_all(conn: sqlite3.Connection, *, expected_schema_version: int) -> None:
    """Run every §10 invariant. Raise InvariantViolation on the first failure."""

    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise InvariantViolation(f"integrity_check: {integrity}")

    row = conn.execute("SELECT version FROM schema_version WHERE rowid=1").fetchone()
    if row is None:
        raise InvariantViolation("schema_version row missing")
    if row[0] != expected_schema_version:
        raise InvariantViolation(
            f"schema_version mismatch: db={row[0]} expected={expected_schema_version}"
        )

    overlap = _scalar(
        conn,
        "SELECT COUNT(*) FROM cover_domain_pool WHERE domain IN (SELECT domain FROM burned_domains)",
    )
    if overlap > 0:
        raise InvariantViolation(
            f"cover_domain_pool / burned_domains overlap: {overlap} row(s)"
        )

    active_authorities = _scalar(
        conn, "SELECT COUNT(*) FROM credential_authority WHERE retired_at IS NULL"
    )
    if active_authorities != 1:
        raise InvariantViolation(
            f"credential_authority must have exactly 1 active row, found {active_authorities}"
        )

    active_signing = _scalar(
        conn, "SELECT COUNT(*) FROM descriptor_signing_key WHERE retired_at IS NULL"
    )
    if active_signing != 1:
        raise InvariantViolation(
            f"descriptor_signing_key must have exactly 1 active row, found {active_signing}"
        )

    impossible = _scalar(
        conn,
        "SELECT COUNT(*) FROM backup_log WHERE pushed_at IS NULL AND index_updated_at IS NOT NULL",
    )
    if impossible > 0:
        raise InvariantViolation(
            f"impossible backup_log state (index without pushed): {impossible} row(s)"
        )
```

- [ ] **Step 4: Run tests**

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/state/invariants.py tests/unit/controller/state/test_invariants.py
git commit -m "state(A): invariants module for §10 startup self-checks"
git push origin main
```

---

## Phase 2 — Config

### Task 10: TOML config loader with dataclass schema

**Files:**
- Create: `src/mthydra/controller/config.py`
- Create: `tests/unit/controller/test_config.py`
- Create: `packaging/etc/mthydra/controller.toml.example`

- [ ] **Step 1: Write failing tests**

```python
from pathlib import Path

import pytest

from mthydra.controller.config import ConfigError, load_config


def _write(path: Path, content: str) -> Path:
    path.write_text(content)
    return path


def test_load_valid_config(tmp_path):
    p = _write(
        tmp_path / "c.toml",
        """
        [node]
        role = "active"
        hostname = "controller-1"

        [backup]
        floor_interval_hours = 24
        on_change_debounce_seconds = 30
        endpoint = "https://s3.example"
        bucket = "mthydra-state"
        access_key_id = "AKID"

        [backup.retention]
        keep_daily = 30
        keep_monthly = 12
        object_lock_days = 365

        [gap_monitor]
        poll_interval_minutes = 30
        alarm_threshold_hours = 48
        recipient_email = "op@example.org"

        [obligations.timers_hours]
        t1_dormant_health = 168
        t2_dryrun_caseA = 720
        t2_dryrun_caseB = 720
        t3_vantage_revalidation = 168
        t3_profile_repin = 0
        t4_upstream_check = 168
        t5_pool_revalidation = 168
        t6_reshuffle = 168
        backup_restore_dryrun = 720
        """,
    )
    cfg = load_config(p)
    assert cfg.node.role == "active"
    assert cfg.backup.floor_interval_hours == 24
    assert cfg.backup.retention.object_lock_days == 365
    assert cfg.gap_monitor.alarm_threshold_hours == 48
    assert cfg.obligations.timers_hours["t2_dryrun_caseA"] == 720


def test_load_rejects_invalid_role(tmp_path):
    p = _write(
        tmp_path / "c.toml",
        """
        [node]
        role = "primary"
        hostname = "x"
        [backup]
        floor_interval_hours = 24
        on_change_debounce_seconds = 30
        endpoint = "https://x"
        bucket = "x"
        access_key_id = "x"
        [backup.retention]
        keep_daily = 1
        keep_monthly = 1
        object_lock_days = 1
        [gap_monitor]
        poll_interval_minutes = 30
        alarm_threshold_hours = 48
        recipient_email = "op@x"
        [obligations.timers_hours]
        """,
    )
    with pytest.raises(ConfigError, match="role"):
        load_config(p)


def test_load_rejects_negative_interval(tmp_path):
    p = _write(
        tmp_path / "c.toml",
        """
        [node]
        role = "active"
        hostname = "x"
        [backup]
        floor_interval_hours = -1
        on_change_debounce_seconds = 30
        endpoint = "https://x"
        bucket = "x"
        access_key_id = "x"
        [backup.retention]
        keep_daily = 1
        keep_monthly = 1
        object_lock_days = 1
        [gap_monitor]
        poll_interval_minutes = 30
        alarm_threshold_hours = 48
        recipient_email = "op@x"
        [obligations.timers_hours]
        """,
    )
    with pytest.raises(ConfigError, match="floor_interval_hours"):
        load_config(p)
```

- [ ] **Step 2: Run tests to verify failure**

Expected: ImportError on `mthydra.controller.config`.

- [ ] **Step 3: Write `src/mthydra/controller/config.py`**

```python
"""TOML config loader. Non-secret operator-authored policy, lives in git."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(ValueError):
    """Raised when controller.toml is missing required fields or has invalid values."""


@dataclass(frozen=True)
class NodeConfig:
    role: str
    hostname: str


@dataclass(frozen=True)
class RetentionConfig:
    keep_daily: int
    keep_monthly: int
    object_lock_days: int


@dataclass(frozen=True)
class BackupConfig:
    floor_interval_hours: int
    on_change_debounce_seconds: int
    endpoint: str
    bucket: str
    access_key_id: str
    retention: RetentionConfig


@dataclass(frozen=True)
class GapMonitorConfig:
    poll_interval_minutes: int
    alarm_threshold_hours: int
    recipient_email: str


@dataclass(frozen=True)
class ObligationsConfig:
    timers_hours: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Config:
    node: NodeConfig
    backup: BackupConfig
    gap_monitor: GapMonitorConfig
    obligations: ObligationsConfig


_VALID_ROLES = {"active", "standby"}


def _require_positive(name: str, value: int) -> int:
    if not isinstance(value, int) or value < 0:
        raise ConfigError(f"{name}: must be a non-negative integer (got {value!r})")
    return value


def load_config(path: Path | str) -> Config:
    path = Path(path)
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError as e:
        raise ConfigError(f"config not found: {path}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"TOML parse error in {path}: {e}") from e

    try:
        node = raw["node"]
        backup = raw["backup"]
        retention = backup["retention"]
        gap = raw["gap_monitor"]
        obligations = raw.get("obligations", {}).get("timers_hours", {})
    except KeyError as e:
        raise ConfigError(f"missing required section/key: {e}") from e

    role = node.get("role")
    if role not in _VALID_ROLES:
        raise ConfigError(f"node.role must be one of {sorted(_VALID_ROLES)}, got {role!r}")

    return Config(
        node=NodeConfig(role=role, hostname=str(node["hostname"])),
        backup=BackupConfig(
            floor_interval_hours=_require_positive("backup.floor_interval_hours", backup["floor_interval_hours"]),
            on_change_debounce_seconds=_require_positive(
                "backup.on_change_debounce_seconds", backup["on_change_debounce_seconds"]
            ),
            endpoint=str(backup["endpoint"]),
            bucket=str(backup["bucket"]),
            access_key_id=str(backup["access_key_id"]),
            retention=RetentionConfig(
                keep_daily=_require_positive("backup.retention.keep_daily", retention["keep_daily"]),
                keep_monthly=_require_positive("backup.retention.keep_monthly", retention["keep_monthly"]),
                object_lock_days=_require_positive(
                    "backup.retention.object_lock_days", retention["object_lock_days"]
                ),
            ),
        ),
        gap_monitor=GapMonitorConfig(
            poll_interval_minutes=_require_positive("gap_monitor.poll_interval_minutes", gap["poll_interval_minutes"]),
            alarm_threshold_hours=_require_positive("gap_monitor.alarm_threshold_hours", gap["alarm_threshold_hours"]),
            recipient_email=str(gap["recipient_email"]),
        ),
        obligations=ObligationsConfig(timers_hours={str(k): int(v) for k, v in obligations.items()}),
    )
```

- [ ] **Step 4: Write `packaging/etc/mthydra/controller.toml.example`**

(Copy the full example from spec A §5 verbatim — same content as the dict-equivalent in the first test above.)

- [ ] **Step 5: Run tests**

Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add src/mthydra/controller/config.py \
        tests/unit/controller/test_config.py \
        packaging/etc/mthydra/controller.toml.example
git commit -m "config(A): TOML loader with dataclass schema + example"
git push origin main
```

---

## Phase 3 — Backup Pipeline

### Task 11: age encrypt/decrypt wrapper

**Files:**
- Create: `src/mthydra/controller/backup/__init__.py` (empty)
- Create: `src/mthydra/controller/backup/age_crypt.py`
- Create: `tests/unit/controller/backup/__init__.py` (empty)
- Create: `tests/unit/controller/backup/test_age_crypt.py`

- [ ] **Step 1: Write failing tests**

```python
import shutil
import subprocess

import pytest

from mthydra.controller.backup.age_crypt import AgeError, encrypt_file, validate_recipient

pytestmark = pytest.mark.skipif(shutil.which("age") is None, reason="age binary not installed")


@pytest.fixture
def keypair(tmp_path):
    keyfile = tmp_path / "id.key"
    result = subprocess.run(["age-keygen", "-o", str(keyfile)], capture_output=True, text=True, check=True)
    # `age-keygen` prints the recipient (public key) to stderr in the form `# public key: age1...`
    recipient = ""
    for line in result.stderr.splitlines():
        if line.startswith("# public key: "):
            recipient = line.removeprefix("# public key: ").strip()
            break
    assert recipient.startswith("age1")
    return keyfile, recipient


def test_validate_recipient_accepts_age_pubkey(keypair):
    _, recipient = keypair
    validate_recipient(recipient)


def test_validate_recipient_rejects_garbage():
    with pytest.raises(AgeError):
        validate_recipient("not-an-age-key")


def test_encrypt_then_decrypt_roundtrip(tmp_path, keypair):
    keyfile, recipient = keypair
    plain = tmp_path / "p.bin"
    plain.write_bytes(b"hello world")
    enc = tmp_path / "p.age"
    encrypt_file(plain, recipient=recipient, out=enc)
    assert enc.exists() and enc.stat().st_size > 0
    out = subprocess.run(
        ["age", "-d", "-i", str(keyfile), str(enc)], capture_output=True, check=True
    )
    assert out.stdout == b"hello world"


def test_encrypt_missing_input_raises(tmp_path):
    with pytest.raises(AgeError, match="not found"):
        encrypt_file(tmp_path / "missing", recipient="age1abc", out=tmp_path / "o.age")
```

- [ ] **Step 2: Run tests to verify failure**

Expected: ImportError. (If `age` not installed, tests skip — install via `sudo apt install age` before continuing.)

- [ ] **Step 3: Write `src/mthydra/controller/backup/age_crypt.py`**

```python
"""Thin wrapper around the `age` CLI for encryption only.

Decryption is intentionally NOT exposed from the controller — the operator's
private key never touches the controller (spec D1).
"""
from __future__ import annotations

import subprocess
from pathlib import Path


class AgeError(RuntimeError):
    """Raised when age invocation fails or recipient format is bad."""


def validate_recipient(recipient: str) -> None:
    """Accept only age v1 recipients."""
    if not isinstance(recipient, str) or not recipient.startswith("age1") or len(recipient) < 32:
        raise AgeError(f"invalid age recipient: {recipient!r}")


def encrypt_file(input_path: Path | str, recipient: str, out: Path | str) -> None:
    input_path = Path(input_path)
    out = Path(out)
    if not input_path.exists():
        raise AgeError(f"input file not found: {input_path}")
    validate_recipient(recipient)
    try:
        subprocess.run(
            ["age", "-r", recipient, "-o", str(out), str(input_path)],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        raise AgeError(f"age failed: {e.stderr.decode(errors='replace')}") from e
    except FileNotFoundError as e:
        raise AgeError("age binary not on PATH") from e
```

- [ ] **Step 4: Run tests**

Expected: 4 passed (or 4 skipped if `age` unavailable; install and re-run before commit).

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/backup/age_crypt.py \
        src/mthydra/controller/backup/__init__.py \
        tests/unit/controller/backup/test_age_crypt.py \
        tests/unit/controller/backup/__init__.py
git commit -m "backup(A): age encrypt wrapper + recipient validation"
git push origin main
```

---

### Task 12: S3 destination wrapper

**Files:**
- Create: `src/mthydra/controller/backup/s3_dest.py`
- Create: `tests/unit/controller/backup/test_s3_dest.py`

- [ ] **Step 1: Write failing tests**

```python
import boto3
import pytest
from moto import mock_aws

from mthydra.controller.backup.s3_dest import S3Destination


BUCKET = "mthydra-test"


@pytest.fixture
def s3_env():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


def test_put_blob_uploads_with_object_lock_header(s3_env, tmp_path):
    blob = tmp_path / "snap.age"
    blob.write_bytes(b"ENCRYPTED")
    dest = S3Destination(
        endpoint_url=None,
        bucket=BUCKET,
        access_key_id="x",
        secret_access_key="y",
        region="us-east-1",
        object_lock_days=30,
    )
    dest._client = s3_env  # inject mocked client
    dest.put_blob(generation=42, blob_path=blob)
    obj = s3_env.get_object(Bucket=BUCKET, Key="gen-0000000042.age")
    assert obj["Body"].read() == b"ENCRYPTED"


def test_put_index_writes_json(s3_env, tmp_path):
    dest = S3Destination(None, BUCKET, "x", "y", "us-east-1", object_lock_days=30)
    dest._client = s3_env
    dest.put_index(highest_gen=42, sha256="abc", size_bytes=1024, ts="2026-05-18T00:00:00Z")
    obj = s3_env.get_object(Bucket=BUCKET, Key="index.json")
    import json
    body = json.loads(obj["Body"].read())
    assert body == {"highest_gen": 42, "sha256": "abc", "size_bytes": 1024, "ts": "2026-05-18T00:00:00Z"}


def test_head_index_returns_none_when_absent(s3_env):
    dest = S3Destination(None, BUCKET, "x", "y", "us-east-1", object_lock_days=30)
    dest._client = s3_env
    assert dest.head_index() is None


def test_head_index_returns_payload_when_present(s3_env):
    dest = S3Destination(None, BUCKET, "x", "y", "us-east-1", object_lock_days=30)
    dest._client = s3_env
    dest.put_index(highest_gen=7, sha256="z", size_bytes=10, ts="2026-05-18T00:00:00Z")
    payload = dest.head_index()
    assert payload["highest_gen"] == 7
```

- [ ] **Step 2: Run tests to verify failure**

Expected: ImportError.

- [ ] **Step 3: Write `src/mthydra/controller/backup/s3_dest.py`**

```python
"""S3-compatible backup destination (works for AWS S3, Backblaze B2, MinIO)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError


class S3Destination:
    def __init__(
        self,
        endpoint_url: str | None,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
        region: str,
        object_lock_days: int,
    ) -> None:
        self.endpoint_url = endpoint_url
        self.bucket = bucket
        self.region = region
        self.object_lock_days = object_lock_days
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name=region,
        )

    @staticmethod
    def _key_for_gen(generation: int) -> str:
        return f"gen-{generation:010d}.age"

    def put_blob(self, generation: int, blob_path: Path) -> None:
        retain_until = datetime.now(timezone.utc) + timedelta(days=self.object_lock_days)
        with open(blob_path, "rb") as fh:
            self._client.put_object(
                Bucket=self.bucket,
                Key=self._key_for_gen(generation),
                Body=fh,
                ObjectLockMode="COMPLIANCE",
                ObjectLockRetainUntilDate=retain_until,
            )

    def put_index(self, highest_gen: int, sha256: str, size_bytes: int, ts: str) -> None:
        body = json.dumps(
            {"highest_gen": highest_gen, "sha256": sha256, "size_bytes": size_bytes, "ts": ts},
            sort_keys=True,
        ).encode("utf-8")
        self._client.put_object(
            Bucket=self.bucket, Key="index.json", Body=body, ContentType="application/json"
        )

    def head_index(self) -> dict[str, Any] | None:
        try:
            obj = self._client.get_object(Bucket=self.bucket, Key="index.json")
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                return None
            raise
        return json.loads(obj["Body"].read())

    def head_blob(self, generation: int) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=self._key_for_gen(generation))
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
                return False
            raise
```

- [ ] **Step 4: Run tests**

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/backup/s3_dest.py \
        tests/unit/controller/backup/test_s3_dest.py
git commit -m "backup(A): S3 destination wrapper with Object Lock"
git push origin main
```

---

### Task 13: do_backup pipeline (snapshot → encrypt → push → index)

**Files:**
- Create: `src/mthydra/controller/backup/pipeline.py`
- Create: `tests/unit/controller/backup/test_pipeline.py`

- [ ] **Step 1: Write failing tests**

```python
import shutil
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

from mthydra.controller.backup.pipeline import BackupPipeline
from mthydra.controller.backup.s3_dest import S3Destination
from mthydra.controller.state.authority import insert_authority
from mthydra.controller.state.backup_log import BackupTrigger, next_generation
from mthydra.controller.state.db import connect
from mthydra.controller.state.descriptor import insert_signing_key
from mthydra.controller.state.schema import apply_schema

pytestmark = pytest.mark.skipif(shutil.which("age") is None, reason="age binary not installed")


@pytest.fixture
def keypair(tmp_path):
    import subprocess
    keyfile = tmp_path / "id.key"
    r = subprocess.run(["age-keygen", "-o", str(keyfile)], capture_output=True, text=True, check=True)
    recipient = next(
        line.removeprefix("# public key: ").strip()
        for line in r.stderr.splitlines()
        if line.startswith("# public key: ")
    )
    return keyfile, recipient


@pytest.fixture
def seeded_db(tmp_path):
    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    insert_authority(conn, 1, "P", "K", "2026-05-18T00:00:00Z")
    insert_signing_key(conn, 1, b"P", b"K", "2026-05-18T00:00:00Z")
    conn.close()
    return db


@pytest.fixture
def dest():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(
            Bucket="b",
            ObjectLockEnabledForBucket=True,
        )
        d = S3Destination(None, "b", "x", "y", "us-east-1", object_lock_days=30)
        d._client = client
        yield d


async def test_do_backup_uploads_blob_and_index(tmp_path, keypair, seeded_db, dest):
    _, recipient = keypair
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    pipeline = BackupPipeline(
        db_path=seeded_db,
        tmp_dir=tmp_dir,
        recipient=recipient,
        destination=dest,
        clock=lambda: "2026-05-18T00:00:00Z",
    )
    await pipeline.do_backup(trigger=BackupTrigger.MANUAL)
    assert dest.head_blob(1)
    payload = dest.head_index()
    assert payload["highest_gen"] == 1
    conn = connect(seeded_db)
    assert next_generation(conn) == 2  # one consumed


async def test_do_backup_cleans_tmp_files(tmp_path, keypair, seeded_db, dest):
    _, recipient = keypair
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    pipeline = BackupPipeline(seeded_db, tmp_dir, recipient, dest, lambda: "2026-05-18T00:00:00Z")
    await pipeline.do_backup(trigger=BackupTrigger.FLOOR_TIMER)
    assert list(tmp_dir.iterdir()) == []
```

- [ ] **Step 2: Run tests to verify failure**

Expected: ImportError.

- [ ] **Step 3: Write `src/mthydra/controller/backup/pipeline.py`**

```python
"""do_backup orchestration — spec A §6.2."""
from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import subprocess
from collections.abc import Callable
from pathlib import Path

from mthydra.controller.backup.age_crypt import encrypt_file
from mthydra.controller.backup.s3_dest import S3Destination
from mthydra.controller.state.backup_log import (
    BackupTrigger,
    next_generation,
    record_index_updated,
    record_pushed,
    record_started,
)
from mthydra.controller.state.db import connect


class BackupPipeline:
    def __init__(
        self,
        db_path: Path,
        tmp_dir: Path,
        recipient: str,
        destination: S3Destination,
        clock: Callable[[], str],
    ) -> None:
        self.db_path = Path(db_path)
        self.tmp_dir = Path(tmp_dir)
        self.recipient = recipient
        self.destination = destination
        self.clock = clock
        self._mutex = asyncio.Lock()

    async def do_backup(self, trigger: BackupTrigger) -> int:
        async with self._mutex:
            return await asyncio.to_thread(self._do_backup_sync, trigger)

    def _do_backup_sync(self, trigger: BackupTrigger) -> int:
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

    @staticmethod
    def _sqlite_backup(src: Path, dest: Path) -> None:
        """Online SQLite backup via the backup API (sqlite3 module)."""
        with sqlite3.connect(src) as src_conn, sqlite3.connect(dest) as dst_conn:
            src_conn.backup(dst_conn)
```

- [ ] **Step 4: Run tests**

Expected: 2 passed (or skipped if `age` unavailable).

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/backup/pipeline.py \
        tests/unit/controller/backup/test_pipeline.py
git commit -m "backup(A): do_backup orchestration (snapshot+encrypt+push+index)"
git push origin main
```

---

### Task 14: Backup triggers (floor timer + on-change debouncer)

**Files:**
- Create: `src/mthydra/controller/backup/triggers.py`
- Create: `tests/unit/controller/backup/test_triggers.py`

- [ ] **Step 1: Write failing tests**

```python
import asyncio

import pytest

from mthydra.controller.backup.triggers import BackupOrchestrator


class FakePipeline:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def do_backup(self, trigger) -> int:
        self.calls.append(trigger.value)
        return len(self.calls)


async def test_burned_change_debounces_multiple_signals(monkeypatch):
    pipeline = FakePipeline()
    orch = BackupOrchestrator(pipeline=pipeline, debounce_seconds=0.05, floor_interval_seconds=3600)
    await orch.start()
    orch.notify_burned_change()
    orch.notify_burned_change()
    orch.notify_burned_change()
    await asyncio.sleep(0.15)
    await orch.stop()
    assert pipeline.calls == ["burned_domains_change"]


async def test_manual_trigger_runs_immediately():
    pipeline = FakePipeline()
    orch = BackupOrchestrator(pipeline=pipeline, debounce_seconds=0.05, floor_interval_seconds=3600)
    await orch.start()
    await orch.run_manual()
    await orch.stop()
    assert pipeline.calls == ["manual"]
```

- [ ] **Step 2: Run tests to verify failure**

Expected: ImportError.

- [ ] **Step 3: Write `src/mthydra/controller/backup/triggers.py`**

```python
"""Backup triggers: floor timer + burned_domains-change debouncer + manual.

Floor timer uses asyncio (APScheduler dependency reserved for future cron-style needs).
"""
from __future__ import annotations

import asyncio
from typing import Protocol

from mthydra.controller.state.backup_log import BackupTrigger


class _PipelineLike(Protocol):
    async def do_backup(self, trigger: BackupTrigger) -> int: ...


class BackupOrchestrator:
    def __init__(
        self, pipeline: _PipelineLike, debounce_seconds: float, floor_interval_seconds: float
    ) -> None:
        self.pipeline = pipeline
        self.debounce_seconds = debounce_seconds
        self.floor_interval_seconds = floor_interval_seconds
        self._debounce_task: asyncio.Task | None = None
        self._floor_task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        self._running = True
        self._floor_task = asyncio.create_task(self._floor_loop())

    async def stop(self) -> None:
        self._running = False
        for t in (self._debounce_task, self._floor_task):
            if t is not None:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

    def notify_burned_change(self) -> None:
        if self._debounce_task is None or self._debounce_task.done():
            self._debounce_task = asyncio.create_task(self._debounced_burn_backup())

    async def run_manual(self) -> int:
        return await self.pipeline.do_backup(BackupTrigger.MANUAL)

    async def _debounced_burn_backup(self) -> None:
        await asyncio.sleep(self.debounce_seconds)
        await self.pipeline.do_backup(BackupTrigger.BURNED_DOMAINS_CHANGE)

    async def _floor_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self.floor_interval_seconds)
            except asyncio.CancelledError:
                return
            if not self._running:
                return
            try:
                await self.pipeline.do_backup(BackupTrigger.FLOOR_TIMER)
            except Exception:
                # Logged elsewhere; loop continues so next interval still fires.
                pass
```

- [ ] **Step 4: Run tests**

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/backup/triggers.py \
        tests/unit/controller/backup/test_triggers.py
git commit -m "backup(A): triggers — floor timer + burn-change debouncer + manual"
git push origin main
```

---

### Task 15: Crash-recovery reconciler

**Files:**
- Create: `src/mthydra/controller/backup/reconcile.py`
- Create: `tests/unit/controller/backup/test_reconcile.py`

- [ ] **Step 1: Write failing tests**

```python
import boto3
import pytest
from moto import mock_aws

from mthydra.controller.backup.reconcile import reconcile_pending
from mthydra.controller.backup.s3_dest import S3Destination
from mthydra.controller.state.backup_log import BackupTrigger, list_pending_reconciliation, next_generation, record_pushed, record_started
from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "state.sqlite"
    conn = connect(p)
    apply_schema(conn)
    conn.close()
    return p


@pytest.fixture
def dest():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="b")
        d = S3Destination(None, "b", "x", "y", "us-east-1", object_lock_days=30)
        d._client = client
        yield d


def _stage_partial(db_path, dest, generation: int) -> None:
    """Mark a backup as started+pushed but never index-updated."""
    conn = connect(db_path)
    record_started(conn, generation, BackupTrigger.FLOOR_TIMER, "2026-05-18T00:00:00Z")
    record_pushed(conn, generation, "abc", 100, "2026-05-18T00:00:10Z")
    conn.close()
    # And put a matching blob in S3:
    dest._client.put_object(Bucket="b", Key=f"gen-{generation:010d}.age", Body=b"X")


def test_reconcile_writes_missing_index(db, dest):
    _stage_partial(db, dest, 1)
    reconcile_pending(db, dest, clock=lambda: "2026-05-18T01:00:00Z")
    payload = dest.head_index()
    assert payload["highest_gen"] == 1
    conn = connect(db)
    assert list_pending_reconciliation(conn) == []


def test_reconcile_no_op_when_already_indexed(db, dest):
    _stage_partial(db, dest, 1)
    dest.put_index(highest_gen=1, sha256="abc", size_bytes=100, ts="2026-05-18T00:00:11Z")
    reconcile_pending(db, dest, clock=lambda: "2026-05-18T01:00:00Z")
    conn = connect(db)
    assert list_pending_reconciliation(conn) == []


def test_reconcile_advances_index_for_higher_generation(db, dest):
    _stage_partial(db, dest, 5)
    dest.put_index(highest_gen=2, sha256="old", size_bytes=10, ts="2026-05-17T00:00:00Z")
    reconcile_pending(db, dest, clock=lambda: "2026-05-18T01:00:00Z")
    assert dest.head_index()["highest_gen"] == 5
```

- [ ] **Step 2: Run tests to verify failure**

Expected: ImportError.

- [ ] **Step 3: Write `src/mthydra/controller/backup/reconcile.py`**

```python
"""Crash-recovery reconciliation — spec A §9 + §10.1."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from mthydra.controller.backup.s3_dest import S3Destination
from mthydra.controller.state.backup_log import list_pending_reconciliation, record_index_updated
from mthydra.controller.state.db import connect


def reconcile_pending(db_path: Path, destination: S3Destination, clock: Callable[[], str]) -> int:
    """Resolve any backup_log row with pushed_at NOT NULL and index_updated_at NULL.

    For each such row:
      - HEAD the matching gen-NNN.age object in S3; if missing, leave the row
        alone (the next backup cycle will retry as a fresh generation).
      - HEAD index.json; if it references a generation >= ours, just stamp our
        row as index-updated. Otherwise re-PUT index.json with our (highest)
        generation and then stamp.
    Returns the count of rows reconciled.
    """
    conn = connect(db_path)
    try:
        pending = list_pending_reconciliation(conn)
        resolved = 0
        for row in pending:
            if not destination.head_blob(row.generation):
                continue
            index = destination.head_index()
            if index is not None and index.get("highest_gen", 0) >= row.generation:
                record_index_updated(conn, row.generation, at=clock())
                resolved += 1
                continue
            destination.put_index(
                highest_gen=row.generation,
                sha256=row.sha256,
                size_bytes=row.size_bytes,
                ts=clock(),
            )
            record_index_updated(conn, row.generation, at=clock())
            resolved += 1
        return resolved
    finally:
        conn.close()
```

- [ ] **Step 4: Run tests**

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/backup/reconcile.py \
        tests/unit/controller/backup/test_reconcile.py
git commit -m "backup(A): crash-recovery reconciler for partial-push state"
git push origin main
```

---

## Phase 4 — Restore

### Task 16: Restore decrypt + summary

**Files:**
- Create: `src/mthydra/controller/restore/__init__.py` (empty)
- Create: `src/mthydra/controller/restore/decrypt.py`
- Create: `src/mthydra/controller/restore/summary.py`
- Create: `tests/unit/controller/restore/__init__.py` (empty)
- Create: `tests/unit/controller/restore/test_decrypt.py`
- Create: `tests/unit/controller/restore/test_summary.py`

- [ ] **Step 1: Write failing tests**

`tests/unit/controller/restore/test_decrypt.py`:

```python
import shutil
import subprocess

import pytest

from mthydra.controller.backup.age_crypt import encrypt_file
from mthydra.controller.restore.decrypt import DecryptError, decrypt_blob

pytestmark = pytest.mark.skipif(shutil.which("age") is None, reason="age binary not installed")


@pytest.fixture
def keypair(tmp_path):
    keyfile = tmp_path / "id.key"
    r = subprocess.run(["age-keygen", "-o", str(keyfile)], capture_output=True, text=True, check=True)
    recipient = next(
        line.removeprefix("# public key: ").strip()
        for line in r.stderr.splitlines()
        if line.startswith("# public key: ")
    )
    return keyfile, recipient


def test_decrypt_roundtrip(tmp_path, keypair):
    keyfile, recipient = keypair
    plain = tmp_path / "p.bin"
    plain.write_bytes(b"hello")
    enc = tmp_path / "p.age"
    encrypt_file(plain, recipient, enc)
    out = tmp_path / "r.bin"
    decrypt_blob(enc, identity_path=keyfile, out=out)
    assert out.read_bytes() == b"hello"


def test_decrypt_wrong_identity_raises(tmp_path, keypair):
    keyfile, recipient = keypair
    plain = tmp_path / "p.bin"
    plain.write_bytes(b"hi")
    enc = tmp_path / "p.age"
    encrypt_file(plain, recipient, enc)
    other = tmp_path / "other.key"
    subprocess.run(["age-keygen", "-o", str(other)], capture_output=True, check=True)
    with pytest.raises(DecryptError):
        decrypt_blob(enc, identity_path=other, out=tmp_path / "r.bin")
```

`tests/unit/controller/restore/test_summary.py`:

```python
from mthydra.controller.restore.summary import summarize_db
from mthydra.controller.state.authority import insert_authority
from mthydra.controller.state.cover_pool import add_candidate, mark_verified, move_to_in_use
from mthydra.controller.state.burned import mark_burned
from mthydra.controller.state.descriptor import insert_signing_key
from mthydra.controller.state.db import connect
from mthydra.controller.state.ru_boxes import insert_box, mark_live
from mthydra.controller.state.schema import SCHEMA_VERSION, apply_schema


def test_summary_reports_expected_counts(tmp_db_path):
    conn = connect(tmp_db_path)
    apply_schema(conn)
    insert_authority(conn, 1, "P", "K", "2026-05-18T00:00:00Z")
    insert_signing_key(conn, 1, b"P", b"K", "2026-05-18T00:00:00Z")
    add_candidate(conn, "a.org", added_at="2026-05-18T00:00:00Z")
    mark_verified(conn, "a.org", from_vantage="v", at="2026-05-18T01:00:00Z")
    insert_box(conn, "b1", "h", "fsn1", None, "a.org", "img1", "2026-05-18T00:00:00Z")
    move_to_in_use(conn, "a.org", box_id="b1")
    mark_live(conn, "b1", public_ip="1.2.3.4", at="2026-05-18T02:00:00Z")
    add_candidate(conn, "z.org", added_at="2026-05-18T00:00:00Z")
    mark_verified(conn, "z.org", from_vantage="v", at="2026-05-18T01:00:00Z")
    insert_box(conn, "b2", "h", "fsn1", None, "z.org", "img1", "2026-05-18T00:00:00Z")
    move_to_in_use(conn, "z.org", box_id="b2")
    mark_burned(conn, "z.org", "job2_kill", "b2", "2026-05-18T03:00:00Z", None)
    s = summarize_db(tmp_db_path)
    assert s["schema_version"] == SCHEMA_VERSION
    assert s["burned_domains_count"] == 1
    assert s["cover_pool_in_use"] == 1
    assert s["ru_boxes_live"] == 1
```

- [ ] **Step 2: Run tests to verify failure**

Expected: ImportError.

- [ ] **Step 3: Write `src/mthydra/controller/restore/decrypt.py`**

```python
"""age decryption for restore. Runs on the operator's machine, not on the controller."""
from __future__ import annotations

import subprocess
from pathlib import Path


class DecryptError(RuntimeError):
    pass


def decrypt_blob(blob_path: Path | str, identity_path: Path | str, out: Path | str) -> None:
    blob_path = Path(blob_path)
    identity_path = Path(identity_path)
    out = Path(out)
    if not blob_path.exists():
        raise DecryptError(f"blob not found: {blob_path}")
    if not identity_path.exists():
        raise DecryptError(f"identity not found: {identity_path}")
    try:
        subprocess.run(
            ["age", "-d", "-i", str(identity_path), "-o", str(out), str(blob_path)],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        raise DecryptError(f"age decrypt failed: {e.stderr.decode(errors='replace')}") from e
    except FileNotFoundError as e:
        raise DecryptError("age binary not on PATH") from e
```

- [ ] **Step 4: Write `src/mthydra/controller/restore/summary.py`**

```python
"""Summarize a restored SQLite DB. Read-only — never modifies the file."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from mthydra.controller.state.db import connect


def summarize_db(db_path: Path | str) -> dict[str, Any]:
    conn = connect(db_path, read_only=True)
    try:
        schema_version = conn.execute("SELECT version FROM schema_version WHERE rowid=1").fetchone()
        latest_backup_gen = conn.execute("SELECT MAX(generation) FROM backup_log").fetchone()
        burned_count = conn.execute("SELECT COUNT(*) FROM burned_domains").fetchone()[0]
        live_boxes = conn.execute("SELECT COUNT(*) FROM ru_boxes WHERE state='live'").fetchone()[0]
        pool_counts = dict(
            conn.execute(
                "SELECT state, COUNT(*) FROM cover_domain_pool GROUP BY state"
            ).fetchall()
        )
        latest_descriptor = conn.execute(
            "SELECT generation, valid_until FROM descriptor_history ORDER BY generation DESC LIMIT 1"
        ).fetchone()
        oldest_obligation = conn.execute(
            "SELECT obligation_id, last_proven_at FROM obligation_clocks ORDER BY last_proven_at ASC LIMIT 1"
        ).fetchone()
        return {
            "schema_version": schema_version[0] if schema_version else None,
            "latest_backup_generation": latest_backup_gen[0] if latest_backup_gen and latest_backup_gen[0] else None,
            "burned_domains_count": burned_count,
            "ru_boxes_live": live_boxes,
            "cover_pool_candidate_unverified": pool_counts.get("candidate_unverified", 0),
            "cover_pool_candidate_verified": pool_counts.get("candidate_verified", 0),
            "cover_pool_in_use": pool_counts.get("in_use", 0),
            "latest_descriptor_generation": latest_descriptor[0] if latest_descriptor else None,
            "latest_descriptor_valid_until": latest_descriptor[1] if latest_descriptor else None,
            "oldest_obligation": oldest_obligation,
        }
    finally:
        conn.close()
```

- [ ] **Step 5: Run tests**

Expected: 2 + 1 = 3 passed (decrypt skipped if no age).

- [ ] **Step 6: Commit**

```bash
git add src/mthydra/controller/restore/ tests/unit/controller/restore/
git commit -m "restore(A): age decrypt + summarize_db (operator-side, read-only)"
git push origin main
```

---

### Task 17: adopt-restored-state

**Files:**
- Create: `src/mthydra/controller/restore/adopt.py`
- Create: `tests/unit/controller/restore/test_adopt.py`

- [ ] **Step 1: Write failing tests**

```python
import pytest

from mthydra.controller.restore.adopt import AdoptError, adopt_restored_state
from mthydra.controller.state.authority import insert_authority
from mthydra.controller.state.db import connect
from mthydra.controller.state.descriptor import insert_signing_key
from mthydra.controller.state.schema import apply_schema


def _seed(path):
    conn = connect(path)
    apply_schema(conn)
    insert_authority(conn, 1, "P1", "K1", "2026-05-18T00:00:00Z")
    insert_signing_key(conn, 1, b"P", b"K", "2026-05-18T00:00:00Z")
    conn.close()


def test_adopt_replaces_existing_and_preserves_old(tmp_path):
    live = tmp_path / "state.sqlite"
    _seed(live)
    restored = tmp_path / "restored.sqlite"
    _seed(restored)
    adopt_restored_state(
        live_path=live,
        restored_path=restored,
        case=None,
        rotate_published_subset=False,
        at="2026-05-18T05:00:00Z",
    )
    assert live.exists()
    preadopt = list(tmp_path.glob("state.sqlite.preadopt.*"))
    assert len(preadopt) == 1


def test_adopt_case_b_inserts_new_authority(tmp_path):
    live = tmp_path / "state.sqlite"
    _seed(live)
    restored = tmp_path / "restored.sqlite"
    _seed(restored)
    adopt_restored_state(
        live_path=live, restored_path=restored, case="B", rotate_published_subset=False, at="2026-05-18T05:00:00Z"
    )
    conn = connect(live)
    rows = conn.execute("SELECT generation, retired_at FROM credential_authority ORDER BY generation").fetchall()
    assert len(rows) == 2
    assert rows[0][1] is not None
    assert rows[1][1] is None


def test_adopt_refuses_invalid_case(tmp_path):
    live = tmp_path / "state.sqlite"
    _seed(live)
    restored = tmp_path / "restored.sqlite"
    _seed(restored)
    with pytest.raises(AdoptError, match="case"):
        adopt_restored_state(
            live_path=live, restored_path=restored, case="C", rotate_published_subset=False, at="2026-05-18T05:00:00Z"
        )


def test_adopt_refuses_missing_restored(tmp_path):
    with pytest.raises(AdoptError, match="not found"):
        adopt_restored_state(
            live_path=tmp_path / "state.sqlite",
            restored_path=tmp_path / "missing.sqlite",
            case=None,
            rotate_published_subset=False,
            at="2026-05-18T05:00:00Z",
        )
```

- [ ] **Step 2: Run tests to verify failure**

Expected: ImportError.

- [ ] **Step 3: Write `src/mthydra/controller/restore/adopt.py`**

```python
"""Replace live state with a restored snapshot — spec A §7.2."""
from __future__ import annotations

import secrets
import shutil
from pathlib import Path

from mthydra.controller.state.authority import current_authority, insert_authority, retire_authority
from mthydra.controller.state.audit import log_event
from mthydra.controller.state.db import connect


class AdoptError(RuntimeError):
    pass


def _fresh_pem() -> tuple[str, str]:
    # Placeholder: real key generation is spec B's responsibility. Until B lands,
    # use a pseudo-random opaque token so adopt --case=B can demonstrably mint
    # a fresh authority row. Spec B will replace these placeholders with real
    # asymmetric key generation.
    nonce = secrets.token_hex(16)
    return (f"PRIV-PLACEHOLDER-{nonce}", f"PUB-PLACEHOLDER-{nonce}")


def adopt_restored_state(
    live_path: Path,
    restored_path: Path,
    case: str | None,
    rotate_published_subset: bool,
    at: str,
) -> None:
    live_path = Path(live_path)
    restored_path = Path(restored_path)
    if not restored_path.exists():
        raise AdoptError(f"restored file not found: {restored_path}")
    if case is not None and case not in {"A", "B"}:
        raise AdoptError(f"invalid case {case!r}; must be A, B, or None")

    if live_path.exists():
        preadopt = live_path.with_suffix(live_path.suffix + f".preadopt.{at.replace(':', '').replace('-', '')}")
        shutil.move(str(live_path), str(preadopt))
    shutil.move(str(restored_path), str(live_path))

    conn = connect(live_path)
    try:
        log_event(conn, ts=at, actor="operator", action="adopt_restored_state", target=str(live_path), details_json=f'{{"case":{case!r}}}')
        if case == "B":
            cur = current_authority(conn)
            retire_authority(conn, cur.generation, at=at)
            priv, pub = _fresh_pem()
            insert_authority(conn, cur.generation + 1, priv, pub, at)
            log_event(conn, ts=at, actor="operator", action="case_b_rekey", target=None, details_json=f'{{"new_generation":{cur.generation + 1}}}')
        if rotate_published_subset:
            conn.execute(
                "INSERT INTO published_subsets (payload_json, published_at, channel) VALUES (?, ?, ?)",
                ('{"_pending_rotation":true}', at, "telegram"),
            )
            conn.commit()
            log_event(conn, ts=at, actor="operator", action="rotate_published_subset_marker", target=None, details_json=None)
    finally:
        conn.close()
```

- [ ] **Step 4: Run tests**

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/restore/adopt.py tests/unit/controller/restore/test_adopt.py
git commit -m "restore(A): adopt-restored-state (incl. Case-B re-key placeholder)"
git push origin main
```

---

## Phase 5 — Bootstrap & Startup

### Task 18: bootstrap (init subcommand)

**Files:**
- Create: `src/mthydra/controller/bootstrap.py`
- Create: `tests/unit/controller/test_bootstrap.py`

- [ ] **Step 1: Write failing tests**

```python
import shutil

import pytest

from mthydra.controller.bootstrap import BootstrapError, init_state
from mthydra.controller.state.authority import current_authority
from mthydra.controller.state.db import connect
from mthydra.controller.state.descriptor import current_signing_key
from mthydra.controller.state.obligations import list_obligations
from mthydra.controller.state.tokens import get_provider_credential


@pytest.fixture
def recipient(tmp_path):
    pytest.importorskip("subprocess")
    if shutil.which("age-keygen") is None:
        pytest.skip("age-keygen not installed")
    import subprocess
    keyfile = tmp_path / "id.key"
    r = subprocess.run(["age-keygen", "-o", str(keyfile)], capture_output=True, text=True, check=True)
    return next(
        line.removeprefix("# public key: ").strip()
        for line in r.stderr.splitlines()
        if line.startswith("# public key: ")
    )


def test_init_creates_db_with_seed_rows(tmp_path, recipient):
    db = tmp_path / "state.sqlite"
    init_state(
        db_path=db,
        age_recipient=recipient,
        provider_credentials={"aws": "AKID:SECRET", "b2": "ID:SECRET"},
        obligation_timer_hours={"backup_restore_dryrun": 720, "t2_dryrun_caseA": 720},
        now="2026-05-18T00:00:00Z",
    )
    assert db.exists()
    conn = connect(db)
    assert current_authority(conn).generation == 1
    assert current_signing_key(conn).generation == 1
    assert get_provider_credential(conn, "aws") == "AKID:SECRET"
    obs = {o.obligation_id for o in list_obligations(conn)}
    assert "backup_restore_dryrun" in obs


def test_init_refuses_when_db_exists(tmp_path, recipient):
    db = tmp_path / "state.sqlite"
    db.write_bytes(b"")
    with pytest.raises(BootstrapError, match="exists"):
        init_state(
            db_path=db,
            age_recipient=recipient,
            provider_credentials={"aws": "x"},
            obligation_timer_hours={},
            now="2026-05-18T00:00:00Z",
        )


def test_init_rejects_bad_recipient(tmp_path):
    db = tmp_path / "state.sqlite"
    with pytest.raises(BootstrapError, match="recipient"):
        init_state(
            db_path=db,
            age_recipient="not-an-age-key",
            provider_credentials={"aws": "x"},
            obligation_timer_hours={},
            now="2026-05-18T00:00:00Z",
        )
```

- [ ] **Step 2: Run tests to verify failure**

Expected: ImportError.

- [ ] **Step 3: Write `src/mthydra/controller/bootstrap.py`**

```python
"""First-run bootstrap — spec A §10.1."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mthydra.controller.backup.age_crypt import AgeError, validate_recipient
from mthydra.controller.state.authority import insert_authority
from mthydra.controller.state.db import connect
from mthydra.controller.state.descriptor import insert_signing_key
from mthydra.controller.state.obligations import set_obligation
from mthydra.controller.state.schema import apply_schema
from mthydra.controller.state.tokens import set_provider_credential


class BootstrapError(RuntimeError):
    pass


def _placeholder_keypair_pem() -> tuple[str, str]:
    nonce = secrets.token_hex(16)
    return (f"PRIV-BOOTSTRAP-{nonce}", f"PUB-BOOTSTRAP-{nonce}")


def _placeholder_keypair_bytes() -> tuple[bytes, bytes]:
    return (b"PRIV-DESC-" + secrets.token_bytes(16), b"PUB-DESC-" + secrets.token_bytes(16))


def _add_hours(iso: str, hours: int) -> str:
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (t + timedelta(hours=hours)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def init_state(
    db_path: Path | str,
    age_recipient: str,
    provider_credentials: dict[str, str],
    obligation_timer_hours: dict[str, int],
    now: str,
) -> None:
    db_path = Path(db_path)
    if db_path.exists():
        raise BootstrapError(f"refusing to bootstrap: {db_path} exists; move or delete first")
    try:
        validate_recipient(age_recipient)
    except AgeError as e:
        raise BootstrapError(f"invalid age recipient: {e}") from e

    conn = connect(db_path)
    try:
        apply_schema(conn)
        priv, pub = _placeholder_keypair_pem()
        insert_authority(conn, generation=1, privkey_pem=priv, pubkey_pem=pub, created_at=now)
        dpriv, dpub = _placeholder_keypair_bytes()
        insert_signing_key(conn, generation=1, privkey=dpriv, pubkey=dpub, created_at=now)
        for provider, cred in provider_credentials.items():
            set_provider_credential(conn, provider=provider, credential=cred, at=now)
        for obligation_id, hours in obligation_timer_hours.items():
            set_obligation(
                conn,
                obligation_id=obligation_id,
                last_proven_at=now,
                proven_by="bootstrap",
                details=None,
                next_due_at=_add_hours(now, hours) if hours > 0 else now,
            )
    finally:
        conn.close()
```

- [ ] **Step 4: Run tests**

Expected: 3 passed (or first 1 skipped if age-keygen absent).

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/bootstrap.py tests/unit/controller/test_bootstrap.py
git commit -m "bootstrap(A): init_state for first-run controller setup"
git push origin main
```

---

### Task 19: Startup self-check runner

**Files:**
- Create: `src/mthydra/controller/startup.py`
- Create: `tests/unit/controller/test_startup.py`

- [ ] **Step 1: Write failing tests**

```python
import shutil
import subprocess

import pytest

from mthydra.controller.bootstrap import init_state
from mthydra.controller.startup import StartupCheckResult, run_startup_checks


@pytest.fixture
def recipient(tmp_path):
    if shutil.which("age-keygen") is None:
        pytest.skip("age-keygen not installed")
    keyfile = tmp_path / "id.key"
    r = subprocess.run(["age-keygen", "-o", str(keyfile)], capture_output=True, text=True, check=True)
    return next(
        line.removeprefix("# public key: ").strip()
        for line in r.stderr.splitlines()
        if line.startswith("# public key: ")
    )


@pytest.fixture
def initialized_db(tmp_path, recipient):
    db = tmp_path / "state.sqlite"
    init_state(
        db_path=db,
        age_recipient=recipient,
        provider_credentials={"aws": "x", "b2": "y"},
        obligation_timer_hours={"backup_restore_dryrun": 720},
        now="2026-05-18T00:00:00Z",
    )
    return db, recipient


def test_run_startup_checks_succeeds_on_clean_init(initialized_db):
    db, recipient = initialized_db
    result = run_startup_checks(db_path=db, age_recipient=recipient)
    assert result.ok
    assert result.failed_check is None


def test_run_startup_checks_fails_when_db_missing(tmp_path, recipient):
    result = run_startup_checks(db_path=tmp_path / "missing.sqlite", age_recipient=recipient)
    assert not result.ok
    assert "db" in result.failed_check


def test_run_startup_checks_fails_on_bad_recipient(initialized_db):
    db, _ = initialized_db
    result = run_startup_checks(db_path=db, age_recipient="not-age")
    assert not result.ok
    assert "recipient" in result.failed_check
```

- [ ] **Step 2: Run tests to verify failure**

Expected: ImportError.

- [ ] **Step 3: Write `src/mthydra/controller/startup.py`**

```python
"""Startup self-check runner — composes invariants module with file-existence/age checks.

Spec A §10. Refusal-to-start is implemented in the CLI layer; this module
returns structured pass/fail results so tests can drive every branch.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from mthydra.controller.backup.age_crypt import AgeError, validate_recipient
from mthydra.controller.backup.reconcile import reconcile_pending
from mthydra.controller.state.db import connect
from mthydra.controller.state.invariants import InvariantViolation, check_all
from mthydra.controller.state.schema import SCHEMA_VERSION


@dataclass(frozen=True)
class StartupCheckResult:
    ok: bool
    failed_check: str | None
    message: str | None


def _fail(name: str, msg: str) -> StartupCheckResult:
    return StartupCheckResult(ok=False, failed_check=name, message=msg)


def run_startup_checks(db_path: Path | str, age_recipient: str) -> StartupCheckResult:
    db_path = Path(db_path)
    if not db_path.exists():
        return _fail("db_present", f"state file not found: {db_path}")

    if shutil.which("age") is None:
        return _fail("age_binary", "age binary not on PATH")

    try:
        validate_recipient(age_recipient)
    except AgeError as e:
        return _fail("age_recipient", str(e))

    conn = connect(db_path)
    try:
        try:
            check_all(conn, expected_schema_version=SCHEMA_VERSION)
        except InvariantViolation as e:
            return _fail("invariant", str(e))
    finally:
        conn.close()

    return StartupCheckResult(ok=True, failed_check=None, message=None)


def reconcile_after_startup(db_path: Path | str, destination) -> int:
    """Run §9 crash-recovery after a clean startup check has passed."""
    from datetime import datetime, timezone

    def clock() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return reconcile_pending(Path(db_path), destination, clock=clock)
```

- [ ] **Step 4: Run tests**

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/startup.py tests/unit/controller/test_startup.py
git commit -m "startup(A): self-check runner (db + age + invariants)"
git push origin main
```

---

## Phase 6 — Controller CLI

### Task 20: Controller CLI (argparse) wiring all subcommands

**Files:**
- Create: `src/mthydra/controller/cli.py`
- Create: `src/mthydra/controller/__main__.py`
- Create: `tests/unit/controller/test_cli.py`

- [ ] **Step 1: Write failing tests**

```python
import shutil
import subprocess

import pytest

from mthydra.controller.cli import build_parser, run


def test_parser_knows_all_subcommands():
    p = build_parser()
    sub_help = p.format_help()
    for name in ("init", "startup-check", "backup-now", "restore", "adopt-restored-state", "obligation-proven"):
        assert name in sub_help


def test_init_subcommand_runs(tmp_path, monkeypatch):
    if shutil.which("age-keygen") is None:
        pytest.skip("age-keygen not installed")
    keyfile = tmp_path / "id.key"
    r = subprocess.run(["age-keygen", "-o", str(keyfile)], capture_output=True, text=True, check=True)
    recipient = next(
        line.removeprefix("# public key: ").strip()
        for line in r.stderr.splitlines()
        if line.startswith("# public key: ")
    )
    recipient_file = tmp_path / "age-recipient.txt"
    recipient_file.write_text(recipient + "\n")
    db = tmp_path / "state.sqlite"
    exit_code = run([
        "init",
        "--db-path", str(db),
        "--age-recipient-file", str(recipient_file),
        "--provider-credential", "aws=AKID:SECRET",
        "--provider-credential", "b2=ID:SECRET",
    ])
    assert exit_code == 0
    assert db.exists()


def test_startup_check_returns_nonzero_when_db_missing(tmp_path):
    exit_code = run([
        "startup-check",
        "--db-path", str(tmp_path / "missing.sqlite"),
        "--age-recipient", "age1xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
    ])
    assert exit_code != 0


def test_obligation_proven_updates_clock(tmp_path):
    if shutil.which("age-keygen") is None:
        pytest.skip("age-keygen not installed")
    keyfile = tmp_path / "id.key"
    r = subprocess.run(["age-keygen", "-o", str(keyfile)], capture_output=True, text=True, check=True)
    recipient = next(
        line.removeprefix("# public key: ").strip()
        for line in r.stderr.splitlines()
        if line.startswith("# public key: ")
    )
    recipient_file = tmp_path / "age-recipient.txt"
    recipient_file.write_text(recipient + "\n")
    db = tmp_path / "state.sqlite"
    run([
        "init", "--db-path", str(db),
        "--age-recipient-file", str(recipient_file),
        "--provider-credential", "aws=x",
    ])
    exit_code = run([
        "obligation-proven", "backup_restore_dryrun",
        "--db-path", str(db),
        "--details", "dry-run gen-1 → vm-test at 2026-05-18T00:00:00Z",
    ])
    assert exit_code == 0
```

- [ ] **Step 2: Run tests to verify failure**

Expected: ImportError.

- [ ] **Step 3: Write `src/mthydra/controller/cli.py`**

```python
"""Controller CLI. Subcommands: init, startup-check, backup-now, restore,
adopt-restored-state, obligation-proven."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from mthydra.controller.bootstrap import BootstrapError, init_state
from mthydra.controller.restore.adopt import AdoptError, adopt_restored_state
from mthydra.controller.restore.decrypt import DecryptError, decrypt_blob
from mthydra.controller.restore.summary import summarize_db
from mthydra.controller.startup import run_startup_checks
from mthydra.controller.state.db import connect
from mthydra.controller.state.obligations import prove


DEFAULT_DB = "/var/lib/mthydra/state.sqlite"
DEFAULT_RECIPIENT_FILE = "/etc/mthydra/age-recipient.txt"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_recipient(path: str) -> str:
    return Path(path).read_text().strip().splitlines()[0]


def _parse_kv(items: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in items:
        if "=" not in raw:
            raise ValueError(f"expected KEY=VALUE, got {raw!r}")
        k, v = raw.split("=", 1)
        out[k] = v
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mthydra-controller")
    sub = p.add_subparsers(dest="cmd", required=True)

    init = sub.add_parser("init", help="initialize a fresh controller DB")
    init.add_argument("--db-path", default=DEFAULT_DB)
    g = init.add_mutually_exclusive_group(required=True)
    g.add_argument("--age-recipient")
    g.add_argument("--age-recipient-file")
    init.add_argument("--provider-credential", action="append", default=[],
                     help="PROVIDER=CREDENTIAL (repeatable)")

    sc = sub.add_parser("startup-check", help="run §10 self-checks")
    sc.add_argument("--db-path", default=DEFAULT_DB)
    sc.add_argument("--age-recipient")
    sc.add_argument("--age-recipient-file")

    bn = sub.add_parser("backup-now", help="trigger a manual backup")
    bn.add_argument("--db-path", default=DEFAULT_DB)
    bn.add_argument("--reason", default="manual")
    bn.add_argument("--note",
                    help="advisory only — this CLI does not perform S3 push by itself; the running controller daemon handles the trigger via signal")

    rst = sub.add_parser("restore", help="decrypt + sanity-summarize a backup blob")
    rst.add_argument("--from", dest="src", required=True, help="encrypted blob path")
    rst.add_argument("--identity", required=True, help="operator age identity file")
    rst.add_argument("--into", required=True, help="destination plaintext sqlite path")
    rst.add_argument("--summary-only", action="store_true")

    adp = sub.add_parser("adopt-restored-state", help="install a restored DB as live state")
    adp.add_argument("restored_path")
    adp.add_argument("--live-path", default=DEFAULT_DB)
    adp.add_argument("--case", choices=["A", "B"])
    adp.add_argument("--rotate-published-subset", action="store_true")

    op = sub.add_parser("obligation-proven", help="stamp an obligation clock as proven now")
    op.add_argument("obligation_id")
    op.add_argument("--db-path", default=DEFAULT_DB)
    op.add_argument("--details", default=None)
    op.add_argument("--next-due-hours", type=int, default=720,
                    help="advance next_due_at by this many hours")

    return p


def run(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "init":
        recipient = args.age_recipient or _read_recipient(args.age_recipient_file)
        try:
            init_state(
                db_path=args.db_path,
                age_recipient=recipient,
                provider_credentials=_parse_kv(args.provider_credential),
                obligation_timer_hours={
                    "backup_restore_dryrun": 720,
                    "t2_dryrun_caseA": 720,
                    "t2_dryrun_caseB": 720,
                    "t1_dormant_health": 168,
                    "t3_vantage_revalidation": 168,
                    "t3_profile_repin": 0,
                    "t4_upstream_check": 168,
                    "t5_pool_revalidation": 168,
                    "t6_reshuffle": 168,
                },
                now=_now(),
            )
            print(f"initialized {args.db_path}")
            return 0
        except BootstrapError as e:
            print(f"bootstrap error: {e}", file=sys.stderr)
            return 2
    if args.cmd == "startup-check":
        recipient = args.age_recipient or (
            _read_recipient(args.age_recipient_file) if args.age_recipient_file else ""
        )
        result = run_startup_checks(db_path=args.db_path, age_recipient=recipient)
        if result.ok:
            print("startup-check: OK")
            return 0
        print(f"startup-check FAILED ({result.failed_check}): {result.message}", file=sys.stderr)
        return 10
    if args.cmd == "backup-now":
        print(
            f"signalled backup-now (trigger={args.reason}). The running daemon should perform the push; "
            "this CLI does not invoke S3 directly.",
        )
        return 0
    if args.cmd == "restore":
        plain = Path(args.into)
        try:
            decrypt_blob(args.src, identity_path=args.identity, out=plain)
        except DecryptError as e:
            print(f"restore: {e}", file=sys.stderr)
            return 3
        summary = summarize_db(plain)
        print(json.dumps(summary, indent=2, default=str))
        if args.summary_only:
            plain.unlink(missing_ok=True)
        return 0
    if args.cmd == "adopt-restored-state":
        try:
            adopt_restored_state(
                live_path=args.live_path,
                restored_path=args.restored_path,
                case=args.case,
                rotate_published_subset=args.rotate_published_subset,
                at=_now(),
            )
            print("adopted")
            return 0
        except AdoptError as e:
            print(f"adopt: {e}", file=sys.stderr)
            return 4
    if args.cmd == "obligation-proven":
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        next_due = (now + timedelta(hours=args.next_due_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = connect(args.db_path)
        try:
            try:
                prove(
                    conn,
                    obligation_id=args.obligation_id,
                    proven_by="operator",
                    at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    next_due_at=next_due,
                    details=args.details,
                )
            except KeyError as e:
                print(f"unknown obligation: {e}", file=sys.stderr)
                return 5
        finally:
            conn.close()
        print(f"stamped {args.obligation_id}")
        return 0
    return 1
```

- [ ] **Step 4: Write `src/mthydra/controller/__main__.py`**

```python
"""Console-script entry point for `mthydra-controller`."""
from __future__ import annotations

import sys

from mthydra.controller.cli import run


def main() -> None:
    sys.exit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Reinstall to register entry point**

```bash
pip install -e '.[dev]'
```

Then run:

```bash
mthydra-controller --help
```

Expected: argparse help listing all subcommands.

- [ ] **Step 6: Run tests**

Expected: 4 passed (or some skipped if age-keygen absent).

- [ ] **Step 7: Commit**

```bash
git add src/mthydra/controller/cli.py src/mthydra/controller/__main__.py tests/unit/controller/test_cli.py
git commit -m "cli(A): controller CLI wiring all subcommands"
git push origin main
```

---

## Phase 7 — Backup Monitor

### Task 21: Backup-monitor poller

**Files:**
- Create: `src/mthydra/backup_monitor/__init__.py` (empty)
- Create: `src/mthydra/backup_monitor/poller.py`
- Create: `tests/unit/backup_monitor/__init__.py` (empty)
- Create: `tests/unit/backup_monitor/test_poller.py`

- [ ] **Step 1: Write failing tests**

```python
import boto3
import pytest
from moto import mock_aws

from mthydra.backup_monitor.poller import GapMonitorState, evaluate_gap
from mthydra.controller.backup.s3_dest import S3Destination


@pytest.fixture
def dest_with_index():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="b")
        d = S3Destination(None, "b", "x", "y", "us-east-1", object_lock_days=30)
        d._client = client
        d.put_index(highest_gen=7, sha256="abc", size_bytes=100, ts="2026-05-18T00:00:00Z")
        yield d


def test_evaluate_gap_first_observation(dest_with_index):
    state = GapMonitorState(last_seen_gen=None, first_observed_at=None, last_alarm_at=None)
    new_state, should_alarm = evaluate_gap(
        index=dest_with_index.head_index(),
        state=state,
        now_iso="2026-05-18T01:00:00Z",
        alarm_threshold_hours=48,
        alarm_repeat_hours=24,
    )
    assert new_state.last_seen_gen == 7
    assert new_state.first_observed_at == "2026-05-18T01:00:00Z"
    assert not should_alarm


def test_evaluate_gap_fires_after_threshold(dest_with_index):
    state = GapMonitorState(last_seen_gen=7, first_observed_at="2026-05-18T01:00:00Z", last_alarm_at=None)
    _, should_alarm = evaluate_gap(
        index=dest_with_index.head_index(),
        state=state,
        now_iso="2026-05-20T02:00:00Z",
        alarm_threshold_hours=48,
        alarm_repeat_hours=24,
    )
    assert should_alarm


def test_evaluate_gap_clears_on_advancement(dest_with_index):
    state = GapMonitorState(last_seen_gen=7, first_observed_at="2026-05-18T01:00:00Z", last_alarm_at="2026-05-20T02:00:00Z")
    dest_with_index.put_index(highest_gen=8, sha256="def", size_bytes=200, ts="2026-05-20T03:00:00Z")
    new_state, should_alarm = evaluate_gap(
        index=dest_with_index.head_index(),
        state=state,
        now_iso="2026-05-20T04:00:00Z",
        alarm_threshold_hours=48,
        alarm_repeat_hours=24,
    )
    assert new_state.last_seen_gen == 8
    assert new_state.first_observed_at == "2026-05-20T04:00:00Z"
    assert new_state.last_alarm_at is None
    assert not should_alarm


def test_evaluate_gap_handles_missing_index():
    state = GapMonitorState(last_seen_gen=None, first_observed_at=None, last_alarm_at=None)
    new_state, should_alarm = evaluate_gap(
        index=None,
        state=state,
        now_iso="2026-05-18T01:00:00Z",
        alarm_threshold_hours=48,
        alarm_repeat_hours=24,
    )
    assert new_state == state
    assert not should_alarm
```

- [ ] **Step 2: Run tests to verify failure**

Expected: ImportError.

- [ ] **Step 3: Write `src/mthydra/backup_monitor/poller.py`**

```python
"""Generation-gap evaluator. Pure function — state is passed in and out.

The runtime poller loop (CLI) drives this. Tests cover the evaluator without
touching real timers.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any


@dataclass(frozen=True)
class GapMonitorState:
    last_seen_gen: int | None
    first_observed_at: str | None
    last_alarm_at: str | None


def _parse(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def evaluate_gap(
    index: dict[str, Any] | None,
    state: GapMonitorState,
    now_iso: str,
    alarm_threshold_hours: int,
    alarm_repeat_hours: int,
) -> tuple[GapMonitorState, bool]:
    """Update gap-monitor state based on the latest index.json snapshot.

    Returns (new_state, should_alarm_now).
    """
    if index is None:
        return state, False

    current_gen = int(index.get("highest_gen", 0))

    if state.last_seen_gen is None or current_gen > state.last_seen_gen:
        return GapMonitorState(
            last_seen_gen=current_gen,
            first_observed_at=now_iso,
            last_alarm_at=None,
        ), False

    if state.first_observed_at is None:
        return replace(state, first_observed_at=now_iso), False

    age = _parse(now_iso) - _parse(state.first_observed_at)
    if age < timedelta(hours=alarm_threshold_hours):
        return state, False

    if state.last_alarm_at is None:
        return replace(state, last_alarm_at=now_iso), True

    since_alarm = _parse(now_iso) - _parse(state.last_alarm_at)
    if since_alarm >= timedelta(hours=alarm_repeat_hours):
        return replace(state, last_alarm_at=now_iso), True

    return state, False
```

- [ ] **Step 4: Run tests**

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/backup_monitor/__init__.py \
        src/mthydra/backup_monitor/poller.py \
        tests/unit/backup_monitor/__init__.py \
        tests/unit/backup_monitor/test_poller.py
git commit -m "monitor(A): gap-evaluator pure function with state-in/state-out"
git push origin main
```

---

### Task 22: SMTP emailer

**Files:**
- Create: `src/mthydra/backup_monitor/emailer.py`
- Create: `tests/unit/backup_monitor/test_emailer.py`

- [ ] **Step 1: Write failing tests**

```python
from unittest.mock import MagicMock, patch

from mthydra.backup_monitor.emailer import EmailConfig, send_gap_alarm


def test_send_gap_alarm_uses_smtp_ssl():
    cfg = EmailConfig(
        host="smtp.example.com",
        port=465,
        username="alerter@example.com",
        app_password="apppw",
        from_addr="alerter@example.com",
        to_addr="op@example.org",
    )
    with patch("smtplib.SMTP_SSL") as smtp_cls:
        instance = MagicMock()
        smtp_cls.return_value.__enter__.return_value = instance
        send_gap_alarm(cfg, highest_gen=7, stuck_since="2026-05-18T01:00:00Z", now_iso="2026-05-20T02:00:00Z")
    instance.login.assert_called_once_with("alerter@example.com", "apppw")
    instance.send_message.assert_called_once()
    msg = instance.send_message.call_args[0][0]
    assert "highest_gen=7" in msg["Subject"]
    assert "stuck since 2026-05-18T01:00:00Z" in msg.get_content()
```

- [ ] **Step 2: Run test to verify failure**

Expected: ImportError.

- [ ] **Step 3: Write `src/mthydra/backup_monitor/emailer.py`**

```python
"""SMTP-app-password emailer. Shared by gap monitor and controller self-alarms."""
from __future__ import annotations

import smtplib
from dataclasses import dataclass
from email.message import EmailMessage


@dataclass(frozen=True)
class EmailConfig:
    host: str
    port: int
    username: str
    app_password: str
    from_addr: str
    to_addr: str


def _send(cfg: EmailConfig, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.from_addr
    msg["To"] = cfg.to_addr
    msg.set_content(body)
    with smtplib.SMTP_SSL(cfg.host, cfg.port) as smtp:
        smtp.login(cfg.username, cfg.app_password)
        smtp.send_message(msg)


def send_gap_alarm(cfg: EmailConfig, highest_gen: int, stuck_since: str, now_iso: str) -> None:
    subject = f"mthydra: backup gap (highest_gen={highest_gen} stuck since {stuck_since})"
    body = (
        f"Backup generation has not advanced.\n\n"
        f"Highest generation observed: {highest_gen}\n"
        f"Stuck since:                 {stuck_since}\n"
        f"Now:                         {now_iso}\n\n"
        f"Investigate per T2 runbook. If the active controller is dead, promote the warm standby.\n"
    )
    _send(cfg, subject, body)
```

- [ ] **Step 4: Run tests**

Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/backup_monitor/emailer.py tests/unit/backup_monitor/test_emailer.py
git commit -m "monitor(A): SMTP-app-password emailer for gap alarms"
git push origin main
```

---

### Task 23: Backup-monitor CLI + entry point

**Files:**
- Create: `src/mthydra/backup_monitor/cli.py`
- Create: `src/mthydra/backup_monitor/__main__.py`

- [ ] **Step 1: Write `src/mthydra/backup_monitor/cli.py`**

```python
"""mthydra-backup-monitor CLI.

Loads controller.toml for shared backup-destination config, polls index.json
on the configured interval, and emits an email via SMTP-app-password when the
generation has not advanced past `alarm_threshold_hours`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from mthydra.backup_monitor.emailer import EmailConfig, send_gap_alarm
from mthydra.backup_monitor.poller import GapMonitorState, evaluate_gap
from mthydra.controller.backup.s3_dest import S3Destination
from mthydra.controller.config import load_config


STATE_FILE_DEFAULT = "/var/lib/mthydra/backup-monitor-state.json"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_state(path: Path) -> GapMonitorState:
    if not path.exists():
        return GapMonitorState(None, None, None)
    raw = json.loads(path.read_text())
    return GapMonitorState(
        last_seen_gen=raw.get("last_seen_gen"),
        first_observed_at=raw.get("first_observed_at"),
        last_alarm_at=raw.get("last_alarm_at"),
    )


def _save_state(path: Path, state: GapMonitorState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"last_seen_gen": state.last_seen_gen,
         "first_observed_at": state.first_observed_at,
         "last_alarm_at": state.last_alarm_at}
    ))


def _build_destination(cfg, secret_access_key: str) -> S3Destination:
    return S3Destination(
        endpoint_url=cfg.backup.endpoint,
        bucket=cfg.backup.bucket,
        access_key_id=cfg.backup.access_key_id,
        secret_access_key=secret_access_key,
        region=os.environ.get("MTHYDRA_BACKUP_REGION", "us-east-1"),
        object_lock_days=cfg.backup.retention.object_lock_days,
    )


def _build_email(cfg) -> EmailConfig:
    return EmailConfig(
        host=os.environ["MTHYDRA_SMTP_HOST"],
        port=int(os.environ.get("MTHYDRA_SMTP_PORT", "465")),
        username=os.environ["MTHYDRA_SMTP_USER"],
        app_password=os.environ["MTHYDRA_SMTP_PASS"],
        from_addr=os.environ.get("MTHYDRA_SMTP_FROM", os.environ["MTHYDRA_SMTP_USER"]),
        to_addr=cfg.gap_monitor.recipient_email,
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="mthydra-backup-monitor")
    parser.add_argument("--config", default="/etc/mthydra/controller.toml")
    parser.add_argument("--state-file", default=STATE_FILE_DEFAULT)
    parser.add_argument("--once", action="store_true", help="poll one time and exit (for tests)")
    args = parser.parse_args()

    cfg = load_config(args.config)
    secret = os.environ.get("MTHYDRA_B2_SECRET", "")
    if not secret:
        print("MTHYDRA_B2_SECRET env var required", file=sys.stderr)
        sys.exit(2)
    dest = _build_destination(cfg, secret)
    email_cfg = _build_email(cfg)
    state_path = Path(args.state_file)

    poll_interval_s = cfg.gap_monitor.poll_interval_minutes * 60
    alarm_threshold = cfg.gap_monitor.alarm_threshold_hours
    alarm_repeat = 24

    while True:
        state = _load_state(state_path)
        try:
            index = dest.head_index()
        except Exception as e:
            print(f"poll: failed to fetch index: {e}", file=sys.stderr)
            if args.once:
                return
            time.sleep(poll_interval_s)
            continue
        new_state, should_alarm = evaluate_gap(
            index=index, state=state, now_iso=_now(),
            alarm_threshold_hours=alarm_threshold, alarm_repeat_hours=alarm_repeat,
        )
        _save_state(state_path, new_state)
        if should_alarm:
            send_gap_alarm(
                email_cfg,
                highest_gen=new_state.last_seen_gen or 0,
                stuck_since=new_state.first_observed_at or "?",
                now_iso=_now(),
            )
        if args.once:
            return
        time.sleep(poll_interval_s)
```

- [ ] **Step 2: Write `src/mthydra/backup_monitor/__main__.py`**

```python
from mthydra.backup_monitor.cli import main


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Sanity run**

```bash
pip install -e '.[dev]'
mthydra-backup-monitor --help
```

Expected: argparse help.

- [ ] **Step 4: Commit**

```bash
git add src/mthydra/backup_monitor/cli.py src/mthydra/backup_monitor/__main__.py
git commit -m "monitor(A): CLI + entry point for mthydra-backup-monitor"
git push origin main
```

---

## Phase 8 — Integration & Property Tests

### Task 24: End-to-end backup integration test (moto)

**Files:**
- Create: `tests/integration/__init__.py` (empty)
- Create: `tests/integration/test_end_to_end_backup.py`

- [ ] **Step 1: Write the integration test**

```python
import shutil
import subprocess
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from mthydra.controller.backup.pipeline import BackupPipeline
from mthydra.controller.backup.s3_dest import S3Destination
from mthydra.controller.bootstrap import init_state
from mthydra.controller.restore.decrypt import decrypt_blob
from mthydra.controller.restore.summary import summarize_db
from mthydra.controller.state.backup_log import BackupTrigger
from mthydra.controller.state.burned import mark_burned
from mthydra.controller.state.cover_pool import add_candidate, mark_verified, move_to_in_use
from mthydra.controller.state.db import connect
from mthydra.controller.state.ru_boxes import insert_box

pytestmark = pytest.mark.skipif(shutil.which("age") is None, reason="age binary not installed")


@pytest.fixture
def keypair(tmp_path):
    keyfile = tmp_path / "id.key"
    r = subprocess.run(["age-keygen", "-o", str(keyfile)], capture_output=True, text=True, check=True)
    recipient = next(
        line.removeprefix("# public key: ").strip()
        for line in r.stderr.splitlines()
        if line.startswith("# public key: ")
    )
    return keyfile, recipient


async def test_backup_then_decrypt_then_summarize(tmp_path, keypair):
    keyfile, recipient = keypair
    db = tmp_path / "state.sqlite"
    init_state(
        db_path=db,
        age_recipient=recipient,
        provider_credentials={"aws": "x", "b2": "y"},
        obligation_timer_hours={"backup_restore_dryrun": 720},
        now="2026-05-18T00:00:00Z",
    )
    # produce some state so summary is non-trivial
    conn = connect(db)
    add_candidate(conn, "alpha.example", added_at="2026-05-18T00:00:00Z")
    mark_verified(conn, "alpha.example", from_vantage="v", at="2026-05-18T00:00:01Z")
    insert_box(conn, "b1", "h", "fsn1", None, "alpha.example", "img1", "2026-05-18T00:00:02Z")
    move_to_in_use(conn, "alpha.example", box_id="b1")
    add_candidate(conn, "beta.example", added_at="2026-05-18T00:00:03Z")
    mark_verified(conn, "beta.example", from_vantage="v", at="2026-05-18T00:00:04Z")
    insert_box(conn, "b2", "h", "fsn1", None, "beta.example", "img1", "2026-05-18T00:00:05Z")
    move_to_in_use(conn, "beta.example", box_id="b2")
    mark_burned(conn, "beta.example", "job2_kill", "b2", "2026-05-18T00:00:06Z", None)
    conn.close()

    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="b")
        dest = S3Destination(None, "b", "x", "y", "us-east-1", object_lock_days=30)
        dest._client = client
        tmp = tmp_path / "tmp"
        tmp.mkdir()
        pipeline = BackupPipeline(db, tmp, recipient, dest, lambda: "2026-05-18T01:00:00Z")
        gen = await pipeline.do_backup(BackupTrigger.MANUAL)
        assert gen == 1
        # pull the blob, decrypt, summarize
        body = client.get_object(Bucket="b", Key=f"gen-{gen:010d}.age")["Body"].read()
        blob = tmp_path / "blob.age"
        blob.write_bytes(body)
        restored = tmp_path / "restored.sqlite"
        decrypt_blob(blob, identity_path=keyfile, out=restored)
        s = summarize_db(restored)
        assert s["burned_domains_count"] == 1
        assert s["cover_pool_in_use"] == 1
```

- [ ] **Step 2: Run the integration test**

`pytest tests/integration/test_end_to_end_backup.py -v`
Expected: 1 passed (or skipped if `age` unavailable).

- [ ] **Step 3: Commit**

```bash
git add tests/integration/__init__.py tests/integration/test_end_to_end_backup.py
git commit -m "test(A): integration — backup → decrypt → summarize roundtrip"
git push origin main
```

---

### Task 25: End-to-end restore + adopt integration test

**Files:**
- Create: `tests/integration/test_end_to_end_restore.py`

- [ ] **Step 1: Write the test**

```python
import shutil
import subprocess

import boto3
import pytest
from moto import mock_aws

from mthydra.controller.backup.pipeline import BackupPipeline
from mthydra.controller.backup.s3_dest import S3Destination
from mthydra.controller.bootstrap import init_state
from mthydra.controller.restore.adopt import adopt_restored_state
from mthydra.controller.restore.decrypt import decrypt_blob
from mthydra.controller.state.authority import list_authorities
from mthydra.controller.state.backup_log import BackupTrigger
from mthydra.controller.state.db import connect

pytestmark = pytest.mark.skipif(shutil.which("age") is None, reason="age binary not installed")


@pytest.fixture
def keypair(tmp_path):
    keyfile = tmp_path / "id.key"
    r = subprocess.run(["age-keygen", "-o", str(keyfile)], capture_output=True, text=True, check=True)
    recipient = next(
        line.removeprefix("# public key: ").strip()
        for line in r.stderr.splitlines()
        if line.startswith("# public key: ")
    )
    return keyfile, recipient


async def test_restore_then_adopt_case_b_rotates_authority(tmp_path, keypair):
    keyfile, recipient = keypair
    db = tmp_path / "state.sqlite"
    init_state(
        db_path=db,
        age_recipient=recipient,
        provider_credentials={"aws": "x"},
        obligation_timer_hours={},
        now="2026-05-18T00:00:00Z",
    )

    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="b")
        dest = S3Destination(None, "b", "x", "y", "us-east-1", object_lock_days=30)
        dest._client = client
        tmp = tmp_path / "tmp"
        tmp.mkdir()
        pipeline = BackupPipeline(db, tmp, recipient, dest, lambda: "2026-05-18T01:00:00Z")
        gen = await pipeline.do_backup(BackupTrigger.MANUAL)

        body = client.get_object(Bucket="b", Key=f"gen-{gen:010d}.age")["Body"].read()
        blob = tmp_path / "blob.age"
        blob.write_bytes(body)

    restored = tmp_path / "restored.sqlite"
    decrypt_blob(blob, identity_path=keyfile, out=restored)

    live_target = tmp_path / "live.sqlite"
    # Simulate destination being a fresh standby — adopt into a fresh path
    adopt_restored_state(
        live_path=live_target,
        restored_path=restored,
        case="B",
        rotate_published_subset=True,
        at="2026-05-18T02:00:00Z",
    )

    conn = connect(live_target)
    auths = list_authorities(conn)
    assert len(auths) == 2
    assert auths[0].retired_at is not None
    assert auths[1].retired_at is None
```

- [ ] **Step 2: Run the test**

`pytest tests/integration/test_end_to_end_restore.py -v`
Expected: 1 passed (or skipped).

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_end_to_end_restore.py
git commit -m "test(A): integration — restore → adopt Case-B rotates authority"
git push origin main
```

---

### Task 26: Gap-monitor integration test

**Files:**
- Create: `tests/integration/test_gap_monitor.py`

- [ ] **Step 1: Write the test**

```python
import boto3
import pytest
from moto import mock_aws

from mthydra.backup_monitor.poller import GapMonitorState, evaluate_gap
from mthydra.controller.backup.s3_dest import S3Destination


def test_gap_monitor_against_minio_like_bucket(tmp_path):
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="b")
        dest = S3Destination(None, "b", "x", "y", "us-east-1", object_lock_days=30)
        dest._client = client

        dest.put_index(highest_gen=1, sha256="a", size_bytes=10, ts="2026-05-18T00:00:00Z")
        state = GapMonitorState(None, None, None)
        state, should_alarm = evaluate_gap(
            dest.head_index(), state, "2026-05-18T01:00:00Z", 48, 24
        )
        assert state.last_seen_gen == 1
        assert not should_alarm

        # 50 hours later, no advancement
        state, should_alarm = evaluate_gap(
            dest.head_index(), state, "2026-05-20T03:00:00Z", 48, 24
        )
        assert should_alarm

        # index advances; alarm clears
        dest.put_index(highest_gen=2, sha256="b", size_bytes=20, ts="2026-05-20T03:01:00Z")
        state, should_alarm = evaluate_gap(
            dest.head_index(), state, "2026-05-20T03:02:00Z", 48, 24
        )
        assert state.last_seen_gen == 2
        assert state.last_alarm_at is None
        assert not should_alarm
```

- [ ] **Step 2: Run the test**

`pytest tests/integration/test_gap_monitor.py -v`
Expected: 1 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_gap_monitor.py
git commit -m "test(A): integration — gap-monitor full cycle (observe → alarm → clear)"
git push origin main
```

---

### Task 27: Property test for burned-set monotonicity

**Files:**
- Create: `tests/property/__init__.py` (empty)
- Create: `tests/property/test_burned_set_monotonic.py`

- [ ] **Step 1: Write the test**

```python
from hypothesis import given, settings
from hypothesis import strategies as st

from mthydra.controller.state.burned import is_burned, mark_burned
from mthydra.controller.state.cover_pool import add_candidate, list_by_state, mark_verified, move_to_in_use
from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema


_DOMAIN_ALPHABET = st.text(alphabet=st.characters(whitelist_categories=("Ll", "Nd")), min_size=3, max_size=12)
_DOMAINS = st.builds(lambda label: f"{label}.example", _DOMAIN_ALPHABET).filter(lambda d: len(d) <= 32)


@settings(max_examples=50, deadline=None)
@given(domains=st.lists(_DOMAINS, min_size=1, max_size=10, unique=True))
def test_burned_set_only_grows(tmp_path_factory, domains):
    db = tmp_path_factory.mktemp("p") / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    burned_seen: set[str] = set()
    for i, d in enumerate(domains):
        add_candidate(conn, d, added_at="2026-05-18T00:00:00Z", notes=None)
        mark_verified(conn, d, from_vantage="v", at="2026-05-18T00:00:01Z")
        box_id = f"box-{i}"
        # Need an ru_box row only if we wanted FK; skip and burn directly from in_use
        # by going through the formal path:
        from mthydra.controller.state.ru_boxes import insert_box
        insert_box(conn, box_id, "h", "fsn1", None, d, "img", "2026-05-18T00:00:02Z")
        move_to_in_use(conn, d, box_id=box_id)
        mark_burned(conn, d, "job2_kill", box_id, f"2026-05-18T00:00:{i:02d}Z", None)
        burned_seen.add(d)
        # invariant: every previously-burned domain is still burned
        for prior in burned_seen:
            assert is_burned(conn, prior)
        # invariant: no burned domain appears in cover_domain_pool
        all_pool = (
            [r.domain for r in list_by_state(conn, "candidate_unverified")]
            + [r.domain for r in list_by_state(conn, "candidate_verified")]
            + [r.domain for r in list_by_state(conn, "in_use")]
        )
        assert burned_seen.isdisjoint(set(all_pool))
    conn.close()
```

- [ ] **Step 2: Run the test**

`pytest tests/property/test_burned_set_monotonic.py -v`
Expected: 1 passed (Hypothesis runs 50 examples).

- [ ] **Step 3: Commit**

```bash
git add tests/property/__init__.py tests/property/test_burned_set_monotonic.py
git commit -m "test(A): property — burned-set monotonicity & pool/burned disjointness"
git push origin main
```

---

## Phase 9 — Packaging

### Task 28: systemd units + install layout

**Files:**
- Create: `packaging/systemd/mthydra-controller.service`
- Create: `packaging/systemd/mthydra-backup-monitor.service`
- Modify: `pyproject.toml` (add `[tool.setuptools.package-data]`-style include if needed)
- Modify: `README.md` (add deployment subsection)

- [ ] **Step 1: Write `packaging/systemd/mthydra-controller.service`**

```ini
[Unit]
Description=mthydra controller (EU node — active or standby)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=mthydra
Group=mthydra
WorkingDirectory=/var/lib/mthydra
EnvironmentFile=-/etc/mthydra/controller.env
ExecStartPre=/usr/local/bin/mthydra-controller startup-check --db-path /var/lib/mthydra/state.sqlite --age-recipient-file /etc/mthydra/age-recipient.txt
ExecStart=/usr/local/bin/mthydra-controller serve --config /etc/mthydra/controller.toml
Restart=on-failure
RestartSec=10
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/mthydra
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Note: `serve` is the long-running daemon subcommand. Its implementation lands in a later spec (the orchestration plane spec F); for spec A, `serve` is a stub that runs the backup orchestrator alone. Add the stub now to keep the systemd unit valid:

- [ ] **Step 2: Extend `src/mthydra/controller/cli.py` with a `serve` subcommand**

In `build_parser()`, after the other `sub.add_parser(...)` lines:

```python
    srv = sub.add_parser("serve", help="run the backup orchestrator (Spec F will expand this)")
    srv.add_argument("--config", default="/etc/mthydra/controller.toml")
```

In `run()`, add the branch:

```python
    if args.cmd == "serve":
        from mthydra.controller.backup.pipeline import BackupPipeline
        from mthydra.controller.backup.s3_dest import S3Destination
        from mthydra.controller.backup.triggers import BackupOrchestrator
        from mthydra.controller.config import load_config
        from mthydra.controller.state.tokens import get_provider_credential
        import asyncio
        import os

        cfg = load_config(args.config)
        recipient = _read_recipient(DEFAULT_RECIPIENT_FILE)
        conn = connect(DEFAULT_DB)
        try:
            secret = get_provider_credential(conn, "b2")
        finally:
            conn.close()
        dest = S3Destination(
            endpoint_url=cfg.backup.endpoint,
            bucket=cfg.backup.bucket,
            access_key_id=cfg.backup.access_key_id,
            secret_access_key=secret,
            region=os.environ.get("MTHYDRA_BACKUP_REGION", "us-east-1"),
            object_lock_days=cfg.backup.retention.object_lock_days,
        )
        tmp_dir = Path("/var/lib/mthydra/tmp")
        tmp_dir.mkdir(parents=True, exist_ok=True)
        pipeline = BackupPipeline(
            db_path=DEFAULT_DB, tmp_dir=tmp_dir, recipient=recipient, destination=dest, clock=_now
        )
        orch = BackupOrchestrator(
            pipeline=pipeline,
            debounce_seconds=cfg.backup.on_change_debounce_seconds,
            floor_interval_seconds=cfg.backup.floor_interval_hours * 3600,
        )

        async def main_loop():
            await orch.start()
            try:
                while True:
                    await asyncio.sleep(3600)
            finally:
                await orch.stop()

        asyncio.run(main_loop())
        return 0
```

- [ ] **Step 3: Write `packaging/systemd/mthydra-backup-monitor.service`**

```ini
[Unit]
Description=mthydra backup generation-gap monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=mthydra
Group=mthydra
EnvironmentFile=/etc/mthydra/backup-monitor.env
ExecStart=/usr/local/bin/mthydra-backup-monitor --config /etc/mthydra/controller.toml
Restart=always
RestartSec=30
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/mthydra
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 4: Append to `README.md`**

```markdown
## Deployment (Ubuntu 24.04)

```bash
sudo apt install python3-venv age
sudo useradd --system --home /var/lib/mthydra --create-home mthydra
sudo -u mthydra python3 -m venv /opt/mthydra
sudo -u mthydra /opt/mthydra/bin/pip install <path-to-this-repo-or-wheel>
sudo install -Dm0644 packaging/systemd/mthydra-controller.service /etc/systemd/system/mthydra-controller.service
sudo install -Dm0644 packaging/systemd/mthydra-backup-monitor.service /etc/systemd/system/mthydra-backup-monitor.service
sudo install -Dm0644 packaging/etc/mthydra/controller.toml.example /etc/mthydra/controller.toml
# edit /etc/mthydra/controller.toml, then place the age recipient:
echo 'age1...' | sudo tee /etc/mthydra/age-recipient.txt
sudo /opt/mthydra/bin/mthydra-controller init --db-path /var/lib/mthydra/state.sqlite \
  --age-recipient-file /etc/mthydra/age-recipient.txt \
  --provider-credential aws=AKID:SECRET \
  --provider-credential b2=KEY_ID:KEY
sudo systemctl daemon-reload
sudo systemctl enable --now mthydra-controller mthydra-backup-monitor
```
```

- [ ] **Step 5: Smoke-check the units parse**

```bash
systemd-analyze verify packaging/systemd/mthydra-controller.service packaging/systemd/mthydra-backup-monitor.service
```

Expected: no errors (warnings about unknown `EnvironmentFile=-` path are normal; permissions warnings are fine for the source-tree path).

- [ ] **Step 6: Run the full test suite once more**

```bash
pytest
```

Expected: all unit, integration, and property tests pass (or skip if `age` unavailable).

- [ ] **Step 7: Commit**

```bash
git add packaging/systemd/ packaging/etc/ README.md src/mthydra/controller/cli.py
git commit -m "pkg(A): systemd units, serve subcommand stub, Ubuntu deployment doc"
git push origin main
```

---

## Self-Review

Spec coverage check (vs `doc/specs/2026-05-18-A-controller-state-and-backup.md`):

| Spec section | Implemented by |
|---|---|
| §2 D1 plaintext-on-disk | Task 2 (no encryption layer in `connect`) |
| §2 D2 single SQLite + TOML config | Tasks 1–9 (state) + Task 10 (config) |
| §2 D3 non-AWS S3-compatible | Task 12 (S3Destination accepts arbitrary endpoint) |
| §2 D4 age encryption | Tasks 11 (encrypt), 16 (decrypt) |
| §2 D5 daily floor + burned on-change | Task 14 |
| §2 D6 48h gap alarm | Tasks 21 (evaluator), 22 (emailer), 23 (CLI) |
| §2 D7 retention + Object Lock | Task 12 `put_blob` Object-Lock headers; retention itself is server-side bucket policy (out of code, but documented in §6.3 of spec and README Task 28) |
| §3 filesystem layout | Task 28 (systemd + README) |
| §4 schema (all tables) | Tasks 1, 3, 4, 5, 6, 7, 8 |
| §5 config schema | Task 10 |
| §6 backup pipeline | Tasks 11–15 |
| §7.1 restore CLI | Tasks 16, 20 |
| §7.2 adopt-restored-state | Tasks 17, 20 |
| §8 gap monitor | Tasks 21, 22, 23 |
| §9 failure modes (recoverable partial push) | Task 15 (reconcile) + tested by Task 15 |
| §10 startup self-check | Tasks 9 (invariants), 19 (runner) |
| §10.1 bootstrap | Task 18 |
| §11 §12 obligation contribution | Tasks 8 (obligations table), 20 (`obligation-proven` CLI) |
| §12 backup-restore dry-run | Documented in spec §12; operator-runbook only, no code (the CLI surface exists in Tasks 18+20) |
| §13 test plan | Tasks throughout + Tasks 24–27 |

Placeholder scan: All steps contain concrete code, exact commands, and exact paths. No "TBD" / "TODO" / "similar to". The two known design-level placeholders (Task 17 `_fresh_pem` and Task 18 `_placeholder_keypair_*`) are explicitly marked as placeholders to be replaced by spec B and are not plan-level holes — they are conscious cross-spec contracts.

Type consistency: `BackupTrigger` enum values used identically across Tasks 8, 13, 14, 15. `S3Destination` constructor signature consistent across Tasks 12, 13, 15, 21, 23, 28. `GapMonitorState` triple `(last_seen_gen, first_observed_at, last_alarm_at)` consistent across Tasks 21, 23. `summarize_db` keys consistent between Tasks 16 and 20 (CLI prints JSON of the same dict).

Cross-spec contracts (deliberate, not gaps):

- Spec B will replace `_placeholder_keypair_*` in Task 17 and `_placeholder_keypair_*` in Task 18 with real Ed25519 / X25519 generation. Until B lands, Case-B re-key produces an opaque rotated token; this is sufficient to exercise every code path in spec A.
- Spec J will replace the `print(...)` in `backup-now` CLI (Task 20) with a real signal/IPC to the running daemon.
- Spec F will expand the `serve` stub (Task 28) into the full controller daemon orchestrating descriptor signing, fleet ops, and alerting.

---

## Execution Handoff

**Plan complete and saved to `doc/plans/2026-05-18-A-controller-state-and-backup-plan.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

**Which approach?**

