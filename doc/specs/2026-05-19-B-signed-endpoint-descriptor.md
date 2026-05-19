# Spec B — Signed Endpoint Descriptor

Status: **Draft, awaiting operator review.**
Predecessor: `doc/design.md`, `doc/build-plan.md` artifact `B`, `doc/specs/2026-05-18-A-controller-state-and-backup.md`.
Successors blocked on this: `C` (cover-domain pool), `E` (RU node init), `G` (provisioning artifact generator).

---

## 1. Purpose

Define the controller's signed-endpoint-descriptor mechanism: a generationally-numbered, Ed25519-signed JSON document that pins the current EU exit set, validity window, and signing key metadata. The controller produces descriptors; RU boxes verify them without trusting anything derived from their own state.

This spec also closes spec A's `_placeholder_keypair_bytes` residual by replacing the opaque byte-string placeholder in `descriptor_signing_key` rows with real Ed25519 key material.

Out of scope: descriptor *transport* to RU boxes (spec E/G), credential-authority key generation (spec A `_fresh_pem` — different table, different spec), observability integration (spec J), RU image packaging of the verifier (spec D/E).

---

## 2. Locked design decisions

Approved during planning session 2026-05-19.

| ID | Decision | Rationale |
|---|---|---|
| B-D1 | **Ed25519 plain** (no pre-hash) | 32-byte keys, 64-byte signatures, no parameter choices to get wrong. PyCA `cryptography` library, already a transitive dep via `boto3` → `botocore`. |
| B-D2 | **Canonical JSON; store exact signed bytes** | Payload stored in `descriptor_history.payload` as the exact bytes that were signed. Never re-serialise for verification — avoids canonicalisation bugs. Floats are prohibited in payload to sidestep IEEE-754 drift. Canonical form: `json.dumps(sort_keys=True, separators=(",",":"), ensure_ascii=False).encode("utf-8")`. |
| B-D3 | **New `eu_exit_set` table** in spec B | No spec-A table covers EU exit endpoints. Operator-populated until spec F (EU node setup) automates it. Empty set is allowed; produces a descriptor with empty `eu_exit_set`. |
| B-D4 | **`previous_generation_hash` chain field** | sha256 of the previous descriptor's payload bytes. Defends against generation-drop rollback attacks. Absent only for generation 1. |
| B-D5 | **RU `valid_until` grace = 24h** | Tolerates RU clock drift. Strict on controller side (controller never signs a descriptor that is already expired). |
| B-D6 | **Pure-Python verifier with zero `mthydra.controller` imports** | `mthydra.descriptor.verify` is RU-callable. Spec E will copy it into RU image builds. Import isolation enforced by a unit test (AST walk). |
| B-D7 | **Trust ≤ 2 concurrent signing keys** (current + outgoing) | Sufficient for graceful rotation without accumulation of stale trusted keys. |
| B-D8 | **`next_signing_pubkey` advertised in current descriptor** | RU learns about an upcoming key via the descriptor channel itself, not via the seed bundle. |
| B-D9 | **One-shot `descriptor-migrate-placeholder`** for spec A → B migration | Detects placeholder format (`b"PRIV-DESC-"` prefix), mints a real Ed25519 key, signs a fresh descriptor. Produces an audit_log entry. |
| B-D10 | **Transport out of scope** | Descriptors are transport-agnostic (signed bytes; replay-safe via `generation` + `valid_until` + chain hash). Transport is spec E/G. |
| B-D11 | **`signing-key-rotate` is single-step** | Mints + activates + signs in one command. Old key remains trusted for one validity window. |

---

## 3. Schema additions (v1 → v2)

### 3.1 New table: `eu_exit_set`

```sql
CREATE TABLE eu_exit_set (
  fingerprint  TEXT PRIMARY KEY,    -- hex sha256 of EU node public key
  endpoint     TEXT NOT NULL,       -- "host:port" or opaque transport address
  weight       INTEGER NOT NULL DEFAULT 1,
  added_at     TEXT NOT NULL,
  retired_at   TEXT                 -- NULL = currently in the active set
);
```

### 3.2 `descriptor_history` — add `signature` column

```sql
ALTER TABLE descriptor_history ADD COLUMN signature BLOB NOT NULL DEFAULT X'';
```

