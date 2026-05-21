# Spec G — RU Provisioning Artifact Generator

Status: **Draft, awaiting operator review.**
Predecessor: `doc/design.md` §3 (Operational model), `doc/specs/2026-05-18-A-controller-state-and-backup.md` (schema, `_placeholder_keypair_pem` residual), `doc/specs/2026-05-19-B-signed-endpoint-descriptor.md`, `doc/specs/2026-05-19-C-cover-domain-pool-manager.md` (`assign_to_box` contract), `doc/specs/2026-05-20-F-eu-node-setup.md` (`authority-rotate`), `doc/specs/2026-05-21-D-ru-image-build-pipeline.md` (`current_promoted`, signed B2 URL).
Successors blocked on this: `G2` (provider-API VM provisioning), `E` (RU node init — consumer of the seed), `F2` (data-exit — runtime verifier of onward credentials), `H` (shard manager — sets `ru_boxes.shard_id` after `provision-seed`).

---

## 1. Purpose

Make the EU controller able to *provision* an RU box: emit a self-contained seed bundle the box can boot from, atomically claim a cover domain + image + onward credential for it, and record the new box in the inventory. This is the bridge between everything the controller knows (verified domains, promoted image, signed descriptor, authority key) and a freshly-launched RU VM that needs that bundle delivered to tmpfs at first boot.

Spec G also closes the last remaining placeholder residual: spec A's `bootstrap._placeholder_keypair_pem()` for `credential_authority`. The authority's job — signing per-box onward credentials — only becomes real here, because G is the first spec that actually mints onward credentials. Spec B closed the descriptor signing key placeholder; spec G closes the authority placeholder.

Out of scope: provider-API calls to launch the actual VM (deferred to spec **G2**). Descriptor refresh after first boot (spec E). Box shard assignment (spec H). Runtime credential verification on the EU data-exit (spec F2 — but spec G defines the verifier interface F2 will copy).

---

## 2. Locked design decisions

Approved during brainstorming session 2026-05-21.

| ID | Decision | Rationale |
|---|---|---|
| G-D1 | **Scope = seed generation + close spec A's authority placeholder.** Provider-API VM provisioning deferred to spec G2. | Tunnel-software-style sysadmin work (AWS / Hetzner SDK calls) belongs in its own spec, mirroring the F → F2 split. G1 ships a real, testable bundle the operator can paste into a provider console; G2 automates the paste. |
| G-D2 | **Onward credential = Ed25519-signed JSON token** `{schema, box_id, issued_at, authority_generation}`. Wire format: 2-byte BE length prefix + canonical JSON + 64-byte Ed25519 signature. | Mirrors spec B's descriptor wire format: same crypto, same canonicalisation rules, same pure-Python verifier discipline. Revocation lives in `onward_credentials.revoked_at` (the data-exit consults the controller's list), not in the token. |
| G-D3 | **Output = cloud-init `#cloud-config` YAML by default**; `--format json` emits the raw payload. | The design says "cloud-init into tmpfs"; default matches. JSON-only path stays available for operators using non-cloud-init provisioners. |
| G-D4 | **Seed contains: box_id, sni, transport_role, onward_credential, authority_pubkey, descriptor trust anchors, initial signed descriptor inline, signed image URL + sha256, issued_at.** | Box boots with everything it needs to validate its own credential, validate descriptors (current + outgoing pubkeys per spec B B-D7), and fetch+verify the image — no network calls before the relay starts. |
| G-D5 | **State transition = `provision-seed` inserts the row as `provisioning`; operator runs `ru-box-mark-live` after confirming the box is up.** Shard assignment deferred to spec H. | The operator drives the live-confirmation in the same way they attest cover-pool verification and EU-standby drills (spec C / spec F pattern). Spec I/J will later automate live-detection through probes. |
| G-D6 | **`provision-seed` is atomic** — cover-domain assignment, ru_boxes insert, onward credential issue all happen in one transaction. B2 URL minting happens *after* commit. | Atomicity prevents the worst failure (orphaned cover domain or orphaned credential). B2 minting after commit creates a documented edge case (URL fails to mint → row exists without usable URL); this is an honest residual rather than a structural correctness gap. |
| G-D7 | **`authority-migrate-placeholder` is a one-shot CLI** mirroring spec B's `descriptor-migrate-placeholder`. | Spec B's pattern is proven. Detect the `PRIV-BOOTSTRAP-` prefix; replace in place; emit audit row; idempotent. Invariant #26 confirms migration completed. |

