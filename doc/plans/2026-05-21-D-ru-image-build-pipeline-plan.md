# Spec D — RU Image Build Pipeline (D1) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement D1 of spec D — `doc/specs/2026-05-21-D-ru-image-build-pipeline.md`: schema v4→v5 with `ru_images` catalog, `S3Destination` extension for image artifacts, builder that downloads + verifies the upstream mtg release, weekly APScheduler upstream tracker, six CLI subcommands, and operator-attested promote. D2 (canary, validation gate, profile pinning) is structurally blocked on specs I/G/H and is not part of this plan.

**Architecture:** Schema v4→v5 adds `ru_images` table. `mthydra.controller.state.ru_images` is a new thin repository following the spec-A pattern. `mthydra.controller.image.builder` downloads upstream binaries via an injectable HTTP client, verifies sha256 against upstream's published checksum, uploads to B2 under content-addressed prefix, inserts the DB row. `mthydra.controller.image.upstream_tracker` mirrors the spec-C/F scheduler pattern (BackgroundScheduler + ThreadPoolExecutor, offline-mode short-circuit). `S3Destination` grows `put_image` + `head_image` methods. Six CLI subcommands, all active-only except read-only `image-current`. The tracker is armed on the active serve loop alongside the spec-C cover-pool sweeps and spec-F standby-poller.

**Tech stack:** Python 3.12 stdlib (`urllib.request`, `hashlib`, `json`) + APScheduler + boto3. No new runtime dependencies — GitHub HTTP is stdlib-only.

**Design decisions:** See spec §2 (D-D1 through D-D8).

---

## File Structure (locked before tasks)

**Modified:**
- `src/mthydra/controller/state/schema.py` — bump `SCHEMA_VERSION` to 5; add `_V5_MIGRATION` (table + index); add `migrate_v4_to_v5`; extend `apply_schema` dispatcher.
- `src/mthydra/controller/state/invariants.py` — extend `check_all()` with invariants #24–#25.
- `src/mthydra/controller/config.py` — add `ImageConfig` dataclass; load `[image]` TOML section.
- `src/mthydra/controller/backup/s3_dest.py` — add `put_image` + `head_image` methods.
- `src/mthydra/controller/cli.py` — six new subparsers + handlers; bootstrap obligation list extended; serve loop arms tracker.
- `packaging/etc/mthydra/controller.toml.example` — add `[image]` section.

**Created:**
- `src/mthydra/controller/state/ru_images.py` — repository.
- `src/mthydra/controller/image/__init__.py` — empty.
- `src/mthydra/controller/image/builder.py` — `build_image()` + `BuildError`.
- `src/mthydra/controller/image/upstream_tracker.py` — `UpstreamReleaseTracker`.
- `tests/unit/controller/state/test_ru_images.py`
- `tests/unit/controller/image/__init__.py` — empty.
- `tests/unit/controller/image/test_builder.py`
- `tests/unit/controller/image/test_upstream_tracker.py`
- `tests/integration/test_image_lifecycle.py`

Responsibility per file: schema owns DDL + migrations; ru_images owns state-machine semantics + audit emission; builder owns the download-verify-upload pipeline; upstream_tracker owns the GitHub poll + obligation. CLI is thin — handlers delegate to the modules above. No cross-imports between builder and tracker — both depend on the repository.

---

## Phase 1 — Schema v4 → v5

### Task 1: Schema migration + `ru_images` DDL

**Files:**
- Modify: `src/mthydra/controller/state/schema.py`
- Modify: `tests/unit/controller/state/test_schema.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/unit/controller/state/test_schema.py`:

```python
def test_schema_version_is_5(tmp_db_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import SCHEMA_VERSION, apply_schema
    assert SCHEMA_VERSION == 5
    conn = connect(tmp_db_path)
    apply_schema(conn)
    row = conn.execute("SELECT version FROM schema_version WHERE rowid=1").fetchone()
    assert row[0] == 5


def test_ru_images_table_present(tmp_db_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    conn = connect(tmp_db_path)
    apply_schema(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(ru_images)").fetchall()]
    assert {"image_version", "upstream_release", "upstream_repo",
            "binary_url", "manifest_url", "binary_sha256",
            "binary_size_bytes", "state", "built_at",
            "promoted_at", "retired_at", "notes"} == set(cols)


def test_ru_images_state_index_present(tmp_db_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    conn = connect(tmp_db_path)
    apply_schema(conn)
    idxs = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='ru_images'"
        ).fetchall()
    }
    assert "ix_ru_images_state" in idxs


def test_v4_to_v5_migration_adds_table(tmp_db_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import migrate_v4_to_v5
    conn = connect(tmp_db_path)
    conn.executescript(
        "CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL, CHECK (rowid=1));"
        "INSERT INTO schema_version (rowid, version, applied_at) VALUES (1, 4, '2026-05-21T00:00:00Z');"
    )
    conn.commit()
    migrate_v4_to_v5(conn)
    v = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    assert v == 5
    cols = [r[1] for r in conn.execute("PRAGMA table_info(ru_images)").fetchall()]
    assert "image_version" in cols
```

- [ ] **Step 2: Run tests — expect failure**

```bash
cd /home/asharov/RedHat/Dev/mthydra
pytest tests/unit/controller/state/test_schema.py -v
```

Expected: 4 failures.

- [ ] **Step 3: Edit `src/mthydra/controller/state/schema.py`**

Bump version:

```python
SCHEMA_VERSION = 5
```

Append to `_STATEMENTS` (after the spec-F `eu_nodes` table):

```python
    # --- spec D additions: RU image catalog ---
    """
    CREATE TABLE IF NOT EXISTS ru_images (
      image_version       TEXT    PRIMARY KEY,
      upstream_release    TEXT    NOT NULL,
      upstream_repo       TEXT    NOT NULL,
      binary_url          TEXT    NOT NULL,
      manifest_url        TEXT    NOT NULL,
      binary_sha256       TEXT    NOT NULL,
      binary_size_bytes   INTEGER NOT NULL,
      state               TEXT    NOT NULL CHECK (state IN ('candidate','promoted','retired')),
      built_at            TEXT    NOT NULL,
      promoted_at         TEXT,
      retired_at          TEXT,
      notes               TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_ru_images_state ON ru_images(state)
    """,
```

Add `migrate_v4_to_v5` after `migrate_v3_to_v4`:

```python
def migrate_v4_to_v5(conn: sqlite3.Connection) -> None:
    """Idempotent v4 → v5 migration: add ru_images table + index."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ru_images (
          image_version       TEXT    PRIMARY KEY,
          upstream_release    TEXT    NOT NULL,
          upstream_repo       TEXT    NOT NULL,
          binary_url          TEXT    NOT NULL,
          manifest_url        TEXT    NOT NULL,
          binary_sha256       TEXT    NOT NULL,
          binary_size_bytes   INTEGER NOT NULL,
          state               TEXT    NOT NULL CHECK (state IN ('candidate','promoted','retired')),
          built_at            TEXT    NOT NULL,
          promoted_at         TEXT,
          retired_at          TEXT,
          notes               TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_ru_images_state ON ru_images(state);
        """
    )
    conn.execute(
        "UPDATE schema_version SET version=?, applied_at=? WHERE rowid=1",
        (5, _now()),
    )
    conn.commit()
```

Extend the `apply_schema` migration dispatcher — after the existing `if current < 4: migrate_v3_to_v4(conn)` add:

```python
        if current < 5:
            migrate_v4_to_v5(conn)
```

- [ ] **Step 4: Run schema tests** — expect PASS

```bash
pytest tests/unit/controller/state/test_schema.py -v
```

- [ ] **Step 5: Run full unit suite for regressions**

```bash
pytest tests/unit -q
```

- [ ] **Step 6: Commit**

```bash
git add src/mthydra/controller/state/schema.py tests/unit/controller/state/test_schema.py
git commit -m "schema(D): v4→v5 migration — ru_images catalog table + state index"
```

---

## Phase 2 — Repository

### Task 2: `ru_images` repository module

**Files:**
- Create: `src/mthydra/controller/state/ru_images.py`
- Create: `tests/unit/controller/state/test_ru_images.py`

- [ ] **Step 1: Write failing tests**

