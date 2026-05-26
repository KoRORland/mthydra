# Spec I2 — Per-(Box, Vantage) Probe Credentials

Status: **Draft, awaiting operator review.**
Predecessor: `doc/design.md` §8 ("Connect with a **probe** credential (never a user's)"), `doc/specs/2026-05-25-I-probe-vantage-harness.md` (§11 residual #7: "the operator generates probe credentials out-of-band").

---

## 1. Purpose

Design §8's Job 1 explicitly says probes must connect with a **probe credential**, never a user's. Spec I MVP punted on this — the operator generated probe credentials out-of-band and the controller had no record of which vantage was using which credential. Spec I2 closes the gap with a per-(box, vantage) credential table that mirrors `onward_credentials` but lives separately so user credentials and probe credentials never accidentally cross-pollinate.

**Out of scope:** the actual cryptographic operation a vantage performs to authenticate (out-of-band runbook concern; spec I2 stores the bytes the operator generates). Issuing identical credentials to multiple vantages for the same box (rejected by a UNIQUE constraint).

---

## 2. Locked design decisions

| ID | Decision | Rationale |
|---|---|---|
| I2-D1 | **Separate `probe_credentials` table — not a `kind` column on `onward_credentials`.** | A `kind` column would force every existing call site to filter; the failure mode of "forgot the filter, leaked a probe cred to a user" is silent. Distinct tables make the type system catch the mistake. |
| I2-D2 | **One credential per (box, vantage, authority_generation) tuple.** A vantage probing a box uses one credential at a time; rotation = revoke + issue. UNIQUE on `(box_id, vantage_id, authority_generation, revoked_at IS NULL)`. | Mirrors `onward_credentials` discipline. |
| I2-D3 | **Credential blob is generated controller-side via the same `sign_onward_credential` helper.** | One signing path; one trust anchor; the only difference is which table the row lands in. |
| I2-D4 | **Revocation is immediate.** `revoked_at` UPDATE — that's it. No grace period; probe vantages should pick up the new credential on next cycle (out-of-band operator concern). | Same as user credentials. |
| I2-D5 | **CLI: `probe-credential-issue --box <id> --vantage <id> [--evidence <text>]`** + **`probe-credential-list [--box ...] [--vantage ...] [--include-revoked]`** + **`probe-credential-revoke <cred_id> --reason <text>`**. | Three operations, three commands. |

---

## 3. Schema additions (v13 → v14)

```sql
CREATE TABLE probe_credentials (
  cred_id              TEXT PRIMARY KEY,
  box_id               TEXT NOT NULL,
  vantage_id           TEXT NOT NULL,
  credential           BLOB NOT NULL,
  issued_at            TEXT NOT NULL,
  revoked_at           TEXT,
  authority_generation INTEGER NOT NULL,
  FOREIGN KEY (box_id) REFERENCES ru_boxes(box_id),
  FOREIGN KEY (vantage_id) REFERENCES probe_vantages(vantage_id),
  FOREIGN KEY (authority_generation) REFERENCES credential_authority(generation)
);

CREATE UNIQUE INDEX ix_probe_credentials_active
  ON probe_credentials(box_id, vantage_id, authority_generation)
  WHERE revoked_at IS NULL;

CREATE INDEX ix_probe_credentials_box ON probe_credentials(box_id);
CREATE INDEX ix_probe_credentials_vantage ON probe_credentials(vantage_id);
```

`SCHEMA_VERSION = 14`; `migrate_v13_to_v14` creates table + indexes.

---

## 4. Repository API

`mthydra.controller.state.probe_credentials`:

```python
@dataclass(frozen=True)
class ProbeCredential:
    cred_id: str
    box_id: str
    vantage_id: str
    credential: bytes
    issued_at: str
    revoked_at: str | None
    authority_generation: int

def issue(conn, *, box_id, vantage_id, authority_generation,
          credential, issued_at) -> str: ...               # returns cred_id
def revoke(conn, cred_id, *, at, reason) -> None: ...
def list_active_for_box(conn, box_id) -> list[ProbeCredential]: ...
def list_active_for_vantage(conn, vantage_id) -> list[ProbeCredential]: ...
def list_all(conn, *, box_id=None, vantage_id=None,
             include_revoked=False) -> list[ProbeCredential]: ...
```

---

## 5. CLI

```
probe-credential-issue --box <box_id> --vantage <vantage_id> [--evidence <text>]
    # Generates a fresh Ed25519-signed blob via the same path as onward
    # credentials, inserts a probe_credentials row, audit-logs.

probe-credential-list [--box <id>] [--vantage <id>] [--include-revoked] [--json]
    # Read-only.

probe-credential-revoke <cred_id> --reason <text>
    # UPDATE revoked_at. Audit-logged.
```

---

## 6. Test plan

- Unit: issue + revoke + list + UNIQUE-on-active raises on double-issue
- CLI: each subcommand happy + error paths
- Spec C unaffected; probe-record (spec I) still uses `vantage_id` directly — the credentials are an operator-side concern between the vantage and the box

---

## 7. Honest residuals

1. **Spec I2 stores credentials but does not push them to vantages.** Distribution of the cred blob to the vantage operator is out-of-band — same as the operator-driven probe submission in spec I-D1. Future automation would close this gap.
2. **Revoking a probe credential does not stop probe-record from accepting submissions with that vantage_id.** The spec I record path doesn't reference probe_credentials. A future enhancement: the record path could check that the vantage has an active credential for that box. Named here, not built.
3. **No `kind` discrimination at the use site.** Probe credentials are stored separately precisely *because* spec I doesn't currently use them at the use site; once it does (residual #2), the table boundary still provides the type-safety win.