---

## 3. Onward credential format

### 3.1 Wire format

```
+-----------------+-------------------------------------------+----------------------+
| 2-byte BE len N | <N bytes of canonical JSON, UTF-8>        | 64-byte Ed25519 sig  |
+-----------------+-------------------------------------------+----------------------+
```

Length prefix lets the verifier locate the signature without parsing the JSON first. Canonical JSON = `json.dumps(payload, sort_keys=True, separators=(",", ":"))` — same as spec B's descriptor.

### 3.2 Payload schema

```json
{
  "schema": "mthydra.onward_credential.v1",
  "box_id": "...",
  "issued_at": "2026-05-21T12:00:00Z",
  "authority_generation": 2
}
```

The payload deliberately omits the SNI, transport role, and any other per-box detail. Those live in `ru_boxes` (the EU side) and in the seed bundle (the RU side). The credential is only "this signed token authorises this box_id for the EU control plane." Spec F2 looks up the box_id in `ru_boxes` to make any further decisions.

### 3.3 RU-side verifier interface (spec F2 will import this verbatim)

```python
def verify_onward_credential(
    credential_bytes: bytes,
    authority_pubkey_pem: str,
) -> OnwardCredentialPayload:
    """Returns the payload on success; raises VerifyError on signature failure,
    malformed wire format, or schema-version mismatch.

    Does NOT consult the controller — pure-Python, RU-embeddable. Zero
    `mthydra.controller` imports (enforced by AST-walk test, mirroring B-D6).
    Revocation is the data-exit's job and lives outside this function.
    """


@dataclass(frozen=True)
class OnwardCredentialPayload:
    schema: str          # always "mthydra.onward_credential.v1"
    box_id: str
    issued_at: str
    authority_generation: int


class VerifyError(RuntimeError): ...
```

---

## 4. Authority migration (closes spec A residual)

Spec A's `bootstrap._placeholder_keypair_pem()` emits `("PRIV-BOOTSTRAP-<hex>", "PUB-BOOTSTRAP-<hex>")` strings. Spec G replaces these with real Ed25519 PEM-encoded keys.

### 4.1 Key generation

```python
def generate_authority_keypair() -> tuple[str, str]:
    """Generate a fresh Ed25519 authority keypair.

    Returns (privkey_pem, pubkey_pem) where:
      - privkey_pem is PKCS#8 PEM (unencrypted; spec A D1 says state lives plaintext)
      - pubkey_pem is SubjectPublicKeyInfo PEM

    Uses cryptography.hazmat.primitives.asymmetric.ed25519 (already a spec B
    dependency).
    """
```

### 4.2 `authority-migrate-placeholder` CLI

Mirrors `descriptor-migrate-placeholder` from spec B exactly. For each non-retired `credential_authority` row whose `privkey_pem` starts with `"PRIV-BOOTSTRAP-"`:

1. Mint a fresh Ed25519 keypair via `generate_authority_keypair()`.
2. UPDATE the row's `privkey_pem` and `pubkey_pem` (keep the same `generation` number).
3. Emit `audit_log` row: `action='authority_migrated_placeholder'`, target=`generation`, details=`{"old_prefix": "PRIV-BOOTSTRAP-"}`.

Idempotent: re-running on an already-migrated DB is a no-op (no placeholder rows present).

