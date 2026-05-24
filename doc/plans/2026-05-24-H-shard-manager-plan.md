# Spec H — Shard Manager — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement spec H — `doc/specs/2026-05-24-H-shard-manager.md`: schema v6 → v7 with `shards.target_size` column + two `ru_boxes` triggers; a thin `state/shards.py` repository (lifecycle, reshuffle, audit); `state/users_shards.py` extensions (size-capped assignment, unassigned roster, batch fold-in); four startup invariants (#33–#36); the `ShardReshuffleWheel` APScheduler ticker; eight new CLI subcommands; a synchronous compromise-reshuffle hook wired into `ru-box-terminate --reason=compromise`; full unit + property + integration coverage.

**Architecture:** Spec H is pure controller-side; no RU/EU runtime impact. The state machine lives in SQLite; reshuffle is one transaction per shard. The scheduler mirrors spec C/F/E patterns (`BackgroundScheduler` + `ThreadPoolExecutor`, offline-mode short-circuit). Shard disjointness is enforced structurally (two triggers on `ru_boxes`); helper APIs enforce size caps. The compromise hook lives inline in `_cmd_ru_box_terminate` — no new module, no message bus.

**Tech stack:** Python 3.12 stdlib + APScheduler. No new runtime dependencies. `uuid.uuid7()` (Python 3.13?) — use `uuid.uuid4()` if 3.13 not available; the spec's "fresh UUID-7" wording is descriptive, not version-locked.

**Design decisions:** See spec §2 (H-D1 through H-D9).

---

## File Structure (locked before tasks)

**Schema + state:**
- Modify: `src/mthydra/controller/state/schema.py` — `SCHEMA_VERSION = 7`, new `migrate_v6_to_v7`, two triggers + `target_size` column, fresh-install statements updated.
- Modify: `src/mthydra/controller/state/users_shards.py` — split shards-only concerns out (see below); add `assign_user_to_shard`, `unassigned_users`, `reshuffle_unassigned`.
- Create: `src/mthydra/controller/state/shards.py` — `Shard` dataclass, `ShardHealth` dataclass, `create_shard`, `retire_shard`, `list_active`, `get_shard`, `list_shard_boxes`, `assign_box_to_shard`, `reshuffle`, `health`.

**Config:**
- Modify: `src/mthydra/controller/config.py` — add `ShardManagerConfig` dataclass + `_load_shard_manager(raw)` + parse `[shard_manager]` section.
- Modify: `packaging/etc/mthydra/controller.toml.example` — add `[shard_manager]` section.

**Invariants:**
- Modify: `src/mthydra/controller/state/invariants.py` — extend `check_all()` with #33–#36.

**Scheduler:**
- Create: `src/mthydra/controller/shard_manager/__init__.py` — empty.
- Create: `src/mthydra/controller/shard_manager/picker.py` — pure `pick_new_rosters(current_members, unassigned, target_size, rng)` helper.
- Create: `src/mthydra/controller/shard_manager/wheel.py` — `ShardReshuffleWheel` (APScheduler-driven).

**Compromise hook:**
- Modify: `src/mthydra/controller/cli.py` — inside `_cmd_ru_box_terminate`, after the terminate commits, branch on `args.reason == 'compromise'` and call `shards.reshuffle(...)`.

**CLI (modified):**
- Modify: `src/mthydra/controller/cli.py` — eight new subcommands: `user-add`, `user-list`, `shard-create`, `shard-list`, `shard-show`, `shard-assign-box`, `shard-reshuffle`, `shard-stats`. Bootstrap obligation list extended.

**Bootstrap:**
- Modify: `src/mthydra/controller/bootstrap.py` — seed five new obligations (see spec §7.3).

**Tests (created):**
- `tests/unit/controller/state/test_shards.py`
- `tests/unit/controller/state/test_shards_triggers.py`
- `tests/unit/controller/shard_manager/__init__.py` — empty.
- `tests/unit/controller/shard_manager/test_picker.py`
- `tests/unit/controller/shard_manager/test_wheel.py`
- `tests/property/test_shard_invariants.py`
- `tests/integration/test_shard_lifecycle.py`

