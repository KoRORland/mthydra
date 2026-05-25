# Spec K — User Distribution Channel — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement spec K — `doc/specs/2026-05-25-K-user-distribution-channel.md`: schema v9 → v10 with `user_channels` registry + `distribution_log` append-only via triggers; pure `distribution/payload.py` subset builder over `users` → `state.shards.list_shard_boxes` → `ru_boxes` + most-recent `onward_credentials`; two new distribution sinks (Telegram, email) distinct from spec J's observability sinks; `distribution/publisher.py` delta-only scheduler using `subset_hash` dedupe; `distribution/user_heartbeat.py` per-user dead-man's-switch (Telegram-only); six CLI subcommands; one new startup invariant (#43); three new §12 obligations + per-user anti-obligations that feed spec J's alerter (J severity table amended).

**Architecture:** Pure controller-side. The two new sinks share stdlib (`urllib.request`, `smtplib`) with spec J's sinks but live in a separate module so type identity catches accidental wiring. Subset builder is one read transaction. Publisher dedupes against `last_subset_hash` for `(user_id, channel)`. Heartbeat publisher mirrors spec J's `ObsHeartbeatPublisher` pattern with per-user counters.

**Tech stack:** Python 3.12 stdlib + APScheduler. No new runtime dependencies.

**Design decisions:** See spec §2 (K-D1 through K-D10).

---

## File Structure (locked before tasks)

**Schema + state:**
- Modify: `src/mthydra/controller/state/schema.py` — `SCHEMA_VERSION = 10`, new `migrate_v9_to_v10`, two tables + two triggers + two indexes.
- Create: `src/mthydra/controller/state/user_channels.py`.
- Create: `src/mthydra/controller/state/distribution_log.py`.

**Distribution:**
- Create: `src/mthydra/controller/distribution/__init__.py` — empty.
- Create: `src/mthydra/controller/distribution/payload.py` — `SubsetBox`, `SubsetPayload`, `build_subset`, `hash_subset`.
- Create: `src/mthydra/controller/distribution/sinks.py` — `TelegramDistributionSink`, `EmailDistributionSink`, `DryRunDistributionSink`.
- Create: `src/mthydra/controller/distribution/publisher.py` — `DistributionPublisher` scheduler.
- Create: `src/mthydra/controller/distribution/user_heartbeat.py` — `DistUserHeartbeatPublisher` scheduler.

**Config:**
- Modify: `src/mthydra/controller/config.py` — `DistributionConfig`, `DistributionTelegramConfig`, `DistributionEmailConfig` + loader.
- Modify: `packaging/etc/mthydra/controller.toml.example` — `[distribution]` section.

**Bootstrap:**
- Modify: `src/mthydra/controller/cli.py` `init` block — seed `dist_publish_sweep_ran`.

**Invariants:**
- Modify: `src/mthydra/controller/state/invariants.py` — #43 (distribution_log triggers).

**CLI:**
- Modify: `src/mthydra/controller/cli.py` — six new subcommands; refusal-to-start active-mode without sinks; arm `DistributionPublisher` + `DistUserHeartbeatPublisher` in `_cmd_serve` active.

**Spec J severity table amendment:**
- Modify: `src/mthydra/controller/observability/severity.py` — add `dist_user_unregistered` → warn, `dist_user_heartbeat_breach` → crit.
- Modify: `src/mthydra/controller/observability/snapshot.py` — add the two new prefixes to `_ANTI_PREFIXES`.

**Tests (created):**
- `tests/unit/controller/state/test_user_channels.py`
- `tests/unit/controller/state/test_distribution_log.py`
- `tests/unit/controller/state/test_distribution_log_triggers.py`
- `tests/unit/controller/distribution/__init__.py` — empty.
- `tests/unit/controller/distribution/test_payload.py`
- `tests/unit/controller/distribution/test_sinks.py`
- `tests/unit/controller/distribution/test_publisher.py`
- `tests/unit/controller/distribution/test_user_heartbeat.py`
- `tests/integration/test_distribution_lifecycle.py`

**Tests (modified):**
- `tests/unit/controller/state/test_schema.py` — v9→v10.
- `tests/unit/controller/state/test_invariants.py` — #43.
- `tests/unit/controller/test_config.py` — `[distribution]`.
- `tests/unit/controller/test_cli.py` — six new subcommands, serve arming, active-mode refusal.
- `tests/unit/controller/test_bootstrap.py` — new obligation.
- `tests/unit/controller/observability/test_severity.py` — two new mappings.
- `tests/unit/controller/observability/test_snapshot.py` — two new prefixes classified as anti.

---

## Phase 1 — Schema v10

### Task 1: v9 → v10 migration

Failing tests: schema_version==10, both tables present, triggers refuse UPDATE/DELETE, migrate idempotent.

```bash
git commit -m "schema(K): v9 -> v10 — user_channels + distribution_log + append-only triggers"
git push origin main
```

---

## Phase 2 — Repositories

### Task 2: `state/user_channels.py`

API per spec §5. Refuses upsert when BOTH telegram_chat_id and email_addr would be NULL (defence-in-depth). Audit row per upsert.

### Task 3: `state/distribution_log.py`

`append`, `last_subset_hash` (filter `kind='subset_delta' AND delivered_at IS NOT NULL`), `recent`.

### Task 4: Trigger catalogue tests

UPDATE / DELETE refused.

```bash
git commit -m "test(K): distribution_log triggers — append-only catalogue"
```

---