In practice the migration adds the column and backfills the default. New rows must always carry a real 64-byte signature.

### 3.3 Schema version bump

`SCHEMA_VERSION` advances from 1 → 2. The migration function `migrate_v1_to_v2(conn)` runs automatically during `apply_schema` when an existing v1 DB is detected.

---

## 4. Descriptor payload format (`mthydra.descriptor.v1`)

All timestamps ISO-8601 UTC with `Z` suffix. All binary fields hex-encoded strings.

```json
{
  "schema":                   "mthydra.descriptor.v1",
  "generation":               1,
  "signing_key_gen":          1,
  "issued_at":                "2026-05-19T00:00:00Z",
  "valid_until":              "2026-05-19T01:00:00Z",
  "eu_exit_set": [
    {
      "fingerprint":  "abcd1234...",
      "endpoint":     "eu1.example.org:443",
      "weight":       1
    }
  ],
  "previous_generation_hash": null,
  "next_signing_pubkey":      null
}
```

Field constraints:
- `schema` MUST be the literal string `"mthydra.descriptor.v1"`.
- `generation` MUST be a positive integer, monotonically increasing.
- `signing_key_gen` MUST reference an active or outgoing `descriptor_signing_key.generation`.
- `issued_at` MUST precede `valid_until`.
- `eu_exit_set` MAY be empty (during controller bootstrapping).
- `previous_generation_hash` MUST be null iff `generation == 1`.
- `next_signing_pubkey` is null unless a signing key rotation is in progress; when non-null it is the hex pubkey of the *next* (pre-published but not yet active) signing key.
- No float values anywhere in the payload.

Canonical serialisation: `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")`.

Signed content: the canonical bytes of the payload object. The signature is `Ed25519Sign(privkey, canonical_bytes)`.

---

## 5. Module layout

```
src/mthydra/
├── descriptor/
│   ├── __init__.py
│   ├── keys.py       # Ed25519 generate / serialise / sign / verify-primitive
│   ├── payload.py    # DescriptorPayload dataclass + canonical encoding
│   ├── sign.py       # Controller-side: assemble from DB + sign + persist
│   ├── verify.py     # RU-callable: pure verifier, zero controller imports
│   └── scheduler.py  # DescriptorRotator (APScheduler job)
├── controller/
│   └── state/
│       └── eu_exit_set.py  # Repository for eu_exit_set table
```

---

## 6. Sign path (`descriptor/sign.py`)

```
sign_new_descriptor(conn, *, now_iso, valid_until_iso, next_signing_pubkey_hex=None)
  → (generation: int, payload_bytes: bytes, signature: bytes)
```

Steps:
1. Read active `descriptor_signing_key` (highest generation with `retired_at IS NULL`).
2. Read previous descriptor (max generation in `descriptor_history`); sha256 its payload bytes.
3. Read `eu_exit_set` active rows, sorted by fingerprint for stability.
4. Assemble `DescriptorPayload`; compute canonical bytes.
5. Sign: `Ed25519Sign(privkey, canonical_bytes)` → 64-byte signature.
6. INSERT into `descriptor_history`: `generation`, `payload` (canonical bytes decoded as UTF-8 text), `signed_at`, `valid_until`, `signing_key_generation`, `signature`.
7. Audit-log the event.

Error conditions:
- No active `descriptor_signing_key` → `SignError`.
- Signing key in placeholder format → `SignError` with message directing operator to run `descriptor-migrate-placeholder`.

---

## 7. Verify path (`descriptor/verify.py`)

Zero imports from `mthydra.controller`. Importable standalone.

```python
verify_descriptor(
    blob: bytes,
    signature: bytes,
    trusted_keys: Sequence[TrustedKey],
    now_iso: str,
    *,
    previous_descriptor_hash: str | None = None,
    grace_hours: int = 24,
) -> DescriptorPayload
```

Raises `VerifyError` on:
- Invalid canonical JSON or schema mismatch.
- `signing_key_gen` not in `trusted_keys`.
- Signature does not verify under the named trusted key.
- `now_iso > valid_until + grace_hours`.
- `previous_descriptor_hash` provided and does not match `payload.previous_generation_hash`.
- Generation 1 with non-null `previous_generation_hash`.
- Unknown fields in payload (strict mode).

