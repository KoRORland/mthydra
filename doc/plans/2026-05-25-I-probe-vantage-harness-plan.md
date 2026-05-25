# Spec I — Probe Vantage Harness — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement spec I — `doc/specs/2026-05-25-I-probe-vantage-harness.md`: schema v7 → v8 with `probe_vantages` + `probe_results` (append-only) + `image_profiles` tables and four triggers; three thin repositories (`state/probe_vantages.py`, `state/probe_results.py`, `state/image_profiles.py`); a pure `probe/evaluator.py` implementing §8 Job 2 hard-kill + N-of-M soft-fail rules; `probe/audit_wheel.py` APScheduler sweep emitting per-box `probe_kill_pending` / `probe_coverage_pending` and per-vantage `probe_vantage_rotation_pending` anti-obligations; four startup invariants (#37–#40 with #40 warning-only); nine CLI subcommands; full unit + property + integration coverage.

**Architecture:** Pure controller-side; no RU/EU runtime impact. Vantage state machine lives in SQLite with the same monotonic-burned discipline as `cover_domain_pool`. `probe_results` is append-only (triggers refuse UPDATE/DELETE). Evaluator is a pure function; the wheel mirrors spec C/H scheduler patterns.

**Tech stack:** Python 3.12 stdlib + APScheduler. No new runtime dependencies.

**Design decisions:** See spec §2 (I-D1 through I-D10).

---

## File Structure (locked before tasks)

**Schema + state:**
- Modify: `src/mthydra/controller/state/schema.py` — `SCHEMA_VERSION = 8`, new `migrate_v7_to_v8`, four triggers + indexes, fresh-install statements updated.
- Create: `src/mthydra/controller/state/probe_vantages.py` — `ProbeVantage` dataclass, lifecycle helpers.
- Create: `src/mthydra/controller/state/probe_results.py` — `ProbeResult` dataclass, append-only record + read helpers.
- Create: `src/mthydra/controller/state/image_profiles.py` — `ImageProfile` dataclass, pin + get.

**Evaluator + scheduler:**
- Create: `src/mthydra/controller/probe/__init__.py` — empty.
- Create: `src/mthydra/controller/probe/evaluator.py` — `EvaluationResult` + `evaluate_box(...)` pure function.
- Create: `src/mthydra/controller/probe/audit_wheel.py` — `ProbeAuditWheel` (APScheduler-driven).

**Config:**
- Modify: `src/mthydra/controller/config.py` — add `ProbeConfig` dataclass + `_load_probe(raw)` + `[probe]` section.
- Modify: `packaging/etc/mthydra/controller.toml.example` — add `[probe]` section.

**Bootstrap:**
- Modify: `src/mthydra/controller/cli.py` `init` block — seed three obligation_ids: `probe_audit_sweep_ran`, `probe_coverage_proven`, `probe_vantage_rotation_proven`.

**Invariants:**
- Modify: `src/mthydra/controller/state/invariants.py` — extend `check_all()` with #37–#40 (gated on schema v8+). #40 logs a warning, does not raise.

**CLI (modified):**
- Modify: `src/mthydra/controller/cli.py` — nine new subcommands (`vantage-add`, `vantage-attest-active`, `vantage-list`, `vantage-retire`, `vantage-burn`, `profile-pin`, `profile-show`, `probe-record`, `probe-evaluate`, `probe-due`). Arm `ProbeAuditWheel` in `_cmd_serve` on active node.

**Tests (created):**
- `tests/unit/controller/state/test_probe_vantages.py`
- `tests/unit/controller/state/test_probe_results.py`
- `tests/unit/controller/state/test_image_profiles.py`
- `tests/unit/controller/state/test_probe_triggers.py`
- `tests/unit/controller/probe/__init__.py` — empty.
- `tests/unit/controller/probe/test_evaluator.py`
- `tests/unit/controller/probe/test_audit_wheel.py`
- `tests/property/test_probe_invariants.py`
- `tests/integration/test_probe_harness_lifecycle.py`

