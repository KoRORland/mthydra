# Spec A — Controller State Model & Backup Pipeline

Status: **Draft, awaiting operator review.**
Predecessor: `doc/design.md`, `doc/build-plan.md` artifact `A`.
Successors blocked on this: `B` (signed descriptor), `C` (cover-domain pool), `D` (image pipeline), `F` (EU node setup), `G` (artifact generator), `H` (shard manager), `J` (observability), `K` (distribution channel).

---

## 1. Purpose

Define the EU controller's authoritative runtime-state model, the encrypted off-box backup pipeline, the operator-driven restore procedure, and the operator-side generation-gap monitor. This is the substrate every later artifact reads from and writes to.

Out of scope: the controller's transport-plane logic (descriptor signing, fleet operations, alerting) — those are separate specs that *consume* the schema defined here.

---

## 2. Locked design decisions

Approved during brainstorming session 2026-05-18.

| ID | Decision | Rationale |
|---|---|---|
| D1 | Controller's runtime state lives **plaintext** on disk. No at-rest encryption layer on the controller itself. | Design treats controller seizure as Case B in T2, which is re-key-everything. Adding at-rest encryption duplicates Case-B coverage at the cost of either operator-passphrase-on-boot (availability cliff) or runtime-readable key (no real protection). |
| D2 | Runtime state in a single **SQLite** file. Operator-authored policy/config in a separate **TOML** file deployed from this repo, *not* part of the backup pipeline. | SQLite gives transactional atomicity (critical for burned-set transitions), stdlib-only in Python, trivial backup/restore. Config is reproducible from git, contains no secrets. |
| D3 | EU compute on **AWS**. Backup destination must be **non-AWS S3-compatible**. Default: **Backblaze B2**. | T9: "independent of the EU node *and* its provider." Single account-level event must not destroy both. |
| D4 | Backups encrypted with **age** using an X25519 operator-held key. Operator's private key never touches the controller; only the public recipient is deployed. | Modern AEAD, simple CLI, short keys, pluggable for hardware-backed identities (`age-plugin-yubikey`, `age-plugin-tpm`) without pipeline code changes. |
| D5 | **Daily** floor timer + **on-change debounced 30s** trigger restricted to `burned_domains` inserts. Other "sensitive" rotations are operator-initiated and use a manual `backup-now` command. | RKN response window is slower than 24h. Burned-set loss is the single unrecoverable mistake (T5 §2); it gets the carve-out. Everything else can ride the daily cadence. |
| D6 | Generation-gap alarm threshold = **2× floor timer = 48h**. Operator-side monitor runs on an independent host (warm-standby or operator-controlled). | Detects silent-dead controller — the case the controller cannot self-alarm. |
| D7 | S3 lifecycle: 30 daily + 12 monthly. **Object Lock + Object Versioning** on the bucket. | Server-side retention keeps controller code dumb. Object Lock means a compromised controller's credentials cannot rewrite history — append-only past. |

---

## 3. Filesystem layout (controller)

```
/etc/mthydra/
  controller.toml          # operator-authored config (in git, deploy-time)
  age-recipient.txt        # operator's age PUBLIC key (recipient line)
  aws-region.txt           # static, deploy-time
  backup-destination.toml  # B2 endpoint, bucket name, access-key id only (secret in DB)

/var/lib/mthydra/
  state.sqlite             # the runtime state (D2)
  state.sqlite-wal         # SQLite write-ahead log
  state.sqlite-shm
  tmp/                     # ephemeral snapshot staging
  logs/
    controller.log
    backup.log
    audit.log              # mirror of audit_log table for grep convenience

/usr/local/bin/
  mthydra-controller       # CLI + daemon entry point
  mthydra-backup-monitor   # the operator-side gap monitor (runs on standby)
```

Owner `mthydra:mthydra`, mode `0700` on `/var/lib/mthydra`, `0600` on files containing secrets-equivalent material (the whole SQLite DB qualifies).

---

## 4. Runtime state schema (SQLite)

All timestamps stored as ISO-8601 UTC strings with `Z` suffix unless otherwise noted. Foreign keys enabled (`PRAGMA foreign_keys=ON`). WAL mode (`PRAGMA journal_mode=WAL`). Synchronous=NORMAL.

### 4.1 Meta

