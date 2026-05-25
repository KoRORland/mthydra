# Spec J — Observability Service

Status: **Draft, awaiting operator review.**
Predecessor: `doc/design.md` §3 (distribution / controller-state notes), §12 ("time since last proven"), §13 (dead-man's-switch as the alarm), `doc/specs/2026-05-19-C-cover-domain-pool-manager.md` (rotation_pending anti-obligations), `doc/specs/2026-05-20-F-eu-node-setup.md` (eu_nodes heartbeat), `doc/specs/2026-05-23-E-ru-eu-data-plane.md` (data_exit_state), `doc/specs/2026-05-24-H-shard-manager.md` (shard_overdue_pending), `doc/specs/2026-05-25-I-probe-vantage-harness.md` (probe_kill_pending, probe_coverage_pending, probe_evaluate_blocked, probe_vantage_rotation_pending).
Successors blocked on this: none in the MVP build order. Spec K (distribution channel) reads the same obligation table for its own gating but does not depend on J's scheduler or sinks.

---

## 1. Purpose

Define the controller's **observability service**: the snapshot aggregator that consolidates every safety-margin signal (probe-driven kill verdicts, coverage gaps, shard overdue rows, cover-pool rotation backlog, EU-node heartbeat staleness, obligation_clocks staleness), the **alerter** scheduler that dispatches severity-graded alerts through pluggable sinks (Telegram + email), the **dead-man's-switch heartbeat** that turns silence itself into the alarm (§13), and the read APIs that expose the §12 "time since last proven" metric.

Spec J is **the single place** where alerts are decided and emitted. Spec C/F/G/H/I each surface their *signal* (obligation rows, anti-obligation rows, state-table values); they do NOT send Telegram messages or emails. Spec J reads, ranks, dedupes, and dispatches. Spec K's user-facing publishing (distribution channel for user-visible boxes) is a separate Telegram channel/bot and a separate email path — not shared infrastructure with spec J.

**Out of scope:** the user distribution channel itself (spec K); generating new signals (every other spec already does that); arbitrary metric backends like Prometheus (the design's "one metric to surface" is `time since obligation X was last proven`, which is plain SQL — no time-series store needed at MVP scale); automated remediation (the operator acts on alerts; spec J only delivers them).

---

## 2. Locked design decisions

Approved during brainstorming session 2026-05-25.

| ID | Decision | Rationale |
|---|---|---|
| J-D1 | **Two alert sinks: Telegram + email. Both are required.** The operator MUST configure both at deploy time. If either credential is missing, `_cmd_serve` refuses to start (active mode). Standby mode does not require the credentials (the standby alerter is disabled — only the active node emits). | Design §3 + §13 name both channels for a reason: Telegram is fast-but-rare-takedown-vulnerable; email is slow-but-near-uncensorable. Single-channel deployments lose the safety margin the design assumes. The refusal-to-start is the *structural* enforcement of "both channels exist." |
| J-D2 | **Severity = `info` \| `warn` \| `crit`.** `crit` → both sinks; `warn` → Telegram only (low-latency but bounded chatter); `info` → no sink, just `alert_log` table + `obs-status` view. The severity of each *source* is hard-coded in the alerter (Job-3-style protocol burn is `crit`; single overdue rotation is `warn`; sweep heartbeat success is `info`). | Three severities is the smallest set the design needs (§13 demands a `crit` channel; §12's "time since proven" can degrade gracefully through `info → warn → crit` as staleness grows). Five-level severities (debug/info/warn/error/crit) are a future generality not warranted at MVP scale. |
| J-D3 | **Per-alert dedupe via `dedupe_key`.** A given (source, target) tuple cannot re-fire within `alert_dedupe_window_seconds` (default 1h for `warn`, 15 minutes for `crit` — operator-tunable per severity). Tracked in `alert_log.dedupe_key` + last-emission timestamp index. | Without dedup, a single misbehaving box would emit one alert per sweep tick. Per-severity windows recognise that `crit` re-fires should be more aggressive (the operator might have missed it) while `warn` re-fires should be quiet. |
| J-D4 | **Dead-man's-switch heartbeat is its own scheduler.** Every `heartbeat_interval_seconds` (default 1h), `ObsHeartbeatPublisher` emits a one-line "all-green at $TS, $N obligations green, $M pending" message to **email only**. Telegram is too easily silenced by a takeover; email-from-EU is the trustworthy heartbeat path per §13. The publisher records each emission attempt in `alert_log` with `kind='heartbeat'`. | Design §13 names email as the dead-man's-switch path. Telegram heartbeats would be confusable with takedown silence. Recording the attempt (success or failure) lets the operator audit, after the fact, "did my mail server actually accept it?" |
| J-D5 | **Sinks are pluggable callables.** `AlertSink = Callable[[AlertPayload], SinkResult]`. Production sinks: `EmailAlertSink` (SMTP via `smtplib`, app password) and `TelegramAlertSink` (HTTPS POST to `https://api.telegram.org/bot<token>/sendMessage`, urllib stdlib). Tests inject fakes. Offline mode replaces both with a `DryRunSink` that logs but never sends. | Mirrors how `S3Destination` is plugged into the backup pipeline (spec A). Tests don't need real SMTP/HTTP. Operators can swap in OAuth-SMTP later by writing a new sink class — spec stays unchanged. |
| J-D6 | **Snapshot aggregation is pure: `collect_snapshot(conn, now) -> Snapshot`.** Walks `obligation_clocks`, `eu_nodes`, `node_state`, `ru_boxes`, and the `probe_*` anti-obligation rows; returns a structured immutable view. No I/O outside the connection. | Pure aggregator = trivially testable, reusable by CLI (`obs-status`) and by the alerter scheduler with no plumbing duplication. |
| J-D7 | **Alert sources are hard-coded in the alerter; thresholds are config-tunable.** The alerter knows the names of the anti-obligation keys (`probe_kill_pending::*`, `probe_coverage_pending::*`, `cover_pool_rotation_pending::*`, `shard_overdue_pending::*`, `cover_pool_rotation_frozen`) and the obligation IDs (`probe_audit_sweep_ran`, `shard_reshuffle_sweep_ran`, …). New sources cost a one-line addition. | The design has six finite Tx categories; the anti-obligation keys are already standardised. Reflecting them in code keeps the alerter readable. Trying to make the source table data-driven would invite operator-typo bugs that drop alerts silently. |
| J-D8 | **`alert_log` table is append-only via trigger.** Every emission attempt (success or failure) gets a row with `attempted_at`, `delivered_at` (NULL on failure), `sink`, `severity`, `payload`, `dedupe_key`, `error`. Same discipline as `probe_results` and `burned_domains`. | Post-mortem after a missed alert: "did the email actually go out?" requires the row to survive. Append-only via trigger means even a careless DELETE can't lose the trail. |
| J-D9 | **Dead-man-switch breach is its own anti-obligation.** When `ObsHeartbeatPublisher` fails to deliver `heartbeat_breach_threshold` (default 3) consecutive heartbeats, an `obs_dead_mans_switch_breach` row is set. The next successful heartbeat clears it. This is itself a `crit` alert source — and yes, this means a *Telegram*-only alert may fire about email failure, which is the right ordering (Telegram has confirmed delivery, email does not). | Without this, "email rotted, operator did not notice" is a silent gap. The dead-man's-switch is no good if its own failure is silent. |
| J-D10 | **`audit_log` row per alert decision.** Every alert decision (fired / deduped / suppressed) writes an audit row even if the sink rejects the send. | Matches every prior spec. |

---

## 3. Schema additions (v8 → v9)

### 3.1 `alert_log` table (append-only)

```sql
CREATE TABLE alert_log (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  attempted_at  TEXT NOT NULL,
  delivered_at  TEXT,                       -- NULL on failure
  sink          TEXT NOT NULL,              -- 'telegram' | 'email' | 'dryrun'
  severity      TEXT NOT NULL CHECK (severity IN ('info','warn','crit','heartbeat')),
  kind          TEXT NOT NULL,              -- e.g. 'probe_kill_pending', 'heartbeat'
  target        TEXT,                       -- e.g. box_id, vantage_id
  dedupe_key    TEXT NOT NULL,
  payload       TEXT NOT NULL,              -- human-readable message
  error         TEXT                        -- error string on failure
);

CREATE INDEX ix_alert_log_dedupe ON alert_log(dedupe_key, attempted_at DESC);
CREATE INDEX ix_alert_log_attempted ON alert_log(attempted_at DESC);
```

### 3.2 Triggers — append-only

```sql
CREATE TRIGGER alert_log_no_update
BEFORE UPDATE ON alert_log
BEGIN
  SELECT RAISE(ABORT, 'alert-log: append-only');
END;

CREATE TRIGGER alert_log_no_delete
BEFORE DELETE ON alert_log
BEGIN
  SELECT RAISE(ABORT, 'alert-log: append-only');
END;
```

### 3.3 Schema version bump

`SCHEMA_VERSION = 9`; `migrate_v8_to_v9(conn)` creates the table + two triggers + two indexes (idempotent).

---

## 4. Snapshot structure

`mthydra.controller.observability.snapshot`:

```python
@dataclass(frozen=True)
class ObligationStatus:
    obligation_id: str
    last_proven_at: str
    next_due_at: str
    overdue_seconds: int             # 0 if not overdue
    severity: str                    # 'info' | 'warn' | 'crit'

@dataclass(frozen=True)
class AntiObligationRow:
    obligation_id: str               # e.g. 'probe_kill_pending::b1'
    last_proven_at: str
    details: str                     # JSON details column
    kind: str                        # short prefix: 'probe_kill_pending', etc.
    target: str | None               # the '::' suffix or None
    severity: str                    # derived from kind

@dataclass(frozen=True)
class EuNodeView:
    node_id: str
    role: str                        # 'active' | 'standby' | 'retired'
    last_heartbeat_at: str | None
    heartbeat_age_seconds: int | None
    data_exit_state: str | None      # 'healthy' | 'degraded' | 'stopped' | None
    severity: str

@dataclass(frozen=True)
class FleetCounts:
    boxes_provisioning: int
    boxes_live: int
    boxes_terminated: int
    cover_domains_in_use: int
    cover_domains_burned: int
    active_vantages: int
    active_shards: int

@dataclass(frozen=True)
class Snapshot:
    collected_at: str
    obligations_healthy: tuple[ObligationStatus, ...]
    obligations_overdue: tuple[ObligationStatus, ...]
    anti_obligations: tuple[AntiObligationRow, ...]
    eu_nodes: tuple[EuNodeView, ...]
    counts: FleetCounts
    summary_line: str                # one-line "$M green, $N overdue, $K anti" for heartbeat
```

`collect_snapshot(conn, now) -> Snapshot` is pure (one SQLite read transaction). Severity mapping is hard-coded per J-D7 in a small lookup table inside the module.

---

## 5. Alert sinks

`mthydra.controller.observability.sinks`:

```python
@dataclass(frozen=True)
class AlertPayload:
    severity: str
    kind: str
    target: str | None
    dedupe_key: str
    subject: str
    body: str

@dataclass(frozen=True)
class SinkResult:
    sink: str                        # 'telegram' | 'email' | 'dryrun'
    success: bool
    error: str | None

AlertSink = Callable[[AlertPayload], SinkResult]

class TelegramAlertSink:
    def __init__(self, bot_token: str, chat_id: str,
                 http_post: Callable | None = None) -> None: ...
    def __call__(self, payload: AlertPayload) -> SinkResult: ...

class EmailAlertSink:
    def __init__(self, smtp_host: str, smtp_port: int,
                 from_addr: str, to_addr: str,
                 username: str, password: str,
                 smtp_factory: Callable | None = None) -> None: ...
    def __call__(self, payload: AlertPayload) -> SinkResult: ...

class DryRunSink:
    def __call__(self, payload: AlertPayload) -> SinkResult: ...
```

`http_post` and `smtp_factory` are dependency-injected for tests.

---

## 6. Alerter scheduler

`mthydra.controller.observability.alerter.AlertSweep`:

- Cadence: `alerter_sweep_interval_seconds` (default `2m`)
- Action:
  1. `snapshot = collect_snapshot(conn, now)`
  2. For each `anti_obligations` row: decide severity (J-D7 table); check `alert_log` for an existing row with same `dedupe_key` within the severity's dedupe window. If not deduped: build `AlertPayload`, dispatch to sinks per severity (`crit → [telegram, email]`, `warn → [telegram]`, `info → []`).
  3. For each `obligations_overdue` row: same flow, with severity grown by staleness band (`overdue < 2×window: warn`; `≥ 2×window: crit`).
  4. For each EU node with `heartbeat_age_seconds > standby.staleness_alert_seconds`: warn (active node missing heartbeat in a Case-A/B promotion context is `crit`).
  5. Append `alert_log` rows for every emission attempt (delivered or not). Audit row per *decision*.
  6. Heartbeat obligation `obs_alerter_sweep_ran` proven each tick.
- `mode='offline'` → use `DryRunSink` for both; never call real SMTP/HTTP.

`mthydra.controller.observability.heartbeat.ObsHeartbeatPublisher`:

- Cadence: `heartbeat_interval_seconds` (default `1h`)
- Action:
  1. `snapshot = collect_snapshot(conn, now)`
  2. Compose a one-line `summary_line` heartbeat email.
  3. Dispatch via `EmailAlertSink` only (J-D4). Telegram heartbeats are deliberately NOT sent.
  4. On success: clear `obs_dead_mans_switch_breach` obligation if present; prove `obs_heartbeat_proven`.
  5. On failure: increment a counter in memory (heartbeat publisher state); after `heartbeat_breach_threshold` (default 3) consecutive failures, set `obs_dead_mans_switch_breach` anti-obligation row (which the next alerter sweep dispatches as `crit`-Telegram per the irony in J-D9).

Both are armed in `_cmd_serve` on active nodes; both short-circuit in `mode='offline'`.

---

## 7. Severity table (J-D7)

| Source | Severity rule |
|---|---|
| `probe_kill_pending::*` | **crit** |
| `probe_evaluate_blocked::*` | **warn** (missing image profile is a configuration error, not a kill) |
| `probe_coverage_pending::*` | **warn** (first 2h) → **crit** (≥ 6h) |
| `probe_vantage_rotation_pending::*` | **info** (T7 rotation prompt; operator-driven, low urgency) |
| `cover_pool_rotation_pending::*` | **warn** |
| `cover_pool_rotation_frozen` | **crit** (T5 floor hit) |
| `shard_overdue_pending::*` | **warn** |
| `shard_unassigned_pending::*` | **info** |
| `obs_dead_mans_switch_breach` | **crit** (Telegram-only, since email is the failing channel) |
| Heartbeat obligation `*_sweep_ran` overdue by < 2× cadence | **warn** |
| Heartbeat obligation overdue by ≥ 2× cadence | **crit** |
| `*_proven` obligation overdue by < 2× cadence | **warn** |
| `*_proven` obligation overdue by ≥ 2× cadence | **crit** |
| EU node `last_heartbeat_age > staleness_alert_seconds` | **warn** (standby) / **crit** (active) |

The table is hard-coded; a small `_severity_for(kind, age_seconds, role)` function makes the mapping testable.

---

## 8. Config additions

New `[observability]` section in `controller.toml`:

```toml
[observability]
alerter_sweep_interval      = "2m"
heartbeat_interval           = "1h"
heartbeat_breach_threshold   = 3
alert_dedupe_window_warn_seconds = 3600
alert_dedupe_window_crit_seconds = 900
alert_dedupe_window_info_seconds = 21600

[observability.telegram]
bot_token = ""    # operator alert channel — NOT the user distribution channel
chat_id   = ""    # operator's private chat or alert group

[observability.email]
smtp_host = ""
smtp_port = 587
from_addr = ""
to_addr   = ""
username  = ""
password  = ""    # app password; deployment-runbook discipline keeps this out of git
```

Loaded into `ObservabilityConfig`, `TelegramConfig`, `EmailConfig` dataclasses.

**Active-mode refusal:** `_cmd_serve` on `node.role='active'` refuses to start if either of `[observability.telegram]` or `[observability.email]` has an empty required field (J-D1).

---

## 9. CLI surface

```
mthydra-controller obs-status [--json]
    # one-screen dump of collect_snapshot(); useful for ad-hoc operator checks.

mthydra-controller obs-alerts-recent [--limit 50] [--severity ...] [--json]
    # tail of alert_log; most-recent first.

mthydra-controller obs-alert-test --severity {info|warn|crit} \
    [--message <text>]
    # synthetic alert; dispatches through configured sinks. Useful for
    # confirming credentials work post-deploy. Records a normal alert_log row.

mthydra-controller obs-heartbeat-now
    # forces an immediate heartbeat (bypasses the cadence). Useful for
    # debugging SMTP setup.
```

No `obs-alerts-clear` — `alert_log` is append-only by design.

---

## 10. Startup invariants (extend `state/invariants.py` with #41–#42)

| ID | Statement | Failure → |
|---|---|---|
| #41 | `alert_log` triggers `alert_log_no_update` + `alert_log_no_delete` are present. | `alert-log triggers missing` |
| #42 | On active nodes (role='active'): if any `alert_log` rows exist, the most-recent successful heartbeat (`severity='heartbeat' AND delivered_at IS NOT NULL`) must be within `2 × heartbeat_interval_seconds`. (Caveat: only fires when alert_log has any rows — fresh installs have no rows yet, no spurious raise.) | `dead-man's-switch breach detected at startup` |

#42 is the *startup* version of J-D9; the running alerter handles the live case.

---

## 11. §12 obligation contributions

| Obligation key | Cadence / semantics |
|---|---|
| `obs_alerter_sweep_ran` | hourly heartbeat — sweep alive |
| `obs_heartbeat_proven` | proven on every successful heartbeat email; healthy interval = `heartbeat_interval_seconds × 2` |
| `obs_dead_mans_switch_breach` | anti-obligation; presence = `heartbeat_breach_threshold` consecutive heartbeat failures |

---

## 12. Test plan

### 12.1 Unit — snapshot

- `tests/unit/controller/observability/test_snapshot.py` — given a seeded DB, the snapshot returns the expected counts, the anti-obligations are split out, severity mapping matches the §7 table.

### 12.2 Unit — sinks

- `tests/unit/controller/observability/test_sinks.py`
  - `TelegramAlertSink` with injected `http_post` — returns `SinkResult(success=True)` on 200; `(success=False, error='...')` on non-200 or exception.
  - `EmailAlertSink` with injected `smtp_factory` — SUBJECT and BODY appear in the captured message; SMTP `quit` is always called.
  - `DryRunSink` — always success, never raises.

### 12.3 Unit — alerter

- `tests/unit/controller/observability/test_alerter.py`
  - seeded anti-obligation row → fake sink called with the right severity
  - dedupe: same `dedupe_key` within window → second sweep does NOT call the sink
  - dedupe window expired → second sweep calls the sink again
  - `crit` dispatches to BOTH sinks; `warn` only to Telegram; `info` skipped
  - sink failure recorded in `alert_log` with `error`
  - `obs_alerter_sweep_ran` heartbeat proven each tick

### 12.4 Unit — heartbeat publisher

- `tests/unit/controller/observability/test_heartbeat.py`
  - successful tick: dispatches to email sink, proves `obs_heartbeat_proven`, clears breach row if present
  - failure tick: counter increments
  - 3 consecutive failures: sets `obs_dead_mans_switch_breach` row
  - 3 fails followed by a success: clears the breach row

### 12.5 Trigger tests

- `tests/unit/controller/state/test_alert_log_triggers.py`
  - INSERT succeeds
  - UPDATE / DELETE refused

### 12.6 Invariant tests

- extend `tests/unit/controller/state/test_invariants.py` — #41 missing-trigger; #42 stale-heartbeat at startup

### 12.7 CLI tests

- extend `tests/unit/controller/test_cli.py` — `obs-status --json`, `obs-alerts-recent --json`, `obs-alert-test --severity crit` happy paths

### 12.8 Integration

- `tests/integration/test_observability_lifecycle.py`
  - bootstrap + provision + spec-I probe results
  - run one alerter sweep with fake sinks → alert dispatched, alert_log row, dedupe holds on second sweep
  - heartbeat publisher tick → email sink called, obligation proven
  - one heartbeat failure scenario → breach obligation set after 3 fails

### 12.9 Failure-mode catalogue

| Failure | Behaviour |
|---|---|
| SMTP server unreachable | `alert_log.delivered_at IS NULL`, `error` populated; heartbeat publisher counter increments |
| Telegram bot token revoked | Same — `alert_log` records the 401; alerter still ran (no exception escapes) |
| `[observability.email]` missing on active node | `_cmd_serve` exits 2 with `observability.email required for active node` |
| Alerter sweep races a probe-record insert | SQLite serialises; whichever commits first wins; second sweep cleans up |
| `obs-alert-test` with no `--message` | Default subject `"mthydra alert test"`, default body lists the snapshot summary line |
| Clock skew across dedupe boundary | Same accepted residual as elsewhere |

---

## 13. Cross-spec contracts

| Source spec | What J consumes | Notes |
|---|---|---|
| Spec A | `obligation_clocks`, `eu_nodes.last_heartbeat_at`, `backup_log` | Read-only |
| Spec C | `cover_pool_rotation_pending::*`, `cover_pool_rotation_frozen` | Read-only |
| Spec F | `eu_nodes` heartbeat freshness, `node_state.role` | Read-only |
| Spec H | `shard_overdue_pending::*`, `shard_unassigned_pending::*` | Read-only |
| Spec I | `probe_kill_pending::*`, `probe_coverage_pending::*`, `probe_evaluate_blocked::*`, `probe_vantage_rotation_pending::*` | Read-only |

Spec J writes only to `alert_log`, `audit_log`, and `obligation_clocks` (its own heartbeat keys + the breach anti-obligation). It does not mutate any other spec's tables.

**Spec K coupling:** spec K will publish to a *different* Telegram channel (the user distribution channel). Spec J's `[observability.telegram]` and spec K's `[distribution.telegram]` are separate config sections, separate bots (or at minimum, separate chat_ids). The cross-spec discipline is documented in the deployment runbook; spec J does not enforce non-overlap structurally because the operator may legitimately want a single ops chat for both.

---

## 14. Honest residuals (Spec J)

1. **Email delivery is best-effort.** SMTP-with-app-password is well-understood but mailbox providers occasionally throttle, classify as spam, or rate-limit on app-password auth. Spec J detects send failure (`smtplib` exceptions) and records it, but cannot detect silent dropped-into-junk delivery. The dead-man's-switch breach catches *prolonged* failure; transient drops can still hide one alert. Operator must whitelist the From address out-of-band.
2. **Telegram bot tokens are credentials with broad scope.** A leaked operator bot token lets an adversary send messages to the configured chat — confusing the operator and possibly faking "all-green" coverage. Mitigation: token is held only in `controller.toml` on the EU node (encrypted at rest by spec A's controller-state backup, but plain text on the EU node itself). A future enhancement could move the token into a hardware-token-signed channel; out of scope for MVP.
3. **Dedupe is per-`dedupe_key`, not per-target context.** Two distinct `probe_kill_pending` boxes produce two alerts (different `dedupe_key`); but if a box flaps between hard_kill and healthy verdicts, dedupe currently allows re-firing once the window expires. Choosing tighter dedupe risks missing real flap-and-recur signals; choosing looser risks alert fatigue. Operator-tunable; defaults are conservative.
4. **No alert acknowledgement mechanism.** The operator reading an alert does not record that they read it. A repeating `crit` may surface every dedupe window until the underlying obligation clears. By design — silence about a `crit` is *not* a signal that the operator acknowledged it. A future `obs-alert-ack` is named (post-MVP).
5. **Heartbeat email may be flagged as spam by providers running aggressive filters.** A heartbeat sent every hour from the same EU IP to the same mailbox looks like a service notification. If the operator's spam filter buries them, the dead-man's-switch becomes silent in both directions. Out-of-band runbook check: the operator must whitelist + verify reception monthly.
6. **The alerter does not page on Sundays-vs-weekdays or business hours.** Every `crit` fires immediately. The design's user base is small enough that on-call rotations are out of scope; the operator IS the on-call. Mentioned in case the deployment ever grows.
7. **`alert_log` grows unbounded.** At default 2m sweep cadence × ~5 anti-obligation kinds × dedupe windows, expect ~50 rows/day in steady state, more during incidents. SQLite handles years of this without trouble, but a future maintenance job will need to compact. Not implemented.
8. **Snapshot aggregation can miss a row written between transaction snapshots.** SQLite's default isolation level is `DEFERRED`; the snapshot is consistent within one read transaction. Two snapshots taken seconds apart may differ — this is correct behaviour but worth naming so post-mortem analysis doesn't expect frozen-in-time guarantees.

---

## 15. §12 obligation summary (deployment-runbook view)

| Obligation | Healthy interval | What "proven" means |
|---|---|---|
| `obs_alerter_sweep_ran` | ≤ 1h | Sweep ran |
| `obs_heartbeat_proven` | ≤ `heartbeat_interval_seconds × 2` (default 2h) | At least one heartbeat email actually accepted by SMTP |
| `obs_dead_mans_switch_breach` | absent | No heartbeat-failure streak ≥ threshold |

**Deployment runbook addition:**
- Configure `[observability.telegram]` + `[observability.email]` before first `_cmd_serve`.
- Run `obs-alert-test --severity crit` once at deploy time to confirm both sinks deliver.
- Whitelist the From address in the operator's mailbox.
- Re-test `obs-alert-test` after every config rotation (provider key changes, app password rotations).