Cold-start rule: if `previous_descriptor_hash=None` and `payload.previous_generation_hash` is also null (generation 1), verification passes. If `previous_descriptor_hash=None` but `payload.previous_generation_hash` is non-null, verification **fails** (defends against TOFU rollback).

---

## 8. Rotation semantics

### R1 — Routine descriptor rotation

`DescriptorRotator` in `descriptor/scheduler.py` fires `sign_new_descriptor` on an `IntervalTrigger(hours=rotation_interval_hours)` using the existing `BackgroundScheduler`. Runs inside `serve`. Config:

```toml
[descriptor]
rotation_interval_hours = 1
validity_window_hours   = 24
```

### R2 — EU exit set change

`eu-add` and `eu-retire` CLI commands call `sign_new_descriptor` immediately after the DB mutation. No separate scheduler needed.

### R3 — Signing key rotation (`signing-key-rotate`)

Single-step (B-D11):
1. Generate a new Ed25519 keypair.
2. INSERT new `descriptor_signing_key` row (generation N+1, `retired_at IS NULL`).
3. Mark previous key as *outgoing*: set `retired_at = now + validity_window_hours` (still trusted for one window).
4. Call `sign_new_descriptor` immediately with the new key active; the new descriptor's `signing_key_gen = N+1` and `next_signing_pubkey = None` (new key is now current, no further pending rotation).
5. Audit-log.

Trust set at any point: all `descriptor_signing_key` rows whose `retired_at IS NULL OR retired_at > now`.  At most 2 during a rotation window (B-D7).

---

## 9. CLI surface

All subcommands added to `mthydra-controller`:

| Subcommand | Arguments | Behaviour |
|---|---|---|
| `descriptor-sign-now` | `[--db-path] [--config]` | Force a fresh sign; print new generation. |
| `descriptor-show` | `[--generation N] [--db-path]` | Print canonical payload JSON (pretty-printed). Latest if N omitted. |
| `descriptor-verify` | `<payload-file> <sig-file> [--db-path] [--now ISO]` | Verify against DB's trusted keys; print PASS/FAIL. |
| `signing-key-rotate` | `[--db-path] [--config]` | Generate new key, activate, sign (B-D11). |
| `eu-add` | `<fingerprint> <endpoint> [--weight N] [--db-path]` | Add to exit set; sign immediately. |
| `eu-retire` | `<fingerprint> [--db-path]` | Retire from exit set; sign immediately. |
| `descriptor-migrate-placeholder` | `[--db-path] [--config]` | One-shot: detect placeholder → mint real key → sign. |

---

## 10. Startup self-check additions (appended to spec A §10)

Run after the existing spec-A checks:

- **Check 13.** At most 2 `descriptor_signing_key` rows have `retired_at IS NULL OR retired_at > now_iso`. Refuse to start on violation.
- **Check 14.** Every `descriptor_history` row's `signing_key_generation` references a real `descriptor_signing_key` row. Refuse on violation.
- **Check 15.** For every `descriptor_history` row with `generation > 1`, the `previous_generation_hash` in the payload matches sha256 of the previous row's payload. Refuse on violation (DB tampering).
- **Check 16.** If the active `descriptor_signing_key.privkey` starts with the placeholder prefix `b"PRIV-DESC-"`, refuse to start in `production` mode with a message directing the operator to run `descriptor-migrate-placeholder`. Skipped in `dryrun`/`offline` modes.

---

## 11. §12 obligation contribution

Spec B owns a new obligation: `descriptor_signing_key_rotation` — time since the signing key was last rotated.

- **Default cadence:** 8760h (yearly).
- **How it is refreshed:** `signing-key-rotate` writes to `obligation_clocks`.
- **Seeded:** `init` seeds `last_proven_at = now, proven_by = "bootstrap"` with `next_due_at = now + 8760h`.

Added to `controller.toml.example`:

```toml
[obligations.timers_hours]
# ... existing ...
descriptor_signing_key_rotation = 8760  # yearly
```

---

## 12. Failure-mode catalogue

