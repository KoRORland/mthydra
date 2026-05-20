# Spec F — EU Node Setup (active + warm-standby roles)

Status: **Draft, awaiting operator review.**
Predecessor: `doc/design.md` §1 (Topology) + §7 (T2 Controller Restore / Promotion Runbook), `doc/specs/2026-05-18-A-controller-state-and-backup.md`, `doc/specs/2026-05-19-B-signed-endpoint-descriptor.md`, `doc/specs/2026-05-19-C-cover-domain-pool-manager.md`.
Successors blocked on this: `F2` (data-exit / Reality-tunnel wrapper), `H` (shard manager — consumes `eu_nodes`), `J` (observability — consumes the readiness obligations), `K` (distribution channel — referenced by Case-B promotion checklist).

---

## 1. Purpose

Make the active/warm-standby split that `design.md` §1 calls for *real*. Today the controller treats `node.role` as a label with no behavioural distinction; this spec gates the serve loop on role, defines how a standby holds (no) state, gives the operator a single audited `promote-active` CLI that performs the T2 §7 procedure, and adds the readiness mechanisms (heartbeat + drill attestation) that turn "warm standby" from aspiration into a tracked obligation.

Out of scope: the data-exit (Reality/sing-box tunnel terminator that actually receives RU→EU traffic). That is deferred to spec F2. Spec F establishes the *control-plane* of the EU node (role, promotion, inventory, readiness); F2 will add the *data-plane*. The split mirrors how spec A separated the controller wheel from the backup-monitor wheel.

---

## 2. Locked design decisions

Approved during brainstorming session 2026-05-20.

| ID | Decision | Rationale |
|---|---|---|
| F-D1 | **Scope split**: this spec covers role gating + promotion + EU inventory; the data-exit lands in spec F2. | Tunnel-software selection (sing-box vs xray) and systemd-level network plumbing are distinct from Python/SQLite controller work; splitting follows the spec A / backup-monitor precedent and keeps F testable in isolation. |
| F-D2 | **Standby holds a skeleton DB** — schema only, no live secrets (one carve-out: B2 credential for the heartbeat publisher). | Avoids the operator-key-on-standby regression. Matches T2 §7's procedure of pushing state to the standby *at promotion time*, not before. Same systemd unit on both roles. |
| F-D3 | **Runtime role lives in `node_state` (DB-singleton); `controller.toml.node.role` is a deploy-time hint.** | DB is the source of truth for runtime state (spec A D2). Surviving a restart after promotion must not depend on the operator remembering to edit a config file. |
| F-D4 | **Readiness = liveness heartbeat (automatic) + drill attestation (operator-attested CLI)**. Two obligations: `eu_standby_liveness_seen::<node_id>` and `eu_standby_drill_proven::<node_id>`. | Heartbeat surfaces "is the standby alive and reachable" cheaply and continuously; the drill obligation enforces T2 §7 standing-obligation ("execute end-to-end on throwaway infra"). One signal is not enough — a live but never-drilled standby is the failure mode the design explicitly warns about. |
| F-D5 | **Heartbeat transport = B2 bucket** under `standby/<node_id>/heartbeat.json` (same bucket as backups; distinct prefix). | Both nodes already have B2 credentials. Avoids new EU↔EU coupling. The backup-monitor wheel could later subscribe to the same prefix; for MVP the active polls it directly. |
| F-D6 | **`promote-active` does bare promotion** (stop unit → atomic DB swap → write `node_state` → start unit). Case B prints a rotation **checklist** of separately-driven commands; it does **not** auto-orchestrate the Case-B rotations. | T2 §7 Gate 0 is a judgement call; mis-classifying B and silently auto-rotating everything is the failure path. Each rotation is its own audit-traceable CLI call (existing spec A/B/C commands). |

---

## 3. Schema additions (v3 → v4)

### 3.1 `node_state` — singleton runtime role

```sql
CREATE TABLE node_state (
  role                        TEXT NOT NULL CHECK (role IN ('active','standby')),
  promoted_at                 TEXT,
  previous_role               TEXT,
  promotion_case              TEXT CHECK (promotion_case IN ('A','B')),
  promotion_backup_generation INTEGER,
  CHECK (rowid = 1)
);
```

Single-row table (the `rowid = 1` CHECK is structural). The row is **seeded once**: during `init` it is seeded from the `--role` flag (defaulting to `active` when absent); during a v3 → v4 migration of an existing deployment it is seeded with `role = 'active'` (every pre-spec-F deployment is implicitly active, since standby behaviour did not exist before). Subsequent updates come exclusively from `promote-active`.