**Tests (modified):**
- `tests/unit/controller/state/test_users_shards.py` — assignment cap, unassigned roster, fold-in.
- `tests/unit/controller/state/test_invariants.py` — #33–#36.
- `tests/unit/controller/state/test_schema.py` — v6→v7 migration assertions.
- `tests/unit/controller/test_cli.py` — eight new subcommands + compromise-reshuffle hook.
- `tests/unit/controller/test_bootstrap.py` — five new obligations seeded.

---

## Phase 1 — Schema v7

### Task 1: Schema v6 → v7 migration

**Files:**
- Modify: `src/mthydra/controller/state/schema.py`
- Modify: `tests/unit/controller/state/test_schema.py`

- [ ] **Step 1: Append failing tests**

```python
def test_schema_version_is_7(tmp_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import SCHEMA_VERSION, apply_schema
    assert SCHEMA_VERSION == 7
    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    row = conn.execute("SELECT version FROM schema_version WHERE rowid=1").fetchone()
    assert row[0] == 7

def test_shards_target_size_column_present(tmp_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    conn = connect(tmp_path / "state.sqlite")
    apply_schema(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(shards)").fetchall()]
    assert "target_size" in cols

def test_ru_boxes_cross_shard_trigger_blocks(tmp_path):
    import sqlite3
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    conn = connect(tmp_path / "state.sqlite")
    apply_schema(conn)
    conn.execute(
        "INSERT INTO shards (shard_id, members_json, target_size, last_reshuffled_at, created_at) "
        "VALUES ('s1', '[]', 2, '2026-05-24T00:00:00Z', '2026-05-24T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO shards (shard_id, members_json, target_size, last_reshuffled_at, created_at) "
        "VALUES ('s2', '[]', 2, '2026-05-24T00:00:00Z', '2026-05-24T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO ru_boxes (box_id, provider, region, sni, shard_id, state, image_version, created_at) "
        "VALUES ('b1', 'p', 'r', 'sni1.example', 's1', 'live', 'v1', '2026-05-24T00:00:00Z')"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE ru_boxes SET shard_id='s2' WHERE box_id='b1'")
        conn.commit()

def test_ru_boxes_terminate_keeps_shard_trigger(tmp_path):
    import sqlite3
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    conn = connect(tmp_path / "state.sqlite")
    apply_schema(conn)
    conn.execute(
        "INSERT INTO shards (shard_id, members_json, target_size, last_reshuffled_at, created_at) "
        "VALUES ('s1', '[]', 2, '2026-05-24T00:00:00Z', '2026-05-24T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO ru_boxes (box_id, provider, region, sni, shard_id, state, image_version, created_at) "
        "VALUES ('b1', 'p', 'r', 'sni1.example', 's1', 'live', 'v1', '2026-05-24T00:00:00Z')"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE ru_boxes SET state='terminated', shard_id=NULL WHERE box_id='b1'")
        conn.commit()
```

- [ ] **Step 2: Implement migration**

In `src/mthydra/controller/state/schema.py`:

```python
SCHEMA_VERSION = 7

_TRIGGER_RU_BOXES_NO_CROSS_SHARD_REASSIGN = """
    CREATE TRIGGER IF NOT EXISTS ru_boxes_no_cross_shard_reassign
    BEFORE UPDATE OF shard_id ON ru_boxes
    WHEN OLD.shard_id IS NOT NULL
     AND OLD.shard_id IS NOT NEW.shard_id
     AND OLD.state != 'provisioning'
    BEGIN
      SELECT RAISE(ABORT, 'shard-manager: live/terminated boxes cannot change shard_id');
    END
    """

_TRIGGER_RU_BOXES_TERMINATED_KEEPS_SHARD = """
    CREATE TRIGGER IF NOT EXISTS ru_boxes_terminated_keeps_shard
    BEFORE UPDATE OF state ON ru_boxes
    WHEN NEW.state = 'terminated'
     AND NEW.shard_id IS NULL
     AND OLD.shard_id IS NOT NULL
    BEGIN
      SELECT RAISE(ABORT, 'shard-manager: terminating a box does not clear shard_id (history preservation)');
    END
    """
```

Add both trigger statements to `_STATEMENTS` (after the existing trigger constants). Update the `shards` `CREATE TABLE IF NOT EXISTS` to include `target_size INTEGER`.