```python
"""Spec D — ru_images catalog repository."""
import pytest

from mthydra.controller.state.audit import recent_events
from mthydra.controller.state.db import connect
from mthydra.controller.state.obligations import list_obligations, set_obligation
from mthydra.controller.state.ru_images import (
    RUImage, current_promoted, get_image, insert_candidate, list_images,
    promote, retire,
)
from mthydra.controller.state.schema import apply_schema


@pytest.fixture
def conn(tmp_db_path):
    c = connect(tmp_db_path)
    apply_schema(c)
    return c


NOW = "2026-05-21T00:00:00Z"


def _candidate(conn, image_version="iv1", release="v2.1.7"):
    insert_candidate(
        conn,
        image_version=image_version,
        upstream_release=release,
        upstream_repo="9seconds/mtg",
        binary_url=f"images/{image_version}/mtg",
        manifest_url=f"images/{image_version}/manifest.json",
        binary_sha256=image_version,
        binary_size_bytes=1024,
        built_at=NOW,
    )


def test_insert_candidate_round_trips(conn):
    _candidate(conn, "iv1", "v2.1.7")
    n = get_image(conn, "iv1")
    assert n.upstream_release == "v2.1.7"
    assert n.state == "candidate"
    assert n.promoted_at is None
    assert n.retired_at is None


def test_insert_candidate_emits_audit(conn):
    _candidate(conn, "iv1")
    ev = recent_events(conn, limit=1)
    assert ev[0].action == "image_built"
    assert ev[0].target == "iv1"


def test_promote_candidate(conn):
    _candidate(conn, "iv1", "v2.1.7")
    promote(conn, "iv1", at="2026-05-21T01:00:00Z", evidence="ran probes from RU vps")
    n = get_image(conn, "iv1")
    assert n.state == "promoted"
    assert n.promoted_at == "2026-05-21T01:00:00Z"


def test_promote_retires_prior_promoted(conn):
    _candidate(conn, "iv1", "v2.1.6")
    promote(conn, "iv1", at="2026-05-20T00:00:00Z", evidence="first")
    _candidate(conn, "iv2", "v2.1.7")
    promote(conn, "iv2", at="2026-05-21T00:00:00Z", evidence="second")
    n1 = get_image(conn, "iv1")
    n2 = get_image(conn, "iv2")
    assert n1.state == "retired"
    assert n1.retired_at == "2026-05-21T00:00:00Z"
    assert n2.state == "promoted"


def test_promote_refuses_non_candidate(conn):
    _candidate(conn, "iv1")
    promote(conn, "iv1", at=NOW, evidence="x")
    with pytest.raises(ValueError, match="candidate"):
        promote(conn, "iv1", at=NOW, evidence="x")  # already promoted


def test_promote_clears_upstream_release_obligation(conn):
    _candidate(conn, "iv1", "v2.1.7")
    set_obligation(
        conn,
        obligation_id="t4_upstream_release_available::v2.1.7",
        last_proven_at=NOW, proven_by="tracker",
        next_due_at=NOW,
    )
    promote(conn, "iv1", at=NOW, evidence="x")
    obs = {o.obligation_id for o in list_obligations(conn)}
    assert "t4_upstream_release_available::v2.1.7" not in obs
    assert "t4_image_promoted" in obs


def test_retire_candidate(conn):
    _candidate(conn, "iv1")
    retire(conn, "iv1", at="2026-05-21T02:00:00Z", reason="build_failed_validation")
    n = get_image(conn, "iv1")
    assert n.state == "retired"
    assert n.retired_at == "2026-05-21T02:00:00Z"


def test_retire_promoted(conn):
    _candidate(conn, "iv1")
    promote(conn, "iv1", at=NOW, evidence="x")
    retire(conn, "iv1", at="2026-05-21T03:00:00Z", reason="found_regression_in_wild")
    n = get_image(conn, "iv1")
    assert n.state == "retired"


def test_retire_refuses_already_retired(conn):
    _candidate(conn, "iv1")
    retire(conn, "iv1", at=NOW, reason="x")
    with pytest.raises(ValueError, match="already retired"):
        retire(conn, "iv1", at=NOW, reason="x")


def test_current_promoted_none_when_no_image(conn):
    assert current_promoted(conn) is None


def test_current_promoted_returns_singleton(conn):
    _candidate(conn, "iv1")
    promote(conn, "iv1", at=NOW, evidence="x")
    n = current_promoted(conn)
    assert n is not None
    assert n.image_version == "iv1"


def test_list_images_filters_by_state(conn):
    _candidate(conn, "iv1", "v2.1.6")
    promote(conn, "iv1", at=NOW, evidence="x")
    _candidate(conn, "iv2", "v2.1.7")
    cands = list_images(conn, state="candidate")
    proms = list_images(conn, state="promoted")
    assert [i.image_version for i in cands] == ["iv2"]
    assert [i.image_version for i in proms] == ["iv1"]
```

- [ ] **Step 2: Run tests — expect ImportError**

```bash
pytest tests/unit/controller/state/test_ru_images.py -v
```

- [ ] **Step 3: Create `src/mthydra/controller/state/ru_images.py`**

```python
"""Spec D — ru_images catalog repository.

The ru_images table tracks every binary we have built (`candidate`),
promoted to the provisioning default (`promoted`), or retired. State
transitions emit audit_log rows; `promote` is atomic within a single
transaction and re-stamps the t4 obligations.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from mthydra.controller.state.audit import log_event
from mthydra.controller.state.obligations import set_obligation


@dataclass(frozen=True)
class RUImage:
    image_version: str
    upstream_release: str
    upstream_repo: str
    binary_url: str
    manifest_url: str
    binary_sha256: str
    binary_size_bytes: int
    state: str
    built_at: str
    promoted_at: str | None
    retired_at: str | None
    notes: str | None


_COLS = (
    "image_version, upstream_release, upstream_repo, binary_url, manifest_url, "
    "binary_sha256, binary_size_bytes, state, built_at, "
    "promoted_at, retired_at, notes"
)


def insert_candidate(
    conn: sqlite3.Connection,
    *,
    image_version: str,
    upstream_release: str,
    upstream_repo: str,
    binary_url: str,
    manifest_url: str,
    binary_sha256: str,
    binary_size_bytes: int,
    built_at: str,
    notes: str | None = None,
    actor: str = "operator",
) -> None:
    """Insert a new candidate. Emits one audit row."""
    conn.execute(
        "INSERT INTO ru_images "
        "(image_version, upstream_release, upstream_repo, binary_url, manifest_url, "
        " binary_sha256, binary_size_bytes, state, built_at, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'candidate', ?, ?)",
        (image_version, upstream_release, upstream_repo, binary_url, manifest_url,
         binary_sha256, binary_size_bytes, built_at, notes),
    )
    log_event(
        conn, ts=built_at, actor=actor, action="image_built",
        target=image_version,
        details_json=json.dumps({
            "upstream_release": upstream_release,
            "upstream_repo": upstream_repo,
            "binary_sha256": binary_sha256,
        }, separators=(",", ":")),
    )
    conn.commit()


def promote(
    conn: sqlite3.Connection,
    image_version: str,
    *,
    at: str,
    evidence: str,
    actor: str = "operator",
) -> None:
    """Atomic candidate → promoted; prior promoted (if any) → retired.

    Re-stamps t4_image_promoted; clears t4_upstream_release_available::<tag>.
    """
    row = conn.execute(
        "SELECT state, upstream_release FROM ru_images WHERE image_version=?",
        (image_version,),
    ).fetchone()
    if row is None:
        raise LookupError(f"ru_image {image_version!r} not found")
    if row[0] != "candidate":
        raise ValueError(
            f"ru_image {image_version!r} is not a candidate (state={row[0]})"
        )
    upstream_release = row[1]

    prior = conn.execute(
        "SELECT image_version FROM ru_images WHERE state='promoted'"
    ).fetchone()
    prior_iv = prior[0] if prior is not None else None

    try:
        conn.execute("BEGIN")
        if prior_iv is not None:
            conn.execute(
                "UPDATE ru_images SET state='retired', retired_at=? "
                "WHERE image_version=?",
                (at, prior_iv),
            )
        conn.execute(
            "UPDATE ru_images SET state='promoted', promoted_at=? "
            "WHERE image_version=?",
            (at, image_version),
        )
        log_event(
            conn, ts=at, actor=actor, action="image_promoted",
            target=image_version,
            details_json=json.dumps({
                "evidence": evidence,
                "retired_predecessor": prior_iv,
            }, separators=(",", ":")),
        )
        # Re-stamp t4_image_promoted (30d heartbeat).
        next_due = _add_days_iso(at, 30)
        set_obligation(
            conn,
            obligation_id="t4_image_promoted",
            last_proven_at=at,
            proven_by="operator",
            next_due_at=next_due,
            details=image_version,
        )
        # Clear the matching anti-obligation.
        conn.execute(
            "DELETE FROM obligation_clocks WHERE obligation_id=?",
            (f"t4_upstream_release_available::{upstream_release}",),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def retire(
    conn: sqlite3.Connection,
    image_version: str,
    *,
    at: str,
    reason: str,
    actor: str = "operator",
) -> None:
    """state -> 'retired'. Legal from candidate or promoted. Idempotency:
    re-retiring a retired row raises ValueError."""
    row = conn.execute(
        "SELECT state FROM ru_images WHERE image_version=?", (image_version,)
    ).fetchone()
    if row is None:
        raise LookupError(f"ru_image {image_version!r} not found")
    prior_state = row[0]
    if prior_state == "retired":
        raise ValueError(f"ru_image {image_version!r} is already retired")
    conn.execute(
        "UPDATE ru_images SET state='retired', retired_at=? WHERE image_version=?",
        (at, image_version),
    )
    log_event(
        conn, ts=at, actor=actor, action="image_retired",
        target=image_version,
        details_json=json.dumps({"reason": reason, "prior_state": prior_state},
                                  separators=(",", ":")),
    )
    conn.commit()


def current_promoted(conn: sqlite3.Connection) -> RUImage | None:
    row = conn.execute(
        f"SELECT {_COLS} FROM ru_images WHERE state='promoted' LIMIT 1"
    ).fetchone()
    return RUImage(*row) if row is not None else None


def list_images(
    conn: sqlite3.Connection,
    *,
    state: str | None = None,
) -> list[RUImage]:
    if state is None:
        rows = conn.execute(
            f"SELECT {_COLS} FROM ru_images ORDER BY built_at DESC"
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {_COLS} FROM ru_images WHERE state=? ORDER BY built_at DESC",
            (state,),
        ).fetchall()
    return [RUImage(*r) for r in rows]


def get_image(conn: sqlite3.Connection, image_version: str) -> RUImage:
    row = conn.execute(
        f"SELECT {_COLS} FROM ru_images WHERE image_version=?", (image_version,)
    ).fetchone()
    if row is None:
        raise LookupError(f"ru_image {image_version!r} not found")
    return RUImage(*row)


def _add_days_iso(iso: str, days: int) -> str:
    from datetime import datetime, timedelta
    t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (t + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
```

- [ ] **Step 4: Run tests — expect PASS (12/12)**

```bash
pytest tests/unit/controller/state/test_ru_images.py -v
```

- [ ] **Step 5: Run full unit suite**

```bash
pytest tests/unit -q
```

- [ ] **Step 6: Commit**