| Failure | Detection | Response |
|---|---|---|
| `cryptography` package missing | Import fails at startup | Controller refuses to start (loud). |
| Active signing key is placeholder | Startup check 16 | Refuse to start in production; run `descriptor-migrate-placeholder`. |
| More than 2 active signing keys | Startup check 13 | Refuse to start; operator investigates `descriptor_signing_key` table. |
| Chain hash mismatch in `descriptor_history` | Startup check 15 | Refuse to start; DB may have been tampered with. |
| `eu_exit_set` empty at sign time | Allowed; descriptor signed with empty set | Warn in audit_log; RU boxes will verify successfully but see no endpoints. |
| Signature verification fails on RU | `VerifyError` | RU refuses to update its endpoint cache; retains previous descriptor. |
| Descriptor expired (past grace) | `VerifyError` at `verify_descriptor` | RU retains previous descriptor; operator must push a fresh sign. |
| `valid_until` in the past when signing | `SignError` (clock check in sign path) | Should not happen in normal ops; arises only from extreme clock skew. |

---

## 13. Test plan

### 13.1 Unit (pytest)

- `test_keys.py` — round-trip, tamper-detect, wrong-length inputs.
- `test_payload.py` — canonical encoding stability, round-trip, unknown-field rejection.
- `test_eu_exit_set.py` — add/retire/list/weight.
- `test_sign.py` — sign with seeded DB, generation increment, chain hash links, placeholder rejection.
- `test_verify.py` — every `VerifyError` condition has its own test; cold-start rule; multi-key trust.
- `test_verify_import_isolation.py` — AST walk asserts no `mthydra.controller` imports in `verify.py`.

### 13.2 Integration (pytest)

- `test_descriptor_roundtrip.py` — init DB, add 2 eu exits, sign, fetch from DB, verify with DB's active pubkey; assert chain links on second sign.

### 13.3 Property (Hypothesis)

- `test_descriptor_chain_integrity.py` — random sequence of (sign, eu-add, eu-retire, signing-key-rotate). Invariant: `verify_chain([all descriptors in order])` succeeds; every `signing_key_gen` is in the trust set `{current, outgoing}`; chain hashes link.

### 13.4 Coverage target

≥ 90% line coverage on `mthydra.descriptor.*`.

---

## 14. Open items deliberately deferred to consuming specs

- Spec E will define how the verifier and trusted pubkeys are embedded in the RU image build.
- Spec G will define the seed-bundle format that carries the initial trusted pubkey(s) to a freshly-provisioned RU box.
- Spec D will handle detection and alerting on RU clock skew > 24h.
- Spec J will attach the descriptor-sign failure path to the outbound alerting channel.
- Spec F will automate `eu-add` / `eu-retire` as EU nodes are provisioned and decommissioned.

---

## 15. Honest residuals

- **Closed (from spec A):** `_placeholder_keypair_bytes` in `bootstrap.py` — replaced by real Ed25519 key generation in this spec.
- **Carried forward (from spec A):** `_fresh_pem` in `restore/adopt.py` — this is `credential_authority`, not descriptor signing; not in scope for spec B.
- **Carried forward (from spec A):** synthetic `do_backup` on `init` — unrelated to spec B.
- **Transport (B-D10).** Descriptor bytes have no defined delivery channel until specs E/G. During the window between spec B landing and spec E landing, descriptors exist in the DB but are never read by any RU box. Acceptable — spec B's value is the *artifact* and *verification contract*, not the delivery.
- **Signing key custody (B-D1 + spec A D1).** Private signing key stored plaintext in `descriptor_signing_key.privkey`. Same threat model as spec A: controller seizure triggers Case-B, descriptor re-key is part of that response. Not closed at this layer by design.
- **RU clock skew > 24h (B-D5).** Grace window is generous but not infinite. Deferred to spec D / observability.
- **Placeholder migration on existing controllers.** Any controller running spec A will have `PRIV-DESC-` placeholder keys. Startup check 16 will refuse production mode until `descriptor-migrate-placeholder` is run. This is a one-time manual step; document in release notes when spec B is deployed.
- **Empty `eu_exit_set`.** Valid state; signed descriptor has empty list. RU sees no endpoints and retains whatever it had before. Acceptable during bootstrapping; spec F closes this.
- **`cryptography` version pinning.** Currently a transitive dep via `botocore`; not pinned directly. If `botocore` drops or changes the dep, spec B breaks loudly at import time (startup check catches it). Accept; add explicit pin to `pyproject.toml` in this spec.