```sql
CREATE TABLE schema_version (
  version    INTEGER NOT NULL,
  applied_at TEXT    NOT NULL,
  CHECK (rowid = 1)
);
-- Single-row table. Migrations append history elsewhere if needed.
```

### 4.2 Cover-domain pool (consumed by spec C)

```sql
CREATE TABLE cover_domain_pool (
  domain          TEXT PRIMARY KEY,
  state           TEXT NOT NULL CHECK (state IN ('candidate_unverified','candidate_verified','in_use')),
  last_verified_at TEXT,
  verified_from_vantage TEXT,   -- which probe vantage signed off
  assigned_box_id TEXT,         -- NULL unless in_use; FK to ru_boxes
  added_at        TEXT NOT NULL,
  notes           TEXT,
  FOREIGN KEY (assigned_box_id) REFERENCES ru_boxes(box_id)
);

CREATE TABLE burned_domains (
  domain     TEXT PRIMARY KEY,        -- monotonic; NEVER deleted
  burned_at  TEXT NOT NULL,
  reason     TEXT NOT NULL,           -- job2_kill | job3_protocol_burn | manual_retire | other
  last_box_id TEXT,                   -- the box this domain was on when burned
  details    TEXT
);
```

Invariants enforced in code (not just schema):

- A domain may exist in `cover_domain_pool` XOR `burned_domains`, never both.
- Insert into `burned_domains` is always paired with delete from `cover_domain_pool` in a single transaction.
- No code path may delete from `burned_domains`. Period. (Validated by a startup self-check; see §10.)

### 4.3 Credential authority + descriptor signing (consumed by spec B)

```sql
CREATE TABLE credential_authority (
  generation  INTEGER PRIMARY KEY,    -- monotonically increasing; new row on re-key
  privkey_pem TEXT NOT NULL,          -- plaintext on disk per D1
  pubkey_pem  TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  retired_at  TEXT                    -- NULL = current
);

CREATE TABLE descriptor_signing_key (
  generation  INTEGER PRIMARY KEY,
  privkey     BLOB NOT NULL,          -- plaintext on disk per D1
  pubkey      BLOB NOT NULL,
  created_at  TEXT NOT NULL,
  retired_at  TEXT
);

CREATE TABLE descriptor_history (
  generation   INTEGER PRIMARY KEY,
  payload      TEXT NOT NULL,         -- the signed descriptor (signing key gen, EU exit set, validity window)
  signed_at    TEXT NOT NULL,
  valid_until  TEXT NOT NULL,
  signing_key_generation INTEGER NOT NULL,
  FOREIGN KEY (signing_key_generation) REFERENCES descriptor_signing_key(generation)
);
```

### 4.4 RU fleet (consumed by specs E, G, H, J)

```sql
CREATE TABLE ru_boxes (
  box_id            TEXT PRIMARY KEY,           -- UUID; assigned at provisioning
  provider          TEXT NOT NULL,
  region            TEXT NOT NULL,
  public_ip         TEXT,
  sni               TEXT UNIQUE NOT NULL,        -- the assigned cover domain
  shard_id          TEXT,                        -- NULL until assigned; FK to shards
  state             TEXT NOT NULL CHECK (state IN ('provisioning','live','terminated')),
  image_version     TEXT NOT NULL,               -- transport build hash from T4
  created_at        TEXT NOT NULL,
  went_live_at      TEXT,
  terminated_at     TEXT,
  termination_reason TEXT,
  FOREIGN KEY (shard_id) REFERENCES shards(shard_id)
);

CREATE TABLE onward_credentials (
  cred_id      TEXT PRIMARY KEY,
  box_id       TEXT NOT NULL,
  credential   BLOB NOT NULL,                    -- plaintext per D1; the revocable per-box secret
  issued_at    TEXT NOT NULL,
  revoked_at   TEXT,
  authority_generation INTEGER NOT NULL,
  FOREIGN KEY (box_id) REFERENCES ru_boxes(box_id),
  FOREIGN KEY (authority_generation) REFERENCES credential_authority(generation)
);
```

### 4.5 Users, shards, publishing (consumed by specs H, K)