Add `migrate_v6_to_v7`:

```python
def migrate_v6_to_v7(conn: sqlite3.Connection) -> None:
    """Idempotent v6 → v7 migration: add shards.target_size + two ru_boxes triggers."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(shards)").fetchall()]
    if "target_size" not in cols:
        conn.execute("ALTER TABLE shards ADD COLUMN target_size INTEGER")
    conn.execute(_TRIGGER_RU_BOXES_NO_CROSS_SHARD_REASSIGN)
    conn.execute(_TRIGGER_RU_BOXES_TERMINATED_KEEPS_SHARD)
    conn.execute(
        "UPDATE schema_version SET version=?, applied_at=? WHERE rowid=1",
        (7, _now()),
    )
    conn.commit()
```

Wire the migration into `apply_schema`:

```python
if current < 7:
    migrate_v6_to_v7(conn)
```

- [ ] **Step 3: Run tests + commit**

```bash
pytest tests/unit/controller/state/test_schema.py -v
git add src/mthydra/controller/state/schema.py tests/unit/controller/state/test_schema.py
git commit -m "schema(H): v6 -> v7 — shards.target_size + ru_boxes disjointness triggers"
git push origin main
```

---

## Phase 2 — Shard repository

### Task 2: `state/shards.py` module

**Files:**
- Create: `src/mthydra/controller/state/shards.py`
- Create: `tests/unit/controller/state/test_shards.py`

- [ ] **Step 1: Write failing tests**

Cover the API surface from spec §5: `create_shard`, `retire_shard`, `list_active`, `get_shard`, `list_shard_boxes`, `assign_box_to_shard`, `reshuffle`, `health`. Each helper writes an `audit_log` row — assert on `SELECT * FROM audit_log` after each call. `reshuffle` is the integration of the lot: assert it retires the old shard, creates a new one with the redistributed members, updates `users.current_shard_id`, and writes exactly the expected audit rows.

- [ ] **Step 2: Implement module**

Pure SQL helpers, one transaction per public function. `reshuffle` takes the picker output (next task) as an injected list of new rosters; this keeps the I/O-free picker testable separately. Audit row format mirrors spec C's `audit_log` columns (timestamp, actor='shard_manager', action, target=shard_id, prev_state=JSON, new_state=JSON, reason).

- [ ] **Step 3: Run tests + commit**

```bash
pytest tests/unit/controller/state/test_shards.py -v
git add src/mthydra/controller/state/shards.py tests/unit/controller/state/test_shards.py
git commit -m "shards(H): repository — lifecycle, reshuffle, audit"
git push origin main
```

### Task 3: Extend `state/users_shards.py`

**Files:**
- Modify: `src/mthydra/controller/state/users_shards.py`
- Modify: `tests/unit/controller/state/test_users_shards.py`

- [ ] **Step 1: Write failing tests**

```python
def test_assign_user_to_shard_refuses_above_max(tmp_db):
    # Set up a shard with members=[u1, u2, u3] and call with max_size=3 + new user u4
    # Expect ValueError.

def test_unassigned_users_lists_null_shard(tmp_db):
    # Add 3 users; assign 1 to a shard; expect the other 2 in unassigned_users.

def test_reshuffle_unassigned_folds_into_chunks(tmp_db):
    # Add 5 unassigned users; target_size=2; expect 3 new shards (2+2+1).
```

- [ ] **Step 2: Implement**