Active-only. Invariant #26 (§5) catches any deployment where this migration was skipped before production traffic starts.

### 4.3 `authority-rotate` update

Spec F's `authority-rotate` currently calls `_placeholder_keypair_pem()`. Spec G replaces that call with `generate_authority_keypair()`. One-line change. Existing tests must be updated to expect real PEM strings rather than `"PRIV-BOOTSTRAP-..."` placeholders.

---

## 5. Invariants (extending spec D's #24–#25)

- **#26 — Authority is real Ed25519 (production mode).** Every non-retired `credential_authority.privkey_pem` does NOT start with `"PRIV-BOOTSTRAP-"`. Skipped in `mode='offline'` / `mode='dryrun'`. Mirrors spec B's check #16 for the descriptor signing key.
- **#27 — Every live box has an active onward credential.** For each `ru_boxes` row with `state IN ('provisioning','live')`, at least one `onward_credentials` row with matching `box_id AND revoked_at IS NULL` exists.
- **#28 — No two non-terminated boxes share an SNI.** `SELECT sni, COUNT(*) FROM ru_boxes WHERE state != 'terminated' GROUP BY sni HAVING COUNT(*) > 1` returns no rows. (The `ru_boxes.sni UNIQUE` column already enforces this; #28 is a redundancy check that surfaces clearly if the constraint is ever weakened by a future migration.)

Each check raises `InvariantViolation` with the offending row(s). #26 is the load-bearing addition — it is the first time the authority placeholder is structurally caught at startup.

---

## 6. Seed bundle format

The bundle is a JSON document delivered to the RU box via cloud-init at first boot. Spec E's RU init script parses it from `/run/mthydra/seed.json` (tmpfs).

```json
{
  "schema": "mthydra.ru_seed.v1",
  "box_id": "01HXAA-...",
  "sni": "calm-cover-domain.example",
  "transport_role": "ru_relay",
  "onward_credential": "<base64 of length-prefixed JSON + 64-byte sig>",
  "authority_pubkey_pem": "-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----\n",
  "descriptor_trust_anchors": [
    "<base64 of 32-byte Ed25519 raw pubkey>",
    "<base64 of 32-byte Ed25519 raw pubkey, if outgoing key present>"
  ],
  "initial_descriptor": "<base64 of length-prefixed JSON + 64-byte sig>",
  "image": {
    "version": "<sha256 hex of mtg binary>",
    "url": "<short-lived signed B2 URL>",
    "url_expires_at": "2026-05-21T13:00:00Z",
    "sha256": "<same hex>",
    "size_bytes": 10485760
  },
  "issued_at": "2026-05-21T12:00:00Z",
  "issued_by_authority_generation": 2
}
```

**Field semantics:**

- `box_id` — `uuid.uuid4()` string. Appears in `ru_boxes.box_id` AND in the signed onward credential payload.
- `sni` — the cover domain from `cover_domain_pool` that was atomically assigned to this box.
- `transport_role` — single value `"ru_relay"` for MVP. Reserved for D2's canary role.
- `onward_credential` — base64 of the full length-prefixed signed token from §3.1.
- `authority_pubkey_pem` — PEM-encoded Ed25519 pubkey of the *issuing* authority generation. The box uses this to confirm its own credential validates (sanity check). EU data-exit (F2) uses the same pubkey at runtime to validate incoming connections.
- `descriptor_trust_anchors` — base64 of raw 32-byte Ed25519 pubkeys, one per non-retired `descriptor_signing_key` row (current + outgoing during rotation; B-D7). The RU verifier accepts a descriptor signed by *any* listed key. B-D8 ensures the next-signing key is advertised in the descriptor itself, so the trust set grows naturally on rotation.
- `initial_descriptor` — base64 of the *current* signed descriptor bytes (canonical JSON + 64-byte sig). The box validates this at boot using the trust anchors; this gives the box its initial EU exit set without any network calls.
- `image.url` — short-lived signed B2 URL (default TTL: 1 hour, configurable via `--ttl-seconds`). `image.sha256` lets the box verify the downloaded bytes before exec.
- `issued_at` / `issued_by_authority_generation` — provisioning metadata. The seed itself has no TTL, but the embedded `image.url_expires_at` effectively limits seed usability (after the URL expires, the box can't download the binary → won't start).

