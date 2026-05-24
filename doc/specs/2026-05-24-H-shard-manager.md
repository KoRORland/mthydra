# Spec H — Shard Manager

Status: **Draft, awaiting operator review.**
Predecessor: `doc/design.md` §11 (T6), `doc/specs/2026-05-18-A-controller-state-and-backup.md` §4.5 (shards/users tables), `doc/specs/2026-05-20-F-eu-node-setup.md` (active/standby), `doc/specs/2026-05-21-G-ru-provisioning-artifact-generator.md` (ru-boxes lifecycle), `doc/specs/2026-05-19-C-cover-domain-pool-manager.md` (rotation seam).
Successors blocked on this: `K` (user distribution channel — publishes per-shard subsets).

---

## 1. Purpose

Define the controller's shard manager: the state machine that governs each shard from creation through retirement, the structural enforcement of **shard disjointness** (one RU box serves exactly one shard), the reshuffle scheduler that breaks the durable user↔box fingerprint, and the compromise-driven reshuffle hook that responds to T3 Job-2 terminations. Spec H is the *who-is-on-which-box* policy layer that turns T6 from a discipline-only control into a controller-enforced one.

Spec A established the schemas (`shards`, `users`, `published_subsets`) and the thin repository `state/users_shards.py`. Spec H completes T6: it formalizes the shard state machine, adds the SQLite triggers that make multi-shard box assignment structurally impossible, runs the reshuffle scheduler, integrates with `ru-box-terminate --reason=compromise`, and surfaces the §12 obligations that prove disjointness + reshuffle cadence are actually being honoured.

Out of scope: publishing per-shard subsets to users (spec K), automated rotation/replacement of boxes (deferred — spec H exposes a `request_replacement()` hook that spec G/H integration will later call), per-shard credentials beyond the existing per-box `onward_credentials` (the design's "per-shard credential" point is satisfied by published-subset partitioning — a user only ever sees their shard's box set — not by adding a new credential layer).

---

## 2. Locked design decisions

Approved during brainstorming session 2026-05-24.

| ID | Decision | Rationale |
|---|---|---|
| H-D1 | **Shard size policy = operator-configured target with hard cap.** `[shard_manager] target_size = 2`, `max_size = 3`. The reshuffle scheduler aims for `target_size`; `assign_user_to_shard` refuses to exceed `max_size`. Operators with a sub-`target_size` circle (e.g., 3 users with `target_size=2`) accept size-1 shards. | Design.md §11: shard size is the operator's blast-radius knob. Refusing to optimise it automatically because no good rule exists; the design degrades gracefully and the operator must pick consciously (§11 residual #2). |
| H-D2 | **Shard disjointness on boxes = SQLite trigger.** A trigger on `ru_boxes` rejects updates that would set `shard_id` while the box is not in `state='provisioning'` and is already shard-assigned. Re-assignment of a non-provisioning box to a different shard is forbidden; the supported transition is `terminated → new box → fresh shard_id`. | Design.md §11 residual #4: "must be structurally enforced by the controller (refuse multi-shard assignment), not left to operator care — same lesson as T5's burned-set rule." Mirrors spec C's burned-set trigger pattern. |
| H-D3 | **Reshuffle = atomic regeneration of one shard.** Reshuffle retires the old `shards` row (sets `retired_at`), creates a *new* `shards` row with a fresh `shard_id`, redistributes members per H-D1, updates `users.current_shard_id`. Boxes are NOT touched by reshuffle — they expire by their own rotation TTL (spec C `cover_pool_rotation_pending`). The published subset (spec K) re-emits per the new shard layout on the next publish. | Decouples shard membership rotation from box rotation. The two were originally intertwined in design.md §11 #3 ("rides the existing replace-on-burn"), but operationally they are separate clocks: spec H rotates *who is in which shard*; spec C/G rotates *which boxes the fleet uses*. Keeping the clocks separate keeps either one observable on its own. |
| H-D4 | **Reshuffle scheduler is fully automated.** `[shard_manager] reshuffle_interval_days = 14`. An APScheduler job sweeps every `reshuffle_sweep_interval` (default 1h); for each shard past TTL, performs H-D3 in-transaction. No operator gate. | Design.md §11 #3 explicitly says "rotated on a timer." Spec C made *rotation* operator-driven because it required a replacement-box pipeline (not built); reshuffle is just relabeling and has no such dependency, so it can be fully automated from day one. |
| H-D5 | **Compromise-driven reshuffle = synchronous hook in `ru-box-terminate`.** When `ru-box-terminate --reason=compromise` runs, immediately after the box transitions to `terminated`, the box's shard is reshuffled (H-D3) regardless of TTL. Other termination reasons (`manual_rotate`, `aged_out`, `health`) do NOT trigger reshuffle. | Design.md §11 #5: "compromise of one shard's box → terminate + replace + reshuffle that shard." Tying the hook to the `reason='compromise'` literal keeps benign terminations cheap and makes the dangerous-path response automatic. |
| H-D6 | **Reshuffle picks new `shard_id` as fresh UUID-7.** Old shard_id is never re-used. | Design.md §11 #3: even the user↔shard relationship must not be a durable handle. A fresh shard_id every reshuffle removes the last persistent label. |
| H-D7 | **Per-shard credentials = published-subset partitioning, not a new credential type.** A user only ever receives connection info for boxes whose `shard_id == users.current_shard_id`. The box's per-box `onward_credentials` row is unchanged. | Design.md §11 #4 reads "per-shard credentials/parameters, not per-user-global ones." The existing per-box scheme satisfies this *if and only if* the published view is partitioned by shard. Spec H provides `list_shard_boxes(shard_id)` for spec K to consume; no new credential storage. |
| H-D8 | **`audit_log` row per shard transition.** Every shard create, retire, reshuffle, user assignment, box assignment writes an `audit_log` row capturing `(actor, action, target, prev_state, new_state, reason)`. | Matches specs A/B/C/D discipline. Reshuffle history must survive backup/restore so post-mortem analysis after a compromise can answer "who was on this box on date X?" |
| H-D9 | **`unassigned` users are first-class.** `users.current_shard_id` may be `NULL` (newly added, between reshuffles). `unassigned` users do not appear in any published subset; the scheduler picks them up on next sweep. | Avoids the "ghost user" failure mode where a user added between reshuffles never gets assigned. Sweep is the authority. |