**Tests (modified):**
- `tests/unit/controller/state/test_invariants.py` — #37–#40.
- `tests/unit/controller/state/test_schema.py` — v7→v8 migration assertions.
- `tests/unit/controller/test_cli.py` — nine new subcommands + bootstrap obligations + serve arming.
- `tests/unit/controller/test_bootstrap.py` — three new obligations seeded.
- `tests/unit/controller/test_config.py` — `[probe]` section.

---

## Phase 1 — Schema v8

### Task 1: Schema v7 → v8 migration

**Files:**
- Modify: `src/mthydra/controller/state/schema.py`
- Modify: `tests/unit/controller/state/test_schema.py`

- [ ] **Step 1: Failing tests**

```python
def test_schema_version_is_8(tmp_path):
    from mthydra.controller.state.schema import SCHEMA_VERSION
    assert SCHEMA_VERSION == 8

def test_probe_vantages_table_present(tmp_path):
    # PRAGMA table_info(probe_vantages) lists the expected columns.

def test_probe_results_append_only_triggers(tmp_path):
    # INSERT a row; assert UPDATE raises IntegrityError; assert DELETE raises.

def test_probe_vantages_no_relabel_burned(tmp_path):
    # INSERT a row, UPDATE state='burned'; attempt INSERT with same label;
    # assert IntegrityError.

def test_probe_vantages_burned_no_revert(tmp_path):
    # UPDATE OF state from 'burned' to anything else raises.

def test_image_profiles_fk_to_ru_images(tmp_path):
    # INSERT row referencing non-existent image_version: SQLite FK off by
    # default, but we test the row is rejected when FK is on via PRAGMA.

def test_v7_to_v8_migration_idempotent(tmp_path):
    # Force schema_version back to 7; migrate; assert version=8;
    # re-run; assert still 8 and no duplicate triggers.
```

- [ ] **Step 2: Implement**

In `schema.py`, add the four trigger constants, three CREATE TABLE statements (+ two indexes), and the migration function. Wire into `apply_schema` after the v6→v7 hop.

- [ ] **Step 3: Run + commit**

```bash
pytest tests/unit/controller/state/test_schema.py -v
git add src/mthydra/controller/state/schema.py tests/unit/controller/state/test_schema.py
git commit -m "schema(I): v7 -> v8 — probe_vantages + probe_results + image_profiles + triggers"
git push origin main
```

---

## Phase 2 — Repositories

### Task 2: `state/probe_vantages.py`

**Files:**
- Create: `src/mthydra/controller/state/probe_vantages.py`
- Create: `tests/unit/controller/state/test_probe_vantages.py`

- [ ] **Step 1: Failing tests**

Cover the public API from spec §5: `add_candidate`, `attest_active`, `retire`, `burn`, `list_by_state`, `get_vantage`, `list_due_for_rotation`. Each mutating helper writes an `audit_log` row.

- [ ] **Step 2: Implement**

Pure SQL helpers, one transaction per public function. State transitions enforced inline (raise ValueError on illegal transitions; the burned trigger backstops). Audit row format: `actor='probe_harness'`.

- [ ] **Step 3: Run + commit**

```bash
pytest tests/unit/controller/state/test_probe_vantages.py -v
git add src/mthydra/controller/state/probe_vantages.py tests/unit/controller/state/test_probe_vantages.py
git commit -m "probe_vantages(I): registry — lifecycle, burn, audit, rotation TTL"
git push origin main
```

### Task 3: `state/probe_results.py`

**Files:**
- Create: `src/mthydra/controller/state/probe_results.py`
- Create: `tests/unit/controller/state/test_probe_results.py`

- [ ] **Step 1: Failing tests**

- `record(...)` refuses if vantage state != 'active'
- `recent_for_box(box, limit)` returns rows in descending cycle_at
- `last_cycle_at(box)` returns the most recent or None
- `distinct_vantages_in_window(box, window=3600, now)` counts unique vantage_ids in the last hour

- [ ] **Step 2: Implement**

`record` does the vantage-state precheck. `last_used_at` on `probe_vantages` is updated in the same transaction.

- [ ] **Step 3: Run + commit**

```bash
pytest tests/unit/controller/state/test_probe_results.py -v
git add src/mthydra/controller/state/probe_results.py tests/unit/controller/state/test_probe_results.py
git commit -m "probe_results(I): append-only ingest + read helpers"
git push origin main
```

