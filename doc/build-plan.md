# mthydra — Build Plan & Spec Decomposition

Companion to `design.md`. Decomposes the design into discrete, independently-specifiable artifacts. Each artifact below gets its own design spec → implementation plan → implementation cycle. This document is the **index**, not a spec.

Base OS for all node types: **Ubuntu 24.04**. Implementation language where applicable: **Python 3**. Every artifact must be testable; test plan is a mandatory section of each downstream spec.

---

## 1. Gap analysis — items the design demands but the initial brief omitted

Cross-checked against `design.md` §3, §6–§11, and T7–T12.

| # | Missing artifact | Source | Why it cannot be folded into another artifact |
|---|---|---|---|
| 1 | Controller state backup pipeline | §3, T2 | Encrypted-to-operator-key, push-on-change + timer, multi-generation, generation-gap alarm. Recovery substrate, distinct from observability. |
| 2 | Signed endpoint descriptor service | §3, T11 | Controller signs current EU exit set; RU verifies (verify-not-forge). Defines what a seized RU box yields. |
| 3 | Cover-domain pool manager | T5 | State machine `candidate-unverified → candidate-verified → in-use → burned`. Burned-set is authoritative controller state; never-reuse must be structurally enforced. |
| 4 | Russia-approximating probe vantage harness | T3 Job 1, T5 §4, T7 | What observability *queries*. Without it, every probe is an EU→RU false green. T7 has unresolved design substance — its own session. |
| 5 | Shard manager (per-user isolation) | T6 | Disjoint shard assignment + periodic reshuffle + reshuffle-on-compromise. Controller-enforced refusal of multi-shard assignment. |
| 6 | User distribution channel | §3 | Telegram bot/channel publishing a *rotating subset* + email backstop + dead-man's-switch. Audience and criticality differ from operator alerts. |
| 7 | RU image build & canary pipeline | T4 | Track upstream → build → pin candidate profile → canary soak → validation gate → atomic promotion + profile re-pin. Not "build a Docker image once." |
| 8 | Warm-standby EU node role | §1, T2 | Exit-only, controller-capable on manual promotion. Setup must produce both roles or T2's promotion path is fiction. |
| 9 | Break-glass dormant Reality path | T1 | Separate fleet, separate maintenance clock. MVP-deferrable but explicit. |
| 10 | Operator key custody policy | §3, T2 | Out of automation scope, but specs must declare where the key lives and what is encrypted to it — so automation knows what *not* to hold. |
| 11 | Test harness & §12 obligation clock | §12 | "Time since last proven" is the design's load-bearing metric. Each spec contributes; harness is the common substrate. |

---

## 2. Build order

Dependency-ordered. Items within the same group may proceed in parallel once predecessors land.

```
Foundation (controller primitives — prerequisites for everything else)
  A. Controller state model + backup pipeline                [#1, #10]
  B. Signed endpoint descriptor scheme                       [#2, T11]
  C. Cover-domain pool manager                               [#3, T5]

RU side
  D. RU image build pipeline (covers T4 gate)                [#7]
  E. RU node init from cloud-init seed                       (the "RU init script")

EU side
  F. EU node setup (active + warm-standby roles)             [#8]
  G. Artifact generator for RU provisioning                  (the "EU→RU artifact")
  H. Shard manager                                           [#5, T6]

Operations
  I. Probe vantage harness                                   [#4, T3 Job 1, T7]
  J. Observability service (consumes I; emits alerts)        (the original "observability service")
  K. User distribution channel (Telegram bot + email)        [#6]

Deferred (post-MVP)
  L. Break-glass dormant path                                [#9, T1]
```

**Edges:**

- `A`, `B`, `C` are prerequisites for everything else.
- `D` and `F` may run in parallel once `A–C` land.
- `E` depends on `D` (image) and `G` (provisioning artifact format).
- `G` depends on `B` (descriptor format) and `C` (cover-domain assignment).
- `H` depends on `F` (lives on EU controller) and `C` (consumes cover domains per assignment).
- `J` depends on `I`; `J` also depends on `F` (alert egress path).
- `K` depends on `F` (publishing logic on controller) and `H` (knows which subset to publish to whom).

---

## 3. Per-artifact one-line scope (placeholder until each spec exists)