```python
def assign_user_to_shard(
    conn, user_id: str, shard_id: str, *, at: str, max_size: int,
) -> None:
    row = conn.execute(
        "SELECT members_json FROM shards WHERE shard_id=? AND retired_at IS NULL",
        (shard_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"no active shard {shard_id!r}")
    members = json.loads(row[0])
    if len(members) >= max_size:
        raise ValueError(f"shard {shard_id} at max_size={max_size}")
    if user_id in members:
        return  # idempotent
    members.append(user_id)
    conn.execute(
        "UPDATE shards SET members_json=? WHERE shard_id=?",
        (json.dumps(members), shard_id),
    )
    conn.execute("UPDATE users SET current_shard_id=? WHERE user_id=?", (shard_id, user_id))
    _audit(conn, action="assign_user_to_shard", target=shard_id, ...)
    conn.commit()

def unassigned_users(conn) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT user_id FROM users WHERE current_shard_id IS NULL ORDER BY user_id"
    ).fetchall()]

def reshuffle_unassigned(
    conn, *, now: str, target_size: int, shard_id_factory,
) -> list[str]:
    unassigned = unassigned_users(conn)
    if not unassigned:
        return []
    new_shard_ids: list[str] = []
    for i in range(0, len(unassigned), target_size):
        chunk = unassigned[i : i + target_size]
        sid = shard_id_factory()
        from mthydra.controller.state.shards import create_shard
        create_shard(conn, shard_id=sid, members=chunk, target_size=target_size, at=now)
        for u in chunk:
            conn.execute("UPDATE users SET current_shard_id=? WHERE user_id=?", (sid, u))
        new_shard_ids.append(sid)
    conn.commit()
    return new_shard_ids
```

- [ ] **Step 3: Run + commit**

```bash
pytest tests/unit/controller/state/test_users_shards.py -v
git add src/mthydra/controller/state/users_shards.py tests/unit/controller/state/test_users_shards.py
git commit -m "users_shards(H): size-capped assign + unassigned roster + fold-in"
git push origin main
```

### Task 4: Trigger tests

**Files:**
- Create: `tests/unit/controller/state/test_shards_triggers.py`

Test the two triggers from Phase 1 in isolation (the schema-test file already smoke-tested them; this file does the systematic catalogue): provisioning→live then cross-shard UPDATE refuses; provisioning→provisioning cross-shard UPDATE refuses (the box is non-provisioning by `OLD.state` only); terminating with shard_id retained succeeds; terminating with NULL shard_id refuses.

```bash
pytest tests/unit/controller/state/test_shards_triggers.py -v
git add tests/unit/controller/state/test_shards_triggers.py
git commit -m "test(H): ru_boxes shard-disjointness triggers — full catalogue"
git push origin main
```

---

## Phase 3 — Config + bootstrap

### Task 5: `ShardManagerConfig` + TOML

**Files:**
- Modify: `src/mthydra/controller/config.py`
- Modify: `packaging/etc/mthydra/controller.toml.example`
- Modify: `tests/unit/controller/test_config.py`

- [ ] **Step 1: Failing test**

```python
def test_config_loads_shard_manager_section(tmp_path):
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_BASE_TOML + """
[shard_manager]
target_size = 2
max_size = 3
reshuffle_interval_days = 14
reshuffle_sweep_interval = "1h"
""")
    cfg = load_config(cfg_path)
    assert cfg.shard_manager.target_size == 2
    assert cfg.shard_manager.max_size == 3
    assert cfg.shard_manager.reshuffle_interval_days == 14
    assert cfg.shard_manager.reshuffle_sweep_interval_seconds == 3600
```

- [ ] **Step 2: Implement**

Add `ShardManagerConfig`:

```python
@dataclass(frozen=True)
class ShardManagerConfig:
    target_size: int
    max_size: int
    reshuffle_interval_days: int
    reshuffle_sweep_interval_seconds: int
```

Add `_load_shard_manager(raw)` (parse `"1h"` via the same `_parse_interval_seconds` already used by `image`). Add field to `Config` dataclass. Wire into `load_config`.

Update `controller.toml.example` with a `[shard_manager]` block.

- [ ] **Step 3: Run + commit**

```bash
pytest tests/unit/controller/test_config.py -v
git add src/mthydra/controller/config.py packaging/etc/mthydra/controller.toml.example tests/unit/controller/test_config.py
git commit -m "config(H): [shard_manager] section + ShardManagerConfig dataclass"
git push origin main
```

### Task 6: Bootstrap obligations

**Files:**
- Modify: `src/mthydra/controller/bootstrap.py`
- Modify: `tests/unit/controller/test_bootstrap.py`

- [ ] **Step 1: Test seeds new obligations**

```python
def test_bootstrap_seeds_shard_obligations(tmp_path):
    # Run bootstrap; assert these obligation_ids exist:
    expected = {
        "shard_reshuffle_sweep_ran",
        "shard_reshuffle_proven",
        "shard_disjointness_check_proven",
    }
    rows = conn.execute("SELECT obligation_id FROM obligation_clocks").fetchall()
    ids = {r[0] for r in rows}
    assert expected <= ids
```

