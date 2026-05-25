# Spec D2 — Image Canary + Validation Gate — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement spec D2 — `doc/specs/2026-05-25-D2-image-canary-validation.md`: schema v10 → v11 (`ru_boxes.is_canary` + immutability trigger for retired profile rows); pure `image/gate.py` evaluator; updates to `image-build` (mandatory `--profile-json` with atomic image_profiles insert), `image-promote` (gated), `provision-seed` (new `--canary` flag); new CLI `image-promote-status`, `image-rollback`, `ru-box-canary-clear`; one new invariant (#44); spec J severity table + snapshot amendment for `image_rollback_pending::*`.

**Architecture:** Single-spec amendment to existing D1 + G code paths. No new schedulers. Gate is a pure function that callers (CLI) compose. Rollback is two atomic state changes + N anti-obligation row inserts that spec J automatically surfaces.

**Tech stack:** Same as D1 — Python 3.12 stdlib + APScheduler (unused here). No new dependencies.

**Design decisions:** See spec §2 (D2-D1 through D2-D10).

---

## File Structure (locked before tasks)

**Schema + state:**
- Modify: `src/mthydra/controller/state/schema.py` — `SCHEMA_VERSION = 11`, `migrate_v10_to_v11` (ALTER + index + trigger).
- Modify: `src/mthydra/controller/state/ru_boxes.py` — `insert_box(..., is_canary=False)`, `list_canary_boxes`, `clear_canary_flag`.
- Modify: `src/mthydra/controller/state/ru_images.py` — `list_live_boxes_for_image`.
- Modify: `src/mthydra/controller/state/image_profiles.py` — read-only `list_pinned_for_versions(versions)` helper for `image-promote-status` (optional; if simple loop works inline, skip).

**Gate:**
- Create: `src/mthydra/controller/image/__init__.py` (may already exist) — empty.
- Create: `src/mthydra/controller/image/gate.py` — `GateResult` + `GateConfigView` + `evaluate_promotion_gate`.

**Config:**
- Modify: `src/mthydra/controller/config.py` — add `ImageCanaryConfig` nested under `ImageConfig`.
- Modify: `packaging/etc/mthydra/controller.toml.example` — add `[image.canary]` block.

**Spec J severity amendment:**
- Modify: `src/mthydra/controller/observability/severity.py` — add `image_rollback_pending` → crit.
- Modify: `src/mthydra/controller/observability/snapshot.py` — add prefix.

**CLI changes:**
- Modify: `src/mthydra/controller/cli.py`:
  - `image-build` — require `--profile-json`; insert profile in same transaction as image
  - `image-promote` — call gate; refuse with reasons on failure
  - `provision-seed` — accept `--canary` flag; pass through to `insert_box`
  - NEW: `image-promote-status`, `image-rollback`, `ru-box-canary-clear`

**Invariants:**
- Modify: `src/mthydra/controller/state/invariants.py` — #44 live-canary-on-retired-image.

**Tests (created):**
- `tests/unit/controller/image/__init__.py` — empty.
- `tests/unit/controller/image/test_gate.py`
- `tests/integration/test_image_canary_lifecycle.py`

**Tests (modified):**
- `tests/unit/controller/state/test_schema.py` — v10→v11
- `tests/unit/controller/state/test_invariants.py` — #44
- `tests/unit/controller/state/test_ru_boxes.py` — `is_canary` + helpers (if file exists; else create)
- `tests/unit/controller/test_config.py` — `[image.canary]`
- `tests/unit/controller/test_cli.py` — all the CLI changes + the new commands
- `tests/unit/controller/observability/test_severity.py` — `image_rollback_pending`
- `tests/unit/controller/observability/test_snapshot.py` — prefix classification

---

## Phase 1 — Schema v11

### Task 1: v10 → v11 migration

Failing tests: `SCHEMA_VERSION==11`, `is_canary` column present, default 0 on existing rows, partial index present, immutability trigger refuses UPDATE on retired image_profiles, migrate idempotent.

```bash
git commit -m "schema(D2): v10 -> v11 — ru_boxes.is_canary + image_profiles retired-immutable trigger"
git push origin main
```

---

## Phase 2 — Repository extensions

### Task 2: `ru_boxes.is_canary` field + helpers

- `insert_box` gains `is_canary: bool = False` (positional or keyword — keyword to avoid breaking existing callers).
- `list_canary_boxes(conn, *, image_version=None, state_filter=None) -> list[str]`.
- `clear_canary_flag(conn, box_id, *, at, reason)` — UPDATE + audit row.
- Tests cover all three.

```bash
git commit -m "ru_boxes(D2): is_canary field + list_canary_boxes + clear_canary_flag"
```

### Task 3: `ru_images.list_live_boxes_for_image`

Simple helper: SELECT box_id FROM ru_boxes WHERE image_version=? AND state IN ('live','provisioning'); optional include_terminated. Tests cover.

```bash
git commit -m "ru_images(D2): list_live_boxes_for_image helper"
git push origin main
```

---

## Phase 3 — Gate

### Task 4: `image/gate.py` pure evaluator

API per spec §4. Failing tests cover the six cases in spec §11.2.

```bash
pytest tests/unit/controller/image/test_gate.py -v
git commit -m "gate(D2): evaluate_promotion_gate — profile + canary cohort + cycle/vantage threshold + kill-pending check"
git push origin main
```

---

## Phase 4 — Config + spec J amendment