---

## 3. Schema additions (v6 → v7)

### 3.1 No new tables

All the tables Spec H needs already exist (Spec A): `shards`, `users`, `ru_boxes`, `audit_log`. Spec H reuses them.

### 3.2 Columns to add

- `shards.target_size INTEGER` — captured at shard creation; used by reshuffle to know what size to aim for. Defaulted from config on create; NULL on legacy rows is treated as the current `[shard_manager].target_size`.

### 3.3 Triggers — structural enforcement of H-D2

```sql
CREATE TRIGGER IF NOT EXISTS ru_boxes_no_cross_shard_reassign
BEFORE UPDATE OF shard_id ON ru_boxes
WHEN OLD.shard_id IS NOT NULL
 AND OLD.shard_id IS NOT NEW.shard_id
 AND OLD.state != 'provisioning'
BEGIN
  SELECT RAISE(ABORT, 'shard-manager: live/terminated boxes cannot change shard_id');
END;
```

A second trigger pairs with H-D2 to refuse assigning a `terminated` box's shard_id to non-NULL during the terminate transition:

```sql
CREATE TRIGGER IF NOT EXISTS ru_boxes_terminated_keeps_shard
BEFORE UPDATE OF state ON ru_boxes
WHEN NEW.state = 'terminated'
 AND NEW.shard_id IS NULL
 AND OLD.shard_id IS NOT NULL
BEGIN
  SELECT RAISE(ABORT, 'shard-manager: terminating a box does not clear shard_id (history preservation)');
END;
```

Rationale: post-mortem analysis after a compromise must answer "who was on this box?" The shard_id on a terminated box is part of the audit trail.

### 3.4 Schema version bump

`SCHEMA_VERSION = 7`; `migrate_v6_to_v7(conn)` adds the `target_size` column (idempotent ALTER) and installs the two triggers.

---

## 4. State machine — shards

```
            create_shard
         ───────────────→  active
                              │
                              │ reshuffle_due (TTL or compromise)
                              ▼
                          retiring  (no new boxes assigned, no new members joined)
                              │
                              │ reshuffle complete (members migrated to new shard)
                              ▼
                           retired  (retired_at set; immutable; kept for audit)
```

- **active**: `retired_at IS NULL`. Users may be assigned; boxes may be paired.
- **retiring**: a synthetic in-transaction state during reshuffle. Not stored — the transition is atomic in one SQL transaction.
- **retired**: `retired_at IS NOT NULL`. The row stays for audit; no new assignments. `current_shard_id` of users that pointed to it is updated to the new shard in the same transaction.