### 6.1 cloud-init wrapper (default output)

```yaml
#cloud-config
write_files:
  - path: /run/mthydra/seed.json
    permissions: '0600'
    owner: root:root
    content: |
      {<the JSON above, indented>}
runcmd:
  - mkdir -p /run/mthydra
  - chmod 0700 /run/mthydra
```

No `runcmd:` entry to start the relay yet — that's spec E's job (the RU init script reads `/run/mthydra/seed.json`, downloads the binary, validates sha256, launches mtg). The cloud-init `write_files:` order guarantees the seed lands before any `runcmd:` runs; spec E will append its own `runcmd:` entry when it ships.

### 6.2 `--format json` output

Just the JSON payload, no YAML wrapper. For operators using non-cloud-init provisioning.

---

## 7. Repository + signing API

### 7.1 New module: `mthydra.descriptor.authority`

Companion to `mthydra.descriptor.keys` (descriptor signing keys). Naming reflects "Ed25519 key management is consolidated under `descriptor/`."

```python
def generate_authority_keypair() -> tuple[str, str]:
    """Ed25519 PKCS#8 private + SPKI public, both PEM-encoded."""


def sign_onward_credential(
    privkey_pem: str,
    *,
    box_id: str,
    issued_at: str,
    authority_generation: int,
) -> bytes:
    """Returns the length-prefixed (canonical JSON + 64-byte sig) credential
    blob suitable for storing in onward_credentials.credential."""


def verify_onward_credential(
    credential_bytes: bytes,
    authority_pubkey_pem: str,
) -> OnwardCredentialPayload:
    """Pure-Python verifier. RU-embeddable. See §3.3."""


@dataclass(frozen=True)
class OnwardCredentialPayload:
    schema: str
    box_id: str
    issued_at: str
    authority_generation: int


class VerifyError(RuntimeError): ...
```

AST-walk test enforces zero imports from `mthydra.controller.*` (mirroring spec B B-D6).

### 7.2 New module: `mthydra.controller.provisioning.seed`

```python
class ProvisionError(RuntimeError): ...


def provision_box(
    *,
    conn: sqlite3.Connection,
    b2_destination,
    provider: str,
    region: str,
    image_signed_url_ttl_seconds: int,
    now: str,
    actor: str = "operator",
) -> SeedBundle:
    """Atomic provisioning transaction.

    Procedure:
      1. current_authority(conn) — refuse with ProvisionError if placeholder
         (#26 check inlined: privkey_pem.startswith('PRIV-BOOTSTRAP-')).
      2. ru_images.current_promoted(conn) — refuse with ProvisionError if None.
      3. Pick a candidate_verified cover_domain (sort by added_at ASC, then
         domain ASC). Refuse with ProvisionError if none.
      4. Fetch latest descriptor_history row. Refuse if none.
      5. Generate box_id = str(uuid.uuid4()).
      6. BEGIN transaction:
         - insert_box(conn, box_id=..., provider, region, public_ip=None,
                      sni=<picked_domain>, image_version=<promoted.image_version>,
                      created_at=now)
         - cover_pool.assign_to_box(conn, <picked_domain>, box_id=box_id, at=now)
         - sign onward credential
         - credentials.issue_credential(conn, box_id, credential, now,
                                         authority_generation=<current.generation>)
         COMMIT.
      7. presigned_image_url(image_version, ttl=image_signed_url_ttl_seconds)
         — happens after commit. If this raises, the box exists but has no
         usable image URL; documented honest residual.
      8. Collect descriptor_signing_key non-retired pubkeys.
      9. Build SeedBundle dataclass.
     10. log_event action='box_provisioned', target=box_id,
         details_json={"sni": ..., "image_version": ..., "authority_gen": ...}.

    Returns the SeedBundle. Caller renders to JSON or cloud-init.
    """


@dataclass(frozen=True)
class SeedBundle:
    schema: str
    box_id: str
    sni: str
    transport_role: str
    onward_credential_b64: str
    authority_pubkey_pem: str
    descriptor_trust_anchors_b64: tuple[str, ...]
    initial_descriptor_b64: str
    image_version: str
    image_url: str
    image_url_expires_at: str
    image_sha256: str
    image_size_bytes: int
    issued_at: str
    issued_by_authority_generation: int

    def to_json(self) -> bytes: ...           # canonical (sort_keys, no whitespace)
    def to_json_pretty(self) -> bytes: ...    # indent=2 for cloud-init display
    def to_cloud_init(self) -> bytes:
        """Returns a #cloud-config YAML wrapping to_json_pretty() in write_files:."""
```