(`shard_overdue_pending` and `shard_unassigned_pending` are anti-obligations — emitted dynamically, not seeded.)

- [ ] **Step 2: Implement**

Append the three obligation_ids to the bootstrap seeder, mirroring spec C's pattern.

- [ ] **Step 3: Run + commit**

```bash
pytest tests/unit/controller/test_bootstrap.py -v
git add src/mthydra/controller/bootstrap.py tests/unit/controller/test_bootstrap.py
git commit -m "bootstrap(H): seed shard_reshuffle + disjointness obligations"
git push origin main
```

---

## Phase 4 — Invariants #33–#36

### Task 7: Implement four invariants

**Files:**
- Modify: `src/mthydra/controller/state/invariants.py`
- Modify: `tests/unit/controller/state/test_invariants.py`

- [ ] **Step 1: Failing tests**

One test per invariant — set up the violating state via raw SQL (bypassing helpers), call `check_all()`, assert the specific `Invariant` is in the returned list.

- [ ] **Step 2: Implement**

Append four checks to `check_all()`:

```python
# #33: every live box has shard_id
rows = conn.execute(
    "SELECT box_id FROM ru_boxes WHERE state='live' AND shard_id IS NULL"
).fetchall()
if rows:
    violations.append(Invariant(33, f"live box without shard: {[r[0] for r in rows]}"))

# #34: no user references retired shard
rows = conn.execute(
    "SELECT u.user_id FROM users u "
    "JOIN shards s ON s.shard_id = u.current_shard_id "
    "WHERE s.retired_at IS NOT NULL"
).fetchall()
if rows:
    violations.append(Invariant(34, f"user on retired shard: {[r[0] for r in rows]}"))

# #35: no cross-shard user (intersection of members_json across active shards)
active = conn.execute(
    "SELECT shard_id, members_json FROM shards WHERE retired_at IS NULL"
).fetchall()
seen: dict[str, str] = {}
for sid, mj in active:
    for u in json.loads(mj):
        if u in seen and seen[u] != sid:
            violations.append(Invariant(35, f"user {u} in both {seen[u]} and {sid}"))
        seen[u] = sid

# #36: no empty active shard
rows = conn.execute(
    "SELECT shard_id, members_json FROM shards WHERE retired_at IS NULL"
).fetchall()
for sid, mj in rows:
    if not json.loads(mj):
        violations.append(Invariant(36, f"empty active shard: {sid}"))
```

- [ ] **Step 3: Run + commit**

```bash
pytest tests/unit/controller/state/test_invariants.py -v
git add src/mthydra/controller/state/invariants.py tests/unit/controller/state/test_invariants.py
git commit -m "invariants(H): #33-#36 — live-has-shard + retired-ref + disjointness + non-empty"
git push origin main
```

---

## Phase 5 — Reshuffle scheduler

### Task 8: Pure picker

**Files:**
- Create: `src/mthydra/controller/shard_manager/__init__.py` (empty)
- Create: `src/mthydra/controller/shard_manager/picker.py`
- Create: `tests/unit/controller/shard_manager/__init__.py` (empty)
- Create: `tests/unit/controller/shard_manager/test_picker.py`

- [ ] **Step 1: Failing tests**

```python
def test_picker_chunks_by_target_size():
    rosters = pick_new_rosters(
        current_members=["a", "b", "c", "d", "e"],
        unassigned=["f"],
        target_size=2,
        rng=random.Random(42),  # seeded for determinism
    )
    flat = [u for r in rosters for u in r]
    assert sorted(flat) == ["a", "b", "c", "d", "e", "f"]
    assert all(len(r) <= 2 for r in rosters)
    assert sum(1 for r in rosters if len(r) == 2) >= 2  # at least two full chunks

def test_picker_anti_correlation():
    # Run 100 times with different seeds; assert pair-co-occurrence is bounded.
    # For pool=6 users, target_size=2, expect each pair to co-occur with frequency ~ 1/5
    # (each user pairs with 5 others; over many shuffles, uniform).
```

- [ ] **Step 2: Implement**

