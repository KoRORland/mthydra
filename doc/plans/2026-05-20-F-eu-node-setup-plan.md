# Spec F — EU Node Setup — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the EU active/standby role split as defined in `doc/specs/2026-05-20-F-eu-node-setup.md`: DB-authoritative `node_state` singleton, `eu_nodes` inventory table, skeleton-DB standby mode, B2-heartbeat readiness, operator-driven `promote-active` CLI with Case A/B branching, drill-attestation obligation. Data-exit (Reality tunnel terminator) is explicitly deferred to spec F2.

**Architecture:** Schema v3→v4 forward migration adds two tables. `mthydra.controller.state.node_state` + `mthydra.controller.state.eu_nodes` are new thin repositories following the spec-A pattern (one module per table-group). Heartbeat code lives in a new `mthydra.controller.standby` package with two APScheduler classes mirroring the spec-C scheduler pattern. `S3Destination` (spec A) gets two new methods for keyed object access (heartbeats reuse the backup bucket under a distinct prefix). `promote-active` is the only complex piece — atomic state replacement with safety net via `.bak` rename. The serve loop branches on `node_state.role`.

**Tech stack:** Python 3.12 stdlib + APScheduler + boto3 + `cryptography`. No new runtime dependencies.

**Design decisions:** See spec §2 (F-D1 through F-D6).

---

## File Structure (locked before tasks)

**Modified:**
- `src/mthydra/controller/state/schema.py` — bump `SCHEMA_VERSION` to 4; add `_V4_MIGRATION`; add `migrate_v3_to_v4`; add DDL for `node_state` and `eu_nodes`.
- `src/mthydra/controller/state/invariants.py` — extend `check_all()` with invariants #21–#23.
- `src/mthydra/controller/config.py` — add `StandbyConfig` dataclass; load `[standby]` TOML section.
- `src/mthydra/controller/bootstrap.py` — accept `role` parameter; on `role='standby'`, seed only schema + B2 credential + `node_state`; on `role='active'` (default), preserve existing behaviour and seed `node_state` too.
- `src/mthydra/controller/cli.py` — `init --role` flag; six new subcommands (`eu-node-add`, `eu-node-retire`, `eu-node-list`, `standby-drill-proven`, `authority-rotate`, `promote-active`); role-gated `_cmd_serve`; bootstrap obligation list extended.
- `src/mthydra/controller/backup/s3_dest.py` — add `put_heartbeat`, `get_heartbeat`, `head_heartbeat` methods (Object Lock OFF for the heartbeat prefix is impossible with this bucket; we accept the per-overwrite versioning as the documented residual).
- `packaging/etc/mthydra/controller.toml.example` — add `[standby]` section.

**Created:**
- `src/mthydra/controller/state/node_state.py` — singleton role repository.
- `src/mthydra/controller/state/eu_nodes.py` — EU infrastructure inventory repository.
- `src/mthydra/controller/standby/__init__.py` — empty.
- `src/mthydra/controller/standby/heartbeat.py` — `StandbyHeartbeatPublisher` + `StandbyHeartbeatPoller`.
- `src/mthydra/controller/promote.py` — atomic state replacement logic for `promote-active`.
- `tests/unit/controller/state/test_node_state.py`
- `tests/unit/controller/state/test_eu_nodes.py`
- `tests/unit/controller/standby/__init__.py`
- `tests/unit/controller/standby/test_heartbeat_publisher.py`
- `tests/unit/controller/standby/test_heartbeat_poller.py`
- `tests/unit/controller/test_promote.py`
- `tests/integration/test_promotion_lifecycle.py`

Responsibility per file: each state module owns one table-group's CRUD. `standby/heartbeat.py` owns the heartbeat publish/poll plumbing. `promote.py` owns the atomic file-system + DB swap. CLI dispatch lives in `cli.py` and is thin — handlers delegate to the modules above.

---

## Phase 1 — Schema v3 → v4

### Task 1: Schema migration + DDL for `node_state` + `eu_nodes`

**Files:**
- Modify: `src/mthydra/controller/state/schema.py`
- Modify: `tests/unit/controller/state/test_schema.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/controller/state/test_schema.py`:

```python
def test_schema_version_is_4(tmp_db_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import SCHEMA_VERSION, apply_schema
    assert SCHEMA_VERSION == 4
    conn = connect(tmp_db_path)
    apply_schema(conn)
    row = conn.execute("SELECT version FROM schema_version WHERE rowid=1").fetchone()
    assert row[0] == 4


def test_node_state_table_present_and_seeded_active(tmp_db_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    conn = connect(tmp_db_path)
    apply_schema(conn)
    rows = conn.execute("SELECT role FROM node_state").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "active"


def test_node_state_singleton_rejects_second_row(tmp_db_path):
    import sqlite3
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    conn = connect(tmp_db_path)
    apply_schema(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO node_state (rowid, role) VALUES (2, 'standby')")
        conn.commit()


def test_eu_nodes_table_present(tmp_db_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    conn = connect(tmp_db_path)
    apply_schema(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(eu_nodes)").fetchall()]
    assert {"node_id", "hostname", "provider", "region", "public_ip",
            "role", "added_at", "promoted_at", "retired_at",
            "last_heartbeat_at", "last_heartbeat_b2_etag", "notes"} == set(cols)


def test_v3_to_v4_migration_seeds_node_state_active(tmp_db_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import migrate_v3_to_v4
    conn = connect(tmp_db_path)
    # Manually construct a v3 DB shell (just schema_version and minimal cover_pool tables).
    conn.executescript(
        "CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL, CHECK (rowid=1));"
        "INSERT INTO schema_version (rowid, version, applied_at) VALUES (1, 3, '2026-05-20T00:00:00Z');"
    )
    conn.commit()
    migrate_v3_to_v4(conn)
    v = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    assert v == 4
    role = conn.execute("SELECT role FROM node_state WHERE rowid=1").fetchone()[0]
    assert role == "active"
```

`pytest` import is already at top of `test_schema.py`.

- [ ] **Step 2: Run tests — expect failure**

```bash
cd /home/asharov/RedHat/Dev/mthydra
pytest tests/unit/controller/state/test_schema.py -v
```

Expected: 5 failures (SCHEMA_VERSION != 4; missing tables; missing migrate_v3_to_v4).

- [ ] **Step 3: Edit `src/mthydra/controller/state/schema.py`**

Bump version:

```python
SCHEMA_VERSION = 4
```

Append two new tables to `_STATEMENTS` (after the `# --- spec C additions ---` triggers):

```python
    # --- spec F additions: EU node setup (active / standby) ---
    """
    CREATE TABLE IF NOT EXISTS node_state (
      role                        TEXT NOT NULL CHECK (role IN ('active','standby')),
      promoted_at                 TEXT,
      previous_role               TEXT,
      promotion_case              TEXT CHECK (promotion_case IN ('A','B')),
      promotion_backup_generation INTEGER,
      CHECK (rowid = 1)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS eu_nodes (
      node_id                TEXT PRIMARY KEY,
      hostname               TEXT NOT NULL,
      provider               TEXT NOT NULL,
      region                 TEXT NOT NULL,
      public_ip              TEXT,
      role                   TEXT NOT NULL CHECK (role IN ('active','standby','retired')),
      added_at               TEXT NOT NULL,
      promoted_at            TEXT,
      retired_at             TEXT,
      last_heartbeat_at      TEXT,
      last_heartbeat_b2_etag TEXT,
      notes                  TEXT
    )
    """,
```

Add `migrate_v3_to_v4`:

```python
def migrate_v3_to_v4(conn: sqlite3.Connection) -> None:
    """Idempotent v3 → v4 migration: add node_state + eu_nodes; seed node_state='active'."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS node_state (
          role                        TEXT NOT NULL CHECK (role IN ('active','standby')),
          promoted_at                 TEXT,
          previous_role               TEXT,
          promotion_case              TEXT CHECK (promotion_case IN ('A','B')),
          promotion_backup_generation INTEGER,
          CHECK (rowid = 1)
        );
        CREATE TABLE IF NOT EXISTS eu_nodes (
          node_id                TEXT PRIMARY KEY,
          hostname               TEXT NOT NULL,
          provider               TEXT NOT NULL,
          region                 TEXT NOT NULL,
          public_ip              TEXT,
          role                   TEXT NOT NULL CHECK (role IN ('active','standby','retired')),
          added_at               TEXT NOT NULL,
          promoted_at            TEXT,
          retired_at             TEXT,
          last_heartbeat_at      TEXT,
          last_heartbeat_b2_etag TEXT,
          notes                  TEXT
        );
        """
    )
    # Seed singleton if absent. Existing v3 deployments are implicitly 'active'.
    existing = conn.execute("SELECT COUNT(*) FROM node_state").fetchone()[0]
    if existing == 0:
        conn.execute(
            "INSERT INTO node_state (rowid, role) VALUES (1, 'active')"
        )
    conn.execute(
        "UPDATE schema_version SET version=?, applied_at=? WHERE rowid=1",
        (4, _now()),
    )
    conn.commit()
```

Extend `apply_schema` dispatcher — after the existing `if current < 3: migrate_v2_to_v3(conn)` add:

```python
        if current < 4:
            migrate_v3_to_v4(conn)
```

And: `apply_schema` must seed `node_state` on a fresh (version-0) install. Update the `INSERT INTO schema_version` block to also seed `node_state` when the DB is brand new:

```python
    if existing == 0:
        conn.execute(
            "INSERT INTO schema_version (rowid, version, applied_at) VALUES (1, ?, ?)",
            (SCHEMA_VERSION, _now()),
        )
        # Fresh install: seed node_state singleton as active by default. Bootstrap
        # may overwrite to 'standby' if --role standby was passed.
        n = conn.execute("SELECT COUNT(*) FROM node_state").fetchone()[0]
        if n == 0:
            conn.execute("INSERT INTO node_state (rowid, role) VALUES (1, 'active')")
    else:
        ...  # existing migration dispatcher block
```

- [ ] **Step 4: Run schema tests**

```bash
pytest tests/unit/controller/state/test_schema.py -v
```

Expected: PASS (5 new + existing).

- [ ] **Step 5: Run full unit suite for regressions**

```bash
pytest tests/unit -q
```

If existing invariant tests fail because they `_seeded(tmp_db_path)` which now lands on schema v4 — that's a side-effect of the v4 schema requiring `node_state` to exist for any active-checking invariants. The existing tests should still pass because `apply_schema` auto-seeds `node_state='active'` on fresh DBs.

- [ ] **Step 6: Commit**

```bash
git add src/mthydra/controller/state/schema.py tests/unit/controller/state/test_schema.py
git commit -m "schema(F): v3→v4 migration — node_state singleton + eu_nodes inventory"
```

---

## Phase 2 — Repositories

### Task 2: `node_state` repository

**Files:**
- Create: `src/mthydra/controller/state/node_state.py`
- Create: `tests/unit/controller/state/test_node_state.py`

- [ ] **Step 1: Write failing tests**

```python
"""Spec F — node_state singleton repository."""
import pytest

from mthydra.controller.state.audit import recent_events
from mthydra.controller.state.db import connect
from mthydra.controller.state.node_state import (
    NodeState, current_node_state, set_node_role,
)
from mthydra.controller.state.schema import apply_schema


@pytest.fixture
def conn(tmp_db_path):
    c = connect(tmp_db_path)
    apply_schema(c)
    return c


def test_current_node_state_returns_seeded_active(conn):
    ns = current_node_state(conn)
    assert ns.role == "active"
    assert ns.promoted_at is None
    assert ns.previous_role is None


def test_set_node_role_to_active_after_promotion(conn):
    set_node_role(
        conn, role="active",
        promoted_at="2026-05-20T01:00:00Z",
        previous_role="standby",
        promotion_case="A",
        promotion_backup_generation=42,
    )
    ns = current_node_state(conn)
    assert ns.role == "active"
    assert ns.promoted_at == "2026-05-20T01:00:00Z"
    assert ns.previous_role == "standby"
    assert ns.promotion_case == "A"
    assert ns.promotion_backup_generation == 42


def test_set_node_role_emits_audit(conn):
    set_node_role(
        conn, role="active",
        promoted_at="2026-05-20T01:00:00Z",
        previous_role="standby",
        promotion_case="B",
        promotion_backup_generation=99,
    )
    ev = recent_events(conn, limit=1)
    assert ev[0].action == "node_role_set"
    assert "active" in (ev[0].details_json or "")


def test_set_node_role_rejects_invalid_role(conn):
    with pytest.raises(ValueError, match="role"):
        set_node_role(
            conn, role="invalid",
            promoted_at=None, previous_role=None,
            promotion_case=None, promotion_backup_generation=None,
        )
```

- [ ] **Step 2: Run tests — expect ImportError**

```bash
pytest tests/unit/controller/state/test_node_state.py -v
```