### 7.3 `S3Destination` extension

```python
def presigned_image_url(
    self, *, image_version: str, ttl_seconds: int = 3600,
) -> tuple[str, str]:
    """Returns (url, expires_at_iso). Generates a short-lived signed GET URL
    for images/<image_version>/mtg via boto3 generate_presigned_url."""
```

---

## 8. CLI

All commands active-only unless flagged otherwise; all emit audit rows.

```
mthydra-controller provision-seed \
    --provider <name> --region <name> \
    [--format cloud-init|json] [--ttl-seconds <n>] \
    [--db-path ...] [--config ...]
    # Atomic provisioning. Emits seed (default cloud-init YAML; --format json
    # for raw payload). Default TTL: 3600s.
    # Refuses if: placeholder authority (#26), no promoted image, no
    # candidate_verified cover domain, no signed descriptor in DB.
    # Exit codes: 0 OK / 2 input err / 3 missing prerequisite (image / domain /
    # descriptor / migrated authority) / 5 B2 URL minting failed (row already
    # committed; operator must retry or terminate).

mthydra-controller ru-box-list [--state ...] [--json] [--db-path ...]
    # Prints all ru_boxes rows + their assigned SNI + image_version + state.
    # Active-only.

mthydra-controller ru-box-mark-live <box_id> --public-ip <ip> [--db-path ...]
    # state: provisioning -> live. Sets public_ip, went_live_at.
    # Emits audit action='ru_box_live'.

mthydra-controller ru-box-terminate <box_id> --reason <text> [--db-path ...]
    # state: provisioning|live -> terminated. In one transaction:
    #   - revoke ALL active onward_credentials for this box_id
    #   - cover_pool.rotate_and_burn(conn, sni, reason=..., last_box_id=box_id, at=now)
    #   - mark_terminated(conn, box_id, ...)
    # Emits audit action='ru_box_terminated'.

mthydra-controller authority-migrate-placeholder [--db-path ...] [--config ...]
    # One-shot. Detects placeholder rows in credential_authority and replaces
    # with real Ed25519. Idempotent: no-op if no placeholder rows.
    # Emits audit action='authority_migrated_placeholder'.
```

### 8.1 Bootstrap obligations

Added to the active-init obligation dict in `cli.py`:

```python
"g_provision_drill_proven":  90 * 24,    # 90 days — operator drill cadence
```

No `provision-drill-proven` CLI is shipped in this spec; operators clear the obligation by actually provisioning a box every 90 days (or by manually stamping the obligation via the existing `obligation-proven` command). A dedicated CLI is a small follow-up.

### 8.2 §12 obligation summary

| Obligation | Healthy interval | What "proven" means |
|---|---|---|
| `g_provision_drill_proven` | ≤ 90d | Operator has provisioned at least one box in the last 90 days (provisioning-path heartbeat) |

