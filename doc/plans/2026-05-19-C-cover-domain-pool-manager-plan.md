# Spec C — Cover-Domain Pool Manager — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the cover-domain pool manager as defined in `doc/specs/2026-05-19-C-cover-domain-pool-manager.md`. Completes T5 (`doc/design.md` §10): structural never-reuse via SQLite triggers, operator-attested Russia-vantage verification gate, rotation-TTL scheduler, reverify-TTL sweep, pool-low freeze policy, and six new CLI subcommands.

**Architecture:** Schema v2→v3 forward migration (add `entered_in_use_at`, install two triggers, bump version). The existing thin `state/cover_pool.py` repository is rewritten in place: `mark_verified` → `attest_verified` (gains evidence + audit), `move_to_in_use` → `assign_to_box` (sets `entered_in_use_at`, refuses stale). New repository functions `downgrade_stale_verified`, `list_due_for_rotation`, `pool_health`, `rotate_and_burn`. Startup self-check gains invariants #17–#20. Two APScheduler classes — `CoverPoolReverifySweep` and `CoverPoolRotationSweep` — wired into `serve`, mirroring `DescriptorRotator`. Six CLI subcommands. `CoverPoolConfig` dataclass loads from `[cover_pool]` TOML section.

**Tech stack:** Same as spec A/B — Python 3.12, stdlib `sqlite3`, `APScheduler`, `pytest` + `hypothesis`. No new runtime deps.

**Design decisions:** See spec §2 (C-D1 through C-D8).

---

## File Structure (locked before tasks)

**Modified:**
- `src/mthydra/controller/state/schema.py` — bump `SCHEMA_VERSION` to 3; add `_V3_MIGRATION`; add `migrate_v2_to_v3`; add trigger DDL to `_STATEMENTS`; add `entered_in_use_at` column.
- `src/mthydra/controller/state/cover_pool.py` — rewrite: rename + extend functions, add audit-log emission per transition, add 4 new functions, add `PoolHealth` dataclass, add `entered_in_use_at` to `CoverDomain`.
- `src/mthydra/controller/state/invariants.py` — extend `check_all()` with invariants #17–#20.
- `src/mthydra/controller/config.py` — add `CoverPoolConfig` dataclass; load `[cover_pool]` TOML section.
- `src/mthydra/controller/bootstrap.py` — seed spec-C obligations (`cover_pool_reverify_pass_proven`, `cover_pool_replenishment_proven`).
- `src/mthydra/controller/cli.py` — add 6 subparsers + 6 `_cmd_cover_*` handlers; serve wires both schedulers.
- `packaging/etc/mthydra/controller.toml.example` — add `[cover_pool]` section.
- `tests/unit/controller/state/test_cover_pool.py` — update for new API (rename, evidence, audit, `entered_in_use_at`); add unit tests for new functions.
- `tests/unit/controller/state/test_invariants.py` — add invariant #17–#20 tests.

**Created:**
- `src/mthydra/controller/state/cover_pool_scheduler.py` — `CoverPoolReverifySweep` + `CoverPoolRotationSweep` classes (mirror `descriptor.scheduler`).
- `tests/unit/controller/state/test_cover_pool_triggers.py` — trigger-level tests.
- `tests/unit/controller/state/test_cover_pool_scheduler.py` — scheduler tests with mock clock.
- `tests/property/test_cover_pool_invariants.py` — Hypothesis property test for state-machine invariants.
- `tests/integration/test_cover_pool_lifecycle.py` — end-to-end lifecycle + backup/restore round-trip.

Responsibility per file: schema owns DDL + migrations; cover_pool owns the state-machine semantics + audit emission; cover_pool_scheduler owns timer plumbing; invariants own startup self-checks; config owns TOML loading; bootstrap owns first-run seeding; cli owns argparse + dispatch. No cross-imports between scheduler and CLI — both depend on the repository.

---

## Phase 1 — Schema v2 → v3

### Task 1: Schema migration, triggers, and column

**Files:**
- Modify: `src/mthydra/controller/state/schema.py`
- Modify: `tests/unit/controller/state/test_schema.py`

- [ ] **Step 1: Write failing schema-version test**

Add to `tests/unit/controller/state/test_schema.py`:

```python
def test_schema_version_is_3(tmp_db_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import SCHEMA_VERSION, apply_schema
    assert SCHEMA_VERSION == 3
    conn = connect(tmp_db_path)
    apply_schema(conn)
    row = conn.execute("SELECT version FROM schema_version WHERE rowid=1").fetchone()
    assert row[0] == 3


def test_cover_pool_has_entered_in_use_at(tmp_db_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    conn = connect(tmp_db_path)
    apply_schema(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(cover_domain_pool)").fetchall()]
    assert "entered_in_use_at" in cols


def test_triggers_present(tmp_db_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    conn = connect(tmp_db_path)
    apply_schema(conn)
    triggers = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
    }
    assert "cover_pool_reject_burned" in triggers
    assert "burned_domains_no_delete" in triggers


def test_v2_to_v3_migration_adds_column_and_triggers(tmp_db_path):
    import sqlite3
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema, migrate_v2_to_v3
    # Manually construct a v2 DB (no entered_in_use_at, no triggers)
    conn = connect(tmp_db_path)
    conn.executescript(
        "CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL, CHECK (rowid=1));"
        "INSERT INTO schema_version (rowid, version, applied_at) VALUES (1, 2, '2026-05-19T00:00:00Z');"
        "CREATE TABLE cover_domain_pool ("
        "  domain TEXT PRIMARY KEY, state TEXT NOT NULL, last_verified_at TEXT,"
        "  verified_from_vantage TEXT, assigned_box_id TEXT, added_at TEXT NOT NULL, notes TEXT);"
        "CREATE TABLE burned_domains ("
        "  domain TEXT PRIMARY KEY, burned_at TEXT NOT NULL, reason TEXT NOT NULL,"
        "  last_box_id TEXT, details TEXT);"
    )
    conn.commit()
    migrate_v2_to_v3(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(cover_domain_pool)").fetchall()]
    assert "entered_in_use_at" in cols
    v = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    assert v == 3
    # Trigger refuses INSERT of burned domain
    conn.execute(
        "INSERT INTO burned_domains (domain, burned_at, reason) VALUES ('x.org', '2026-05-19T00:00:00Z', 'manual')"
    )
    conn.commit()
    try:
        conn.execute(
            "INSERT INTO cover_domain_pool (domain, state, added_at) "
            "VALUES ('x.org', 'candidate_unverified', '2026-05-19T01:00:00Z')"
        )
    except sqlite3.IntegrityError as e:
        assert "burned_domains" in str(e)
    else:
        raise AssertionError("expected IntegrityError")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/controller/state/test_schema.py -v`
Expected: 4 failures (SCHEMA_VERSION != 3; missing column; missing triggers; missing `migrate_v2_to_v3`).

- [ ] **Step 3: Edit `src/mthydra/controller/state/schema.py`**

Change `SCHEMA_VERSION` and extend `_STATEMENTS`:

```python
SCHEMA_VERSION = 3
```

Replace the `cover_domain_pool` CREATE TABLE statement in `_STATEMENTS` with the v3 form (adds `entered_in_use_at`):

```python
    """
    CREATE TABLE IF NOT EXISTS cover_domain_pool (
      domain                TEXT PRIMARY KEY,
      state                 TEXT NOT NULL CHECK (state IN ('candidate_unverified','candidate_verified','in_use')),
      last_verified_at      TEXT,
      verified_from_vantage TEXT,
      assigned_box_id       TEXT,
      added_at              TEXT NOT NULL,
      notes                 TEXT,
      entered_in_use_at     TEXT
    )
    """,
```

Append two trigger statements to `_STATEMENTS` (place at the end, after `eu_exit_set`):

```python
    # --- spec C additions: structural enforcement of T5 burned-set rule ---
    """
    CREATE TRIGGER IF NOT EXISTS cover_pool_reject_burned
    BEFORE INSERT ON cover_domain_pool
    WHEN EXISTS (SELECT 1 FROM burned_domains WHERE domain = NEW.domain)
    BEGIN
      SELECT RAISE(ABORT, 'cover-pool: domain is in burned_domains; never reuse');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS burned_domains_no_delete
    BEFORE DELETE ON burned_domains
    BEGIN
      SELECT RAISE(ABORT, 'cover-pool: burned_domains is append-only');
    END
    """,
```

Add `migrate_v2_to_v3` after `migrate_v1_to_v2`:

```python
_V3_MIGRATION_TRIGGERS: list[str] = [
    """
    CREATE TRIGGER IF NOT EXISTS cover_pool_reject_burned
    BEFORE INSERT ON cover_domain_pool
    WHEN EXISTS (SELECT 1 FROM burned_domains WHERE domain = NEW.domain)
    BEGIN
      SELECT RAISE(ABORT, 'cover-pool: domain is in burned_domains; never reuse');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS burned_domains_no_delete
    BEFORE DELETE ON burned_domains
    BEGIN
      SELECT RAISE(ABORT, 'cover-pool: burned_domains is append-only');
    END
    """,
]


def migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
    """Idempotent v2 → v3 migration: add entered_in_use_at + spec-C triggers."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(cover_domain_pool)").fetchall()]
    if "entered_in_use_at" not in cols:
        conn.execute("ALTER TABLE cover_domain_pool ADD COLUMN entered_in_use_at TEXT")
    for stmt in _V3_MIGRATION_TRIGGERS:
        conn.execute(stmt)
    conn.execute(
        "UPDATE schema_version SET version=?, applied_at=? WHERE rowid=1",
        (3, _now()),
    )
    conn.commit()
```

Update the migration dispatcher inside `apply_schema` — replace:

```python
        if current < 2:
            migrate_v1_to_v2(conn)
```

with:

```python
        if current < 2:
            migrate_v1_to_v2(conn)
        if current < 3:
            migrate_v2_to_v3(conn)
```

- [ ] **Step 4: Run schema tests to verify they pass**

Run: `pytest tests/unit/controller/state/test_schema.py -v`
Expected: PASS (4/4 new tests).

- [ ] **Step 5: Run the rest of the existing test suite — invariants test depends on this**

Run: `pytest tests/unit -q`
Expected: existing tests still pass (invariant `test_check_all_passes_on_clean_seeded_db` will continue to pass because the new column is nullable and the existing pool/burned overlap test does not exercise triggers).

- [ ] **Step 6: Commit**

```bash
git add src/mthydra/controller/state/schema.py tests/unit/controller/state/test_schema.py
git commit -m "schema(C): v2→v3 migration — entered_in_use_at + burned-set triggers"
```

---

### Task 2: Trigger-level behavioural tests

**Files:**
- Create: `tests/unit/controller/state/test_cover_pool_triggers.py`

- [ ] **Step 1: Write failing test**

```python
"""Spec C structural enforcement — SQLite triggers on cover_domain_pool / burned_domains."""
import sqlite3

import pytest

from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema


@pytest.fixture
def conn(tmp_db_path):
    c = connect(tmp_db_path)
    apply_schema(c)
    return c


def _insert_burned(c, domain: str) -> None:
    c.execute(
        "INSERT INTO burned_domains (domain, burned_at, reason) VALUES (?, '2026-05-19T00:00:00Z', 'manual')",
        (domain,),
    )
    c.commit()


def test_trigger_blocks_insert_of_burned_domain(conn):
    _insert_burned(conn, "burned.org")
    with pytest.raises(sqlite3.IntegrityError) as exc:
        conn.execute(
            "INSERT INTO cover_domain_pool (domain, state, added_at) "
            "VALUES ('burned.org', 'candidate_unverified', '2026-05-19T01:00:00Z')"
        )
        conn.commit()
    assert "burned_domains" in str(exc.value)
    n = conn.execute("SELECT COUNT(*) FROM cover_domain_pool").fetchone()[0]
    assert n == 0


def test_trigger_blocks_delete_from_burned_domains(conn):
    _insert_burned(conn, "appendonly.org")
    with pytest.raises(sqlite3.IntegrityError) as exc:
        conn.execute("DELETE FROM burned_domains WHERE domain='appendonly.org'")
        conn.commit()
    assert "append-only" in str(exc.value)
    n = conn.execute("SELECT COUNT(*) FROM burned_domains").fetchone()[0]
    assert n == 1


def test_trigger_allows_normal_insert_for_non_burned_domain(conn):
    conn.execute(
        "INSERT INTO cover_domain_pool (domain, state, added_at) "
        "VALUES ('fresh.org', 'candidate_unverified', '2026-05-19T00:00:00Z')"
    )
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM cover_domain_pool").fetchone()[0]
    assert n == 1


def test_trigger_survives_vacuum(conn):
    _insert_burned(conn, "vacuumed.org")
    conn.execute("VACUUM")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO cover_domain_pool (domain, state, added_at) "
            "VALUES ('vacuumed.org', 'candidate_unverified', '2026-05-19T01:00:00Z')"
        )
        conn.commit()
```