- [ ] **Step 3: Create `src/mthydra/controller/state/node_state.py`**

```python
"""Spec F — node_state singleton repository.

The node_state table is a single-row description of *this* node's runtime
role. It is the DB-authoritative source for active/standby; controller.toml
carries only deploy-time hints.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from mthydra.controller.state.audit import log_event

_VALID_ROLES = {"active", "standby"}


@dataclass(frozen=True)
class NodeState:
    role: str
    promoted_at: str | None
    previous_role: str | None
    promotion_case: str | None
    promotion_backup_generation: int | None


def current_node_state(conn: sqlite3.Connection) -> NodeState:
    row = conn.execute(
        "SELECT role, promoted_at, previous_role, promotion_case, "
        "       promotion_backup_generation "
        "FROM node_state WHERE rowid=1"
    ).fetchone()
    if row is None:
        raise LookupError("node_state singleton missing — DB not initialised")
    return NodeState(*row)


def set_node_role(
    conn: sqlite3.Connection,
    *,
    role: str,
    promoted_at: str | None,
    previous_role: str | None,
    promotion_case: str | None,
    promotion_backup_generation: int | None,
    actor: str = "operator",
) -> None:
    """Update the singleton row. Emits one audit_log entry.

    The CHECK constraints on the table reject invalid role / promotion_case;
    this function pre-validates to surface a friendlier ValueError.
    """
    if role not in _VALID_ROLES:
        raise ValueError(f"role must be one of {_VALID_ROLES}, got {role!r}")
    if promotion_case is not None and promotion_case not in {"A", "B"}:
        raise ValueError(f"promotion_case must be 'A' or 'B', got {promotion_case!r}")

    conn.execute(
        "UPDATE node_state SET role=?, promoted_at=?, previous_role=?, "
        "       promotion_case=?, promotion_backup_generation=? "
        "WHERE rowid=1",
        (role, promoted_at, previous_role, promotion_case, promotion_backup_generation),
    )
    log_event(
        conn,
        ts=promoted_at or "1970-01-01T00:00:00Z",
        actor=actor,
        action="node_role_set",
        target=role,
        details_json=json.dumps({
            "role": role,
            "promoted_at": promoted_at,
            "previous_role": previous_role,
            "promotion_case": promotion_case,
            "promotion_backup_generation": promotion_backup_generation,
        }, separators=(",", ":")),
    )
    conn.commit()
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/unit/controller/state/test_node_state.py -v
```

Expected: PASS (4/4).

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/state/node_state.py tests/unit/controller/state/test_node_state.py
git commit -m "state(F): node_state singleton repository"
```

---

### Task 3: `eu_nodes` repository

**Files:**
- Create: `src/mthydra/controller/state/eu_nodes.py`
- Create: `tests/unit/controller/state/test_eu_nodes.py`

- [ ] **Step 1: Write failing tests**

```python
"""Spec F — eu_nodes inventory repository."""
import pytest

from mthydra.controller.state.audit import recent_events
from mthydra.controller.state.db import connect
from mthydra.controller.state.eu_nodes import (
    EUNode, add_eu_node, get_eu_node, list_eu_nodes, retire_eu_node,
    update_heartbeat,
)
from mthydra.controller.state.schema import apply_schema


@pytest.fixture
def conn(tmp_db_path):
    c = connect(tmp_db_path)
    apply_schema(c)
    return c


NOW = "2026-05-20T00:00:00Z"


def test_add_eu_node_default_role_is_standby(conn):
    add_eu_node(
        conn, node_id="eu-standby-de-1", hostname="standby.example",
        provider="hetzner", region="de", added_at=NOW,
    )
    n = get_eu_node(conn, "eu-standby-de-1")
    assert n.role == "standby"
    assert n.hostname == "standby.example"


def test_add_eu_node_active_role(conn):
    add_eu_node(
        conn, node_id="eu-active-fr-1", hostname="active.example",
        provider="aws", region="fr", role="active", added_at=NOW,
    )
    n = get_eu_node(conn, "eu-active-fr-1")
    assert n.role == "active"


def test_add_eu_node_refuses_second_active(conn):
    add_eu_node(conn, node_id="eu-active-fr-1", hostname="a", provider="aws",
                region="fr", role="active", added_at=NOW)
    with pytest.raises(ValueError, match="only one active"):
        add_eu_node(conn, node_id="eu-active-fr-2", hostname="b", provider="aws",
                    region="fr", role="active", added_at=NOW)


def test_add_eu_node_emits_audit(conn):
    add_eu_node(conn, node_id="eu-standby-de-1", hostname="h", provider="hetzner",
                region="de", added_at=NOW)
    ev = recent_events(conn, limit=1)
    assert ev[0].action == "eu_node_added"
    assert ev[0].target == "eu-standby-de-1"


def test_retire_eu_node(conn):
    add_eu_node(conn, node_id="eu-standby-de-1", hostname="h", provider="hetzner",
                region="de", added_at=NOW)
    retire_eu_node(conn, "eu-standby-de-1", at="2026-05-21T00:00:00Z")
    n = get_eu_node(conn, "eu-standby-de-1")
    assert n.role == "retired"
    assert n.retired_at == "2026-05-21T00:00:00Z"


def test_update_heartbeat_idempotent_on_same_etag(conn):
    add_eu_node(conn, node_id="eu-standby-de-1", hostname="h", provider="hetzner",
                region="de", added_at=NOW)
    update_heartbeat(conn, "eu-standby-de-1", at="2026-05-20T01:00:00Z", b2_etag="abc")
    n_first = get_eu_node(conn, "eu-standby-de-1")
    audit_count_first = len(recent_events(conn, limit=20))

    update_heartbeat(conn, "eu-standby-de-1", at="2026-05-20T01:01:00Z", b2_etag="abc")
    n_second = get_eu_node(conn, "eu-standby-de-1")
    audit_count_second = len(recent_events(conn, limit=20))

    # Same ETag → no update, no audit churn
    assert n_first.last_heartbeat_at == "2026-05-20T01:00:00Z"
    assert n_second.last_heartbeat_at == "2026-05-20T01:00:00Z"  # unchanged
    assert audit_count_first == audit_count_second


def test_update_heartbeat_writes_on_new_etag(conn):
    add_eu_node(conn, node_id="eu-standby-de-1", hostname="h", provider="hetzner",
                region="de", added_at=NOW)
    update_heartbeat(conn, "eu-standby-de-1", at="2026-05-20T01:00:00Z", b2_etag="abc")
    update_heartbeat(conn, "eu-standby-de-1", at="2026-05-20T01:01:00Z", b2_etag="xyz")
    n = get_eu_node(conn, "eu-standby-de-1")
    assert n.last_heartbeat_at == "2026-05-20T01:01:00Z"
    assert n.last_heartbeat_b2_etag == "xyz"


def test_list_eu_nodes_filters_by_role(conn):
    add_eu_node(conn, node_id="a", hostname="h", provider="p", region="r",
                role="active", added_at=NOW)
    add_eu_node(conn, node_id="s1", hostname="h", provider="p", region="r",
                role="standby", added_at=NOW)
    add_eu_node(conn, node_id="s2", hostname="h", provider="p", region="r",
                role="standby", added_at=NOW)
    standbys = list_eu_nodes(conn, role="standby")
    assert {n.node_id for n in standbys} == {"s1", "s2"}
