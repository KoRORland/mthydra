# Spec C — Cover-Domain Pool Manager

Status: **Draft, awaiting operator review.**
Predecessor: `doc/design.md` §10 (T5), `doc/specs/2026-05-18-A-controller-state-and-backup.md` §4.2, `doc/specs/2026-05-19-B-signed-endpoint-descriptor.md`.
Successors blocked on this: `G` (RU provisioning artifact generator), `H` (shard manager), `I` (probe vantage harness), `J` (observability), `K` (distribution channel).

---

## 1. Purpose

Define the controller's cover-domain pool: the state machine that governs every Fake-TLS SNI from sourcing through retirement, the structural enforcement of the burned-set rule, the operator-attested Russia-vantage verification gate, and the rotation/replenishment policies that keep T5 (`doc/design.md` §10) from being a discipline-only control.

Spec A established the schema (`cover_domain_pool`, `burned_domains`) and the atomic burn move (`state/burned.py`). Spec C completes T5: it formalizes every state transition, ships SQLite triggers that make the burned-set rule structurally impossible to violate, adds the verification gate, and runs the schedulers that surface rotation and re-verification obligations.

Out of scope: automated probe execution (spec I), published-vs-reserve distinction (spec K), replacement-box provisioning (spec G/H), box→user assignment policy (spec H). Spec C exposes the seams these consume; it does not implement them.

---

## 2. Locked design decisions

Approved during brainstorming session 2026-05-19.

| ID | Decision | Rationale |
|---|---|---|
| C-D1 | **Russia-vantage verification gate is operator-attested for MVP.** A CLI `cover-attest-verified --vantage <name> --evidence <text>` is the only path that moves `candidate_unverified → candidate_verified`. | Spec I (automated probe harness) is not built yet, and T5 §4 makes the gate non-negotiable. Operator attestation ships now; the same function signature is later called by spec I with no schema change. |
| C-D2 | **Spec C owns the rotation timer + due-for-rotation decision.** An APScheduler job flags overdue `in_use` domains; rotation itself is operator-driven via `cover-rotate` for MVP. | The pool is the natural home for "is this domain past its lifetime." Replacement-box machinery lives in spec G/H; spec C exposes a stable `list_due_for_rotation()` hook that spec H plugs into later. |
| C-D3 | **Single rotation TTL — `in_use` is treated as published immediately.** | Conservative until spec K introduces the published/reserve distinction. Over-rotates reserve boxes once they exist, but reserve boxes do not yet exist. Honest about what we know. |
| C-D4 | **Pool-low policy = alert + freeze rotation; refuse assignment only at zero.** When verified-pool count `< freeze_threshold`, the rotation sweep pauses and emits a `cover_pool_rotation_frozen` obligation breach. Independently, `assign_to_box` refuses only when zero verified domains remain (cannot satisfy the request). Between the two thresholds, existing assignments succeed but no new rotations are initiated. | T5 §5 is absolute: never reuse burned. Freezing rotation preserves the rule structurally while still serving whatever verified inventory exists; the residual is "rotation slows," never "reuse burned." |
| C-D5 | **`candidate_verified → candidate_unverified` on TTL.** Verification staleness is enforced by a periodic sweep; stale rows cannot be assigned. | T5 §4: "verification is point-in-time and must be re-run, not assumed durable." Deterministic and testable; matches the spec A obligation-timer pattern. |
| C-D6 | **Burned-set non-reuse enforced by SQLite trigger, not just code.** | Spec A's invariant ("XOR membership") was code-policy. A trigger makes it structurally impossible to violate even via raw SQL during operator debugging. |
| C-D7 | **No `cover-unburn` CLI.** | The burned-set is monotonic by design (T5 §2). Not offering the affordance is the discipline. |
| C-D8 | **`audit_log` row per state transition.** | Matches spec A §4.7 / spec B's discipline. Verification evidence is captured verbatim and survives backup/restore. |

---

## 3. Schema additions (v2 → v3)

### 3.1 `cover_domain_pool` — add `entered_in_use_at`

```sql
ALTER TABLE cover_domain_pool ADD COLUMN entered_in_use_at TEXT;
-- NULL unless state='in_use'; set when transitioning to in_use; used by rotation TTL.
```

### 3.2 Triggers — structural enforcement of burned-set rule

```sql
CREATE TRIGGER cover_pool_reject_burned
BEFORE INSERT ON cover_domain_pool
WHEN EXISTS (SELECT 1 FROM burned_domains WHERE domain = NEW.domain)
BEGIN
  SELECT RAISE(ABORT, 'cover-pool: domain is in burned_domains; never reuse');
END;

CREATE TRIGGER burned_domains_no_delete
BEFORE DELETE ON burned_domains
BEGIN
  SELECT RAISE(ABORT, 'cover-pool: burned_domains is append-only');
END;
```