## Phase 3 — Payload

### Task 5: `distribution/payload.py`

Pure functions:
- `build_subset(conn, user_id, *, now)` → `SubsetPayload | None`. Joins `users` → `state.shards.list_shard_boxes` → `ru_boxes` (state in {provisioning, live}) → most-recent `onward_credentials WHERE revoked_at IS NULL` per box. Skips boxes without an active credential.
- `hash_subset(boxes)` → sha256 hex of sorted "`box_id|public_ip|sni|cred_b64`" lines.

Tests: unassigned user → None; assigned user → boxes only from their shard; hash deterministic + permutation-independent; revoked credential → box skipped.

```bash
git commit -m "payload(K): pure subset builder + deterministic hash"
```

---

## Phase 4 — Config + bootstrap

### Task 6: `DistributionConfig` + TOML

Three dataclasses; empty credentials collapse to `None`. Active-mode refusal lives in `_cmd_serve` (later task).

```bash
git commit -m "config(K): [distribution] section + dataclasses"
```

### Task 7: Bootstrap obligation

Add `"dist_publish_sweep_ran": 1` to the seed map.

```bash
git commit -m "bootstrap(K): seed dist_publish_sweep_ran obligation"
```

---

## Phase 5 — Spec J amendment

### Task 8: Severity table + snapshot prefixes

Spec K emits two new anti-obligation kinds (`dist_user_unregistered::*`, `dist_user_heartbeat_breach::*`). Spec J's alerter must classify them. Two-file change:
- `observability/severity.py`: add to `_FIXED`.
- `observability/snapshot.py`: add to `_ANTI_PREFIXES`.

Extend `tests/unit/controller/observability/test_severity.py` and `test_snapshot.py`.

```bash
git commit -m "observability(K): severity + snapshot classify dist_user_* anti-obligations"
```

---

## Phase 6 — Sinks

### Task 9: `distribution/sinks.py`

`TelegramDistributionSink(__call__(*, chat_id, message))` — POST sendMessage; injectable http_post.
`EmailDistributionSink(__call__(*, to_addr, subject, body))` — SMTP+STARTTLS; injectable smtp_factory.
`DryRunDistributionSink` — records every call.

Tests: same injected-fake pattern as spec J §12.2.

```bash
git commit -m "sinks(K): distribution Telegram + email + dryrun (separate from spec J sinks)"
```

---

## Phase 7 — Schedulers

### Task 10: `DistributionPublisher`

Per spec §7. Failing tests:
- new subset → both sinks dispatched
- same subset twice → second tick deduped (no new log rows)
- changed subset → re-dispatched with new hash
- unassigned user skipped
- both channels NULL → `dist_user_unregistered::<u>` set
- offline mode → DryRun
- heartbeat obligation `dist_publish_sweep_ran` proven each tick

```bash
git commit -m "publisher(K): delta-only per-user subset publishing scheduler"
```

### Task 11: `DistUserHeartbeatPublisher`

Per spec §7. Telegram-only. Per-user in-memory counter; breach at threshold. Tests mirror spec J §12.4.

```bash
git commit -m "user_heartbeat(K): per-user dead-man's switch — Telegram-only, 3-fail breach"
```

---

## Phase 8 — Invariant + CLI + serve

### Task 12: Invariant #43

Mirror spec J #41.

```bash
git commit -m "invariants(K): #43 distribution_log triggers present"
```

### Task 13: CLI subcommands

Six new subcommands per spec §8. Use the existing `_h_init` / `_h_cfg` helpers in test_cli.py.

```bash
git commit -m "cli(K): user-channels-set/show/list + dist-status/publish-now/test/log-recent"
```

### Task 14: Arm in `_cmd_serve` + active-mode refusal

Mirror spec J's K-D10 pattern: if `cfg.distribution.telegram is None` or `cfg.distribution.email is None` and `mode != 'offline'` and role is active, exit 2 with the message. Build distribution sinks via a helper `_build_dist_sinks(cfg, mode)` mirroring `_build_alert_sinks`. Arm both schedulers.

```bash
git commit -m "serve(K): arm DistributionPublisher + DistUserHeartbeat; refuse active without sinks"
```

---

## Phase 9 — Integration + coverage

### Task 15: Integration test

Per spec §12.7.

```bash
git commit -m "test(K): distribution lifecycle — provision -> publish -> dedupe -> reshuffle -> re-publish"
```

### Task 16: Full suite + coverage

Target: ≥ 90% on:
- `mthydra.controller.distribution.*`
- `mthydra.controller.state.user_channels`
- `mthydra.controller.state.distribution_log`

If below threshold, add error-path tests.

```bash
git commit -m "test(K): cover distribution + repos to >=90%" || echo "nothing"
git push origin main
```

---

## Done criteria

- All 16 task checkboxes ticked.
- `pytest -q` passes cleanly.
- ≥ 90% coverage on `mthydra.controller.distribution.*` and the two new `state/*` modules.
- Six new CLI subcommands work.
- Triggers `distribution_log_no_update` + `distribution_log_no_delete` enforce append-only.
- Spec invariant #43 fires when triggers are missing.
- Integration test demonstrates: register channels, run publisher → both sinks delivered, re-run → deduped, mutate boxes → re-publish with new hash.
- One new bootstrap obligation seeded (`dist_publish_sweep_ran`).
- Active-mode serve refuses to start without both sink credentials.
- Spec J severity table classifies the two new anti-obligation kinds.
- DB schema bumped to v10.