```

- [ ] **Step 2: Run tests — expect ImportError**

- [ ] **Step 3: Create `src/mthydra/controller/state/eu_nodes.py`**

```python
"""Spec F — eu_nodes inventory repository."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from mthydra.controller.state.audit import log_event


@dataclass(frozen=True)
class EUNode:
    node_id: str
    hostname: str
    provider: str
    region: str
    public_ip: str | None
    role: str
    added_at: str
    promoted_at: str | None
    retired_at: str | None
    last_heartbeat_at: str | None
    last_heartbeat_b2_etag: str | None
    notes: str | None


_COLS = (
    "node_id, hostname, provider, region, public_ip, role, added_at, "
    "promoted_at, retired_at, last_heartbeat_at, last_heartbeat_b2_etag, notes"
)


def add_eu_node(
    conn: sqlite3.Connection,
    *,
    node_id: str,
    hostname: str,
    provider: str,
    region: str,
    added_at: str,
    role: str = "standby",
    public_ip: str | None = None,
    notes: str | None = None,
    actor: str = "operator",
) -> None:
    """Insert a new EU node row.

    Refuses to insert a second role='active' row (split-brain by definition).
    """
    if role == "active":
        existing = conn.execute(
            "SELECT node_id FROM eu_nodes WHERE role='active'"
        ).fetchone()
        if existing is not None:
            raise ValueError(
                f"only one active EU node permitted (existing: {existing[0]!r})"
            )
    conn.execute(
        "INSERT INTO eu_nodes (node_id, hostname, provider, region, public_ip, "
        "                      role, added_at, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (node_id, hostname, provider, region, public_ip, role, added_at, notes),
    )
    log_event(
        conn, ts=added_at, actor=actor, action="eu_node_added",
        target=node_id,
        details_json=json.dumps({
            "hostname": hostname, "provider": provider, "region": region,
            "role": role,
        }, separators=(",", ":")),
    )
    conn.commit()


def retire_eu_node(
    conn: sqlite3.Connection,
    node_id: str,
    *,
    at: str,
    actor: str = "operator",
) -> None:
    cur = conn.execute(
        "UPDATE eu_nodes SET role='retired', retired_at=? WHERE node_id=? AND role != 'retired'",
        (at, node_id),
    )
    if cur.rowcount == 0:
        raise ValueError(f"eu node {node_id!r} not found or already retired")
    log_event(
        conn, ts=at, actor=actor, action="eu_node_retired",
        target=node_id, details_json=None,
    )
    conn.commit()


def update_heartbeat(
    conn: sqlite3.Connection,
    node_id: str,
    *,
    at: str,
    b2_etag: str,
) -> None:
    """Update last_heartbeat_at + etag. Idempotent on identical etag (no audit churn)."""
    row = conn.execute(
        "SELECT last_heartbeat_b2_etag FROM eu_nodes WHERE node_id=?", (node_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"eu node {node_id!r} not in inventory")
    if row[0] == b2_etag:
        return  # no change; suppress audit
    conn.execute(
        "UPDATE eu_nodes SET last_heartbeat_at=?, last_heartbeat_b2_etag=? WHERE node_id=?",
        (at, b2_etag, node_id),
    )
    conn.commit()


def get_eu_node(conn: sqlite3.Connection, node_id: str) -> EUNode:
    row = conn.execute(
        f"SELECT {_COLS} FROM eu_nodes WHERE node_id=?", (node_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"eu node {node_id!r} not found")
    return EUNode(*row)


def list_eu_nodes(
    conn: sqlite3.Connection,
    *,
    role: str | None = None,
) -> list[EUNode]:
    if role is None:
        rows = conn.execute(
            f"SELECT {_COLS} FROM eu_nodes ORDER BY node_id"
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {_COLS} FROM eu_nodes WHERE role=? ORDER BY node_id", (role,)
        ).fetchall()
    return [EUNode(*r) for r in rows]
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/unit/controller/state/test_eu_nodes.py -v
```

Expected: PASS (8/8).

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/state/eu_nodes.py tests/unit/controller/state/test_eu_nodes.py
git commit -m "state(F): eu_nodes inventory repository"
```

---

### Task 4: Invariants #21–#23

**Files:**
- Modify: `src/mthydra/controller/state/invariants.py`
- Modify: `tests/unit/controller/state/test_invariants.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/controller/state/test_invariants.py`:

```python
# ---------------------------------------------------------------------------
# Spec F invariant checks (#21–#23)
# ---------------------------------------------------------------------------

def test_check_21_rejects_missing_node_state(tmp_db_path):
    conn = _seeded(tmp_db_path)
    conn.execute("DELETE FROM node_state")
    conn.commit()
    with pytest.raises(InvariantViolation, match="check 21"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_22_active_requires_authority(tmp_db_path):
    conn = _seeded(tmp_db_path)
    # _seeded() inserts authority+key; node_state default 'active'. Retire authority.
    conn.execute("UPDATE credential_authority SET retired_at=?", (NOW,))
    conn.commit()
    with pytest.raises(InvariantViolation, match="check 22"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_23_standby_must_be_skeleton(tmp_db_path):
    """A standby with a credential_authority row is structurally invalid."""
    conn = _seeded(tmp_db_path)  # has authority + key
    # Flip to standby — but the authority row is still there, violating #23.
    conn.execute("UPDATE node_state SET role='standby' WHERE rowid=1")
    conn.commit()
    with pytest.raises(InvariantViolation, match="check 23"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_23_standby_with_only_b2_credential_passes(tmp_db_path):
    """The skeleton-DB invariant has one carve-out: B2 provider credential."""
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    from mthydra.controller.state.tokens import set_provider_credential
    # Build a true skeleton DB (no _seeded helper — that inserts authority).
    conn = connect(tmp_db_path)
    apply_schema(conn)
    conn.execute("UPDATE node_state SET role='standby' WHERE rowid=1")
    conn.commit()
    set_provider_credential(conn, provider="b2", credential="id:secret", at=NOW)
    # Must NOT raise.
    check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_23_standby_with_non_b2_credential_fails(tmp_db_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    from mthydra.controller.state.tokens import set_provider_credential
    conn = connect(tmp_db_path)
    apply_schema(conn)
    conn.execute("UPDATE node_state SET role='standby' WHERE rowid=1")
    conn.commit()
    set_provider_credential(conn, provider="aws", credential="id:secret", at=NOW)
    with pytest.raises(InvariantViolation, match="check 23"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/unit/controller/state/test_invariants.py -k 'check_21 or check_22 or check_23' -v
```

- [ ] **Step 3: Append invariants to `check_all` in `src/mthydra/controller/state/invariants.py`** (at end of function):

```python
    # --- spec F checks (#21–#23) ---

    # Check 21: node_state singleton exists
    n = _scalar(conn, "SELECT COUNT(*) FROM node_state")
    if n != 1:
        raise InvariantViolation(f"check 21: node_state must have exactly 1 row, found {n}")

    role_row = conn.execute("SELECT role FROM node_state WHERE rowid=1").fetchone()
    role = role_row[0]

    # Check 22: active role requires non-retired authority + signing key
    if role == "active":
        a = _scalar(
            conn,
            "SELECT COUNT(*) FROM credential_authority WHERE retired_at IS NULL",
        )
        k = _scalar(
            conn,
            "SELECT COUNT(*) FROM descriptor_signing_key WHERE retired_at IS NULL",
        )
        if a < 1 or k < 1:
            raise InvariantViolation(
                f"check 22: active node requires authority + signing key "
                f"(authority={a}, signing_key={k})"
            )

    # Check 23: standby is skeleton (no live state, except B2 provider credential)
    if role == "standby":
        forbidden_tables = (
            "credential_authority",
            "descriptor_signing_key",
            "descriptor_history",
            "publishing_tokens",
            "cover_domain_pool",
            "burned_domains",
            "eu_exit_set",
        )
        for tbl in forbidden_tables:
            cnt = _scalar(conn, f"SELECT COUNT(*) FROM {tbl}")
            if cnt > 0:
                raise InvariantViolation(
                    f"check 23: standby DB must be skeleton; {tbl} has {cnt} row(s)"
                )
        # provider_api_credentials: B2 only carve-out
        non_b2 = _scalar(
            conn,
            "SELECT COUNT(*) FROM provider_api_credentials WHERE provider != 'b2'",
        )
        if non_b2 > 0:
            raise InvariantViolation(
                f"check 23: standby may hold only B2 provider credentials; "
                f"found {non_b2} non-B2 row(s)"
            )
```

- [ ] **Step 4: Run invariant tests**

```bash
pytest tests/unit/controller/state/test_invariants.py -v
```

Expected: PASS.

- [ ] **Step 5: Run full unit suite**

```bash
pytest tests/unit -q
```

- [ ] **Step 6: Commit**

```bash
git add src/mthydra/controller/state/invariants.py tests/unit/controller/state/test_invariants.py
git commit -m "invariants(F): startup checks 21-23 (singleton, active-needs-authority, standby-skeleton)"
```

---

## Phase 3 — Config

### Task 5: `StandbyConfig` dataclass + TOML parsing

**Files:**
- Modify: `src/mthydra/controller/config.py`
- Modify: `packaging/etc/mthydra/controller.toml.example`
- Modify: `tests/unit/controller/test_config.py`

- [ ] **Step 1: Append failing test to `tests/unit/controller/test_config.py`**

Reuse the existing `_MIN_TOML` helper if present; otherwise add a focused test:

```python
def test_load_standby_config(tmp_path):
    from mthydra.controller.config import load_config

    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(
        "[node]\nrole='standby'\nhostname='h'\n"
        "[backup]\nfloor_interval_hours=24\non_change_debounce_seconds=30\n"
        "endpoint='https://example'\nbucket='b'\naccess_key_id='k'\n"
        "[backup.retention]\nkeep_daily=30\nkeep_monthly=12\nobject_lock_days=365\n"
        "[gap_monitor]\npoll_interval_minutes=30\nalarm_threshold_hours=48\n"
        "recipient_email='op@example.org'\n"
        "[descriptor]\nrotation_interval_hours=1\nvalidity_window_hours=24\n"
        "[obligations]\n[obligations.timers_hours]\n"
        "[cover_pool]\n"
        "rotation_ttl_days=14\nreverify_after_days=30\nfreeze_threshold=2\n"
        "reverify_sweep_interval='1h'\nrotation_sweep_interval='1h'\n"
        "replenishment_interval_days=90\n"
        "[standby]\nnode_id='eu-standby-de-1'\n"
        "heartbeat_interval_seconds=60\n"
        "heartbeat_poll_interval='5m'\n"
        "staleness_alert_seconds=600\n"
    )
    cfg = load_config(cfg_path)
    assert cfg.standby.node_id == "eu-standby-de-1"
    assert cfg.standby.heartbeat_interval_seconds == 60
    assert cfg.standby.heartbeat_poll_interval_seconds == 300
    assert cfg.standby.staleness_alert_seconds == 600


def test_load_config_standby_section_defaults(tmp_path):
    """Missing [standby] section: load with safe defaults."""
    from mthydra.controller.config import load_config

    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(
        "[node]\nrole='active'\nhostname='h'\n"
        "[backup]\nfloor_interval_hours=24\non_change_debounce_seconds=30\n"
        "endpoint='https://example'\nbucket='b'\naccess_key_id='k'\n"
        "[backup.retention]\nkeep_daily=30\nkeep_monthly=12\nobject_lock_days=365\n"
        "[gap_monitor]\npoll_interval_minutes=30\nalarm_threshold_hours=48\n"
        "recipient_email='op@example.org'\n"
        "[descriptor]\nrotation_interval_hours=1\nvalidity_window_hours=24\n"
        "[obligations]\n[obligations.timers_hours]\n"
        "[cover_pool]\nrotation_ttl_days=14\nreverify_after_days=30\n"
        "freeze_threshold=2\nreverify_sweep_interval='1h'\n"
        "rotation_sweep_interval='1h'\nreplenishment_interval_days=90\n"
    )
    cfg = load_config(cfg_path)
    assert cfg.standby.node_id == ""  # empty default
    assert cfg.standby.heartbeat_interval_seconds == 60
    assert cfg.standby.heartbeat_poll_interval_seconds == 300
    assert cfg.standby.staleness_alert_seconds == 600
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/unit/controller/test_config.py::test_load_standby_config -v
```

- [ ] **Step 3: Modify `src/mthydra/controller/config.py`**

Add the dataclass below `CoverPoolConfig`:

```python
@dataclass(frozen=True)
class StandbyConfig:
    node_id: str
    heartbeat_interval_seconds: int
    heartbeat_poll_interval_seconds: int
    staleness_alert_seconds: int
```

Add to `Config`:

```python
@dataclass(frozen=True)
class Config:
    node: NodeConfig
    backup: BackupConfig
    gap_monitor: GapMonitorConfig
    obligations: ObligationsConfig
    descriptor: DescriptorConfig
    cover_pool: CoverPoolConfig
    standby: StandbyConfig
```

Add a parser (the `_parse_interval_seconds` helper from spec C handles `"5m"` etc.):

```python
def _load_standby(data: dict) -> StandbyConfig:
    sec = data.get("standby", {})
    return StandbyConfig(
        node_id=str(sec.get("node_id", "")),
        heartbeat_interval_seconds=_require_positive(
            "standby.heartbeat_interval_seconds",
            sec.get("heartbeat_interval_seconds", 60),
        ),
        heartbeat_poll_interval_seconds=_parse_interval_seconds(
            "standby.heartbeat_poll_interval",
            sec.get("heartbeat_poll_interval", 300),
        ),
        staleness_alert_seconds=_require_positive(
            "standby.staleness_alert_seconds",
            sec.get("staleness_alert_seconds", 600),
        ),
    )
```

Wire `standby=_load_standby(data)` into the `Config(...)` constructor call in `load_config`.

- [ ] **Step 4: Append to `packaging/etc/mthydra/controller.toml.example`**

```toml

[standby]
node_id                      = ""        # set on standby hosts only; eu-standby-<region>-<n>
heartbeat_interval_seconds   = 60
heartbeat_poll_interval      = "5m"
staleness_alert_seconds      = 600
```

- [ ] **Step 5: Run config tests**

```bash
pytest tests/unit/controller/test_config.py -v
```

- [ ] **Step 6: Run full suite (catches any `Config` literal in test_cli.py needing the new field)**

```bash
pytest tests/unit -q
```

If `test_build_destination_uses_override_bucket_in_dryrun` or other tests construct `Config(...)` literally, add a default `StandbyConfig(node_id="", heartbeat_interval_seconds=60, heartbeat_poll_interval_seconds=300, staleness_alert_seconds=600)` to the literal.

- [ ] **Step 7: Commit**

```bash
git add src/mthydra/controller/config.py packaging/etc/mthydra/controller.toml.example tests/unit/controller/test_config.py tests/unit/controller/test_cli.py
git commit -m "config(F): StandbyConfig dataclass + [standby] TOML section"
```

---

## Phase 4 — Bootstrap (init --role)

### Task 6: `init_state` accepts `role`; new skeleton path

**Files:**
- Modify: `src/mthydra/controller/bootstrap.py`
- Modify: `src/mthydra/controller/cli.py`
- Modify: `tests/unit/controller/test_bootstrap.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/controller/test_bootstrap.py`:

```python
def test_init_state_standby_creates_skeleton(tmp_path, recipient):
    """Standby init seeds only schema + B2 credential + node_state; no authority, no keys."""
    db = tmp_path / "state.sqlite"
    init_state(
        db_path=db,
        age_recipient=recipient,
        provider_credentials={"b2": "id:secret"},
        obligation_timer_hours={},  # standby seeds no obligations from bootstrap
        now="2026-05-20T00:00:00Z",
        role="standby",
    )
    from mthydra.controller.state.authority import list_authorities
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.node_state import current_node_state
    conn = connect(db)
    assert list_authorities(conn) == []
    # descriptor_signing_key empty too
    cnt = conn.execute("SELECT COUNT(*) FROM descriptor_signing_key").fetchone()[0]
    assert cnt == 0
    ns = current_node_state(conn)
    assert ns.role == "standby"
    # B2 credential is the one carve-out
    creds = conn.execute("SELECT provider FROM provider_api_credentials").fetchall()
    assert creds == [("b2",)]


def test_init_state_active_default_role(tmp_path, recipient):
    """Active init (no --role) seeds full state and node_state='active'."""
    db = tmp_path / "state.sqlite"
    init_state(
        db_path=db,
        age_recipient=recipient,
        provider_credentials={"b2": "id:secret"},
        obligation_timer_hours={"backup_restore_dryrun": 720},
        now="2026-05-20T00:00:00Z",
        role="active",
    )
    from mthydra.controller.state.authority import list_authorities
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.node_state import current_node_state
    conn = connect(db)
    assert len(list_authorities(conn)) == 1
    ns = current_node_state(conn)
    assert ns.role == "active"


def test_init_state_standby_refuses_no_b2_credential(tmp_path, recipient):
    """Standby requires a B2 credential; init without one raises."""
    from mthydra.controller.bootstrap import BootstrapError
    db = tmp_path / "state.sqlite"
    with pytest.raises(BootstrapError, match="b2"):
        init_state(
            db_path=db,
            age_recipient=recipient,
            provider_credentials={"aws": "id:secret"},  # only AWS, no B2
            obligation_timer_hours={},
            now="2026-05-20T00:00:00Z",
            role="standby",
        )
```

- [ ] **Step 2: Verify failure**

```bash
pytest tests/unit/controller/test_bootstrap.py -k 'standby or active_default' -v
```

Expected: TypeError on the new `role=` keyword.

- [ ] **Step 3: Modify `src/mthydra/controller/bootstrap.py`**

Change the signature:

```python
def init_state(
    db_path: Path | str,
    age_recipient: str,
    provider_credentials: dict[str, str],
    obligation_timer_hours: dict[str, int],
    now: str,
    role: str = "active",
) -> None:
    """Create a fresh state.sqlite at db_path.

    role='active' (default): seeds credential_authority + descriptor_signing_key
        + provider credentials + obligation_clocks + node_state='active'.
    role='standby': seeds only schema + B2 provider credential + node_state='standby'.
        Refuses if 'b2' not in provider_credentials.
    """
```

Branch the body:

```python
    if role not in ("active", "standby"):
        raise BootstrapError(f"unknown role {role!r}")

    if role == "standby" and "b2" not in provider_credentials:
        raise BootstrapError(
            "standby role requires a 'b2' provider credential (for heartbeat publishing)"
        )

    db_path = Path(db_path)
    if db_path.exists():
        raise BootstrapError(
            f"refusing to bootstrap: {db_path} already exists; move or delete first"
        )
    try:
        validate_recipient(age_recipient)
    except AgeError as e:
        raise BootstrapError(f"invalid age recipient: {e}") from e

    conn = connect(db_path)
    try:
        apply_schema(conn)

        # node_state is auto-seeded as 'active' by apply_schema. For standby:
        if role == "standby":
            conn.execute("UPDATE node_state SET role='standby' WHERE rowid=1")
            conn.commit()
            # Skeleton: only the B2 provider credential.
            set_provider_credential(
                conn, provider="b2",
                credential=provider_credentials["b2"], at=now,
            )
            # Standby seeds no obligations on its own DB.
        else:
            # Full active seed (existing logic).
            priv, pub = _placeholder_keypair_pem()
            insert_authority(conn, generation=1, privkey_pem=priv, pubkey_pem=pub, created_at=now)

            dpriv, dpub = generate_keypair()
            insert_signing_key(conn, generation=1, privkey=dpriv, pubkey=dpub, created_at=now)

            for provider, cred in provider_credentials.items():
                set_provider_credential(conn, provider=provider, credential=cred, at=now)

            for obligation_id, hours in obligation_timer_hours.items():
                next_due = _add_hours(now, hours) if hours > 0 else now
                set_obligation(
                    conn,
                    obligation_id=obligation_id,
                    last_proven_at=now,
                    proven_by="bootstrap",
                    next_due_at=next_due,
                    details=None,
                )
    finally:
        conn.close()

    if hasattr(os, "chmod"):
        os.chmod(db_path, 0o600)
        os.chmod(db_path.parent, 0o700)
```

- [ ] **Step 4: Modify `init` subcommand in `src/mthydra/controller/cli.py`**

Add the `--role` flag to the `init` subparser (just below the existing args):

```python
    init_p.add_argument(
        "--role",
        choices=["active", "standby"],
        default="active",
        help="initialise as active (default) or standby (skeleton DB)",
    )
```

In the `if args.cmd == "init":` block, pass `role=args.role`:

```python
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
                    "descriptor_signing_key_rotation": 8760,
                    "cover_pool_reverify_pass_proven": 60 * 24,
                    "cover_pool_replenishment_proven": 90 * 24,
                    "eu_standby_drill_proven": 30 * 24,  # 30 days (spec F §9.2)
                } if args.role == "active" else {},
                now=_now(),
                role=args.role,
            )
            print(f"initialized {args.db_path} (role={args.role})")
            return 0
```

- [ ] **Step 5: Run bootstrap + CLI tests**

```bash
pytest tests/unit/controller/test_bootstrap.py tests/unit/controller/test_cli.py -v
```

- [ ] **Step 6: Commit**

```bash
git add src/mthydra/controller/bootstrap.py src/mthydra/controller/cli.py tests/unit/controller/test_bootstrap.py
git commit -m "bootstrap(F): init --role=active|standby; skeleton DB for standby"
```

---

## Phase 5 — Heartbeat plumbing

### Task 7: Extend `S3Destination` with heartbeat methods

**Files:**
- Modify: `src/mthydra/controller/backup/s3_dest.py`
- Modify: `tests/unit/controller/backup/test_s3_dest.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/controller/backup/test_s3_dest.py`:

```python
def test_put_and_get_heartbeat_roundtrip(s3_destination_with_object_lock_bucket):
    """put_heartbeat stores a JSON payload; get_heartbeat returns the same bytes + ETag."""
    dest = s3_destination_with_object_lock_bucket
    payload = b'{"node_id":"eu-standby-de-1","ts":"2026-05-20T00:00:00Z"}'
    dest.put_heartbeat(node_id="eu-standby-de-1", payload=payload)
    body, etag = dest.get_heartbeat(node_id="eu-standby-de-1")
    assert body == payload
    assert etag  # ETag is a non-empty quoted string in S3


def test_head_heartbeat_returns_none_when_absent(s3_destination_with_object_lock_bucket):
    dest = s3_destination_with_object_lock_bucket
    result = dest.head_heartbeat(node_id="eu-no-such-node")
    assert result is None


def test_head_heartbeat_returns_etag_and_modified(s3_destination_with_object_lock_bucket):
    dest = s3_destination_with_object_lock_bucket
    dest.put_heartbeat(node_id="eu-standby-de-1", payload=b'{"x":1}')
    info = dest.head_heartbeat(node_id="eu-standby-de-1")
    assert info is not None
    assert "etag" in info
    assert "last_modified_iso" in info
```

You will need a `s3_destination_with_object_lock_bucket` fixture matching the existing test pattern (`moto[s3]` with Object Lock enabled). Look at the top of `tests/unit/controller/backup/test_s3_dest.py` for the existing fixture and add the heartbeat tests using the same fixture name (renaming or aliasing as needed).

- [ ] **Step 2: Verify failure**

```bash
pytest tests/unit/controller/backup/test_s3_dest.py -k heartbeat -v
```

- [ ] **Step 3: Add methods to `S3Destination` in `src/mthydra/controller/backup/s3_dest.py`**

```python
    @staticmethod
    def _heartbeat_key(node_id: str) -> str:
        return f"standby/{node_id}/heartbeat.json"

    def put_heartbeat(self, *, node_id: str, payload: bytes) -> None:
        """Upload a standby heartbeat object.

        Goes into the same bucket (Object Lock COMPLIANCE required); the
        accumulated versions are accepted residual (spec F §11).
        """
        retain_until = datetime.now(timezone.utc) + timedelta(days=self.object_lock_days)
        self._client.put_object(
            Bucket=self.bucket,
            Key=self._heartbeat_key(node_id),
            Body=payload,
            ContentType="application/json",
            ObjectLockMode="COMPLIANCE",
            ObjectLockRetainUntilDate=retain_until,
        )

    def get_heartbeat(self, *, node_id: str) -> tuple[bytes, str]:
        """Returns (payload, etag). Raises ClientError on absence."""
        obj = self._client.get_object(
            Bucket=self.bucket, Key=self._heartbeat_key(node_id)
        )
        return obj["Body"].read(), obj["ETag"]

    def head_heartbeat(self, *, node_id: str) -> dict[str, Any] | None:
        """Returns {'etag', 'last_modified_iso', 'size_bytes'} or None if absent."""
        try:
            obj = self._client.head_object(
                Bucket=self.bucket, Key=self._heartbeat_key(node_id)
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
                return None
            raise
        return {
            "etag": obj["ETag"],
            "last_modified_iso": obj["LastModified"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "size_bytes": int(obj["ContentLength"]),
        }
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/unit/controller/backup/test_s3_dest.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/backup/s3_dest.py tests/unit/controller/backup/test_s3_dest.py
git commit -m "s3_dest(F): put/get/head_heartbeat methods for standby liveness pings"
```

---

### Task 8: `StandbyHeartbeatPublisher`

**Files:**
- Create: `src/mthydra/controller/standby/__init__.py` (empty)
- Create: `src/mthydra/controller/standby/heartbeat.py`
- Create: `tests/unit/controller/standby/__init__.py` (empty)
- Create: `tests/unit/controller/standby/test_heartbeat_publisher.py`

- [ ] **Step 1: Touch `__init__.py` files**

```bash
mkdir -p /home/asharov/RedHat/Dev/mthydra/src/mthydra/controller/standby
mkdir -p /home/asharov/RedHat/Dev/mthydra/tests/unit/controller/standby
touch /home/asharov/RedHat/Dev/mthydra/src/mthydra/controller/standby/__init__.py
touch /home/asharov/RedHat/Dev/mthydra/tests/unit/controller/standby/__init__.py
```

- [ ] **Step 2: Write failing tests**

```python
"""Spec F — StandbyHeartbeatPublisher."""
import json
from unittest.mock import MagicMock

import pytest

from mthydra.controller.standby.heartbeat import StandbyHeartbeatPublisher


def test_publisher_run_once_uploads_well_formed_json():
    dest = MagicMock()
    pub = StandbyHeartbeatPublisher(
        node_id="eu-standby-de-1",
        b2_destination=dest,
        interval_seconds=60,
        mode="offline",
        clock=lambda: "2026-05-20T01:00:00Z",
    )
    pub.run_once()
    dest.put_heartbeat.assert_called_once()
    call_kwargs = dest.put_heartbeat.call_args.kwargs
    assert call_kwargs["node_id"] == "eu-standby-de-1"
    payload = json.loads(call_kwargs["payload"])
    assert payload == {
        "schema": "mthydra.standby_heartbeat.v1",
        "node_id": "eu-standby-de-1",
        "ts": "2026-05-20T01:00:00Z",
        "schema_version": 4,
        "controller_version": pytest.helpers.ANY if False else payload["controller_version"],
    }
    assert isinstance(payload["controller_version"], str)


def test_publisher_offline_mode_arm_is_noop():
    dest = MagicMock()
    pub = StandbyHeartbeatPublisher(
        node_id="x", b2_destination=dest,
        interval_seconds=60, mode="offline",
        clock=lambda: "2026-05-20T01:00:00Z",
    )
    pub.arm()
    pub.disarm()
    dest.put_heartbeat.assert_not_called()


def test_publisher_run_once_callable_even_in_offline_mode():
    dest = MagicMock()
    pub = StandbyHeartbeatPublisher(
        node_id="x", b2_destination=dest,
        interval_seconds=60, mode="offline",
        clock=lambda: "2026-05-20T01:00:00Z",
    )
    pub.run_once()
    dest.put_heartbeat.assert_called_once()
```

- [ ] **Step 3: Verify failure**

```bash
pytest tests/unit/controller/standby/test_heartbeat_publisher.py -v
```

- [ ] **Step 4: Create `src/mthydra/controller/standby/heartbeat.py`**

```python
"""Standby heartbeat publisher + active-side poller (spec F §5).