```python
def pick_new_rosters(
    *,
    current_members: list[str],
    unassigned: list[str],
    target_size: int,
    rng: random.Random | None = None,
) -> list[list[str]]:
    rng = rng or random.SystemRandom()
    pool = list(current_members) + list(unassigned)
    rng.shuffle(pool)
    return [pool[i : i + target_size] for i in range(0, len(pool), target_size)]
```

- [ ] **Step 3: Run + commit**

```bash
pytest tests/unit/controller/shard_manager/test_picker.py -v
git add src/mthydra/controller/shard_manager/ tests/unit/controller/shard_manager/
git commit -m "shard_manager(H): pure picker — SystemRandom shuffle + chunk"
git push origin main
```

### Task 9: `ShardReshuffleWheel`

**Files:**
- Create: `src/mthydra/controller/shard_manager/wheel.py`
- Create: `tests/unit/controller/shard_manager/test_wheel.py`

- [ ] **Step 1: Failing tests**

Mirror spec C's `test_cover_pool_scheduler.py`:
- mock clock; advance past `reshuffle_interval_days`; assert the overdue shard was reshuffled
- unassigned users get folded in
- heartbeat obligation `shard_reshuffle_sweep_ran` proven each tick
- `shard_overdue_pending` rows appear before sweep, disappear after
- offline mode: scheduler does not arm

- [ ] **Step 2: Implement**

```python
class ShardReshuffleWheel:
    def __init__(
        self,
        *,
        db_path: Path,
        cfg: ShardManagerConfig,
        now_fn: Callable[[], str],
        shard_id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
        mode: str = "online",
    ) -> None:
        ...

    def tick(self) -> None:
        conn = connect(self._db_path)
        try:
            h = health(conn, now=self._now_fn(),
                      reshuffle_interval_seconds=self._cfg.reshuffle_interval_days * 86400)
            for sid in h.overdue_for_reshuffle:
                shard = get_shard(conn, sid)
                new_rosters = pick_new_rosters(
                    current_members=json.loads(shard.members_json),
                    unassigned=[],
                    target_size=self._cfg.target_size,
                )
                # Reshuffle uses only the first new roster for this shard;
                # leftovers go into a separate create.
                primary = new_rosters[0]
                new_sid = self._shard_id_factory()
                reshuffle(conn, sid, now=self._now_fn(),
                          target_size=self._cfg.target_size,
                          new_shard_id=new_sid,
                          new_members=primary,
                          reason="ttl")
                for leftover in new_rosters[1:]:
                    extra_sid = self._shard_id_factory()
                    create_shard(conn, shard_id=extra_sid,
                                 members=leftover,
                                 target_size=self._cfg.target_size,
                                 at=self._now_fn())
                    for u in leftover:
                        conn.execute(
                            "UPDATE users SET current_shard_id=? WHERE user_id=?",
                            (extra_sid, u),
                        )
                conn.commit()
            reshuffle_unassigned(
                conn,
                now=self._now_fn(),
                target_size=self._cfg.target_size,
                shard_id_factory=self._shard_id_factory,
            )
            prove(conn, "shard_reshuffle_sweep_ran", self._now_fn(),
                  next_due_at_seconds=3600)
        finally:
            conn.close()

    def start(self) -> None:
        if self._mode == "offline":
            return
        from apscheduler.schedulers.background import BackgroundScheduler
        self._sched = BackgroundScheduler(timezone="UTC")
        self._sched.add_job(
            self.tick,
            "interval",
            seconds=self._cfg.reshuffle_sweep_interval_seconds,
        )
        self._sched.start()

    def shutdown(self) -> None:
        if getattr(self, "_sched", None) is not None:
            self._sched.shutdown(wait=False)
```

(Note: the `reshuffle` helper signature gains a `new_members` argument; if it currently does its own picking, refactor to accept the externally-picked list.)

- [ ] **Step 3: Run + commit**

```bash
pytest tests/unit/controller/shard_manager/test_wheel.py -v
git add src/mthydra/controller/shard_manager/wheel.py tests/unit/controller/shard_manager/test_wheel.py
git commit -m "shard_manager(H): reshuffle wheel — APScheduler tick + overdue + unassigned"
git push origin main
```