### Task 4: `state/image_profiles.py`

**Files:**
- Create: `src/mthydra/controller/state/image_profiles.py`
- Create: `tests/unit/controller/state/test_image_profiles.py`

- [ ] **Step 1: Failing tests**

- `pin(image_version, profile_json, ...)` inserts or overwrites
- `get_profile(image_version)` returns None for missing
- `list_pinned()` returns all rows

- [ ] **Step 2: Implement**

Trivial. Audit row on every pin (operator-attested).

- [ ] **Step 3: Run + commit**

```bash
pytest tests/unit/controller/state/test_image_profiles.py -v
git add src/mthydra/controller/state/image_profiles.py tests/unit/controller/state/test_image_profiles.py
git commit -m "image_profiles(I): operator-attested pinned profile repository"
git push origin main
```

### Task 5: Trigger tests

**Files:**
- Create: `tests/unit/controller/state/test_probe_triggers.py`

Systematic catalogue (schema-test file already smoke-tested):
- relabeling a burned label refuses
- burned → anything refuses
- append-only: UPDATE refuses, DELETE refuses
- non-burned state transitions succeed

```bash
pytest tests/unit/controller/state/test_probe_triggers.py -v
git commit -m "test(I): probe_vantages + probe_results triggers — full catalogue"
git push origin main
```

---

## Phase 3 — Config + bootstrap

### Task 6: `ProbeConfig` + TOML

**Files:**
- Modify: `src/mthydra/controller/config.py`
- Modify: `packaging/etc/mthydra/controller.toml.example`
- Modify: `tests/unit/controller/test_config.py`

- [ ] **Step 1: Failing test**

```python
def test_config_loads_probe_section(tmp_path):
    cfg = load_config(...)
    assert cfg.probe.soft_fail_window_M == 4
    assert cfg.probe.soft_fail_threshold_N == 3
    assert cfg.probe.min_distinct_vantages == 2
    assert cfg.probe.coverage_window_seconds == 3600
    assert cfg.probe.probe_vantage_ttl_days == 14
    assert cfg.probe.probe_audit_sweep_interval_seconds == 300
```

- [ ] **Step 2: Implement**

Add `ProbeConfig` dataclass; `_load_probe(raw)`; thread through `Config`; update `controller.toml.example` and the single CLI test that constructs `Config` directly.

- [ ] **Step 3: Run + commit**

```bash
pytest tests/unit/controller/test_config.py -v
git add src/mthydra/controller/config.py packaging/etc/mthydra/controller.toml.example tests/unit/controller/test_config.py tests/unit/controller/test_cli.py
git commit -m "config(I): [probe] section + ProbeConfig dataclass"
git push origin main
```

### Task 7: Bootstrap obligations

**Files:**
- Modify: `src/mthydra/controller/cli.py` (init block)
- Modify: `tests/unit/controller/test_bootstrap.py`

- [ ] **Step 1: Failing test**

```python
def test_init_seeds_probe_obligations(tmp_path):
    expected = {
        "probe_audit_sweep_ran",
        "probe_coverage_proven",
        "probe_vantage_rotation_proven",
    }
    ...
```

- [ ] **Step 2: Implement**

Append three obligation_ids to the bootstrap seeder map.

- [ ] **Step 3: Run + commit**

```bash
pytest tests/unit/controller/test_bootstrap.py -v
git commit -m "bootstrap(I): seed probe_audit + probe_coverage + vantage_rotation obligations"
git push origin main
```

---

## Phase 4 — Invariants #37–#40

### Task 8: Implement four invariants

**Files:**
- Modify: `src/mthydra/controller/state/invariants.py`
- Modify: `tests/unit/controller/state/test_invariants.py`

- [ ] **Step 1: Failing tests**

One test per invariant, exactly as in spec §9.4. #37 dup labels, #38 orphan results (need to bypass FK to construct — raw SQL with `PRAGMA foreign_keys = OFF` then back ON for check_all). #39 promoted-image-without-profile. #40 dup region/source → assert warning emitted via `warnings.warn` (use `pytest.warns`).

- [ ] **Step 2: Implement**