The publisher runs on standby nodes and writes a small JSON ping to B2
on a timer. The poller runs on the active node and reads the same key,
stamps eu_nodes.last_heartbeat_at + the eu_standby_liveness_seen
obligation, and emits eu_standby_liveness_stale when staleness alerts.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

_HEARTBEAT_SCHEMA = "mthydra.standby_heartbeat.v1"


def _default_clock() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _add_seconds_iso(iso: str, seconds: float) -> str:
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (t + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _controller_version() -> str:
    try:
        from importlib.metadata import version
        return version("mthydra")
    except Exception:
        return "0.0.0"


class StandbyHeartbeatPublisher:
    """Pushes a heartbeat JSON object to B2 at a regular cadence."""

    def __init__(
        self,
        *,
        node_id: str,
        b2_destination,
        interval_seconds: float,
        mode: str = "production",
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.node_id = node_id
        self.b2 = b2_destination
        self.interval_seconds = interval_seconds
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
            trigger=IntervalTrigger(seconds=self.interval_seconds),
        )
        self._scheduler.start()

    def disarm(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    def run_once(self) -> None:
        payload_dict = {
            "schema": _HEARTBEAT_SCHEMA,
            "node_id": self.node_id,
            "ts": self._clock(),
            "schema_version": 4,
            "controller_version": _controller_version(),
        }
        payload = json.dumps(payload_dict, separators=(",", ":")).encode("utf-8")
        self.b2.put_heartbeat(node_id=self.node_id, payload=payload)
```

(Poller goes in the same module — added in Task 9.)

- [ ] **Step 5: Run tests**

```bash
pytest tests/unit/controller/standby/test_heartbeat_publisher.py -v
```

The first test has a slightly tricky `pytest.helpers.ANY` line — replace it with the simpler check above where `controller_version` is just asserted to be a string. The corrected test body should be:

```python
def test_publisher_run_once_uploads_well_formed_json():
    dest = MagicMock()
    pub = StandbyHeartbeatPublisher(
        node_id="eu-standby-de-1",
        b2_destination=dest,
        interval_seconds=60,
        mode="offline",
        clock=lambda: "2026-05-20T01:00:00Z",
    )
    pub.run_once()
    dest.put_heartbeat.assert_called_once()
    call_kwargs = dest.put_heartbeat.call_args.kwargs
    assert call_kwargs["node_id"] == "eu-standby-de-1"
    payload = json.loads(call_kwargs["payload"])
    assert payload["schema"] == "mthydra.standby_heartbeat.v1"
    assert payload["node_id"] == "eu-standby-de-1"
    assert payload["ts"] == "2026-05-20T01:00:00Z"
    assert payload["schema_version"] == 4
    assert isinstance(payload["controller_version"], str)
```

(If the original was already pasted, edit it before running.)

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/mthydra/controller/standby/ tests/unit/controller/standby/
git commit -m "standby(F): StandbyHeartbeatPublisher — B2 ping on interval"
```

---

### Task 9: `StandbyHeartbeatPoller`

**Files:**
- Modify: `src/mthydra/controller/standby/heartbeat.py`
- Create: `tests/unit/controller/standby/test_heartbeat_poller.py`

- [ ] **Step 1: Write failing tests**

```python
"""Spec F — StandbyHeartbeatPoller."""
import json
from unittest.mock import MagicMock

import pytest

from mthydra.controller.standby.heartbeat import StandbyHeartbeatPoller
from mthydra.controller.state.db import connect
from mthydra.controller.state.eu_nodes import add_eu_node, get_eu_node
from mthydra.controller.state.obligations import list_obligations
from mthydra.controller.state.schema import apply_schema


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "state.sqlite"
    conn = connect(p)
    apply_schema(conn)
    conn.close()
    return p


def _seed_standby(p, node_id="eu-standby-de-1"):
    conn = connect(p)
    add_eu_node(conn, node_id=node_id, hostname="h", provider="hetzner",
                region="de", role="standby", added_at="2026-05-20T00:00:00Z")
    conn.close()


def test_poller_fresh_heartbeat_proves_obligation(db):
    _seed_standby(db)
    b2 = MagicMock()
    b2.head_heartbeat.return_value = {
        "etag": '"abc"',
        "last_modified_iso": "2026-05-20T01:00:00Z",
        "size_bytes": 100,
    }
    poller = StandbyHeartbeatPoller(
        db_path=db, b2_destination=b2, poll_interval_seconds=300,
        staleness_alert_seconds=600, mode="offline",
        clock=lambda: "2026-05-20T01:01:00Z",
    )
    stale = poller.run_once()
    assert stale == []

    conn = connect(db)
    obs = {o.obligation_id: o for o in list_obligations(conn)}
    assert "eu_standby_liveness_seen::eu-standby-de-1" in obs
    n = get_eu_node(conn, "eu-standby-de-1")
    assert n.last_heartbeat_b2_etag == '"abc"'
    conn.close()


def test_poller_stale_heartbeat_emits_anti_obligation(db):
    _seed_standby(db)
    b2 = MagicMock()
    b2.head_heartbeat.return_value = {
        "etag": '"abc"',
        "last_modified_iso": "2026-05-20T01:00:00Z",
        "size_bytes": 100,
    }
    poller = StandbyHeartbeatPoller(
        db_path=db, b2_destination=b2, poll_interval_seconds=300,
        staleness_alert_seconds=600, mode="offline",
        # 30 minutes after last heartbeat — well beyond 600s staleness
        clock=lambda: "2026-05-20T01:30:00Z",
    )
    stale = poller.run_once()
    assert stale == ["eu-standby-de-1"]
    conn = connect(db)
    obs = {o.obligation_id for o in list_obligations(conn)}
    assert "eu_standby_liveness_stale::eu-standby-de-1" in obs
    conn.close()


def test_poller_missing_heartbeat_treated_as_stale(db):
    _seed_standby(db)
    b2 = MagicMock()
    b2.head_heartbeat.return_value = None  # no object in B2
    poller = StandbyHeartbeatPoller(
        db_path=db, b2_destination=b2, poll_interval_seconds=300,
        staleness_alert_seconds=600, mode="offline",
        clock=lambda: "2026-05-20T01:00:00Z",
    )
    stale = poller.run_once()
    assert stale == ["eu-standby-de-1"]


def test_poller_skips_retired_nodes(db):
    """A retired node's heartbeat is not polled at all."""
    _seed_standby(db, "eu-standby-old")
    conn = connect(db)
    from mthydra.controller.state.eu_nodes import retire_eu_node
    retire_eu_node(conn, "eu-standby-old", at="2026-05-20T00:30:00Z")
    conn.close()

    b2 = MagicMock()
    poller = StandbyHeartbeatPoller(
        db_path=db, b2_destination=b2, poll_interval_seconds=300,
        staleness_alert_seconds=600, mode="offline",
        clock=lambda: "2026-05-20T01:00:00Z",
    )
    poller.run_once()
    b2.head_heartbeat.assert_not_called()


def test_poller_clears_stale_when_fresh_heartbeat_returns(db):
    _seed_standby(db)
    b2 = MagicMock()
    poller = StandbyHeartbeatPoller(
        db_path=db, b2_destination=b2, poll_interval_seconds=300,
        staleness_alert_seconds=600, mode="offline",
        clock=lambda: "2026-05-20T02:00:00Z",
    )
    # First poll: B2 is empty → stale
    b2.head_heartbeat.return_value = None
    poller.run_once()
    conn = connect(db)
    obs = {o.obligation_id for o in list_obligations(conn)}
    assert "eu_standby_liveness_stale::eu-standby-de-1" in obs
    conn.close()

    # Second poll: B2 returns a fresh heartbeat → stale obligation cleared
    b2.head_heartbeat.return_value = {
        "etag": '"new"',
        "last_modified_iso": "2026-05-20T01:59:30Z",
        "size_bytes": 100,
    }
    poller.run_once()
    conn = connect(db)
    obs = {o.obligation_id for o in list_obligations(conn)}
    assert "eu_standby_liveness_stale::eu-standby-de-1" not in obs
    assert "eu_standby_liveness_seen::eu-standby-de-1" in obs
    conn.close()
```

- [ ] **Step 2: Verify failure**

```bash
pytest tests/unit/controller/standby/test_heartbeat_poller.py -v
```

- [ ] **Step 3: Append `StandbyHeartbeatPoller` to `src/mthydra/controller/standby/heartbeat.py`**

```python
import logging

from mthydra.controller.state.db import connect
from mthydra.controller.state.eu_nodes import list_eu_nodes, update_heartbeat
from mthydra.controller.state.obligations import set_obligation

log = logging.getLogger(__name__)


class StandbyHeartbeatPoller:
    """Polls B2 for every active standby node's heartbeat.

    Each non-retired eu_nodes.role='standby' row is checked once per tick.
    Fresh heartbeats prove `eu_standby_liveness_seen::<node_id>` and clear
    `eu_standby_liveness_stale::<node_id>`. Missing or aged-past-staleness
    heartbeats set the stale anti-obligation.
    """

    def __init__(
        self,
        *,
        db_path: Path | str,
        b2_destination,
        poll_interval_seconds: float,
        staleness_alert_seconds: float,
        mode: str = "production",
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.b2 = b2_destination
        self.poll_interval_seconds = poll_interval_seconds
        self.staleness_alert_seconds = staleness_alert_seconds
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

    def run_once(self) -> list[str]:
        """Poll every non-retired standby. Returns list of stale node_ids."""
        now = self._clock()
        stale: list[str] = []
        conn = connect(self.db_path)
        try:
            standbys = [n for n in list_eu_nodes(conn, role="standby")]
            for n in standbys:
                hb = self.b2.head_heartbeat(node_id=n.node_id)
                if hb is None:
                    self._mark_stale(conn, n.node_id, now,
                                     reason="no heartbeat object in B2")
                    stale.append(n.node_id)
                    continue
                age_seconds = _age_seconds(now, hb["last_modified_iso"])
                if age_seconds > self.staleness_alert_seconds:
                    self._mark_stale(conn, n.node_id, now,
                                     reason=f"age={age_seconds:.0f}s")
                    stale.append(n.node_id)
                    continue
                # Fresh: update heartbeat (idempotent on same etag), prove obligation,
                # clear any prior stale anti-obligation.
                update_heartbeat(conn, n.node_id, at=now, b2_etag=hb["etag"])
                next_due = _add_seconds_iso(now, self.staleness_alert_seconds)
                set_obligation(
                    conn,
                    obligation_id=f"eu_standby_liveness_seen::{n.node_id}",
                    last_proven_at=now,
                    proven_by="heartbeat_poller",
                    next_due_at=next_due,
                    details=json.dumps({"etag": hb["etag"]}),
                )
                conn.execute(
                    "DELETE FROM obligation_clocks WHERE obligation_id=?",
                    (f"eu_standby_liveness_stale::{n.node_id}",),
                )
                conn.commit()
            return stale
        finally:
            conn.close()

    def _mark_stale(self, conn, node_id: str, now: str, *, reason: str) -> None:
        set_obligation(
            conn,
            obligation_id=f"eu_standby_liveness_stale::{node_id}",
            last_proven_at=now,
            proven_by="heartbeat_poller",
            next_due_at=now,  # anti-obligation; next_due irrelevant
            details=json.dumps({"reason": reason}),
        )


def _age_seconds(now_iso: str, then_iso: str) -> float:
    now = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
    then = datetime.fromisoformat(then_iso.replace("Z", "+00:00"))
    return (now - then).total_seconds()
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/unit/controller/standby/ -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/standby/heartbeat.py tests/unit/controller/standby/test_heartbeat_poller.py
git commit -m "standby(F): StandbyHeartbeatPoller — B2 polling + per-node liveness obligations"
```

---

## Phase 6 — Promotion + authority-rotate

### Task 10: `authority-rotate` CLI

**Files:**
- Modify: `src/mthydra/controller/cli.py`
- Modify: `tests/unit/controller/test_cli.py`

- [ ] **Step 1: Write failing tests**

```python
def test_authority_rotate_adds_new_generation(tmp_path, age_recipient):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    rc = run(["authority-rotate", "--db-path", str(db), "--config", str(cfg_path)])
    assert rc == 0
    from mthydra.controller.state.authority import list_authorities
    from mthydra.controller.state.db import connect
    conn = connect(db)
    auths = list_authorities(conn)
    assert len(auths) == 2  # original + new
    # Old retired, new active
    assert sum(1 for a in auths if a.retired_at is None) == 1


def test_authority_rotate_refuses_on_standby(tmp_path, age_recipient, capsys):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    run(["init", "--role", "standby", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    rc = run(["authority-rotate", "--db-path", str(db), "--config", str(cfg_path)])
    assert rc == 2
    assert "active-only" in capsys.readouterr().err.lower() or "standby" in capsys.readouterr().err.lower()
```

- [ ] **Step 2: Add subparser + dispatch + handler in `src/mthydra/controller/cli.py`**

Subparser:

```python
    ar = sub.add_parser("authority-rotate",
                         help="rotate credential_authority — insert new generation, retire current")
    ar.add_argument("--db-path", default=DEFAULT_DB)
    ar.add_argument("--config", default="/etc/mthydra/controller.toml")
```

Dispatch:

```python
    if args.cmd == "authority-rotate":
        return _cmd_authority_rotate(args)
```

Handler:

```python
def _cmd_authority_rotate(args) -> int:
    from mthydra.controller.bootstrap import _placeholder_keypair_pem
    from mthydra.controller.state.authority import (
        current_authority, insert_authority, retire_authority,
    )
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.node_state import current_node_state

    conn = connect(args.db_path)
    try:
        ns = current_node_state(conn)
        if ns.role != "active":
            print("authority-rotate: refused — active-only command", file=sys.stderr)
            return 2
        try:
            current = current_authority(conn)
        except LookupError:
            print("authority-rotate: no active credential_authority found", file=sys.stderr)
            return 2
        new_gen = current.generation + 1
        priv, pub = _placeholder_keypair_pem()
        insert_authority(conn, generation=new_gen, privkey_pem=priv,
                         pubkey_pem=pub, created_at=_now())
        retire_authority(conn, current.generation, at=_now())
        # Emit audit row (insert_authority/retire_authority don't emit).
        from mthydra.controller.state.audit import log_event
        import json as _json
        log_event(conn, ts=_now(), actor="operator", action="authority_rotated",
                  target=str(new_gen),
                  details_json=_json.dumps({"new_generation": new_gen,
                                             "retired_generation": current.generation}))
        print(f"authority-rotate: new generation {new_gen} active; "
              f"generation {current.generation} retired")
        return 0
    finally:
        conn.close()
```

- [ ] **Step 3: Run tests**

```bash
pytest tests/unit/controller/test_cli.py -k authority_rotate -v
```

Expected: PASS (2/2).

- [ ] **Step 4: Commit**

```bash
git add src/mthydra/controller/cli.py tests/unit/controller/test_cli.py
git commit -m "cli(F): authority-rotate subcommand (referenced by promote-active --case B checklist)"
```

---

### Task 11: `promote-active` CLI

**Files:**
- Create: `src/mthydra/controller/promote.py`
- Create: `tests/unit/controller/test_promote.py`
- Modify: `src/mthydra/controller/cli.py`

This is the load-bearing piece. The atomic state replacement lives in its own module so the CLI handler is thin.

- [ ] **Step 1: Write failing tests**

```python
"""Spec F — promote-active atomic state replacement."""
import shutil
import subprocess

import pytest

from mthydra.controller.promote import PromotionError, promote_active
from mthydra.controller.state.db import connect
from mthydra.controller.state.node_state import current_node_state


@pytest.fixture
def age_keypair(tmp_path):
    if shutil.which("age-keygen") is None:
        pytest.skip("age-keygen not installed")
    keyfile = tmp_path / "identity"
    subprocess.run(["age-keygen", "-o", str(keyfile)], capture_output=True, check=True)
    for line in keyfile.read_text().splitlines():
        if line.startswith("# public key: "):
            return keyfile, line.removeprefix("# public key: ").strip()
    raise RuntimeError("no public key line")


def _seed_active_db(path, recipient):
    """Build a 'donor' active DB with full state, then encrypt it as a backup blob."""
    from mthydra.controller.bootstrap import init_state
    init_state(
        db_path=path,
        age_recipient=recipient,
        provider_credentials={"b2": "id:secret"},
        obligation_timer_hours={"backup_restore_dryrun": 720},
        now="2026-05-20T00:00:00Z",
        role="active",
    )


def _encrypt_db(src, dst, recipient):
    from mthydra.controller.backup.age_crypt import encrypt_file
    encrypt_file(src, recipient=recipient, out=dst)


def _seed_skeleton_db(path, recipient):
    from mthydra.controller.bootstrap import init_state
    init_state(
        db_path=path,
        age_recipient=recipient,
        provider_credentials={"b2": "id:secret"},
        obligation_timer_hours={},
        now="2026-05-20T00:00:00Z",
        role="standby",
    )


def test_promote_active_case_a_swaps_db(tmp_path, age_keypair):
    identity, recipient = age_keypair
    donor_db = tmp_path / "donor.sqlite"
    blob = tmp_path / "backup.age"
    target_db = tmp_path / "state.sqlite"

    _seed_active_db(donor_db, recipient)
    _encrypt_db(donor_db, blob, recipient)
    _seed_skeleton_db(target_db, recipient)

    case_b_checklist = promote_active(
        db_path=target_db,
        backup_blob=blob,
        age_identity=identity,
        case="A",
        node_id="eu-promoted-1",
        now="2026-05-20T01:00:00Z",
    )
    assert case_b_checklist is None  # Case A returns no checklist

    conn = connect(target_db)
    ns = current_node_state(conn)
    assert ns.role == "active"
    assert ns.previous_role == "standby"
    assert ns.promotion_case == "A"
    # The donor's authority + signing-key rows are now in target.
    cnt_auth = conn.execute("SELECT COUNT(*) FROM credential_authority WHERE retired_at IS NULL").fetchone()[0]
    assert cnt_auth == 1
    conn.close()


def test_promote_active_case_b_returns_checklist(tmp_path, age_keypair):
    identity, recipient = age_keypair
    donor_db = tmp_path / "donor.sqlite"
    blob = tmp_path / "backup.age"
    target_db = tmp_path / "state.sqlite"
    _seed_active_db(donor_db, recipient)
    _encrypt_db(donor_db, blob, recipient)
    _seed_skeleton_db(target_db, recipient)

    checklist = promote_active(
        db_path=target_db,
        backup_blob=blob,
        age_identity=identity,
        case="B",
        node_id="eu-promoted-1",
        now="2026-05-20T01:00:00Z",
    )
    assert checklist is not None
    assert "authority-rotate" in checklist
    assert "signing-key-rotate" in checklist
    assert "rotate-provider-credential" in checklist


def test_promote_active_refuses_when_role_is_active(tmp_path, age_keypair):
    identity, recipient = age_keypair
    donor_db = tmp_path / "donor.sqlite"
    blob = tmp_path / "backup.age"
    target_db = tmp_path / "state.sqlite"
    _seed_active_db(donor_db, recipient)
    _encrypt_db(donor_db, blob, recipient)
    _seed_active_db(target_db, recipient)  # target is also active — refuse

    with pytest.raises(PromotionError, match="standby"):
        promote_active(
            db_path=target_db,
            backup_blob=blob,
            age_identity=identity,
            case="A",
            node_id="x",
            now="2026-05-20T01:00:00Z",
        )


def test_promote_active_refuses_invalid_case(tmp_path, age_keypair):
    identity, recipient = age_keypair
    donor_db = tmp_path / "donor.sqlite"
    blob = tmp_path / "backup.age"
    target_db = tmp_path / "state.sqlite"
    _seed_active_db(donor_db, recipient)
    _encrypt_db(donor_db, blob, recipient)
    _seed_skeleton_db(target_db, recipient)

    with pytest.raises(PromotionError, match="case"):
        promote_active(
            db_path=target_db,
            backup_blob=blob,
            age_identity=identity,
            case="C",
            node_id="x",
            now="2026-05-20T01:00:00Z",
        )


def test_promote_active_rolls_back_on_startup_check_failure(tmp_path, age_keypair, monkeypatch):
    """If startup-check fails on the new DB, the .bak is restored."""
    identity, recipient = age_keypair
    donor_db = tmp_path / "donor.sqlite"
    blob = tmp_path / "backup.age"
    target_db = tmp_path / "state.sqlite"
    _seed_active_db(donor_db, recipient)
    _encrypt_db(donor_db, blob, recipient)
    _seed_skeleton_db(target_db, recipient)

    # Inject a failing startup-check.
    from mthydra.controller.state.invariants import InvariantViolation
    def _fail(*_a, **_kw):
        raise InvariantViolation("simulated")
    monkeypatch.setattr("mthydra.controller.promote.check_all", _fail)

    with pytest.raises(PromotionError, match="invariant"):
        promote_active(
            db_path=target_db,
            backup_blob=blob,
            age_identity=identity,
            case="A",
            node_id="x",
            now="2026-05-20T01:00:00Z",
        )
    # The skeleton DB must still be the live one.
    conn = connect(target_db)
    ns = current_node_state(conn)
    assert ns.role == "standby"
    conn.close()
```

- [ ] **Step 2: Create `src/mthydra/controller/promote.py`**

```python
"""Spec F — promote-active atomic state replacement.

The promote_active() function performs steps 1–10 of the spec F §8 procedure:
decrypts a backup blob, swaps it for the live skeleton DB atomically, writes
the node_state UPDATE, runs startup-check, and rolls back via .bak rename on
failure. It does NOT invoke systemctl — the operator stops/starts the service.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

from mthydra.controller.restore.decrypt import decrypt_blob
from mthydra.controller.state.audit import log_event
from mthydra.controller.state.db import connect
from mthydra.controller.state.eu_nodes import get_eu_node
from mthydra.controller.state.invariants import InvariantViolation, check_all
from mthydra.controller.state.node_state import current_node_state, set_node_role
from mthydra.controller.state.schema import SCHEMA_VERSION, apply_schema


class PromotionError(RuntimeError):
    """Raised when promote_active cannot complete safely."""


_CASE_B_CHECKLIST = """\
CASE B — SUSPECTED COMPROMISE. Run the following in order on the now-active node:

  1. Re-key credential authority:
       mthydra-controller authority-rotate

  2. Rotate descriptor signing key:
       mthydra-controller signing-key-rotate

  3. Rotate the B2 provider credential (revoke old in B2 console, mint new, push):
       mthydra-controller rotate-provider-credential --provider b2 --credential <NEW>

  4. Mint fresh publishing tokens — spec K, when shipped.

  5. Trigger immediate descriptor sign:
       mthydra-controller descriptor-sign-now

  6. Rotate the published subset forward — spec K. For MVP, accelerate cover-rotate.

  7. Verify recovery:
       - probe from a Russia-vantage shows green
       - backup-monitor dead-man's-switch clears
       - at least one end-to-end user path confirmed

  8. Record the drill:
       mthydra-controller standby-drill-proven --node-id <previous-standby-id> \\
           --case B --notes "promoted <date>, reason <text>"
"""


def promote_active(
    *,
    db_path: Path | str,
    backup_blob: Path | str,
    age_identity: Path | str,
    case: str,
    node_id: str,
    now: str,
) -> str | None:
    """Perform the atomic state replacement. Returns the Case-B checklist or None for Case A.

    Refuses with PromotionError if:
      - local node_state.role != 'standby'
      - case not in {'A','B'}
      - --backup-blob does not decrypt
      - decrypted DB's schema is incompatible
      - startup-check fails on the new DB (rolls back to .bak first)
    """
    if case not in ("A", "B"):
        raise PromotionError(f"case must be 'A' or 'B', got {case!r}")

    db_path = Path(db_path)
    backup_blob = Path(backup_blob)
    age_identity = Path(age_identity)

    if not db_path.exists():
        raise PromotionError(f"db_path {db_path} does not exist")
    if not backup_blob.exists():
        raise PromotionError(f"backup_blob {backup_blob} does not exist")
    if not age_identity.exists():
        raise PromotionError(f"age_identity {age_identity} does not exist")

    # 1. Verify current role is standby.
    conn = connect(db_path)
    try:
        ns = current_node_state(conn)
        if ns.role != "standby":
            raise PromotionError(
                f"refused: current node_state.role={ns.role!r}, expected 'standby'"
            )
    finally:
        conn.close()

    # 2. Decrypt to a temp path in the same dir as db_path (atomic rename
    #    must stay on the same filesystem).
    tmp_dir = db_path.parent / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    temp_db = tmp_dir / f"promote-{datetime.now().strftime('%Y%m%dT%H%M%S')}.sqlite"

    try:
        decrypt_blob(str(backup_blob), identity_path=str(age_identity), out=temp_db)
    except Exception as e:
        raise PromotionError(f"decryption failed: {e}") from e

    # 3. apply_schema on the temp DB to forward-migrate. Read backup generation.
    conn = connect(temp_db)
    try:
        apply_schema(conn)
        v = conn.execute("SELECT version FROM schema_version WHERE rowid=1").fetchone()
        if v is None or v[0] > SCHEMA_VERSION:
            raise PromotionError(
                f"backup schema_version {v} is newer than local {SCHEMA_VERSION}"
            )
        gen_row = conn.execute(
            "SELECT MAX(generation) FROM backup_log WHERE pushed_at IS NOT NULL"
        ).fetchone()
        backup_generation = gen_row[0] if gen_row and gen_row[0] is not None else 0
    finally:
        conn.close()

    # 4. Atomic file swap.
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    bak_path = db_path.with_suffix(db_path.suffix + f".preskel-{timestamp}.bak")
    db_path.rename(bak_path)
    try:
        temp_db.rename(db_path)
    except Exception:
        # Restore .bak before re-raising.
        bak_path.rename(db_path)
        raise
    db_path.chmod(0o600)

    # 5. Write the node_state UPDATE on the new DB.
    conn = connect(db_path)
    try:
        set_node_role(
            conn,
            role="active",
            promoted_at=now,
            previous_role="standby",
            promotion_case=case,
            promotion_backup_generation=backup_generation,
        )
        # Update the local eu_nodes row if the operator pre-registered it.
        try:
            n = get_eu_node(conn, node_id)
            conn.execute(
                "UPDATE eu_nodes SET role='active', promoted_at=? WHERE node_id=?",
                (now, node_id),
            )
            conn.commit()
        except LookupError:
            # Node not pre-registered; operator may add it after promotion.
            pass

        log_event(
            conn, ts=now, actor="operator", action="eu_node_promoted",
            target=node_id,
            details_json=json.dumps({
                "case": case, "backup_generation": backup_generation,
                "previous_role": "standby",
            }, separators=(",", ":")),
        )

        # 6. Run startup-check. Rollback on failure.
        try:
            check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=now)
        except InvariantViolation as e:
            conn.close()
            # Replace the new DB with the .bak.
            failed_path = db_path.with_suffix(db_path.suffix + f".failed-{timestamp}")
            db_path.rename(failed_path)
            bak_path.rename(db_path)
            raise PromotionError(f"invariant check failed after promotion: {e}") from e
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return _CASE_B_CHECKLIST if case == "B" else None
```

The existing `decrypt_blob` is in `src/mthydra/controller/restore/decrypt.py` and writes the decrypted bytes to `out` (a Path). Signature is `decrypt_blob(src, identity_path, out)`. Adapt the call as needed if its actual signature differs.

- [ ] **Step 3: Add CLI subparser + dispatch + handler in `src/mthydra/controller/cli.py`**

Subparser:

```python
    pa = sub.add_parser("promote-active",
                         help="promote this standby to active via T2 §7 atomic state replacement")
    pa.add_argument("--backup-blob", required=True)
    pa.add_argument("--age-identity", required=True)
    pa.add_argument("--case", choices=["A", "B"], required=True)
    pa.add_argument("--db-path", default=DEFAULT_DB)
    pa.add_argument("--config", default="/etc/mthydra/controller.toml")
    pa.add_argument("--yes", action="store_true",
                     help="skip the interactive confirmation prompt")
```

Dispatch:

```python
    if args.cmd == "promote-active":
        return _cmd_promote_active(args)
```

Handler:

```python
def _cmd_promote_active(args) -> int:
    from mthydra.controller.config import ConfigError, load_config
    from mthydra.controller.promote import PromotionError, promote_active

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"promote-active: config error: {e}", file=sys.stderr)
        return 2

    if not args.yes:
        if not sys.stdin.isatty():
            print("promote-active: refusing — pass --yes or run on a TTY", file=sys.stderr)
            return 2
        confirm = input("Type 'PROMOTE' to proceed: ")
        if confirm != "PROMOTE":
            print("promote-active: aborted", file=sys.stderr)
            return 2

    node_id = cfg.standby.node_id or "unknown"

    try:
        checklist = promote_active(
            db_path=args.db_path,
            backup_blob=args.backup_blob,
            age_identity=args.age_identity,
            case=args.case,
            node_id=node_id,
            now=_now(),
        )
    except PromotionError as e:
        print(f"promote-active: {e}", file=sys.stderr)
        return 2

    print(f"promote-active: node {node_id} is now ACTIVE "
          f"(case {args.case}, ready to start systemd unit)")
    if checklist is not None:
        print(checklist)
    return 0
```

- [ ] **Step 4: Run promote tests**

```bash
pytest tests/unit/controller/test_promote.py -v
```

Expected: PASS (5/5).

- [ ] **Step 5: Run full unit suite**

```bash
pytest tests/unit -q
```

- [ ] **Step 6: Commit**

```bash
git add src/mthydra/controller/promote.py src/mthydra/controller/cli.py tests/unit/controller/test_promote.py
git commit -m "promote(F): promote-active atomic state replacement with Case A/B branching"
```

---

## Phase 7 — Inventory + drill CLIs

### Task 12: `eu-node-add` / `eu-node-retire` / `eu-node-list`

**Files:**
- Modify: `src/mthydra/controller/cli.py`
- Modify: `tests/unit/controller/test_cli.py`

- [ ] **Step 1: Write failing tests**

```python
def test_eu_node_add_default_standby(tmp_path, age_recipient):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    rc = run(["eu-node-add", "eu-standby-de-1",
              "--hostname", "standby.example",
              "--provider", "hetzner",
              "--region", "de",
              "--db-path", str(db)])
    assert rc == 0
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.eu_nodes import get_eu_node
    conn = connect(db)
    n = get_eu_node(conn, "eu-standby-de-1")
    assert n.role == "standby"


def test_eu_node_add_refuses_second_active(tmp_path, age_recipient, capsys):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    run(["eu-node-add", "eu-active-1", "--hostname", "h",
         "--provider", "aws", "--region", "fr",
         "--role", "active", "--db-path", str(db)])
    rc = run(["eu-node-add", "eu-active-2", "--hostname", "h",
              "--provider", "aws", "--region", "fr",
              "--role", "active", "--db-path", str(db)])
    assert rc == 2
    assert "only one active" in capsys.readouterr().err


def test_eu_node_retire_happy(tmp_path, age_recipient):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    run(["eu-node-add", "eu-standby-de-1", "--hostname", "h",
         "--provider", "hetzner", "--region", "de", "--db-path", str(db)])
    rc = run(["eu-node-retire", "eu-standby-de-1", "--db-path", str(db)])
    assert rc == 0
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.eu_nodes import get_eu_node
    conn = connect(db)
    n = get_eu_node(conn, "eu-standby-de-1")
    assert n.role == "retired"


def test_eu_node_list_json(tmp_path, age_recipient, capsys):
    import json
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    run(["eu-node-add", "eu-standby-de-1", "--hostname", "h",
         "--provider", "hetzner", "--region", "de", "--db-path", str(db)])
    capsys.readouterr()
    rc = run(["eu-node-list", "--db-path", str(db), "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert any(r["node_id"] == "eu-standby-de-1" for r in data)


def test_eu_node_add_refused_on_standby(tmp_path, age_recipient, capsys):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run(["init", "--role", "standby", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    rc = run(["eu-node-add", "eu-anything", "--hostname", "h",
              "--provider", "p", "--region", "r", "--db-path", str(db)])
    assert rc == 2
    assert "active" in capsys.readouterr().err.lower()
```

- [ ] **Step 2: Add subparsers + dispatches + handlers**

Subparsers:

```python
    ena = sub.add_parser("eu-node-add",
                          help="add an EU node to the inventory (default role=standby)")
    ena.add_argument("node_id")
    ena.add_argument("--hostname", required=True)
    ena.add_argument("--provider", required=True)
    ena.add_argument("--region", required=True)
    ena.add_argument("--public-ip", default=None)
    ena.add_argument("--role", choices=["active", "standby"], default="standby")
    ena.add_argument("--notes", default=None)
    ena.add_argument("--db-path", default=DEFAULT_DB)

    enr = sub.add_parser("eu-node-retire",
                          help="retire an EU node (role -> retired)")
    enr.add_argument("node_id")
    enr.add_argument("--db-path", default=DEFAULT_DB)

    enl = sub.add_parser("eu-node-list", help="list eu_nodes inventory")
    enl.add_argument("--state", choices=["active", "standby", "retired"], default=None)
    enl.add_argument("--db-path", default=DEFAULT_DB)
    enl.add_argument("--json", action="store_true")
```

Dispatch:

```python
    if args.cmd == "eu-node-add":
        return _cmd_eu_node_add(args)
    if args.cmd == "eu-node-retire":
        return _cmd_eu_node_retire(args)
    if args.cmd == "eu-node-list":
        return _cmd_eu_node_list(args)
```

Handlers:

```python
def _require_active_role(conn, cmd_name: str) -> int | None:
    """Returns exit code 2 if standby; None if active (continue)."""
    from mthydra.controller.state.node_state import current_node_state
    ns = current_node_state(conn)
    if ns.role != "active":
        print(f"{cmd_name}: refused — active-only command", file=sys.stderr)
        return 2
    return None


def _cmd_eu_node_add(args) -> int:
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.eu_nodes import add_eu_node

    conn = connect(args.db_path)
    try:
        rc = _require_active_role(conn, "eu-node-add")
        if rc is not None:
            return rc
        try:
            add_eu_node(
                conn,
                node_id=args.node_id,
                hostname=args.hostname,
                provider=args.provider,
                region=args.region,
                public_ip=args.public_ip,
                role=args.role,
                added_at=_now(),
                notes=args.notes,
            )
        except ValueError as e:
            print(f"eu-node-add: {e}", file=sys.stderr)
            return 2
        print(f"eu-node-add: {args.node_id} added (role={args.role})")
        return 0
    finally:
        conn.close()


def _cmd_eu_node_retire(args) -> int:
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.eu_nodes import retire_eu_node
    from mthydra.controller.state.node_state import current_node_state

    conn = connect(args.db_path)
    try:
        rc = _require_active_role(conn, "eu-node-retire")
        if rc is not None:
            return rc
        # Refuse to retire the local active.
        ns = current_node_state(conn)
        # We don't know the local node_id here without config; load it.
        from mthydra.controller.config import ConfigError, load_config
        try:
            cfg = load_config("/etc/mthydra/controller.toml")
            if cfg.standby.node_id == args.node_id and ns.role == "active":
                print(f"eu-node-retire: refusing to retire local active node "
                      f"{args.node_id}; promote a standby first", file=sys.stderr)
                return 2
        except (ConfigError, FileNotFoundError):
            pass  # cannot determine local id; proceed
        try:
            retire_eu_node(conn, args.node_id, at=_now())
        except ValueError as e:
            print(f"eu-node-retire: {e}", file=sys.stderr)
            return 2
        print(f"eu-node-retire: {args.node_id} retired")
        return 0
    finally:
        conn.close()


def _cmd_eu_node_list(args) -> int:
    import json as _json
    from dataclasses import asdict

    from mthydra.controller.state.db import connect
    from mthydra.controller.state.eu_nodes import list_eu_nodes

    conn = connect(args.db_path)
    try:
        nodes = list_eu_nodes(conn, role=args.state)
        if args.json:
            print(_json.dumps([asdict(n) for n in nodes], indent=2))
        else:
            print(f"{'node_id':30} {'role':10} {'hostname':30} last_heartbeat_at")
            for n in nodes:
                print(f"{n.node_id:30} {n.role:10} {n.hostname:30} "
                      f"{n.last_heartbeat_at or '-'}")
        return 0
    finally:
        conn.close()
```

- [ ] **Step 3: Run tests**

```bash
pytest tests/unit/controller/test_cli.py -k 'eu_node' -v
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/mthydra/controller/cli.py tests/unit/controller/test_cli.py
git commit -m "cli(F): eu-node-add, eu-node-retire, eu-node-list subcommands"
```

---

### Task 13: `standby-drill-proven` CLI

**Files:**
- Modify: `src/mthydra/controller/cli.py`
- Modify: `tests/unit/controller/test_cli.py`

- [ ] **Step 1: Write failing tests**

```python
def test_standby_drill_proven_case_a_proves_both_obligations(tmp_path, age_recipient):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    run(["eu-node-add", "eu-standby-de-1", "--hostname", "h",
         "--provider", "hetzner", "--region", "de", "--db-path", str(db)])

    # Stamp obligations to old timestamps first.
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.obligations import list_obligations, set_obligation
    conn = connect(db)
    set_obligation(conn, "t2_dryrun_caseA",
                   last_proven_at="2025-01-01T00:00:00Z",
                   proven_by="bootstrap",
                   next_due_at="2025-02-01T00:00:00Z")
    conn.close()

    rc = run(["standby-drill-proven", "--node-id", "eu-standby-de-1",
              "--case", "A", "--notes", "test drill",
              "--db-path", str(db)])
    assert rc == 0

    conn = connect(db)
    obs = {o.obligation_id: o for o in list_obligations(conn)}
    assert obs["t2_dryrun_caseA"].last_proven_at > "2025-01-01T00:00:00Z"
    assert "eu_standby_drill_proven::eu-standby-de-1" in obs
    conn.close()


def test_standby_drill_proven_case_b_proves_caseB(tmp_path, age_recipient):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    run(["eu-node-add", "eu-standby-de-1", "--hostname", "h",
         "--provider", "hetzner", "--region", "de", "--db-path", str(db)])

    from mthydra.controller.state.db import connect
    from mthydra.controller.state.obligations import list_obligations
    conn = connect(db)
    pre = next((o for o in list_obligations(conn) if o.obligation_id == "t2_dryrun_caseB"), None)
    pre_at = pre.last_proven_at if pre else None
    conn.close()

    run(["standby-drill-proven", "--node-id", "eu-standby-de-1",
         "--case", "B", "--db-path", str(db)])

    conn = connect(db)
    obs = {o.obligation_id: o for o in list_obligations(conn)}
    assert "t2_dryrun_caseB" in obs
    if pre_at is not None:
        assert obs["t2_dryrun_caseB"].last_proven_at > pre_at
    conn.close()
```

- [ ] **Step 2: Add subparser + dispatch + handler**

Subparser:

```python
    sdp = sub.add_parser("standby-drill-proven",
                          help="operator attests an end-to-end T2 §7 drill against a standby")
    sdp.add_argument("--node-id", required=True)
    sdp.add_argument("--case", choices=["A", "B"], required=True)
    sdp.add_argument("--notes", default=None)
    sdp.add_argument("--db-path", default=DEFAULT_DB)
```

Dispatch:

```python
    if args.cmd == "standby-drill-proven":
        return _cmd_standby_drill_proven(args)
```

Handler:

```python
def _cmd_standby_drill_proven(args) -> int:
    import json as _json

    from mthydra.controller.state.audit import log_event
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.obligations import prove, set_obligation

    conn = connect(args.db_path)
    try:
        rc = _require_active_role(conn, "standby-drill-proven")
        if rc is not None:
            return rc
        now = _now()
        case = args.case
        # Prove the case-specific T2 obligation.
        try:
            prove(conn, f"t2_dryrun_case{case}",
                  proven_by="operator", at=now,
                  next_due_at=_add_hours_iso(now, 30 * 24),
                  details=args.notes)
        except KeyError:
            # Obligation may not be seeded; seed it now.
            set_obligation(conn,
                           obligation_id=f"t2_dryrun_case{case}",
                           last_proven_at=now,
                           proven_by="operator",
                           next_due_at=_add_hours_iso(now, 30 * 24),
                           details=args.notes)
        # Prove per-node obligation.
        set_obligation(conn,
                       obligation_id=f"eu_standby_drill_proven::{args.node_id}",
                       last_proven_at=now,
                       proven_by="operator",
                       next_due_at=_add_hours_iso(now, 30 * 24),
                       details=args.notes)
        log_event(conn, ts=now, actor="operator", action="eu_standby_drill_proven",
                  target=args.node_id,
                  details_json=_json.dumps({"case": case, "notes": args.notes}))
        print(f"standby-drill-proven: case {case} attested for {args.node_id}")
        return 0
    finally:
        conn.close()
```

- [ ] **Step 3: Run tests**

```bash
pytest tests/unit/controller/test_cli.py -k 'standby_drill' -v
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/mthydra/controller/cli.py tests/unit/controller/test_cli.py
git commit -m "cli(F): standby-drill-proven subcommand — operator attests T2 §7 drill"
```

---

## Phase 8 — Role-gated serve

### Task 14: Branch `_cmd_serve` on `node_state.role`

**Files:**
- Modify: `src/mthydra/controller/cli.py`
- Modify: `tests/unit/controller/test_cli.py`

- [ ] **Step 1: Write failing tests**

```python
def test_serve_standby_arms_publisher_not_orchestrator(tmp_path, age_recipient, monkeypatch):
    """Standby serve loop: heartbeat publisher armed; backup/descriptor/cover-pool NOT."""
    import threading as _t
    from mthydra.controller.cli import run

    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    # Standby config with node_id set
    cfg_path.write_text(_MIN_TOML.replace("node_id                      = \"\"",
                                          "node_id                      = \"eu-standby-de-1\""))
    run(["init", "--role", "standby", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])

    recipient_file = tmp_path / "age-recipient.txt"
    recipient_file.write_text(age_recipient + "\n")
    monkeypatch.setattr("mthydra.controller.cli.DEFAULT_RECIPIENT_FILE",
                         str(recipient_file))

    # Make the daemon loop exit immediately.
    def _fast_wait(self, timeout=None):
        self.set()
        return True
    monkeypatch.setattr(_t.Event, "wait", _fast_wait)

    rc = run([
        "--mode", "offline",
        "--bucket-override", "off-bucket",
        "serve",
        "--db-path", str(db),
        "--config", str(cfg_path),
    ])
    assert rc == 0
```

- [ ] **Step 2: Modify `_cmd_serve` in `src/mthydra/controller/cli.py`**

Find the existing `_cmd_serve` function. At the top, after `cfg = load_config(args.config)`, read the runtime role:

```python
    from mthydra.controller.state.node_state import current_node_state
    conn = connect(args.db_path)
    try:
        ns = current_node_state(conn)
    finally:
        conn.close()
    role = ns.role
```

Then branch. The standby branch is added before the existing active-path setup:

```python
    if role == "standby":
        return _serve_standby(args, cfg, mode)
    # else active — existing code continues
```

Add the new function:

```python
def _serve_standby(args, cfg, mode: str) -> int:
    """Spec F standby serve loop: heartbeat publisher only, no mutating schedulers."""
    import signal
    import time

    from mthydra.controller.backup.s3_dest import S3Destination
    from mthydra.controller.standby.heartbeat import StandbyHeartbeatPublisher
    from mthydra.controller.state.audit import set_audit_mirror
    from mthydra.controller.state.tokens import get_provider_credential

    if not cfg.standby.node_id:
        print("serve: refused — standby role requires [standby].node_id in controller.toml",
              file=sys.stderr)
        return 2

    set_audit_mirror("/var/lib/mthydra/logs/audit.log")

    conn = connect(args.db_path)
    try:
        secret = get_provider_credential(conn, "b2")
    except KeyError:
        print("serve: b2 provider credential not in DB; standby cannot publish heartbeat",
              file=sys.stderr)
        return 7
    finally:
        conn.close()

    dest = _build_destination(cfg, secret, mode=mode, bucket_override=args.bucket_override)
    publisher = StandbyHeartbeatPublisher(
        node_id=cfg.standby.node_id,
        b2_destination=dest,
        interval_seconds=cfg.standby.heartbeat_interval_seconds,
        mode=mode,
    )
    if mode != "offline":
        publisher.arm()
        print(f"serve: standby heartbeat armed (node_id={cfg.standby.node_id})", flush=True)
    else:
        print("serve: standby in offline mode — heartbeat not armed", flush=True)

    stop_event = _install_signal_handler()
    try:
        while not stop_event.is_set():
            stop_event.wait(timeout=60)
    finally:
        publisher.disarm()
        print("serve: standby stopped", flush=True)
    return 0
```

Then, in the active branch, after the `rotation_sweep` is constructed, **add the heartbeat poller**:

```python
    from mthydra.controller.standby.heartbeat import StandbyHeartbeatPoller
    poller = StandbyHeartbeatPoller(
        db_path=args.db_path,
        b2_destination=dest,
        poll_interval_seconds=cfg.standby.heartbeat_poll_interval_seconds,
        staleness_alert_seconds=cfg.standby.staleness_alert_seconds,
        mode=mode,
    )
```

In the `if args.mode != "offline":` arm block, add:

```python
        poller.arm()
        print("serve: backup + descriptor + cover-pool sweeps + standby poller armed", flush=True)
```

In the `finally:` disarm block:

```python
        poller.disarm()
```

- [ ] **Step 3: Run tests**

```bash
pytest tests/unit/controller/test_cli.py -k serve -v
```

Expected: PASS (existing serve tests + the new standby smoke test).

- [ ] **Step 4: Run full suite**

```bash
pytest tests/unit -q
```

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/cli.py tests/unit/controller/test_cli.py
git commit -m "serve(F): role-gated branch — standby runs heartbeat publisher; active adds poller"
```

---

## Phase 9 — Integration

### Task 15: Promotion lifecycle integration test

**Files:**
- Create: `tests/integration/test_promotion_lifecycle.py`

- [ ] **Step 1: Write the integration test**

```python
"""Spec F — end-to-end promotion lifecycle.

Active DB is created + populated. backup-now produces an encrypted blob.
The blob is decrypted + promoted onto a separately-init'd standby skeleton DB.
After promotion, the standby's DB is the active's state.
"""
import shutil
import subprocess

import pytest

from mthydra.controller.bootstrap import init_state
from mthydra.controller.promote import promote_active
from mthydra.controller.state.audit import recent_events
from mthydra.controller.state.cover_pool import add_candidate, attest_verified
from mthydra.controller.state.db import connect
from mthydra.controller.state.invariants import check_all
from mthydra.controller.state.node_state import current_node_state
from mthydra.controller.state.schema import SCHEMA_VERSION


@pytest.fixture
def age_keypair(tmp_path):
    if shutil.which("age-keygen") is None:
        pytest.skip("age-keygen not installed")
    keyfile = tmp_path / "identity"
    subprocess.run(["age-keygen", "-o", str(keyfile)], capture_output=True, check=True)
    for line in keyfile.read_text().splitlines():
        if line.startswith("# public key: "):
            return keyfile, line.removeprefix("# public key: ").strip()
    raise RuntimeError("no public key line")


def test_promotion_lifecycle(tmp_path, age_keypair):
    identity, recipient = age_keypair

    # 1. Set up the active "donor" DB with some real state.
    active_db = tmp_path / "active.sqlite"
    init_state(
        db_path=active_db,
        age_recipient=recipient,
        provider_credentials={"b2": "id:secret"},
        obligation_timer_hours={"backup_restore_dryrun": 720},
        now="2026-05-20T00:00:00Z",
        role="active",
    )
    conn = connect(active_db)
    add_candidate(conn, "live.org", added_at="2026-05-20T00:30:00Z")
    attest_verified(conn, "live.org", from_vantage="ru-vps-01",
                    at="2026-05-20T00:35:00Z")
    conn.close()

    # 2. Encrypt the active DB (simulating a backup blob).
    blob = tmp_path / "backup.age"
    from mthydra.controller.backup.age_crypt import encrypt_file
    encrypt_file(active_db, recipient=recipient, out=blob)

    # 3. Set up a separate standby skeleton DB.
    standby_db = tmp_path / "standby.sqlite"
    init_state(
        db_path=standby_db,
        age_recipient=recipient,
        provider_credentials={"b2": "id:secret"},
        obligation_timer_hours={},
        now="2026-05-20T01:00:00Z",
        role="standby",
    )
    conn = connect(standby_db)
    ns = current_node_state(conn)
    assert ns.role == "standby"
    conn.close()

    # 4. Promote the standby with Case A.
    checklist = promote_active(
        db_path=standby_db,
        backup_blob=blob,
        age_identity=identity,
        case="A",
        node_id="eu-promoted-1",
        now="2026-05-20T02:00:00Z",
    )
    assert checklist is None

    # 5. Verify the promoted DB carries the active's full state.
    conn = connect(standby_db)
    ns = current_node_state(conn)
    assert ns.role == "active"
    assert ns.previous_role == "standby"
    assert ns.promotion_case == "A"

    cnt = conn.execute(
        "SELECT COUNT(*) FROM credential_authority WHERE retired_at IS NULL"
    ).fetchone()[0]
    assert cnt == 1

    # cover_pool row carried over
    pool = conn.execute(
        "SELECT domain, state FROM cover_domain_pool WHERE domain='live.org'"
    ).fetchone()
    assert pool == ("live.org", "candidate_verified")

    # startup-check passes
    check_all(conn, expected_schema_version=SCHEMA_VERSION,
              now_iso="2026-05-20T02:01:00Z")

    # audit log contains the promotion event
    actions = {e.action for e in recent_events(conn, limit=50)}
    assert "eu_node_promoted" in actions
    assert "node_role_set" in actions
    conn.close()
```

- [ ] **Step 2: Run the integration test**

```bash
pytest tests/integration/test_promotion_lifecycle.py -v
```

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_promotion_lifecycle.py
git commit -m "test(F): end-to-end promotion lifecycle — backup, decrypt, promote, verify"
```

---

## Phase 10 — Final verification

### Task 16: Full pytest + coverage + smoke

- [ ] **Step 1: Run the full test suite**

```bash
cd /home/asharov/RedHat/Dev/mthydra
pytest -q --ignore=tests/integration/test_gap_monitor.py
```

Expected: all green. Should be in the 280+ test range (spec F adds ~30 new tests).

- [ ] **Step 2: Run coverage on the new modules**

```bash
pytest --cov=mthydra.controller.state.node_state \
       --cov=mthydra.controller.state.eu_nodes \
       --cov=mthydra.controller.standby.heartbeat \
       --cov=mthydra.controller.promote \
       --cov-report=term-missing tests/ \
       --ignore=tests/integration/test_gap_monitor.py
```

Expected: ≥ 90% line coverage on each of the four new modules.

- [ ] **Step 3: CLI end-to-end smoke**

```bash
TMP=$(mktemp -d)
age-keygen -o "$TMP/active-id" 2>/dev/null
PUB_ACTIVE=$(grep '# public key:' "$TMP/active-id" | awk '{print $4}')
age-keygen -o "$TMP/standby-id" 2>/dev/null
PUB_STANDBY=$(grep '# public key:' "$TMP/standby-id" | awk '{print $4}')

# Initialise active.
.venv/bin/mthydra-controller init \
  --db-path "$TMP/active.sqlite" \
  --age-recipient "$PUB_ACTIVE" \
  --provider-credential "b2=id:secret"

# Add a standby to inventory.
.venv/bin/mthydra-controller eu-node-add eu-standby-de-1 \
  --hostname standby.example --provider hetzner --region de \
  --db-path "$TMP/active.sqlite"

# Initialise standby (separately).
.venv/bin/mthydra-controller init --role standby \
  --db-path "$TMP/standby.sqlite" \
  --age-recipient "$PUB_STANDBY" \
  --provider-credential "b2=id:secret"

# List inventory.
.venv/bin/mthydra-controller eu-node-list --db-path "$TMP/active.sqlite"

# Attest a drill.
.venv/bin/mthydra-controller standby-drill-proven \
  --node-id eu-standby-de-1 --case A --notes "smoke test" \
  --db-path "$TMP/active.sqlite"

# Rotate authority on the active.
cp packaging/etc/mthydra/controller.toml.example "$TMP/controller.toml"
.venv/bin/mthydra-controller authority-rotate \
  --db-path "$TMP/active.sqlite" --config "$TMP/controller.toml"

rm -rf "$TMP"
echo "smoke ok"
```

Expected: each command exits 0; `eu-node-list` shows the standby; the drill attestation succeeds; `authority-rotate` reports a new generation.

- [ ] **Step 4: Verify all 23 commits**

```bash
git log --oneline | head -25
```

You should see ~16 spec-F commits on top of the spec-C commits.

---

## Done criteria

- All 16 task checkboxes ticked.
- `pytest -q` passes cleanly.
- ≥ 90% line coverage on `mthydra.controller.state.node_state`, `mthydra.controller.state.eu_nodes`, `mthydra.controller.standby.heartbeat`, `mthydra.controller.promote`.
- All 6 new CLI subcommands work end-to-end via the smoke test.
- Integration test demonstrates round-trip: active DB → encrypted blob → decrypted onto skeleton standby → promoted to active with all state intact.
- `node_state` singleton + `eu_nodes` inventory survive backup/restore (the existing spec C lifecycle test already verifies arbitrary table survival; this is implicitly proven).
- Startup invariants #21–#23 cover the singleton + role-state consistency.