### Task 10: Wheel armed in `_cmd_serve`

**Files:**
- Modify: `src/mthydra/controller/cli.py`
- Modify: `tests/unit/controller/test_cli.py`

In `_cmd_serve`, alongside the existing `CoverPoolWheel`, `StandbyHeartbeatPoller`, `UpstreamTracker`, `DataExitWheel` arm-and-shutdown blocks, add the `ShardReshuffleWheel`. Active-only (gate on `node_state.role == 'active'`). Test: assert the wheel's `start()` is invoked exactly when role=='active'.

```bash
pytest tests/unit/controller/test_cli.py -v -k 'serve'
git add src/mthydra/controller/cli.py tests/unit/controller/test_cli.py
git commit -m "serve(H): arm ShardReshuffleWheel on active node"
git push origin main
```

---

## Phase 6 — CLI subcommands

### Task 11: Eight new subcommands

**Files:**
- Modify: `src/mthydra/controller/cli.py`
- Modify: `tests/unit/controller/test_cli.py`

Implement, in order, with one commit per group:

**Group A: user management (2 commands)**
- `user-add <user_id> --out-of-band-channel <text> [--display-name <text>]`
- `user-list [--json]`

```bash
git commit -m "cli(H): user-add + user-list"
```

**Group B: shard query (3 commands)**
- `shard-create <shard_id> --members <csv>` — operator bootstrap path
- `shard-list [--include-retired] [--json]`
- `shard-show <shard_id> [--json]`

```bash
git commit -m "cli(H): shard-create/list/show"
```

**Group C: shard operations (3 commands)**
- `shard-assign-box <box_id> --shard <shard_id>`
- `shard-reshuffle <shard_id> [--reason <text>]` — operator manual reshuffle (out-of-band)
- `shard-stats [--json]`

```bash
git commit -m "cli(H): shard-assign-box/reshuffle/stats"
git push origin main
```

Each subcommand: argparse wiring, dispatch to `_cmd_<name>(args)`, structured-output guard (`--json` uses `json.dumps(sort_keys=True)`), exit codes (0 OK, 2 user error, 1 unexpected). Tests assert happy path + the documented error in spec §9.9.

---

## Phase 7 — Compromise-reshuffle hook

### Task 12: Wire reshuffle into `ru-box-terminate --reason=compromise`

**Files:**
- Modify: `src/mthydra/controller/cli.py`
- Modify: `tests/unit/controller/test_cli.py`

- [ ] **Step 1: Failing test**

```python
def test_ru_box_terminate_compromise_triggers_reshuffle(tmp_db, ...):
    # Provision a box, mark live, assign to shard s1 with members [u1, u2].
    # Call ru-box-terminate <box> --reason compromise.
    # Assert: s1 is retired; a new active shard exists containing u1 and u2;
    # audit_log has a 'reshuffle' row with reason='compromise'.
```

Also assert `--reason aged_out` and `--reason manual_rotate` do NOT trigger reshuffle.

- [ ] **Step 2: Implement**

In `_cmd_ru_box_terminate`, after the existing terminate logic commits, before returning:

```python
if args.reason == "compromise":
    # Read the box's shard_id (retained by the H-D2 trigger).
    sid = conn.execute(
        "SELECT shard_id FROM ru_boxes WHERE box_id=?", (args.box_id,)
    ).fetchone()
    if sid and sid[0]:
        from mthydra.controller.state.shards import get_shard, reshuffle
        from mthydra.controller.shard_manager.picker import pick_new_rosters
        import uuid as _uuid
        shard = get_shard(conn, sid[0])
        new_rosters = pick_new_rosters(
            current_members=json.loads(shard.members_json),
            unassigned=[],
            target_size=cfg.shard_manager.target_size,
        )
        primary = new_rosters[0] if new_rosters else []
        new_sid = str(_uuid.uuid4())
        reshuffle(
            conn, sid[0],
            now=_now(),
            target_size=cfg.shard_manager.target_size,
            new_shard_id=new_sid,
            new_members=primary,
            reason="compromise",
        )
        # Leftovers, if any (shouldn't happen with primary == all members in normal sizes)
        for leftover in new_rosters[1:]:
            ...
```

- [ ] **Step 3: Run + commit**