### 3.2 `eu_nodes` — EU infrastructure inventory

```sql
CREATE TABLE eu_nodes (
  node_id                TEXT PRIMARY KEY,
  hostname               TEXT NOT NULL,
  provider               TEXT NOT NULL,
  region                 TEXT NOT NULL,
  public_ip              TEXT,
  role                   TEXT NOT NULL CHECK (role IN ('active','standby','retired')),
  added_at               TEXT NOT NULL,
  promoted_at            TEXT,
  retired_at             TEXT,
  last_heartbeat_at      TEXT,
  last_heartbeat_b2_etag TEXT,
  notes                  TEXT
);
```

`eu_nodes` is the infrastructure side: which EU hosts exist, their roles, what we last heard from them. **It is distinct from `eu_exit_set`** (spec B), which holds *protocol-visible* exit endpoints the descriptor advertises to RU boxes. F2 (data-exit) is responsible for the eventual mapping between the two; spec F only owns infrastructure inventory.

### 3.3 Schema version bump

`meta.schema_version` advances `3 → 4`. Migration `migrate_v3_to_v4(conn)` creates both tables and seeds `node_state` from `controller.toml.node.role` if absent.

---

## 4. Invariants (extending spec C's #17–#20)

- **#21 — `node_state` singleton.** `SELECT COUNT(*) FROM node_state` is exactly 1; `rowid = 1` always.
- **#22 — Active requires authority.** If `node_state.role = 'active'`, the DB has ≥ 1 non-retired `credential_authority` row and ≥ 1 non-retired `descriptor_signing_key` row.
- **#23 — Standby is skeleton (with B2 carve-out).** If `node_state.role = 'standby'`, the DB has **zero** rows in `credential_authority`, `descriptor_signing_key`, `descriptor_history`, `publishing_tokens`, `cover_domain_pool`, `burned_domains`, `eu_exit_set`. The DB **may** hold rows in `provider_api_credentials` **but only with `provider = 'b2'`** (heartbeat publisher requires it).

Each check raises `InvariantViolation` with the violating count/row. Standby's invariant is the harshest — it codifies the design intent that a standby is structurally not a live controller.

---

## 5. Role-gated serve loop

`_cmd_serve` reads `node_state.role` (not `cfg.node.role`) at startup and branches.

### 5.1 Active path (unchanged behaviour + one addition)

- `BackupOrchestrator` armed (spec A)
- `DescriptorRotator` armed (spec B)
- `CoverPoolReverifySweep` + `CoverPoolRotationSweep` armed (spec C)
- **New:** `StandbyHeartbeatPoller` armed

### 5.2 Standby path (new)

- Startup-check runs and verifies invariants #21 and #23
- All four spec-A/B/C schedulers are **not constructed** on the standby branch — the standby code path simply does not instantiate them. (Most would fail at first tick anyway: `BackupOrchestrator` needs an authority-bearing DB to back up; `DescriptorRotator` needs a signing key. Skipping construction is the clean expression of "standby does nothing mutating.")
- **New:** `StandbyHeartbeatPublisher` armed
- Wait on signal as today

Both paths share the same systemd unit, same wheel, same entrypoint. The role gate is purely runtime behaviour.

### 5.3 New module: `mthydra.controller.standby.heartbeat`

```python
class StandbyHeartbeatPublisher:
    """Writes a small JSON ping to B2 on a timer.

    Runs on standby only. Reads B2 credentials from provider_api_credentials.
    """
    def __init__(self, node_id: str, b2_destination, interval_seconds: float,
                 mode: str = "production", clock: Callable[[], str] | None = None) -> None: ...
    def arm(self) -> None: ...
    def disarm(self) -> None: ...
    def run_once(self) -> None: ...  # publishes one heartbeat object


class StandbyHeartbeatPoller:
    """Polls every active eu_nodes.role='standby' row's heartbeat object in B2.

    Runs on active only. Updates eu_nodes.last_heartbeat_{at,b2_etag} on freshness,
    proves eu_standby_liveness_seen::<node_id> on success, sets
    eu_standby_liveness_stale::<node_id> when staleness_alert_seconds elapsed.
    """
    def __init__(self, db_path, b2_destination, poll_interval_seconds: float,
                 staleness_alert_seconds: float, mode: str = "production",
                 clock: Callable[[], str] | None = None) -> None: ...
    def arm(self) -> None: ...
    def disarm(self) -> None: ...
    def run_once(self) -> list[str]: ...  # returns stale node_ids
```

