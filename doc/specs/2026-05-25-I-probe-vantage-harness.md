# Spec I — Probe Vantage Harness

Status: **Draft, awaiting operator review.**
Predecessor: `doc/design.md` §8 (T3 Job 1, Job 2), the **Open** T7 entry, `doc/specs/2026-05-19-C-cover-domain-pool-manager.md` (C-D1 vantage label coupling), `doc/specs/2026-05-21-D-ru-image-build-pipeline.md` (image versioning), `doc/specs/2026-05-21-G-ru-provisioning-artifact-generator.md` (box lifecycle), `doc/specs/2026-05-24-H-shard-manager.md` (compromise-reshuffle hook).
Successors blocked on this: `J` (observability — queries probe results), `K` (distribution — only publishes boxes that have passed recent probes).

---

## 1. Purpose

Define the controller's **probe vantage harness**: the registry that catalogs every Russia-approximating probe vantage (its lifecycle, rotation discipline, and burnt-set carve-out for vantages that became attributable), the **pinned known-good profile** keyed by image_version, the **probe-result ingest path** that records every per-(box, vantage, cycle) check outcome, the **kill-decision evaluator** that implements §8 Job 2's hard-kill + N-of-M soft-fail rule, and the audit scheduler that surfaces kill-pending obligations + coverage-gap obligations.

Spec I closes the **#4 gap** ("Russia-approximating probe vantage harness") and turns T3 Job 1 + Job 2 from drafted-but-unreal into a controller-enforced control. Spec C (C-D1) already named spec I as the planned upgrade path for cover-domain attestation; spec I delivers the API spec C calls (with no schema change to `cover_domain_pool`).