```sql
CREATE TABLE users (
  user_id              TEXT PRIMARY KEY,         -- operator-chosen stable handle
  display_name         TEXT,
  out_of_band_channel  TEXT NOT NULL,            -- description; used in T1 trigger checks
  current_shard_id     TEXT,
  added_at             TEXT NOT NULL,
  FOREIGN KEY (current_shard_id) REFERENCES shards(shard_id)
);

CREATE TABLE shards (
  shard_id           TEXT PRIMARY KEY,
  members_json       TEXT NOT NULL,              -- JSON array of user_ids
  last_reshuffled_at TEXT NOT NULL,
  created_at         TEXT NOT NULL,
  retired_at         TEXT                        -- shards are immutable once retired
);

CREATE TABLE published_subsets (
  publish_gen   INTEGER PRIMARY KEY,
  payload_json  TEXT NOT NULL,                   -- which boxes/SNIs were published to whom
  published_at  TEXT NOT NULL,
  channel       TEXT NOT NULL                    -- telegram | email | both
);

CREATE TABLE publishing_tokens (
  kind        TEXT PRIMARY KEY,                  -- telegram_bot | smtp_app_password
  value       TEXT NOT NULL,                     -- plaintext per D1
  rotated_at  TEXT NOT NULL
);

CREATE TABLE provider_api_credentials (
  provider     TEXT PRIMARY KEY,                 -- aws | b2 | hetzner | ...
  credential   TEXT NOT NULL,                    -- plaintext per D1
  rotated_at   TEXT NOT NULL
);
```

### 4.6 §12 obligation clocks

```sql
CREATE TABLE obligation_clocks (
  obligation_id   TEXT PRIMARY KEY,              -- t1_dormant_health | t2_dryrun_caseA | t2_dryrun_caseB |
                                                  -- t3_vantage_revalidation | t3_profile_repin |
                                                  -- t4_upstream_check | t5_pool_revalidation |
                                                  -- t6_reshuffle | backup_restore_dryrun
  last_proven_at  TEXT NOT NULL,
  proven_by       TEXT NOT NULL,                 -- who ran the test
  details         TEXT,
  next_due_at     TEXT NOT NULL                  -- pre-computed for monitor convenience
);
```

Spec J reads this table for the dashboard / alert evaluation.

### 4.7 Operational logs

```sql
CREATE TABLE backup_log (
  generation       INTEGER PRIMARY KEY,
  created_at       TEXT NOT NULL,
  size_bytes       INTEGER NOT NULL,
  sha256           TEXT NOT NULL,
  pushed_at        TEXT,                          -- NULL until S3 ack
  index_updated_at TEXT,                          -- NULL until index.json updated
  trigger          TEXT NOT NULL                  -- floor_timer | burned_domains_change | manual
);

CREATE TABLE audit_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          TEXT NOT NULL,
  actor       TEXT NOT NULL,                      -- 'controller' | 'operator:<name>'
  action      TEXT NOT NULL,
  target      TEXT,
  details_json TEXT
);
```

`audit_log` is append-only by convention; mirrored to `/var/lib/mthydra/logs/audit.log` for grep.

---

## 5. Config schema (`/etc/mthydra/controller.toml`)

Non-secret operator-authored policy. Lives in git, not in backups.

```toml
[node]
role = "active"                    # "active" | "standby"
hostname = "controller-eu-1"

[backup]
floor_interval_hours = 24
on_change_debounce_seconds = 30
endpoint = "https://s3.us-west-002.backblazeb2.com"
bucket = "mthydra-controller-state"
# access-key-id is non-secret enough to live here; the secret access key
# lives in provider_api_credentials inside the DB
access_key_id = "..."

[backup.retention]
keep_daily = 30
keep_monthly = 12
object_lock_days = 365             # minimum retention period for Object Lock

[gap_monitor]
poll_interval_minutes = 30
alarm_threshold_hours = 48
recipient_email = "operator@example.org"

[obligations]
# how long is each obligation valid before it counts as stale
[obligations.timers_hours]
t1_dormant_health     = 168        # weekly
t2_dryrun_caseA       = 720        # monthly
t2_dryrun_caseB       = 720
t3_vantage_revalidation = 168
t3_profile_repin      = 0          # event-driven (T4 promotion); 0 means "must be tied to image_version"
t4_upstream_check     = 168
t5_pool_revalidation  = 168
t6_reshuffle          = 168
backup_restore_dryrun = 720
```

Loaded once at startup. Changes require controller restart (acceptable — config edits are rare and operator-driven).

---

## 6. Backup pipeline

### 6.1 Triggers