Both triggers are created in the v2→v3 migration; both are validated to exist by startup self-check #17 (see §6).

### 3.3 Schema version bump

`meta.schema_version` advances `2 → 3`. Migration is forward-only.

---

## 4. State machine

```
                  cover-add (operator CLI)
                          │
                          ▼
              [candidate_unverified]
                  │            ▲
   cover-attest-  │            │ reverify TTL elapsed
   verified       │            │ (reverify_sweep job)
   (operator)     ▼            │
              [candidate_verified]
                          │
   assign_to_box (spec G/  │
   H call site, MVP n/a)   ▼
                      [in_use]
                          │
   cover-rotate /         │
   box terminated         ▼
                     [burned]    ← monotonic, terminal, separate table
```

**Legal transitions** (all others raise `ValueError` with the offending state pair):

| From | To | Caller | Notes |
|---|---|---|---|
| ∅ | `candidate_unverified` | `add_candidate` (operator CLI) | Trigger refuses if domain ∈ `burned_domains` |
| `candidate_unverified` | `candidate_verified` | `attest_verified` (operator CLI for MVP; spec I later) | Sets `last_verified_at`, `verified_from_vantage` |
| `candidate_verified` | `candidate_unverified` | `downgrade_stale_verified` (sweep) | Clears `verified_from_vantage`, leaves `last_verified_at` for audit |
| `candidate_verified` | `in_use` | `assign_to_box` (spec G/H call site) | Refuses if pool below halt; sets `entered_in_use_at`, `assigned_box_id` |
| `in_use` | `burned` | `rotate_and_burn` (operator CLI / spec H) | Atomic via `burned.mark_burned` |

---

## 5. Repository API

`src/mthydra/controller/state/cover_pool.py` extends the existing thin spec-A repository. All functions take an explicit `sqlite3.Connection` and an explicit `now: str` (ISO-8601 UTC) — no implicit clock, no implicit transactions outside the documented atomic moves.

```python
# Existing (spec A) — kept:
add_candidate(conn, domain: str, *, added_at: str, notes: str | None = None) -> None
list_by_state(conn, state: str) -> list[CoverDomain]

# Renamed for clarity (spec A had `mark_verified`; spec C standardizes):
attest_verified(
    conn,
    domain: str,
    *,
    from_vantage: str,
    at: str,
    evidence: str | None = None,
) -> None
# state: candidate_unverified -> candidate_verified
# emits audit row: category='cover_attest_verified', details=evidence

downgrade_stale_verified(
    conn,
    *,
    now: str,
    reverify_after_days: int,
) -> list[str]
# state: candidate_verified -> candidate_unverified for every row where
#   now - last_verified_at > reverify_after_days
# returns list of downgraded domains; emits one audit row per

assign_to_box(
    conn,
    domain: str,
    *,
    box_id: str,
    at: str,
) -> None
# state: candidate_verified -> in_use
# sets entered_in_use_at=at, assigned_box_id=box_id
# raises if domain is not in candidate_verified state (covers stale-verified
# which was downgraded by the reverify sweep). Does NOT consult
# freeze_threshold — freeze affects only the rotation sweep, not the ability
# to satisfy an explicit assignment when verified inventory exists.

list_due_for_rotation(
    conn,
    *,
    now: str,
    rotation_ttl_days: int,
) -> list[CoverDomain]
# returns in_use rows where now - entered_in_use_at > rotation_ttl_days

pool_health(conn) -> PoolHealth
# struct: counts per state + rotation_frozen: bool
#         + oldest_in_use_at + oldest_unverified_at + last_attest_at

rotate_and_burn(
    conn,
    domain: str,
    *,
    reason: str,
    last_box_id: str,
    at: str,
    details: str | None = None,
) -> None
# convenience: asserts state='in_use', then calls burned.mark_burned
# emits audit row: category='cover_rotated', details captures reason
```

`PoolHealth` is a frozen dataclass; `CoverDomain` already exists from spec A and gains the `entered_in_use_at` field.

**Audit-log integration:** every state-changing function writes one `audit_log` row before commit, with categories: `cover_added`, `cover_attest_verified`, `cover_downgraded_stale`, `cover_assigned`, `cover_rotated`, `cover_burned`. Mirrors spec A §4.7.

---

## 6. Startup invariants (extend spec B's #13–#16)

