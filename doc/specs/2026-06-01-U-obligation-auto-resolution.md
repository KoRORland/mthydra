# Spec U — operator-obligation auto-resolution

Status: **Draft, awaiting operator review.**
Predecessor: spec J (observability + obligation clocks), spec P (probe runner),
spec C (cover-domain manager), spec H (shard manager).
Successors blocked on this: none. This is a load-reduction spec —
fewer operator pings, more controller-side work.

---

## 1. Purpose

The obligation clock is the single load-bearing metric in spec J: "time since
each obligation was last proven." Today many of those obligations require
human-driven proofs (`cover-attest-verified` every 60 days,
`rotate-vantage` every 28 days, manual reshuffle when shards drift, etc.).
Each is honest individually, but the cumulative effect is operator alert
fatigue → ignored alerts → "get rid of it" reflex → real outages missed.

This spec automates the obligations where the controller already has the
means (network, credentials, the operation itself is mechanical) and the
operator currently does what the controller could do itself. Obligations
that fundamentally require human judgment (terminate a compromised box,
pick a new cover domain) stay manual.

Out of scope:
- Anything that requires out-of-band provisioning (new vantage VPS, new
  cover-domain candidate).
- Final compromise decisions (probe_kill_pending).
- User-side actions (dist_user_unregistered).

---

## 2. Locked design decisions

| ID | Decision | Rationale |
|---|---|---|
| U-D1 | **Auto-reverify cover domains every few hours.** Replace the operator-driven `cover-attest-verified` re-attestation cadence (60 days) with controller-side automated reverification: TCP reach, TLS handshake fingerprint, expected SNI surface, response-shape baseline match. On pass, stamp `cover_pool_reverify_pass_proven` automatically. On fail, raise `cover_pool_reverify_drift_pending::<domain>` anti-obligation for operator review. | Biggest win in the audit — eliminates a regular human task entirely for the common case. Operator only sees the domain when the automated checks produce a "drift" verdict that needs human eyes. Failure mode is bounded (the anti-obligation still gates rotation; the operator still has the final word). |
| U-D2 | **Probe runner auto-failover across vantages.** When a probe attempt against vantage V1 fails (SSH unreachable, command error), retry against the next active vantage before incrementing `probe_coverage_pending`. Surface a separate `probe_vantage_unreachable::<vantage>` per-vantage anti-obligation when ALL probe attempts from that vantage fail for >N minutes — that's the operator-actionable alert. | Today: one dead vantage triggers N box-level alerts (one per box that vantage was probing). After: one alert per dead vantage, plus silent failover for box coverage. Avoids "the vantage's dead" surfacing as "all my boxes are failing probes" which is operator-confusing. |
| U-D3 | **Shard manager auto-resolves `shard_overdue_pending` and `shard_unassigned_pending`.** Both anti-obligations exist because the reshuffle wheel noticed a problem and stopped. The wheel should attempt the resolution itself (reshuffle or assignment) before raising. If the attempt fails (no candidates, constraint violation), THEN the anti-obligation goes up. | Both operations are deterministic and already implemented as controller actions. The current behaviour is "I noticed and bailed"; the new behaviour is "I noticed and tried; here's what happened." Same observability for failures, no human-in-the-loop for successes. |
| U-D4 | **Heartbeat-breach self-diagnosis.** When `obs_dead_mans_switch_breach` is about to be raised, run a brief self-test: SMTP reachability via the configured sink, sink-credentials validity check, summary of exception classes from the last N failures. Attach the verdict to the breach details_json. | Operator gets "SMTP connect timeout to mail.example.com:587 — last 3 attempts identical" instead of "heartbeat breached, go investigate." Faster triage; same alerting cadence. |

---

## 3. Components changed

### 3.1 `src/mthydra/controller/state/cover_pool.py` + new sweeper

New `cover_pool_auto_reverify` sweep loop (similar to the existing
reverify scheduler at `state/cover_pool_scheduler.py`). For each domain in
`state in ('in_use', 'candidate_verified', 'verified')`:
- TCP connect on :443 (configurable; timeout 5s)
- TLS handshake; capture cipher + extensions; compare to baseline
- HTTP/1.1 HEAD `/` (or :443 path); compare response shape to baseline
- On all-pass: stamp `cover_pool_reverify_pass_proven` (singleton — passes
  if any domain proves, since at least one verified domain exists)
- On per-domain fail: raise `cover_pool_reverify_drift_pending::<domain>`
- Cadence: hourly by default (override via config)

Out of scope for U-D1: storing the baseline per domain at `cover-add` time
(MVP: any successful TLS handshake passes; baseline-drift detection is a
follow-up).

### 3.2 `src/mthydra/controller/probe_runner/runner.py` (spec P)

Add per-vantage health tracking + failover. Today the runner picks a
vantage and probes; on failure, the box-level coverage obligation slips.
After: vantage-level health is tracked separately. Per-box probes pick
the first healthy vantage; if all healthy vantages fail a specific probe,
THEN the box-level obligation slips. Per-vantage failures over the
threshold raise `probe_vantage_unreachable::<vantage>`.

### 3.3 `src/mthydra/controller/shard_manager/wheel.py`

Currently raises `shard_overdue_pending` / `shard_unassigned_pending`
directly. Insert an attempt-to-resolve step before the raise: call the
existing reshuffle / assign functions; only raise if the attempt itself
errors or returns no progress.

### 3.4 `src/mthydra/controller/observability/heartbeat.py`

Extend `_check_breach` (or wherever the threshold check fires) with a
diagnostic helper: collect the last 3 exception strings (already in
audit-log), do a TCP+SMTP smoke check against the configured sink, and
write the verdict into the breach row's `details` JSON.

---

## 4. TDD plan

- **U-Task 1** (U-D1): cover-domain auto-reverify sweeper + tests.
- **U-Task 2** (U-D2): probe runner per-vantage failover + tests.
- **U-Task 3** (U-D3): shard wheel attempt-then-raise + tests.
- **U-Task 4** (U-D4): heartbeat-breach self-diagnosis + tests.

Each lands as one commit + push. No version bump until the broader
0.0.5 release is cut.

---

## 5. What we are deliberately NOT changing

- The obligation IDs and severities themselves — observability stays
  identical from the operator's perspective when something genuinely
  goes wrong. Only the trigger rate changes (controller absorbs the
  successes; operator sees only the failures).
- The schema. All four U-Tasks reuse existing tables; no migration.
- Final-judgment anti-obligations (`probe_kill_pending`,
  `cover_pool_rotation_frozen`, etc.) stay operator-driven.
