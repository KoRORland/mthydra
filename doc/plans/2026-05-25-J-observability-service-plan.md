# Spec J — Observability Service — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement spec J — `doc/specs/2026-05-25-J-observability-service.md`: schema v8 → v9 with `alert_log` (append-only via triggers); pure `observability/snapshot.py` aggregator over `obligation_clocks` + EU heartbeat freshness + every anti-obligation kind; pluggable sinks in `observability/sinks.py` (Telegram, email, dryrun); `observability/alerter.py` AlertSweep + `observability/heartbeat.py` ObsHeartbeatPublisher schedulers; per-severity dedupe; refusal-to-start on active nodes when sink credentials missing; four CLI subcommands; two startup invariants; three new §12 obligations.

**Architecture:** Single-process additions. All emission decisions are SQLite-driven (snapshot the DB, decide, write `alert_log`). Sinks are network calls (stdlib only: `smtplib` + `urllib.request`). Tests inject fakes for both. Active-only schedulers, mirror previous wheel patterns.

**Tech stack:** Python 3.12 stdlib (`smtplib`, `urllib.request`, `email.message.EmailMessage`) + APScheduler. No new runtime dependencies.

**Design decisions:** See spec §2 (J-D1 through J-D10).

---

## File Structure (locked before tasks)

**Schema + state:**
- Modify: `src/mthydra/controller/state/schema.py` — `SCHEMA_VERSION = 9`, new `migrate_v8_to_v9`, alert_log + two triggers + two indexes; fresh-install statements updated.
- Create: `src/mthydra/controller/state/alert_log.py` — `AlertLogEntry` dataclass + append helpers + read helpers (`recent`, `last_for_key`).

**Observability:**
- Create: `src/mthydra/controller/observability/__init__.py` — empty.
- Create: `src/mthydra/controller/observability/snapshot.py` — dataclasses + `collect_snapshot(conn, now)`.
- Create: `src/mthydra/controller/observability/severity.py` — `_severity_for(kind, age, role)` lookup.
- Create: `src/mthydra/controller/observability/sinks.py` — `AlertPayload`, `SinkResult`, `TelegramAlertSink`, `EmailAlertSink`, `DryRunSink`.
- Create: `src/mthydra/controller/observability/alerter.py` — `AlertSweep` scheduler.
- Create: `src/mthydra/controller/observability/heartbeat.py` — `ObsHeartbeatPublisher` scheduler.

**Config:**
- Modify: `src/mthydra/controller/config.py` — `ObservabilityConfig`, `TelegramConfig`, `EmailConfig`; `_load_observability(raw)`; thread through `Config`.
- Modify: `packaging/etc/mthydra/controller.toml.example` — `[observability]` section.

**Bootstrap:**
- Modify: `src/mthydra/controller/cli.py` `init` block — seed three obligation_ids: `obs_alerter_sweep_ran`, `obs_heartbeat_proven`. (The third, `obs_dead_mans_switch_breach`, is an anti-obligation, not seeded at bootstrap.)

**Invariants:**
- Modify: `src/mthydra/controller/state/invariants.py` — #41 (alert_log triggers present) + #42 (heartbeat freshness at startup). Gated on schema v9+.

**CLI:**
- Modify: `src/mthydra/controller/cli.py` — four new subcommands: `obs-status`, `obs-alerts-recent`, `obs-alert-test`, `obs-heartbeat-now`. Arm `AlertSweep` + `ObsHeartbeatPublisher` in `_cmd_serve` on active. Refuse to start active mode if `[observability.telegram]` or `[observability.email]` credentials are missing.

**Tests (created):**
- `tests/unit/controller/state/test_alert_log.py`
- `tests/unit/controller/state/test_alert_log_triggers.py`
- `tests/unit/controller/observability/__init__.py` — empty
- `tests/unit/controller/observability/test_snapshot.py`
- `tests/unit/controller/observability/test_severity.py`
- `tests/unit/controller/observability/test_sinks.py`
- `tests/unit/controller/observability/test_alerter.py`
- `tests/unit/controller/observability/test_heartbeat.py`
- `tests/integration/test_observability_lifecycle.py`

**Tests (modified):**
- `tests/unit/controller/state/test_schema.py` — v8→v9
- `tests/unit/controller/state/test_invariants.py` — #41, #42
- `tests/unit/controller/test_config.py` — `[observability]`
- `tests/unit/controller/test_cli.py` — new subcommands + active-mode refusal + serve arming
- `tests/unit/controller/test_bootstrap.py` — two new obligations