```bash
git add src/mthydra/controller/state/ru_images.py tests/unit/controller/state/test_ru_images.py
git commit -m "state(D): ru_images catalog repository with audit-emitting state transitions"
```

---

## Phase 3 — Invariants #24-#25

### Task 3: Extend `check_all` with image-catalog invariants

**Files:**
- Modify: `src/mthydra/controller/state/invariants.py`
- Modify: `tests/unit/controller/state/test_invariants.py`

- [ ] **Step 1: Write failing tests** — append to `tests/unit/controller/state/test_invariants.py`:

```python
# ---------------------------------------------------------------------------
# Spec D invariant checks (#24–#25)
# ---------------------------------------------------------------------------

def test_check_24_rejects_two_promoted_images(tmp_db_path):
    conn = _seeded(tmp_db_path)
    # Insert two promoted rows directly (bypass repo).
    for iv in ("iv1", "iv2"):
        conn.execute(
            "INSERT INTO ru_images "
            "(image_version, upstream_release, upstream_repo, binary_url, manifest_url, "
            " binary_sha256, binary_size_bytes, state, built_at, promoted_at) "
            "VALUES (?, 'v', 'r', 'b', 'm', ?, 100, 'promoted', ?, ?)",
            (iv, iv, NOW, NOW),
        )
    conn.commit()
    with pytest.raises(InvariantViolation, match="check 24"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_25_rejects_promoted_without_promoted_at(tmp_db_path):
    conn = _seeded(tmp_db_path)
    conn.execute(
        "INSERT INTO ru_images "
        "(image_version, upstream_release, upstream_repo, binary_url, manifest_url, "
        " binary_sha256, binary_size_bytes, state, built_at) "
        "VALUES ('iv1', 'v', 'r', 'b', 'm', 'iv1', 100, 'promoted', ?)",
        (NOW,),
    )
    conn.commit()
    with pytest.raises(InvariantViolation, match="check 25"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_25_rejects_retired_without_retired_at(tmp_db_path):
    conn = _seeded(tmp_db_path)
    conn.execute(
        "INSERT INTO ru_images "
        "(image_version, upstream_release, upstream_repo, binary_url, manifest_url, "
        " binary_sha256, binary_size_bytes, state, built_at) "
        "VALUES ('iv1', 'v', 'r', 'b', 'm', 'iv1', 100, 'retired', ?)",
        (NOW,),
    )
    conn.commit()
    with pytest.raises(InvariantViolation, match="check 25"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_25_rejects_candidate_with_promoted_at(tmp_db_path):
    conn = _seeded(tmp_db_path)
    conn.execute(
        "INSERT INTO ru_images "
        "(image_version, upstream_release, upstream_repo, binary_url, manifest_url, "
        " binary_sha256, binary_size_bytes, state, built_at, promoted_at) "
        "VALUES ('iv1', 'v', 'r', 'b', 'm', 'iv1', 100, 'candidate', ?, ?)",
        (NOW, NOW),
    )
    conn.commit()
    with pytest.raises(InvariantViolation, match="check 25"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)
```

- [ ] **Step 2: Verify failure**

```bash
pytest tests/unit/controller/state/test_invariants.py -k 'check_24 or check_25' -v
```

- [ ] **Step 3: Append checks to `check_all` in `src/mthydra/controller/state/invariants.py`** (at end of function):

```python
    # --- spec D checks (#24–#25) ---

    # Check 24: at most one promoted image
    p = _scalar(conn, "SELECT COUNT(*) FROM ru_images WHERE state='promoted'")
    if p > 1:
        raise InvariantViolation(
            f"check 24: at most one promoted ru_image permitted, found {p}"
        )

    # Check 25: state timestamps consistent
    bad = conn.execute(
        "SELECT image_version, state, promoted_at, retired_at FROM ru_images WHERE "
        "(state='promoted'  AND promoted_at IS NULL) OR "
        "(state='retired'   AND retired_at  IS NULL) OR "
        "(state='candidate' AND (promoted_at IS NOT NULL OR retired_at IS NOT NULL)) "
        "LIMIT 1"
    ).fetchone()
    if bad is not None:
        raise InvariantViolation(
            f"check 25: ru_image {bad[0]!r} state={bad[1]!r} has inconsistent "
            f"timestamps (promoted_at={bad[2]!r}, retired_at={bad[3]!r})"
        )
```

- [ ] **Step 4: Run tests + full suite**

```bash
pytest tests/unit/controller/state/test_invariants.py -v
pytest tests/unit -q
```

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/state/invariants.py tests/unit/controller/state/test_invariants.py
git commit -m "invariants(D): startup checks 24-25 (singleton promoted, state-timestamp consistency)"
```

---

## Phase 4 — Config

### Task 4: `ImageConfig` + `[image]` TOML section

**Files:**
- Modify: `src/mthydra/controller/config.py`
- Modify: `packaging/etc/mthydra/controller.toml.example`
- Modify: `tests/unit/controller/test_config.py`
- Modify: `tests/unit/controller/test_cli.py` (if any test constructs `Config(...)` literally)

- [ ] **Step 1: Append failing test to `tests/unit/controller/test_config.py`**

```python
def test_load_image_config(tmp_path):
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
        "[image]\n"
        "upstream_repo = '9seconds/mtg'\n"
        "upstream_release_asset = 'mtg-linux-amd64'\n"
        "upstream_check_interval = '168h'\n"
        "github_api_url = 'https://api.github.com'\n"
        "build_tmp_dir = '/var/lib/mthydra/tmp'\n"
    )
    cfg = load_config(cfg_path)
    assert cfg.image.upstream_repo == "9seconds/mtg"
    assert cfg.image.upstream_release_asset == "mtg-linux-amd64"
    assert cfg.image.upstream_check_interval_seconds == 168 * 3600
    assert cfg.image.github_api_url == "https://api.github.com"
    assert cfg.image.build_tmp_dir == "/var/lib/mthydra/tmp"


def test_load_image_config_defaults(tmp_path):
    """Missing [image] section: load with safe defaults."""
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
    assert cfg.image.upstream_repo == "9seconds/mtg"
    assert cfg.image.upstream_release_asset == "mtg-linux-amd64"
    assert cfg.image.upstream_check_interval_seconds == 168 * 3600
    assert cfg.image.github_api_url == "https://api.github.com"
    assert cfg.image.build_tmp_dir == "/var/lib/mthydra/tmp"
```

- [ ] **Step 2: Verify failure**

- [ ] **Step 3: Modify `src/mthydra/controller/config.py`**

Add dataclass below `StandbyConfig`:

```python
@dataclass(frozen=True)
class ImageConfig:
    upstream_repo: str
    upstream_release_asset: str
    upstream_check_interval_seconds: int
    github_api_url: str
    build_tmp_dir: str
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
    image: ImageConfig
```

Add `_load_image` helper (mirrors `_load_cover_pool`):

```python
def _load_image(data: dict) -> ImageConfig:
    sec = data.get("image", {})
    return ImageConfig(
        upstream_repo=str(sec.get("upstream_repo", "9seconds/mtg")),
        upstream_release_asset=str(sec.get("upstream_release_asset", "mtg-linux-amd64")),
        upstream_check_interval_seconds=_parse_interval_seconds(
            "image.upstream_check_interval",
            sec.get("upstream_check_interval", "168h"),
        ),
        github_api_url=str(sec.get("github_api_url", "https://api.github.com")),
        build_tmp_dir=str(sec.get("build_tmp_dir", "/var/lib/mthydra/tmp")),
    )
```

Wire `image=_load_image(data)` into the `Config(...)` constructor call in `load_config`.

- [ ] **Step 4: Append to `packaging/etc/mthydra/controller.toml.example`**

```toml

[image]
upstream_repo           = "9seconds/mtg"
upstream_release_asset  = "mtg-linux-amd64"
upstream_check_interval = "168h"
github_api_url          = "https://api.github.com"
build_tmp_dir           = "/var/lib/mthydra/tmp"
```

- [ ] **Step 5: Run config tests + full suite**

```bash
pytest tests/unit/controller/test_config.py -v
pytest tests/unit -q
```

If any `Config(...)` literal in `test_cli.py` needs the new field, add a default `ImageConfig(upstream_repo="9seconds/mtg", upstream_release_asset="mtg-linux-amd64", upstream_check_interval_seconds=168*3600, github_api_url="https://api.github.com", build_tmp_dir="/var/lib/mthydra/tmp")`.

- [ ] **Step 6: Commit**

```bash
git add src/mthydra/controller/config.py packaging/etc/mthydra/controller.toml.example tests/unit/controller/test_config.py tests/unit/controller/test_cli.py
git commit -m "config(D): ImageConfig dataclass + [image] TOML section"
```

---

## Phase 5 — S3Destination image methods

### Task 5: `put_image` + `head_image`

**Files:**
- Modify: `src/mthydra/controller/backup/s3_dest.py`
- Modify: `tests/unit/controller/backup/test_s3_dest.py`

- [ ] **Step 1: Read `tests/unit/controller/backup/test_s3_dest.py`** to identify the existing Object-Lock-enabled bucket fixture (likely `s3_env` per spec F's Task 7 adaptation). Use the same fixture name.

- [ ] **Step 2: Append failing tests**

```python
def test_put_image_uploads_binary_and_manifest(s3_env, tmp_path):
    """put_image uploads both binary and manifest under content-addressed prefix."""
    dest = s3_env
    binary_path = tmp_path / "mtg"
    binary_path.write_bytes(b"\x7fELF" + b"\x00" * 100)
    manifest = b'{"image_version":"abc","schema":"mthydra.ru_image.v1"}'

    dest.put_image(image_version="abc123", binary_path=binary_path, manifest=manifest)

    # Verify both keys present via head
    info = dest.head_image(image_version="abc123")
    assert info is not None
    assert "etag" in info
    assert info["size_bytes"] == binary_path.stat().st_size