Added to `state/invariants.py`; run by `controller.startup.self_check`:

- **#17 — Triggers present.** Both `cover_pool_reject_burned` and `burned_domains_no_delete` exist in `sqlite_master`. (Schema migration created them; this check defends against an externally-tampered DB.)
- **#18 — `entered_in_use_at` consistency.** `entered_in_use_at IS NOT NULL` iff `state = 'in_use'`. Counter-example query:

  ```sql
  SELECT domain, state, entered_in_use_at FROM cover_domain_pool
  WHERE (state = 'in_use' AND entered_in_use_at IS NULL)
     OR (state != 'in_use' AND entered_in_use_at IS NOT NULL);
  ```

- **#19 — `in_use` ↔ live box link.** Every `in_use` row has non-null `assigned_box_id` referencing an active row in `ru_boxes`.
- **#20 — Verification timestamps populated.** `last_verified_at IS NOT NULL` and `verified_from_vantage IS NOT NULL` for every row where state ∈ {`candidate_verified`, `in_use`}.

Each check raises `InvariantViolation` with the violating row(s) when broken; startup aborts before serving.

---

## 7. Schedulers & obligations

Two APScheduler jobs added to the controller serve loop (mirroring `mthydra.descriptor.scheduler` from spec B).

### 7.1 `cover_pool_reverify_sweep`

- Cadence: `reverify_sweep_interval` (default `1h`)
- Action: `downgrade_stale_verified(conn, now=…, reverify_after_days=…)`
- Emits one audit row per downgraded domain plus one per-sweep audit row with the count
- Heartbeat: obligation `cover_pool_reverify_sweep_ran` proven each tick

### 7.2 `cover_pool_rotation_sweep`

- Cadence: `rotation_sweep_interval` (default `1h`)
- Action:
  1. `pool_health(conn)`. If `rotation_frozen=True`: emit `cover_pool_rotation_frozen` obligation breach and **skip** steps 2–3.
  2. Otherwise: `list_due_for_rotation(conn, now=…, rotation_ttl_days=…)`.
  3. For each due-for-rotation domain, emit a `cover_pool_rotation_pending` obligation row keyed by domain. Operator drives `cover-rotate <domain>` to clear it (or, post-G/H, an automated rotation pipeline consumes the same list).
- Heartbeat: obligation `cover_pool_rotation_sweep_ran` proven each tick.

### 7.3 §12 obligation contributions

Added to the obligation registry seeded by `controller.bootstrap`:

| Obligation key | Cadence / semantics |
|---|---|
| `cover_pool_reverify_sweep_ran` | hourly heartbeat — sweep is alive |
| `cover_pool_rotation_sweep_ran` | hourly heartbeat — sweep is alive |
| `cover_pool_reverify_pass_proven` | `reverify_after_days × 2` (default 60d) — proven when any operator-attest happens; surfaces "operator is actually re-verifying" |
| `cover_pool_replenishment_proven` | `replenishment_interval_days` (default 90d) — proven when any operator `cover-add` succeeds; surfaces "operator is actually topping up" |
| `cover_pool_rotation_frozen` | anti-obligation; presence = breach. Set when verified count `< freeze_threshold`, cleared automatically when pool refills |
| `cover_pool_rotation_pending` (per-domain key) | anti-obligation; one row per overdue domain; cleared by `cover-rotate` or by the eventual spec G/H rotation pipeline |

### 7.4 Config additions

New `[cover_pool]` section in `controller.toml`:

```toml
[cover_pool]
rotation_ttl_days           = 14
reverify_after_days         = 30
freeze_threshold            = 2
reverify_sweep_interval     = "1h"
rotation_sweep_interval     = "1h"
replenishment_interval_days = 90
```