**Out of scope:** automated probe *execution* from real disposable RU-approximating vantages (deployment-runbook discipline; the operator runs probes from configured vantages and pipes results into `probe-record`). Vantage *sourcing* — finding and provisioning genuinely disposable, non-attributable vantages — is also operator work; spec I provides the registry and discipline, not the procurement. **Automated termination** on hard-kill is also deferred: the harness emits a `probe_kill_pending` obligation that the operator acts on with `ru-box-terminate --reason=compromise` (which already triggers spec H's compromise-reshuffle). This mirrors the C-D1 pattern — ship the seams, ship operator-driven workflow, let automation slot in later without schema change.

---

## 2. Locked design decisions

Approved during brainstorming session 2026-05-25.

| ID | Decision | Rationale |
|---|---|---|
| I-D1 | **Probe execution is operator-driven for MVP; result *ingest* is automated via CLI.** A CLI `probe-record --box-id <id> --vantage <name> --check <type> --status <pass|soft_fail|hard_fail> --evidence <text-or-path> --cycle-at <iso>` is the only path that inserts rows into `probe_results`. | Spec C established the pattern (C-D1) of "operator-attested for MVP, structurally swappable later." Job 1 actually executing probes from RU-approximating vantages is an operations problem (vantage sourcing, network egress, payload generation) that the controller does not solve. The harness's value is the *bookkeeping discipline* — what was checked, when, from where, with what result — not the network bytes. |
| I-D2 | **Vantage state machine = `candidate → active → retired`, with `burned` carve-out.** Vantages enter as `candidate` (operator added, not yet attested as Russia-approximating); operator runs `vantage-attest-active` after confirming it sees what a real user sees; rotation expires vantages from `active` to `retired` (TTL); operator may force `burned` via `vantage-burn` if the vantage's identity leaked or the provider became attributable (e.g. cloud egress now flagged). `burned` is monotonic — never reused, no CLI affordance to undo, same discipline as `cover_domain_pool → burned_domains`. | Design §8 demands rotation and non-attributability. The same lesson as T5: structurally enforce, don't policy-enforce. A `burned` carve-out captures the "this vantage's egress IP is now in adversary fingerprint lists" failure mode. |
| I-D3 | **Probe results stored in a separate append-only `probe_results` table; one row per (box_id, vantage_id, cycle_at, check_type).** Evidence is captured verbatim in `evidence_json` so audit/post-mortem can rebuild context. | Append-only mirrors `audit_log` and `burned_domains`. Per-check-type granularity (not per-cycle) is required by §8's six different check methods — surfacing *which* check failed is essential to telling "broken cover" from "broken transport." |
| I-D4 | **The kill-decision evaluator is pure: `evaluate_box(conn, box_id, cfg, now) -> EvaluationResult`.** Reads `probe_results` for the box; applies §8 Job 2 rules; returns one of `{healthy, soft_pending, hard_kill, soft_threshold_reached}` plus the offending check(s) and the evidence pointer. No side effects. | The scheduler decides what to do with the result (emit an obligation, log, etc.). Pure evaluator means tests can assert decisions against arbitrary probe-result histories without DB plumbing per test. |
| I-D5 | **Hard-kill is single-occurrence-across-any-vantage; soft-fail debounce is N-of-M across *rotated* vantages.** `cfg.probe.soft_fail_M = 4`, `cfg.probe.soft_fail_N = 3`. The evaluator counts only the last M cycles, distinct vantages, and requires N hard-or-soft fails to fire `soft_threshold_reached`. **At least 2 distinct vantages** must contribute to the count — a single noisy vantage cannot trigger a kill. | Design §8 directly states the N=3-of-M=4 starting point. The "distinct vantages" requirement is *added* by spec I as the structural anti-flake guard the design only hinted at via "across rotated vantages." |
| I-D6 | **Hard-kill emits `probe_kill_pending::<box_id>` obligation; operator runs `ru-box-terminate --reason=compromise` to clear it.** The CLI may grow a `--auto-terminate` flag later (post-MVP) once the evaluator's false-positive rate has been characterised against real probe data. | Operator-driven termination matches C-D1 + the design's "kill, never investigate" rule (Job 2). The compromise-reshuffle (spec H H-D5) already fires the moment terminate runs, so the user-facing protection ships intact; only the human-in-the-loop changes. Honest residual: this adds latency before kill. Listed in §11. |
| I-D7 | **Pinned known-good profile is stored in a new `image_profiles` table keyed by `image_version` (FK to `ru_images`).** Profile contents are a JSON blob with the §8 schema (TLS handshake characteristics, malformed-input timing/content, expected surface :443-only, latency baseline ranges, transport build hash). Spec D's `image-promote` will be extended in a follow-up to require a pinned profile before promotion — for spec I MVP, the profile is set via `profile-pin <image_version> --profile-json <path>` and the linkage is enforced only at probe-evaluation time (a box whose `image_version` has no profile cannot be probed). | The profile travels with the image, per §8: "image version + transport build hash, so 'expected behaviour' changes *deliberately* on a refresh (T4)." The follow-up amendment to spec D is named here so the coupling is explicit; spec I does not modify spec D's tables. |
| I-D8 | **Probe coverage is a §12 obligation: every live box must have ≥ 1 probe cycle within `probe_coverage_window_seconds` (default 1h).** Surfaced as a per-box `probe_coverage_pending::<box_id>` anti-obligation when a live box exceeds the window without any new probe_results row. Cleared automatically by the next ingest. | Without this, a stalled probe vantage produces silent false greens — the design's *exact* T3 failure mode (§357: "Vantage quality is decisive and fragile"). The obligation makes "no probes happened" loud. |
| I-D9 | **Vantage rotation TTL = `probe_vantage_ttl_days` (default 14d).** Active vantages past TTL are flagged for retirement; operator runs `vantage-retire` to confirm. No automated retirement: the operator may also explicitly extend a vantage that is still working (the TTL is a *prompt*, not a deadline). | Same pattern as spec C's rotation TTL — a structural prompt, operator-driven action, not silent automation. |
| I-D10 | **`audit_log` row per vantage transition + per kill-pending decision.** | Matches every prior spec. |

---

## 3. Schema additions (v7 → v8)

### 3.1 `probe_vantages` table

```sql
CREATE TABLE probe_vantages (
  vantage_id       TEXT PRIMARY KEY,
  label            TEXT UNIQUE NOT NULL,
  source_kind      TEXT NOT NULL,           -- 'tor', 'cloud-ru-adjacent', 'vps-cis', 'manual', etc.
  region_hint      TEXT,                    -- free-text e.g. 'KZ-almaty', 'BY', 'transit-ldn'
  state            TEXT NOT NULL CHECK (state IN ('candidate','active','retired','burned')),
  added_at         TEXT NOT NULL,
  attested_at      TEXT,                    -- when state went candidate -> active
  last_used_at     TEXT,                    -- updated on every probe_results insert
  retired_at       TEXT,
  burned_at        TEXT,
  burn_reason      TEXT,
  notes            TEXT
);

CREATE INDEX ix_probe_vantages_state ON probe_vantages(state);
```

### 3.2 `probe_results` table (append-only)

```sql
CREATE TABLE probe_results (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  box_id        TEXT NOT NULL,
  vantage_id    TEXT NOT NULL,
  cycle_at      TEXT NOT NULL,
  check_type    TEXT NOT NULL CHECK (check_type IN (
                  'tls_fall_through','cover_domain_consistency','surface_scan',
                  'valid_path_liveness','latency_loss','behavioural_identity'
                )),
  status        TEXT NOT NULL CHECK (status IN ('pass','soft_fail','hard_fail')),
  evidence_json TEXT,
  image_version TEXT NOT NULL,              -- the image the box was running at probe time
  recorded_at   TEXT NOT NULL,
  FOREIGN KEY (box_id) REFERENCES ru_boxes(box_id),
  FOREIGN KEY (vantage_id) REFERENCES probe_vantages(vantage_id),
  FOREIGN KEY (image_version) REFERENCES ru_images(image_version)
);

CREATE INDEX ix_probe_results_box_cycle ON probe_results(box_id, cycle_at DESC);
CREATE INDEX ix_probe_results_vantage ON probe_results(vantage_id, cycle_at DESC);
```

### 3.3 `image_profiles` table

```sql
CREATE TABLE image_profiles (
  image_version  TEXT PRIMARY KEY REFERENCES ru_images(image_version),
  profile_json   TEXT NOT NULL,
  recorded_at    TEXT NOT NULL,
  recorded_by    TEXT NOT NULL,             -- operator name / evidence
  notes          TEXT
);
```

### 3.4 Triggers — structural enforcement

```sql
-- probe_vantages: never reuse a burned label as a new candidate
CREATE TRIGGER probe_vantages_no_relabel_burned
BEFORE INSERT ON probe_vantages
WHEN EXISTS (SELECT 1 FROM probe_vantages WHERE label = NEW.label AND state='burned')
BEGIN
  SELECT RAISE(ABORT, 'probe-vantage: label is in burned state; never reuse');
END;

-- probe_vantages: burned is monotonic (no UPDATE OUT of burned)
CREATE TRIGGER probe_vantages_burned_no_revert
BEFORE UPDATE OF state ON probe_vantages
WHEN OLD.state='burned' AND NEW.state != 'burned'
BEGIN
  SELECT RAISE(ABORT, 'probe-vantage: burned state is monotonic');
END;

-- probe_results: rows are append-only (no UPDATE / DELETE)
CREATE TRIGGER probe_results_no_update
BEFORE UPDATE ON probe_results
BEGIN
  SELECT RAISE(ABORT, 'probe-results: append-only');
END;
CREATE TRIGGER probe_results_no_delete
BEFORE DELETE ON probe_results
BEGIN
  SELECT RAISE(ABORT, 'probe-results: append-only');
END;
```

### 3.5 Schema version bump

`SCHEMA_VERSION = 8`; `migrate_v7_to_v8(conn)` creates the three tables + four triggers + two indexes (idempotent CREATE IF NOT EXISTS pattern).

---

## 4. Vantage state machine

```
         vantage-add
       ────────────→  candidate
                          │
                          │ vantage-attest-active (operator confirms RU-approximating)
                          ▼
                       active  ──── TTL exceeded ───→ (prompt: operator runs vantage-retire)
                          │
                          │ vantage-burn (vantage attributable; identity leaked)
                          ▼
                       burned  (immutable; trigger enforces)
                          ▲
                          │ vantage-retire (operator-driven; clean retire)
                       retired
```

- **candidate**: present in registry; not yet eligible for `probe-record` (rejected at ingest).
- **active**: eligible. `last_used_at` updates on each successful `probe-record`.
- **retired**: ineligible for new probes. Historical `probe_results` rows survive for audit.
- **burned**: ineligible AND label permanently poisoned. Same discipline as `burned_domains`.

---

## 5. Repository API

In `src/mthydra/controller/state/probe_vantages.py`:

```python
@dataclass(frozen=True)
class ProbeVantage:
    vantage_id: str
    label: str
    source_kind: str
    region_hint: str | None
    state: str
    added_at: str
    attested_at: str | None
    last_used_at: str | None
    retired_at: str | None
    burned_at: str | None
    burn_reason: str | None
    notes: str | None

def add_candidate(conn, *, vantage_id, label, source_kind, at,
                  region_hint=None, notes=None) -> None: ...
def attest_active(conn, vantage_id: str, *, at: str, evidence: str | None) -> None: ...
def retire(conn, vantage_id: str, *, at: str, reason: str | None) -> None: ...
def burn(conn, vantage_id: str, *, at: str, reason: str) -> None: ...
def list_by_state(conn, state: str | None = None) -> list[ProbeVantage]: ...
def get_vantage(conn, vantage_id: str) -> ProbeVantage: ...
def list_due_for_rotation(conn, *, now: str, ttl_days: int) -> list[str]: ...
```

In `src/mthydra/controller/state/probe_results.py`:

```python
@dataclass(frozen=True)
class ProbeResult:
    id: int
    box_id: str
    vantage_id: str
    cycle_at: str
    check_type: str
    status: str
    evidence_json: str | None
    image_version: str
    recorded_at: str

def record(conn, *, box_id, vantage_id, cycle_at, check_type, status,
           evidence_json, image_version, recorded_at) -> int: ...
def recent_for_box(conn, box_id: str, *, limit: int = 50) -> list[ProbeResult]: ...
def last_cycle_at(conn, box_id: str) -> str | None: ...
def distinct_vantages_in_window(conn, box_id: str, *, window_seconds: int,
                                 now: str) -> list[str]: ...
```

In `src/mthydra/controller/state/image_profiles.py`:

```python
@dataclass(frozen=True)
class ImageProfile:
    image_version: str
    profile_json: str
    recorded_at: str
    recorded_by: str
    notes: str | None

def pin(conn, *, image_version, profile_json, recorded_by, at,
        notes=None) -> None: ...
def get_profile(conn, image_version: str) -> ImageProfile | None: ...
def list_pinned(conn) -> list[ImageProfile]: ...
```

In `src/mthydra/controller/probe/evaluator.py` (pure, no DB write):

```python
@dataclass(frozen=True)
class EvaluationResult:
    box_id: str
    verdict: str             # 'healthy' | 'soft_pending' | 'hard_kill' | 'soft_threshold_reached'
    offending_checks: tuple[str, ...]
    distinct_vantages_consulted: int
    evidence_pointer: tuple[int, ...]      # probe_results.id refs

def evaluate_box(conn, *, box_id: str, cfg: ProbeConfig, now: str) -> EvaluationResult: ...
```

---

## 6. Startup invariants (extend `state/invariants.py` with #37–#40)

| ID | Statement | Failure → |
|---|---|---|
| #37 | No two `probe_vantages` rows share the same `label`. (Defence-in-depth on UNIQUE.) | `probe_vantage label dup` |
| #38 | Every `probe_results` row references a real `ru_boxes` row, a real `probe_vantages` row, and a real `ru_images` row. (FK plus a row-count assertion catches stale schema migrations.) | `probe_results orphan` |
| #39 | Every `ru_images` row in state `promoted` has a row in `image_profiles`. (Couples T3 to T4: a promoted image without a pinned profile makes every probe compare against the wrong reference. §357.) | `promoted image without pinned profile` |
| #40 | No two `probe_vantages` rows share a `vantage_id` in `state='active'` AND a `region_hint`. (Anti-fingerprint: two "active" vantages with the same region label is operationally fine, but if their region_hints are identical and they came from the same `source_kind`, the rotation is cosmetic. Surface as a soft invariant — warning, not raise.) | (warning only — see §11 residual #6) |

#40 is genuinely subtle. The MVP enforces only #37–#39 hard; #40 is logged but not raised. This is called out in §11.

---

## 7. Schedulers & obligations

### 7.1 `probe_audit_sweep`

- Cadence: `probe_audit_sweep_interval` (default `5m`)
- Action:
  1. For each `ru_boxes` row with `state='live'`: call `evaluate_box(...)`. Emit:
     - `probe_kill_pending::<box_id>` anti-obligation row when verdict is `hard_kill` or `soft_threshold_reached`. Includes offending_checks + evidence_pointer in `details`.
     - `probe_coverage_pending::<box_id>` anti-obligation row when `last_cycle_at` is older than `cfg.probe.coverage_window_seconds` (default 1h).
     - Cleared automatically on the next sweep when the verdict goes green / fresh probes arrive.
  2. For each `probe_vantages` row in `state='active'` past TTL: emit `probe_vantage_rotation_pending::<vantage_id>` anti-obligation row. Operator clears with `vantage-retire`.
- Heartbeat: obligation `probe_audit_sweep_ran` proven each tick.

### 7.2 §12 obligation contributions

| Obligation key | Cadence / semantics |
|---|---|
| `probe_audit_sweep_ran` | hourly heartbeat — sweep is alive |
| `probe_vantage_rotation_proven` | proven on every `vantage-attest-active` or `vantage-retire`; healthy interval = `probe_vantage_ttl_days × 2` (default 28d) — surfaces "operator is actually rotating vantages" |
| `probe_coverage_proven` | proven on every successful `probe-record`; healthy interval = `coverage_window_seconds × 2` (default 2h) — fleet-wide, not per-box; per-box gap surfaces via `probe_coverage_pending::*` |
| `probe_kill_pending::<box_id>` | anti-obligation; presence = box needs `ru-box-terminate --reason=compromise` (or `--auto-terminate` if enabled post-MVP) |
| `probe_coverage_pending::<box_id>` | anti-obligation; presence = box has not been probed within the window |
| `probe_vantage_rotation_pending::<vantage_id>` | anti-obligation; presence = vantage past TTL, operator must `vantage-retire` |

### 7.3 Config additions

New `[probe]` section in `controller.toml`:

```toml
[probe]
soft_fail_window_M           = 4
soft_fail_threshold_N        = 3
min_distinct_vantages        = 2
coverage_window_seconds      = 3600
probe_vantage_ttl_days       = 14
probe_audit_sweep_interval   = "5m"
```

Loaded into a `ProbeConfig` dataclass.

---

## 8. CLI surface

Added to `mthydra-controller` via `src/mthydra/controller/cli.py`. All take `--db-path`; structured commands accept `--json`.

```
mthydra-controller vantage-add <vantage_id> --label <name> \
    --source-kind <kind> [--region-hint <text>] [--notes <text>]
    # state: ∅ -> candidate

mthydra-controller vantage-attest-active <vantage_id> \
    [--evidence <text-or-path>]
    # state: candidate -> active. Captures evidence in audit_log.

mthydra-controller vantage-list [--state <state>] [--json]

mthydra-controller vantage-retire <vantage_id> [--reason <text>]
    # state: active -> retired

mthydra-controller vantage-burn <vantage_id> --reason <text>
    # state: {candidate, active, retired} -> burned (monotonic).
    # No undo (trigger enforces).

mthydra-controller profile-pin <image_version> \
    --profile-json <path-or-->> --recorded-by <text> [--notes <text>]
    # creates / overwrites the image_profiles row. Operator-attested.

mthydra-controller profile-show <image_version> [--json]

mthydra-controller probe-record --box-id <id> --vantage <id> \
    --check <type> --status <pass|soft_fail|hard_fail> \
    --cycle-at <iso> --evidence <text-or-path> [--image-version <ver>]
    # appends one row to probe_results. image_version defaults to
    # the box's current row; explicit flag wins.
    # Refuses if vantage state != 'active'.

mthydra-controller probe-evaluate --box-id <id> [--json]
    # runs evaluate_box and prints the verdict + offending checks.
    # Pure read; no side effects.

mthydra-controller probe-due [--json]
    # lists per-box probe_kill_pending + probe_coverage_pending + per-vantage
    # rotation_pending. The "what should I do?" preflight command.
```

No `vantage-unburn` subcommand. No `probe-result-delete`. No `probe-evaluate --then-terminate` until the false-positive rate is characterised.

---

## 9. Test plan

### 9.1 Unit tests — repository

- `tests/unit/controller/state/test_probe_vantages.py` — state transitions, trigger refusals, list_by_state, list_due_for_rotation
- `tests/unit/controller/state/test_probe_results.py` — record + recent_for_box + distinct_vantages_in_window
- `tests/unit/controller/state/test_image_profiles.py` — pin + get + list

### 9.2 Trigger tests — `tests/unit/controller/state/test_probe_triggers.py`

- relabeling a burned vantage refuses (`probe_vantages_no_relabel_burned`)
- updating a burned vantage out of burned refuses (`probe_vantages_burned_no_revert`)
- UPDATE / DELETE on `probe_results` refuses

### 9.3 Evaluator tests — `tests/unit/controller/probe/test_evaluator.py`

- empty history → `healthy`
- one hard_fail → `hard_kill`
- N-of-M soft fails across ≥ `min_distinct_vantages` → `soft_threshold_reached`
- N-of-M soft fails on a single vantage → `soft_pending` (not enough vantages)
- mix of passes and stale fails outside window → `healthy`

### 9.4 Invariant tests — extend `tests/unit/controller/state/test_invariants.py`

- #37 dup label → raises
- #38 orphan probe_result → raises
- #39 promoted image without profile → raises
- #40 (warning-only; assert it does NOT raise but does emit a logged warning)

### 9.5 Scheduler tests — `tests/unit/controller/probe/test_audit_wheel.py`

- mock clock; advance; sweep emits `probe_kill_pending` rows for live boxes whose evaluator returns hard_kill
- coverage gap surfaces `probe_coverage_pending` per-box
- vantage past TTL surfaces `probe_vantage_rotation_pending`
- heartbeat `probe_audit_sweep_ran` proven each tick

### 9.6 Property test — `tests/property/test_probe_invariants.py`

Hypothesis: generate sequences of (add_vantage, attest, record_probe, retire, burn) plus probe results across rotated vantages. After each step:
- no `probe_vantages` row exits `burned`
- every `probe_results` row's referenced vantage exists
- the evaluator never returns `hard_kill` purely on `pass` rows
- a `hard_fail` row on any vantage triggers `hard_kill` even for a single-vantage history (per §8 hard-kill = single occurrence)

### 9.7 Integration test — `tests/integration/test_probe_harness_lifecycle.py`

End-to-end:
1. Bootstrap; provision a box; `image-promote` an image and `profile-pin` it.
2. Add 3 vantages; attest 2 active.
3. Record passing probes from both vantages → `probe-evaluate` returns `healthy`.
4. Record one `hard_fail` from one vantage → `probe-evaluate` returns `hard_kill`, sweep emits `probe_kill_pending::<box>`.
5. Operator runs `ru-box-terminate --reason=compromise`; the spec H reshuffle hook fires; `probe_kill_pending` row clears on next sweep (because the box is no longer `live`).
6. Burn one vantage; assert the next `probe-record` from it refuses.

### 9.8 CLI tests — extend `tests/unit/controller/test_cli.py`

- `vantage-add` + `vantage-list --json` round-trip
- `vantage-attest-active` happy + audit row
- `vantage-burn` writes audit + refuses subsequent `probe-record`
- `profile-pin` + `profile-show --json` round-trip
- `probe-record` happy + refuses non-active vantage
- `probe-evaluate --json` returns the expected verdict

### 9.9 Failure-mode catalogue

| Failure | Behaviour |
|---|---|
| `probe-record` from a `candidate` vantage | CLI exit 2 with `probe-record: vantage state=candidate; only active vantages may record` |
| `probe-record` for a box whose `image_version` has no `image_profiles` row | Allowed (the row records what was checked); but `probe-evaluate` raises `EvaluationError: image profile missing for v<X>` so the operator notices |
| Operator tries to add a burned vantage label | Trigger raises `IntegrityError`; CLI surfaces `probe-vantage: label is in burned state; never reuse` |
| Operator tries to UPDATE a `burned` vantage row out of burned | Trigger raises; CLI surfaces |
| Sweep finds a live box with image_version that has no profile | `probe-evaluate` style failure surfaces a per-box obligation: `probe_evaluate_blocked::<box_id>` with `reason='missing_image_profile'` |
| `vantage-attest-active` on a non-candidate vantage | CLI exit 2 |
| Clock skew across coverage window | Same accepted residual pattern as spec C — wall-clock based |

---

## 10. Cross-spec contracts

| Consumer | What spec I exposes | Notes |
|---|---|---|
| **Spec C** (cover-domain pool) | `vantage-attest-active --label <name>` is the upgrade path C-D1 anticipated. C's `cover-attest-verified --vantage <name>` already accepts a free-text vantage label. **Post-spec-I follow-up:** spec C is amended to require that `--vantage` references a row in `probe_vantages` with `state='active'`. Until that amendment lands, the two systems are loosely coupled by string label. | The amendment is small (one foreign-key check in `attest_verified`) and is named here as the linking work; spec I does not make it mandatory in v8 because some existing operators may have legacy vantage strings. |
| **Spec D** (image build) | `profile-pin` is the seam D2 (canary + validation gate) will later wire into `image-promote`. For spec I MVP, `profile-pin` is a separate operator step. **Post-spec-I follow-up:** spec D's `image-promote` is amended to refuse promotion until `image_profiles` has a row for the candidate's `image_version`. | Spec D is intentionally not modified by spec I — the amendment ships when D2 lands. |
| **Spec H** (shard manager) | None directly. The probe-driven termination path uses `ru-box-terminate --reason=compromise`, which spec H already hooks. | Verified by the integration test step 5. |
| **Spec J** (observability) | `probe-due --json` is the read API; the obligation rows are the alerting surface. | Spec J is the natural consumer; spec I does not implement alerting. |
| **Spec K** (distribution) | `evaluate_box(...) == healthy` is the gate K can later check before publishing a box's subset to a user. For spec I MVP, K may rely on the obligation set being empty. | No schema change required; K calls the same evaluator. |

---

## 11. Honest residuals (Spec I)

1. **Operator-driven probe execution is a trust hop.** A careless or malicious operator can submit fabricated `pass` results from a real vantage that didn't actually probe. Spec I captures `evidence_json` verbatim in `probe_results` but cannot validate it. The §12 `probe_vantage_rotation_proven` obligation surfaces "operator is actually rotating," and `probe_coverage_proven` surfaces "operator is actually probing," but neither proves *truthfulness* of submitted evidence. Bounded only by operator discipline until automated probe execution exists (post-MVP).
2. **Single-vantage hard-kill is risky.** The design says hard-kill is single-occurrence (no debounce). A faulty vantage that misclassifies a healthy box as `hard_fail` causes a false termination. False-positive cost is operationally trivial at MVP fleet size (one new box) but the trigger comes from one source, so a *systematically* broken vantage chains many false kills. Mitigation: the `min_distinct_vantages = 2` rule for soft-fails does NOT apply to hard-fails — that is the design's choice. Spec I makes the rule literal; operators with paranoia about a single vantage may raise `min_distinct_vantages` for hard-fails via a future config knob (named here, not built).
3. **Pinned profile contents are not enforced — only existence is.** Spec I checks that `image_profiles.profile_json` is non-empty; it does not validate schema or content. A profile that omits the TLS handshake fingerprint silently makes that check a no-op. Validation is operator/spec-D's responsibility. The §12 obligation `probe_vantage_rotation_proven` does NOT catch profile staleness; only the operator's discipline does (per design §357).
4. **Vantage burn is monotonic, but vantage *attribution* may be discovered after the fact.** A vantage running for weeks may be "active" but already attributable (e.g. its egress IP appeared in a public block list yesterday). Spec I has no automated burn-detection — burns are operator decisions on operator evidence. Honest because the alternative (automated burn-detection) would require its own probe-of-probes, which is out of scope.
5. **`probe_coverage_window_seconds = 1h` is conservative for small fleets.** For 3–5 boxes and 2–3 active vantages, an hourly probe per (box, vantage) is ~6–15 probes/hour total — manageable. For larger fleets, this becomes a vantage-load problem the operator must tune. Named here, not solved.
6. **Invariant #40 is warning-only.** Two active vantages with the same `region_hint` and `source_kind` may be rotation-cosmetic, but they may also be legitimately two independent VMs in the same cloud region. The structural check is therefore a warning, not a raise — the operator decides. Honest residual: a careless operator may pick rotation that looks rotated but isn't independent.
7. **The probe credential** (design §294: "Valid-path liveness — Connect with a **probe** credential (never a user's)") is named by the design but not implemented by spec I. The credential is per-vantage and would live in `onward_credentials` with a `kind='probe'` carve-out. Spec I assumes the operator generates probe credentials out-of-band when needed; an integrated `probe-credential-issue` CLI is a follow-up (one task; not blocked on anything in spec I).
8. **Audit-table size growth.** `probe_results` is append-only and grows with `box_count × vantage_count × cycles_per_window × time`. At 10 boxes × 3 vantages × 6 checks × 24 cycles/day = 4320 rows/day. Sustainable for years on SQLite, but a future maintenance job will need to compact old `probe_results` (e.g. keep `pass` rows for 30d, `fail` rows for 1y). Not implemented in spec I MVP.

---

## 12. §12 obligation summary (deployment-runbook view)

| Obligation | Healthy interval | What "proven" means |
|---|---|---|
| `probe_audit_sweep_ran` | ≤ 1h | Sweep ran successfully |
| `probe_coverage_proven` | ≤ `coverage_window_seconds × 2` (default 2h) | At least one `probe-record` ingest somewhere in the fleet |
| `probe_vantage_rotation_proven` | ≤ `probe_vantage_ttl_days × 2` (default 28d) | At least one operator-attested vantage transition (attest/retire) |
| `probe_kill_pending::<box_id>` | absent or cleared within operator-response SLA | No live box has a current hard_kill / soft_threshold_reached verdict |
| `probe_coverage_pending::<box_id>` | absent or cleared within sweep interval | No live box has been unprobed longer than `coverage_window_seconds` |
| `probe_vantage_rotation_pending::<vantage_id>` | absent or cleared within sweep interval | No active vantage past its rotation TTL |

**Deployment runbook addition:** the operator must (a) source ≥ 2 disposable, non-attributable, Russia-approximating vantages and run `vantage-attest-active` per spec §11 residual #1; (b) pin a known-good profile via `profile-pin` for every promoted image (spec §11 residual #3); (c) treat any `probe_kill_pending::*` row as a 1-hour operator-response SLA — the design's "short dwell time" guarantee depends on it (§8 Job 2).