def test_head_image_returns_none_when_absent(s3_env):
    dest = s3_env
    info = dest.head_image(image_version="not-there")
    assert info is None
```

- [ ] **Step 3: Verify failure**

```bash
pytest tests/unit/controller/backup/test_s3_dest.py -k image -v
```

- [ ] **Step 4: Add methods to `S3Destination` in `src/mthydra/controller/backup/s3_dest.py`** (alongside the existing heartbeat methods):

```python
    @staticmethod
    def _image_binary_key(image_version: str) -> str:
        return f"images/{image_version}/mtg"

    @staticmethod
    def _image_manifest_key(image_version: str) -> str:
        return f"images/{image_version}/manifest.json"

    def put_image(
        self, *, image_version: str, binary_path: Path, manifest: bytes,
    ) -> None:
        """Upload binary + manifest to B2, both under Object Lock COMPLIANCE."""
        retain_until = datetime.now(timezone.utc) + timedelta(days=self.object_lock_days)
        with open(binary_path, "rb") as fh:
            self._client.put_object(
                Bucket=self.bucket,
                Key=self._image_binary_key(image_version),
                Body=fh,
                ObjectLockMode="COMPLIANCE",
                ObjectLockRetainUntilDate=retain_until,
            )
        self._client.put_object(
            Bucket=self.bucket,
            Key=self._image_manifest_key(image_version),
            Body=manifest,
            ContentType="application/json",
            ObjectLockMode="COMPLIANCE",
            ObjectLockRetainUntilDate=retain_until,
        )

    def head_image(self, *, image_version: str) -> dict[str, Any] | None:
        """Returns binary head info or None if absent."""
        try:
            obj = self._client.head_object(
                Bucket=self.bucket, Key=self._image_binary_key(image_version)
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

- [ ] **Step 5: Run tests** — expect PASS

```bash
pytest tests/unit/controller/backup/test_s3_dest.py -v
```

- [ ] **Step 6: Commit**

```bash
git add src/mthydra/controller/backup/s3_dest.py tests/unit/controller/backup/test_s3_dest.py
git commit -m "s3_dest(D): put_image/head_image methods for ru_images B2 storage"
```

---

## Phase 6 — Builder

### Task 6: `image.builder.build_image`

**Files:**
- Create: `src/mthydra/controller/image/__init__.py` (empty)
- Create: `src/mthydra/controller/image/builder.py`
- Create: `tests/unit/controller/image/__init__.py` (empty)
- Create: `tests/unit/controller/image/test_builder.py`

- [ ] **Step 1: Create empty package files**

```bash
mkdir -p src/mthydra/controller/image tests/unit/controller/image
touch src/mthydra/controller/image/__init__.py
touch tests/unit/controller/image/__init__.py
```

- [ ] **Step 2: Write failing tests** (`tests/unit/controller/image/test_builder.py`):

```python
"""Spec D — image.builder unit tests."""
import hashlib
import json
from unittest.mock import MagicMock

import pytest

from mthydra.controller.image.builder import BuildError, build_image
from mthydra.controller.state.db import connect
from mthydra.controller.state.ru_images import get_image, list_images
from mthydra.controller.state.schema import apply_schema


@pytest.fixture
def conn(tmp_db_path):
    c = connect(tmp_db_path)
    apply_schema(c)
    return c


def _mock_http(release_json, asset_bytes, checksum_text):
    """Build a MagicMock http_client whose .get(url) returns sensible responses."""
    def _get(url):
        resp = MagicMock()
        if url.endswith("/releases/tags/v2.1.7"):
            resp.status = 200
            resp.read.return_value = json.dumps(release_json).encode("utf-8")
        elif url.endswith("/mtg-linux-amd64"):
            resp.status = 200
            resp.read.return_value = asset_bytes
        elif url.endswith("/SHA256SUMS") or url.endswith(".sha256"):
            resp.status = 200
            resp.read.return_value = checksum_text.encode("utf-8")
        else:
            resp.status = 404
            resp.read.return_value = b""
        return resp
    return _get


def test_build_image_happy_path(conn, tmp_path):
    asset_bytes = b"mtg-binary-bytes" * 100
    sha = hashlib.sha256(asset_bytes).hexdigest()
    checksum_text = f"{sha}  mtg-linux-amd64\n"
    release_json = {
        "tag_name": "v2.1.7",
        "assets": [
            {"name": "mtg-linux-amd64", "browser_download_url": "https://example/mtg-linux-amd64"},
            {"name": "SHA256SUMS", "browser_download_url": "https://example/SHA256SUMS"},
        ],
    }
    b2 = MagicMock()

    image_version = build_image(
        conn=conn,
        b2_destination=b2,
        upstream_repo="9seconds/mtg",
        upstream_release="v2.1.7",
        asset_filename="mtg-linux-amd64",
        github_api_url="https://api.github.com",
        tmp_dir=tmp_path,
        now="2026-05-21T00:00:00Z",
        http_client=_mock_http(release_json, asset_bytes, checksum_text),
    )

    assert image_version == sha
    b2.put_image.assert_called_once()
    kwargs = b2.put_image.call_args.kwargs
    assert kwargs["image_version"] == sha
    assert kwargs["binary_path"].exists()

    n = get_image(conn, sha)
    assert n.state == "candidate"
    assert n.upstream_release == "v2.1.7"
    assert n.binary_sha256 == sha


def test_build_image_checksum_mismatch_raises(conn, tmp_path):
    asset_bytes = b"some bytes"
    checksum_text = "deadbeef" * 8 + "  mtg-linux-amd64\n"  # wrong sha
    release_json = {
        "tag_name": "v2.1.7",
        "assets": [
            {"name": "mtg-linux-amd64", "browser_download_url": "https://example/mtg-linux-amd64"},
            {"name": "SHA256SUMS", "browser_download_url": "https://example/SHA256SUMS"},
        ],
    }
    b2 = MagicMock()

    with pytest.raises(BuildError, match="sha256 mismatch"):
        build_image(
            conn=conn, b2_destination=b2,
            upstream_repo="9seconds/mtg",
            upstream_release="v2.1.7",
            asset_filename="mtg-linux-amd64",
            github_api_url="https://api.github.com",
            tmp_dir=tmp_path,
            now="2026-05-21T00:00:00Z",
            http_client=_mock_http(release_json, asset_bytes, checksum_text),
        )
    b2.put_image.assert_not_called()
    assert list_images(conn) == []


def test_build_image_release_not_found(conn, tmp_path):
    def _get(url):
        resp = MagicMock()
        resp.status = 404
        resp.read.return_value = b'{"message":"Not Found"}'
        return resp
    with pytest.raises(BuildError, match="release"):
        build_image(
            conn=conn, b2_destination=MagicMock(),
            upstream_repo="9seconds/mtg",
            upstream_release="v9.99.99",
            asset_filename="mtg-linux-amd64",
            github_api_url="https://api.github.com",
            tmp_dir=tmp_path,
            now="2026-05-21T00:00:00Z",
            http_client=_get,
        )


def test_build_image_asset_missing(conn, tmp_path):
    release_json = {
        "tag_name": "v2.1.7",
        "assets": [{"name": "OTHER", "browser_download_url": "https://example/OTHER"}],
    }
    def _get(url):
        resp = MagicMock()
        if url.endswith("/releases/tags/v2.1.7"):
            resp.status = 200
            resp.read.return_value = json.dumps(release_json).encode()
        else:
            resp.status = 404
            resp.read.return_value = b""
        return resp
    with pytest.raises(BuildError, match="asset"):
        build_image(
            conn=conn, b2_destination=MagicMock(),
            upstream_repo="9seconds/mtg",
            upstream_release="v2.1.7",
            asset_filename="mtg-linux-amd64",
            github_api_url="https://api.github.com",
            tmp_dir=tmp_path,
            now="2026-05-21T00:00:00Z",
            http_client=_get,
        )


def test_build_image_checksum_file_missing(conn, tmp_path):
    asset_bytes = b"binary"
    release_json = {
        "tag_name": "v2.1.7",
        "assets": [
            {"name": "mtg-linux-amd64", "browser_download_url": "https://example/mtg-linux-amd64"},
        ],
    }
    def _get(url):
        resp = MagicMock()
        if url.endswith("/releases/tags/v2.1.7"):
            resp.status = 200
            resp.read.return_value = json.dumps(release_json).encode()
        elif url.endswith("/mtg-linux-amd64"):
            resp.status = 200
            resp.read.return_value = asset_bytes
        else:
            resp.status = 404
            resp.read.return_value = b""
        return resp
    with pytest.raises(BuildError, match="checksum"):
        build_image(
            conn=conn, b2_destination=MagicMock(),
            upstream_repo="9seconds/mtg",
            upstream_release="v2.1.7",
            asset_filename="mtg-linux-amd64",
            github_api_url="https://api.github.com",
            tmp_dir=tmp_path,
            now="2026-05-21T00:00:00Z",
            http_client=_get,
        )


def test_build_image_b2_upload_failure_no_db_row(conn, tmp_path):
    asset_bytes = b"some bytes"
    sha = hashlib.sha256(asset_bytes).hexdigest()
    checksum_text = f"{sha}  mtg-linux-amd64\n"
    release_json = {
        "tag_name": "v2.1.7",
        "assets": [
            {"name": "mtg-linux-amd64", "browser_download_url": "https://example/mtg-linux-amd64"},
            {"name": "SHA256SUMS", "browser_download_url": "https://example/SHA256SUMS"},
        ],
    }
    b2 = MagicMock()
    b2.put_image.side_effect = RuntimeError("B2 upload failed")

    with pytest.raises(BuildError, match="B2 upload"):
        build_image(
            conn=conn, b2_destination=b2,
            upstream_repo="9seconds/mtg",
            upstream_release="v2.1.7",
            asset_filename="mtg-linux-amd64",
            github_api_url="https://api.github.com",
            tmp_dir=tmp_path,
            now="2026-05-21T00:00:00Z",
            http_client=_mock_http(release_json, asset_bytes, checksum_text),
        )
    assert list_images(conn) == []
```

- [ ] **Step 3: Verify failure**

```bash
pytest tests/unit/controller/image/test_builder.py -v
```

- [ ] **Step 4: Create `src/mthydra/controller/image/builder.py`**

```python
"""Spec D — image builder.

build_image() downloads the upstream release artifact + checksum file from
GitHub, verifies sha256, uploads to B2, and inserts a ru_images candidate row.
B2 upload happens BEFORE the DB insert so a failure only leaves a possibly-
orphaned B2 object (visible via head_image), never a phantom catalog row.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from mthydra.controller.state.ru_images import insert_candidate


class BuildError(RuntimeError):
    """Raised when image-build cannot complete safely."""


_CHECKSUM_ASSET_CANDIDATES = ("SHA256SUMS", "checksums.txt")


def _default_http_get(url: str):
    """urllib.request stdlib client; returns a response-like object with
    .status (int) and .read() -> bytes."""
    req = urllib.request.Request(url, headers={"Accept": "application/octet-stream"})
    resp = urllib.request.urlopen(req, timeout=30)
    # urllib's response doesn't have a `.status` on older Pythons; alias it.
    class _R:
        def __init__(self, r):
            self.status = r.getcode()
            self._r = r
        def read(self):
            return self._r.read()
    return _R(resp)


def build_image(
    *,
    conn: sqlite3.Connection,
    b2_destination,
    upstream_repo: str,
    upstream_release: str,
    asset_filename: str,
    github_api_url: str,
    tmp_dir: Path,
    now: str,
    actor: str = "operator",
    http_client: Callable | None = None,
) -> str:
    """Download upstream binary, verify sha256, upload to B2, insert ru_images.

    Returns the new image_version (hex sha256). Raises BuildError on any
    failure path; never partially writes (B2 upload precedes DB insert).
    """
    get = http_client or _default_http_get
    tmp_dir = Path(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    # 1. Fetch release metadata.
    release_url = f"{github_api_url}/repos/{upstream_repo}/releases/tags/{upstream_release}"
    try:
        resp = get(release_url)
        if resp.status != 200:
            raise BuildError(
                f"release not found: GET {release_url} -> {resp.status}"
            )
        release = json.loads(resp.read())
    except BuildError:
        raise
    except Exception as e:
        raise BuildError(f"GitHub API request failed: {e}") from e

    assets = {a["name"]: a["browser_download_url"] for a in release.get("assets", [])}

    # 2. Locate the binary asset.
    if asset_filename not in assets:
        raise BuildError(
            f"asset {asset_filename!r} not present in release {upstream_release!r}; "
            f"available: {sorted(assets)}"
        )
    binary_url = assets[asset_filename]

    # 3. Locate the checksum file.
    checksum_url: str | None = None
    for name in (f"{asset_filename}.sha256", *_CHECKSUM_ASSET_CANDIDATES):
        if name in assets:
            checksum_url = assets[name]
            break
    if checksum_url is None:
        raise BuildError(
            f"checksum file not in release {upstream_release!r}; "
            f"expected one of: {asset_filename}.sha256, SHA256SUMS, checksums.txt"
        )

    # 4. Download both.
    try:
        binary_bytes = get(binary_url).read()
        checksum_bytes = get(checksum_url).read()
    except Exception as e:
        raise BuildError(f"asset download failed: {e}") from e

    # 5. Verify sha256.
    expected_sha = _parse_checksum_for(asset_filename, checksum_bytes.decode("utf-8", errors="replace"))
    if expected_sha is None:
        raise BuildError(
            f"checksum file does not contain a line for {asset_filename!r}"
        )
    actual_sha = hashlib.sha256(binary_bytes).hexdigest()
    if actual_sha != expected_sha:
        raise BuildError(
            f"sha256 mismatch for {asset_filename!r}: "
            f"upstream={expected_sha} actual={actual_sha}"
        )
    image_version = actual_sha

    # 6. Write the binary into tmp_dir.
    binary_path = tmp_dir / f"image-{image_version}.bin"
    binary_path.write_bytes(binary_bytes)
    binary_path.chmod(0o600)

    # 7. Build manifest.
    manifest_dict = {
        "schema": "mthydra.ru_image.v1",
        "image_version": image_version,
        "upstream_repo": upstream_repo,
        "upstream_release": upstream_release,
        "binary_filename": asset_filename,
        "binary_sha256": image_version,
        "binary_size_bytes": len(binary_bytes),
        "built_at": now,
        "built_by": actor,
    }
    manifest_bytes = json.dumps(manifest_dict, separators=(",", ":")).encode("utf-8")

    # 8. Upload to B2 BEFORE inserting the DB row.
    try:
        b2_destination.put_image(
            image_version=image_version,
            binary_path=binary_path,
            manifest=manifest_bytes,
        )
    except Exception as e:
        raise BuildError(f"B2 upload failed: {e}") from e

    # 9. Insert ru_images candidate row.
    insert_candidate(
        conn,
        image_version=image_version,
        upstream_release=upstream_release,
        upstream_repo=upstream_repo,
        binary_url=f"images/{image_version}/mtg",
        manifest_url=f"images/{image_version}/manifest.json",
        binary_sha256=image_version,
        binary_size_bytes=len(binary_bytes),
        built_at=now,
        actor=actor,
    )
    return image_version


def _parse_checksum_for(asset_filename: str, checksum_text: str) -> str | None:
    """Find the sha256 line for `asset_filename` in a checksum file.

    Supports both `<sha>  <filename>` (SHA256SUMS) and bare-hash (.sha256) formats.
    """
    for line in checksum_text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) == 1 and len(parts[0]) == 64:
            # Bare hash format
            return parts[0].lower()
        if len(parts) >= 2:
            sha, name = parts[0], parts[-1].lstrip("*")
            if name == asset_filename and len(sha) == 64:
                return sha.lower()
    return None
```

- [ ] **Step 5: Run tests + full suite**

```bash
pytest tests/unit/controller/image/test_builder.py -v
pytest tests/unit -q
```

Expected: PASS (6/6 builder tests).

- [ ] **Step 6: Commit**

```bash
git add src/mthydra/controller/image/ tests/unit/controller/image/
git commit -m "image(D): builder — download + verify upstream release + B2 upload + DB insert"
```

---

## Phase 7 — Upstream tracker

### Task 7: `UpstreamReleaseTracker`

**Files:**
- Create: `src/mthydra/controller/image/upstream_tracker.py`
- Create: `tests/unit/controller/image/test_upstream_tracker.py`

- [ ] **Step 1: Write failing tests**

```python
"""Spec D — UpstreamReleaseTracker."""
import json
from unittest.mock import MagicMock

import pytest

from mthydra.controller.image.upstream_tracker import UpstreamReleaseTracker
from mthydra.controller.state.audit import recent_events
from mthydra.controller.state.db import connect
from mthydra.controller.state.obligations import list_obligations
from mthydra.controller.state.schema import apply_schema


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "state.sqlite"
    conn = connect(p)
    apply_schema(conn)
    conn.close()
    return p


def _mk_http(tag, status=200):
    def _get(url):
        resp = MagicMock()
        resp.status = status
        if status == 200:
            resp.read.return_value = json.dumps({"tag_name": tag}).encode("utf-8")
        else:
            resp.read.return_value = b""
        return resp
    return _get


def test_first_run_sets_anti_obligation(db):
    tracker = UpstreamReleaseTracker(
        db_path=db, upstream_repo="9seconds/mtg",
        github_api_url="https://api.github.com",
        poll_interval_seconds=3600, mode="offline",
        clock=lambda: "2026-05-21T00:00:00Z",
        http_client=_mk_http("v2.1.7"),
    )
    latest = tracker.run_once()
    assert latest == "v2.1.7"
    conn = connect(db)
    obs = {o.obligation_id for o in list_obligations(conn)}
    assert "t4_upstream_release_available::v2.1.7" in obs
    assert "t4_upstream_check" in obs
    conn.close()


def test_repeat_with_same_tag_does_not_double_announce(db):
    """Re-running with the same upstream tag should not produce duplicate audit
    rows beyond the per-tick check-stamp."""
    tracker = UpstreamReleaseTracker(
        db_path=db, upstream_repo="9seconds/mtg",
        github_api_url="https://api.github.com",
        poll_interval_seconds=3600, mode="offline",
        clock=lambda: "2026-05-21T00:00:00Z",
        http_client=_mk_http("v2.1.7"),
    )
    tracker.run_once()
    audit_count_first = len(recent_events(connect(db), limit=50))
    tracker.run_once()
    audit_count_second = len(recent_events(connect(db), limit=50))
    # Both runs emit an "upstream_release_seen" audit row each tick (heartbeat).
    assert audit_count_second == audit_count_first + 1


def test_run_once_returns_none_on_github_failure(db):
    tracker = UpstreamReleaseTracker(
        db_path=db, upstream_repo="9seconds/mtg",
        github_api_url="https://api.github.com",
        poll_interval_seconds=3600, mode="offline",
        clock=lambda: "2026-05-21T00:00:00Z",
        http_client=_mk_http("ignored", status=503),
    )
    latest = tracker.run_once()
    assert latest is None
    conn = connect(db)
    obs = {o.obligation_id for o in list_obligations(conn)}
    # On failure, t4_upstream_check is NOT stamped (we never lie about checking).
    assert "t4_upstream_check" not in obs
    conn.close()


def test_known_tag_does_not_re_emit_anti_obligation(db):
    """If the tag is already in ru_images (was built/promoted), no anti-obligation."""
    conn = connect(db)
    from mthydra.controller.state.ru_images import insert_candidate
    insert_candidate(
        conn,
        image_version="iv1",
        upstream_release="v2.1.7",
        upstream_repo="9seconds/mtg",
        binary_url="x", manifest_url="x", binary_sha256="iv1",
        binary_size_bytes=100,
        built_at="2026-05-21T00:00:00Z",
    )
    conn.close()
    tracker = UpstreamReleaseTracker(
        db_path=db, upstream_repo="9seconds/mtg",
        github_api_url="https://api.github.com",
        poll_interval_seconds=3600, mode="offline",
        clock=lambda: "2026-05-21T01:00:00Z",
        http_client=_mk_http("v2.1.7"),
    )
    tracker.run_once()
    conn = connect(db)
    obs = {o.obligation_id for o in list_obligations(conn)}
    assert "t4_upstream_release_available::v2.1.7" not in obs
    assert "t4_upstream_check" in obs
    conn.close()
```

- [ ] **Step 2: Verify failure**

```bash
pytest tests/unit/controller/image/test_upstream_tracker.py -v
```

- [ ] **Step 3: Create `src/mthydra/controller/image/upstream_tracker.py`**

```python
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

        Side effects:
          - sets t4_upstream_release_available::<tag> if newer
          - stamps t4_upstream_check on success only
          - emits audit row action='upstream_release_seen'
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
            # Stamp the per-tick check obligation (success only).
            set_obligation(
                conn,
                obligation_id="t4_upstream_check",
                last_proven_at=now,
                proven_by="upstream_tracker",
                next_due_at=_add_seconds_iso(now, self.poll_interval_seconds * 2),
                details=tag,
            )
            # Heartbeat audit row each successful tick (T4 currency signal).
            log_event(
                conn, ts=now, actor="upstream_tracker", action="upstream_release_seen",
                target=tag, details_json=None,
            )
            # If the latest tag isn't yet in ru_images, emit the anti-obligation.
            already = conn.execute(
                "SELECT 1 FROM ru_images WHERE upstream_release=? LIMIT 1", (tag,)
            ).fetchone()
            if already is None:
                set_obligation(
                    conn,
                    obligation_id=f"t4_upstream_release_available::{tag}",
                    last_proven_at=now,
                    proven_by="upstream_tracker",
                    next_due_at=now,  # anti-obligation
                    details=tag,
                )
            return tag
        finally:
            conn.close()
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/unit/controller/image/test_upstream_tracker.py -v
pytest tests/unit -q
```

Expected: PASS (4/4 tracker tests).

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/image/upstream_tracker.py tests/unit/controller/image/test_upstream_tracker.py
git commit -m "image(D): UpstreamReleaseTracker — weekly GitHub-releases poll with anti-obligation"
```

---

## Phase 8 — CLI

### Task 8: All six CLI subcommands

Because all six commands share helpers (`_require_active_role` from spec F, `_now()`), this lands in one commit; each command is verified by its own test set.

**Files:**
- Modify: `src/mthydra/controller/cli.py`
- Modify: `tests/unit/controller/test_cli.py`

- [ ] **Step 1: Write failing tests** — append to `tests/unit/controller/test_cli.py`:

```python
def test_image_build_happy_path(tmp_path, age_recipient, monkeypatch):
    """image-build delegates to build_image; happy path returns 0."""
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])

    captured = {}
    def _stub_build_image(**kwargs):
        captured.update(kwargs)
        # Minimum: insert a row so subsequent tests see something.
        from mthydra.controller.state.ru_images import insert_candidate
        insert_candidate(
            kwargs["conn"],
            image_version="iv-stub",
            upstream_release=kwargs["upstream_release"],
            upstream_repo=kwargs["upstream_repo"],
            binary_url="images/iv-stub/mtg",
            manifest_url="images/iv-stub/manifest.json",
            binary_sha256="iv-stub",
            binary_size_bytes=100,
            built_at=kwargs["now"],
        )
        return "iv-stub"
    monkeypatch.setattr("mthydra.controller.cli.build_image", _stub_build_image)

    rc = run(["image-build", "--release", "v2.1.7",
              "--db-path", str(db), "--config", str(cfg_path)])
    assert rc == 0
    assert captured["upstream_release"] == "v2.1.7"
    assert captured["upstream_repo"] == "9seconds/mtg"