---

## Phase 1 — Schema v9

### Task 1: v8 → v9 migration

**Files:**
- Modify: `src/mthydra/controller/state/schema.py`
- Modify: `tests/unit/controller/state/test_schema.py`

- [ ] **Step 1: Failing tests**

```python
def test_schema_version_is_9(tmp_path): ...
def test_alert_log_table_present(tmp_path): ...
def test_v9_alert_log_append_only_triggers(tmp_path): ...
def test_v8_to_v9_migration_idempotent(tmp_path): ...
```

- [ ] **Step 2: Implement**

Add the four trigger constants, table CREATE + two indexes, `migrate_v8_to_v9(conn)`, wire into `apply_schema`.

- [ ] **Step 3: Run + commit**

```bash
pytest tests/unit/controller/state/test_schema.py -v
git commit -m "schema(J): v8 -> v9 — alert_log + append-only triggers"
git push origin main
```

---

## Phase 2 — Repository

### Task 2: `state/alert_log.py`

**Files:**
- Create: `src/mthydra/controller/state/alert_log.py`
- Create: `tests/unit/controller/state/test_alert_log.py`

API:

```python
@dataclass(frozen=True)
class AlertLogEntry:
    id: int
    attempted_at: str
    delivered_at: str | None
    sink: str
    severity: str
    kind: str
    target: str | None
    dedupe_key: str
    payload: str
    error: str | None

def append(conn, *, attempted_at, delivered_at, sink, severity, kind,
           target, dedupe_key, payload, error) -> int: ...
def recent(conn, *, limit=50, severity=None) -> list[AlertLogEntry]: ...
def last_for_key(conn, dedupe_key: str) -> AlertLogEntry | None: ...
def last_successful_heartbeat(conn) -> AlertLogEntry | None: ...
```

- [ ] **Step 3: Run + commit**

```bash
pytest tests/unit/controller/state/test_alert_log.py -v
git commit -m "alert_log(J): append-only repository + read helpers"
git push origin main
```

### Task 3: Trigger tests

**Files:**
- Create: `tests/unit/controller/state/test_alert_log_triggers.py`

Catalogue: INSERT succeeds, UPDATE refuses, DELETE refuses.

```bash
git commit -m "test(J): alert_log triggers — append-only catalogue"
git push origin main
```

---

## Phase 3 — Config

### Task 4: Observability dataclasses + section

**Files:**
- Modify: `src/mthydra/controller/config.py`
- Modify: `packaging/etc/mthydra/controller.toml.example`
- Modify: `tests/unit/controller/test_config.py`
- Modify: `tests/unit/controller/test_cli.py` (the lone test that constructs `Config` directly)

```python
@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str
    chat_id: str

@dataclass(frozen=True)
class EmailConfig:
    smtp_host: str
    smtp_port: int
    from_addr: str
    to_addr: str
    username: str
    password: str

@dataclass(frozen=True)
class ObservabilityConfig:
    alerter_sweep_interval_seconds: int
    heartbeat_interval_seconds: int
    heartbeat_breach_threshold: int
    alert_dedupe_window_warn_seconds: int
    alert_dedupe_window_crit_seconds: int
    alert_dedupe_window_info_seconds: int
    telegram: TelegramConfig | None
    email: EmailConfig | None
```

Empty-string credentials → `None`. The active-mode refusal lives in `_cmd_serve`, not in the config loader; the loader merely parses.

```bash
pytest tests/unit/controller/test_config.py -v
git commit -m "config(J): [observability] section + dataclasses"
git push origin main
```

---

## Phase 4 — Aggregator + severity

### Task 5: `observability/snapshot.py`

**Files:**
- Create: `src/mthydra/controller/observability/__init__.py` (empty)
- Create: `src/mthydra/controller/observability/snapshot.py`
- Create: `tests/unit/controller/observability/__init__.py` (empty)
- Create: `tests/unit/controller/observability/test_snapshot.py`

- [ ] **Step 1: Failing tests** — seed a DB with one of each anti-obligation kind, a stale heartbeat, an overdue `*_proven`; assert the snapshot's fields.

- [ ] **Step 2: Implement**