### Task 5: `ImageCanaryConfig`

Nested under `ImageConfig`. Defaults: `min_boxes=1`, `min_cycles_per_box=4`. Inherits `min_distinct_vantages` from `cfg.probe`.

Update `controller.toml.example` + the lone direct-`Config` test.

```bash
git commit -m "config(D2): [image.canary] subsection + ImageCanaryConfig dataclass"
```

### Task 6: Spec J severity + snapshot amendments

Add `image_rollback_pending` to `_FIXED` (crit) and `_ANTI_PREFIXES`. Two tests for each.

```bash
git commit -m "observability(D2): severity + snapshot classify image_rollback_pending as crit"
git push origin main
```

---

## Phase 5 — Invariant + CLI

### Task 7: Invariant #44

Gated on schema v11+. Failing test seeds: provision canary box from a candidate image, mark live, retire the image (via SQL bypass since `retire` may not allow it directly), assert check_all raises check 44.

```bash
git commit -m "invariants(D2): #44 live canary on retired image"
```

### Task 8: `image-build --profile-json` (now required)

Modify `_cmd_image_build` to:
1. Read profile JSON from `args.profile_json` (path or `-` for stdin).
2. After inserting the candidate ru_images row, insert image_profiles row in the same transaction (or back-to-back since `state.image_profiles.pin` opens a fresh transaction — close the call carefully).

Update test_cli.py existing image-build tests (some may need the flag added). Add a new test: missing `--profile-json` exits 2; stdin `-` reads stdin.

```bash
git commit -m "cli(D2): image-build now requires --profile-json (atomic pin per design §9 step 3)"
```

### Task 9: `image-promote` gated

Modify `_cmd_image_promote` to:
1. Load config (it currently doesn't — refactor to load via `args.config`; default `--config /etc/mthydra/controller.toml` if missing).
2. Build `GateConfigView` from `cfg.image.canary` + `cfg.probe.min_distinct_vantages`.
3. Call `evaluate_promotion_gate`. If `not passed`, print each reason on a stderr line, audit-log the refusal, return 2.
4. Otherwise call existing `promote(...)` + record `t4_image_promoted` (D1 may already do this).

Tests: gate refusal with 1 reason; gate refusal with multiple reasons; happy path with full canary cohort.

```bash
git commit -m "cli(D2): image-promote now calls the validation gate before promoting"
git push origin main
```

### Task 10: `provision-seed --canary`

Add flag; pass through to `insert_box(is_canary=args.is_canary)`. Add audit-log detail `{"is_canary": true}`.

Two tests: with flag → row has `is_canary=1`; without → 0.

```bash
git commit -m "cli(D2): provision-seed --canary flag plumbs through to ru_boxes.is_canary"
```

### Task 11: `image-promote-status`

New CLI. Read-only. Loads config, evaluates gate, prints reasons + canary stats. `--json` emits the dataclass.

```bash
git commit -m "cli(D2): image-promote-status — read-only gate evaluation, never refuses to run"
```

### Task 12: `image-rollback`

New CLI. Args: `<version>`, `--to <target_version>`, `--evidence <text>`. Refuses if `target_version` was never promoted (i.e., never had a `promoted_at`). On success:
1. `retire(conn, version, ...)` (reuse existing helper).
2. For target: if it's currently retired, set `state='promoted'`, `promoted_at=_now()`, `retired_at=NULL` via a small direct-SQL update (no existing helper does "un-retire"; add a comment naming this as the only un-retire path).
3. For each live box from `version` (use `list_live_boxes_for_image`): `set_obligation(image_rollback_pending::<box_id>, ...)`.
4. Audit row.

Tests: happy path, refusal when target was never promoted, the per-box anti-obligation rows are created.

```bash
git commit -m "cli(D2): image-rollback — retire + re-promote + per-box rollback_pending obligations"
```

### Task 13: `ru-box-canary-clear`

New CLI. Args: `<box_id>`, `--reason <text>`. Calls `clear_canary_flag`.

```bash
git commit -m "cli(D2): ru-box-canary-clear — demote a canary box to regular fleet"
git push origin main
```

---

## Phase 6 — Integration test + coverage

### Task 14: Integration test

Per spec §11.6.

```bash
git commit -m "test(D2): image canary lifecycle — build + canary + soak + gate + promote + rollback"
```

### Task 15: Full suite + coverage

Targets: ≥ 90% on `mthydra.controller.image.*`. State helper extensions covered by their own files.

```bash
pytest -q --ignore=tests/integration/test_gap_monitor.py
pytest --cov=mthydra.controller.image \
       --cov-report=term tests/ --ignore=tests/integration/test_gap_monitor.py
git commit -m "test(D2): cover image.gate to >=90%" || echo "nothing"
git push origin main
```

---

## Done criteria

- All 15 task checkboxes ticked.
- `pytest -q` passes cleanly.
- ≥ 90% coverage on `mthydra.controller.image.*`.
- `image-build` refuses without `--profile-json`.
- `image-promote` refuses when gate fails; passes when gate is satisfied.
- `image-promote-status` is read-only and never returns non-zero on gate failure.
- `image-rollback` retires source, re-promotes target, emits per-box anti-obligation.
- `provision-seed --canary` marks the row.
- `ru-box-canary-clear` clears the flag with audit.
- Spec invariant #44 fires on live-canary-on-retired-image.
- Spec J classifies `image_rollback_pending::*` as crit.
- DB schema bumped to v11.