- **Floor timer.** APScheduler `IntervalTrigger(hours=floor_interval_hours)` running in the controller process. Fires `do_backup(trigger='floor_timer')`.
- **On-change.** A single application-layer chokepoint function `mark_burned(domain, reason, last_box_id, details)` performs the transactional move from `cover_domain_pool` → `burned_domains` and posts to an asyncio `Queue`. A debouncer task drains the queue, coalescing all events within `on_change_debounce_seconds` into one `do_backup(trigger='burned_domains_change')` call.
- **Manual.** `mthydra-controller backup-now [--reason TEXT]` → `do_backup(trigger='manual')`.

Only one backup runs at a time. A backup in progress when another trigger fires causes the second trigger to be queued (not dropped) and to fire immediately after the first completes; consecutive duplicate triggers collapse into one.

### 6.2 `do_backup()` procedure

```
1. Acquire backup mutex (asyncio.Lock).
2. gen = SELECT COALESCE(MAX(generation), 0) + 1 FROM backup_log
3. Insert (gen, created_at=now, trigger, sha256='', size_bytes=0, ...) into backup_log.
4. snap_path = /var/lib/mthydra/tmp/snap-<gen>.db
   Run `sqlite3 state.sqlite ".backup '<snap_path>'"`     # atomic, online-safe
5. enc_path = /var/lib/mthydra/tmp/snap-<gen>.age
   Spawn `age -r <recipient> -o <enc_path> <snap_path>`
   Wait, verify exit 0.
6. sha = sha256(enc_path), size = stat(enc_path).st_size
7. UPDATE backup_log SET sha256=?, size_bytes=? WHERE generation=?
8. S3 PUT gen-<NNNNNNNNNN>.age (zero-padded to 10 digits for sort).
   Headers: x-amz-object-lock-mode=COMPLIANCE, x-amz-object-lock-retain-until-date=now+object_lock_days.
   Verify ETag matches local md5 of ciphertext (S3-style integrity check).
9. UPDATE backup_log SET pushed_at=now WHERE generation=?
10. S3 PUT index.json with {highest_gen: <gen>, sha256: <sha>, ts: <now>, size_bytes: <size>}.
    Versioning makes this a new object version, not overwrite.
11. UPDATE backup_log SET index_updated_at=now WHERE generation=?
12. Delete snap_path, enc_path.
13. Release mutex.
```

Error handling:

- Any step 4–11 failure leaves `backup_log` row with a NULL marker indicating where it failed. Retried on next floor tick (no auto-retry within `do_backup` to avoid masking persistent failure).
- N=3 consecutive `do_backup` failures fires an out-of-band self-alarm via spec-J's email path. (Spec J ships with a minimal SMTP-only path independent of the main alerting pipeline so the controller can scream even if Telegram bot publishing is down.)

### 6.3 What ends up in S3

```
bucket: mthydra-controller-state
  gen-0000000001.age      (immutable, Object-Locked for object_lock_days)
  gen-0000000002.age
  ...
  index.json              (mutable; versioning keeps history)
```

Lifecycle policy (applied at bucket-creation time, not by the controller):

- Daily generations older than 30 days transition to lower-cost storage class.
- Generations not in the "30 daily + 12 monthly" window expire after their Object-Lock retention elapses.
- `index.json` non-current versions retained for 90 days.

---

## 7. Restore procedure

Two-step by design — decrypt + inspect is decoupled from adopt.

### 7.1 `mthydra-controller restore`

```
mthydra-controller restore \
  --from gen-NNNNNNNNNN.age \
  --identity ~/.age/operator.key \
  --into /tmp/restored.sqlite \
  [--summary-only]
```

Behavior:

1. Pull the blob from S3 to local tmp (or read from a path if the operator already downloaded it).
2. Verify ETag if available.
3. `age -d -i <identity> blob.age > /tmp/restored.sqlite`
4. Run `PRAGMA integrity_check` on restored DB; abort on any non-`ok` result.
5. Read `schema_version`; abort if mismatched against the binary's expected version (no auto-migration during restore — operator must run a known matching binary).
6. Print summary:
   - `schema_version`
   - latest `generation` from `backup_log`
   - burned-set size
   - cover-pool sizes by state
   - live RU boxes count
   - latest descriptor generation + validity window
   - oldest stale obligation clock
7. If `--summary-only`, exit. Otherwise leave the file at `--into` for the next command.

The restore command **never** touches the live controller state. It can run on the operator's laptop, on the warm-standby before promotion, or anywhere else — no controller-side privileges required.