There is no transition out of `retired`. Reshuffle does not modify the retired row — it creates a new active row.

---

## 5. Repository API

In `src/mthydra/controller/state/shards.py` (new module — split from `users_shards.py`, which keeps users-only concerns):

```python
@dataclass(frozen=True)
class Shard:
    shard_id: str
    members_json: str            # JSON list[str] of user_ids
    target_size: int | None
    last_reshuffled_at: str
    created_at: str
    retired_at: str | None

@dataclass(frozen=True)
class ShardHealth:
    total_active: int
    total_retired: int
    oldest_active_age_seconds: int
    overdue_for_reshuffle: list[str]   # shard_ids past TTL
    unassigned_users: list[str]

def create_shard(conn, *, shard_id: str, members: list[str], target_size: int,
                 at: str) -> None: ...
def retire_shard(conn, shard_id: str, at: str) -> None: ...
def list_active(conn) -> list[Shard]: ...
def get_shard(conn, shard_id: str) -> Shard: ...
def list_shard_boxes(conn, shard_id: str, *, include_terminated: bool = False) -> list[str]: ...
def assign_box_to_shard(conn, box_id: str, shard_id: str, at: str) -> None: ...
def reshuffle(conn, shard_id: str, *, now: str, target_size: int,
              new_shard_id: str, reason: str) -> str:
    """Atomic. Retires old shard, creates new shard with redistributed members,
    updates users.current_shard_id. Returns new_shard_id. Audit row per move."""
def health(conn, *, now: str, reshuffle_interval_seconds: int) -> ShardHealth: ...
```

In `src/mthydra/controller/state/users_shards.py` (existing — extend):

```python
def assign_user_to_shard(conn, user_id: str, shard_id: str, *,
                         at: str, max_size: int) -> None:
    """Refuses if shard already at max_size. Writes audit row."""
def unassigned_users(conn) -> list[str]: ...
def reshuffle_unassigned(conn, *, now: str, target_size: int,
                         shard_id_factory: Callable[[], str]) -> list[str]:
    """Group unassigned users into shards of size <= target_size; create them."""
```

All mutating functions emit `audit_log` rows. Concurrency is whatever SQLite serialization provides (single-writer); these helpers do not take a separate lock.

---

## 6. Startup invariants (extend `state/invariants.py` with #33–#36)

| ID | Statement | Failure → |
|---|---|---|
| #33 | Every `ru_boxes` row with `state='live'` has `shard_id IS NOT NULL`. | `live box without shard assignment` |
| #34 | No `users` row references a `shard_id` whose `retired_at IS NOT NULL`. | `user assigned to retired shard` (must be reshuffled forward) |
| #35 | No two active shards (where `retired_at IS NULL`) share any user_id in `members_json`. | `cross-shard user membership` (structurally impossible if the helpers are used; invariant guards against direct SQL) |
| #36 | Every active shard's `members_json` list is non-empty (an empty active shard is a controller bug — should have been retired). | `empty active shard` |

These extend the spec-E set; the invariant module already has 32 entries.

---

## 7. Schedulers & obligations

### 7.1 `shard_reshuffle_sweep`

- Cadence: `reshuffle_sweep_interval` (default `1h`)
- Action:
  1. `health(conn, now=…, reshuffle_interval_seconds=…)`.
  2. For each `shard_id` in `overdue_for_reshuffle`: call `reshuffle(...)` with `reason='ttl'`, picking new members via the helper in §7.2.
  3. If `unassigned_users` non-empty: call `reshuffle_unassigned(...)` to fold them into new shards (size ≤ `target_size`).
- Heartbeat: obligation `shard_reshuffle_sweep_ran` proven each tick.

### 7.2 Reshuffle membership picker

Pure helper, no I/O. Input: current shard rosters + a pool of unassigned users + `target_size`. Output: new shard rosters.

- **Algorithm:** shuffle the union of (current shard members of the overdue shard + unassigned users) using `random.SystemRandom().shuffle()`; chunk into groups of `target_size` (last group may be smaller). Each chunk becomes a new shard.
- **Anti-correlation note:** the picker MUST NOT preserve previous co-membership (i.e., the same two users should not always end up together). Using SystemRandom over the full pool — not a stable hash — satisfies this. Honest residual: with a very small circle (size 3, `target_size=2`), the probability that pairs re-occur is high. Stated openly; the only fix is more reshuffles, which the operator can tune.

