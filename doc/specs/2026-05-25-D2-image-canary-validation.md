# Spec D2 — Image Canary + Validation Gate + Soft-Burn Rollback

Status: **Draft, awaiting operator review.**
Predecessor: `doc/design.md` §9 (T4 steps 3–6), `doc/specs/2026-05-21-D-ru-image-build-pipeline.md` (D1 baseline — image catalog + builder + upstream tracker), `doc/specs/2026-05-21-G-ru-provisioning-artifact-generator.md` (provisioning entrypoint), `doc/specs/2026-05-24-H-shard-manager.md` (compromise-reshuffle hook for canary box termination), `doc/specs/2026-05-25-I-probe-vantage-harness.md` (`image_profiles` table + probe-driven kill verdicts the gate consumes).
Successors blocked on this: none in MVP. T4 obligation `t4_image_promoted` is owned here; spec D2 makes T4 *real* in the §13 honesty-ledger sense.

---

## 1. Purpose

Implement the four steps of T4 (design.md §9 steps 3–6) that spec D1 deferred:

3. **Candidate profile pinning** — atomic with image build.
4. **Canary deployment** — a small number of boxes built from the candidate, probed by spec I, observed for a soak.
5. **Validation gate** — `image-promote` refuses unless the gate passes (profile pinned, ≥ N canary boxes have completed ≥ M probe cycles, no canary has `probe_kill_pending`, operator evidence text supplied).
6. **Atomic re-pin + soft-burn rollback** — promotion re-points "the current image" without touching live boxes (cattle model); rollback flags live boxes built from a soft-burned image for replacement.

Spec D2 is the **first place in the system where promotion is a calculated bet, not a rubber stamp.** Spec D1's `image-promote --evidence <text>` accepted any evidence string; D2 makes the gate structural.

