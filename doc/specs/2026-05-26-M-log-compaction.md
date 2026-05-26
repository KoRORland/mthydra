# Spec M — Append-Only Log Compaction

Status: **Draft, awaiting operator review.**
Predecessor: `doc/specs/2026-05-25-I-probe-vantage-harness.md` (§11 residual #8), `doc/specs/2026-05-25-J-observability-service.md` (§11 residual #7), `doc/specs/2026-05-25-K-user-distribution-channel.md` (§11 residual #8). Each named "logs grow unbounded; future compaction not implemented."
Successors blocked on this: none. M is maintenance-only.

---

## 1. Purpose

Three append-only tables grow unbounded:

- `alert_log` (spec J)
- `probe_results` (spec I)
- `distribution_log` (spec K)

At default cadences, steady-state growth is ~50 rows/day per table; incidents push it higher. SQLite handles years of this without trouble, but a deployment running for a decade with multiple incidents will accumulate millions of rows. Spec M ships the **operator-driven compaction** mechanism: delete rows older than a chosen cutoff, with a structural mechanism that respects the append-only triggers shipped in specs I/J/K.

**Out of scope:** automated retention policies (operator decides per-deployment); per-row-kind retention (the operator runs separate compactions if they want different cutoffs for heartbeat vs. failure rows); cross-table consistency (each table compacts independently — the rollback hooks in spec D2 + the rollback obligations in spec J don't depend on log row presence, so compaction is safe at any cutoff).

---

## 2. Locked design decisions

| ID | Decision | Rationale |
|---|---|---|
| M-D1 | **Trigger relief via sentinel table.** A `compactor_marker` table is consulted by the existing `*_no_delete` triggers. When a row is present naming the table being compacted, the trigger short-circuits the `RAISE(ABORT, ...)`. Compactor inserts the marker, performs the DELETE, removes the marker — all in one transaction. | Alternatives (drop trigger + recreate; per-row exception logic; raw `PRAGMA` overrides) are either race-prone or weaken the append-only discipline outside the compactor's scope. The sentinel mechanism keeps the no-DELETE rule fully enforced for any code path that doesn't go through the compactor. |
| M-D2 | **Compaction is operator-driven via CLI.** `compact-logs --table <name|all> --before <iso> [--dry-run]`. No scheduler. The deployment runbook documents a per-environment retention SLA. | Automated retention requires per-deployment policy decisions (some operators want 30d retention for compliance, others want 5y for forensic). Shipping a default would invite either silent data loss or unbounded growth. |
| M-D3 | **`--dry-run` is the default discipline.** The CLI prints how many rows would be deleted; the operator re-runs without `--dry-run` to actually compact. | Compaction is irreversible. Two-step is the right default for a maintenance command operators run rarely. |
| M-D4 | **Cutoff key per table is the natural "when the row was made" column:** `alert_log.attempted_at`, `probe_results.cycle_at`, `distribution_log.attempted_at`. | All three are non-null timestamp strings already indexed (the indexes shipped in I/J/K). The DELETE is `WHERE <ts_col> < <cutoff>` — index-friendly. |
| M-D5 | **`audit_log` row per compaction run.** Captures table, cutoff, dry-run flag, deleted row count, evidence text. | The compaction itself becomes a forensic event (lost rows must be discoverable; the audit row tells you *when* and *what*). |
| M-D6 | **No new obligations.** Compaction is operator-driven; not running it is not a safety risk, only a disk-space risk. | Adding a "you haven't compacted lately" obligation would create alert fatigue. |

---

## 3. Schema additions (v11 → v12)

### 3.1 `compactor_marker` table

```sql
CREATE TABLE compactor_marker (
  table_name TEXT PRIMARY KEY,
  acquired_at TEXT NOT NULL,
  acquired_by TEXT NOT NULL
);
```

A row in this table means "the named table is currently being compacted; the no-DELETE trigger should allow DELETE." Compactor inserts on start, deletes on end (the marker row itself is excluded from the no-DELETE discipline by virtue of not being in the protected tables).

### 3.2 Modified triggers

Each of the three existing `*_no_delete` triggers gains a `WHEN NOT EXISTS (SELECT 1 FROM compactor_marker WHERE table_name=<self>)` clause. The `*_no_update` triggers are unchanged — compaction only needs DELETE relief.

### 3.3 Schema version bump

`SCHEMA_VERSION = 12`; `migrate_v11_to_v12(conn)` creates the marker table + replaces the three triggers (DROP old + CREATE new with the WHEN clause).

---

## 4. Repository API

`mthydra.controller.state.compactor`:

```python
@dataclass(frozen=True)
class CompactionResult:
    table_name: str
    cutoff: str
    dry_run: bool
    deleted: int                  # for dry_run, this is "would delete"

def compact_alert_log(conn, *, before: str, dry_run: bool, actor: str) -> CompactionResult: ...
def compact_probe_results(conn, *, before: str, dry_run: bool, actor: str) -> CompactionResult: ...
def compact_distribution_log(conn, *, before: str, dry_run: bool, actor: str) -> CompactionResult: ...
```

Internal helper acquires the marker, runs the COUNT (for dry_run) or DELETE, removes the marker, audit-logs.

---

## 5. CLI surface

```
mthydra-controller compact-logs \
    --table {alert_log|probe_results|distribution_log|all} \
    --before <iso-timestamp> \
    [--dry-run] \
    [--evidence <text>]
```

Default `--dry-run` is **on** when `--evidence` is missing; explicit `--no-dry-run` is required to actually delete (defence in depth — operator types out their intention).

---

## 6. Test plan

- Unit: `compact_alert_log` deletes only rows whose `attempted_at < cutoff`; preserves newer rows; respects `dry_run`.
- Unit: marker is set + removed in the same transaction (assert marker table empty after success and after exception).
- Unit: same for `probe_results` (cutoff on `cycle_at`) and `distribution_log` (`attempted_at`).
- Unit: schema v12 — triggers updated; raw DELETE without marker still refuses.
- CLI: `compact-logs --dry-run` counts; without `--dry-run` deletes; refuses without `--evidence`.

---

## 7. Honest residuals

1. **Compaction can be triggered while a sweep is mid-flight.** SQLite serialises; the compactor blocks until any in-progress sweep commits. Acceptable: compaction is a maintenance operation; the operator picks a low-traffic window.
2. **No "keep last N" retention.** Operators who want "always keep the last 100 rows regardless of age" must invert their cutoff calculation themselves (`compact-logs --before <some-ts-N-rows-ago>`). Not solved.
3. **No cross-table consistency check.** If the operator compacts `alert_log` but not `probe_results`, audit chains involving both will reference deleted rows on one side. Acceptable; the audit_log table itself is never compacted (no spec M support for it).
4. **Marker table is global, one row per protected table.** A second compactor on the same table while one is running would error on the PRIMARY KEY conflict — desirable: prevents concurrent compaction races. Stated for completeness.