- [ ] **Step 2: Run tests to verify they pass**

Run: `pytest tests/unit/controller/state/test_cover_pool_triggers.py -v`
Expected: PASS (4/4).

- [ ] **Step 3: Commit**

```bash
git add tests/unit/controller/state/test_cover_pool_triggers.py
git commit -m "test(C): trigger-level behavioural tests for burned-set rule"
```

---

## Phase 2 — Repository rewrite (`state/cover_pool.py`)

### Task 3: Rename `mark_verified` → `attest_verified`; add evidence + audit

**Files:**
- Modify: `src/mthydra/controller/state/cover_pool.py`
- Modify: `tests/unit/controller/state/test_cover_pool.py`

- [ ] **Step 1: Update the existing test for the renamed function and audit emission**

Replace `tests/unit/controller/state/test_cover_pool.py` entirely:

```python
"""Spec C — cover-domain pool state machine + audit emission."""
import pytest

from mthydra.controller.state.audit import recent_events
from mthydra.controller.state.burned import is_burned, mark_burned
from mthydra.controller.state.cover_pool import (
    add_candidate,
    assign_to_box,
    attest_verified,
    downgrade_stale_verified,
    list_by_state,
    list_due_for_rotation,
    pool_health,
    rotate_and_burn,
)
from mthydra.controller.state.db import connect
from mthydra.controller.state.ru_boxes import insert_box, mark_live
from mthydra.controller.state.schema import apply_schema

NOW = "2026-05-19T00:00:00Z"


@pytest.fixture
def conn(tmp_db_path):
    c = connect(tmp_db_path)
    apply_schema(c)
    return c


def _seed_live_box(c, box_id: str = "box-1", sni: str = "box-sni.invalid") -> None:
    insert_box(c, box_id, "aws", "eu-west-1", "10.0.0.1", sni, "img-v1", NOW)
    mark_live(c, box_id, public_ip="10.0.0.1", at=NOW)


def test_add_candidate_emits_audit(conn):
    add_candidate(conn, "example.org", added_at=NOW)
    rows = list_by_state(conn, "candidate_unverified")
    assert [r.domain for r in rows] == ["example.org"]
    ev = recent_events(conn, limit=1)
    assert ev[0].action == "cover_added"
    assert ev[0].target == "example.org"


def test_attest_verified_records_vantage_and_evidence(conn):
    add_candidate(conn, "example.org", added_at=NOW)
    attest_verified(
        conn,
        "example.org",
        from_vantage="ru-vps-01",
        at="2026-05-19T01:00:00Z",
        evidence="curl from RU vps: 200 OK + matching cert chain",
    )
    rows = list_by_state(conn, "candidate_verified")
    assert rows[0].verified_from_vantage == "ru-vps-01"
    assert rows[0].last_verified_at == "2026-05-19T01:00:00Z"
    ev = recent_events(conn, limit=1)
    assert ev[0].action == "cover_attest_verified"
    assert ev[0].target == "example.org"
    assert "curl from RU vps" in (ev[0].details_json or "")


def test_attest_verified_rejects_non_unverified(conn):
    add_candidate(conn, "example.org", added_at=NOW)
    attest_verified(conn, "example.org", from_vantage="ru-vps-01", at=NOW)
    with pytest.raises(ValueError, match="candidate_unverified"):
        attest_verified(conn, "example.org", from_vantage="ru-vps-01", at=NOW)
```

(Additional tests for the rest of the API are added in Tasks 4–8 as each function lands.)

- [ ] **Step 2: Run tests to verify they fail with import errors**

Run: `pytest tests/unit/controller/state/test_cover_pool.py -v`
Expected: ImportError on `attest_verified`, `assign_to_box`, `downgrade_stale_verified`, `list_due_for_rotation`, `pool_health`, `rotate_and_burn`.

- [ ] **Step 3: Rewrite `src/mthydra/controller/state/cover_pool.py`**

Full replacement file:

```python
"""Cover-domain pool repository — spec C §5.

State machine (spec C §4):
    ∅ → candidate_unverified → candidate_verified → in_use → burned

Every state transition emits one audit_log row. Audit is the durable
record of operator-attested verification (C-D1).
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from mthydra.controller.state.audit import log_event


@dataclass(frozen=True)
class CoverDomain:
    domain: str
    state: str
    last_verified_at: str | None
    verified_from_vantage: str | None
    assigned_box_id: str | None
    added_at: str
    notes: str | None
    entered_in_use_at: str | None


@dataclass(frozen=True)
class PoolHealth:
    candidate_unverified: int
    candidate_verified: int
    in_use: int
    burned: int
    rotation_frozen: bool
    oldest_in_use_at: str | None
    oldest_unverified_at: str | None
    last_attest_at: str | None


_SELECT_COLS = (
    "domain, state, last_verified_at, verified_from_vantage, "
    "assigned_box_id, added_at, notes, entered_in_use_at"
)


def add_candidate(
    conn: sqlite3.Connection,
    domain: str,
    *,
    added_at: str,
    notes: str | None = None,
    actor: str = "operator",
) -> None:
    """Insert a fresh candidate. Trigger raises if domain is burned."""
    conn.execute(
        "INSERT INTO cover_domain_pool (domain, state, added_at, notes) "
        "VALUES (?, 'candidate_unverified', ?, ?)",
        (domain, added_at, notes),
    )
    log_event(
        conn, ts=added_at, actor=actor, action="cover_added",
        target=domain, details_json=json.dumps({"notes": notes}) if notes else None,
    )
    conn.commit()


def attest_verified(
    conn: sqlite3.Connection,
    domain: str,
    *,
    from_vantage: str,
    at: str,
    evidence: str | None = None,
    actor: str = "operator",
) -> None:
    """candidate_unverified → candidate_verified (C-D1, operator-attested for MVP)."""
    cur = conn.execute(
        "UPDATE cover_domain_pool SET state='candidate_verified', "
        "verified_from_vantage=?, last_verified_at=? "
        "WHERE domain=? AND state='candidate_unverified'",
        (from_vantage, at, domain),
    )
    if cur.rowcount == 0:
        raise ValueError(
            f"domain {domain!r} is not in candidate_unverified state"
        )
    details = json.dumps(
        {"vantage": from_vantage, "evidence": evidence},
        separators=(",", ":"),
    )
    log_event(
        conn, ts=at, actor=actor, action="cover_attest_verified",
        target=domain, details_json=details,
    )
    conn.commit()


def downgrade_stale_verified(
    conn: sqlite3.Connection,
    *,
    now: str,
    reverify_after_days: int,
    actor: str = "reverify_sweep",
) -> list[str]:
    """candidate_verified → candidate_unverified for rows past reverify TTL.

    Returns the list of downgraded domains; emits one audit row per.
    """
    cutoff = _iso_minus_days(now, reverify_after_days)
    rows = conn.execute(
        "SELECT domain FROM cover_domain_pool "
        "WHERE state='candidate_verified' AND last_verified_at < ?",
        (cutoff,),
    ).fetchall()
    downgraded: list[str] = [r[0] for r in rows]
    for domain in downgraded:
        conn.execute(
            "UPDATE cover_domain_pool SET state='candidate_unverified', "
            "verified_from_vantage=NULL "
            "WHERE domain=? AND state='candidate_verified'",
            (domain,),
        )
        log_event(
            conn, ts=now, actor=actor, action="cover_downgraded_stale",
            target=domain,
            details_json=json.dumps({"cutoff": cutoff}),
        )
    conn.commit()
    return downgraded


def assign_to_box(
    conn: sqlite3.Connection,
    domain: str,
    *,
    box_id: str,
    at: str,
    actor: str = "controller",
) -> None:
    """candidate_verified → in_use. Sets entered_in_use_at + assigned_box_id.

    Raises if domain is not in candidate_verified state (covers stale-verified
    after sweep downgrade). Does NOT consult freeze_threshold — freeze affects
    only the rotation sweep (spec C-D4).
    """
    cur = conn.execute(
        "UPDATE cover_domain_pool SET state='in_use', assigned_box_id=?, "
        "entered_in_use_at=? "
        "WHERE domain=? AND state='candidate_verified'",
        (box_id, at, domain),
    )
    if cur.rowcount == 0:
        raise ValueError(f"domain {domain!r} is not in candidate_verified state")
    log_event(
        conn, ts=at, actor=actor, action="cover_assigned",
        target=domain, details_json=json.dumps({"box_id": box_id}),
    )
    conn.commit()


def list_by_state(conn: sqlite3.Connection, state: str) -> list[CoverDomain]:
    rows = conn.execute(
        f"SELECT {_SELECT_COLS} FROM cover_domain_pool WHERE state=? ORDER BY domain",
        (state,),
    ).fetchall()
    return [CoverDomain(*r) for r in rows]


def list_due_for_rotation(
    conn: sqlite3.Connection,
    *,
    now: str,
    rotation_ttl_days: int,
) -> list[CoverDomain]:
    """Return in_use rows where now - entered_in_use_at > rotation_ttl_days."""
    cutoff = _iso_minus_days(now, rotation_ttl_days)
    rows = conn.execute(
        f"SELECT {_SELECT_COLS} FROM cover_domain_pool "
        "WHERE state='in_use' AND entered_in_use_at IS NOT NULL "
        "AND entered_in_use_at < ? ORDER BY entered_in_use_at",
        (cutoff,),
    ).fetchall()
    return [CoverDomain(*r) for r in rows]


def pool_health(
    conn: sqlite3.Connection,
    *,
    freeze_threshold: int = 2,
) -> PoolHealth:
    counts = {
        "candidate_unverified": 0,
        "candidate_verified": 0,
        "in_use": 0,
    }
    for state, n in conn.execute(
        "SELECT state, COUNT(*) FROM cover_domain_pool GROUP BY state"
    ).fetchall():
        counts[state] = n
    burned = conn.execute("SELECT COUNT(*) FROM burned_domains").fetchone()[0]

    oldest_in_use = conn.execute(
        "SELECT MIN(entered_in_use_at) FROM cover_domain_pool WHERE state='in_use'"
    ).fetchone()[0]
    oldest_unverified = conn.execute(
        "SELECT MIN(added_at) FROM cover_domain_pool WHERE state='candidate_unverified'"
    ).fetchone()[0]
    last_attest = conn.execute(
        "SELECT MAX(last_verified_at) FROM cover_domain_pool "
        "WHERE last_verified_at IS NOT NULL"
    ).fetchone()[0]

    return PoolHealth(
        candidate_unverified=counts["candidate_unverified"],
        candidate_verified=counts["candidate_verified"],
        in_use=counts["in_use"],
        burned=burned,
        rotation_frozen=counts["candidate_verified"] < freeze_threshold,
        oldest_in_use_at=oldest_in_use,
        oldest_unverified_at=oldest_unverified,
        last_attest_at=last_attest,
    )


def rotate_and_burn(
    conn: sqlite3.Connection,
    domain: str,
    *,
    reason: str,
    last_box_id: str,
    at: str,
    details: str | None = None,
    actor: str = "operator",
) -> None:
    """in_use → burned (atomic via burned.mark_burned).

    Asserts state='in_use' first; emits a cover_rotated audit row before
    mark_burned commits the burn. mark_burned itself emits a cover_burned
    audit row (introduced by spec A) — both rows survive.
    """
    from mthydra.controller.state.burned import mark_burned

    row = conn.execute(
        "SELECT state FROM cover_domain_pool WHERE domain=?", (domain,)
    ).fetchone()
    if row is None:
        raise ValueError(f"domain {domain!r} not in cover_domain_pool")
    if row[0] != "in_use":
        raise ValueError(
            f"cover-pool: {domain!r} is not in_use (state={row[0]})"
        )
    log_event(
        conn, ts=at, actor=actor, action="cover_rotated",
        target=domain,
        details_json=json.dumps({"reason": reason, "last_box_id": last_box_id}),
    )
    mark_burned(conn, domain, reason, last_box_id, at, details)


def _iso_minus_days(iso: str, days: int) -> str:
    from datetime import timedelta
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (t - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
```

- [ ] **Step 4: Verify the Task 3 subset of tests passes**