Both follow the spec C `CoverPoolReverifySweep` pattern (APScheduler, `mode='offline'` no-ops `arm()`, `run_once()` is the test seam).

### 5.4 Heartbeat object format

```json
{
  "schema": "mthydra.standby_heartbeat.v1",
  "node_id": "eu-standby-de-1",
  "ts": "2026-05-20T12:34:56Z",
  "schema_version": 4,
  "controller_version": "0.0.1"
}
```

Written to `standby/<node_id>/heartbeat.json` in the B2 bucket configured under `[backup].bucket`. Each write overwrites the same key (B2 Object Lock retains prior versions; that is the accepted residual — see §11).

---

## 6. Config additions

New `[standby]` section in `controller.toml`:

```toml
[standby]
node_id                      = "eu-standby-de-1"   # required when role=standby
heartbeat_interval_seconds   = 60
heartbeat_poll_interval      = "5m"
staleness_alert_seconds      = 600                  # 10m
```

Loaded into `StandbyConfig` dataclass on `Config`. `node_id` is the only field that must be set on a standby (validated at startup when `node_state.role='standby'`); it is informational on active.

---

## 7. CLI surface

All commands run on **active** unless noted. They refuse with exit 2 on a standby node where indicated.

```
mthydra-controller eu-node-add <node_id> \
    --hostname <fqdn> --provider <name> --region <name> \
    [--public-ip <ip>] [--role active|standby] [--notes <text>] [--db-path ...]
    # Default --role=standby. Refuses to insert a second role='active' row.
    # Active-only.

mthydra-controller eu-node-retire <node_id> [--db-path ...]
    # role -> 'retired'; sets retired_at. Refuses if node_id is the local active.
    # Active-only.

mthydra-controller eu-node-list [--state active|standby|retired] [--json]
    # Prints rows plus last_heartbeat_at age and a per-row freshness flag.
    # Active-only.

mthydra-controller standby-drill-proven \
    --node-id <id> --case A|B \
    [--notes <text>] [--db-path ...]
    # Operator attests: T2 §7 end-to-end drill against <id>, Case <X>, passed
    # on throwaway infrastructure. Proves:
    #   eu_standby_drill_proven::<node_id>
    #   t2_dryrun_caseA  (if --case A)
    #   t2_dryrun_caseB  (if --case B)
    # Emits audit row action='eu_standby_drill_proven', target=node_id.
    # Active-only.

mthydra-controller promote-active \
    --backup-blob <path> --age-identity <path> --case A|B \
    [--db-path ...] [--config ...] [--yes]
    # Runs on STANDBY. See §8 for the procedure.

mthydra-controller authority-rotate [--db-path ...] [--config ...]
    # Inserts a new generation in credential_authority; marks the prior one
    # retired_at=now. Fleet re-credentialing happens via replace-on-burn (T2
    # Case-B fleet consequence). Emits audit row action='authority_rotated'.
    # Active-only. Referenced by promote-active --case B's checklist; can
    # also be run independently for routine authority rotation drills.
```

No `demote-active` command — the design forbids demoting a running active (split-brain risk). To rotate authority back, retire the active node entirely.

---

## 8. `promote-active` — the load-bearing procedure

```
Preconditions (each failure → exit 2):
  - node_state.role == 'standby'
  - skeleton-invariant #23 passes on the current DB
  - --backup-blob exists and decrypts with --age-identity
  - decrypted DB's schema_version ≤ local SCHEMA_VERSION (apply_schema migrates forward)
  - --yes given OR TTY-interactive confirmation typed exactly "PROMOTE"
  - the local systemd unit is stopped (refuses with diagnostic + the systemctl
    command otherwise; promote-active does not invoke systemctl itself)

Procedure:
  1. Decrypt --backup-blob to /var/lib/mthydra/tmp/promote-<gen>.sqlite (chmod 0600).
  2. apply_schema() on the temp DB (forward-migrate if needed).
  3. Read the backup generation number from the decrypted DB.
  4. Atomically:
       state.sqlite -> state.sqlite.preskel-<ts>.bak
       /var/lib/mthydra/tmp/promote-<gen>.sqlite -> state.sqlite
  5. chmod 0600 state.sqlite; chmod 0700 /var/lib/mthydra (spec A §3).
  6. Open the new DB. Write the node_state UPDATE:
       SET promoted_at=now, previous_role='standby', promotion_case=?,
           promotion_backup_generation=?
       (role is already 'active' from the restored backup's own node_state.)
  7. UPDATE eu_nodes locally: role='active', promoted_at=now (matching --config
     [standby].node_id).
  8. log_event audit row: action='eu_node_promoted', target=node_id,
     details_json={"case": X, "backup_generation": N, "previous_role": "standby"}.
  9. Run startup-check on the new DB. If it raises:
       - rename state.sqlite -> state.sqlite.failed-<ts>
       - rename state.sqlite.preskel-<ts>.bak -> state.sqlite
       - exit 10 with the InvariantViolation diagnostic
 10. Print success banner + (Case-B-only) the rotation checklist.

  The operator runs `systemctl start mthydra-controller` after step 10.
```