```bash
pytest tests/unit/controller/test_cli.py -v -k 'compromise'
git add src/mthydra/controller/cli.py tests/unit/controller/test_cli.py
git commit -m "cli(H): ru-box-terminate --reason=compromise triggers shard reshuffle"
git push origin main
```

---

## Phase 8 — Property + integration tests

### Task 13: Property test

**Files:**
- Create: `tests/property/test_shard_invariants.py`

Hypothesis strategy: generate sequences of operations (add_user, create_shard, assign_user, assign_box, terminate_box (compromise + benign), reshuffle, sweep_tick) and assert after each step that all four spec H invariants (#33–#36) hold + cross-checks:
- no active shard has > `max_size` members
- no two active shards share a user
- every live box has shard_id set
- reshuffle never re-uses a previous shard_id

```bash
pytest tests/property/test_shard_invariants.py -v
git add tests/property/test_shard_invariants.py
git commit -m "test(H): property — shard invariants hold under random op sequences"
git push origin main
```

### Task 14: Integration test

**Files:**
- Create: `tests/integration/test_shard_lifecycle.py`

End-to-end per spec §9.7:

1. Bootstrap; `user-add` ×6; `shard-create s1 --members u1,u2`, `s2`, `s3`.
2. `provision-seed --shard s1` ×3 (one per shard); `shard-assign-box` is implicit if provision-seed wires it, otherwise explicit.
3. `ru-box-mark-live` ×3.
4. Build the wheel with `mode='offline'`; advance the clock past 14d; call `tick()`; assert all 3 shards retired + 3 new active shards exist + every user has a new `current_shard_id`.
5. `ru-box-terminate <box1> --reason compromise`; assert immediate reshuffle of box1's shard (out of TTL band); other shards untouched.
6. Assert audit log has the expected reshuffle entries.

```bash
pytest tests/integration/test_shard_lifecycle.py -v
git add tests/integration/test_shard_lifecycle.py
git commit -m "test(H): shard lifecycle — provision -> assign -> tick -> compromise"
git push origin main
```

---

## Phase 9 — Final verification

### Task 15: Full suite + coverage + residuals

**Files:**
- Modify: `doc/specs/2026-05-24-H-shard-manager.md` — if any implementation deviated from the spec, append a §11 residual.

- [ ] **Step 1: Full unit suite**

```bash
cd /home/asharov/RedHat/Dev/mthydra
pytest -q --ignore=tests/integration/test_gap_monitor.py
```

Expected: green. New test count: ~450 (E baseline) + ~40 H = ~490 passing.

- [ ] **Step 2: Coverage on new modules**

```bash
pytest --cov=mthydra.controller.shard_manager \
       --cov=mthydra.controller.state.shards \
       --cov-report=term tests/ \
       --ignore=tests/integration/test_gap_monitor.py
```

Expected:
- `mthydra.controller.shard_manager.*` ≥ 90%
- `mthydra.controller.state.shards` ≥ 90%

If below threshold, add targeted tests for uncovered error paths (mirror the spec-E cleanup pattern).

- [ ] **Step 3: Spec residual update if needed**

If any implementation choice deviated from the spec, append a bullet to §11. Otherwise, no change.

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "test(H): cover shard_manager + shards repo to >=90%" || echo "nothing to commit"
git log --oneline | head -20
git push origin main
```

You should see ~15 `(H):` commits on top of the spec-E tip.

---

## Done criteria

- All 15 task checkboxes ticked.
- `pytest -q` passes cleanly.
- ≥ 90% coverage on `mthydra.controller.shard_manager.*` and `mthydra.controller.state.shards`.
- All 8 new CLI subcommands work; `ru-box-terminate --reason=compromise` reshuffles synchronously.
- Triggers `ru_boxes_no_cross_shard_reassign` and `ru_boxes_terminated_keeps_shard` block their respective forbidden transitions.
- Spec invariants #33–#36 catch their respective violations.
- The integration test demonstrates: provision boxes, assign to shards, TTL-tick reshuffles, compromise-terminate triggers immediate reshuffle of the affected shard only.
- Five new bootstrap obligations seeded (three timer-based + two anti-obligations emitted dynamically).
- DB schema bumped to v7.