---

## 9. Cross-spec contracts

- **Spec G2 (provider-API VM provisioning).** Calls `provision_box(...)` to mint the seed, then submits it as userdata to the provider's create-VM API (Hetzner Cloud, AWS EC2, etc.). Captures the resulting `public_ip` and calls `ru-box-mark-live`. May add `--auto-provision` to `provision-seed` once G2 lands.
- **Spec E (RU node init).** Reads `/run/mthydra/seed.json` from tmpfs at first boot. Parses fields per §6. Downloads + verifies the image binary against `image.sha256`. Validates the embedded `initial_descriptor` against `descriptor_trust_anchors`. Launches mtg with the configured SNI + onward credential. Implements descriptor refresh (spec G does not).
- **Spec F2 (data-exit).** Imports `mthydra.descriptor.authority.verify_onward_credential` verbatim (RU-embeddable). Pulls the revocation list (`onward_credentials.revoked_at IS NOT NULL`) from controller state on a refresh cadence and refuses revoked credentials. The verifier is pure-Python with zero `mthydra.controller` imports.
- **Spec H (shard manager).** Sets `ru_boxes.shard_id` after `provision-seed` runs. May trigger `provision-seed` automatically when a shard needs more capacity. Calls `ru-box-terminate` when retiring boxes, which already calls spec C's `rotate_and_burn` per existing contract.
- **Spec C (cover-domain pool).** Receives `assign_to_box` inside `provision_box`'s transaction. The C-G contract was pre-declared in spec C §10; spec G fulfills it.
- **Spec B (descriptor).** Reads `descriptor_history` (latest signed payload) + `descriptor_signing_key` (non-retired pubkeys) at provision time. No mutation.
- **Spec D (RU image).** Reads `current_promoted()` from `ru_images`. Mints a presigned URL via `S3Destination.presigned_image_url(image_version, ttl_seconds)`. No mutation.
- **Spec F (`authority-rotate`).** Updated to call `generate_authority_keypair()` instead of `_placeholder_keypair_pem()`. One-line change in the handler.

---

## 10. Honest residuals (Spec G)

- **B2 URL minting happens after the DB transaction commits.** If B2 is unreachable at provision time, the operator ends up with a provisioning row + a signed credential but no usable image URL. They can re-mint manually (or terminate and re-provision). G2 will fold URL minting into the transaction or add a `re-mint-image-url` CLI; for MVP this is a documented edge case.
- **Cover-domain selection is deterministic** (sort by `added_at` then domain). An adversary observing two provisioning events back-to-back could correlate the *order* of cover-domain assignment. Practical risk is low (cover domains are independently published; ordering reveals nothing) but worth noting. Randomised selection is a one-line change later.
- **Onward credential has no TTL.** Revocation lives in `onward_credentials.revoked_at`; the F2 data-exit must consult the revocation list. A revoked credential remains cryptographically valid; only the revocation list distinguishes live from revoked. Matches spec A D1's plaintext-state model.
- **First descriptor is baked into the seed.** A box held in inventory for hours before booting will start with a stale descriptor. Mitigation: short signing cadence (spec B `rotation_interval_hours=1` default). Spec E's descriptor-refresh closes the long-term version.
- **Image presigned URL TTL = 1 hour by default.** Seeds older than the TTL contain a dead link. Operators must paste the seed into the provider console within minutes of generation. Loud failure (box can't download binary → won't start) rather than silent compromise.
- **Single transport role.** `"ru_relay"` is hard-coded. D2 may add a second role ("ru_canary"); spec G's MVP does not need it.
- **`provision-seed` does not assign a shard.** `ru_boxes.shard_id` is NULL until spec H. Boxes can be live but unbound to any user subset until H runs.
- **Drill obligation is informational only.** `g_provision_drill_proven` is seeded but no CLI proves it. Operators clear by actually provisioning. A `provision-drill-proven` CLI is a small follow-up.
- **`authority-migrate-placeholder` and the `authority-rotate` update are MUST-run-before-production steps.** A production deployment that boots straight off spec A without running these will fail invariant #26 at startup. This is the intended behaviour — startup is the right place to catch it, not at first `provision-seed`.