### 8.1 Success banner — Case A

```
promote-active: node <node_id> is now ACTIVE (case A, generation <N>)
Previous DB backed up to /var/lib/mthydra/state.sqlite.preskel-<ts>.bak

Next steps:
  - Start the service:        sudo systemctl start mthydra-controller
  - Verify startup-check OK:  mthydra-controller startup-check
  - Confirm a Russia-vantage probe shows the descriptor serving correctly
  - Record the drill against the previous active (now retired):
      mthydra-controller standby-drill-proven --node-id <other-id> --case A
```

### 8.2 Success banner — Case B

Same banner as A, then:

```
CASE B — SUSPECTED COMPROMISE. Run the following in order on the now-active node:

  1. Re-key credential authority:
       mthydra-controller authority-rotate

  2. Rotate descriptor signing key (RU boxes learn the new key via descriptor):
       mthydra-controller signing-key-rotate

  3. Rotate the B2 provider credential (revoke old in B2 console, mint new, push):
       mthydra-controller rotate-provider-credential --provider b2 --credential <NEW>

  4. Mint fresh publishing tokens — spec K, when shipped. Until then, manually mark
     all currently-published links as dead on the distribution channel.

  5. Trigger immediate descriptor sign (new authority + key set takes effect):
       mthydra-controller descriptor-sign-now

  6. Rotate the published subset forward — spec K. For MVP, accelerate rotation of
     currently-in-use cover domains via `cover-rotate`.

  7. Verify recovery:
       - probe from a Russia-vantage shows green
       - backup-monitor dead-man's-switch clears
       - at least one end-to-end user path confirmed

  8. Record the drill:
       mthydra-controller standby-drill-proven --node-id <previous-standby-id> \
           --case B --notes "promoted <date>, reason <text>"
```

Spec F does **not** execute any step 1–8. It prints them and exits.

### 8.3 `authority-rotate` (mentioned in the Case-B checklist)

This subcommand does not yet exist in spec A. **Spec F adds it** as a small companion since it is referenced by `promote-active --case B`'s checklist. Behaviour: insert a new generation in `credential_authority`, mark the prior one `retired_at = now`. Fleet re-credentialing happens via replace-on-burn (design §7 / T2 Case-B fleet consequence). Emits one audit row.

---

## 9. Bootstrap + obligations

### 9.1 New init mode for skeleton standby

```
mthydra-controller init --role standby --db-path ... --config ...
```

When `--role standby` is set:
- Creates the schema (same `apply_schema` path)
- Seeds `node_state.role = 'standby'`
- Inserts the B2 provider credential (from `--provider-credential`) — **the only secret**
- Does **NOT** seed `credential_authority`, `descriptor_signing_key`, obligations, or any other table
- Verifies invariant #23 passes before exiting

Existing `init` (no `--role` flag) continues to seed full active state and defaults `node_state.role = 'active'`.

### 9.2 New obligations seeded by bootstrap

Added to the `obligation_timer_hours` dict in `_cmd_init`:

```python
"eu_standby_drill_proven":  30 * 24,    # 30 days (T2 §7 cadence)
```

Per-node obligations (`eu_standby_liveness_seen::<id>`, `eu_standby_liveness_stale::<id>`) are created **lazily** when a standby row is added via `eu-node-add` or first observed by the heartbeat poller.

`t2_dryrun_caseA` / `t2_dryrun_caseB` already exist (spec A). Spec F repurposes them: they are proven by `standby-drill-proven`, not by a future stand-alone "dryrun" CLI.