Run: `pytest tests/unit/controller/state/test_cover_pool.py::test_add_candidate_emits_audit tests/unit/controller/state/test_cover_pool.py::test_attest_verified_records_vantage_and_evidence tests/unit/controller/state/test_cover_pool.py::test_attest_verified_rejects_non_unverified -v`
Expected: PASS (3/3).

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/state/cover_pool.py tests/unit/controller/state/test_cover_pool.py
git commit -m "cover_pool(C): rewrite repository — audit-emitting transitions, attest_verified rename"
```

---

### Task 4: `assign_to_box` (replaces `move_to_in_use`)

**Files:**
- Modify: `tests/unit/controller/state/test_cover_pool.py`

- [ ] **Step 1: Append failing tests**

```python
def test_assign_to_box_sets_entered_in_use_at(conn):
    _seed_live_box(conn, "box-1")
    add_candidate(conn, "example.org", added_at=NOW)
    attest_verified(conn, "example.org", from_vantage="ru-vps-01", at=NOW)
    assign_to_box(conn, "example.org", box_id="box-1", at="2026-05-20T00:00:00Z")
    rows = list_by_state(conn, "in_use")
    assert rows[0].entered_in_use_at == "2026-05-20T00:00:00Z"
    assert rows[0].assigned_box_id == "box-1"


def test_assign_to_box_refuses_non_verified(conn):
    _seed_live_box(conn, "box-1")
    add_candidate(conn, "example.org", added_at=NOW)
    # not yet attested
    with pytest.raises(ValueError, match="candidate_verified"):
        assign_to_box(conn, "example.org", box_id="box-1", at=NOW)


def test_assign_to_box_refuses_stale_after_downgrade(conn):
    _seed_live_box(conn, "box-1")
    add_candidate(conn, "example.org", added_at="2026-04-01T00:00:00Z")
    attest_verified(
        conn, "example.org", from_vantage="ru-vps-01",
        at="2026-04-01T01:00:00Z",
    )
    downgrade_stale_verified(
        conn, now="2026-05-19T00:00:00Z", reverify_after_days=30,
    )
    with pytest.raises(ValueError, match="candidate_verified"):
        assign_to_box(conn, "example.org", box_id="box-1", at=NOW)
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/unit/controller/state/test_cover_pool.py::test_assign_to_box_sets_entered_in_use_at tests/unit/controller/state/test_cover_pool.py::test_assign_to_box_refuses_non_verified tests/unit/controller/state/test_cover_pool.py::test_assign_to_box_refuses_stale_after_downgrade -v`
Expected: PASS (3/3). (`assign_to_box` was already implemented in Task 3.)

- [ ] **Step 3: Commit**

```bash
git add tests/unit/controller/state/test_cover_pool.py
git commit -m "test(C): assign_to_box transition + stale refusal coverage"
```

---

### Task 5: `downgrade_stale_verified` boundary tests

**Files:**
- Modify: `tests/unit/controller/state/test_cover_pool.py`

- [ ] **Step 1: Append failing tests**

```python
def test_downgrade_stale_verified_returns_empty_when_no_stale(conn):
    add_candidate(conn, "fresh.org", added_at="2026-05-19T00:00:00Z")
    attest_verified(conn, "fresh.org", from_vantage="ru-vps-01", at="2026-05-19T01:00:00Z")
    downgraded = downgrade_stale_verified(
        conn, now="2026-05-20T00:00:00Z", reverify_after_days=30,
    )
    assert downgraded == []
    assert [r.domain for r in list_by_state(conn, "candidate_verified")] == ["fresh.org"]


def test_downgrade_stale_verified_returns_stale_only(conn):
    add_candidate(conn, "stale.org", added_at="2026-04-01T00:00:00Z")
    attest_verified(conn, "stale.org", from_vantage="ru-vps-01", at="2026-04-01T01:00:00Z")
    add_candidate(conn, "fresh.org", added_at="2026-05-15T00:00:00Z")
    attest_verified(conn, "fresh.org", from_vantage="ru-vps-01", at="2026-05-15T01:00:00Z")
    downgraded = downgrade_stale_verified(
        conn, now="2026-05-19T00:00:00Z", reverify_after_days=30,
    )
    assert downgraded == ["stale.org"]
    assert {r.domain for r in list_by_state(conn, "candidate_verified")} == {"fresh.org"}
    assert {r.domain for r in list_by_state(conn, "candidate_unverified")} == {"stale.org"}


def test_downgrade_stale_verified_emits_one_audit_per_row(conn):
    add_candidate(conn, "a.org", added_at="2026-04-01T00:00:00Z")
    attest_verified(conn, "a.org", from_vantage="ru-vps-01", at="2026-04-01T01:00:00Z")
    add_candidate(conn, "b.org", added_at="2026-04-01T00:00:00Z")
    attest_verified(conn, "b.org", from_vantage="ru-vps-01", at="2026-04-01T01:00:00Z")
    downgrade_stale_verified(
        conn, now="2026-05-19T00:00:00Z", reverify_after_days=30,
    )
    actions = [e.action for e in recent_events(conn, limit=10)]
    assert actions.count("cover_downgraded_stale") == 2
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/unit/controller/state/test_cover_pool.py -k downgrade -v`
Expected: PASS (3/3).

- [ ] **Step 3: Commit**

```bash
git add tests/unit/controller/state/test_cover_pool.py
git commit -m "test(C): downgrade_stale_verified TTL boundary + audit coverage"
```

---

### Task 6: `list_due_for_rotation` boundary tests

**Files:**
- Modify: `tests/unit/controller/state/test_cover_pool.py`

- [ ] **Step 1: Append failing tests**

```python
def test_list_due_for_rotation_empty_when_no_in_use(conn):
    add_candidate(conn, "fresh.org", added_at=NOW)
    attest_verified(conn, "fresh.org", from_vantage="ru-vps-01", at=NOW)
    due = list_due_for_rotation(conn, now=NOW, rotation_ttl_days=14)
    assert due == []


def test_list_due_for_rotation_returns_overdue_only(conn):
    _seed_live_box(conn, "box-old")
    _seed_live_box(conn, "box-new", sni="new-sni.invalid")
    add_candidate(conn, "old.org", added_at="2026-04-01T00:00:00Z")
    attest_verified(conn, "old.org", from_vantage="ru-vps-01", at="2026-04-01T01:00:00Z")
    assign_to_box(conn, "old.org", box_id="box-old", at="2026-04-01T02:00:00Z")
    add_candidate(conn, "new.org", added_at="2026-05-15T00:00:00Z")
    attest_verified(conn, "new.org", from_vantage="ru-vps-01", at="2026-05-15T01:00:00Z")
    assign_to_box(conn, "new.org", box_id="box-new", at="2026-05-15T02:00:00Z")

    due = list_due_for_rotation(conn, now="2026-05-19T00:00:00Z", rotation_ttl_days=14)
    assert [r.domain for r in due] == ["old.org"]
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/unit/controller/state/test_cover_pool.py -k due_for_rotation -v`
Expected: PASS (2/2).

- [ ] **Step 3: Commit**

```bash
git add tests/unit/controller/state/test_cover_pool.py
git commit -m "test(C): list_due_for_rotation TTL boundary coverage"
```

---

### Task 7: `pool_health` correctness + freeze flag

**Files:**
- Modify: `tests/unit/controller/state/test_cover_pool.py`

- [ ] **Step 1: Append failing tests**

```python
def test_pool_health_counts_and_freeze_flag(conn):
    _seed_live_box(conn, "box-1")
    add_candidate(conn, "a.org", added_at=NOW)
    attest_verified(conn, "a.org", from_vantage="ru-vps-01", at=NOW)
    add_candidate(conn, "b.org", added_at=NOW)            # unverified
    add_candidate(conn, "c.org", added_at=NOW)
    attest_verified(conn, "c.org", from_vantage="ru-vps-01", at=NOW)
    assign_to_box(conn, "c.org", box_id="box-1", at=NOW)  # in_use

    h = pool_health(conn, freeze_threshold=2)
    assert h.candidate_unverified == 1
    assert h.candidate_verified == 1
    assert h.in_use == 1
    assert h.burned == 0
    # only 1 verified → below freeze_threshold of 2 → frozen
    assert h.rotation_frozen is True


def test_pool_health_not_frozen_when_above_threshold(conn):
    add_candidate(conn, "a.org", added_at=NOW)
    attest_verified(conn, "a.org", from_vantage="ru-vps-01", at=NOW)
    add_candidate(conn, "b.org", added_at=NOW)
    attest_verified(conn, "b.org", from_vantage="ru-vps-01", at=NOW)
    h = pool_health(conn, freeze_threshold=2)
    assert h.candidate_verified == 2
    assert h.rotation_frozen is False
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/unit/controller/state/test_cover_pool.py -k pool_health -v`
Expected: PASS (2/2).

- [ ] **Step 3: Commit**

```bash
git add tests/unit/controller/state/test_cover_pool.py
git commit -m "test(C): pool_health counts + freeze_threshold boundary"
```

---

### Task 8: `rotate_and_burn` atomicity + ordering

**Files:**
- Modify: `tests/unit/controller/state/test_cover_pool.py`

- [ ] **Step 1: Append failing tests**

```python
def test_rotate_and_burn_happy_path(conn):
    _seed_live_box(conn, "box-1")
    add_candidate(conn, "rotate.org", added_at=NOW)
    attest_verified(conn, "rotate.org", from_vantage="ru-vps-01", at=NOW)
    assign_to_box(conn, "rotate.org", box_id="box-1", at=NOW)

    rotate_and_burn(
        conn, "rotate.org",
        reason="rotation_ttl",
        last_box_id="box-1",
        at="2026-06-01T00:00:00Z",
        details="ttl elapsed",
    )

    assert list_by_state(conn, "in_use") == []
    assert is_burned(conn, "rotate.org")
    actions = [e.action for e in recent_events(conn, limit=10)]
    # cover_rotated audit row precedes cover_burned (latest first)
    assert "cover_rotated" in actions
    assert "cover_burned" in actions


def test_rotate_and_burn_refuses_non_in_use(conn):
    add_candidate(conn, "newborn.org", added_at=NOW)
    with pytest.raises(ValueError, match="is not in_use"):
        rotate_and_burn(
            conn, "newborn.org",
            reason="manual_rotate",
            last_box_id="none",
            at=NOW,
        )


def test_rotate_and_burn_refuses_missing(conn):
    with pytest.raises(ValueError, match="not in cover_domain_pool"):
        rotate_and_burn(
            conn, "ghost.org",
            reason="manual_rotate",
            last_box_id="none",
            at=NOW,
        )
```

Verify that `burned.mark_burned` writes the `cover_burned` audit row. If it does not yet (spec A predates the audit-emission convention), add one line to `state/burned.py` immediately after the COMMIT statement: `log_event(conn, ts=at, actor="controller", action="cover_burned", target=domain, details_json=json.dumps({"reason": reason, "last_box_id": last_box_id}))`. Inspect the current file before changing:

```bash
sed -n '1,60p' src/mthydra/controller/state/burned.py
```

If `cover_burned` audit emission is missing, append it inside `mark_burned` before the function returns. The trigger ensures the burn-DELETE half is atomic, but the audit log row is its own INSERT — call `log_event` *after* COMMIT so a triggered ABORT cannot orphan the audit row.

- [ ] **Step 2: Run tests**

Run: `pytest tests/unit/controller/state/test_cover_pool.py -k rotate_and_burn -v`
Expected: PASS (3/3).

- [ ] **Step 3: Commit**

```bash
git add tests/unit/controller/state/test_cover_pool.py src/mthydra/controller/state/burned.py
git commit -m "cover_pool(C): rotate_and_burn happy path + refusal paths + cover_burned audit"
```

---

## Phase 3 — Startup invariants (#17–#20)

### Task 9: Extend `check_all` with invariants #17–#20

**Files:**
- Modify: `src/mthydra/controller/state/invariants.py`
- Modify: `tests/unit/controller/state/test_invariants.py`

- [ ] **Step 1: Write failing invariant tests**

Append to `tests/unit/controller/state/test_invariants.py`:

```python
# ---------------------------------------------------------------------------
# Spec C invariant checks (#17–#20)
# ---------------------------------------------------------------------------