### 7.3 §12 obligation contributions

Added to the obligation registry seeded by `controller.bootstrap`:

| Obligation key | Cadence / semantics |
|---|---|
| `shard_reshuffle_sweep_ran` | hourly heartbeat — sweep is alive |
| `shard_reshuffle_proven` | proven on every successful reshuffle (TTL or compromise); healthy interval = `reshuffle_interval_days × 2` |
| `shard_disjointness_check_proven` | proven on every invariant run; healthy interval = `1d` — confirms #33–#36 hold |
| `shard_overdue_pending` (per-shard key) | anti-obligation; one row per shard past TTL; cleared automatically by next reshuffle sweep. Surfaces "scheduler is stuck or shards are growing faster than they reshuffle" |
| `shard_unassigned_pending` (per-user key) | anti-obligation; one row per unassigned user older than `reshuffle_interval_days`; cleared by next sweep |

### 7.4 Config additions

New `[shard_manager]` section in `controller.toml`:

```toml
[shard_manager]
target_size              = 2
max_size                 = 3
reshuffle_interval_days  = 14
reshuffle_sweep_interval = "1h"
```

Loaded into a `ShardManagerConfig` dataclass; defaults shipped in `packaging/etc/mthydra/controller.toml.example`. Tuning named as a deployment-runbook obligation (see §11 residual #1).

---

## 8. CLI surface

Added to `mthydra-controller` via `src/mthydra/controller/cli.py`. All take `--db-path`; structured commands accept `--json`.

```
mthydra-controller user-add <user_id> --out-of-band-channel <text> \
    [--display-name <text>]
    # state: ∅ -> users row; current_shard_id NULL until next sweep

mthydra-controller user-list [--json]
    # prints users + their current_shard_id (or "<unassigned>")

mthydra-controller shard-create <shard_id> --members <user_id>,<user_id>,...
    # explicit-membership create. Refuses if any user already in an active shard.
    # Mostly used for bootstrap; the sweep handles ongoing creation.

mthydra-controller shard-list [--include-retired] [--json]
    # prints active (and optionally retired) shards + member count + age

mthydra-controller shard-show <shard_id> [--json]
    # full detail including all member user_ids and assigned box_ids

mthydra-controller shard-assign-box <box_id> --shard <shard_id>
    # box must be in 'provisioning' state; trigger enforces. Updates ru_boxes.shard_id.

mthydra-controller shard-reshuffle <shard_id> [--reason <text>]
    # operator-driven reshuffle (out-of-band; sweep does scheduled ones)
    # default --reason='operator_manual'

mthydra-controller shard-stats [--json]
    # counts + overdue list + unassigned list + last_sweep_at
```

`ru-box-terminate --reason=compromise` (spec G command) gains the synchronous reshuffle hook per H-D5; no new CLI for that path — it is built into the existing terminate command.

---

## 9. Test plan

### 9.1 Unit tests — `tests/unit/controller/state/test_shards.py`

- `create_shard` + `retire_shard` + `list_active` happy path
- `assign_box_to_shard` succeeds when box is `provisioning`; raises when box is `live` and `shard_id` differs (trigger fires)
- `reshuffle` atomically retires old + creates new + migrates users; old shard_id never reused; audit rows written
- `list_shard_boxes` filters by `retired_at IS NULL` of the *shard* and `state IN ('provisioning','live')` of the *box*
- `health(...)` returns the expected overdue/unassigned lists given a mock clock

### 9.2 Users tests — extend `tests/unit/controller/state/test_users_shards.py`

- `assign_user_to_shard` refuses when shard at `max_size`
- `unassigned_users` lists rows with `current_shard_id IS NULL`
- `reshuffle_unassigned` groups them into ≤ `target_size` chunks

### 9.3 Trigger tests — `tests/unit/controller/state/test_shards_triggers.py`

- Attempting to UPDATE `ru_boxes` to change `shard_id` while state ≠ `'provisioning'` raises `IntegrityError`
- Attempting to set `shard_id` to `NULL` on a terminating row raises
- Terminating with `shard_id` retained succeeds; the row survives in audit

### 9.4 Invariant tests — extend `tests/unit/controller/test_invariants.py`

- #33 fires when a `live` box has `shard_id IS NULL`
- #34 fires when a user references a retired shard
- #35 fires when two active shards share a user (insert raw SQL bypassing helpers)
- #36 fires when an active shard's `members_json` is `[]`

### 9.5 Scheduler tests — `tests/unit/controller/test_shard_scheduler.py`

- Mock clock; advance past `reshuffle_interval_days`; sweep reshuffles exactly the overdue shards
- Unassigned users are folded into new shards by the same sweep
- Sweep heartbeat obligation `shard_reshuffle_sweep_ran` is proven each tick
- `shard_overdue_pending` rows appear before sweep, disappear after

### 9.6 Property test — `tests/property/test_shard_invariants.py`

Hypothesis strategy: generate sequences of (add_user, create_shard, assign_box, reshuffle, terminate-compromise) operations. After each operation:
- No active shard has > `max_size` members
- No two active shards share a user
- Every live box has `shard_id` set
- Reshuffle never re-uses a previous shard_id (track history in the test)

### 9.7 Integration test — `tests/integration/test_shard_lifecycle.py`

End-to-end:
1. Bootstrap controller; add 6 users; create 3 shards of 2 via `shard-create`.
2. Provision 3 boxes via spec-G `provision-seed`; `shard-assign-box` pairs each to one shard.
3. `ru-box-mark-live` for each.
4. Advance clock past `reshuffle_interval_days`; run sweep; assert all 3 shards retired + 3 new shards exist + every user has a new `current_shard_id`.
5. `ru-box-terminate --reason=compromise` one box; assert that box's shard reshuffled immediately (out of TTL band); other shards untouched.
6. Verify audit log records: 6 reshuffles total (3 TTL + 1 compromise + 2 unassigned-folds expected? — depends on exact sequencing; assert ≥ 4 reshuffle audit rows).

### 9.8 CLI tests — extend `tests/unit/controller/test_cli.py`

- `user-add` + `user-list --json` round-trip
- `shard-create` + `shard-list --json` round-trip
- `shard-assign-box` happy + refuses for `live` box
- `shard-reshuffle` writes audit row, creates new shard_id

### 9.9 Failure-mode catalogue

| Failure | Behaviour |
|---|---|
| Operator tries to reassign a live box to a different shard | Trigger raises `IntegrityError`; CLI exit 2 with `shard-manager: live/terminated boxes cannot change shard_id` |
| Operator tries to assign user to a shard already at `max_size` | `assign_user_to_shard` raises `ValueError`; CLI exit 2 |
| Reshuffle races a `ru-box-terminate --reason=compromise` | Both serialize through SQLite; whichever commits first wins. Compromise-reshuffle is idempotent: if the shard was just reshuffled, the new shard's box is the one terminated, and a second reshuffle simply makes a third shard. Honest residual: rapid double-compromise causes shard churn. Bounded by the rate at which boxes can actually be replaced. |
| Sweep runs against an empty fleet (no users, no shards) | No-op; obligation `shard_reshuffle_sweep_ran` still proven |
| User added between sweeps | `current_shard_id` stays NULL; next sweep folds them in. `shard_unassigned_pending` obligation row appears if they linger > `reshuffle_interval_days` |
| Clock skew at sweep boundary | Same handling as spec C — wall-clock based, accepted residual (§11 #5) |

---

## 10. Cross-spec contracts

| Consumer | What spec H exposes | Notes |
|---|---|---|
| **Spec G** (provisioning) | `assign_box_to_shard(conn, box_id, shard_id, at)` — called by `provision-seed` after creating the new `ru_boxes` row, before the box is marked `live`. | The CLI subcommand `provision-seed` gains a required `--shard <shard_id>` argument; until spec K, the operator picks. The trigger blocks all non-provisioning-state assignment, so spec G must call this before `mark-live`. |
| **Spec G** (termination) | The synchronous hook in `ru-box-terminate --reason=compromise` calls `reshuffle(..., reason='compromise')` after the box transitions to `terminated`. | The shard_id of the terminated box is retained (H-D2 trigger keeps it). The hook reads it, computes the new shard, performs the swap, all in one transaction with the termination. |
| **Spec C** (cover pool) | No coupling beyond what existed: shard rotation does not consume cover domains directly; box rotation (spec C) consumes them. | Honest residual: the design.md §11 #3 statement "reshuffle rides the existing replace-on-burn/rotation mechanism" is now decoupled — spec H reshuffles members, spec C rotates boxes. The combined system still achieves the design's goal because users continue to migrate across boxes (via shard rotation) AND boxes continue to rotate independently. Two clocks, one outcome. |
| **Spec K** (distribution) | `list_shard_boxes(shard_id)` is the read API. Spec K builds per-user published subsets by joining `users.current_shard_id` → `list_shard_boxes`. | Spec K filters retired-shard memberships out via the §6 invariants. |
| **Spec B/E** (descriptor / data-plane) | None. The descriptor is global (all EU exits); shards do not affect descriptor content. Per-shard partitioning happens on the *distribution* side (spec K), not the *signing* side. | Verified: no descriptor field references shard_id. |

---

## 11. Honest residuals (Spec H)

1. **Shard size is a real cost/safety trade with no free optimum.** Smaller shards = more boxes/cost/pool consumption (spec C); larger shards = more exposure per compromise. The operator must pick `target_size` and `max_size` consciously, and the deployment runbook must call out re-tuning under load. Spec H does not auto-tune.
2. **Reshuffle frequency trades fingerprint-resistance against churn.** Too long → user↔shard becomes a durable handle; too short → operational load. Another conscious tunable; `reshuffle_interval_days` defaults to 14 because that matches a typical box-lifetime ceiling, but a paranoid operator should consider 7.
3. **Very small circles degrade silently.** With 3 users and `target_size=2`, the shard layout is `{A,B}` and `{C}` (or some 2+1 partition). After reshuffle, the same partition is one of only 3 distinct possibilities — so pair-correlation across reshuffles is bounded below by `1/3`, not negligible. Reshuffle must be more frequent for tiny circles; the design (§11 residual #4) names this and spec H does not solve it.
4. **Box-rotation (spec C) and shard-rotation (spec H) are now two independent clocks.** Both must run for the combined T6 + T5 guarantee. If only one runs, the failure mode is different: shard-only rotation rotates members on stale boxes; box-only rotation rotates boxes but freezes the user↔shard mapping. Spec H's §12 obligations surface the shard side; spec C's surface the box side. Composition is the operator's responsibility.
5. **Clock skew across TTL boundaries.** Reshuffle TTL uses the controller's wall clock; a large NTP fault could mis-classify a shard's reshuffle eligibility by minutes. TTLs are measured in days; impact is negligible. Same accepted residual as spec C.
6. **`reshuffle()` is not transactional across a B2 backup.** A reshuffle commits to SQLite immediately; the off-box backup (spec A) catches it on the next debounce window. A controller crash between commit and backup means the backup may show the previous shard layout. Acceptable because the controller can re-derive shard state from its current `shards` table — the backup is for full-loss recovery, not transactional consistency.
7. **Multi-shard accidental commingling is forbidden but not detected post-mortem.** The trigger refuses *creation* of the forbidden state; invariant #35 detects it at startup. There is no continuous online check — if a future code path bypasses both, the bad state could persist between startups. Mitigation: every helper goes through the audited API; raw SQL is a discipline-only foot-gun.
8. **Compromise reshuffle assumes the shard composition is now adversary-known.** This is the design's premise (§11 #5). If the compromise was actually a false-positive Job-2 kill, the reshuffle was unnecessary and the user↔shard mapping was unnecessarily churned. Cost is operational, not safety; we err on reshuffling.

---

## 12. §12 obligation summary (deployment-runbook view)

| Obligation | Healthy interval | What "proven" means |
|---|---|---|
| `shard_reshuffle_sweep_ran` | ≤ 1h | Sweep ran successfully |
| `shard_reshuffle_proven` | ≤ `reshuffle_interval_days × 2` (default 28d) | At least one reshuffle (TTL or compromise) committed; surfaces "the reshuffle clock is actually advancing" |
| `shard_disjointness_check_proven` | ≤ 1d | Invariant run confirmed #33–#36 hold |
| `shard_overdue_pending` (per-shard) | absent or cleared within sweep interval | No shard past its reshuffle TTL |
| `shard_unassigned_pending` (per-user) | absent or cleared within sweep interval | No user lingering unassigned longer than `reshuffle_interval_days` |

**Deployment runbook addition:** the operator must (a) pick `target_size`/`max_size`/`reshuffle_interval_days` consciously per §11 residuals #1–#3, (b) check that *both* `shard_reshuffle_proven` *and* `cover_pool_rotation_pending` are advancing — neither alone is sufficient for the combined T5 + T6 guarantee (§11 #4).