Single SQL query per group:
- `obligation_clocks WHERE obligation_id LIKE '%_pending::%' OR obligation_id LIKE '%_breach' OR obligation_id IN (known anti-obligation singletons like 'cover_pool_rotation_frozen')` → anti_obligations
- `obligation_clocks WHERE obligation_id NOT LIKE '%::%' AND obligation_id NOT LIKE '%breach' AND obligation_id NOT LIKE '%frozen'` → ranked by `next_due_at` vs `now` → healthy + overdue
- `eu_nodes` → EuNodeView per row
- counts via four COUNT(*) queries

- [ ] **Step 3: Commit**

```bash
git commit -m "snapshot(J): pure aggregator over obligation_clocks + eu_nodes + anti-obligations"
git push origin main
```

### Task 6: `observability/severity.py`

**Files:**
- Create: `src/mthydra/controller/observability/severity.py`
- Create: `tests/unit/controller/observability/test_severity.py`

Pure lookup: `_severity_for(kind: str, age_seconds: int, role: str | None = None) -> str`. Hard-coded table per spec §7.

```bash
pytest tests/unit/controller/observability/test_severity.py -v
git commit -m "severity(J): hard-coded mapping kind+age+role -> info|warn|crit"
git push origin main
```

---

## Phase 5 — Sinks

### Task 7: Telegram + email + dry-run sinks

**Files:**
- Create: `src/mthydra/controller/observability/sinks.py`
- Create: `tests/unit/controller/observability/test_sinks.py`

- [ ] **Step 1: Failing tests**

```python
def test_telegram_sink_posts_success(...):
    # http_post returns 200; SinkResult.success=True; payload formatted correctly
def test_telegram_sink_handles_4xx(...): ...
def test_telegram_sink_handles_exception(...): ...
def test_email_sink_via_smtp_factory(...):
    # smtp_factory returns a recording fake; subject + body captured
def test_email_sink_quit_on_failure(...): ...
def test_dryrun_sink_always_succeeds(...): ...
```

- [ ] **Step 2: Implement**

`TelegramAlertSink` uses `urllib.request.urlopen` by default; injectable `http_post: Callable[[str, dict], int]` returns the HTTP status code. Payload: `{chat_id, text, parse_mode='Markdown'}`.

`EmailAlertSink` uses `smtplib.SMTP` by default; injectable `smtp_factory: Callable[[str, int], SMTPLike]` returns an object implementing `starttls`, `login`, `send_message`, `quit`. Use `email.message.EmailMessage` for composition.

`DryRunSink` returns success immediately.

```bash
git commit -m "sinks(J): Telegram + email + dryrun pluggable callables"
git push origin main
```

---

## Phase 6 — Schedulers

### Task 8: `AlertSweep`

**Files:**
- Create: `src/mthydra/controller/observability/alerter.py`
- Create: `tests/unit/controller/observability/test_alerter.py`

`AlertSweep` constructor accepts the sinks as injected callables (production wiring builds the real sinks from `cfg`).

- [ ] **Step 1: Failing tests**

```python
def test_sweep_dispatches_crit_to_both_sinks(...): ...
def test_sweep_dispatches_warn_only_to_telegram(...): ...
def test_sweep_skips_info(...): ...
def test_dedupe_blocks_repeat_within_window(...): ...
def test_dedupe_window_expiry_re_emits(...): ...
def test_sink_failure_recorded_in_alert_log(...): ...
def test_heartbeat_obligation_proven_each_tick(...): ...
def test_offline_mode_uses_dryrun(...): ...
```

- [ ] **Step 2: Implement**

```bash
git commit -m "alerter(J): sweep — snapshot -> dedupe -> dispatch -> log"
git push origin main
```

### Task 9: `ObsHeartbeatPublisher`

**Files:**
- Create: `src/mthydra/controller/observability/heartbeat.py`
- Create: `tests/unit/controller/observability/test_heartbeat.py`

- [ ] **Step 1: Failing tests**

```python
def test_heartbeat_dispatches_to_email_only(...): ...
def test_heartbeat_proven_obligation_set(...): ...
def test_heartbeat_failure_increments_counter(...): ...
def test_three_consecutive_failures_set_breach(...): ...
def test_success_after_failures_clears_breach(...): ...
def test_offline_mode_uses_dryrun(...): ...
```

- [ ] **Step 2: Implement**

Internal state: a `_consecutive_failures: int` counter on the instance. Persisting it is unnecessary — a controller restart resets the count, which is conservative (one missed emission won't trigger breach on restart; that's fine).

```bash
git commit -m "heartbeat(J): dead-man's switch — email-only, 3-fail breach threshold"
git push origin main
```