---

## 10. Cross-spec contracts

Stable seams that downstream specs MUST respect:

- **Spec F2 (data-exit).** F2 owns the Reality/sing-box wrapper. F2 reads `node_state.role` to gate exit-only vs exit+control behaviour on its side. F2 writes `eu_nodes.public_ip` and is the only writer of `eu_exit_set` (spec B) for *real, externally-reachable* endpoints. Spec F does NOT touch `eu_exit_set`.
- **Spec H (shard manager).** H filters shard assignments to non-retired `eu_nodes` and respects the active/standby boundary (boxes are assigned to shards targeting EU exits; the active controller signs which exits are in the descriptor).
- **Spec J (observability).** J consumes `eu_standby_liveness_seen::*`, `eu_standby_liveness_stale::*`, `eu_standby_drill_proven::*` obligations + the `node_state` row. The dead-man's-switch + readiness panels read from these.
- **Spec K (distribution channel).** K's Case-B procedure (mint fresh publishing tokens + accelerated subset rotation) is referenced by `promote-active --case B`'s checklist as steps 4 and 6. F prints; K's CLIs will perform.
- **Spec A backup-monitor.** No change. Monitor watches backup generation in B2; it is unaware of the standby heartbeat prefix. (J may later subscribe; out of scope for F.)
- **Spec B descriptor.** Active-only. After promotion, the new active's first scheduled `DescriptorRotator` tick will sign a fresh descriptor with the restored signing key; this is automatic. Case-B operators are instructed to force an immediate sign via `descriptor-sign-now` rather than waiting.

---

## 11. Honest residuals (Spec F)

- **Heartbeat conflates "standby dead" with "B2 unreachable."** A B2 outage marks every standby stale until B2 recovers. T2 §7 considers this acceptable (the dead-man's-switch is the same signal either way), but the deployment runbook should call out the false-positive mode.
- **`promote-active` requires the operator to stop the systemd unit first.** No daemon-self-promotion, no automatic stop. If the standby's serve loop is running, the SQLite write lock blocks the atomic rename. The precondition check surfaces this with the exact `systemctl stop` command. The trade-off: explicit operator action vs an automated stop that could mis-fire if the standby is intentionally serving (F2 data-exit may keep it serving traffic even mid-promotion).
- **One carve-out to "no secrets on standby": the B2 credential.** The heartbeat publisher needs it. This is the lowest-blast-radius secret in the system (stolen B2 key → adversary reads age-encrypted backups → still cannot decrypt them). Codified in invariant #23.
- **`node_id` is free-text.** A typo splits a logical standby into two ghost identities. No registry, no validation. The deployment runbook should document a convention (`eu-{role}-{region}-{index}`).
- **B2 versioning retains stale heartbeat objects.** Heartbeats overwrite a single key, but Object Lock + Versioning keep every prior version. Over a year this is small but nonzero garbage. Accept; the alternative (disabling Object Lock for the heartbeat prefix) is a worse trade.
- **Backup-from-node-A restored to node-B carries A's `node_state` and inventory.** Operator can mis-promote with impunity (the resulting active "is" node A). No programmatic guard; documented assumption that the operator drives this carefully. A future enhancement: print a warning when the restored backup's `eu_nodes` row matching the local hostname has `retired_at IS NOT NULL`.
- **Manual promotion is the floor on RTO.** T2 §7 already names this; spec F restates it: recovery time is bounded by operator availability, not by software speed. The design accepts this in exchange for eliminating split-brain risk.

---

## 12. §12 obligation summary (deployment-runbook view)

| Obligation | Healthy interval | What "proven" means |
|---|---|---|
| `eu_standby_liveness_seen::<node_id>` | ≤ `staleness_alert_seconds` (default 10m) | Active polled B2 and saw a fresh heartbeat from that standby |
| `eu_standby_liveness_stale::<node_id>` (anti-obligation) | absent | Presence = standby has not heartbeat'd in `staleness_alert_seconds` |
| `eu_standby_drill_proven::<node_id>` | ≤ 30d | Operator ran a T2 end-to-end drill against that standby and recorded it |
| `t2_dryrun_caseA` | ≤ 30d | Operator ran a Case-A drill (proven by `standby-drill-proven --case A`) |
| `t2_dryrun_caseB` | ≤ 30d | Operator ran a Case-B drill (proven by `standby-drill-proven --case B`) |
