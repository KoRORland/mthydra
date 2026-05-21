# Spec G — RU Provisioning Artifact Generator (G1) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement G1 of spec G — `doc/specs/2026-05-21-G-ru-provisioning-artifact-generator.md`: real Ed25519 `credential_authority` (closes spec A's last placeholder), Ed25519-signed onward credentials with a pure-Python RU-embeddable verifier, atomic `provision_box` transaction (cover-domain assign + ru_boxes insert + credential issue), seed bundle output in JSON or cloud-init YAML, B2 presigned URL minting for image binaries, four CLI subcommands. G2 (provider-API VM provisioning) is out of scope.

**Architecture:** New `mthydra.descriptor.authority` module owns the credential crypto, sitting alongside `mthydra.descriptor.keys` (descriptor signing). New `mthydra.controller.provisioning.seed` module owns the atomic provisioning transaction and the `SeedBundle` dataclass with `to_json()` + `to_cloud_init()` rendering. `S3Destination` grows `presigned_image_url`. Four new CLI subcommands: `provision-seed`, `ru-box-list`, `ru-box-mark-live`, `ru-box-terminate`, `authority-migrate-placeholder`. Spec F's `authority-rotate` is updated in place (one-line change). Startup invariants #26–#28 catch placeholder authority, credential-less live boxes, and SNI collisions.

**Tech stack:** Python 3.12 + `cryptography` (Ed25519 — already a spec B dependency) + boto3 + APScheduler (unchanged). PyYAML or plain string templating for the cloud-init wrapper — plan uses string templating to avoid a new dependency.

**Design decisions:** See spec §2 (G-D1 through G-D7).

---

## File Structure (locked before tasks)

**Modified:**
- `src/mthydra/controller/cli.py` — five new subcommands + handlers; bootstrap obligations extended; `authority-rotate` handler updated to call `generate_authority_keypair()`.
- `src/mthydra/controller/state/invariants.py` — extend `check_all()` with invariants #26–#28.
- `src/mthydra/controller/backup/s3_dest.py` — add `presigned_image_url` method.
- `tests/unit/controller/state/test_invariants.py` — add #26–#28 tests.
- `tests/unit/controller/test_cli.py` — add CLI tests for the five new subcommands; update existing `authority-rotate` test for real Ed25519 output.

**Created:**
- `src/mthydra/descriptor/authority.py` — `generate_authority_keypair`, `sign_onward_credential`, `verify_onward_credential`, `OnwardCredentialPayload`, `VerifyError`. RU-embeddable.
- `src/mthydra/controller/provisioning/__init__.py` — empty.
- `src/mthydra/controller/provisioning/seed.py` — `provision_box`, `SeedBundle`, `ProvisionError`.
- `tests/unit/descriptor/test_authority.py`
- `tests/unit/controller/provisioning/__init__.py` — empty.
- `tests/unit/controller/provisioning/test_seed.py`
- `tests/integration/test_provisioning_lifecycle.py`

Responsibility per file: `descriptor/authority.py` owns the onward-credential crypto and is RU-embeddable (zero `mthydra.controller` imports, enforced by AST-walk test mirroring spec B B-D6). `provisioning/seed.py` owns the atomic provisioning transaction and seed-bundle rendering. CLI is thin — handlers delegate.

---

## Phase 1 — Authority crypto

### Task 1: `mthydra.descriptor.authority` module

**Files:**
- Create: `src/mthydra/descriptor/authority.py`
- Create: `tests/unit/descriptor/test_authority.py`

- [ ] **Step 1: Write failing tests**

```python
"""Spec G — onward-credential crypto + authority keypair generation."""
import base64

import pytest

from mthydra.descriptor.authority import (
    OnwardCredentialPayload, VerifyError,
    generate_authority_keypair, sign_onward_credential, verify_onward_credential,
)


def test_generate_authority_keypair_returns_real_pem():
    priv, pub = generate_authority_keypair()
    assert priv.startswith("-----BEGIN PRIVATE KEY-----")
    assert pub.startswith("-----BEGIN PUBLIC KEY-----")
    assert not priv.startswith("PRIV-BOOTSTRAP-")


def test_sign_and_verify_round_trip():
    priv, pub = generate_authority_keypair()
    blob = sign_onward_credential(
        priv,
        box_id="box-xyz",
        issued_at="2026-05-21T12:00:00Z",
        authority_generation=2,
    )
    payload = verify_onward_credential(blob, pub)
    assert isinstance(payload, OnwardCredentialPayload)
    assert payload.box_id == "box-xyz"
    assert payload.issued_at == "2026-05-21T12:00:00Z"
    assert payload.authority_generation == 2
    assert payload.schema == "mthydra.onward_credential.v1"


def test_sign_is_deterministic_for_fixed_inputs():
    """Ed25519 is deterministic: same inputs + same key → same bytes."""
    priv, _ = generate_authority_keypair()
    a = sign_onward_credential(priv, box_id="b", issued_at="t", authority_generation=1)
    b = sign_onward_credential(priv, box_id="b", issued_at="t", authority_generation=1)
    assert a == b


def test_verify_rejects_tampered_json():
    priv, pub = generate_authority_keypair()
    blob = sign_onward_credential(priv, box_id="box-1", issued_at="t", authority_generation=1)
    # Flip a single byte inside the JSON section.
    # Layout: [2-byte BE length N][N bytes JSON][64-byte sig]
    n = int.from_bytes(blob[:2], "big")
    tampered = blob[:2] + bytes([blob[2] ^ 0x01]) + blob[3:2 + n] + blob[2 + n:]
    with pytest.raises(VerifyError):
        verify_onward_credential(tampered, pub)


def test_verify_rejects_tampered_signature():
    priv, pub = generate_authority_keypair()
    blob = sign_onward_credential(priv, box_id="box-1", issued_at="t", authority_generation=1)
    tampered = blob[:-1] + bytes([blob[-1] ^ 0x01])
    with pytest.raises(VerifyError):
        verify_onward_credential(tampered, pub)


def test_verify_rejects_wrong_pubkey():
    priv1, _ = generate_authority_keypair()
    _, pub2 = generate_authority_keypair()  # different keypair
    blob = sign_onward_credential(priv1, box_id="b", issued_at="t", authority_generation=1)
    with pytest.raises(VerifyError):
        verify_onward_credential(blob, pub2)


def test_verify_rejects_truncated_blob():
    priv, pub = generate_authority_keypair()
    blob = sign_onward_credential(priv, box_id="b", issued_at="t", authority_generation=1)
    with pytest.raises(VerifyError):
        verify_onward_credential(blob[:10], pub)


def test_verify_rejects_wrong_schema_version():
    """Manually craft a payload with a future schema version; verify must refuse."""
    import json
    import struct
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.hazmat.primitives import serialization

    priv_bytes = ed25519.Ed25519PrivateKey.generate()
    priv_pem = priv_bytes.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    pub_pem = priv_bytes.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    payload = json.dumps(
        {"schema": "mthydra.onward_credential.v99", "box_id": "x",
         "issued_at": "t", "authority_generation": 1},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    sig = priv_bytes.sign(payload)
    blob = struct.pack(">H", len(payload)) + payload + sig
    with pytest.raises(VerifyError, match="schema"):
        verify_onward_credential(blob, pub_pem)
```

- [ ] **Step 2: Run tests — expect ImportError**

```bash
cd /home/asharov/RedHat/Dev/mthydra
pytest tests/unit/descriptor/test_authority.py -v
```

- [ ] **Step 3: Create `src/mthydra/descriptor/authority.py`**

