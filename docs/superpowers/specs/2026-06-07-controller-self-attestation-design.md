# Controller Self-Attestation via Vantage — Design

**Date:** 2026-06-07
**Status:** Draft for review
**Goal:** Stop nagging the operator with routine revalidation obligations the
controller can verify itself. When the controller has a usable vantage, it runs
a *meaningful* automated check and self-proves the obligation; the operator
hears about it **only on failure**.

## Decision (locked with operator)

**Full self-attest on pass.** A passing automated check counts as the
attestation and self-proves the obligation. The operator is notified only when
a check *fails* (an anti-obligation / overdue obligation). The manual
`*-attest-*` commands stay as an override.

## Background / what's actually wrong today

Three "every 168h" obligations surface to the operator as `warn` when overdue:

| Obligation | Today | Reality |
|---|---|---|
| `t4_upstream_check` | auto-proven by `UpstreamReleaseTracker` in `serve` | **cadence bug** — poll interval (168h) == obligation window (168h), so it perpetually races the deadline and shows overdue right before each poll. |
| `t3_vantage_revalidation` | "operator runs `vantage-attest-active`" | **orphan** — *nothing* calls `prove("t3_vantage_revalidation")`. `vantage-attest-active` sets vantage state but never proves t3. The remediation advice does not clear the warn. |
| `t5_pool_revalidation` | "operator runs `cover-attest-verified`" | **orphan** — nothing proves `t5_pool_revalidation`. `cover-attest-verified` proves `cover_pool_reverify_pass_proven` (a *different*, 60d obligation), not t5. |

So this work both (a) adds the requested automation and (b) fixes a latent
"obligation with no prover / wrong remediation" bug.

## Existing building blocks (reused, not reinvented)

- `probe_vantages.list_by_state(conn, "active")` — active vantages + their SSH config.
- `probe_runner/ssh.py:ssh_cmd` — run a command on a vantage over SSH.
- `probe_runner/probers.py` — `probe_tls_fall_through(ssh_cmd_fn, box_ip, cover_sni)` etc. (the vantage's real job: TLS-probe a box).
- `ProbeRunnerWheel` — already SSH-pings each vantage per tick and raises/clears `probe_vantage_unreachable::<id>`.
- `cover_pool_scheduler.auto_reverify_check(domain)` + `CoverPoolAutoReverifySweep` — already TLS-reverify cover domains, stamp `cover_pool_reverify_pass_proven`, raise `cover_pool_reverify_drift_pending::<domain>`.
- `UpstreamReleaseTracker` — polls GitHub, stamps `t4_upstream_check`.

## Design

### 1. `t4_upstream_check` — fix the cadence race

The tracker already self-proves; it just sets the obligation's `next_due_at`
too tight. Change the tracker so a successful poll stamps
`next_due_at = now + 2 × poll_interval` (a generous grace: one late/missed poll
never alarms; two missed polls — a genuinely stuck tracker — legitimately do).
No new component; `serve`'s existing tracker tick keeps it green.

### 2. `t3_vantage_revalidation` — `VantageSelfCheckSweep` (NEW)

A small periodic sweep (mirrors the other sweeps: `BackgroundScheduler` +
`IntervalTrigger`, no-op in offline mode, opens its own connection per tick).

Per tick:
1. List active vantages with SSH configured. If **none**, do nothing — the
   controller has no vantage to self-attest from, so t3 stays operator-owned
   (its overdue warn correctly tells the operator "you have no working
   vantage"). This is the "controller that is able to use its vantage" gate.
2. For each active vantage, run a **meaningful** liveness probe over SSH (not a
   bare ping): SSH reachable **and** the vantage can do its actual job — an
   outbound TLS handshake from the vantage to a reference HTTPS host. The
   reference defaults to `[data_exit].cover_sni_default` (already configured, a
   real reachable HTTPS host the operator vouched as credible Western traffic —
   no new config knob). The check passes iff SSH works **and** the TLS
   handshake to `<ref>:443` succeeds. If no reference is configured, fall back
   to SSH liveness alone (honest about what can be verified without a target).
3. If **≥1** vantage passes → `prove("t3_vantage_revalidation",
   proven_by="vantage_self_check", next_due_at = now + 2×sweep_interval window)`.
   Operator stays silent.
4. Per-vantage failures continue to surface via the existing
   `probe_vantage_unreachable::<id>` anti-obligation (raised by
   `ProbeRunnerWheel`; this sweep may also raise it for consistency). If
   **every** active vantage fails, t3 is simply *not* proven this cycle → it
   goes overdue → operator is alerted (the "something really needs attention"
   case).

### 3. `t5_pool_revalidation` — extend `CoverPoolAutoReverifySweep`

The auto-reverify sweep already TLS-checks in-use cover domains every cycle and
stamps `cover_pool_reverify_pass_proven`. Extend it: on a **clean pass** (no
domain raised `cover_pool_reverify_drift_pending` this cycle) also
`prove("t5_pool_revalidation", proven_by="cover_auto_reverify")`. Per-domain
drift already escalates (drift_pending → rotation), so failures still reach the
operator; a clean pool just stops nagging.

### 4. Manual overrides actually prove their obligation (bug fix)

Make the existing operator commands prove the matching obligation so the
override path works and the remediation advice is truthful:
- `vantage-attest-active` → also `prove("t3_vantage_revalidation", proven_by="operator")`.
- `cover-attest-verified` → also `prove("t5_pool_revalidation", proven_by="operator")`.

### Wiring

`VantageSelfCheckSweep` is constructed + `arm()`/`disarm()`'d in `_cmd_serve`
alongside the other sweeps (active node, non-offline). Its interval reuses an
existing knob (probe runner interval or a new `[probe] self_check_interval`,
default well under the 168h window, e.g. 6h). The t4 and t5 changes need no new
wiring.

## What the operator experiences

- Healthy fleet with a working vantage: **silence** on t3/t4/t5.
- A vantage that can't be reached / can't do TLS: `probe_vantage_unreachable`
  (crit) **and**, if all vantages are down, t3 overdue (warn) — real signal.
- A cover domain that drifts: `cover_pool_reverify_drift_pending` (existing) +
  rotation — real signal.
- No vantage configured at all: t3 overdue (warn) — correctly tells them to
  add a vantage.

## Security / trust notes

- Self-attestation is scoped to **routine liveness/reachability revalidation**,
  which is what these 168h obligations actually are. Higher-stakes, judgment
  vouches (initial cover-domain credibility, vantage onboarding) remain manual
  operator actions via the unchanged `add-candidate` / onboarding flows.
- `proven_by` records `vantage_self_check` / `cover_auto_reverify` (vs
  `operator`) in the obligation + audit row, so the provenance of every
  self-attestation is auditable and distinguishable from a human vouch.

## Testing

- `t4`: tracker stamps `next_due_at` with the 2× grace; obligation not overdue
  across a normal poll cadence; overdue only after 2 missed polls.
- `t3`: sweep with a passing fake `ssh_cmd`/prober proves t3; all-failing
  vantages → t3 not proven + `probe_vantage_unreachable` raised; no active
  vantages → no-op (t3 untouched).
- `t5`: clean auto-reverify pass proves t5; a drift cycle does **not** prove t5.
- overrides: `vantage-attest-active` proves t3; `cover-attest-verified` proves t5.
- `proven_by` provenance asserted.

## Out of scope

- Auto-onboarding new vantages or cover domains (still operator-driven).
- Changing the obligation cadences themselves (168h windows stay).
- Replacing operator judgment for first-time attestation.