### 7.2 `mthydra-controller adopt-restored-state`

```
mthydra-controller adopt-restored-state /tmp/restored.sqlite \
  [--case A|B] \
  [--rotate-published-subset]
```

Run **on the controller host** (active or freshly-promoted standby). This is the only command that touches the live DB.

1. Refuse if the controller daemon is running; require `systemctl stop mthydra-controller` first.
2. Move existing `state.sqlite` → `state.sqlite.preadopt.<ts>` (kept for forensics, never auto-deleted).
3. Move `/tmp/restored.sqlite` → `state.sqlite`. Fix ownership/mode.
4. If `--case B`: re-key the credential authority (insert a new `credential_authority` row, mark prior as retired). The fleet will roll via replace-on-burn; this command does not touch any RU box.
5. If `--rotate-published-subset`: append a new `published_subsets` row marked `_pending_rotation` so the next publish cycle issues a fresh subset (per T2 step 6).
6. Update `obligation_clocks` row `backup_restore_dryrun` to now if `--reason` indicates this was a dry-run.
7. Print "ready to start" message. Operator runs `systemctl start mthydra-controller`.

This matches T2's required flow: classify (Gate 0), retrieve, sanity-check, push to standby, apply Case branch.

---

## 8. Operator-side gap monitor

A separate binary `mthydra-backup-monitor` that runs on a host *other than* the active controller. Default deployment: on the warm-standby (which already exists per spec F and runs exit-only otherwise).

```
[Unit]
Description=mthydra backup generation-gap monitor
After=network.target

[Service]
ExecStart=/usr/local/bin/mthydra-backup-monitor --config /etc/mthydra/controller.toml
Restart=always
User=mthydra
```

Behavior:

1. Every `poll_interval_minutes` (default 30), HEAD `index.json` in the configured bucket.
2. Track `highest_gen` and the timestamp it was first observed at this value.
3. If `now - first_observed_at > alarm_threshold_hours` (default 48), send a single email to `recipient_email` via the same SMTP-app-password path the controller uses for self-alarms. Subject: `mthydra: backup gap (highest_gen=N stuck since TS)`.
4. Re-fires once per 24h while the gap persists, not on every poll (suppress flood).
5. Clears state when `highest_gen` advances.

The monitor has **no controller-side privileges**. It only reads B2. Its credential is a separate B2 application key with read-only scope on `index.json`.

This is the dead-man's-switch substrate from §3 of the master design. Loss of advancing `highest_gen` = silently-dead controller = operator invokes T2.

---

## 9. Failure-mode catalogue

| Failure | Detection | Response |
|---|---|---|
| B2 unreachable, transient | `do_backup` step 8 exception | Mark row partial; retry on next trigger; N=3 consecutive → self-alarm. |
| B2 credentials revoked | 403 from step 8 | Self-alarm immediately. Operator rotates `provider_api_credentials` and runs `backup-now`. |
| age binary missing / decrypt fails | Spawn exit non-zero | Controller refuses to start (loud failure, not silent skip). Restore command also refuses. |
| Operator pubkey malformed | Startup check parses recipient | Refuse to start. |
| SQLite corruption | `PRAGMA integrity_check` on startup | Refuse to start. Operator runs `restore` from latest known-good generation. |
| Disk full preventing snapshot | step 4 fails | Skip this trigger; emit a self-alarm. Operator clears space. |
| index.json upload succeeds but blob upload failed | `backup_log.pushed_at IS NULL AND index_updated_at IS NOT NULL` — impossible by construction (order in §6.2) | Schema invariant; startup self-check verifies. |
| Controller crashes between §6.2 step 9 and step 11 | Row has `pushed_at IS NOT NULL AND index_updated_at IS NULL` | Recoverable. On startup, for any such row: HEAD `index.json` from S3 and HEAD `gen-<N>.age`; if both present and index references this generation, set `index_updated_at = now`; if index references an older generation, re-PUT index.json and then set `index_updated_at`. Idempotent. |
| Two simultaneous backups (race) | asyncio.Lock | Serialized; second waits and runs immediately after. |
| Burned-domain insert without paired pool delete | Application-layer chokepoint enforces transaction | Startup self-check: `SELECT 1 FROM cover_domain_pool WHERE domain IN (SELECT domain FROM burned_domains)` MUST return zero rows. Refuse to start on violation. |
| Controller crashes mid-backup | partial row in backup_log | On restart, scan for rows with `pushed_at IS NULL AND created_at > now - 1h`; mark them abandoned; retry as fresh generation. |
| S3 Object-Lock prevents overwrite of a corrupt blob | by design | Reupload as next generation; never reuse a generation number. |
| Warm-standby gap-monitor itself dies silently | Out of scope for this spec; covered by spec J (controller heartbeat to operator via independent path). | The monitor is the dead-man's-switch for the controller; the heartbeat is the dead-man's-switch for the monitor. |