Gate the four checks on `expected_schema_version >= 8`. #37–#39 raise; #40 emits a `RuntimeWarning`.

- [ ] **Step 3: Run + commit**

```bash
pytest tests/unit/controller/state/test_invariants.py -v
git commit -m "invariants(I): #37-#40 — label-uniq + orphan-results + profile-required + dup-region-warn"
git push origin main
```

---

## Phase 5 — Evaluator + scheduler

### Task 9: Pure evaluator

**Files:**
- Create: `src/mthydra/controller/probe/__init__.py`
- Create: `src/mthydra/controller/probe/evaluator.py`
- Create: `tests/unit/controller/probe/__init__.py`
- Create: `tests/unit/controller/probe/test_evaluator.py`

- [ ] **Step 1: Failing tests**

```python
def test_empty_history_is_healthy(...):
    # no probe_results -> verdict 'healthy'

def test_one_hard_fail_kills(...):
    # single hard_fail row -> verdict 'hard_kill'

def test_N_soft_across_M_window_across_distinct_vantages(...):
    # 3 soft_fail across 2 vantages within last 4 cycles -> 'soft_threshold_reached'

def test_N_soft_on_single_vantage_is_soft_pending(...):
    # 3 soft_fail on 1 vantage -> 'soft_pending' (not enough distinct vantages)

def test_old_fails_outside_window_are_ignored(...):
    # 3 soft_fail older than window -> 'healthy'

def test_missing_image_profile_raises(...):
    # box with image_version that has no image_profiles row -> raises
    # EvaluationError on evaluate_box (so the audit wheel can emit the
    # 'probe_evaluate_blocked' obligation instead of silently passing).
```

- [ ] **Step 2: Implement**

`evaluate_box` reads recent `probe_results` for the box (limit = M), checks profile existence, counts distinct vantages of fail rows, and returns one of four verdicts. Pure: no DB writes.

- [ ] **Step 3: Run + commit**

```bash
pytest tests/unit/controller/probe/test_evaluator.py -v
git add src/mthydra/controller/probe/ tests/unit/controller/probe/
git commit -m "probe(I): pure evaluator — hard-kill + N-of-M-of-distinct soft-fail"
git push origin main
```

### Task 10: `ProbeAuditWheel`

**Files:**
- Create: `src/mthydra/controller/probe/audit_wheel.py`
- Create: `tests/unit/controller/probe/test_audit_wheel.py`

- [ ] **Step 1: Failing tests**

Mirror spec C `test_cover_pool_scheduler.py`:
- mock clock + frozen DB; advance; sweep emits the right obligation rows
- coverage gap → `probe_coverage_pending::<box>` row
- vantage past TTL → `probe_vantage_rotation_pending::<vantage>` row
- heartbeat `probe_audit_sweep_ran` proven each tick
- offline mode: scheduler does not arm

- [ ] **Step 2: Implement**

Standard `BackgroundScheduler` + `ThreadPoolExecutor` + `IntervalTrigger`. `run_once()` is the testable seam.

- [ ] **Step 3: Run + commit**

```bash
pytest tests/unit/controller/probe/test_audit_wheel.py -v
git commit -m "probe(I): audit wheel — sweep + kill_pending + coverage_pending + rotation_pending"
git push origin main
```

### Task 11: Arm in `_cmd_serve`

**Files:**
- Modify: `src/mthydra/controller/cli.py` `_serve_active` block
- Modify: `tests/unit/controller/test_cli.py`

Mirror the spec H `ShardReshuffleWheel` arming. Active-only.

```bash
pytest tests/unit/controller/test_cli.py -v -k 'serve'
git commit -m "serve(I): arm ProbeAuditWheel on active node"
git push origin main
```

---

## Phase 6 — CLI subcommands

### Task 12: Nine new subcommands

**Files:**
- Modify: `src/mthydra/controller/cli.py`
- Modify: `tests/unit/controller/test_cli.py`

Implement in three groups with separate commits:

**Group A: vantage lifecycle (5)**
- `vantage-add <id> --label <name> --source-kind <kind> [--region-hint ...] [--notes ...]`
- `vantage-attest-active <id> [--evidence ...]`
- `vantage-list [--state ...] [--json]`
- `vantage-retire <id> [--reason ...]`
- `vantage-burn <id> --reason <text>`