Loaded into a `CoverPoolConfig` dataclass (mirroring spec B's `DescriptorConfig`); defaults shipped in `packaging/etc/mthydra/controller.toml.example`. All values operator-tunable; tuning is named as a deployment-runbook obligation.

---

## 8. CLI surface

Added to `mthydra-controller` via `src/mthydra/controller/cli.py` alongside the existing spec A/B subcommands. All take `--db-path`; structured commands accept `--json`.

```
mthydra-controller cover-add <domain> [--notes <text>]
    # state: ∅ -> candidate_unverified
    # refuses if domain in burned_domains (trigger blocks; CLI surfaces clean error)

mthydra-controller cover-attest-verified <domain> --vantage <name> \
    [--evidence <text-or-path>]
    # state: candidate_unverified -> candidate_verified
    # --vantage is free-text (spec I will add a registry)
    # --evidence is captured verbatim in audit_log

mthydra-controller cover-list [--state <state>] [--json]
    # default: prints counts per state + table of all rows
    # --state filters; --json emits structured output

mthydra-controller cover-rotate <domain> [--reason <text>]
    # state: in_use -> burned (atomic via burned.mark_burned)
    # default --reason='manual_rotate'

mthydra-controller cover-due [--json]
    # prints due-for-rotation + stale-verified + pool-health summary
    # the "what should I do?" pre-flight command

mthydra-controller cover-pool-stats [--json]
    # counts + rotation_frozen + oldest_in_use_age + oldest_unverified_age + last_attest_at
```

No `cover-unburn` or `cover-burn-delete` subcommand exists. The burned-set is structurally enforced (trigger #2) and operationally enforced (no CLI affordance).

---

## 9. Test plan

Coverage target: ≥ 90% line coverage on `mthydra.controller.state.cover_pool`, the new CLI handlers, and the schedulers.

### 9.1 Unit tests — `tests/unit/test_cover_pool.py`

- Full state-pair matrix: every legal transition succeeds; every illegal transition raises with the offending state pair.
- `attest_verified`: writes audit row with vantage + evidence; rejects from any non-`candidate_unverified` state.
- `downgrade_stale_verified`: TTL boundary (exactly at TTL ± 1s); returns correct list; emits one audit row per downgraded domain.
- `assign_to_box`: refuses domains not in `candidate_verified` (covers stale-verified after sweep downgrade); sets `entered_in_use_at`. Test the freeze-threshold case separately — assigning the last verified row succeeds; the next rotation-sweep then sees `rotation_frozen=True`.
- `list_due_for_rotation`: TTL boundary; ignores non-`in_use` rows.
- `pool_health`: counts correct; `rotation_frozen` flips at threshold boundary.
- `rotate_and_burn`: atomic on failure (no half-state if `burned.mark_burned` raises mid-way).

### 9.2 Trigger tests — `tests/unit/test_cover_pool_triggers.py`

- `cover_pool_reject_burned`: INSERT of burned domain raises `sqlite3.IntegrityError`; pool row count unchanged.
- `burned_domains_no_delete`: DELETE from `burned_domains` raises; row count unchanged.
- Both triggers survive `PRAGMA foreign_keys=ON` toggling and SQLite `VACUUM`.

### 9.3 Invariant tests — extend `tests/unit/test_invariants.py`

For each of #17–#20: construct a deliberately-broken DB, assert the self-check raises with the right invariant name and the violating row(s) attached.

### 9.4 Scheduler tests — `tests/unit/test_cover_pool_scheduler.py`

- Mock clock; advance time; assert reverify-sweep downgrades exactly the stale subset.
- Rotation-sweep emits one `cover_pool_rotation_pending` row per due-for-rotation domain; clearing rotation (via `cover-rotate`) deletes the matching obligation.
- When `rotation_frozen=True`: rotation-sweep emits the freeze obligation and emits **zero** rotation-pending rows.
- Both sweep jobs heartbeat their `*_ran` obligation each tick.

### 9.5 Property test — `tests/property/test_cover_pool_invariants.py`

Hypothesis: random sequence of `(add, attest, downgrade, assign, rotate)` operations on a fresh DB. Invariants verified after every step:

- `cover_domain_pool ∩ burned_domains = ∅`
- `burned_domains` row count is monotonically non-decreasing
- No domain ever transitions to `in_use` more than once without an intervening burn

### 9.6 Integration test — `tests/integration/test_cover_pool_lifecycle.py`

Full lifecycle: `cover-add → cover-attest-verified → assign_to_box → wait past rotation_ttl → cover-due lists it → cover-rotate → burned_domains contains it`. Assert burned row content (reason, `last_box_id`, details) survives a backup + restore cycle (reusing spec A's backup/restore plumbing).

### 9.7 CLI tests — extend `tests/unit/test_cli.py`

Each new subcommand: happy path + one named failure. `cover-add` of a burned domain returns exit code 2 with the trigger's error surfaced cleanly. `--json` output schema-validated for `cover-list`, `cover-due`, `cover-pool-stats`.

### 9.8 Failure-mode catalogue

| Failure | Behaviour |
|---|---|
| Operator tries to add a burned domain | Trigger raises `IntegrityError`; CLI exit 2 with `cover-pool: domain is in burned_domains; never reuse` |
| Operator tries to delete from `burned_domains` via raw SQL | Trigger raises; row count unchanged |
| Pool drops below `freeze_threshold` mid-operation | Next rotation sweep emits `cover_pool_rotation_frozen` obligation and skips rotation requests. `assign_to_box` remains callable until verified inventory reaches zero, at which point it raises `ValueError` with the offending state |
| Reverify-sweep races with an operator attest | Both serialize through SQLite; whichever commits first wins; an attest immediately after a downgrade simply re-promotes; audit log captures both events |
| Operator runs `cover-rotate` on a non-`in_use` domain | CLI exit 2 with `cover-pool: <domain> is not in_use (state=...)` |
| Operator omits `--vantage` on `cover-attest-verified` | argparse rejects at parse time |
| Clock skew across reverify TTL boundary | TTL uses controller's wall clock; no special handling — accepted residual, noted in §11 |

---

## 10. Cross-spec contracts

Stable seams that downstream specs MUST respect:

- **Spec G (RU provisioning artifact generator).** When generating a seed for a fresh box, MUST call `assign_to_box(conn, domain, box_id=..., at=...)` to atomically claim a `candidate_verified → in_use`. MUST NOT pre-select a domain and write it to the seed before that call commits.
- **Spec H (shard manager).** When a box is terminated for any reason, MUST call `rotate_and_burn(conn, domain, reason=..., last_box_id=box_id, at=..., details=...)` *before* deleting the `ru_boxes` row, so the cover→box link survives in the audit log. The shard manager also wires its rotation pipeline to consume `list_due_for_rotation(...)` — no new mechanism, just a different caller.
- **Spec I (probe vantage harness).** Replaces the operator-attested CLI with automated calls to `attest_verified(..., from_vantage=<registered-vantage>, evidence=<probe-result-blob>)`. Same signature; no schema change. Spec I additionally introduces a vantage registry, replacing the free-text `--vantage` label.
- **Spec J (observability).** Consumes `pool_health(conn)` and the `cover_pool_*` obligation rows; emits operator alerts on `rotation_frozen=True` or when `cover_pool_rotation_pending` exceeds an operator-tunable count.
- **Spec K (distribution channel).** When a box's address is published, K writes a per-row `published_at` (added by K's schema bump) and may declare a shorter rotation TTL for published rows. Spec C's `rotation_ttl_days` remains the floor; K can shorten, never lengthen.

---

## 11. Honest residuals (Spec C)

- **Operator-attested verification is a trust-the-operator hop.** A careless operator can attest a verified state for a domain they never actually probed from a Russia-vantage. Spec C captures `--evidence` verbatim in the audit log but cannot validate it. Bounded only by operator discipline until spec I lands. Listed as a §12 obligation (`cover_pool_reverify_pass_proven`) in the deployment runbook.
- **`in_use` = published is conservative, not accurate.** When spec K lands, some `in_use` domains will be reserve (unpublished, longer-lifetime). The MVP over-rotates them. Honest trade vs. shipping rotation discipline now.
- **Pool freeze can leave a fingerprinted `in_use` domain live past its TTL.** Chosen deliberately over reusing burned. Escape hatch: operator restocking. T5 §5 already names this — spec C accepts and surfaces it via the obligation rather than masking it.
- **Rotation TTL is point-in-time, not adaptive.** No automatic shortening under elevated probe-failure pressure. Spec I + spec J can later wire a hook to drop the TTL; spec C leaves the config knob in place.
- **Vantage labels are free-text.** A typo means `ru-vps-01` and `ru-vps-1` do not equate. Spec I introduces a vantage registry; until then, audit-log search compensates.
- **Clock skew across TTL boundaries.** Reverify and rotation TTLs use the controller's wall clock; a NTP fault or large skew could mis-classify a domain by minutes. Accepted residual — TTLs are measured in days, the impact is negligible. Noted here rather than buried.

---

## 12. §12 obligation summary (deployment-runbook view)

| Obligation | Healthy interval | What "proven" means |
|---|---|---|
| `cover_pool_reverify_sweep_ran` | ≤ 1h | Reverify sweep ran successfully |
| `cover_pool_rotation_sweep_ran` | ≤ 1h | Rotation sweep ran successfully |
| `cover_pool_reverify_pass_proven` | ≤ `reverify_after_days × 2` (default 60d) | Operator (or spec I) actually attested at least one verification |
| `cover_pool_replenishment_proven` | ≤ `replenishment_interval_days` (default 90d) | Operator actually added at least one candidate |
| `cover_pool_rotation_frozen` | absent | Verified pool ≥ `freeze_threshold` |
| `cover_pool_rotation_pending` (per-domain) | absent or cleared within rotation-response SLA | No domain past its rotation TTL |