| ID | Artifact | Owning design § | One-line scope |
|---|---|---|---|
| A | Controller state + backup pipeline | §3, T2 | Define authoritative state schema; encrypt to operator key; push off-box on change + timer; multi-generation retention; generation-gap alarm. |
| B | Signed endpoint descriptor | §3, T11 | Pin contents, signing key, validity window, generation counter; RU-side verify path; rotation semantics. |
| C | Cover-domain pool manager | T5 | State machine + structural never-reuse + Russia-vantage verification gate + pool replenishment policy. |
| D | RU image build pipeline | T4 | Upstream tracking → build candidate → pin candidate T3 profile → canary soak → validation gate → atomic promotion + profile re-pin → rollback path. |
| E | RU node init | §3 | Read cloud-init seed into tmpfs; verify descriptor key; bring up Fake-TLS relay; never write seed/state to disk; reboot = death. |
| F | EU node setup | §1, T2 | Provision active node (data-exit + controller) **and** warm-standby (exit-only, controller-capable). Manual promotion path. |
| G | RU provisioning artifact generator | §3 | Produce the seed bundle consumed by E: per-box SNI, per-box revocable onward credential, descriptor-verify pubkey, transport role. |
| H | Shard manager | T6 | Disjoint shard assignment; reshuffle on timer; reshuffle on Job-2 kill; controller-enforced refusal of multi-shard assignment. |
| I | Probe vantage harness | T3, T7 | Disposable, non-attributable, Russia-approximating probe vantages; rotation; per-cycle check execution against pinned profile. |
| J | Observability service | (brief) | Aggregate I's findings + controller liveness + EU node state + connected-client metadata; emit Telegram-channel + email (app-pw) alerts; expose §12 "time since last proven" metric. |
| K | User distribution channel | §3 | Telegram bot/channel publishing *rotating subset only* + email backstop + dead-man's-switch heartbeat. |
| L | Break-glass dormant path | T1 | Reality-capable separate fleet; per-user Part 1 setup tooling; trigger-message + return-message dispatch over Telegram-independent channel. |

---

## 4. Test discipline (applies to every spec)

Every downstream spec MUST include:

1. **Unit-testable surface.** Components communicate over well-defined interfaces; internals replaceable with fakes.
2. **Integration test plan.** What is exercised end-to-end on throwaway infrastructure. `T2` Case A/B dry-run is the archetype.
3. **§12 obligation contribution.** Which "time since last proven" timer this artifact updates, and how that timer is observed.
4. **Failure-mode catalogue.** What this artifact does on each named failure in `design.md` §4 / §13.

An artifact whose tests live only in the implementer's head does not count as built.

---

## 5. Status

- `design.md` — accepted (initial commit).
- This document — current.
- Specs `A` through `K` — **written, implemented, tested.** Each has its own spec under `doc/specs/`, its plan under `doc/plans/`, and lives implemented in the codebase with passing test suite + per-spec coverage targets met.
- Spec `D2` (canary + validation gate + soft-burn rollback) — written and implemented after `I`/`G`/`H` unblocked it. Lives at `doc/specs/2026-05-25-D2-image-canary-validation.md` + `doc/plans/2026-05-25-D2-image-canary-validation-plan.md`.
- Spec `L` (break-glass dormant path, T1) — **deferred MVP**. Not yet specced or planned. See §6 honesty note below.

### Cross-spec amendments shipped post-MVP

- **C × I:** `cover-attest-verified` now consumes spec I's `probe_vantages` registry. Free-text labels still accepted when the registry is empty (transitional); once any vantage is registered, attestation requires `state='active'`.
- **D × I (via D2):** `image-promote` now requires a pinned `image_profiles` row for the candidate (gate condition #1 in spec D2). `image-build --profile-json` is mandatory.
- **J × K:** spec J's severity table classifies spec K's `dist_user_unregistered::*` and `dist_user_heartbeat_breach::*` anti-obligation kinds.
- **J × D2:** spec J's severity table classifies `image_rollback_pending::*` as `crit`.

### Known follow-ups (named in residuals, not yet built)

- **Spec L** — break-glass dormant path (T1). Deferred MVP per §6 below.
- **`obs-alert-ack`** mechanism (spec J residual #4).
- **`probe-credential-issue` CLI** for the dedicated probe credential (spec I residual #7).
- **`alert_log` / `probe_results` / `distribution_log` compaction** — all three are append-only and grow with traffic.
- **T8 — cold-acquisition verification** (periodic reboot-then-image test).
- **T9 — off-box backup independence** (backups on infra independent of EU node *and* provider).
- **T12 — obscurity-assumption documentation** in the operator runbook (a documentation artifact, not code).
- **Probe profile diffing** — D2 only checks that *a* profile exists; the comparison against the outgoing image's profile is operator judgement (D2 §13 residual #1).

---

## 6. Honesty notes

- This plan is itself subject to revision. The first downstream spec will likely surface coupling we did not anticipate; expect to update §2 dependency edges accordingly.
- "Deferred" on `L` is a scope choice for MVP, not a judgement that T1 is unimportant. The design treats T1 as Critical; deferring it means accepting a circle-wide-burn recovery gap during the MVP window.
- `#10` (operator key custody) is deliberately *out of automation scope*. The specs describe what is encrypted to the key, never how the key is held. If automation ever touches the key, that is a regression.