```python
"""Spec G — onward-credential crypto + authority keypair generation.

RU-embeddable: zero imports from mthydra.controller.* — enforced by AST-walk
test (mirroring spec B B-D6). Spec F2's data-exit copies this module verbatim.

Wire format:
    [2-byte BE length N][N bytes canonical JSON UTF-8][64-byte Ed25519 sig]
"""
from __future__ import annotations

import json
import struct
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

_SCHEMA_V1 = "mthydra.onward_credential.v1"


class VerifyError(RuntimeError):
    """Raised by verify_onward_credential on any failure."""


@dataclass(frozen=True)
class OnwardCredentialPayload:
    schema: str
    box_id: str
    issued_at: str
    authority_generation: int


def generate_authority_keypair() -> tuple[str, str]:
    """Generate a fresh Ed25519 authority keypair.

    Returns (privkey_pem, pubkey_pem) — PKCS#8 PEM private, SPKI PEM public.
    """
    priv = ed25519.Ed25519PrivateKey.generate()
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    pub_pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return priv_pem, pub_pem


def sign_onward_credential(
    privkey_pem: str,
    *,
    box_id: str,
    issued_at: str,
    authority_generation: int,
) -> bytes:
    """Returns the length-prefixed (canonical JSON + 64-byte sig) credential blob."""
    priv = serialization.load_pem_private_key(privkey_pem.encode("utf-8"), password=None)
    if not isinstance(priv, ed25519.Ed25519PrivateKey):
        raise ValueError("privkey_pem must be an Ed25519 PKCS#8 PEM key")
    payload = {
        "schema": _SCHEMA_V1,
        "box_id": box_id,
        "issued_at": issued_at,
        "authority_generation": authority_generation,
    }
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sig = priv.sign(payload_bytes)
    return struct.pack(">H", len(payload_bytes)) + payload_bytes + sig


def verify_onward_credential(
    credential_bytes: bytes,
    authority_pubkey_pem: str,
) -> OnwardCredentialPayload:
    """Pure-Python verifier. RU-embeddable.

    Raises VerifyError on signature failure, schema mismatch, or malformed wire format.
    """
    if len(credential_bytes) < 2 + 64:
        raise VerifyError("credential too short")
    n = int.from_bytes(credential_bytes[:2], "big")
    if len(credential_bytes) != 2 + n + 64:
        raise VerifyError(
            f"credential length mismatch: header says {2 + n + 64}, "
            f"actual {len(credential_bytes)}"
        )
    payload_bytes = credential_bytes[2:2 + n]
    sig = credential_bytes[2 + n:]

    try:
        pub = serialization.load_pem_public_key(authority_pubkey_pem.encode("utf-8"))
    except Exception as e:
        raise VerifyError(f"invalid authority pubkey: {e}") from e
    if not isinstance(pub, ed25519.Ed25519PublicKey):
        raise VerifyError("authority_pubkey_pem is not Ed25519")

    try:
        pub.verify(sig, payload_bytes)
    except InvalidSignature as e:
        raise VerifyError("signature verification failed") from e

    try:
        payload = json.loads(payload_bytes)
    except json.JSONDecodeError as e:
        raise VerifyError(f"payload is not valid JSON: {e}") from e

    if payload.get("schema") != _SCHEMA_V1:
        raise VerifyError(f"unknown schema: {payload.get('schema')!r}")

    try:
        return OnwardCredentialPayload(
            schema=payload["schema"],
            box_id=payload["box_id"],
            issued_at=payload["issued_at"],
            authority_generation=int(payload["authority_generation"]),
        )
    except (KeyError, ValueError, TypeError) as e:
        raise VerifyError(f"payload missing or malformed field: {e}") from e
```

- [ ] **Step 4: Run tests** — expect PASS (8/8)

```bash
pytest tests/unit/descriptor/test_authority.py -v
pytest tests/unit -q
```

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/descriptor/authority.py tests/unit/descriptor/test_authority.py
git commit -m "descriptor(G): authority module — Ed25519 onward credentials + RU-embeddable verifier"
```

---

### Task 2: AST-walk test — `authority` is RU-embeddable

**Files:**
- Modify: `tests/unit/descriptor/test_authority.py`

- [ ] **Step 1: Find the existing spec-B AST-walk test for the descriptor verifier**

```bash
grep -rn "ast.parse\|controller\." tests/unit/descriptor/ 2>&1 | head -20
```

It's likely in `tests/unit/descriptor/test_verify.py` checking `mthydra.descriptor.verify`. Mirror that test for `mthydra.descriptor.authority`.

- [ ] **Step 2: Append the AST-walk test** to `tests/unit/descriptor/test_authority.py`:

```python
def test_authority_module_has_no_controller_imports():
    """RU-embeddability: spec F2 copies this module verbatim. Zero mthydra.controller.*
    imports means it can run on the RU box without the controller package present."""
    import ast
    import pathlib

    src = pathlib.Path(
        "src/mthydra/descriptor/authority.py"
    ).read_text()
    tree = ast.parse(src)
    bad: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.startswith("mthydra.controller"):
                bad.append(f"line {node.lineno}: from {mod}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("mthydra.controller"):
                    bad.append(f"line {node.lineno}: import {alias.name}")
    assert not bad, "authority.py must not import from mthydra.controller.*:\n  " + "\n  ".join(bad)
```

- [ ] **Step 3: Run test** — expect PASS

```bash
pytest tests/unit/descriptor/test_authority.py::test_authority_module_has_no_controller_imports -v
```

- [ ] **Step 4: Commit**

```bash
git add tests/unit/descriptor/test_authority.py
git commit -m "test(G): AST-walk — authority module has zero mthydra.controller.* imports"
```

---

## Phase 2 — Authority migration + rotate update

### Task 3: `authority-migrate-placeholder` CLI + `authority-rotate` real-Ed25519 update

**Files:**
- Modify: `src/mthydra/controller/cli.py`
- Modify: `tests/unit/controller/test_cli.py`

- [ ] **Step 1: Write failing tests** — append to `tests/unit/controller/test_cli.py`:

```python
def test_authority_migrate_placeholder_replaces_placeholder(tmp_path, age_recipient):
    """authority-migrate-placeholder converts PRIV-BOOTSTRAP-... to real Ed25519."""
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])

    from mthydra.controller.state.authority import current_authority
    from mthydra.controller.state.db import connect
    conn = connect(db)
    before = current_authority(conn)
    assert before.privkey_pem.startswith("PRIV-BOOTSTRAP-")
    conn.close()

    rc = run(["authority-migrate-placeholder",
              "--db-path", str(db), "--config", str(cfg_path)])
    assert rc == 0

    conn = connect(db)
    after = current_authority(conn)
    assert after.generation == before.generation  # same generation; in-place
    assert after.privkey_pem.startswith("-----BEGIN PRIVATE KEY-----")
    assert after.pubkey_pem.startswith("-----BEGIN PUBLIC KEY-----")
    conn.close()


def test_authority_migrate_placeholder_idempotent(tmp_path, age_recipient, capsys):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    run(["authority-migrate-placeholder", "--db-path", str(db), "--config", str(cfg_path)])
    capsys.readouterr()
    rc = run(["authority-migrate-placeholder", "--db-path", str(db), "--config", str(cfg_path)])
    assert rc == 0
    # No-op on second run; output may say "nothing to migrate" or similar.