---

## 10. Startup self-check (mandatory)

Run unconditionally before the controller serves any request. Refuse to start on any failure.

1. Config file parses; required fields present.
2. `age-recipient.txt` parses as a valid age recipient.
3. `age` binary present and executable.
4. SQLite `PRAGMA integrity_check` returns `ok`.
5. `schema_version.version` matches binary's expected version.
6. `cover_domain_pool ∩ burned_domains = ∅` (the §4.2 invariant).
7. No `burned_domains` row is older than the oldest `audit_log` entry referencing its insertion (sanity).
8. Exactly one `credential_authority` row has `retired_at IS NULL`.
9. Exactly one `descriptor_signing_key` row has `retired_at IS NULL`.
10. B2 endpoint reachable and credentials valid (HEAD on bucket).
11. Any `backup_log` row with `pushed_at IS NOT NULL AND index_updated_at IS NULL` is reconciled per the §9 crash-recovery rule (HEAD S3, re-PUT index.json if needed). This is a recoverable state, not a failure.
12. No `backup_log` row has `pushed_at IS NULL AND index_updated_at IS NOT NULL` (the truly impossible state — would mean we published an index pointing at a blob we never uploaded). Refuse to start on violation.

Failures log a clear error citing the check number and exit with a distinct exit code per check. No silent degradation.

### 10.1 Bootstrap (first-run)

A fresh controller host has no `state.sqlite`. The operator runs:

```
mthydra-controller init \
  --age-recipient /etc/mthydra/age-recipient.txt \
  --provider-aws-key <id>:<secret> \
  --provider-b2-key <id>:<secret>
```

`init` will:

1. Create `/var/lib/mthydra/state.sqlite` with the current schema.
2. Insert a single `schema_version` row.
3. Generate a fresh `credential_authority` row (generation=1).
4. Generate a fresh `descriptor_signing_key` row (generation=1).
5. Insert seed rows into `provider_api_credentials` (aws, b2).
6. Initialize all `obligation_clocks` rows with `last_proven_at = now`, `proven_by = 'bootstrap'`. (These will go stale quickly, which is correct — bootstrap is not proof.)
7. Run a synthetic `do_backup(trigger='bootstrap')` to verify the pipeline end-to-end before declaring the controller ready.

`init` refuses to run if `state.sqlite` already exists. To re-bootstrap, the operator must explicitly move or delete the existing file (with full understanding of what they are discarding).

---

## 11. §12 obligation contribution

This artifact owns the `backup_restore_dryrun` obligation clock.

- **What it tracks:** time since an end-to-end backup → S3 → operator-laptop decrypt → restore → adopt-restored-state → schema sanity-check was last performed on a throwaway controller host.
- **How it is refreshed:** the dry-run procedure (see §12) ends with `mthydra-controller adopt-restored-state ... --reason dryrun` which writes to `obligation_clocks`.
- **Surfacing:** spec J's dashboard reads `obligation_clocks` and surfaces "time since each obligation was last proven."

This single clock is the answer to "is the backup pipeline real or aspirational." If it goes stale, the pipeline is aspirational.

---

## 12. Backup-restore dry-run procedure (operator runbook)

To be run on the schedule defined by `obligations.timers_hours.backup_restore_dryrun` (default 720h = monthly).

1. On a throwaway VM (any provider, any region) install the `mthydra-controller` binary at the same version as production.
2. `mthydra-controller restore --from <latest-gen> --identity <operator-key> --into /tmp/r.sqlite --summary-only` — verify summary matches production's expected shape.
3. `mthydra-controller restore --from <latest-gen> --identity <operator-key> --into /tmp/r.sqlite`
4. `mthydra-controller adopt-restored-state /tmp/r.sqlite --reason dryrun`
5. `systemctl start mthydra-controller`
6. Observe one successful `do_backup` cycle to a *test* B2 bucket (config override).
7. Tear down the throwaway VM.
8. The `adopt-restored-state ... --reason dryrun` in step 4 updated `obligation_clocks.backup_restore_dryrun` *on the throwaway VM's restored state.* This is NOT what we want — that state was discarded. Instead, the operator manually runs on the *production* controller:
   `mthydra-controller obligation-proven backup_restore_dryrun --details "dry-run completed gen-N → VM <id> at <ts>"`