def test_check_17_rejects_missing_triggers(tmp_db_path):
    conn = _seeded(tmp_db_path)
    conn.execute("DROP TRIGGER IF EXISTS cover_pool_reject_burned")
    conn.commit()
    with pytest.raises(InvariantViolation, match="check 17"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_18_rejects_in_use_without_entered_at(tmp_db_path):
    conn = _seeded(tmp_db_path)
    conn.execute("PRAGMA foreign_keys=OFF")  # box FK not relevant here
    conn.execute(
        "INSERT INTO cover_domain_pool (domain, state, added_at, "
        "last_verified_at, verified_from_vantage, assigned_box_id) "
        "VALUES ('x.org', 'in_use', ?, ?, 'ru-vps-01', 'box-x')",
        (NOW, NOW),
    )
    conn.commit()
    with pytest.raises(InvariantViolation, match="check 18"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_19_rejects_in_use_without_live_box(tmp_db_path):
    conn = _seeded(tmp_db_path)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        "INSERT INTO cover_domain_pool (domain, state, added_at, "
        "last_verified_at, verified_from_vantage, assigned_box_id, entered_in_use_at) "
        "VALUES ('x.org', 'in_use', ?, ?, 'ru-vps-01', 'missing-box', ?)",
        (NOW, NOW, NOW),
    )
    conn.commit()
    with pytest.raises(InvariantViolation, match="check 19"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_20_rejects_verified_without_vantage(tmp_db_path):
    conn = _seeded(tmp_db_path)
    conn.execute(
        "INSERT INTO cover_domain_pool (domain, state, added_at, last_verified_at) "
        "VALUES ('x.org', 'candidate_verified', ?, ?)",
        (NOW, NOW),
    )
    conn.commit()
    with pytest.raises(InvariantViolation, match="check 20"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/controller/state/test_invariants.py -k 'check_17 or check_18 or check_19 or check_20' -v`
Expected: FAIL — checks 17–20 are not implemented yet.

- [ ] **Step 3: Append the invariants to `check_all` in `src/mthydra/controller/state/invariants.py`**

Add at the end of `check_all` (after the existing spec B checks):

```python
    # --- spec C checks (#17–#20) ---

    # Check 17: structural triggers present (cover_pool_reject_burned + burned_domains_no_delete)
    trigs = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
    }
    for required in ("cover_pool_reject_burned", "burned_domains_no_delete"):
        if required not in trigs:
            raise InvariantViolation(f"check 17: trigger {required} is missing")

    # Check 18: entered_in_use_at IS NOT NULL iff state='in_use'
    row = conn.execute(
        "SELECT domain, state, entered_in_use_at FROM cover_domain_pool WHERE "
        "(state='in_use' AND entered_in_use_at IS NULL) OR "
        "(state!='in_use' AND entered_in_use_at IS NOT NULL) LIMIT 1"
    ).fetchone()
    if row is not None:
        raise InvariantViolation(
            f"check 18: cover_domain_pool row violates entered_in_use_at invariant: "
            f"domain={row[0]} state={row[1]} entered_in_use_at={row[2]!r}"
        )

    # Check 19: every in_use row has a live (non-terminated) box
    row = conn.execute(
        "SELECT cdp.domain, cdp.assigned_box_id, rb.state FROM cover_domain_pool cdp "
        "LEFT JOIN ru_boxes rb ON cdp.assigned_box_id = rb.box_id "
        "WHERE cdp.state='in_use' AND (rb.box_id IS NULL OR rb.state='terminated') LIMIT 1"
    ).fetchone()
    if row is not None:
        raise InvariantViolation(
            f"check 19: cover_domain_pool.domain={row[0]!r} in_use but "
            f"assigned_box_id={row[1]!r} is missing or terminated"
        )

    # Check 20: last_verified_at and verified_from_vantage populated for non-unverified rows
    row = conn.execute(
        "SELECT domain, state FROM cover_domain_pool "
        "WHERE state IN ('candidate_verified', 'in_use') "
        "AND (last_verified_at IS NULL OR verified_from_vantage IS NULL) LIMIT 1"
    ).fetchone()
    if row is not None:
        raise InvariantViolation(
            f"check 20: cover_domain_pool.domain={row[0]!r} state={row[1]!r} "
            "missing last_verified_at or verified_from_vantage"
        )
```

- [ ] **Step 4: Run all invariant tests**

Run: `pytest tests/unit/controller/state/test_invariants.py -v`
Expected: PASS (existing tests + 4 new).

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/state/invariants.py tests/unit/controller/state/test_invariants.py
git commit -m "invariants(C): startup checks 17-20 (triggers, in_use consistency, vantage populated)"
```

---

## Phase 4 — Schedulers

### Task 10: `CoverPoolReverifySweep`

**Files:**
- Create: `src/mthydra/controller/state/cover_pool_scheduler.py`
- Create: `tests/unit/controller/state/test_cover_pool_scheduler.py`

- [ ] **Step 1: Write failing tests**

```python
"""Spec C — cover-pool reverify + rotation sweep schedulers."""
import pytest

from mthydra.controller.state.cover_pool import (
    add_candidate,
    assign_to_box,
    attest_verified,
    list_by_state,
)
from mthydra.controller.state.cover_pool_scheduler import (
    CoverPoolReverifySweep,
    CoverPoolRotationSweep,
)
from mthydra.controller.state.db import connect
from mthydra.controller.state.obligations import list_obligations
from mthydra.controller.state.ru_boxes import insert_box, mark_live
from mthydra.controller.state.schema import apply_schema


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "state.sqlite"
    conn = connect(p)
    apply_schema(conn)
    conn.close()
    return p


def _add_attested(p, domain: str, at: str) -> None:
    conn = connect(p)
    add_candidate(conn, domain, added_at=at)
    attest_verified(conn, domain, from_vantage="ru-vps-01", at=at)
    conn.close()


def test_reverify_sweep_downgrades_stale(db):
    _add_attested(db, "stale.org", at="2026-04-01T00:00:00Z")
    sweep = CoverPoolReverifySweep(
        db_path=db, reverify_after_days=30, sweep_interval_seconds=3600,
        mode="offline",
        clock=lambda: "2026-05-19T00:00:00Z",
    )
    sweep.run_once()
    conn = connect(db)
    rows = list_by_state(conn, "candidate_unverified")
    assert [r.domain for r in rows] == ["stale.org"]
    conn.close()


def test_reverify_sweep_proves_obligation(db):
    _add_attested(db, "fresh.org", at="2026-05-19T00:00:00Z")
    sweep = CoverPoolReverifySweep(
        db_path=db, reverify_after_days=30, sweep_interval_seconds=3600,
        mode="offline",
        clock=lambda: "2026-05-19T01:00:00Z",
    )
    sweep.run_once()
    conn = connect(db)
    obs = {o.obligation_id: o for o in list_obligations(conn)}
    assert "cover_pool_reverify_sweep_ran" in obs
    assert obs["cover_pool_reverify_sweep_ran"].last_proven_at == "2026-05-19T01:00:00Z"
    conn.close()
```

- [ ] **Step 2: Verify tests fail with ImportError**

Run: `pytest tests/unit/controller/state/test_cover_pool_scheduler.py -v`
Expected: ImportError on `CoverPoolReverifySweep`, `CoverPoolRotationSweep`.

- [ ] **Step 3: Create `src/mthydra/controller/state/cover_pool_scheduler.py`**

```python
"""Cover-pool sweep schedulers (spec C §7).

Two APScheduler-driven sweeps:
  * CoverPoolReverifySweep — TTL downgrade of stale candidate_verified rows
  * CoverPoolRotationSweep  — flags due-for-rotation in_use domains

Both follow the same all-synchronous + BackgroundScheduler model as
mthydra.descriptor.scheduler.DescriptorRotator. Offline mode disables
the timer entirely; tests use run_once() with a frozen clock.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from mthydra.controller.state.audit import log_event
from mthydra.controller.state.cover_pool import (
    downgrade_stale_verified,
    list_due_for_rotation,
    pool_health,
)
from mthydra.controller.state.db import connect
from mthydra.controller.state.obligations import set_obligation


def _default_clock() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _add_seconds_iso(iso: str, seconds: float) -> str:
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (t + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


class CoverPoolReverifySweep:
    """Periodic downgrade of stale candidate_verified rows (spec C §7.1)."""

    def __init__(
        self,
        db_path: Path | str,
        reverify_after_days: int,
        sweep_interval_seconds: float,
        mode: str = "production",
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.reverify_after_days = reverify_after_days
        self.sweep_interval_seconds = sweep_interval_seconds
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
            trigger=IntervalTrigger(seconds=self.sweep_interval_seconds),
        )
        self._scheduler.start()

    def disarm(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    def run_once(self) -> list[str]:
        now = self._clock()
        conn = connect(self.db_path)
        try:
            downgraded = downgrade_stale_verified(
                conn, now=now, reverify_after_days=self.reverify_after_days,
            )
            log_event(
                conn, ts=now, actor="reverify_sweep", action="cover_reverify_sweep",
                target=None, details_json=json.dumps({"downgraded": len(downgraded)}),
            )
            next_due = _add_seconds_iso(now, self.sweep_interval_seconds * 2)
            set_obligation(
                conn,
                obligation_id="cover_pool_reverify_sweep_ran",
                last_proven_at=now,
                proven_by="reverify_sweep",
                next_due_at=next_due,
                details=json.dumps({"downgraded": len(downgraded)}),
            )
            return downgraded
        finally:
            conn.close()


class CoverPoolRotationSweep:
    """Periodic detection of due-for-rotation in_use domains (spec C §7.2)."""

    def __init__(
        self,
        db_path: Path | str,
        rotation_ttl_days: int,
        freeze_threshold: int,
        sweep_interval_seconds: float,
        mode: str = "production",
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.rotation_ttl_days = rotation_ttl_days
        self.freeze_threshold = freeze_threshold
        self.sweep_interval_seconds = sweep_interval_seconds
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
            trigger=IntervalTrigger(seconds=self.sweep_interval_seconds),
        )
        self._scheduler.start()

    def disarm(self) -> None:
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    def run_once(self) -> list[str]:
        """Returns the list of domains flagged due-for-rotation (empty if frozen)."""
        now = self._clock()
        conn = connect(self.db_path)
        try:
            h = pool_health(conn, freeze_threshold=self.freeze_threshold)
            if h.rotation_frozen:
                set_obligation(
                    conn,
                    obligation_id="cover_pool_rotation_frozen",
                    last_proven_at=now,
                    proven_by="rotation_sweep",
                    # next_due_at irrelevant (anti-obligation) — set to now for visibility
                    next_due_at=now,
                    details=json.dumps({
                        "candidate_verified": h.candidate_verified,
                        "freeze_threshold": self.freeze_threshold,
                    }),
                )
                self._heartbeat(conn, now, flagged=0, frozen=True)
                return []

            # Pool healthy → ensure the freeze obligation row is cleared (if it exists)
            conn.execute(
                "DELETE FROM obligation_clocks WHERE obligation_id='cover_pool_rotation_frozen'"
            )
            conn.commit()

            due = list_due_for_rotation(
                conn, now=now, rotation_ttl_days=self.rotation_ttl_days,
            )
            flagged = [d.domain for d in due]
            for domain in flagged:
                set_obligation(
                    conn,
                    obligation_id=f"cover_pool_rotation_pending::{domain}",
                    last_proven_at=now,
                    proven_by="rotation_sweep",
                    next_due_at=now,
                    details=json.dumps({"domain": domain}),
                )
            self._heartbeat(conn, now, flagged=len(flagged), frozen=False)
            return flagged
        finally:
            conn.close()

    def _heartbeat(self, conn, now: str, *, flagged: int, frozen: bool) -> None:
        next_due = _add_seconds_iso(now, self.sweep_interval_seconds * 2)
        set_obligation(
            conn,
            obligation_id="cover_pool_rotation_sweep_ran",
            last_proven_at=now,
            proven_by="rotation_sweep",
            next_due_at=next_due,
            details=json.dumps({"flagged": flagged, "frozen": frozen}),
        )
        log_event(
            conn, ts=now, actor="rotation_sweep", action="cover_rotation_sweep",
            target=None, details_json=json.dumps({"flagged": flagged, "frozen": frozen}),
        )
```

- [ ] **Step 4: Run Task 10 tests**

Run: `pytest tests/unit/controller/state/test_cover_pool_scheduler.py::test_reverify_sweep_downgrades_stale tests/unit/controller/state/test_cover_pool_scheduler.py::test_reverify_sweep_proves_obligation -v`
Expected: PASS (2/2).

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/state/cover_pool_scheduler.py tests/unit/controller/state/test_cover_pool_scheduler.py
git commit -m "scheduler(C): CoverPoolReverifySweep + CoverPoolRotationSweep classes"
```

---

### Task 11: `CoverPoolRotationSweep` behaviour tests

**Files:**
- Modify: `tests/unit/controller/state/test_cover_pool_scheduler.py`

- [ ] **Step 1: Append failing tests**

```python
def _seed_box(p, box_id="box-1", sni="sni.invalid"):
    conn = connect(p)
    insert_box(conn, box_id, "aws", "eu-west-1", "10.0.0.1", sni, "img-v1", "2026-04-01T00:00:00Z")
    mark_live(conn, box_id, public_ip="10.0.0.1", at="2026-04-01T00:00:00Z")
    conn.close()


def _assign_old_domain(p, domain, box_id, at_entered):
    conn = connect(p)
    add_candidate(conn, domain, added_at=at_entered)
    attest_verified(conn, domain, from_vantage="ru-vps-01", at=at_entered)
    assign_to_box(conn, domain, box_id=box_id, at=at_entered)
    conn.close()


def test_rotation_sweep_flags_overdue(db):
    _seed_box(db, "box-1")
    _assign_old_domain(db, "old.org", "box-1", "2026-04-01T00:00:00Z")
    # Need ≥ freeze_threshold of verified to avoid the freeze path
    _add_attested(db, "spare-a.org", at="2026-05-19T00:00:00Z")
    _add_attested(db, "spare-b.org", at="2026-05-19T00:00:00Z")
    sweep = CoverPoolRotationSweep(
        db_path=db, rotation_ttl_days=14, freeze_threshold=2,
        sweep_interval_seconds=3600, mode="offline",
        clock=lambda: "2026-05-19T00:00:00Z",
    )
    flagged = sweep.run_once()
    assert flagged == ["old.org"]
    conn = connect(db)
    obs = {o.obligation_id: o for o in list_obligations(conn)}
    assert "cover_pool_rotation_pending::old.org" in obs
    assert "cover_pool_rotation_frozen" not in obs
    assert "cover_pool_rotation_sweep_ran" in obs
    conn.close()


def test_rotation_sweep_freezes_when_pool_low(db):
    _seed_box(db, "box-1")
    _assign_old_domain(db, "old.org", "box-1", "2026-04-01T00:00:00Z")
    # only 0 verified left after assignment → below freeze_threshold of 2
    sweep = CoverPoolRotationSweep(
        db_path=db, rotation_ttl_days=14, freeze_threshold=2,
        sweep_interval_seconds=3600, mode="offline",
        clock=lambda: "2026-05-19T00:00:00Z",
    )
    flagged = sweep.run_once()
    assert flagged == []
    conn = connect(db)
    obs = {o.obligation_id: o for o in list_obligations(conn)}
    assert "cover_pool_rotation_frozen" in obs
    # no rotation_pending rows when frozen
    assert not any(k.startswith("cover_pool_rotation_pending::") for k in obs)
    conn.close()


def test_rotation_sweep_clears_freeze_when_refilled(db):
    _seed_box(db, "box-1")
    _assign_old_domain(db, "old.org", "box-1", "2026-04-01T00:00:00Z")
    sweep = CoverPoolRotationSweep(
        db_path=db, rotation_ttl_days=14, freeze_threshold=2,
        sweep_interval_seconds=3600, mode="offline",
        clock=lambda: "2026-05-19T00:00:00Z",
    )
    sweep.run_once()
    # Refill the pool
    _add_attested(db, "spare-a.org", at="2026-05-19T00:30:00Z")
    _add_attested(db, "spare-b.org", at="2026-05-19T00:30:00Z")
    sweep.run_once()
    conn = connect(db)
    obs = {o.obligation_id for o in list_obligations(conn)}
    assert "cover_pool_rotation_frozen" not in obs
    conn.close()
```

Add the missing imports to the existing test file header:

```python
from mthydra.controller.state.cover_pool import (
    add_candidate, assign_to_box, attest_verified, list_by_state,
)
from mthydra.controller.state.ru_boxes import insert_box, mark_live
```

(These may already be present from Task 10 — keep one consolidated import block.)

- [ ] **Step 2: Run tests**

Run: `pytest tests/unit/controller/state/test_cover_pool_scheduler.py -v`
Expected: PASS (5/5 total).

- [ ] **Step 3: Commit**

```bash
git add tests/unit/controller/state/test_cover_pool_scheduler.py
git commit -m "test(C): rotation sweep flags overdue + freezes low pool + clears on refill"
```

---

## Phase 5 — Config + bootstrap

### Task 12: `CoverPoolConfig` dataclass and TOML loading

**Files:**
- Modify: `src/mthydra/controller/config.py`
- Modify: `packaging/etc/mthydra/controller.toml.example`
- Modify: `tests/unit/controller/test_config.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/controller/test_config.py`:

```python
def test_load_cover_pool_config(tmp_path):
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
        "[cover_pool]\n"
        "rotation_ttl_days=14\n"
        "reverify_after_days=30\n"
        "freeze_threshold=2\n"
        "reverify_sweep_interval='1h'\n"
        "rotation_sweep_interval='1h'\n"
        "replenishment_interval_days=90\n"
    )
    cfg = load_config(cfg_path)
    assert cfg.cover_pool.rotation_ttl_days == 14
    assert cfg.cover_pool.reverify_after_days == 30
    assert cfg.cover_pool.freeze_threshold == 2
    assert cfg.cover_pool.reverify_sweep_interval_seconds == 3600
    assert cfg.cover_pool.rotation_sweep_interval_seconds == 3600
    assert cfg.cover_pool.replenishment_interval_days == 90
```

- [ ] **Step 2: Run test to verify failure**

Run: `pytest tests/unit/controller/test_config.py::test_load_cover_pool_config -v`
Expected: FAIL (no `cover_pool` attribute on `Config`).

- [ ] **Step 3: Modify `src/mthydra/controller/config.py`**

Add the dataclass below `DescriptorConfig`:

```python
@dataclass(frozen=True)
class CoverPoolConfig:
    rotation_ttl_days: int
    reverify_after_days: int
    freeze_threshold: int
    reverify_sweep_interval_seconds: int
    rotation_sweep_interval_seconds: int
    replenishment_interval_days: int
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
```

Inside `load_config()`, parse the new section. Add a small interval-string helper (`"1h"` → seconds) near the existing helpers, and a section parser. If the section is missing, default each field; this preserves backward-compat for older configs:

```python
_INTERVAL_SUFFIXES = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _parse_interval_seconds(name: str, value: object) -> int:
    if isinstance(value, int):
        return _require_positive(name, value)
    if isinstance(value, str) and len(value) >= 2 and value[-1] in _INTERVAL_SUFFIXES:
        try:
            n = int(value[:-1])
        except ValueError as e:
            raise ConfigError(f"{name}: invalid interval {value!r}") from e
        return _require_positive(name, n) * _INTERVAL_SUFFIXES[value[-1]]
    raise ConfigError(f"{name}: must be int or 'Nh'/'Nm'/'Nd'/'Ns' string (got {value!r})")


def _load_cover_pool(data: dict) -> CoverPoolConfig:
    sec = data.get("cover_pool", {})
    return CoverPoolConfig(
        rotation_ttl_days=_require_positive(
            "cover_pool.rotation_ttl_days", sec.get("rotation_ttl_days", 14)
        ),
        reverify_after_days=_require_positive(
            "cover_pool.reverify_after_days", sec.get("reverify_after_days", 30)
        ),
        freeze_threshold=_require_positive(
            "cover_pool.freeze_threshold", sec.get("freeze_threshold", 2)
        ),
        reverify_sweep_interval_seconds=_parse_interval_seconds(
            "cover_pool.reverify_sweep_interval",
            sec.get("reverify_sweep_interval", 3600),
        ),
        rotation_sweep_interval_seconds=_parse_interval_seconds(
            "cover_pool.rotation_sweep_interval",
            sec.get("rotation_sweep_interval", 3600),
        ),
        replenishment_interval_days=_require_positive(
            "cover_pool.replenishment_interval_days",
            sec.get("replenishment_interval_days", 90),
        ),
    )
```

Then wire it into the existing `load_config()` return:

```python
    return Config(
        node=_load_node(data),
        backup=_load_backup(data),
        gap_monitor=_load_gap_monitor(data),
        obligations=_load_obligations(data),
        descriptor=_load_descriptor(data),
        cover_pool=_load_cover_pool(data),
    )
```

(Adjust the call sites if the existing helpers have different names — read `config.py` first to confirm.)

- [ ] **Step 4: Append to `packaging/etc/mthydra/controller.toml.example`**

```toml

[cover_pool]
rotation_ttl_days       = 14
reverify_after_days     = 30
freeze_threshold        = 2
reverify_sweep_interval = "1h"
rotation_sweep_interval = "1h"
replenishment_interval_days = 90
```

- [ ] **Step 5: Run config tests**

Run: `pytest tests/unit/controller/test_config.py -v`
Expected: PASS (all previous + new).

- [ ] **Step 6: Run any test that constructs a `Config` fixture (CLI tests)**

Run: `pytest tests/unit/controller/test_cli.py -q`
Expected: PASS — but if any test builds a `Config` literal without `cover_pool=...`, it will fail with `TypeError`. Update those fixtures by passing a default `CoverPoolConfig(...)` instance.

- [ ] **Step 7: Commit**

```bash
git add src/mthydra/controller/config.py packaging/etc/mthydra/controller.toml.example tests/unit/controller/test_config.py tests/unit/controller/test_cli.py
git commit -m "config(C): CoverPoolConfig dataclass + [cover_pool] TOML section"
```

---

### Task 13: Bootstrap seeds spec-C obligations + shared `age_recipient` fixture

**Files:**
- Modify: `tests/conftest.py`
- Modify: `src/mthydra/controller/cli.py`
- Modify: `tests/unit/controller/test_bootstrap.py`

- [ ] **Step 0: Add shared `age_recipient` fixture to `tests/conftest.py`**

Append to `tests/conftest.py`:

```python
import shutil

import pytest


@pytest.fixture
def age_recipient(tmp_path):
    """Real age X25519 public-key recipient; skips when age-keygen is unavailable."""
    if shutil.which("age-keygen") is None:
        pytest.skip("age-keygen not installed")
    import subprocess
    keyfile = tmp_path / "identity"
    r = subprocess.run(
        ["age-keygen", "-o", str(keyfile)],
        capture_output=True, text=True, check=True,
    )
    return next(
        line.removeprefix("# public key: ").strip()
        for line in r.stderr.splitlines()
        if line.startswith("# public key: ")
    )
```

(Keep the existing `tmp_db_path` fixture as-is.)

- [ ] **Step 1: Write failing test**

Append to `tests/unit/controller/test_bootstrap.py`:

```python
def test_init_seeds_cover_pool_obligations(tmp_path, recipient):
    db = tmp_path / "state.sqlite"
    init_state(
        db_path=db,
        age_recipient=recipient,
        provider_credentials={"b2": "id:secret"},
        obligation_timer_hours={
            "cover_pool_reverify_pass_proven": 30 * 2 * 24,  # 60d
            "cover_pool_replenishment_proven": 90 * 24,       # 90d
        },
        now="2026-05-19T00:00:00Z",
    )
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.obligations import list_obligations
    conn = connect(db)
    ids = {o.obligation_id for o in list_obligations(conn)}
    assert "cover_pool_reverify_pass_proven" in ids
    assert "cover_pool_replenishment_proven" in ids
```

- [ ] **Step 2: Run test (PASS without code changes — `init_state` already accepts arbitrary obligation IDs from `obligation_timer_hours`)**

Run: `pytest tests/unit/controller/test_bootstrap.py::test_init_seeds_cover_pool_obligations -v`
Expected: PASS.

- [ ] **Step 3: Edit `init` subcommand in `src/mthydra/controller/cli.py`**

Find the `obligation_timer_hours={...}` dict inside the `if args.cmd == "init":` block (around the start of `run()`). Append:

```python
                    "cover_pool_reverify_pass_proven": 60 * 24,   # reverify_after_days * 2 = 60 days
                    "cover_pool_replenishment_proven": 90 * 24,   # 90 days
```

- [ ] **Step 4: Add a CLI smoke test that verifies seeding via init**

Add to `tests/unit/controller/test_cli.py` (or wherever `init` smoke tests live):

```python
def test_init_seeds_cover_pool_obligations_via_cli(tmp_path, age_recipient):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    rc = run([
        "init",
        "--db-path", str(db),
        "--age-recipient", age_recipient,
        "--provider-credential", "b2=id:secret",
    ])
    assert rc == 0
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.obligations import list_obligations
    conn = connect(db)
    ids = {o.obligation_id for o in list_obligations(conn)}
    assert "cover_pool_reverify_pass_proven" in ids
    assert "cover_pool_replenishment_proven" in ids
```

(Adapt the `age_recipient` fixture name to whatever already exists in `test_cli.py`.)

- [ ] **Step 5: Run all bootstrap and init-related tests**

Run: `pytest tests/unit/controller/test_bootstrap.py tests/unit/controller/test_cli.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/mthydra/controller/cli.py tests/unit/controller/test_bootstrap.py tests/unit/controller/test_cli.py
git commit -m "bootstrap(C): seed cover_pool_reverify_pass_proven + cover_pool_replenishment_proven"
```

---

## Phase 6 — CLI

### Task 14: `cover-add` subcommand

**Files:**
- Modify: `src/mthydra/controller/cli.py`
- Modify: `tests/unit/controller/test_cli.py`

- [ ] **Step 1: Write failing test**

Append to `tests/unit/controller/test_cli.py`:

```python
def test_cover_add_creates_unverified_row(tmp_path, age_recipient):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run([
        "init", "--db-path", str(db),
        "--age-recipient", age_recipient,
        "--provider-credential", "b2=id:secret",
    ])
    rc = run(["cover-add", "fresh.org", "--db-path", str(db),
              "--notes", "fall-2026 batch"])
    assert rc == 0
    from mthydra.controller.state.cover_pool import list_by_state
    from mthydra.controller.state.db import connect
    conn = connect(db)
    rows = list_by_state(conn, "candidate_unverified")
    assert [r.domain for r in rows] == ["fresh.org"]
    assert rows[0].notes == "fall-2026 batch"


def test_cover_add_proves_replenishment_obligation(tmp_path, age_recipient):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run([
        "init", "--db-path", str(db),
        "--age-recipient", age_recipient,
        "--provider-credential", "b2=id:secret",
    ])
    # Stamp obligation to a known-old timestamp first
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.obligations import list_obligations, set_obligation
    conn = connect(db)
    set_obligation(conn, "cover_pool_replenishment_proven",
                   last_proven_at="2025-01-01T00:00:00Z",
                   proven_by="bootstrap",
                   next_due_at="2025-04-01T00:00:00Z")
    conn.close()
    run(["cover-add", "fresh.org", "--db-path", str(db)])
    conn = connect(db)
    obs = {o.obligation_id: o for o in list_obligations(conn)}
    assert obs["cover_pool_replenishment_proven"].last_proven_at > "2025-01-01T00:00:00Z"
    conn.close()


def test_cover_add_refuses_burned(tmp_path, age_recipient, capsys):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run([
        "init", "--db-path", str(db),
        "--age-recipient", age_recipient,
        "--provider-credential", "b2=id:secret",
    ])
    # Seed a burned domain via direct SQL (simulates a prior burn)
    from mthydra.controller.state.db import connect
    conn = connect(db)
    conn.execute(
        "INSERT INTO burned_domains (domain, burned_at, reason) "
        "VALUES ('burned.org', '2026-05-19T00:00:00Z', 'manual')"
    )
    conn.commit()
    conn.close()

    rc = run(["cover-add", "burned.org", "--db-path", str(db)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "burned_domains" in err
```

- [ ] **Step 2: Run tests — verify they fail (cover-add not registered)**

Run: `pytest tests/unit/controller/test_cli.py -k cover_add -v`
Expected: `SystemExit: 2` from argparse (unknown subcommand).

- [ ] **Step 3: Add the subparser in `build_parser()` in `src/mthydra/controller/cli.py`**

```python
    # ----- spec C subcommands -----
    ca = sub.add_parser("cover-add", help="add a candidate cover domain")
    ca.add_argument("domain")
    ca.add_argument("--db-path", default=DEFAULT_DB)
    ca.add_argument("--notes", default=None)
```

- [ ] **Step 4: Add the dispatch + handler in `run()`**

In `run()`, before the final `return 1`:

```python
    if args.cmd == "cover-add":
        return _cmd_cover_add(args)
```

Add `_cmd_cover_add` at the bottom of `cli.py`:

```python
def _cmd_cover_add(args) -> int:
    import sqlite3

    from mthydra.controller.state.cover_pool import add_candidate
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.obligations import prove

    conn = connect(args.db_path)
    try:
        now = _now()
        try:
            add_candidate(conn, args.domain, added_at=now, notes=args.notes)
        except sqlite3.IntegrityError as e:
            print(f"cover-add: {e}", file=sys.stderr)
            return 2
        # Spec C §7.3 obligation: operator topped up the pool.
        next_due = _add_hours_iso(now, 90 * 24)  # default replenishment_interval_days
        try:
            prove(conn, "cover_pool_replenishment_proven",
                  proven_by="operator", at=now,
                  next_due_at=next_due, details=args.domain)
        except KeyError:
            pass  # obligation may not be seeded in older DBs; non-fatal
        print(f"cover-add: {args.domain} added (candidate_unverified)")
        return 0
    finally:
        conn.close()


def _add_hours_iso(iso: str, hours: int) -> str:
    from datetime import datetime, timedelta
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (t + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
```

(If `_add_hours_iso` already exists at the bottom of `cli.py`, reuse it instead of duplicating.)

- [ ] **Step 5: Run tests**

Run: `pytest tests/unit/controller/test_cli.py -k cover_add -v`
Expected: PASS (2/2).

- [ ] **Step 6: Commit**

```bash
git add src/mthydra/controller/cli.py tests/unit/controller/test_cli.py
git commit -m "cli(C): cover-add subcommand + trigger-aware exit code"
```

---

### Task 15: `cover-attest-verified` subcommand

**Files:**
- Modify: `src/mthydra/controller/cli.py`
- Modify: `tests/unit/controller/test_cli.py`

- [ ] **Step 1: Write failing test**

```python
def test_cover_attest_verified_transitions_state(tmp_path, age_recipient):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run([
        "init", "--db-path", str(db),
        "--age-recipient", age_recipient,
        "--provider-credential", "b2=id:secret",
    ])
    run(["cover-add", "fresh.org", "--db-path", str(db)])
    rc = run([
        "cover-attest-verified", "fresh.org",
        "--vantage", "ru-vps-01",
        "--evidence", "curl + cert match",
        "--db-path", str(db),
    ])
    assert rc == 0
    from mthydra.controller.state.cover_pool import list_by_state
    from mthydra.controller.state.db import connect
    conn = connect(db)
    rows = list_by_state(conn, "candidate_verified")
    assert rows[0].verified_from_vantage == "ru-vps-01"


def test_cover_attest_verified_proves_reverify_pass_obligation(tmp_path, age_recipient):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run([
        "init", "--db-path", str(db),
        "--age-recipient", age_recipient,
        "--provider-credential", "b2=id:secret",
    ])
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.obligations import list_obligations, set_obligation
    conn = connect(db)
    set_obligation(conn, "cover_pool_reverify_pass_proven",
                   last_proven_at="2025-01-01T00:00:00Z",
                   proven_by="bootstrap",
                   next_due_at="2025-03-01T00:00:00Z")
    conn.close()
    run(["cover-add", "fresh.org", "--db-path", str(db)])
    run([
        "cover-attest-verified", "fresh.org",
        "--vantage", "ru-vps-01",
        "--db-path", str(db),
    ])
    conn = connect(db)
    obs = {o.obligation_id: o for o in list_obligations(conn)}
    assert obs["cover_pool_reverify_pass_proven"].last_proven_at > "2025-01-01T00:00:00Z"
    conn.close()


def test_cover_attest_verified_rejects_missing_domain(tmp_path, age_recipient, capsys):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run([
        "init", "--db-path", str(db),
        "--age-recipient", age_recipient,
        "--provider-credential", "b2=id:secret",
    ])
    rc = run([
        "cover-attest-verified", "ghost.org",
        "--vantage", "ru-vps-01",
        "--db-path", str(db),
    ])
    assert rc == 2
```

- [ ] **Step 2: Add subparser + dispatch + handler**

Subparser:

```python
    cav = sub.add_parser("cover-attest-verified",
                          help="operator-attested Russia-vantage verification (spec C C-D1)")
    cav.add_argument("domain")
    cav.add_argument("--vantage", required=True,
                      help="vantage label, e.g. 'ru-vps-01' (free-text until spec I)")
    cav.add_argument("--evidence", default=None,
                      help="captured verbatim in audit_log.details_json")
    cav.add_argument("--db-path", default=DEFAULT_DB)
```

Dispatch:

```python
    if args.cmd == "cover-attest-verified":
        return _cmd_cover_attest_verified(args)
```

Handler:

```python
def _cmd_cover_attest_verified(args) -> int:
    from mthydra.controller.state.cover_pool import attest_verified
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.obligations import prove

    conn = connect(args.db_path)
    try:
        now = _now()
        try:
            attest_verified(
                conn, args.domain,
                from_vantage=args.vantage, at=now, evidence=args.evidence,
            )
        except ValueError as e:
            print(f"cover-attest-verified: {e}", file=sys.stderr)
            return 2
        # Spec C §7.3 obligation: operator (or spec I) did re-verify.
        next_due = _add_hours_iso(now, 60 * 24)  # reverify_after_days * 2 = 60d
        try:
            prove(conn, "cover_pool_reverify_pass_proven",
                  proven_by="operator", at=now,
                  next_due_at=next_due, details=args.domain)
        except KeyError:
            pass  # obligation may not be seeded in older DBs; non-fatal
        print(f"cover-attest-verified: {args.domain} -> candidate_verified (vantage={args.vantage})")
        return 0
    finally:
        conn.close()
```

- [ ] **Step 3: Run tests**

Run: `pytest tests/unit/controller/test_cli.py -k cover_attest -v`
Expected: PASS (2/2).

- [ ] **Step 4: Commit**

```bash
git add src/mthydra/controller/cli.py tests/unit/controller/test_cli.py
git commit -m "cli(C): cover-attest-verified subcommand"
```

---

### Task 16: `cover-list` subcommand (with `--json`)

**Files:**
- Modify: `src/mthydra/controller/cli.py`
- Modify: `tests/unit/controller/test_cli.py`

- [ ] **Step 1: Write failing test**

```python
def test_cover_list_default_shows_all_states(tmp_path, age_recipient, capsys):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run([
        "init", "--db-path", str(db),
        "--age-recipient", age_recipient,
        "--provider-credential", "b2=id:secret",
    ])
    run(["cover-add", "a.org", "--db-path", str(db)])
    run(["cover-add", "b.org", "--db-path", str(db)])
    rc = run(["cover-list", "--db-path", str(db)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "a.org" in out
    assert "b.org" in out


def test_cover_list_json_output_schema(tmp_path, age_recipient, capsys):
    import json
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run([
        "init", "--db-path", str(db),
        "--age-recipient", age_recipient,
        "--provider-credential", "b2=id:secret",
    ])
    run(["cover-add", "a.org", "--db-path", str(db)])
    capsys.readouterr()  # drain
    rc = run(["cover-list", "--db-path", str(db), "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert isinstance(data, list)
    assert any(row["domain"] == "a.org" for row in data)
    assert all(set(row.keys()) >= {"domain", "state", "added_at"} for row in data)
```

- [ ] **Step 2: Add subparser + dispatch + handler**

```python
    cl = sub.add_parser("cover-list", help="list cover_domain_pool rows")
    cl.add_argument("--db-path", default=DEFAULT_DB)
    cl.add_argument("--state",
                     choices=["candidate_unverified", "candidate_verified", "in_use"],
                     default=None)
    cl.add_argument("--json", action="store_true")
```

```python
    if args.cmd == "cover-list":
        return _cmd_cover_list(args)
```

```python
def _cmd_cover_list(args) -> int:
    import json
    from dataclasses import asdict

    from mthydra.controller.state.cover_pool import list_by_state
    from mthydra.controller.state.db import connect

    conn = connect(args.db_path)
    try:
        states = (
            [args.state] if args.state else
            ["candidate_unverified", "candidate_verified", "in_use"]
        )
        rows: list = []
        for s in states:
            rows.extend(list_by_state(conn, s))
        if args.json:
            print(json.dumps([asdict(r) for r in rows], indent=2))
        else:
            print(f"{'state':24} {'domain':40} added_at")
            for r in rows:
                print(f"{r.state:24} {r.domain:40} {r.added_at}")
        return 0
    finally:
        conn.close()
```

- [ ] **Step 3: Run tests**

Run: `pytest tests/unit/controller/test_cli.py -k cover_list -v`
Expected: PASS (2/2).

- [ ] **Step 4: Commit**

```bash
git add src/mthydra/controller/cli.py tests/unit/controller/test_cli.py
git commit -m "cli(C): cover-list subcommand with --json"
```

---

### Task 17: `cover-rotate` subcommand

**Files:**
- Modify: `src/mthydra/controller/cli.py`
- Modify: `tests/unit/controller/test_cli.py`

- [ ] **Step 1: Write failing test**

```python
def test_cover_rotate_burns_in_use_domain(tmp_path, age_recipient):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run([
        "init", "--db-path", str(db),
        "--age-recipient", age_recipient,
        "--provider-credential", "b2=id:secret",
    ])
    # Seed an in_use domain via direct repo calls
    from mthydra.controller.state.cover_pool import (
        add_candidate, assign_to_box, attest_verified,
    )
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_boxes import insert_box, mark_live
    conn = connect(db)
    insert_box(conn, "box-1", "aws", "eu-west-1", "10.0.0.1", "sni.invalid",
               "img-v1", "2026-05-19T00:00:00Z")
    mark_live(conn, "box-1", public_ip="10.0.0.1", at="2026-05-19T00:00:00Z")
    add_candidate(conn, "rot.org", added_at="2026-05-19T00:00:00Z")
    attest_verified(conn, "rot.org", from_vantage="ru-vps-01", at="2026-05-19T01:00:00Z")
    assign_to_box(conn, "rot.org", box_id="box-1", at="2026-05-19T02:00:00Z")
    conn.close()

    rc = run([
        "cover-rotate", "rot.org",
        "--reason", "manual_rotate",
        "--db-path", str(db),
    ])
    assert rc == 0
    from mthydra.controller.state.burned import is_burned
    conn = connect(db)
    assert is_burned(conn, "rot.org")


def test_cover_rotate_refuses_non_in_use(tmp_path, age_recipient, capsys):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run([
        "init", "--db-path", str(db),
        "--age-recipient", age_recipient,
        "--provider-credential", "b2=id:secret",
    ])
    run(["cover-add", "newborn.org", "--db-path", str(db)])
    rc = run(["cover-rotate", "newborn.org", "--db-path", str(db)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "is not in_use" in err
```

- [ ] **Step 2: Add subparser + dispatch + handler**

```python
    cr = sub.add_parser("cover-rotate",
                         help="retire an in_use cover domain (in_use -> burned)")
    cr.add_argument("domain")
    cr.add_argument("--reason", default="manual_rotate")
    cr.add_argument("--db-path", default=DEFAULT_DB)
```

```python
    if args.cmd == "cover-rotate":
        return _cmd_cover_rotate(args)
```

```python
def _cmd_cover_rotate(args) -> int:
    from mthydra.controller.state.cover_pool import rotate_and_burn
    from mthydra.controller.state.db import connect

    conn = connect(args.db_path)
    try:
        # Look up the assigned box_id (need it for the burned_domains row)
        row = conn.execute(
            "SELECT state, assigned_box_id FROM cover_domain_pool WHERE domain=?",
            (args.domain,),
        ).fetchone()
        if row is None:
            print(f"cover-rotate: {args.domain} not in cover_domain_pool", file=sys.stderr)
            return 2
        try:
            rotate_and_burn(
                conn, args.domain,
                reason=args.reason,
                last_box_id=row[1] or "",
                at=_now(),
            )
        except ValueError as e:
            print(f"cover-rotate: {e}", file=sys.stderr)
            return 2
        print(f"cover-rotate: {args.domain} -> burned (reason={args.reason})")
        return 0
    finally:
        conn.close()
```

- [ ] **Step 3: Run tests**

Run: `pytest tests/unit/controller/test_cli.py -k cover_rotate -v`
Expected: PASS (2/2).

- [ ] **Step 4: Commit**

```bash
git add src/mthydra/controller/cli.py tests/unit/controller/test_cli.py
git commit -m "cli(C): cover-rotate subcommand"
```

---

### Task 18: `cover-due` subcommand

**Files:**
- Modify: `src/mthydra/controller/cli.py`
- Modify: `tests/unit/controller/test_cli.py`

- [ ] **Step 1: Write failing test**

```python
def test_cover_due_lists_overdue_and_stale(tmp_path, age_recipient, capsys, monkeypatch):
    """cover-due output contains overdue-rotation + stale-verified sections."""
    import json
    from datetime import datetime, timezone

    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run([
        "init", "--db-path", str(db),
        "--age-recipient", age_recipient,
        "--provider-credential", "b2=id:secret",
    ])

    # Seed an in_use domain that's older than the rotation TTL.
    from mthydra.controller.state.cover_pool import (
        add_candidate, assign_to_box, attest_verified,
    )
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_boxes import insert_box, mark_live
    conn = connect(db)
    old = "2026-04-01T00:00:00Z"
    insert_box(conn, "box-1", "aws", "eu-west-1", "10.0.0.1", "sni.invalid", "img-v1", old)
    mark_live(conn, "box-1", public_ip="10.0.0.1", at=old)
    add_candidate(conn, "old.org", added_at=old)
    attest_verified(conn, "old.org", from_vantage="ru-vps-01", at=old)
    assign_to_box(conn, "old.org", box_id="box-1", at=old)
    conn.close()

    # Use a config that defines the TTLs and freeze_threshold; write a minimal one
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)  # see helper below

    capsys.readouterr()  # drain
    rc = run([
        "cover-due", "--db-path", str(db),
        "--config", str(cfg_path),
        "--json",
    ])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert "due_for_rotation" in out
    assert any(r["domain"] == "old.org" for r in out["due_for_rotation"])
    assert "pool_health" in out
```

At the top of `test_cli.py` add the helper (or to a shared `tests/unit/_cli_helpers.py` if one exists):

```python
_MIN_TOML = """\
[node]
role = "active"
hostname = "h"
[backup]
floor_interval_hours = 24
on_change_debounce_seconds = 30
endpoint = "https://example"
bucket = "b"
access_key_id = "k"
[backup.retention]
keep_daily = 30
keep_monthly = 12
object_lock_days = 365
[gap_monitor]
poll_interval_minutes = 30
alarm_threshold_hours = 48
recipient_email = "op@example.org"
[descriptor]
rotation_interval_hours = 1
validity_window_hours = 24
[obligations]
[obligations.timers_hours]
[cover_pool]
rotation_ttl_days = 14
reverify_after_days = 30
freeze_threshold = 2
reverify_sweep_interval = "1h"
rotation_sweep_interval = "1h"
replenishment_interval_days = 90
"""
```

- [ ] **Step 2: Add subparser + dispatch + handler**

```python
    cd = sub.add_parser("cover-due",
                         help="show due-for-rotation + stale-verified + pool health")
    cd.add_argument("--db-path", default=DEFAULT_DB)
    cd.add_argument("--config", default="/etc/mthydra/controller.toml")
    cd.add_argument("--json", action="store_true")
```

```python
    if args.cmd == "cover-due":
        return _cmd_cover_due(args)
```

```python
def _cmd_cover_due(args) -> int:
    import json
    from dataclasses import asdict

    from mthydra.controller.config import ConfigError, load_config
    from mthydra.controller.state.cover_pool import (
        list_due_for_rotation, pool_health,
    )
    from mthydra.controller.state.db import connect

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"cover-due: config error: {e}", file=sys.stderr)
        return 2

    conn = connect(args.db_path)
    try:
        due = list_due_for_rotation(
            conn, now=_now(),
            rotation_ttl_days=cfg.cover_pool.rotation_ttl_days,
        )
        # Stale rows live in candidate_verified that are *about to* be downgraded —
        # surface them now even before the sweep runs.
        from mthydra.controller.state.cover_pool import _iso_minus_days
        cutoff = _iso_minus_days(_now(), cfg.cover_pool.reverify_after_days)
        stale_rows = conn.execute(
            "SELECT domain, last_verified_at FROM cover_domain_pool "
            "WHERE state='candidate_verified' AND last_verified_at < ? "
            "ORDER BY last_verified_at",
            (cutoff,),
        ).fetchall()
        stale = [{"domain": d, "last_verified_at": v} for d, v in stale_rows]
        health = pool_health(conn, freeze_threshold=cfg.cover_pool.freeze_threshold)

        payload = {
            "due_for_rotation": [asdict(r) for r in due],
            "stale_verified": stale,
            "pool_health": asdict(health),
        }
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"pool: unverified={health.candidate_unverified} "
                  f"verified={health.candidate_verified} "
                  f"in_use={health.in_use} burned={health.burned}")
            print(f"rotation_frozen: {health.rotation_frozen}")
            if due:
                print("due for rotation:")
                for r in due:
                    print(f"  {r.domain}  entered_in_use_at={r.entered_in_use_at}")
            if stale:
                print("stale candidate_verified (will downgrade on next sweep):")
                for r in stale:
                    print(f"  {r['domain']}  last_verified_at={r['last_verified_at']}")
        return 0
    finally:
        conn.close()
```

- [ ] **Step 3: Run tests**

Run: `pytest tests/unit/controller/test_cli.py -k cover_due -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/mthydra/controller/cli.py tests/unit/controller/test_cli.py
git commit -m "cli(C): cover-due subcommand — overdue, stale, pool health"
```

---

### Task 19: `cover-pool-stats` subcommand

**Files:**
- Modify: `src/mthydra/controller/cli.py`
- Modify: `tests/unit/controller/test_cli.py`

- [ ] **Step 1: Write failing test**

```python
def test_cover_pool_stats_json(tmp_path, age_recipient, capsys):
    import json
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    run([
        "init", "--db-path", str(db),
        "--age-recipient", age_recipient,
        "--provider-credential", "b2=id:secret",
    ])
    run(["cover-add", "a.org", "--db-path", str(db)])
    capsys.readouterr()
    rc = run([
        "cover-pool-stats", "--db-path", str(db),
        "--config", str(cfg_path),
        "--json",
    ])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["candidate_unverified"] == 1
    assert payload["candidate_verified"] == 0
    assert payload["in_use"] == 0
    assert "rotation_frozen" in payload
```

- [ ] **Step 2: Add subparser + dispatch + handler**

```python
    cps = sub.add_parser("cover-pool-stats",
                          help="pool counts + rotation_frozen + oldest ages")
    cps.add_argument("--db-path", default=DEFAULT_DB)
    cps.add_argument("--config", default="/etc/mthydra/controller.toml")
    cps.add_argument("--json", action="store_true")
```

```python
    if args.cmd == "cover-pool-stats":
        return _cmd_cover_pool_stats(args)
```

```python
def _cmd_cover_pool_stats(args) -> int:
    import json
    from dataclasses import asdict

    from mthydra.controller.config import ConfigError, load_config
    from mthydra.controller.state.cover_pool import pool_health
    from mthydra.controller.state.db import connect

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"cover-pool-stats: config error: {e}", file=sys.stderr)
        return 2

    conn = connect(args.db_path)
    try:
        h = pool_health(conn, freeze_threshold=cfg.cover_pool.freeze_threshold)
        if args.json:
            print(json.dumps(asdict(h), indent=2))
        else:
            print(f"candidate_unverified : {h.candidate_unverified}")
            print(f"candidate_verified   : {h.candidate_verified}")
            print(f"in_use               : {h.in_use}")
            print(f"burned               : {h.burned}")
            print(f"rotation_frozen      : {h.rotation_frozen}")
            print(f"oldest_in_use_at     : {h.oldest_in_use_at}")
            print(f"oldest_unverified_at : {h.oldest_unverified_at}")
            print(f"last_attest_at       : {h.last_attest_at}")
        return 0
    finally:
        conn.close()
```

- [ ] **Step 3: Run tests**

Run: `pytest tests/unit/controller/test_cli.py -k cover_pool_stats -v`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add src/mthydra/controller/cli.py tests/unit/controller/test_cli.py
git commit -m "cli(C): cover-pool-stats subcommand"
```

---

## Phase 7 — Serve integration

### Task 20: Wire schedulers into `serve`

**Files:**
- Modify: `src/mthydra/controller/cli.py`
- Modify: `tests/unit/controller/test_cli.py`

- [ ] **Step 1: Locate the existing `_cmd_serve` and inspect how `DescriptorRotator` is armed**

Run: `grep -n -A 30 'def _cmd_serve' src/mthydra/controller/cli.py | head -60`

- [ ] **Step 2: Modify `_cmd_serve` to also arm the two cover-pool sweeps**

Inside `_cmd_serve`, after the `DescriptorRotator.arm()` call (or alongside it), add:

```python
    from mthydra.controller.state.cover_pool_scheduler import (
        CoverPoolReverifySweep,
        CoverPoolRotationSweep,
    )

    reverify_sweep = CoverPoolReverifySweep(
        db_path=args.db_path,
        reverify_after_days=cfg.cover_pool.reverify_after_days,
        sweep_interval_seconds=cfg.cover_pool.reverify_sweep_interval_seconds,
        mode=mode,
    )
    rotation_sweep = CoverPoolRotationSweep(
        db_path=args.db_path,
        rotation_ttl_days=cfg.cover_pool.rotation_ttl_days,
        freeze_threshold=cfg.cover_pool.freeze_threshold,
        sweep_interval_seconds=cfg.cover_pool.rotation_sweep_interval_seconds,
        mode=mode,
    )
    reverify_sweep.arm()
    rotation_sweep.arm()
```

In the shutdown path of `_cmd_serve` (matching the existing `descriptor_rotator.disarm()`), add:

```python
    reverify_sweep.disarm()
    rotation_sweep.disarm()
```

- [ ] **Step 3: Write a serve-startup smoke test**

```python
def test_serve_arms_cover_pool_sweeps_in_offline_mode(tmp_path, age_recipient, monkeypatch):
    """Smoke: serve with --mode offline arms the sweeps as no-ops and returns 0 quickly."""
    import signal
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    run([
        "init", "--db-path", str(db),
        "--age-recipient", age_recipient,
        "--provider-credential", "b2=id:secret",
    ])
    # Simulate SIGTERM immediately so the daemon loop exits.
    def _send_sigterm(*_):
        signal.raise_signal(signal.SIGTERM)
    monkeypatch.setattr("time.sleep", lambda _x: _send_sigterm())

    rc = run([
        "--mode", "offline",
        "--bucket-override", "off-bucket",
        "serve",
        "--db-path", str(db),
        "--config", str(cfg_path),
    ])
    assert rc == 0
```

(This test exercises arm()/disarm() in offline mode where the actual timer is a no-op; the existing `test_cli.py` already has a similar pattern for `DescriptorRotator`. Adapt the monkeypatch to whatever idle-sleep idiom the serve loop currently uses.)

- [ ] **Step 4: Run tests**

Run: `pytest tests/unit/controller/test_cli.py -k 'serve' -v`
Expected: PASS — existing serve tests + the new one.

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/cli.py tests/unit/controller/test_cli.py
git commit -m "serve(C): arm CoverPoolReverifySweep + CoverPoolRotationSweep alongside DescriptorRotator"
```

---

## Phase 8 — Integration + property tests

### Task 21: End-to-end lifecycle + backup/restore round-trip

**Files:**
- Create: `tests/integration/test_cover_pool_lifecycle.py`

- [ ] **Step 1: Write the integration test**

```python
"""Spec C end-to-end lifecycle: add → attest → assign → rotate-due → burn.

Verifies that the burned row + audit log survive a backup + restore cycle.
"""
import shutil

import pytest

from mthydra.controller.backup.age_crypt import encrypt_to_recipient
from mthydra.controller.restore.decrypt import decrypt_blob
from mthydra.controller.state.audit import recent_events
from mthydra.controller.state.burned import is_burned
from mthydra.controller.state.cover_pool import (
    add_candidate, assign_to_box, attest_verified, list_by_state,
    list_due_for_rotation, rotate_and_burn,
)
from mthydra.controller.state.db import connect
from mthydra.controller.state.ru_boxes import insert_box, mark_live
from mthydra.controller.state.schema import apply_schema


@pytest.fixture
def age_keypair(tmp_path):
    if shutil.which("age-keygen") is None:
        pytest.skip("age-keygen not installed")
    import subprocess
    keyfile = tmp_path / "identity"
    r = subprocess.run(
        ["age-keygen", "-o", str(keyfile)],
        capture_output=True, text=True, check=True,
    )
    pub = next(
        line.removeprefix("# public key: ").strip()
        for line in r.stderr.splitlines()
        if line.startswith("# public key: ")
    )
    return keyfile, pub


def test_full_lifecycle_survives_backup_restore(tmp_path, age_keypair):
    identity, recipient = age_keypair
    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    insert_box(conn, "box-1", "aws", "eu-west-1", "10.0.0.1", "sni.invalid",
               "img-v1", "2026-04-01T00:00:00Z")
    mark_live(conn, "box-1", public_ip="10.0.0.1", at="2026-04-01T00:00:00Z")
    add_candidate(conn, "live.org", added_at="2026-04-01T00:00:00Z")
    attest_verified(conn, "live.org", from_vantage="ru-vps-01",
                    at="2026-04-01T01:00:00Z")
    assign_to_box(conn, "live.org", box_id="box-1", at="2026-04-01T02:00:00Z")

    # Past rotation TTL of 14d
    due = list_due_for_rotation(conn, now="2026-05-19T00:00:00Z", rotation_ttl_days=14)
    assert [d.domain for d in due] == ["live.org"]

    rotate_and_burn(conn, "live.org", reason="rotation_ttl",
                    last_box_id="box-1", at="2026-05-19T00:00:00Z",
                    details="14d TTL elapsed")

    assert is_burned(conn, "live.org")
    assert list_by_state(conn, "in_use") == []
    conn.close()

    # Encrypt + decrypt the DB blob (round-trip)
    enc = tmp_path / "state.sqlite.age"
    encrypt_to_recipient(src=db, dst=enc, recipient=recipient)
    restored = tmp_path / "restored.sqlite"
    decrypt_blob(str(enc), identity_path=str(identity), out=restored)

    # Confirm the burned row + audit_log survive
    conn = connect(restored)
    assert is_burned(conn, "live.org")
    actions = {e.action for e in recent_events(conn, limit=50)}
    assert {"cover_added", "cover_attest_verified", "cover_assigned",
            "cover_rotated", "cover_burned"}.issubset(actions)
    conn.close()
```

- [ ] **Step 2: Run the integration test**

Run: `pytest tests/integration/test_cover_pool_lifecycle.py -v`
Expected: PASS (skips if `age-keygen` is not installed).

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_cover_pool_lifecycle.py
git commit -m "test(C): end-to-end lifecycle + backup/restore round-trip"
```

---

### Task 22: Hypothesis property test for state-machine invariants

**Files:**
- Create: `tests/property/__init__.py` (if not already)
- Create: `tests/property/test_cover_pool_invariants.py`

- [ ] **Step 1: Write the property test**

```python
"""Spec C — random-sequence invariants for the cover-pool state machine.

After every operation in any random sequence of (add, attest, downgrade,
assign, rotate), the structural invariants of T5 must hold:
  * cover_domain_pool ∩ burned_domains = ∅
  * burned_domains row count is monotonically non-decreasing
  * No domain transitions to in_use twice without an intervening burn
"""
from __future__ import annotations

import sqlite3

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mthydra.controller.state.cover_pool import (
    add_candidate, assign_to_box, attest_verified,
    downgrade_stale_verified, rotate_and_burn,
)
from mthydra.controller.state.db import connect
from mthydra.controller.state.ru_boxes import insert_box, mark_live
from mthydra.controller.state.schema import apply_schema

_DOMAINS = ["a.org", "b.org", "c.org", "d.org"]


def _setup(tmp_db_path):
    conn = connect(tmp_db_path)
    apply_schema(conn)
    insert_box(conn, "box-1", "aws", "eu-west-1", "10.0.0.1", "sni.invalid",
               "img-v1", "2026-04-01T00:00:00Z")
    mark_live(conn, "box-1", public_ip="10.0.0.1", at="2026-04-01T00:00:00Z")
    return conn


def _invariants_hold(conn) -> bool:
    overlap = conn.execute(
        "SELECT COUNT(*) FROM cover_domain_pool WHERE domain IN "
        "(SELECT domain FROM burned_domains)"
    ).fetchone()[0]
    return overlap == 0


@settings(max_examples=80, deadline=None)
@given(
    ops=st.lists(
        st.tuples(
            st.sampled_from(["add", "attest", "downgrade", "assign", "rotate"]),
            st.sampled_from(_DOMAINS),
        ),
        min_size=1, max_size=40,
    ),
)
def test_random_operations_preserve_invariants(tmp_db_path, ops):
    conn = _setup(tmp_db_path)
    prev_burned = 0
    in_use_history: dict[str, int] = {}

    for op, dom in ops:
        try:
            if op == "add":
                add_candidate(conn, dom, added_at="2026-05-19T00:00:00Z")
            elif op == "attest":
                attest_verified(conn, dom, from_vantage="ru-vps-01", at="2026-05-19T00:00:00Z")
            elif op == "downgrade":
                downgrade_stale_verified(
                    conn, now="2099-01-01T00:00:00Z", reverify_after_days=30,
                )
            elif op == "assign":
                assign_to_box(conn, dom, box_id="box-1", at="2026-05-19T00:00:00Z")
                in_use_history[dom] = in_use_history.get(dom, 0) + 1
            elif op == "rotate":
                rotate_and_burn(
                    conn, dom, reason="manual_rotate", last_box_id="box-1",
                    at="2026-05-19T00:00:00Z",
                )
        except (ValueError, sqlite3.IntegrityError):
            # Illegal transitions are expected from random sequences.
            pass

        # Invariant 1: no overlap
        assert _invariants_hold(conn), f"overlap after op={op} dom={dom}"

        # Invariant 2: burned count is non-decreasing
        burned = conn.execute("SELECT COUNT(*) FROM burned_domains").fetchone()[0]
        assert burned >= prev_burned
        prev_burned = burned

    # Invariant 3: no domain entered in_use more than (burns_for_that_domain + 1) times
    for d, n in in_use_history.items():
        burns = conn.execute(
            "SELECT COUNT(*) FROM burned_domains WHERE domain=?", (d,)
        ).fetchone()[0]
        assert n <= burns + 1, f"{d} entered in_use {n} times with only {burns} burns"
```

- [ ] **Step 2: Make sure `tests/property/__init__.py` exists (empty file)**

```bash
test -f tests/property/__init__.py || touch tests/property/__init__.py
```

- [ ] **Step 3: Run the property test**

Run: `pytest tests/property/test_cover_pool_invariants.py -v`
Expected: PASS (Hypothesis runs 80 random sequences).

- [ ] **Step 4: Commit**

```bash
git add tests/property/test_cover_pool_invariants.py tests/property/__init__.py
git commit -m "test(C): property test — random operations preserve burned-set invariants"
```

---

## Phase 9 — Final verification

### Task 23: Full test sweep + coverage check

- [ ] **Step 1: Run the full test suite**

Run: `make test` (or `pytest -q` if no Makefile target exists).
Expected: all tests pass; no skipped tests beyond `age-keygen` absence.

- [ ] **Step 2: Run coverage check on spec-C modules**

Run:

```bash
pytest --cov=mthydra.controller.state.cover_pool \
       --cov=mthydra.controller.state.cover_pool_scheduler \
       --cov-report=term-missing tests/
```

Expected: ≥ 90% line coverage on both modules.

- [ ] **Step 3: Smoke-test the CLI end-to-end**

```bash
TMP=$(mktemp -d)
age-keygen -o "$TMP/id"
PUB=$(grep '# public key:' "$TMP/id" | awk '{print $4}')
.venv/bin/mthydra-controller init \
  --db-path "$TMP/state.sqlite" \
  --age-recipient "$PUB" \
  --provider-credential "b2=id:secret"
.venv/bin/mthydra-controller cover-add example.org --db-path "$TMP/state.sqlite"
.venv/bin/mthydra-controller cover-attest-verified example.org \
  --vantage ru-vps-01 --evidence "smoke test" \
  --db-path "$TMP/state.sqlite"
.venv/bin/mthydra-controller cover-list --db-path "$TMP/state.sqlite"
.venv/bin/mthydra-controller cover-pool-stats --db-path "$TMP/state.sqlite" \
  --config packaging/etc/mthydra/controller.toml.example
```

Expected: every command exits 0; `cover-list` shows the domain in `candidate_verified`; `cover-pool-stats` reports `candidate_verified: 1`, `rotation_frozen: true` (only one verified, below default threshold of 2).

- [ ] **Step 4: Commit the coverage report (if any artifact)**

If a `.coverage` artifact or report file is generated and tracked, add it; otherwise this step is a no-op.

- [ ] **Step 5: Final commit summary**

```bash
git log --oneline -25
```

Confirm: 22 commits corresponding to Tasks 1–22 are present.

---

## Done criteria

- All 22 task checkboxes ticked.
- `pytest -q` passes cleanly.
- ≥ 90% line coverage on `mthydra.controller.state.cover_pool` and `mthydra.controller.state.cover_pool_scheduler`.
- All 6 new CLI subcommands exist and behave per spec §8.
- Startup self-check passes invariants #17–#20.
- Both schedulers run cleanly under `serve --mode offline` and `serve --mode production`.
- The integration test demonstrates that burned rows + audit-log entries survive a backup + restore round-trip.