---

## Phase 7 — CLI + bootstrap + invariants + serve

### Task 10: Bootstrap obligations

**Files:**
- Modify: `src/mthydra/controller/cli.py` (init block)
- Modify: `tests/unit/controller/test_bootstrap.py`

Add `"obs_alerter_sweep_ran": 1` and `"obs_heartbeat_proven": 2` to the seed map.

```bash
git commit -m "bootstrap(J): seed obs_alerter_sweep_ran + obs_heartbeat_proven obligations"
git push origin main
```

### Task 11: Invariants #41, #42

**Files:**
- Modify: `src/mthydra/controller/state/invariants.py`
- Modify: `tests/unit/controller/state/test_invariants.py`

```bash
git commit -m "invariants(J): #41 alert_log triggers + #42 heartbeat freshness at startup"
git push origin main
```

### Task 12: CLI subcommands

**Files:**
- Modify: `src/mthydra/controller/cli.py`
- Modify: `tests/unit/controller/test_cli.py`

Implement: `obs-status`, `obs-alerts-recent`, `obs-alert-test`, `obs-heartbeat-now`.

`obs-alert-test --severity X --message <text>`: builds a synthetic `AlertPayload(kind='operator_test', severity=X, …)`, dispatches through configured sinks (real or DryRun based on `--mode`), records an `alert_log` row regardless of outcome.

`obs-heartbeat-now`: same flow but only the email sink.

```bash
git commit -m "cli(J): obs-status/alerts-recent/alert-test/heartbeat-now"
git push origin main
```

### Task 13: Arm schedulers in `_cmd_serve` + active-mode refusal

**Files:**
- Modify: `src/mthydra/controller/cli.py`
- Modify: `tests/unit/controller/test_cli.py`

Before constructing the schedulers in `_cmd_serve` for active mode: if `cfg.observability.telegram is None` or `cfg.observability.email is None`, print to stderr `serve: refusing — [observability.telegram] and [observability.email] are required for active mode` and return 2. Honour `mode='offline'`/`'dryrun'` by substituting `DryRunSink` for both.

Build the real sinks; pass to `AlertSweep` and `ObsHeartbeatPublisher`; arm both alongside the existing schedulers.

```bash
git commit -m "serve(J): arm AlertSweep + ObsHeartbeatPublisher; refuse active without sinks"
git push origin main
```

---

## Phase 8 — Integration test + coverage

### Task 14: Integration test

**Files:**
- Create: `tests/integration/test_observability_lifecycle.py`

End-to-end with fake sinks:
1. Bootstrap + seed an anti-obligation row (e.g. via `set_obligation` for `probe_kill_pending::b1`).
2. Build `AlertSweep` with fake sinks; `run_once()` → fake Telegram + email each called once with crit payload.
3. Re-run within dedupe window → no new dispatch.
4. Build `ObsHeartbeatPublisher` with a fake-failing email sink → after 3 ticks, `obs_dead_mans_switch_breach` row appears.
5. Replace with passing fake → next tick clears the breach + re-proves `obs_heartbeat_proven`.

```bash
git commit -m "test(J): observability lifecycle — anti-obligation -> alert -> dedupe -> heartbeat breach"
git push origin main
```

### Task 15: Full suite + coverage

```bash
pytest -q --ignore=tests/integration/test_gap_monitor.py
pytest --cov=mthydra.controller.observability \
       --cov=mthydra.controller.state.alert_log \
       --cov-report=term tests/ --ignore=tests/integration/test_gap_monitor.py
```

Expected: ≥ 90% on every new module. If not, add error-path tests.

```bash
git commit -m "test(J): cover observability + alert_log to >=90%" || echo "nothing to commit"
git push origin main
```

---

## Done criteria

- All 15 task checkboxes ticked.
- `pytest -q` passes cleanly.
- ≥ 90% coverage on `mthydra.controller.observability.*` and `mthydra.controller.state.alert_log`.
- Four new CLI subcommands work.
- Triggers `alert_log_no_update` + `alert_log_no_delete` enforce append-only.
- Spec invariants #41–#42 fire on their respective violations.
- Integration test demonstrates: anti-obligation surfaces, alerter dispatches crit to both sinks, dedupe blocks repeat, heartbeat failure streak sets the breach obligation, recovery clears it.
- Two new bootstrap obligations seeded (third is anti-obligation only).
- Active-mode serve refuses to start without both sink credentials.
- DB schema bumped to v9.