9. Spec J's "time since last proven" surface now shows 0 for this obligation.

The step-8 manual touch on the production controller is deliberate — the dry-run proves the *path*; the production clock is the operator's signature that they actually did it.

---

## 13. Test plan

### 13.1 Unit (pytest)

- `test_schema_invariants.py` — every check in §10 has a paired test that constructs a violating DB and asserts startup-check rejection.
- `test_backup_pipeline.py` — each step of `do_backup` mocked individually; verify error paths set `backup_log` markers correctly.
- `test_mark_burned.py` — verifies the chokepoint move is transactional; injected failure between delete and insert rolls back both.
- `test_age_roundtrip.py` — encrypt with test recipient, decrypt with test identity, compare bytes.
- `test_restore_summary.py` — golden-file comparison of `restore --summary-only` output for a fixed DB.

### 13.2 Integration (pytest + moto / minio)

- `test_end_to_end_backup.py` — spin up `minio` in a container, configure controller against it, write rows, observe blob + index.json appear, decrypt with test key, verify content.
- `test_gap_monitor.py` — monitor against a minio bucket whose `index.json` is frozen; assert email is dispatched after `alarm_threshold_hours` of simulated time.
- `test_object_lock.py` — verify the controller cannot delete its own past generations even with bucket access.

### 13.3 Property (Hypothesis)

- For any sequence of `mark_burned` operations interleaved with reads, the union `cover_domain_pool.domain ∪ burned_domains.domain` is invariant and `burned_domains.domain` is monotonically growing.

### 13.4 Smoke (manual, operator-run, not CI)

- `make smoke-real-b2` — pushes one generation to a real test B2 bucket with the operator's real key and decrypts on the operator's laptop. Run before every controller release.

### 13.5 Coverage target

≥ 90% line coverage on `mthydra.controller.backup`, `mthydra.controller.state`, `mthydra.controller.restore`. Lower targets elsewhere acceptable.

---

## 14. Open items deliberately left to consuming specs

- Spec B will define the **exact descriptor payload format** that goes into `descriptor_history.payload`. This spec only reserves the column.
- Spec C will define the **state-machine transition logic** for `cover_domain_pool`. This spec only defines the table.
- Spec F will define **how the controller daemon is supervised** (systemd unit, restart policy, environment).
- Spec G will define the **artifact format** consumed by RU node init; this spec only stores the credential authority and descriptor signing key that the artifact references.
- Spec H will define the **shard reshuffle algorithm**; this spec only stores shard state.
- Spec J will define the **alerting service** that consumes `obligation_clocks` and `backup_log`.

---

## 15. Honest residuals specific to spec A

- **Single backup destination accepted (Q3-revised → D3).** A B2 account termination remains an unrecoverable loss of backup history. Mitigated only by §12 obligation clock: the dry-run will catch destination death within `backup_restore_dryrun` interval. Not closed.
- **Plaintext on disk (D1).** A controller seized between backups discloses everything in the SQLite file to the adversary. This is the Case-B scenario in T2 and is the *defined* response (re-key + fleet roll). Not closed at this layer by design.
- **`age` external binary dependency.** If a future Ubuntu base image drops the package, the controller refuses to start (per §10 step 3). Acceptable loud failure; not a silent one.
- **Operator-key compromise (#10 in build-plan).** Out of scope of this spec by construction — the spec defines what is encrypted *to* the key, never how it is held. If the operator key leaks, every historical backup is recoverable by the holder. The design accepts this as the operator's responsibility, not the controller's.
- **Object-Lock retention floor (`object_lock_days = 365`).** Means even the operator cannot prune historical backups for a year, even on legitimate request. Accepted trade — append-only past is the point.
- **Gap monitor on warm-standby (§8).** If standby and active controller share a failure mode (same operator account at AWS, same region), both can be lost together and the monitor is silent. Operator should deploy monitor on a *non-AWS* host for full independence; the spec recommends but does not enforce this — config-driven choice.