---

## 11. Test discipline

Coverage target: ≥ 90% on `mthydra.descriptor.authority`, `mthydra.controller.provisioning.seed`. ≥ 85% on `mthydra.controller.provisioning.cloud_init`.

**Unit tests:**

- `tests/unit/descriptor/test_authority.py` — keypair round-trip; sign/verify happy + tamper/wrong-key/malformed-prefix failure modes; AST-walk for no `mthydra.controller.*` imports.
- `tests/unit/controller/provisioning/test_seed.py` — `provision_box` happy path; refusals for missing prerequisites (#26, no image, no domain, no descriptor); atomicity (mid-transaction failure rolls back assign_to_box); `SeedBundle.to_json()` and `to_cloud_init()` round-trip.

**Invariant tests:** extend `tests/unit/controller/state/test_invariants.py` with checks for #26 (placeholder rejected in production, allowed in offline), #27 (live box without active credential rejected), #28 (duplicate non-terminated SNI rejected via raw SQL).

**CLI tests:** extend `tests/unit/controller/test_cli.py` with happy + named-failure paths for `provision-seed` (both formats), `ru-box-list --json`, `ru-box-mark-live`, `ru-box-terminate`, `authority-migrate-placeholder` (idempotent re-run).

**Integration test:** `tests/integration/test_provisioning_lifecycle.py` — full provisioning flow including credential verification via the pure-Python verifier and descriptor verification via embedded trust anchors.

### 11.1 Failure-mode catalogue

| Failure | Behaviour |
|---|---|
| Operator runs `provision-seed` before `authority-migrate-placeholder` | `ProvisionError("authority is still a placeholder; run authority-migrate-placeholder first")`; #26 confirms at startup |
| `provision-seed` with no promoted image | `ProvisionError("no promoted ru_image; run image-promote first")` |
| `provision-seed` with empty verified cover-pool | `ProvisionError("no candidate_verified cover_domain available")` |
| `provision-seed` before first descriptor sign | `ProvisionError("no signed descriptor in descriptor_history; run descriptor-sign-now first")` |
| Two concurrent `provision-seed` calls racing on the same cover domain | SQLite serialises the BEGIN transaction; the second call sees the first's `state='in_use'` and picks the next candidate. Spec C's `assign_to_box` already raises if the domain isn't `candidate_verified`. |
| B2 presigned URL minting fails after commit | `provision-seed` exits 5; the box exists in the catalog with no usable URL. Operator may terminate + re-provision or re-mint manually (deferred to G2). Audit log captures the partial state. |
| Mid-transaction interrupt between `assign_to_box` and `issue_credential` | SQLite rolls back: cover_domain returns to `candidate_verified`, no `ru_boxes` row, no `onward_credentials` row. Verified by atomicity unit test. |
| `ru-box-mark-live` on a non-existent box | CLI exit 2 with `ru-box-mark-live: box <id> not found` |
| `ru-box-terminate` on an already-terminated box | CLI exit 2 with the underlying `ValueError` from `mark_terminated` |
| `authority-migrate-placeholder` re-run on already-migrated DB | No-op; no audit row written; exit 0 |

---

## 12. Status

**G1: drafted, awaiting implementation.** Once G1 ships, the foundation track gains: a real authority key (closing the last spec-A placeholder), a working `provision-seed` CLI that produces operator-pasteable cloud-init, and a verifiable onward-credential format spec F2 can adopt verbatim. The next foundation-side specs that become possible are **E** (RU node init — now fully unblocked, since both D and G have shipped what it consumes) and **H** (shard manager — already unblocked, would consume `provision-seed` to auto-scale shards).