def test_authority_migrate_placeholder_refused_on_standby(tmp_path, age_recipient, capsys):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    run(["init", "--role", "standby", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    rc = run(["authority-migrate-placeholder", "--db-path", str(db), "--config", str(cfg_path)])
    assert rc == 2
    assert "active-only" in capsys.readouterr().err.lower()


def test_authority_rotate_uses_real_ed25519(tmp_path, age_recipient):
    """authority-rotate now uses generate_authority_keypair() — not PRIV-BOOTSTRAP-."""
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    # First migrate so the existing row is real; then rotate produces another real row.
    run(["authority-migrate-placeholder", "--db-path", str(db), "--config", str(cfg_path)])
    rc = run(["authority-rotate", "--db-path", str(db), "--config", str(cfg_path)])
    assert rc == 0

    from mthydra.controller.state.authority import current_authority
    from mthydra.controller.state.db import connect
    conn = connect(db)
    cur = current_authority(conn)
    assert cur.generation == 2
    assert cur.privkey_pem.startswith("-----BEGIN PRIVATE KEY-----")
    conn.close()
```

- [ ] **Step 2: Verify failure**

```bash
pytest tests/unit/controller/test_cli.py -k 'authority_migrate or authority_rotate_uses_real' -v
```

- [ ] **Step 3: Add subparser + dispatch in `src/mthydra/controller/cli.py`**

Subparser (alongside the spec-F `authority-rotate`):

```python
    amp = sub.add_parser("authority-migrate-placeholder",
                          help="replace PRIV-BOOTSTRAP-* authority rows with real Ed25519")
    amp.add_argument("--db-path", default=DEFAULT_DB)
    amp.add_argument("--config", default="/etc/mthydra/controller.toml")
```

Dispatch:

```python
    if args.cmd == "authority-migrate-placeholder":
        return _cmd_authority_migrate_placeholder(args)
```

- [ ] **Step 4: Add handler** at the bottom of `cli.py`:

```python
def _cmd_authority_migrate_placeholder(args) -> int:
    import json as _json

    from mthydra.controller.state.audit import log_event
    from mthydra.controller.state.db import connect
    from mthydra.descriptor.authority import generate_authority_keypair

    conn = connect(args.db_path)
    try:
        rc = _require_active_role(conn, "authority-migrate-placeholder")
        if rc is not None:
            return rc
        rows = conn.execute(
            "SELECT generation, privkey_pem FROM credential_authority "
            "WHERE privkey_pem LIKE 'PRIV-BOOTSTRAP-%'"
        ).fetchall()
        if not rows:
            print("authority-migrate-placeholder: no placeholder rows; nothing to migrate")
            return 0
        now = _now()
        for gen, _old_priv in rows:
            priv_pem, pub_pem = generate_authority_keypair()
            conn.execute(
                "UPDATE credential_authority SET privkey_pem=?, pubkey_pem=? "
                "WHERE generation=?",
                (priv_pem, pub_pem, gen),
            )
            log_event(
                conn, ts=now, actor="operator",
                action="authority_migrated_placeholder",
                target=str(gen),
                details_json=_json.dumps({"old_prefix": "PRIV-BOOTSTRAP-"},
                                          separators=(",", ":")),
            )
        conn.commit()
        print(f"authority-migrate-placeholder: migrated {len(rows)} row(s) to real Ed25519")
        return 0
    finally:
        conn.close()
```

- [ ] **Step 5: Update `_cmd_authority_rotate` to call `generate_authority_keypair()`**

Find the existing `_cmd_authority_rotate` handler. Replace the line that calls `_placeholder_keypair_pem()` with:

```python
        from mthydra.descriptor.authority import generate_authority_keypair
        priv, pub = generate_authority_keypair()
```

(Remove the `from mthydra.controller.bootstrap import _placeholder_keypair_pem` import; replace with the line above.)

- [ ] **Step 6: Run tests + full suite**

```bash
pytest tests/unit/controller/test_cli.py -k 'authority' -v
pytest tests/unit -q
```

If the existing spec-F `test_authority_rotate_adds_new_generation` test asserts something specific to the placeholder format, update it to expect `-----BEGIN PRIVATE KEY-----` instead.

- [ ] **Step 7: Commit**

```bash
git add src/mthydra/controller/cli.py tests/unit/controller/test_cli.py
git commit -m "cli(G): authority-migrate-placeholder + authority-rotate uses real Ed25519"
```

---

## Phase 3 — Invariants #26-#28

### Task 4: Startup invariants

**Files:**
- Modify: `src/mthydra/controller/state/invariants.py`
- Modify: `tests/unit/controller/state/test_invariants.py`

- [ ] **Step 1: Write failing tests** — append to `tests/unit/controller/state/test_invariants.py`:

```python
# ---------------------------------------------------------------------------
# Spec G invariant checks (#26–#28)
# ---------------------------------------------------------------------------

def test_check_26_rejects_placeholder_authority_in_production(tmp_db_path):
    """Forcing a PRIV-BOOTSTRAP- privkey must trip #26 in production mode."""
    conn = _seeded(tmp_db_path)
    conn.execute(
        "UPDATE credential_authority SET privkey_pem='PRIV-BOOTSTRAP-test' "
        "WHERE retired_at IS NULL"
    )
    conn.commit()
    with pytest.raises(InvariantViolation, match="check 26"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION,
                  mode="production", now_iso=NOW)


def test_check_26_allows_placeholder_in_offline(tmp_db_path):
    """Same placeholder must NOT raise in offline mode."""
    conn = _seeded(tmp_db_path)
    conn.execute(
        "UPDATE credential_authority SET privkey_pem='PRIV-BOOTSTRAP-test' "
        "WHERE retired_at IS NULL"
    )
    conn.commit()
    check_all(conn, expected_schema_version=SCHEMA_VERSION,
              mode="offline", now_iso=NOW)


def test_check_26_allows_real_ed25519(tmp_db_path):
    conn = _seeded(tmp_db_path)
    from mthydra.descriptor.authority import generate_authority_keypair
    priv, pub = generate_authority_keypair()
    conn.execute(
        "UPDATE credential_authority SET privkey_pem=?, pubkey_pem=? "
        "WHERE retired_at IS NULL",
        (priv, pub),
    )
    conn.commit()
    # Should NOT raise.
    check_all(conn, expected_schema_version=SCHEMA_VERSION,
              mode="production", now_iso=NOW)


def test_check_27_rejects_live_box_without_credential(tmp_db_path):
    """A live ru_boxes row with no matching active onward_credentials row is invalid."""
    conn = _seeded(tmp_db_path)
    # Migrate the placeholder so #26 doesn't fire first.
    from mthydra.descriptor.authority import generate_authority_keypair
    priv, pub = generate_authority_keypair()
    conn.execute(
        "UPDATE credential_authority SET privkey_pem=?, pubkey_pem=? "
        "WHERE retired_at IS NULL",
        (priv, pub),
    )
    # Insert a live box without a credential.
    from mthydra.controller.state.ru_boxes import insert_box, mark_live
    insert_box(conn, "boxX", "aws", "eu-1", "10.0.0.1", "sni-x.invalid",
               "img-v1", NOW)
    mark_live(conn, "boxX", public_ip="10.0.0.1", at=NOW)
    conn.commit()
    with pytest.raises(InvariantViolation, match="check 27"):
        check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)


def test_check_28_rejects_two_non_terminated_boxes_with_same_sni(tmp_db_path):
    """Bypass UNIQUE by toggling PRAGMA; #28 catches it as a defence-in-depth."""
    conn = _seeded(tmp_db_path)
    # Migrate authority so #26 passes.
    from mthydra.descriptor.authority import generate_authority_keypair
    priv, pub = generate_authority_keypair()
    conn.execute(
        "UPDATE credential_authority SET privkey_pem=?, pubkey_pem=? "
        "WHERE retired_at IS NULL",
        (priv, pub),
    )
    # SQLite UNIQUE will block normal inserts; we bypass via the integrity-check
    # approach (insert one row, then UPDATE another row to the same sni via PRAGMA).
    # Simpler: insert one row, then INSERT OR IGNORE a second — SQLite UNIQUE
    # makes this impossible at the engine level, so we toggle the UNIQUE
    # constraint check by inserting with a tweaked sni then UPDATE-ing it.
    conn.execute(
        "INSERT INTO ru_boxes (box_id, provider, region, public_ip, sni, "
        "state, image_version, created_at) "
        "VALUES ('b1', 'aws', 'eu', '10.0.0.1', 'shared.invalid', "
        "'provisioning', 'img', ?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO ru_boxes (box_id, provider, region, public_ip, sni, "
        "state, image_version, created_at) "
        "VALUES ('b2', 'aws', 'eu', '10.0.0.2', 'unique-temp.invalid', "
        "'provisioning', 'img', ?)",
        (NOW,),
    )
    # Now overwrite b2's sni to collide. The UNIQUE constraint blocks UPDATE
    # too, so we drop the column via a temp table — but that's heavy. Instead,
    # rely on the fact that the constraint may have been removed in a future
    # migration. Run the check with the current intact DB (no collision) and
    # confirm it passes.
    conn.commit()
    # The DB is currently consistent; check_all should pass.
    # Issue an UPDATE that would create a collision and expect it to fail.
    import sqlite3 as _sql
    with pytest.raises(_sql.IntegrityError):
        conn.execute("UPDATE ru_boxes SET sni='shared.invalid' WHERE box_id='b2'")
        conn.commit()
    # Because UNIQUE catches the collision before #28 has anything to inspect,
    # the explicit invariant is defensive (would surface only if UNIQUE were
    # ever dropped). Confirm the check is wired and passes on a clean DB.
    conn.execute("ROLLBACK")  # in case the failed UPDATE left a tx open
    check_all(conn, expected_schema_version=SCHEMA_VERSION, now_iso=NOW)
```

The check-28 test is defensive — the UNIQUE constraint already prevents the collision at insert time. We confirm the constraint fires AND that `check_all` itself doesn't raise on a clean DB. A direct "two-row collision" test isn't reachable without dropping the UNIQUE; the invariant exists to catch a future schema-migration regression.

- [ ] **Step 2: Verify failure**

```bash
pytest tests/unit/controller/state/test_invariants.py -k 'check_26 or check_27 or check_28' -v
```

- [ ] **Step 3: Append checks to `check_all` in `src/mthydra/controller/state/invariants.py`**:

```python
    # --- spec G checks (#26–#28) ---

    # Check 26: authority is real Ed25519 (production mode only)
    if mode not in ("offline", "dryrun"):
        placeholder = _scalar(
            conn,
            "SELECT COUNT(*) FROM credential_authority "
            "WHERE retired_at IS NULL AND privkey_pem LIKE 'PRIV-BOOTSTRAP-%'",
        )
        if placeholder > 0:
            raise InvariantViolation(
                f"check 26: {placeholder} non-retired credential_authority row(s) "
                f"still use PRIV-BOOTSTRAP- placeholder; "
                f"run: mthydra-controller authority-migrate-placeholder"
            )

    # Check 27: every live/provisioning box has an active onward credential
    orphan = conn.execute(
        "SELECT rb.box_id FROM ru_boxes rb "
        "LEFT JOIN onward_credentials oc ON oc.box_id = rb.box_id "
        "                                  AND oc.revoked_at IS NULL "
        "WHERE rb.state IN ('provisioning','live') AND oc.cred_id IS NULL "
        "LIMIT 1"
    ).fetchone()
    if orphan is not None:
        raise InvariantViolation(
            f"check 27: ru_boxes.box_id={orphan[0]!r} is live/provisioning "
            f"but has no active onward_credentials row"
        )

    # Check 28: no two non-terminated boxes share an SNI (defence-in-depth on UNIQUE)
    dup = conn.execute(
        "SELECT sni, COUNT(*) FROM ru_boxes "
        "WHERE state != 'terminated' "
        "GROUP BY sni HAVING COUNT(*) > 1 LIMIT 1"
    ).fetchone()
    if dup is not None:
        raise InvariantViolation(
            f"check 28: SNI {dup[0]!r} shared by {dup[1]} non-terminated boxes"
        )
```

- [ ] **Step 4: Run invariant tests + full suite**

```bash
pytest tests/unit/controller/state/test_invariants.py -v
pytest tests/unit -q
```

Note: existing invariant tests (1-25) use `_seeded()` which leaves a placeholder authority. Those tests pass `now_iso=NOW` without specifying mode, defaulting to `mode="production"` per `check_all`'s default. **After this change**, those tests must either: (a) explicitly pass `mode="offline"`, OR (b) migrate the authority to real Ed25519 in `_seeded()`. Easiest fix: update `_seeded()` to mint a real Ed25519 keypair.

Look for the existing `_seeded()` helper in test_invariants.py and replace its authority insertion. Convert:

```python
insert_authority(conn, 1, "P", "K", "2026-05-18T00:00:00Z")
```

to:

```python
from mthydra.descriptor.authority import generate_authority_keypair
_priv, _pub = generate_authority_keypair()
insert_authority(conn, 1, _priv, _pub, "2026-05-18T00:00:00Z")
```

Run the full invariant suite again to confirm all pre-existing tests still pass after the helper change.

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/state/invariants.py tests/unit/controller/state/test_invariants.py
git commit -m "invariants(G): startup checks 26-28 (real authority, live-has-credential, no-shared-SNI)"
```

---

## Phase 4 — S3Destination presigned image URL

### Task 5: `presigned_image_url`

**Files:**
- Modify: `src/mthydra/controller/backup/s3_dest.py`
- Modify: `tests/unit/controller/backup/test_s3_dest.py`

- [ ] **Step 1: Append failing test**:

```python
def test_presigned_image_url_returns_signed_url_and_expiry(s3_env):
    """presigned_image_url returns (url, expires_at_iso) for the image binary."""
    dest = s3_env
    # Seed an image binary so the key exists (otherwise B2 won't sign for nothing
    # — although moto/AWS still sign URLs for missing objects; doesn't matter).
    from pathlib import Path
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    bp = tmp / "mtg"
    bp.write_bytes(b"\x7fELF" + b"\x00" * 100)
    dest.put_image(image_version="ivX", binary_path=bp, manifest=b'{"x":1}')
    url, expires_at = dest.presigned_image_url(image_version="ivX", ttl_seconds=3600)
    assert url.startswith("https://") or url.startswith("http://")
    assert "ivX" in url
    assert "X-Amz-Signature" in url or "Signature" in url
    assert expires_at  # ISO-8601 string
```

- [ ] **Step 2: Add method to `S3Destination`**:

```python
    def presigned_image_url(
        self, *, image_version: str, ttl_seconds: int = 3600,
    ) -> tuple[str, str]:
        """Generate a short-lived signed GET URL for the image binary.

        Returns (url, expires_at_iso). Uses boto3.generate_presigned_url.
        """
        url = self._client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self.bucket,
                "Key": self._image_binary_key(image_version),
            },
            ExpiresIn=ttl_seconds,
        )
        expires_at_iso = (
            datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        return url, expires_at_iso
```

- [ ] **Step 3: Run test + suite**

```bash
pytest tests/unit/controller/backup/test_s3_dest.py -v
pytest tests/unit -q
```

- [ ] **Step 4: Commit**

```bash
git add src/mthydra/controller/backup/s3_dest.py tests/unit/controller/backup/test_s3_dest.py
git commit -m "s3_dest(G): presigned_image_url for seed-bundle image references"
```

---

## Phase 5 — Provisioning module

### Task 6: `provisioning.seed.provision_box` + `SeedBundle`

**Files:**
- Create: `src/mthydra/controller/provisioning/__init__.py` (empty)
- Create: `src/mthydra/controller/provisioning/seed.py`
- Create: `tests/unit/controller/provisioning/__init__.py` (empty)
- Create: `tests/unit/controller/provisioning/test_seed.py`

- [ ] **Step 1: Create empty package files**

```bash
mkdir -p src/mthydra/controller/provisioning tests/unit/controller/provisioning
touch src/mthydra/controller/provisioning/__init__.py
touch tests/unit/controller/provisioning/__init__.py
```

- [ ] **Step 2: Write failing tests** (`tests/unit/controller/provisioning/test_seed.py`):

```python
"""Spec G — provisioning.seed unit tests."""
import base64
import json
from unittest.mock import MagicMock

import pytest

from mthydra.controller.provisioning.seed import (
    ProvisionError, SeedBundle, provision_box,
)
from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema


@pytest.fixture
def conn(tmp_db_path):
    c = connect(tmp_db_path)
    apply_schema(c)
    return c


NOW = "2026-05-21T00:00:00Z"


def _migrate_authority(conn):
    from mthydra.controller.state.authority import insert_authority, retire_authority
    from mthydra.descriptor.authority import generate_authority_keypair
    # _seeded helper in tests/unit/controller/state/test_invariants.py uses
    # generation=1 placeholders. apply_schema gives us a brand new DB with
    # nothing in credential_authority — we need to insert a real one.
    priv, pub = generate_authority_keypair()
    insert_authority(conn, 1, priv, pub, NOW)


def _seed_descriptor(conn):
    """Sign a descriptor so descriptor_history has one row."""
    from mthydra.descriptor.keys import generate_keypair
    from mthydra.controller.state.descriptor import insert_signing_key
    priv, pub = generate_keypair()
    insert_signing_key(conn, 1, priv, pub, NOW)
    # Now sign one descriptor.
    from mthydra.descriptor.sign import sign_new_descriptor
    sign_new_descriptor(conn, now_iso=NOW, valid_until_iso="2026-05-22T00:00:00Z")


def _seed_image(conn):
    from mthydra.controller.state.ru_images import insert_candidate, promote
    insert_candidate(
        conn,
        image_version="abc123",
        upstream_release="v2.1.7",
        upstream_repo="9seconds/mtg",
        binary_url="images/abc123/mtg",
        manifest_url="images/abc123/manifest.json",
        binary_sha256="abc123",
        binary_size_bytes=10485760,
        built_at=NOW,
    )
    promote(conn, "abc123", at=NOW, evidence="smoke")


def _seed_cover(conn, domain="example.cover"):
    from mthydra.controller.state.cover_pool import add_candidate, attest_verified
    add_candidate(conn, domain, added_at=NOW)
    attest_verified(conn, domain, from_vantage="ru-vps-01", at=NOW)


def _b2_mock():
    b2 = MagicMock()
    b2.presigned_image_url.return_value = ("https://b2.example/abc123/mtg?sig=zzz",
                                            "2026-05-21T01:00:00Z")
    return b2


def test_provision_box_happy_path(conn):
    _migrate_authority(conn)
    _seed_descriptor(conn)
    _seed_image(conn)
    _seed_cover(conn, "example.cover")
    b2 = _b2_mock()

    seed = provision_box(
        conn=conn, b2_destination=b2,
        provider="hetzner", region="fsn1",
        image_signed_url_ttl_seconds=3600,
        now=NOW,
    )
    assert isinstance(seed, SeedBundle)
    assert seed.sni == "example.cover"
    assert seed.transport_role == "ru_relay"
    assert seed.image_version == "abc123"
    assert "abc123" in seed.image_url
    assert len(seed.descriptor_trust_anchors_b64) == 1

    # ru_boxes row exists
    rows = conn.execute("SELECT box_id, state, sni FROM ru_boxes").fetchall()
    assert len(rows) == 1
    assert rows[0][1] == "provisioning"
    assert rows[0][2] == "example.cover"

    # cover_domain is now in_use
    state = conn.execute(
        "SELECT state, assigned_box_id FROM cover_domain_pool WHERE domain='example.cover'"
    ).fetchone()
    assert state[0] == "in_use"
    assert state[1] == rows[0][0]

    # onward_credentials row exists and verifies
    cred_row = conn.execute(
        "SELECT credential FROM onward_credentials WHERE box_id=?", (rows[0][0],)
    ).fetchone()
    assert cred_row is not None
    cred_blob = bytes(cred_row[0])

    from mthydra.descriptor.authority import verify_onward_credential
    from mthydra.controller.state.authority import current_authority
    pub_pem = current_authority(conn).pubkey_pem
    payload = verify_onward_credential(cred_blob, pub_pem)
    assert payload.box_id == rows[0][0]


def test_provision_box_refuses_placeholder_authority(conn):
    from mthydra.controller.state.authority import insert_authority
    insert_authority(conn, 1, "PRIV-BOOTSTRAP-xx", "PUB-BOOTSTRAP-xx", NOW)
    _seed_descriptor(conn)  # need descriptor key — but using placeholder for authority is the test
    # ... wait, descriptor key seeding needs to be independent of authority.
    # Re-do seeding without the descriptor-keys helper that may fail under placeholder.
    # Actually descriptor and authority are independent: descriptor_signing_key is separate.
    # _seed_descriptor only touches descriptor_signing_key + sign_new_descriptor; works
    # regardless of authority placeholder.
    _seed_image(conn)
    _seed_cover(conn, "example.cover")

    with pytest.raises(ProvisionError, match="authority"):
        provision_box(
            conn=conn, b2_destination=_b2_mock(),
            provider="hetzner", region="fsn1",
            image_signed_url_ttl_seconds=3600,
            now=NOW,
        )


def test_provision_box_refuses_no_promoted_image(conn):
    _migrate_authority(conn)
    _seed_descriptor(conn)
    _seed_cover(conn, "example.cover")
    # No image promoted.
    with pytest.raises(ProvisionError, match="image"):
        provision_box(
            conn=conn, b2_destination=_b2_mock(),
            provider="hetzner", region="fsn1",
            image_signed_url_ttl_seconds=3600,
            now=NOW,
        )


def test_provision_box_refuses_no_cover_domain(conn):
    _migrate_authority(conn)
    _seed_descriptor(conn)
    _seed_image(conn)
    # No cover_verified domain.
    with pytest.raises(ProvisionError, match="cover"):
        provision_box(
            conn=conn, b2_destination=_b2_mock(),
            provider="hetzner", region="fsn1",
            image_signed_url_ttl_seconds=3600,
            now=NOW,
        )


def test_provision_box_refuses_no_descriptor(conn):
    _migrate_authority(conn)
    _seed_image(conn)
    _seed_cover(conn, "example.cover")
    # No descriptor_history entry. _seed_descriptor would create one; skip it.
    with pytest.raises(ProvisionError, match="descriptor"):
        provision_box(
            conn=conn, b2_destination=_b2_mock(),
            provider="hetzner", region="fsn1",
            image_signed_url_ttl_seconds=3600,
            now=NOW,
        )


def test_seed_bundle_to_json_round_trips(conn):
    _migrate_authority(conn)
    _seed_descriptor(conn)
    _seed_image(conn)
    _seed_cover(conn, "example.cover")
    seed = provision_box(
        conn=conn, b2_destination=_b2_mock(),
        provider="hetzner", region="fsn1",
        image_signed_url_ttl_seconds=3600,
        now=NOW,
    )
    payload = json.loads(seed.to_json())
    assert payload["schema"] == "mthydra.ru_seed.v1"
    assert payload["sni"] == "example.cover"
    assert "onward_credential" in payload
    assert "initial_descriptor" in payload
    # Round-trip the embedded credential through the pure-Python verifier.
    cred_bytes = base64.b64decode(payload["onward_credential"])
    from mthydra.descriptor.authority import verify_onward_credential
    verified = verify_onward_credential(cred_bytes, payload["authority_pubkey_pem"])
    assert verified.box_id == payload["box_id"]


def test_seed_bundle_to_cloud_init_wraps_json(conn):
    _migrate_authority(conn)
    _seed_descriptor(conn)
    _seed_image(conn)
    _seed_cover(conn, "example.cover")
    seed = provision_box(
        conn=conn, b2_destination=_b2_mock(),
        provider="hetzner", region="fsn1",
        image_signed_url_ttl_seconds=3600,
        now=NOW,
    )
    yaml_text = seed.to_cloud_init().decode("utf-8")
    assert yaml_text.startswith("#cloud-config")
    assert "write_files:" in yaml_text
    assert "/run/mthydra/seed.json" in yaml_text
    assert "example.cover" in yaml_text  # the JSON content is inside
```

- [ ] **Step 3: Verify failure**

```bash
pytest tests/unit/controller/provisioning/test_seed.py -v
```

- [ ] **Step 4: Create `src/mthydra/controller/provisioning/seed.py`**

```python
"""Spec G — atomic RU-box provisioning + seed bundle assembly.

provision_box() is the single atomic operation that:
  1. Picks a candidate_verified cover domain (oldest-first by added_at).
  2. Mints a box_id.
  3. Inserts ru_boxes row (state='provisioning').
  4. Calls cover_pool.assign_to_box (state -> in_use).
  5. Signs an Ed25519 onward credential.
  6. Inserts an onward_credentials row.
  All inside a single SQLite transaction.
  7. Mints a B2 presigned URL (post-commit; documented honest residual).
  8. Reads descriptor_signing_key trust anchors + latest descriptor.
  9. Returns a SeedBundle ready to render as JSON or cloud-init YAML.
"""
from __future__ import annotations

import base64
import json
import sqlite3
import uuid
from dataclasses import dataclass

from mthydra.controller.state import authority as authority_repo
from mthydra.controller.state import cover_pool, credentials, ru_images
from mthydra.controller.state.audit import log_event
from mthydra.controller.state.ru_boxes import insert_box
from mthydra.descriptor.authority import sign_onward_credential


class ProvisionError(RuntimeError):
    """Raised by provision_box when prerequisites are missing."""


_SEED_SCHEMA = "mthydra.ru_seed.v1"
_TRANSPORT_ROLE = "ru_relay"


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

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "box_id": self.box_id,
            "sni": self.sni,
            "transport_role": self.transport_role,
            "onward_credential": self.onward_credential_b64,
            "authority_pubkey_pem": self.authority_pubkey_pem,
            "descriptor_trust_anchors": list(self.descriptor_trust_anchors_b64),
            "initial_descriptor": self.initial_descriptor_b64,
            "image": {
                "version": self.image_version,
                "url": self.image_url,
                "url_expires_at": self.image_url_expires_at,
                "sha256": self.image_sha256,
                "size_bytes": self.image_size_bytes,
            },
            "issued_at": self.issued_at,
            "issued_by_authority_generation": self.issued_by_authority_generation,
        }

    def to_json(self) -> bytes:
        """Canonical JSON (sorted keys, no whitespace)."""
        return json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    def to_json_pretty(self) -> bytes:
        return json.dumps(self.to_dict(), sort_keys=True, indent=2).encode("utf-8")

    def to_cloud_init(self) -> bytes:
        """Returns a #cloud-config YAML wrapping the JSON in write_files:."""
        pretty = self.to_json_pretty().decode("utf-8")
        # Indent JSON 6 spaces so it sits cleanly under `content: |`.
        indented = "\n".join("      " + line for line in pretty.splitlines())
        body = (
            "#cloud-config\n"
            "write_files:\n"
            "  - path: /run/mthydra/seed.json\n"
            "    permissions: '0600'\n"
            "    owner: root:root\n"
            "    content: |\n"
            f"{indented}\n"
            "runcmd:\n"
            "  - mkdir -p /run/mthydra\n"
            "  - chmod 0700 /run/mthydra\n"
        )
        return body.encode("utf-8")


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
    # 1. Authority must be real Ed25519 (not placeholder).
    try:
        auth = authority_repo.current_authority(conn)
    except LookupError as e:
        raise ProvisionError(f"no active credential_authority: {e}") from e
    if auth.privkey_pem.startswith("PRIV-BOOTSTRAP-"):
        raise ProvisionError(
            "authority is still a placeholder; "
            "run mthydra-controller authority-migrate-placeholder first"
        )

    # 2. A promoted image must exist.
    image = ru_images.current_promoted(conn)
    if image is None:
        raise ProvisionError(
            "no promoted ru_image; run mthydra-controller image-promote first"
        )

    # 3. Pick a candidate_verified cover domain (oldest-first by added_at).
    candidates = cover_pool.list_by_state(conn, "candidate_verified")
    if not candidates:
        raise ProvisionError(
            "no candidate_verified cover_domain available; "
            "run mthydra-controller cover-add + cover-attest-verified first"
        )
    candidates_sorted = sorted(candidates, key=lambda c: (c.added_at, c.domain))
    picked = candidates_sorted[0]

    # 4. There must be at least one signed descriptor.
    desc_row = conn.execute(
        "SELECT payload, signature FROM descriptor_history "
        "ORDER BY generation DESC LIMIT 1"
    ).fetchone()
    if desc_row is None:
        raise ProvisionError(
            "no signed descriptor in descriptor_history; "
            "run mthydra-controller descriptor-sign-now first"
        )
    desc_payload_text, desc_sig = desc_row[0], bytes(desc_row[1])

    # 5. Collect descriptor trust anchors (current + outgoing).
    pubkey_rows = conn.execute(
        "SELECT pubkey FROM descriptor_signing_key WHERE retired_at IS NULL"
    ).fetchall()
    if not pubkey_rows:
        raise ProvisionError("no non-retired descriptor_signing_key rows")
    trust_anchors_b64 = tuple(
        base64.b64encode(bytes(r[0])).decode("ascii") for r in pubkey_rows
    )

    # 6. Reconstruct the descriptor wire format (length-prefixed JSON + sig).
    import struct
    payload_bytes = desc_payload_text.encode("utf-8")
    descriptor_blob = struct.pack(">H", len(payload_bytes)) + payload_bytes + desc_sig
    initial_descriptor_b64 = base64.b64encode(descriptor_blob).decode("ascii")

    # 7. Atomic transaction: insert ru_box + assign cover domain + issue credential.
    box_id = str(uuid.uuid4())
    try:
        conn.execute("BEGIN")
        insert_box(
            conn,
            box_id=box_id,
            provider=provider,
            region=region,
            public_ip=None,
            sni=picked.domain,
            image_version=image.image_version,
            created_at=now,
        )
        cover_pool.assign_to_box(conn, picked.domain, box_id=box_id, at=now)
        credential_blob = sign_onward_credential(
            auth.privkey_pem,
            box_id=box_id,
            issued_at=now,
            authority_generation=auth.generation,
        )
        credentials.issue_credential(
            conn, box_id, credential_blob, now, authority_generation=auth.generation,
        )
        log_event(
            conn, ts=now, actor=actor, action="box_provisioned",
            target=box_id,
            details_json=json.dumps({
                "sni": picked.domain,
                "image_version": image.image_version,
                "authority_generation": auth.generation,
            }, separators=(",", ":")),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    # 8. Mint signed B2 URL (post-commit).
    image_url, image_url_expires_at = b2_destination.presigned_image_url(
        image_version=image.image_version,
        ttl_seconds=image_signed_url_ttl_seconds,
    )

    return SeedBundle(
        schema=_SEED_SCHEMA,
        box_id=box_id,
        sni=picked.domain,
        transport_role=_TRANSPORT_ROLE,
        onward_credential_b64=base64.b64encode(credential_blob).decode("ascii"),
        authority_pubkey_pem=auth.pubkey_pem,
        descriptor_trust_anchors_b64=trust_anchors_b64,
        initial_descriptor_b64=initial_descriptor_b64,
        image_version=image.image_version,
        image_url=image_url,
        image_url_expires_at=image_url_expires_at,
        image_sha256=image.binary_sha256,
        image_size_bytes=image.binary_size_bytes,
        issued_at=now,
        issued_by_authority_generation=auth.generation,
    )
```

The `credentials.issue_credential` signature in spec A is `(conn, box_id, credential, issued_at, authority_generation)` (positional). Confirm by reading `src/mthydra/controller/state/credentials.py`; adjust the call if its signature differs.

- [ ] **Step 5: Run tests**

```bash
pytest tests/unit/controller/provisioning/test_seed.py -v
```

Expected: PASS (7/7).

- [ ] **Step 6: Run full unit suite**

```bash
pytest tests/unit -q
```

- [ ] **Step 7: Commit**

```bash
git add src/mthydra/controller/provisioning/ tests/unit/controller/provisioning/
git commit -m "provisioning(G): atomic provision_box transaction + SeedBundle rendering"
```

---

## Phase 6 — CLI

### Task 7: `provision-seed` CLI

**Files:**
- Modify: `src/mthydra/controller/cli.py`
- Modify: `tests/unit/controller/test_cli.py`

- [ ] **Step 1: Append failing tests** to `tests/unit/controller/test_cli.py`:

```python
def _setup_provision_prereqs(db, age_recipient, cfg_path):
    """Build a DB that's ready for provision-seed: migrate authority,
    promote image, attest cover-domain, sign descriptor."""
    from mthydra.controller.cli import run
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_images import insert_candidate, promote
    from mthydra.controller.state.cover_pool import add_candidate, attest_verified

    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    run(["authority-migrate-placeholder", "--db-path", str(db), "--config", str(cfg_path)])
    conn = connect(db)
    insert_candidate(
        conn,
        image_version="abc123",
        upstream_release="v2.1.7",
        upstream_repo="9seconds/mtg",
        binary_url="images/abc123/mtg",
        manifest_url="images/abc123/manifest.json",
        binary_sha256="abc123",
        binary_size_bytes=10485760,
        built_at="2026-05-21T00:00:00Z",
    )
    promote(conn, "abc123", at="2026-05-21T00:01:00Z", evidence="smoke")
    add_candidate(conn, "example.cover", added_at="2026-05-21T00:02:00Z")
    attest_verified(conn, "example.cover", from_vantage="ru-vps-01",
                     at="2026-05-21T00:03:00Z")
    conn.close()
    # Sign a descriptor.
    run(["descriptor-sign-now", "--db-path", str(db), "--config", str(cfg_path)])


def test_provision_seed_cloud_init_default(tmp_path, age_recipient, capsys, monkeypatch):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)

    # Stub presigned_image_url so we don't need a real B2.
    from mthydra.controller.backup.s3_dest import S3Destination
    monkeypatch.setattr(
        S3Destination, "presigned_image_url",
        lambda self, *, image_version, ttl_seconds=3600: (
            f"https://b2.example/{image_version}/mtg?sig=stub",
            "2026-05-21T01:00:00Z",
        ),
    )
    _setup_provision_prereqs(db, age_recipient, cfg_path)
    capsys.readouterr()
    rc = run(["provision-seed",
              "--provider", "hetzner", "--region", "fsn1",
              "--db-path", str(db), "--config", str(cfg_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("#cloud-config")
    assert "write_files" in out
    assert "example.cover" in out


def test_provision_seed_json_format(tmp_path, age_recipient, capsys, monkeypatch):
    import json
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)

    from mthydra.controller.backup.s3_dest import S3Destination
    monkeypatch.setattr(
        S3Destination, "presigned_image_url",
        lambda self, *, image_version, ttl_seconds=3600: (
            f"https://b2.example/{image_version}/mtg?sig=stub",
            "2026-05-21T01:00:00Z",
        ),
    )
    _setup_provision_prereqs(db, age_recipient, cfg_path)
    capsys.readouterr()
    rc = run(["provision-seed", "--format", "json",
              "--provider", "hetzner", "--region", "fsn1",
              "--db-path", str(db), "--config", str(cfg_path)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "mthydra.ru_seed.v1"
    assert payload["sni"] == "example.cover"
    assert payload["transport_role"] == "ru_relay"


def test_provision_seed_refused_on_standby(tmp_path, age_recipient, capsys):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    run(["init", "--role", "standby", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    rc = run(["provision-seed", "--provider", "p", "--region", "r",
              "--db-path", str(db), "--config", str(cfg_path)])
    assert rc == 2
    assert "active-only" in capsys.readouterr().err.lower()


def test_provision_seed_refused_no_promoted_image(tmp_path, age_recipient, capsys):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    run(["authority-migrate-placeholder", "--db-path", str(db), "--config", str(cfg_path)])
    # No image promoted, no domain attested, no descriptor signed.
    rc = run(["provision-seed", "--provider", "p", "--region", "r",
              "--db-path", str(db), "--config", str(cfg_path)])
    assert rc == 3
    err = capsys.readouterr().err.lower()
    assert "image" in err or "promoted" in err
```

- [ ] **Step 2: Verify failure**

```bash
pytest tests/unit/controller/test_cli.py -k 'provision_seed' -v
```

- [ ] **Step 3: Add subparser + dispatch + handler in `src/mthydra/controller/cli.py`**

Top-level import (next to spec-D imports):

```python
from mthydra.controller.provisioning.seed import ProvisionError, provision_box
```

Subparser:

```python
    ps = sub.add_parser("provision-seed",
                         help="atomic provisioning: claim cover domain + image + credential, emit seed")
    ps.add_argument("--provider", required=True)
    ps.add_argument("--region", required=True)
    ps.add_argument("--format", choices=["cloud-init", "json"], default="cloud-init")
    ps.add_argument("--ttl-seconds", type=int, default=3600)
    ps.add_argument("--db-path", default=DEFAULT_DB)
    ps.add_argument("--config", default="/etc/mthydra/controller.toml")
```

Dispatch:

```python
    if args.cmd == "provision-seed":
        return _cmd_provision_seed(args)
```

Handler:

```python
def _cmd_provision_seed(args) -> int:
    from mthydra.controller.config import ConfigError, load_config
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.tokens import get_provider_credential

    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"provision-seed: config error: {e}", file=sys.stderr)
        return 2

    conn = connect(args.db_path)
    try:
        rc = _require_active_role(conn, "provision-seed")
        if rc is not None:
            return rc
        try:
            secret = get_provider_credential(conn, "b2")
        except KeyError:
            print("provision-seed: b2 provider credential not in DB", file=sys.stderr)
            return 7
        dest = _build_destination(cfg, secret, mode="production",
                                   bucket_override=args.bucket_override)
        try:
            seed = provision_box(
                conn=conn, b2_destination=dest,
                provider=args.provider, region=args.region,
                image_signed_url_ttl_seconds=args.ttl_seconds,
                now=_now(),
            )
        except ProvisionError as e:
            print(f"provision-seed: {e}", file=sys.stderr)
            return 3
        except Exception as e:
            print(f"provision-seed: B2 URL minting failed: {e}", file=sys.stderr)
            return 5

        if args.format == "json":
            print(seed.to_json_pretty().decode("utf-8"))
        else:
            print(seed.to_cloud_init().decode("utf-8"))
        return 0
    finally:
        conn.close()
```

- [ ] **Step 4: Run tests + full suite**

```bash
pytest tests/unit/controller/test_cli.py -k 'provision_seed' -v
pytest tests/unit -q
```

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/cli.py tests/unit/controller/test_cli.py
git commit -m "cli(G): provision-seed — atomic provisioning emits cloud-init or JSON seed"
```

---

### Task 8: `ru-box-list` / `ru-box-mark-live` / `ru-box-terminate`

**Files:**
- Modify: `src/mthydra/controller/cli.py`
- Modify: `tests/unit/controller/test_cli.py`

- [ ] **Step 1: Append failing tests**

```python
def test_ru_box_list_empty_default(tmp_path, age_recipient, capsys):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    run(["init", "--db-path", str(db),
         "--age-recipient", age_recipient,
         "--provider-credential", "b2=id:secret"])
    capsys.readouterr()
    rc = run(["ru-box-list", "--db-path", str(db)])
    assert rc == 0


def test_ru_box_list_json_after_provision(tmp_path, age_recipient, capsys, monkeypatch):
    import json
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    from mthydra.controller.backup.s3_dest import S3Destination
    monkeypatch.setattr(
        S3Destination, "presigned_image_url",
        lambda self, *, image_version, ttl_seconds=3600: (
            f"https://b2.example/{image_version}/mtg?sig=stub",
            "2026-05-21T01:00:00Z",
        ),
    )
    _setup_provision_prereqs(db, age_recipient, cfg_path)
    run(["provision-seed", "--provider", "hetzner", "--region", "fsn1",
         "--db-path", str(db), "--config", str(cfg_path)])
    capsys.readouterr()
    rc = run(["ru-box-list", "--json", "--db-path", str(db)])
    assert rc == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1
    assert rows[0]["state"] == "provisioning"


def test_ru_box_mark_live_happy_path(tmp_path, age_recipient, capsys, monkeypatch):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    from mthydra.controller.backup.s3_dest import S3Destination
    monkeypatch.setattr(
        S3Destination, "presigned_image_url",
        lambda self, *, image_version, ttl_seconds=3600: (
            f"https://b2.example/{image_version}/mtg?sig=stub",
            "2026-05-21T01:00:00Z",
        ),
    )
    _setup_provision_prereqs(db, age_recipient, cfg_path)
    run(["provision-seed", "--provider", "hetzner", "--region", "fsn1",
         "--db-path", str(db), "--config", str(cfg_path)])
    from mthydra.controller.state.db import connect
    conn = connect(db)
    box_id = conn.execute("SELECT box_id FROM ru_boxes LIMIT 1").fetchone()[0]
    conn.close()
    rc = run(["ru-box-mark-live", box_id, "--public-ip", "203.0.113.7",
              "--db-path", str(db)])
    assert rc == 0
    conn = connect(db)
    row = conn.execute("SELECT state, public_ip FROM ru_boxes WHERE box_id=?",
                        (box_id,)).fetchone()
    assert row == ("live", "203.0.113.7")
    conn.close()


def test_ru_box_terminate_burns_sni_and_revokes_credentials(tmp_path, age_recipient, monkeypatch):
    from mthydra.controller.cli import run
    db = tmp_path / "state.sqlite"
    cfg_path = tmp_path / "controller.toml"
    cfg_path.write_text(_MIN_TOML)
    from mthydra.controller.backup.s3_dest import S3Destination
    monkeypatch.setattr(
        S3Destination, "presigned_image_url",
        lambda self, *, image_version, ttl_seconds=3600: (
            f"https://b2.example/{image_version}/mtg?sig=stub",
            "2026-05-21T01:00:00Z",
        ),
    )
    _setup_provision_prereqs(db, age_recipient, cfg_path)
    run(["provision-seed", "--provider", "hetzner", "--region", "fsn1",
         "--db-path", str(db), "--config", str(cfg_path)])
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.burned import is_burned
    conn = connect(db)
    box_id, sni = conn.execute("SELECT box_id, sni FROM ru_boxes LIMIT 1").fetchone()
    conn.close()
    rc = run(["ru-box-terminate", box_id, "--reason", "test",
              "--db-path", str(db)])
    assert rc == 0
    conn = connect(db)
    state = conn.execute(
        "SELECT state FROM ru_boxes WHERE box_id=?", (box_id,)
    ).fetchone()[0]
    assert state == "terminated"
    assert is_burned(conn, sni)
    revoked = conn.execute(
        "SELECT COUNT(*) FROM onward_credentials WHERE box_id=? AND revoked_at IS NULL",
        (box_id,),
    ).fetchone()[0]
    assert revoked == 0
    conn.close()
```

- [ ] **Step 2: Verify failure**

- [ ] **Step 3: Add three subparsers**

```python
    rbl = sub.add_parser("ru-box-list", help="list ru_boxes inventory")
    rbl.add_argument("--state", choices=["provisioning", "live", "terminated"], default=None)
    rbl.add_argument("--db-path", default=DEFAULT_DB)
    rbl.add_argument("--json", action="store_true")

    rbml = sub.add_parser("ru-box-mark-live", help="state: provisioning -> live")
    rbml.add_argument("box_id")
    rbml.add_argument("--public-ip", required=True)
    rbml.add_argument("--db-path", default=DEFAULT_DB)

    rbt = sub.add_parser("ru-box-terminate",
                          help="terminate a box (revokes credentials, burns SNI)")
    rbt.add_argument("box_id")
    rbt.add_argument("--reason", required=True)
    rbt.add_argument("--db-path", default=DEFAULT_DB)
```

- [ ] **Step 4: Add three dispatch lines**

```python
    if args.cmd == "ru-box-list":
        return _cmd_ru_box_list(args)
    if args.cmd == "ru-box-mark-live":
        return _cmd_ru_box_mark_live(args)
    if args.cmd == "ru-box-terminate":
        return _cmd_ru_box_terminate(args)
```

- [ ] **Step 5: Add handlers at bottom of `cli.py`**

```python
def _cmd_ru_box_list(args) -> int:
    import json as _json
    from dataclasses import asdict
    from mthydra.controller.state.db import connect

    conn = connect(args.db_path)
    try:
        rc = _require_active_role(conn, "ru-box-list")
        if rc is not None:
            return rc
        if args.state is None:
            rows = conn.execute(
                "SELECT box_id, provider, region, public_ip, sni, shard_id, state, "
                "image_version, created_at, went_live_at, terminated_at, termination_reason "
                "FROM ru_boxes ORDER BY created_at DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT box_id, provider, region, public_ip, sni, shard_id, state, "
                "image_version, created_at, went_live_at, terminated_at, termination_reason "
                "FROM ru_boxes WHERE state=? ORDER BY created_at DESC", (args.state,)
            ).fetchall()
        cols = ("box_id", "provider", "region", "public_ip", "sni", "shard_id",
                "state", "image_version", "created_at", "went_live_at",
                "terminated_at", "termination_reason")
        out = [dict(zip(cols, r)) for r in rows]
        if args.json:
            print(_json.dumps(out, indent=2))
        else:
            print(f"{'state':14} {'box_id':38} {'sni':40} created_at")
            for r in out:
                print(f"{r['state']:14} {r['box_id']:38} {r['sni']:40} "
                      f"{r['created_at']}")
        return 0
    finally:
        conn.close()


def _cmd_ru_box_mark_live(args) -> int:
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_boxes import mark_live

    conn = connect(args.db_path)
    try:
        rc = _require_active_role(conn, "ru-box-mark-live")
        if rc is not None:
            return rc
        row = conn.execute(
            "SELECT 1 FROM ru_boxes WHERE box_id=?", (args.box_id,)
        ).fetchone()
        if row is None:
            print(f"ru-box-mark-live: box {args.box_id!r} not found", file=sys.stderr)
            return 2
        try:
            mark_live(conn, args.box_id, public_ip=args.public_ip, at=_now())
        except ValueError as e:
            print(f"ru-box-mark-live: {e}", file=sys.stderr)
            return 2
        from mthydra.controller.state.audit import log_event
        log_event(
            conn, ts=_now(), actor="operator", action="ru_box_live",
            target=args.box_id, details_json=None,
        )
        print(f"ru-box-mark-live: {args.box_id} -> live (public_ip={args.public_ip})")
        return 0
    finally:
        conn.close()


def _cmd_ru_box_terminate(args) -> int:
    from mthydra.controller.state.audit import log_event
    from mthydra.controller.state.burned import mark_burned
    from mthydra.controller.state.credentials import active_for_box, revoke_credential
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_boxes import mark_terminated

    conn = connect(args.db_path)
    try:
        rc = _require_active_role(conn, "ru-box-terminate")
        if rc is not None:
            return rc
        row = conn.execute(
            "SELECT sni, state FROM ru_boxes WHERE box_id=?", (args.box_id,)
        ).fetchone()
        if row is None:
            print(f"ru-box-terminate: box {args.box_id!r} not found", file=sys.stderr)
            return 2
        sni, prior_state = row
        if prior_state == "terminated":
            print(f"ru-box-terminate: box {args.box_id!r} already terminated",
                  file=sys.stderr)
            return 2
        now = _now()
        try:
            conn.execute("BEGIN")
            # Revoke credentials.
            for c in active_for_box(conn, args.box_id):
                revoke_credential(conn, c.cred_id, at=now)
            # Burn the SNI (cover_pool -> burned_domains, atomic).
            mark_burned(conn, sni, args.reason, args.box_id, now, None)
            # Mark box terminated.
            mark_terminated(conn, args.box_id, reason=args.reason, at=now)
            log_event(
                conn, ts=now, actor="operator", action="ru_box_terminated",
                target=args.box_id,
                details_json='{"reason":' + repr(args.reason) + ',"prior_state":' + repr(prior_state) + '}',
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        print(f"ru-box-terminate: {args.box_id} -> terminated; sni {sni!r} burned")
        return 0
    finally:
        conn.close()
```

Note the `details_json` in `_cmd_ru_box_terminate` uses string concatenation instead of `json.dumps` because the reason text may contain quotes; if you'd rather use `json.dumps`, that's also fine — adjust accordingly.

- [ ] **Step 6: Add bootstrap obligation `g_provision_drill_proven`**

In the `if args.cmd == "init":` active-role obligation dict, append:

```python
                    "g_provision_drill_proven":  90 * 24,
```

- [ ] **Step 7: Run all tests**

```bash
pytest tests/unit/controller/test_cli.py -k 'ru_box or provision_seed' -v
pytest tests/unit -q
```

- [ ] **Step 8: Commit**

```bash
git add src/mthydra/controller/cli.py tests/unit/controller/test_cli.py
git commit -m "cli(G): ru-box-list/mark-live/terminate + bootstrap g_provision_drill_proven"
```

---

## Phase 7 — Integration

### Task 9: Provisioning lifecycle integration test

**Files:**
- Create: `tests/integration/test_provisioning_lifecycle.py`

- [ ] **Step 1: Write the integration test**

```python
"""Spec G — end-to-end provisioning lifecycle.

init → authority-migrate-placeholder → image-build (mocked) → cover-add +
cover-attest-verified → descriptor-sign-now → provision-seed → verify the
seed bundle via the pure-Python verifier → ru-box-mark-live →
ru-box-terminate → confirm SNI is burned + credentials revoked.
"""
import base64
import json
import shutil
import subprocess
from unittest.mock import MagicMock

import pytest

from mthydra.controller.bootstrap import init_state
from mthydra.controller.image.builder import build_image
from mthydra.controller.provisioning.seed import provision_box
from mthydra.controller.state.audit import recent_events
from mthydra.controller.state.authority import current_authority
from mthydra.controller.state.burned import is_burned
from mthydra.controller.state.cover_pool import add_candidate, attest_verified
from mthydra.controller.state.credentials import active_for_box
from mthydra.controller.state.db import connect
from mthydra.controller.state.ru_boxes import mark_live, mark_terminated
from mthydra.descriptor.authority import (
    generate_authority_keypair, verify_onward_credential,
)


@pytest.fixture
def recipient_fixture(tmp_path):
    if shutil.which("age-keygen") is None:
        pytest.skip("age-keygen not installed")
    keyfile = tmp_path / "identity"
    subprocess.run(["age-keygen", "-o", str(keyfile)], capture_output=True, check=True)
    for line in keyfile.read_text().splitlines():
        if line.startswith("# public key: "):
            return line.removeprefix("# public key: ").strip()
    raise RuntimeError("no public key line")


def test_provisioning_lifecycle_end_to_end(tmp_path, recipient_fixture):
    NOW = "2026-05-21T00:00:00Z"
    db = tmp_path / "state.sqlite"

    # 1. init DB with placeholder authority.
    init_state(
        db_path=db,
        age_recipient=recipient_fixture,
        provider_credentials={"b2": "id:secret"},
        obligation_timer_hours={"backup_restore_dryrun": 720},
        now=NOW,
        role="active",
    )

    # 2. Migrate authority to real Ed25519 (mimics CLI authority-migrate-placeholder).
    conn = connect(db)
    priv, pub = generate_authority_keypair()
    conn.execute(
        "UPDATE credential_authority SET privkey_pem=?, pubkey_pem=? "
        "WHERE retired_at IS NULL", (priv, pub),
    )
    conn.commit()
    conn.close()

    # 3. Sign a descriptor (must come before provision; uses spec-B helpers).
    from mthydra.descriptor.sign import sign_new_descriptor
    conn = connect(db)
    sign_new_descriptor(conn, now_iso=NOW, valid_until_iso="2026-05-22T00:00:00Z")
    conn.close()

    # 4. Build + promote an image (mocked HTTP + mocked B2).
    import hashlib
    asset_bytes = b"binary-bytes" * 100
    sha = hashlib.sha256(asset_bytes).hexdigest()
    checksum_text = f"{sha}  mtg-linux-amd64\n"
    release_json = {
        "tag_name": "v2.1.7",
        "assets": [
            {"name": "mtg-linux-amd64", "browser_download_url": "https://x/mtg-linux-amd64"},
            {"name": "SHA256SUMS", "browser_download_url": "https://x/SHA256SUMS"},
        ],
    }
    def _mock_http(url):
        resp = MagicMock()
        resp.status = 200
        if url.endswith("/releases/tags/v2.1.7"):
            resp.read.return_value = json.dumps(release_json).encode()
        elif url.endswith("/mtg-linux-amd64"):
            resp.read.return_value = asset_bytes
        elif url.endswith("/SHA256SUMS"):
            resp.read.return_value = checksum_text.encode()
        return resp
    b2 = MagicMock()
    b2.presigned_image_url.return_value = (
        f"https://b2.example/{sha}/mtg?sig=stub", "2026-05-21T01:00:00Z",
    )
    conn = connect(db)
    build_image(
        conn=conn, b2_destination=b2,
        upstream_repo="9seconds/mtg",
        upstream_release="v2.1.7",
        asset_filename="mtg-linux-amd64",
        github_api_url="https://api.github.com",
        tmp_dir=tmp_path,
        now=NOW,
        http_client=_mock_http,
    )
    from mthydra.controller.state.ru_images import promote
    promote(conn, sha, at=NOW, evidence="smoke")
    conn.close()

    # 5. Attest a cover domain.
    conn = connect(db)
    add_candidate(conn, "example.cover", added_at=NOW)
    attest_verified(conn, "example.cover", from_vantage="ru-vps-01", at=NOW)
    conn.close()

    # 6. provision_box.
    conn = connect(db)
    seed = provision_box(
        conn=conn, b2_destination=b2,
        provider="hetzner", region="fsn1",
        image_signed_url_ttl_seconds=3600,
        now=NOW,
    )
    conn.close()

    # 7. Verify the embedded onward credential via the pure-Python verifier.
    cred = base64.b64decode(seed.onward_credential_b64)
    verified = verify_onward_credential(cred, seed.authority_pubkey_pem)
    assert verified.box_id == seed.box_id
    assert verified.authority_generation == 1

    # 8. Verify the embedded initial descriptor.
    import struct
    desc_blob = base64.b64decode(seed.initial_descriptor_b64)
    assert len(desc_blob) >= 2 + 64
    n = struct.unpack(">H", desc_blob[:2])[0]
    desc_json = json.loads(desc_blob[2:2 + n])
    assert desc_json["generation"] == 1

    # 9. mark_live + terminate.
    conn = connect(db)
    mark_live(conn, seed.box_id, public_ip="203.0.113.7", at=NOW)
    conn.close()

    conn = connect(db)
    from mthydra.controller.state.credentials import revoke_credential
    from mthydra.controller.state.burned import mark_burned
    for c in active_for_box(conn, seed.box_id):
        revoke_credential(conn, c.cred_id, at=NOW)
    mark_burned(conn, seed.sni, "test", seed.box_id, NOW, None)
    mark_terminated(conn, seed.box_id, reason="test", at=NOW)
    assert is_burned(conn, seed.sni)
    remaining = active_for_box(conn, seed.box_id)
    assert remaining == []
    conn.close()

    # 10. Audit log has the full sequence.
    conn = connect(db)
    actions = {e.action for e in recent_events(conn, limit=50)}
    assert "box_provisioned" in actions
    assert "image_built" in actions
    assert "image_promoted" in actions
    conn.close()
```

- [ ] **Step 2: Run the integration test**

```bash
cd /home/asharov/RedHat/Dev/mthydra
pytest tests/integration/test_provisioning_lifecycle.py -v
```

Expected: PASS.

- [ ] **Step 3: Run all integration tests** for regression

```bash
pytest tests/integration -q --ignore=tests/integration/test_gap_monitor.py
```

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_provisioning_lifecycle.py
git commit -m "test(G): end-to-end provisioning lifecycle — provision, verify credential, terminate"
```

---

## Phase 8 — Final verification

### Task 10: Full pytest + coverage + smoke

- [ ] **Step 1: Full suite**

```bash
cd /home/asharov/RedHat/Dev/mthydra
pytest -q --ignore=tests/integration/test_gap_monitor.py
```

Expected: green; ~30 new tests on top of the spec-D baseline (358).

- [ ] **Step 2: Coverage on new modules**

```bash
pytest --cov=mthydra.descriptor.authority \
       --cov=mthydra.controller.provisioning.seed \
       --cov-report=term tests/ \
       --ignore=tests/integration/test_gap_monitor.py
```

Expected: ≥ 90% on `authority`, ≥ 90% on `provisioning.seed` (the modules are tightly tested by both unit and integration paths).

- [ ] **Step 3: CLI end-to-end smoke**

```bash
TMP=$(mktemp -d)
age-keygen -o "$TMP/id" 2>/dev/null
PUB=$(grep '# public key:' "$TMP/id" | awk '{print $4}')
.venv/bin/mthydra-controller init \
  --db-path "$TMP/state.sqlite" \
  --age-recipient "$PUB" \
  --provider-credential "b2=id:secret"

cp packaging/etc/mthydra/controller.toml.example "$TMP/controller.toml"

echo "=== authority-migrate-placeholder ==="
.venv/bin/mthydra-controller authority-migrate-placeholder \
  --db-path "$TMP/state.sqlite" --config "$TMP/controller.toml"

echo "=== ru-box-list (empty) ==="
.venv/bin/mthydra-controller ru-box-list --db-path "$TMP/state.sqlite"

# Don't run provision-seed in smoke (needs descriptor + image + cover all in place).
# The integration test covers that path.

rm -rf "$TMP"
echo "smoke ok"
```

Expected: each command exits 0; the authority is migrated; `ru-box-list` shows empty.

- [ ] **Step 4: Confirm spec-G commits landed**

```bash
git log --oneline | head -12
```

You should see nine `(G):` commits on top of the spec-D tip.

---

## Done criteria

- All 10 task checkboxes ticked.
- `pytest -q` passes cleanly.
- ≥ 90% coverage on `mthydra.descriptor.authority`, ≥ 90% on `mthydra.controller.provisioning.seed`.
- All 5 new CLI subcommands work; `authority-rotate` produces real Ed25519 keys.
- Startup invariants #26–#28 catch placeholder authority (production mode), live-box-without-credential, and SNI collisions.
- The integration test demonstrates: real Ed25519 authority + signed onward credential + embedded descriptor all verify end-to-end via the pure-Python (RU-embeddable) verifier.
- AST-walk test confirms `mthydra.descriptor.authority` has zero `mthydra.controller.*` imports — F2 can adopt it verbatim.
