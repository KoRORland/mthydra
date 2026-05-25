# Spec K — User Distribution Channel

Status: **Draft, awaiting operator review.**
Predecessor: `doc/design.md` §3 (distribution paragraph: "private Telegram channel/bot publishing a *rotating subset* ... Backstop: email ... deltas only, never a standing full inventory"), T10 (multi-path publishing), `doc/specs/2026-05-18-A-controller-state-and-backup.md` §4.5 (`published_subsets` table — fleet-wide audit), `doc/specs/2026-05-20-F-eu-node-setup.md` (active-only behaviour), `doc/specs/2026-05-24-H-shard-manager.md` (H-D7 published-subset partitioning + `list_shard_boxes`), `doc/specs/2026-05-25-J-observability-service.md` (sink patterns + dead-man's-switch discipline).
Successors blocked on this: none in MVP. Future T1 break-glass (spec L, deferred) will reuse the per-user channel registry.

---

## 1. Purpose

Define the **per-user distribution channel**: the controller-side publisher that sends a **rotating per-shard subset** of currently-live boxes to each registered user via Telegram (primary) + email (backstop), *deltas only* per design §3. The publisher is the load-bearing user-facing surface — it is *the* thing that lets a user reach Telegram when an in-point rotates.

Spec K consumes spec H's `list_shard_boxes(shard_id)` read API (H-D7). It does NOT mutate any other spec's tables; it adds two new ones (`user_channels` registry, `distribution_log` append-only history).

**Distinct from spec J:** J's Telegram + email sinks alert the **operator** about controller-internal state (kill-pending, heartbeat breaches). K's Telegram + email sinks publish **user-facing connection deltas** to *circle members*. They share the underlying SMTP/HTTP technology but use **separate bot tokens, separate chat IDs, separate authentication, separate config sections**. The deployment-runbook discipline is to register two distinct Telegram bots: one operator-alert bot, one circle-distribution bot.

**Out of scope:** an actual Telegram-app integration that auto-applies the proxy URL on the user's phone — that's a client UX problem and belongs to the user-onboarding kit (T1 break-glass, spec L). Spec K MVP delivers the structured proxy-link payload; the user's phone-side recipe (preinstalled MTProto-aware client, tap-to-apply prefab proxy) is set up calmly during onboarding (design §6.1 Part 1). Also out of scope: anti-correlation between user-IDs and Telegram chat IDs — spec K assumes the operator has chosen a per-user chat_id mapping that does NOT itself leak the user↔circle binding (e.g., direct DM rather than a shared group; named in residual #5).

---

## 2. Locked design decisions

Approved during brainstorming session 2026-05-25.

| ID | Decision | Rationale |
|---|---|---|
| K-D1 | **Two distribution sinks per user, separate from spec J.** Each registered user has a `telegram_chat_id` AND an `email_addr` recorded in `user_channels`. Both are delivered to on every meaningful change. The `[distribution.telegram]` config holds a *different* bot token than `[observability.telegram]` (J-D1). | T10 "multi-path publishing" — if Telegram is taken down, email gets the delta out; if email rots, Telegram still gets it. Two channels also means the user can confirm receipt out-of-band ("did the delta land?") on whichever channel still works. |
| K-D2 | **Delta-only publishing.** Each tick computes the per-user "current subset" (the set of `box_id`+`public_ip`+`sni` rows from `list_shard_boxes(users.current_shard_id, include_terminated=False)`), hashes it, and compares to the last successful `distribution_log` row for the same `(user_id, channel)`. Only when the hash differs is a new payload sent. | Design §3 is explicit: "deltas only, never a standing full inventory." Constantly re-sending the same boxes burns the publisher's Telegram quota and trains the user to ignore the channel. |
| K-D3 | **Subset payload is structured JSON, NOT a `tg://proxy?…` URL.** Each box dict carries `{box_id, public_ip, port, sni, credential_b64}`. The operator's onboarding kit (or a future client-side helper) converts to whichever proxy URL the user's app expects. | The exact `tg://proxy?…` format depends on the MTProto Fake-TLS variant + the secret encoding scheme, both of which may change with upstream (T4). Spec K stays format-agnostic; the format is parameterised in the deployment runbook, not baked into the spec. |
| K-D4 | **`user_channels` is operator-managed via CLI.** No self-service. `user-channels-set <user_id> --telegram <chat_id> --email <addr>` upserts the row; `user-channels-show <user_id>` displays. Updates are audited. | Self-service registration would require an unauthenticated entry point — too easy to spam, and the design's "trusted circle" premise (§3) wants the operator vetting every new contact-point. |
| K-D5 | **`distribution_log` is append-only via triggers.** One row per send attempt per (user_id, channel). Records `attempted_at`, `delivered_at` (NULL on failure), `subset_hash`, `payload_json`, `error`. Same discipline as `alert_log` (spec J). | Post-incident: "did this user get the delta?" requires the row to survive. |
| K-D6 | **Unassigned users get nothing.** `users.current_shard_id IS NULL` → no subset, no log row, no heartbeat. Spec H H-D9 names unassigned users as "first-class" precisely so they don't enter publishing. | Avoids leaking the new-user signal into the distribution channel before they have any shard mapping. |
| K-D7 | **Per-user dead-man's-switch heartbeat.** Daily "still here" pulse to each user with a registered channel (Telegram only — email heartbeats train users to ignore; the silence-on-Telegram-IS-the-alarm pattern is symmetric to J-D4 but at the user end, where the silence ALSO matters). On `breach_threshold` consecutive failures (default 3), emit `dist_user_heartbeat_breach::<user_id>` anti-obligation → spec J's alerter dispatches `crit` to the operator. | Without this, a per-user Telegram channel takedown is silent until the user complains. Heartbeat makes it loud at the controller. Telegram-only because email per-day spam would be unkind to the user. |
| K-D8 | **Active-only.** The publisher does not arm on standby. Standby promotion (spec F) reawakens the publisher; the first sweep republishes any in-flight delta the active node missed. | Same reasoning as every other active-mutating scheduler. |
| K-D9 | **`audit_log` row per publish decision.** Every send attempt + every dedup + every channel-registration writes audit. | Matches the pattern across every spec. |
| K-D10 | **Active-mode refusal mirrors spec J.** If `[distribution.telegram]` or `[distribution.email]` credentials are missing on `node.role='active'`, `_cmd_serve` refuses to start. Standby and offline don't require credentials. | If the user-facing publisher is dead at boot, the operator must KNOW before the controller settles into "all systems normal." Hard-fail is the only credible signal. |

---

## 3. Schema additions (v9 → v10)

### 3.1 `user_channels` table

```sql
CREATE TABLE user_channels (
  user_id           TEXT PRIMARY KEY REFERENCES users(user_id),
  telegram_chat_id  TEXT,
  email_addr        TEXT,
  registered_at     TEXT NOT NULL,
  updated_at        TEXT NOT NULL
);
```

Either column may be NULL — a user with only Telegram (no email backstop) is allowed but the operator-runbook warns against it. A row with *both* NULL is allowed (legacy migration) but the publisher skips it with an audit row.

### 3.2 `distribution_log` table (append-only)

```sql
CREATE TABLE distribution_log (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id        TEXT NOT NULL REFERENCES users(user_id),
  channel        TEXT NOT NULL CHECK (channel IN ('telegram','email','dryrun')),
  kind           TEXT NOT NULL,         -- 'subset_delta' | 'heartbeat'
  attempted_at   TEXT NOT NULL,
  delivered_at   TEXT,                  -- NULL on failure
  subset_hash    TEXT,                  -- present on subset_delta rows; NULL on heartbeat
  payload_json   TEXT NOT NULL,
  error          TEXT
);

CREATE INDEX ix_distribution_log_user_channel
  ON distribution_log(user_id, channel, attempted_at DESC);
CREATE INDEX ix_distribution_log_attempted
  ON distribution_log(attempted_at DESC);
```

### 3.3 Triggers — append-only

```sql
CREATE TRIGGER distribution_log_no_update
BEFORE UPDATE ON distribution_log
BEGIN
  SELECT RAISE(ABORT, 'distribution-log: append-only');
END;

CREATE TRIGGER distribution_log_no_delete
BEFORE DELETE ON distribution_log
BEGIN
  SELECT RAISE(ABORT, 'distribution-log: append-only');
END;
```

### 3.4 Schema version bump

`SCHEMA_VERSION = 10`; `migrate_v9_to_v10(conn)` creates both tables + two triggers + two indexes (idempotent).

---

## 4. Payload structure

`mthydra.controller.distribution.payload`:

```python
@dataclass(frozen=True)
class SubsetBox:
    box_id: str
    public_ip: str
    port: int
    sni: str
    credential_b64: str         # base64 of onward_credentials.credential blob

@dataclass(frozen=True)
class SubsetPayload:
    user_id: str
    shard_id: str
    generated_at: str
    boxes: tuple[SubsetBox, ...]
    subset_hash: str            # sha256 hex of sorted (box_id|public_ip|sni|cred) lines

def hash_subset(boxes: list[SubsetBox]) -> str: ...
def build_subset(conn, user_id: str, *, now: str) -> SubsetPayload | None: ...
```

`build_subset` returns `None` for an unassigned user (K-D6). Otherwise it walks `users.current_shard_id` → `state.shards.list_shard_boxes(shard, include_terminated=False)` → `ru_boxes` + most-recent non-revoked `onward_credentials.credential`. Boxes without an active credential are skipped (with an audit row in the publisher caller).

---

## 5. Repository API

`mthydra.controller.state.user_channels`:

```python
@dataclass(frozen=True)
class UserChannelRow:
    user_id: str
    telegram_chat_id: str | None
    email_addr: str | None
    registered_at: str
    updated_at: str

def set_channels(conn, user_id, *, telegram_chat_id, email_addr, at) -> None: ...
def get_channels(conn, user_id) -> UserChannelRow | None: ...
def list_channels(conn) -> list[UserChannelRow]: ...
```

`mthydra.controller.state.distribution_log`:

```python
@dataclass(frozen=True)
class DistributionLogEntry:
    id: int
    user_id: str
    channel: str
    kind: str
    attempted_at: str
    delivered_at: str | None
    subset_hash: str | None
    payload_json: str
    error: str | None

def append(conn, *, user_id, channel, kind, attempted_at,
           delivered_at, subset_hash, payload_json, error) -> int: ...
def last_subset_hash(conn, user_id, channel) -> str | None: ...
def recent(conn, *, user_id=None, limit=50) -> list[DistributionLogEntry]: ...
```

---

## 6. Sinks (distribution-side)

`mthydra.controller.distribution.sinks`:

Two classes structurally identical to spec J's sinks but with separate type identity (different module, different default Bot token field) so the type system catches accidental cross-wiring:

```python
class TelegramDistributionSink:
    def __init__(self, bot_token, ...): ...
    def __call__(self, *, chat_id: str, message: str) -> SinkResult: ...

class EmailDistributionSink:
    def __init__(self, smtp_host, ...): ...
    def __call__(self, *, to_addr: str, subject: str, body: str) -> SinkResult: ...

class DryRunDistributionSink: ...
```

`SinkResult` is reused from `mthydra.controller.observability.sinks` (it's a generic dataclass). Two sinks share the `from email.message.EmailMessage` boilerplate via a small internal helper.

---

## 7. Publisher scheduler

`mthydra.controller.distribution.publisher.DistributionPublisher`:

- Cadence: `publish_sweep_interval_seconds` (default `5m`)
- Per tick:
  1. For each user with `current_shard_id IS NOT NULL`:
     - `build_subset(conn, user_id, now=...)`. If `None`: skip.
     - `get_channels(conn, user_id)`. If both telegram + email are NULL: emit a `dist_user_unregistered::<user_id>` anti-obligation (which spec J's alerter then escalates as `warn`); skip.
     - For each configured channel: compare `subset_hash` to `last_subset_hash(conn, user_id, channel)`. If equal → skip (deduped). If different → call the sink with the rendered message, append a `distribution_log` row.
  2. Audit row per dispatch + per skip-for-no-channels.
  3. Heartbeat obligation `dist_publish_sweep_ran` proven each tick.
- `mode='offline'` → use `DryRunDistributionSink` for both.

`mthydra.controller.distribution.heartbeat.DistUserHeartbeatPublisher`:

- Cadence: `user_heartbeat_interval_seconds` (default `1d`)
- Per tick: for each user with `telegram_chat_id IS NOT NULL`:
  - Send a single-line "still here at $TS" via Telegram only (K-D7).
  - On success: clear `dist_user_heartbeat_breach::<user_id>` if present; prove `dist_user_heartbeat_proven::<user_id>`.
  - On failure: increment in-memory per-user counter; at threshold, set the breach anti-obligation row.
- `mode='offline'` → DryRun.

Both are armed in `_cmd_serve` active-mode only.

---

## 8. CLI surface

```
mthydra-controller user-channels-set <user_id> \
    [--telegram <chat_id>] [--email <addr>]
    # upsert. At least one of --telegram or --email must be provided.

mthydra-controller user-channels-show <user_id> [--json]

mthydra-controller user-channels-list [--json]

mthydra-controller dist-status [--json]
    # for each registered user: their shard_id, current subset count,
    # last-successful-delivery time per channel.

mthydra-controller dist-publish-now --user-id <id> [--config <path>]
    # forces an immediate per-user publish (bypasses the cadence). Useful
    # for first-time onboarding before the next sweep.

mthydra-controller dist-test --user-id <id> [--config <path>]
    # sends a synthetic "test" message (NOT a subset; clearly labelled)
    # to confirm credentials reach the user. Recorded with kind='test'.

mthydra-controller dist-log-recent [--user-id <id>] [--limit 50] [--json]
```

---

## 9. Config additions

```toml
[distribution]
publish_sweep_interval        = "5m"
user_heartbeat_interval        = "24h"
heartbeat_breach_threshold     = 3

[distribution.telegram]
bot_token = ""    # separate from [observability.telegram].bot_token

[distribution.email]
smtp_host = ""
smtp_port = 587
from_addr = ""
username  = ""
password  = ""
# per-user 'to_addr' lives in user_channels.email_addr, not here.
```

Loaded into `DistributionConfig` + `DistributionTelegramConfig` + `DistributionEmailConfig`. Missing/empty creds collapse to `None`; active-mode refuses to start.

---

## 10. Startup invariants (extend with #43)

| ID | Statement | Failure → |
|---|---|---|
| #43 | `distribution_log` triggers `distribution_log_no_update` + `distribution_log_no_delete` are present. | `distribution-log triggers missing` |

(Per-user channel completeness is *not* a startup invariant — operator may add users between server restarts. The publisher emits an anti-obligation when a user has neither channel.)

---

## 11. §12 obligation contributions

| Obligation key | Cadence / semantics |
|---|---|
| `dist_publish_sweep_ran` | hourly heartbeat — sweep is alive |
| `dist_user_heartbeat_proven::<user_id>` | per-user; healthy interval = `user_heartbeat_interval × 2` |
| `dist_user_heartbeat_breach::<user_id>` | anti-obligation; presence = N consecutive heartbeat failures for that user |
| `dist_user_unregistered::<user_id>` | anti-obligation; presence = assigned user has no Telegram OR email channel |

The per-user obligation rows feed spec J's alerter — operator gets a Telegram + email alert when a user's distribution path breaks.

---

## 12. Test plan

### 12.1 Unit — repositories

- `tests/unit/controller/state/test_user_channels.py` — upsert, refuse when both NULL on insert path, `list_channels` ordering.
- `tests/unit/controller/state/test_distribution_log.py` — append, `last_subset_hash` returns most recent delivered, `recent` filters.
- `tests/unit/controller/state/test_distribution_log_triggers.py` — UPDATE / DELETE refused.

### 12.2 Unit — payload

- `tests/unit/controller/distribution/test_payload.py` — `build_subset` returns None for unassigned, correct boxes for assigned, `hash_subset` is deterministic + order-independent.

### 12.3 Unit — sinks

- `tests/unit/controller/distribution/test_sinks.py` — Telegram + email + dryrun via injected fakes (same pattern as spec J §12.2).

### 12.4 Unit — publisher

- `tests/unit/controller/distribution/test_publisher.py`
  - delta-only: same subset twice → only one dispatch per channel
  - new subset (e.g. shard reshuffle changed memberships) → re-dispatch
  - unassigned user → skip + no log row
  - user with both channels NULL → emit `dist_user_unregistered::<user_id>` anti-obligation
  - sink failure recorded with `delivered_at IS NULL`
  - heartbeat obligation proven each tick

### 12.5 Unit — user heartbeat

- `tests/unit/controller/distribution/test_user_heartbeat.py`
  - single tick success → `dist_user_heartbeat_proven::<u>` set
  - 3 consecutive failures → breach row appears
  - recovery clears breach

### 12.6 CLI tests

- `tests/unit/controller/test_cli.py` — `user-channels-set/show/list`, `dist-status`, `dist-publish-now`, `dist-test`, `dist-log-recent`.

### 12.7 Integration

- `tests/integration/test_distribution_lifecycle.py`
  - bootstrap; add user; create shard; provision box; mark live; register channels.
  - `DistributionPublisher.run_once()` → both sinks called, log rows appended, subset_hash recorded.
  - Re-run → deduped (no new rows).
  - Spec H compromise-reshuffle changes the shard's box set → next sweep re-dispatches with a new hash.

### 12.8 Failure-mode catalogue

| Failure | Behaviour |
|---|---|
| `user-channels-set` with neither flag | argparse error before main |
| Sink returns 4xx | `distribution_log.delivered_at IS NULL`, `error` populated |
| User assigned but no channels | `dist_user_unregistered::<u>` anti-obligation; spec J escalates as warn |
| Build subset for a box with no active credential | skip that box; payload may be empty; if empty, still emit (operator notices the "0 boxes" delta) |
| `_cmd_serve` active without distribution sinks | refuses to start with exit 2 |
| Clock skew across heartbeat interval | accepted residual same as elsewhere |

---

## 13. Cross-spec contracts

| Source spec | What K consumes | Notes |
|---|---|---|
| Spec A | `users`, `ru_boxes`, `onward_credentials`, `audit_log` | Read-only |
| Spec H | `state.shards.list_shard_boxes(shard_id)` (read API; H-D7) | Read-only. Spec H's invariants #33–#36 already guarantee the join is consistent |
| Spec J | The `dist_user_heartbeat_breach::*` + `dist_user_unregistered::*` anti-obligation kinds add to spec J's alerter severity table. Spec J is amended (see §14 follow-up) | One-way: K emits, J alerts |
| Spec G | When a box is terminated (`ru_box_terminate`), the live-boxes set shrinks → next sweep republishes a new delta to affected users. No direct API call. | Implicit |

**Spec J amendment (follow-up):** Spec J's severity table (J §7) gains two rows for `dist_user_unregistered::*` (warn) and `dist_user_heartbeat_breach::*` (crit). Spec J's snapshot module gains the two anti-obligation kinds in its prefix set. This amendment ships in the spec K implementation.

---

## 14. Honest residuals (Spec K)

1. **Per-user chat_id is the user↔circle binding.** A leaked operator distribution bot token gives the adversary the full mapping of `user_id → telegram_chat_id` (and thus, with social-graph correlation, real-world identity) for every member of the circle. Mitigation: token lives only on the EU controller (encrypted at rest via spec A backup; plain text on the live node). A future enhancement would use distinct bots per user — too costly to operate at any non-trivial circle size. Stated; not solved.
2. **Email delivery is best-effort.** Same residual as spec J §11 residual #1. Heartbeat-via-Telegram catches *prolonged* email rot but a single dropped delta can hide for one sweep.
3. **No subset is per-user-personalised at the credential level.** Per H-D7, the published-subset partitioning IS the per-user discipline — users see only their shard's boxes. But two users in the same shard see the *same* boxes with the *same* credentials. A compromised user device leaks the shard's credentials. Spec H's reshuffle TTL bounds this; spec K does not add a per-user credential layer.
4. **Delta-only publishing means a user's first message after onboarding contains their **entire** initial subset.** This is the only large payload in steady-state. Honest about: an adversary watching Telegram payload sizes can detect "new user joined the circle" from this single large delta. Mitigation: pad the initial payload to a sweep's typical size (not implemented in MVP).
5. **The user↔chat_id mapping itself leaks the circle membership** even with perfect MTProto crypto, if the distribution bot is observed sending to N specific chat IDs. Mitigation requires either (a) one bot per user (cost-prohibitive), or (b) routing all sends through a single channel ID where users self-filter — abandoning the per-user privacy. Spec K accepts the per-user-DM model and names the leak.
6. **`dist-test` and per-user heartbeats add Telegram noise.** Each user receives one heartbeat per day plus any actual delta. For a 5-user circle, this is ~5/day. Acceptable; for a larger circle, the operator may want to extend the heartbeat interval (deployment-runbook discipline).
7. **Spec K does not detect if the user's *device* is compromised.** A user whose phone is taken still receives deltas; the credentials leak immediately. The mitigation is spec H reshuffle cadence + the design's "trusted circle" premise. Spec K accepts.
8. **`distribution_log` grows.** Each user × each channel × each delta = ~2-5 rows per day in steady state. SQLite handles years; future compaction not implemented.

---

## 15. §12 obligation summary (deployment-runbook view)

| Obligation | Healthy interval | What "proven" means |
|---|---|---|
| `dist_publish_sweep_ran` | ≤ 1h | Publisher sweep ran |
| `dist_user_heartbeat_proven::<user>` | ≤ `user_heartbeat_interval × 2` (default 48h) | At least one heartbeat actually delivered to this user via Telegram |
| `dist_user_heartbeat_breach::<user>` | absent | No 3-fail streak for that user |
| `dist_user_unregistered::<user>` | absent | Every assigned user has at least one of Telegram or email registered |

**Deployment runbook addition:**
- Register a `[distribution.telegram]` bot DIFFERENT from `[observability.telegram]`.
- For each new user: `user-channels-set <user_id> --telegram <chat_id> --email <addr>` before `shard-assign-box` so the first delta lands on a configured channel.
- Confirm receipt: `dist-test --user-id <user>` after registration; the user replies out-of-band that they got the test message.
- Monthly: rotate the distribution bot token; update `[distribution.telegram]`; run `dist-test` for one or two users to confirm.