**Out of scope:** automated rollback (operator decides); cross-image profile diffing (operator inspects pinned profiles manually); the actual orchestration of replacing boxes (handled by spec G's existing `provision-seed` + spec H's compromise-reshuffle); rolling-window cycle counting for soft-fail debounce (spec I's evaluator already does this; D2 just reads its verdict).

---

## 2. Locked design decisions

Approved during brainstorming session 2026-05-25.

| ID | Decision | Rationale |
|---|---|---|
| D2-D1 | **Canary = `ru_boxes.is_canary` boolean column.** A box is marked canary at `provision-seed --canary` time. The flag does NOT auto-clear on promotion — canary boxes built from `image_version=X` remain marked canary even after X is promoted. Operator may explicitly demote with `ru-box-canary-clear` if they want, but the natural lifecycle is: canary box ages out via normal replace-on-burn; new boxes from the promoted image arrive without the flag. | Mixing the flag's lifecycle with promotion would either silently expand the canary cohort (everything built from a now-promoted candidate becomes "canary forever") or silently shrink it (canary boxes lose the flag the moment promotion happens, losing the evidence trail). Keeping the flag stable preserves audit: "this box was provisioned during the X validation window." |
| D2-D2 | **Auto-pin via `image-build --profile-json <path>` and `--profile-json -` (stdin).** The operator supplies the candidate's known-good profile JSON at build time. The `ru_images` insert and the `image_profiles` insert happen in one transaction. `--profile-json` is required on `image-build` (previously optional; one-shot upgrade). | Design.md §9 step 3 names "pin a candidate known-good profile" as part of the build, not a follow-up step. Atomic insertion eliminates the "image built but no profile pinned" failure mode that spec I §11 residual #3 noted. |
| D2-D3 | **Validation gate is a pure function: `evaluate_promotion_gate(conn, image_version, cfg) -> GateResult`.** Returns `{passed: bool, reasons: list[str]}`. The CLI `image-promote` calls it and refuses with each reason printed on its own stderr line if `passed=False`. | Pure evaluator = trivially testable + reusable by `image-promote-status` to show *why* promotion would be refused without performing it. Same pattern as spec I's `evaluate_box`. |
| D2-D4 | **Gate requirements (all four must pass):** (a) `image_profiles` has a row for this `image_version`; (b) at least `cfg.image.canary.min_boxes` (default 1) canary boxes built from this `image_version` exist in `state IN ('live','terminated')` — terminated counts because a canary's termination during soak IS a hard-kill signal that the operator may have already acted on; (c) those canary boxes have at least `cfg.image.canary.min_cycles_per_box` (default 4) probe_results rows each across **distinct vantages** (min_distinct_vantages from cfg.probe); (d) **no live canary** for this image has a `probe_kill_pending::<box_id>` anti-obligation row. The operator's `--evidence <text>` is recorded but the gate does NOT validate its content. | Matches design.md §9 step 5. Defaults are conservative: 1 box × 4 cycles × 2 distinct vantages = 8 successful probe rows minimum before promotion. Operator dials up for higher-paranoia deployments. |
| D2-D5 | **Atomic re-pin happens automatically at promotion.** Each `ru_images` row IS its profile via the `image_profiles` FK relationship; promotion atomically changes `current_promoted` to the new version, and that version's profile becomes the *de facto* "active profile" because probe_results reference `image_version` and the evaluator joins on it. The retired image's profile row stays in `image_profiles` for forensic comparison. | Design.md §9 step 6's "atomic re-pin" is therefore *already implemented* by the schema — D2 just locks the semantics so future contributors don't try to mutate `image_profiles` rows after promotion. A v11 trigger enforces: `image_profiles` is INSERT-only on rows whose `ru_images.state IN ('candidate', 'promoted')`. Updating the JSON of a retired image's profile is forbidden. |
| D2-D6 | **Soft-burn rollback = `image-rollback <image_version> --to <target_version>`.** When invoked: (a) retires `image_version`; (b) re-promotes `target_version` if it's currently retired; (c) marks every `live` ru_box built from the rolled-back version with a `image_rollback_pending::<box_id>` anti-obligation row (spec J alerts the operator as `crit`). The operator drives `ru-box-terminate --reason=compromise` (or a less-loud reason) for each, with the spec H reshuffle hook firing on `compromise`. | Design.md §9 step 8's "rollback" is genuinely two atomic operations (image state changes) plus a per-box cleanup that the operator must drive. Spec H + spec G already handle the per-box work; D2's contribution is the *trigger* — the `image_rollback_pending` anti-obligation. |
| D2-D7 | **`image-promote-status <version>` is read-only.** Calls `evaluate_promotion_gate`, prints the pass/fail breakdown. Always exit 0; non-zero only on argument errors. | Operators run this to plan a soak; it must never refuse to run. |
| D2-D8 | **No new schedulers.** D2 is gate + state + CLI. The probe-driven verdicts the gate reads come from spec I's `ProbeAuditWheel`. No additional sweep. | The work is gating, not monitoring. Adding a sweep that polls the gate would create alert fatigue (operators do not promote daily). |
| D2-D9 | **`audit_log` row per gate evaluation that refuses promotion AND per rollback.** Successful promotions already audit-log; refusals now do too with the failing-reasons captured. | Post-mortem after a bad promotion: was the gate honored? requires the row to survive. |
| D2-D10 | **Active-only.** Standby does not promote, rollback, or build images. Spec D1 already gates these on `_require_active_role`; D2 inherits. | Same reasoning as every active-side mutation. |

---

## 3. Schema additions (v10 → v11)

### 3.1 `ru_boxes.is_canary` column

```sql
ALTER TABLE ru_boxes ADD COLUMN is_canary INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS ix_ru_boxes_canary
  ON ru_boxes(image_version) WHERE is_canary = 1;
```

`INTEGER NOT NULL DEFAULT 0` — SQLite booleans are stored as `0`/`1`; existing rows get `0` (not canary) via the DEFAULT clause.

### 3.2 `image_profiles` immutability trigger

```sql
CREATE TRIGGER image_profiles_no_update_for_retired
BEFORE UPDATE ON image_profiles
WHEN EXISTS (
  SELECT 1 FROM ru_images WHERE image_version = NEW.image_version AND state = 'retired'
)
BEGIN
  SELECT RAISE(ABORT, 'image-profiles: row for retired image is forensic-immutable');
END;
```

Note: the *insert* path for a profile happens at image-build time (image is `candidate`); operator-invoked `profile-pin` (spec I CLI) can still overwrite while the image is `candidate` or `promoted`. Once retired, the row freezes.

### 3.3 Schema version bump

`SCHEMA_VERSION = 11`; `migrate_v10_to_v11(conn)` adds the column + index + trigger (idempotent ALTER + IF NOT EXISTS).

---

## 4. Gate evaluator

`mthydra.controller.image.gate`:

```python
@dataclass(frozen=True)
class GateResult:
    image_version: str
    passed: bool
    reasons: tuple[str, ...]              # empty if passed
    canary_box_ids: tuple[str, ...]
    canary_probe_rows: int                # sum across all canaries
    canary_distinct_vantages: int         # union across all canaries
    pending_kills: tuple[str, ...]        # box_ids with probe_kill_pending

@dataclass(frozen=True)
class GateConfigView:
    min_canary_boxes: int
    min_cycles_per_box: int
    min_distinct_vantages: int            # inherited from cfg.probe

def evaluate_promotion_gate(
    conn, image_version: str, *, cfg: GateConfigView,
) -> GateResult: ...
```

Pure. No writes. Reads `ru_images`, `image_profiles`, `ru_boxes` (filtered by `is_canary=1 AND image_version=X`), `probe_results` (joined by box), `obligation_clocks` (probe_kill_pending::*).

---

## 5. Repository API extensions

`mthydra.controller.state.ru_boxes`:

```python
def insert_box(conn, ..., is_canary: bool = False) -> None: ...      # extended
def list_canary_boxes(
    conn, *, image_version: str | None = None,
    state_filter: tuple[str, ...] | None = None,
) -> list[str]: ...                                                  # new
def clear_canary_flag(conn, box_id: str, *, at: str, reason: str) -> None: ...
```

`mthydra.controller.state.ru_images`:

```python
def list_live_boxes_for_image(
    conn, image_version: str, *, include_terminated: bool = False,
) -> list[str]: ...                                                  # new
```

---

## 6. Startup invariants (extend with #44)

| ID | Statement | Failure → |
|---|---|---|
| #44 | Every `live` ru_box with `is_canary=1` references an `image_version` whose `ru_images.state` is `candidate` or `promoted`. (A live canary cannot reference a `retired` image — that's the soft-burn condition; live retired-image canaries would be unmonitored.) | `live canary on retired image` |

#44 is the structural check that spec D2's lifecycle implies. The migration path on the soft-burn rollback flow ensures any rollback emits the `image_rollback_pending::<box_id>` anti-obligation so the operator notices; #44 catches the case where they ignored it long enough that startup runs.

---

## 7. §12 obligation contributions

| Obligation key | Cadence / semantics |
|---|---|
| `t4_image_promoted` | (already exists from spec D1) — proven on every successful gated promotion |
| `image_rollback_pending::<box_id>` | anti-obligation; spec J alerter classifies as `crit` (see §10 amendment) |

D2 adds no new sweep heartbeats — the gate is evaluated on-demand.

---

## 8. CLI surface

```
mthydra-controller image-build --release <tag> [--asset <name>] \
    --profile-json <path|-> [--notes <text>]
    # NOW REQUIRES --profile-json (was optional in D1). Reads the JSON
    # blob (or stdin) and inserts the image_profiles row atomically with
    # the ru_images row.

mthydra-controller image-promote <version> --evidence <text>
    # CHANGED: now calls evaluate_promotion_gate first. Refuses with
    # one stderr line per failing reason. The --evidence text is
    # recorded but not validated.

mthydra-controller image-promote-status <version> [--json]
    # NEW. Read-only gate evaluation; always exits 0.

mthydra-controller image-rollback <version> --to <target_version> \
    --evidence <text>
    # NEW. Two atomic ops: retire <version>, re-promote <target_version>
    # (if it's a previously-promoted-then-retired row; new candidates are
    # not eligible as a rollback target — the operator must promote them
    # normally). For each live box built from <version>: emit
    # image_rollback_pending::<box_id>. Audit row per op.

mthydra-controller provision-seed --canary [...all existing args...]
    # NEW FLAG on the existing command. Marks the resulting ru_boxes row
    # with is_canary=1. The seed bundle is otherwise unchanged.

mthydra-controller ru-box-canary-clear <box_id> --reason <text>
    # NEW. Demote a canary box to "regular fleet" — e.g. after the soak
    # is complete and the box has aged into its normal role. Audit row.
```

---

## 9. Config additions

New `[image.canary]` subsection in `controller.toml`:

```toml
[image.canary]
min_boxes              = 1
min_cycles_per_box     = 4
# min_distinct_vantages comes from [probe] — DO NOT duplicate here.
```

Loaded into an `ImageCanaryConfig` dataclass nested under `ImageConfig`.

---

## 10. Spec J severity table amendment

Spec D2 emits one new anti-obligation kind:

| Source | Severity |
|---|---|
| `image_rollback_pending::*` | **crit** (a box still running rolled-back code is a fleet-wide-burn risk — same severity as a probe-driven hard_kill) |

Spec J's severity table (`observability/severity.py`) gains the row; the snapshot prefix set gains the kind.

---

## 11. Test plan

### 11.1 Schema migration test
- `test_schema_version_is_11`, `is_canary` column present, immutability trigger blocks updates on retired rows, migration idempotent.

### 11.2 Gate evaluator tests
- `tests/unit/controller/image/test_gate.py`
  - empty canary cohort → fails with "no canary boxes"
  - canary box exists but no profile → fails with "image_profiles missing"
  - profile + 1 canary + 4 cycles across 1 vantage → fails with "insufficient distinct vantages"
  - profile + 1 canary + 4 cycles across 2 vantages, no kill_pending → passes
  - profile + 2 canaries with one having probe_kill_pending → fails with "canary <box_id> has pending kill"
  - terminated canary still counts towards the cohort (D2-D4)

### 11.3 CLI tests
- `image-build` rejects when `--profile-json` missing
- `image-build` with `--profile-json -` reads stdin
- `image-promote` refuses with each reason on its own stderr line when gate fails
- `image-promote-status` exits 0 even when gate fails, prints reasons
- `image-rollback` retires source, re-promotes target, emits per-box pending obligation
- `image-rollback` refuses if `--to` references a non-previously-promoted image (forces operator to use normal promote path)
- `provision-seed --canary` sets `is_canary=1` on the resulting row
- `ru-box-canary-clear` clears the flag + audit row

### 11.4 Invariant test
- live canary on a retired image → check 44 raises

### 11.5 Severity test
- `image_rollback_pending::<box>` → severity `crit`

### 11.6 Integration test
- `tests/integration/test_image_canary_lifecycle.py`
  - Build candidate v2 with profile JSON.
  - `provision-seed --canary` ×2 from v2.
  - Without probe data: `image-promote-status v2` shows failing reasons.
  - Record probe_results: 4 cycles per canary across 2 distinct vantages, all pass.
  - `image-promote-status v2` now shows passed; `image-promote v2 --evidence "..."` succeeds.
  - `image-rollback v2 --to v1` retires v2, re-promotes v1, sets one `image_rollback_pending` per live v2 box.

### 11.7 Failure-mode catalogue

| Failure | Behaviour |
|---|---|
| `image-build` without `--profile-json` | argparse error |
| `image-promote` with `image_profiles` row missing | gate refuses; one stderr line `gate: image_profiles row missing for <version>` |
| `image-promote` with one canary kill_pending | gate refuses; stderr lists the box_ids |
| `image-rollback` with `--to` referencing a candidate or never-promoted version | CLI exit 2 |
| Promotion races a new probe_results insert that creates kill_pending | SQLite serialises; whichever commits first wins; second `image-promote` call would re-evaluate and (likely) refuse |
| Operator tries to update an `image_profiles` row for a retired image | Trigger raises |

---

## 12. Cross-spec contracts

| Source spec | What D2 consumes / amends | Notes |
|---|---|---|
| Spec I | `image_profiles` table (already populated by spec I CLI); `probe_results` (read by gate); `probe_kill_pending::*` obligations | D2 amends spec D's `image-promote` to honour these — closing the spec I §13 follow-up named there. |
| Spec G | `provision-seed` gains the `--canary` flag — pure passthrough to `ru_boxes.is_canary` | One-line CLI change |
| Spec H | When the operator runs `ru-box-terminate --reason=compromise` on a rollback-pending box, the spec H compromise-reshuffle hook fires (a *retired-image* canary's shard is reshuffled) — desirable, the design's "treat every parameter as adversary-known" rule applies | No code change |
| Spec J | Adds `image_rollback_pending` to the severity table + snapshot prefix set | Same pattern as spec K amendment |

---

## 13. Honest residuals (Spec D2)

1. **"Evasiveness regression" detection requires comparing the candidate profile to the outgoing profile.** D2 only checks that *a* profile exists for the candidate — not that it's *better* than the previous one. Design.md §9 step 5 named this as essential. D2 names the gap: comparison is operator judgment, supported by `image-promote-status` listing the prior profile's `recorded_at` for context. A future enhancement could machine-diff TLS fingerprints, but the comparison metric is itself a judgement call. Stated; not solved.
2. **`min_boxes=1` is the absolute floor.** A single canary box can be tampered without any way to tell from a second-vantage probe alone (the box's compromise affects every observation). `min_distinct_vantages` partially compensates, but a single canary is still less robust than 2-3. Default is 1 because for small fleets, dedicating multiple boxes to canary is cost-prohibitive. Operator runbook should recommend `min_boxes=2` for higher-paranoia deployments.
3. **Rollback is operator-driven per-box.** D2 sets the anti-obligation but does not auto-terminate. A long-running operator response on a serious rollback (e.g., promoted image had a hard regression) means rolled-back-but-still-live boxes are reachable until the operator gets to them. Mitigated by the `crit` severity routing through spec J's alerter; not eliminated.
4. **The gate's "no kill_pending" check is point-in-time.** A canary that passes the gate may acquire a kill_pending immediately after promotion completes. This is inherent to any threshold check; spec I's `ProbeAuditWheel` will surface the kill_pending on the now-promoted image and the operator runs `ru-box-terminate` from there. Not specific to D2.
5. **No automated soak duration.** D2 counts probe cycles, not wall-clock soak time. An operator could rapid-fire probe submissions to satisfy `min_cycles_per_box` in minutes. The deployment runbook must enforce wall-clock soak; D2 trusts the operator's clock.
6. **`is_canary` column survives termination by design (D2-D1)** — but that means a permanently-canary historical row exists for forensic analysis. Acceptable; mentioned for completeness.

---

## 14. §12 obligation summary (deployment-runbook view)

| Obligation | Healthy interval | What "proven" means |
|---|---|---|
| `t4_image_promoted` | ≤ 30d (spec D1 setting; unchanged) | At least one image-promote with a passing gate has run |
| `image_rollback_pending::<box>` | absent | No live box references a rolled-back image |

**Deployment runbook addition:**
- Run `image-build --profile-json <path>` with the operator's candidate-profile capture file.
- Provision ≥ `min_boxes` canary boxes via `provision-seed --canary`.
- After soak (wall-clock + cycle-count), run `image-promote-status <version>` to verify the gate would pass.
- Run `image-promote <version> --evidence "<wall-clock soak start: ..., end: ..., observed cover-site behaviour: ...>"`.
- On regression: `image-rollback <bad_version> --to <previous_good>`; then `ru-box-terminate` each pending box.
