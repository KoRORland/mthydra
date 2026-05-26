# Spec M — Log Compaction — Implementation Plan

**Goal:** Schema v11 → v12 (`compactor_marker` table + three modified `*_no_delete` triggers with `WHEN NOT EXISTS (compactor_marker)` clause); `state/compactor.py` module with three table-specific compaction helpers; one new CLI subcommand `compact-logs`; tests.

## Tasks

### 1. Schema v11 → v12
- `compactor_marker` table
- Drop + recreate three `*_no_delete` triggers with sentinel check
- `migrate_v11_to_v12`
- Schema tests

### 2. `state/compactor.py`
- `CompactionResult` dataclass
- `_compact_table` internal helper (acquires marker, performs DELETE or COUNT, releases marker, audit-logs)
- Three public wrappers: `compact_alert_log`, `compact_probe_results`, `compact_distribution_log`
- Tests cover: dry-run COUNT, real DELETE, marker released on exception, raw DELETE without marker still refuses

### 3. CLI `compact-logs`
- Argparse + dispatch
- `--table all` invokes all three
- Default dry-run requires explicit `--no-dry-run` to delete
- Tests

## Done criteria

- All tests pass.
- Triggers still block raw DELETE.
- Compactor can clear rows older than a cutoff.
- Schema bumped to v12.