def test_image_build_refused_on_standby(tmp_path, age_recipient, capsys, monkeypatch):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    run(["init", "--role", "standby", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    rc = run(["image-build", "--release", "v2.1.7",
              "--db-path", str(db), "--config", str(cfg_path)])
    assert rc == 2
    assert "active-only" in capsys.readouterr().err.lower()


def test_image_list_json(tmp_path, age_recipient, capsys):
    import json
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    # Seed a row via direct repo call
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_images import insert_candidate
    conn = connect(db)
    insert_candidate(
        conn, image_version="iv1",
        upstream_release="v2.1.7", upstream_repo="9seconds/mtg",
        binary_url="x", manifest_url="x", binary_sha256="iv1",
        binary_size_bytes=100, built_at="2026-05-21T00:00:00Z",
    )
    conn.close()
    capsys.readouterr()
    rc = run(["image-list", "--db-path", str(db), "--config", str(cfg_path), "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert any(r["image_version"] == "iv1" for r in data)


def test_image_promote_requires_evidence(tmp_path, age_recipient):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    # argparse exits SystemExit code 2 when --evidence missing.
    import pytest as _pt
    with _pt.raises(SystemExit) as exc:
        run(["image-promote", "iv1", "--db-path", str(db)])
    assert exc.value.code == 2


def test_image_promote_clears_upstream_release_obligation(tmp_path, age_recipient):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.obligations import list_obligations, set_obligation
    from mthydra.controller.state.ru_images import insert_candidate
    conn = connect(db)
    insert_candidate(
        conn, image_version="iv1", upstream_release="v2.1.7",
        upstream_repo="9seconds/mtg", binary_url="x", manifest_url="x",
        binary_sha256="iv1", binary_size_bytes=100, built_at="2026-05-21T00:00:00Z",
    )
    set_obligation(conn, "t4_upstream_release_available::v2.1.7",
                   last_proven_at="2026-05-21T00:00:00Z",
                   proven_by="tracker", next_due_at="2026-05-21T00:00:00Z")
    conn.close()
    rc = run(["image-promote", "iv1", "--evidence", "smoke", "--db-path", str(db)])
    assert rc == 0
    conn = connect(db)
    obs = {o.obligation_id for o in list_obligations(conn)}
    assert "t4_upstream_release_available::v2.1.7" not in obs
    assert "t4_image_promoted" in obs
    conn.close()


def test_image_current_works_on_standby(tmp_path, age_recipient, capsys):
    """image-current is the one read-only command callable on standby."""
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    run(["init", "--role", "standby", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    capsys.readouterr()
    rc = run(["image-current", "--db-path", str(db), "--config", str(cfg_path)])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert "none" in out.lower() or "no" in out.lower()


def test_image_retire_promoted_warns(tmp_path, age_recipient, capsys):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_images import insert_candidate, promote
    conn = connect(db)
    insert_candidate(
        conn, image_version="iv1", upstream_release="v2.1.7",
        upstream_repo="9seconds/mtg", binary_url="x", manifest_url="x",
        binary_sha256="iv1", binary_size_bytes=100,
        built_at="2026-05-21T00:00:00Z",
    )
    promote(conn, "iv1", at="2026-05-21T01:00:00Z", evidence="x")
    conn.close()
    capsys.readouterr()
    rc = run(["image-retire", "iv1", "--reason", "regression",
              "--db-path", str(db)])
    assert rc == 0
    out = capsys.readouterr().out + capsys.readouterr().err
    # Warning text mentions the no-default state.
    assert "no" in out.lower() or "promote" in out.lower() or "default" in out.lower()


def test_upstream_check_invokes_tracker(tmp_path, age_recipient, capsys, monkeypatch):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])

    called = {"latest": None}
    class _StubTracker:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
        def run_once(self):
            called["latest"] = "v2.1.7"
            return "v2.1.7"
    monkeypatch.setattr("mthydra.controller.cli.UpstreamReleaseTracker", _StubTracker)

    rc = run(["upstream-check", "--db-path", str(db), "--config", str(cfg_path)])
    assert rc == 0
    assert called["latest"] == "v2.1.7"
    assert "v2.1.7" in capsys.readouterr().out
```

- [ ] **Step 2: Run tests to verify failure**

```bash
pytest tests/unit/controller/test_cli.py -k 'image_ or upstream_check' -v
```

- [ ] **Step 3: Add subparsers in `build_parser()` in `src/mthydra/controller/cli.py`** (in the spec-D block):

```python
    # ----- spec D subcommands -----
    ib = sub.add_parser("image-build",
                         help="download upstream release, verify checksum, upload to B2, register candidate")
    ib.add_argument("--release", required=True)
    ib.add_argument("--asset", default=None,
                     help="override asset filename (defaults to cfg.image.upstream_release_asset)")
    ib.add_argument("--notes", default=None)
    ib.add_argument("--db-path", default=DEFAULT_DB)
    ib.add_argument("--config", default="/etc/mthydra/controller.toml")

    il = sub.add_parser("image-list", help="list ru_images catalog")
    il.add_argument("--state", choices=["candidate", "promoted", "retired"], default=None)
    il.add_argument("--db-path", default=DEFAULT_DB)
    il.add_argument("--config", default="/etc/mthydra/controller.toml")
    il.add_argument("--json", action="store_true")

    ip = sub.add_parser("image-promote",
                         help="atomic candidate -> promoted; prior promoted -> retired")
    ip.add_argument("image_version")
    ip.add_argument("--evidence", required=True,
                     help="evidence text (placeholder for D2 validation gate)")
    ip.add_argument("--db-path", default=DEFAULT_DB)

    ir = sub.add_parser("image-retire", help="mark a ru_images row as retired")
    ir.add_argument("image_version")
    ir.add_argument("--reason", required=True)
    ir.add_argument("--db-path", default=DEFAULT_DB)

    ic = sub.add_parser("image-current",
                         help="print the currently-promoted image_version (read-only)")
    ic.add_argument("--db-path", default=DEFAULT_DB)
    ic.add_argument("--config", default="/etc/mthydra/controller.toml")
    ic.add_argument("--json", action="store_true")

    uc = sub.add_parser("upstream-check",
                         help="force an immediate UpstreamReleaseTracker poll")
    uc.add_argument("--db-path", default=DEFAULT_DB)
    uc.add_argument("--config", default="/etc/mthydra/controller.toml")
```

- [ ] **Step 4: Add dispatch lines in `run()`**

```python
    if args.cmd == "image-build":
        return _cmd_image_build(args)
    if args.cmd == "image-list":
        return _cmd_image_list(args)
    if args.cmd == "image-promote":
        return _cmd_image_promote(args)
    if args.cmd == "image-retire":
        return _cmd_image_retire(args)
    if args.cmd == "image-current":
        return _cmd_image_current(args)
    if args.cmd == "upstream-check":
        return _cmd_upstream_check(args)
```

- [ ] **Step 5: Add handlers at the bottom of `cli.py`**

Also add a top-level import (next to existing config/state imports) so monkeypatch in tests can override `build_image` and `UpstreamReleaseTracker` at module scope:

```python
from mthydra.controller.image.builder import BuildError, build_image
from mthydra.controller.image.upstream_tracker import UpstreamReleaseTracker
```

Handlers:

```python
def _cmd_image_build(args) -> int:
    from pathlib import Path

    from mthydra.controller.backup.s3_dest import S3Destination
    from mthydra.controller.config import ConfigError, load_config
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.tokens import get_provider_credential

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"image-build: config error: {e}", file=sys.stderr)
        return 2

    conn = connect(args.db_path)
    try:
        rc = _require_active_role(conn, "image-build")
        if rc is not None:
            return rc
        try:
            secret = get_provider_credential(conn, "b2")
        except KeyError:
            print("image-build: b2 provider credential not in DB", file=sys.stderr)
            return 7
        dest = _build_destination(cfg, secret, mode="production",
                                   bucket_override=args.bucket_override)
        asset = args.asset or cfg.image.upstream_release_asset
        try:
            iv = build_image(
                conn=conn,
                b2_destination=dest,
                upstream_repo=cfg.image.upstream_repo,
                upstream_release=args.release,
                asset_filename=asset,
                github_api_url=cfg.image.github_api_url,
                tmp_dir=Path(cfg.image.build_tmp_dir),
                now=_now(),
            )
        except BuildError as e:
            msg = str(e).lower()
            if "sha256 mismatch" in msg:
                code = 3
            elif "github" in msg or "release" in msg or "asset" in msg or "checksum" in msg:
                code = 4
            elif "b2" in msg:
                code = 5
            else:
                code = 2
            print(f"image-build: {e}", file=sys.stderr)
            return code
        print(f"image-build: candidate {iv} registered (release={args.release})")
        return 0
    finally:
        conn.close()


def _cmd_image_list(args) -> int:
    import json as _json
    from dataclasses import asdict

    from mthydra.controller.backup.s3_dest import S3Destination
    from mthydra.controller.config import ConfigError, load_config
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_images import list_images
    from mthydra.controller.state.tokens import get_provider_credential

    # b2_present is computed via head_image; if B2 creds missing or
    # head fails, fall back to None (don't block listing).
    b2 = None
    try:
        cfg = load_config(args.config)
        conn = connect(args.db_path)
        try:
            secret = get_provider_credential(conn, "b2")
            b2 = _build_destination(cfg, secret, mode="production",
                                     bucket_override=args.bucket_override)
        finally:
            conn.close()
    except (ConfigError, KeyError, FileNotFoundError):
        pass

    conn = connect(args.db_path)
    try:
        images = list_images(conn, state=args.state)
        rows = []
        for im in images:
            d = asdict(im)
            if b2 is not None:
                try:
                    d["b2_present"] = b2.head_image(image_version=im.image_version) is not None
                except Exception:
                    d["b2_present"] = None
            else:
                d["b2_present"] = None
            rows.append(d)
        if args.json:
            print(_json.dumps(rows, indent=2))
        else:
            print(f"{'state':10} {'image_version':16} {'upstream_release':20} built_at")
            for r in rows:
                print(f"{r['state']:10} {r['image_version'][:16]:16} "
                      f"{r['upstream_release']:20} {r['built_at']}")
        return 0
    finally:
        conn.close()


def _cmd_image_promote(args) -> int:
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_images import promote

    conn = connect(args.db_path)
    try:
        rc = _require_active_role(conn, "image-promote")
        if rc is not None:
            return rc
        try:
            promote(conn, args.image_version, at=_now(), evidence=args.evidence)
        except (ValueError, LookupError) as e:
            print(f"image-promote: {e}", file=sys.stderr)
            return 2
        print(f"image-promote: {args.image_version} -> promoted")
        return 0
    finally:
        conn.close()


def _cmd_image_retire(args) -> int:
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_images import current_promoted, get_image, retire

    conn = connect(args.db_path)
    try:
        rc = _require_active_role(conn, "image-retire")
        if rc is not None:
            return rc
        try:
            target = get_image(conn, args.image_version)
        except LookupError as e:
            print(f"image-retire: {e}", file=sys.stderr)
            return 2
        was_promoted = target.state == "promoted"
        try:
            retire(conn, args.image_version, at=_now(), reason=args.reason)
        except ValueError as e:
            print(f"image-retire: {e}", file=sys.stderr)
            return 2
        msg = f"image-retire: {args.image_version} -> retired"
        if was_promoted and current_promoted(conn) is None:
            msg += "  (WARNING: no promoted image; fleet has no default — promote a candidate)"
        print(msg)
        return 0
    finally:
        conn.close()


def _cmd_image_current(args) -> int:
    """Read-only; works on both active and standby."""
    import json as _json
    from dataclasses import asdict

    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_images import current_promoted

    conn = connect(args.db_path)
    try:
        n = current_promoted(conn)
        if args.json:
            print(_json.dumps(asdict(n) if n is not None else None, indent=2))
        else:
            if n is None:
                print("image-current: none promoted")
            else:
                print(f"image-current: {n.image_version} "
                      f"(release={n.upstream_release}, promoted_at={n.promoted_at})")
        return 0
    finally:
        conn.close()


def _cmd_upstream_check(args) -> int:
    from mthydra.controller.config import ConfigError, load_config

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"upstream-check: config error: {e}", file=sys.stderr)
        return 2

    tracker = UpstreamReleaseTracker(
        db_path=args.db_path,
        upstream_repo=cfg.image.upstream_repo,
        github_api_url=cfg.image.github_api_url,
        poll_interval_seconds=cfg.image.upstream_check_interval_seconds,
        mode="offline",   # one-shot; don't arm the scheduler
    )
    latest = tracker.run_once()
    if latest is None:
        print("upstream-check: GitHub poll failed (see logs)", file=sys.stderr)
        return 4
    print(f"upstream-check: latest upstream tag = {latest}")
    return 0
```

- [ ] **Step 6: Extend bootstrap obligations**

In `if args.cmd == "init":` the active-role obligation dict, append:

```python
                    "t4_image_promoted":  30 * 24,
```

(The `t4_upstream_check` obligation is already seeded by spec A.)

- [ ] **Step 7: Run all CLI tests + full unit suite**

```bash
pytest tests/unit/controller/test_cli.py -k 'image_ or upstream_check' -v
pytest tests/unit -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/mthydra/controller/cli.py tests/unit/controller/test_cli.py
git commit -m "cli(D): six subcommands — image-{build,list,promote,retire,current}, upstream-check"
```

---

## Phase 9 — Serve integration

### Task 9: Arm `UpstreamReleaseTracker` on the active serve loop

**Files:**
- Modify: `src/mthydra/controller/cli.py`
- Modify: `tests/unit/controller/test_cli.py`

- [ ] **Step 1: Write failing test** — append:

```python
def test_serve_arms_upstream_tracker(tmp_path, age_recipient, monkeypatch):
    """Active serve constructs and arms an UpstreamReleaseTracker alongside the
    cover-pool sweeps + heartbeat poller."""
    import threading as _t
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    recipient_file = tmp_path / "age-recipient.txt"
    recipient_file.write_text(age_recipient + "\n")
    monkeypatch.setattr("mthydra.controller.cli.DEFAULT_RECIPIENT_FILE",
                         str(recipient_file))

    armed = {"tracker": 0}
    class _StubTracker:
        def __init__(self, **kwargs): pass
        def arm(self): armed["tracker"] += 1
        def disarm(self): pass
        def run_once(self): return None
    monkeypatch.setattr("mthydra.controller.cli.UpstreamReleaseTracker", _StubTracker)

    def _fast_wait(self, timeout=None):
        self.set()
        return True
    monkeypatch.setattr(_t.Event, "wait", _fast_wait)

    # NOTE: offline mode skips arm() calls; use production mode so arm is invoked.
    rc = run([
        "serve",
        "--db-path", str(db),
        "--config", str(cfg_path),
    ])
    assert rc == 0
    assert armed["tracker"] == 1
```

- [ ] **Step 2: Modify `_cmd_serve` in `src/mthydra/controller/cli.py`** to construct + arm the tracker alongside the existing components

After the `StandbyHeartbeatPoller` construction:

```python
    tracker = UpstreamReleaseTracker(
        db_path=args.db_path,
        upstream_repo=cfg.image.upstream_repo,
        github_api_url=cfg.image.github_api_url,
        poll_interval_seconds=cfg.image.upstream_check_interval_seconds,
        mode=mode,
    )
```

In the `if args.mode != "offline":` arm block, add:

```python
        tracker.arm()
```

In the `finally:` disarm block, add:

```python
        tracker.disarm()
```

- [ ] **Step 3: Run tests + full suite**

```bash
pytest tests/unit/controller/test_cli.py -k serve -v
pytest tests/unit -q
```

- [ ] **Step 4: Commit**

```bash
git add src/mthydra/controller/cli.py tests/unit/controller/test_cli.py
git commit -m "serve(D): arm UpstreamReleaseTracker on the active serve loop"
```

---

## Phase 10 — Integration

### Task 10: Image lifecycle integration test

**Files:**
- Create: `tests/integration/test_image_lifecycle.py`

- [ ] **Step 1: Write the integration test**

```python
"""Spec D — end-to-end image lifecycle.

upstream-check (no rows) -> image-build (mocked HTTP + mocked B2) ->
image-list (candidate) -> image-promote -> image-current ->
image-retire (different candidate) -> image-list (mixed states).
"""
import hashlib
import json
from unittest.mock import MagicMock

import pytest

from mthydra.controller.bootstrap import init_state
from mthydra.controller.image.builder import build_image
from mthydra.controller.image.upstream_tracker import UpstreamReleaseTracker
from mthydra.controller.state.audit import recent_events
from mthydra.controller.state.db import connect
from mthydra.controller.state.obligations import list_obligations
from mthydra.controller.state.ru_images import (
    current_promoted, list_images, promote, retire,
)


def _mock_http(tag, asset_bytes):
    sha = hashlib.sha256(asset_bytes).hexdigest()
    checksum_text = f"{sha}  mtg-linux-amd64\n"
    release_latest_json = {"tag_name": tag}
    release_tag_json = {
        "tag_name": tag,
        "assets": [
            {"name": "mtg-linux-amd64", "browser_download_url": f"https://example/{tag}/mtg-linux-amd64"},
            {"name": "SHA256SUMS", "browser_download_url": f"https://example/{tag}/SHA256SUMS"},
        ],
    }
    def _get(url):
        resp = MagicMock()
        resp.status = 200
        if url.endswith("/releases/latest"):
            resp.read.return_value = json.dumps(release_latest_json).encode()
        elif "/releases/tags/" in url:
            resp.read.return_value = json.dumps(release_tag_json).encode()
        elif url.endswith("mtg-linux-amd64"):
            resp.read.return_value = asset_bytes
        elif url.endswith("SHA256SUMS"):
            resp.read.return_value = checksum_text.encode()
        else:
            resp.status = 404
            resp.read.return_value = b""
        return resp
    return _get, sha


@pytest.fixture
def recipient_fixture(tmp_path):
    import shutil
    import subprocess
    if shutil.which("age-keygen") is None:
        pytest.skip("age-keygen not installed")
    keyfile = tmp_path / "identity"
    subprocess.run(["age-keygen", "-o", str(keyfile)], capture_output=True, check=True)
    for line in keyfile.read_text().splitlines():
        if line.startswith("# public key: "):
            return line.removeprefix("# public key: ").strip()
    raise RuntimeError("no public key line")


def test_image_lifecycle_end_to_end(tmp_path, recipient_fixture):
    db = tmp_path / "state.sqlite"
    init_state(
        db_path=db,
        age_recipient=recipient_fixture,
        provider_credentials={"b2": "id:secret"},
        obligation_timer_hours={"backup_restore_dryrun": 720},
        now="2026-05-21T00:00:00Z",
        role="active",
    )

    asset_v217 = b"binary-v217" * 100
    asset_v218 = b"binary-v218" * 100
    http_217, sha_217 = _mock_http("v2.1.7", asset_v217)
    http_218, sha_218 = _mock_http("v2.1.8", asset_v218)

    # 1. Upstream check before any builds: anti-obligation appears.
    tracker = UpstreamReleaseTracker(
        db_path=db, upstream_repo="9seconds/mtg",
        github_api_url="https://api.github.com",
        poll_interval_seconds=3600, mode="offline",
        clock=lambda: "2026-05-21T00:30:00Z",
        http_client=http_217,
    )
    latest = tracker.run_once()
    assert latest == "v2.1.7"
    conn = connect(db)
    obs = {o.obligation_id for o in list_obligations(conn)}
    assert "t4_upstream_release_available::v2.1.7" in obs
    conn.close()

    # 2. Build v2.1.7 (mock B2).
    b2 = MagicMock()
    conn = connect(db)
    iv_217 = build_image(
        conn=conn, b2_destination=b2,
        upstream_repo="9seconds/mtg",
        upstream_release="v2.1.7",
        asset_filename="mtg-linux-amd64",
        github_api_url="https://api.github.com",
        tmp_dir=tmp_path,
        now="2026-05-21T01:00:00Z",
        http_client=http_217,
    )
    assert iv_217 == sha_217
    conn.close()

    # 3. Promote it.
    conn = connect(db)
    promote(conn, iv_217, at="2026-05-21T01:30:00Z", evidence="manual smoke")
    conn.close()

    # 4. Anti-obligation cleared; image is current.
    conn = connect(db)
    obs = {o.obligation_id for o in list_obligations(conn)}
    assert "t4_upstream_release_available::v2.1.7" not in obs
    assert "t4_image_promoted" in obs
    n = current_promoted(conn)
    assert n is not None
    assert n.image_version == iv_217
    conn.close()

    # 5. Build v2.1.8 as a candidate (do NOT promote yet).
    conn = connect(db)
    iv_218 = build_image(
        conn=conn, b2_destination=b2,
        upstream_repo="9seconds/mtg",
        upstream_release="v2.1.8",
        asset_filename="mtg-linux-amd64",
        github_api_url="https://api.github.com",
        tmp_dir=tmp_path,
        now="2026-05-21T02:00:00Z",
        http_client=http_218,
    )
    conn.close()

    # 6. Retire the unpromoted candidate.
    conn = connect(db)
    retire(conn, iv_218, at="2026-05-21T02:30:00Z", reason="superseded")
    images = list_images(conn)
    by_state = {im.state for im in images}
    assert by_state == {"promoted", "retired"}
    conn.close()

    # 7. Audit log shows the full sequence.
    conn = connect(db)
    actions = [e.action for e in recent_events(conn, limit=50)]
    assert "image_built" in actions
    assert "image_promoted" in actions
    assert "image_retired" in actions
    conn.close()
```

- [ ] **Step 2: Run the integration test**

```bash
cd /home/asharov/RedHat/Dev/mthydra
pytest tests/integration/test_image_lifecycle.py -v
```

- [ ] **Step 3: Run all integration tests** for regression

```bash
pytest tests/integration -q --ignore=tests/integration/test_gap_monitor.py
```

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_image_lifecycle.py
git commit -m "test(D): end-to-end image lifecycle — upstream-check -> build -> promote -> retire"
```

---

## Phase 11 — Final verification

### Task 11: Full pytest + coverage + smoke

- [ ] **Step 1: Full test suite**

```bash
cd /home/asharov/RedHat/Dev/mthydra
pytest -q --ignore=tests/integration/test_gap_monitor.py
```

Expected: green; ~25 new tests on top of the spec-F baseline (316).

- [ ] **Step 2: Coverage on new modules**

```bash
pytest --cov=mthydra.controller.state.ru_images \
       --cov=mthydra.controller.image.builder \
       --cov=mthydra.controller.image.upstream_tracker \
       --cov-report=term tests/ \
       --ignore=tests/integration/test_gap_monitor.py
```

Expected: ≥ 90% on `ru_images`, ≥ 85% on `builder` + `upstream_tracker` (the APScheduler `arm`/`disarm` paths are uncovered by design, matching the spec-C/F scheduler precedent).

- [ ] **Step 3: CLI end-to-end smoke**

```bash
TMP=$(mktemp -d)
age-keygen -o "$TMP/id" 2>/dev/null
PUB=$(grep '# public key:' "$TMP/id" | awk '{print $4}')

.venv/bin/mthydra-controller init \
  --db-path "$TMP/state.sqlite" \
  --age-recipient "$PUB" \
  --provider-credential "b2=id:secret"

cp packaging/etc/mthydra/controller.toml.example "$TMP/controller.toml"

# image-current with no images: prints "none"
.venv/bin/mthydra-controller image-current \
  --db-path "$TMP/state.sqlite" --config "$TMP/controller.toml"

# image-list (empty)
.venv/bin/mthydra-controller image-list \
  --db-path "$TMP/state.sqlite" --config "$TMP/controller.toml"

# upstream-check will likely fail without real network — accept exit 4.
.venv/bin/mthydra-controller upstream-check \
  --db-path "$TMP/state.sqlite" --config "$TMP/controller.toml" || true

rm -rf "$TMP"
echo "smoke ok"
```

Expected: `image-current` exits 0 with "none promoted"; `image-list` exits 0 with an empty table; `upstream-check` may exit 4 (no network); each command's pre-conditions are honored.

- [ ] **Step 4: Confirm 10 spec-D commits landed**

```bash
git log --oneline | head -12
```

You should see ten `(D):` commits on top of the spec-F tip.

---

## Done criteria

- All 11 task checkboxes ticked.
- `pytest -q` passes cleanly.
- ≥ 90% coverage on `mthydra.controller.state.ru_images`, ≥ 85% on `builder` + `upstream_tracker`.
- All 6 new CLI subcommands work end-to-end via the smoke test.
- Startup invariants #24–#25 catch singleton-promoted and timestamp-consistency violations.
- The integration test demonstrates: upstream-check sets the anti-obligation; image-build clears nothing (only promote does); image-promote clears the matching anti-obligation; retire transitions are audited.
- `t4_upstream_check` is stamped on every successful tracker tick; never stamped on failed checks.