```bash
git commit -m "cli(I): vantage-add/attest-active/list/retire/burn"
```

**Group B: profile (2)**
- `profile-pin <image_version> --profile-json <path|-> --recorded-by <text> [--notes ...]`
- `profile-show <image_version> [--json]`

```bash
git commit -m "cli(I): profile-pin + profile-show"
```

**Group C: probe submission + evaluation (3)**
- `probe-record --box-id <id> --vantage <id> --check <type> --status <s> --cycle-at <iso> --evidence <text-or-path> [--image-version ...]`
- `probe-evaluate --box-id <id> [--json]`
- `probe-due [--json]`

```bash
git commit -m "cli(I): probe-record + probe-evaluate + probe-due"
git push origin main
```

Each subcommand: argparse wiring, dispatch to `_cmd_<name>(args)`, structured-output guard, exit codes (0 OK, 2 user error). Tests assert happy path + the documented errors in spec §9.9.

---

## Phase 7 — Property + integration tests

### Task 13: Property test

**Files:**
- Create: `tests/property/test_probe_invariants.py`

Hypothesis: generate sequences of (add_vantage, attest, record_probe, retire, burn) plus probe-result inserts across rotated vantages. Assertions per spec §9.6.

```bash
pytest tests/property/test_probe_invariants.py -v
git commit -m "test(I): property — vantage state + probe-result invariants under random ops"
git push origin main
```

### Task 14: Integration test

**Files:**
- Create: `tests/integration/test_probe_harness_lifecycle.py`

End-to-end per spec §9.7: bootstrap, provision a box, promote + pin an image, add + attest vantages, record probes, evaluate, hard-kill path → operator terminate → reshuffle hook fires → kill_pending clears. Burn a vantage; assert next record refuses.

```bash
pytest tests/integration/test_probe_harness_lifecycle.py -v
git commit -m "test(I): probe harness lifecycle — promote -> attest -> probe -> kill -> terminate"
git push origin main
```

---

## Phase 8 — Final verification

### Task 15: Full suite + coverage + residuals

- [ ] **Step 1: Full unit suite**

```bash
cd /home/asharov/RedHat/Dev/mthydra
pytest -q --ignore=tests/integration/test_gap_monitor.py
```

Expected: green. Baseline was 632 after spec H; +40 from spec I = ~670.

- [ ] **Step 2: Coverage on new modules**

```bash
pytest --cov=mthydra.controller.probe \
       --cov=mthydra.controller.state.probe_vantages \
       --cov=mthydra.controller.state.probe_results \
       --cov=mthydra.controller.state.image_profiles \
       --cov-report=term tests/ \
       --ignore=tests/integration/test_gap_monitor.py
```

Expected:
- `mthydra.controller.probe.*` ≥ 90%
- `mthydra.controller.state.probe_*` ≥ 90%
- `mthydra.controller.state.image_profiles` ≥ 90%

If below threshold, add targeted tests for uncovered error paths.

- [ ] **Step 3: Spec residual update if needed**

If any implementation choice deviated from the spec, append a bullet to §11. Otherwise, no change.

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "test(I): cover probe modules to >=90%" || echo "nothing to commit"
git log --oneline | head -20
git push origin main
```

You should see ~17 `(I):` commits on top of the spec-H tip.

---

## Done criteria

- All 15 task checkboxes ticked.
- `pytest -q` passes cleanly.
- ≥ 90% coverage on `mthydra.controller.probe.*` and the three `state/probe_*` / `state/image_profiles` modules.
- Nine new CLI subcommands work.
- Triggers `probe_vantages_no_relabel_burned`, `probe_vantages_burned_no_revert`, `probe_results_no_update`, `probe_results_no_delete` block their respective forbidden transitions.
- Spec invariants #37–#39 raise on their respective violations; #40 emits a warning.
- The integration test demonstrates: pin profile, attest vantages, record probes (pass + hard_fail), evaluator → hard_kill, operator terminate, spec H reshuffle hook fires, obligation row clears.
- Three new bootstrap obligations seeded (plus three anti-obligations emitted dynamically).
- DB schema bumped to v8.
