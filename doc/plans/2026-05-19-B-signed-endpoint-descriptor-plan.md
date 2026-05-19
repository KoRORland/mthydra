# Spec B — Signed Endpoint Descriptor — Implementation Plan

**Goal:** Implement Ed25519-signed endpoint descriptors as defined in `doc/specs/2026-05-19-B-signed-endpoint-descriptor.md`.

**Architecture:** New `mthydra.descriptor` package (`keys`, `payload`, `sign`, `verify`, `scheduler`). New `eu_exit_set` repository in `controller/state/`. Schema v1→v2 migration. Six new CLI subcommands plus a one-shot migration command. Pure-Python verifier with zero `mthydra.controller` imports — RU-embeddable by spec E.

**Tech stack additions:**
- `cryptography>=41` — explicitly pinned (was transitive via botocore; spec B makes it a direct dep).
- No other new runtime deps.

**Design decisions:** See spec §2 (B-D1 through B-D11).

---

## File structure (locked)

```
src/mthydra/
├── descriptor/
│   ├── __init__.py
│   ├── keys.py
│   ├── payload.py
│   ├── sign.py
│   ├── verify.py
│   └── scheduler.py
├── controller/
│   └── state/
│       └── eu_exit_set.py
tests/
├── unit/
│   └── descriptor/
│       ├── __init__.py
│       ├── test_keys.py
│       ├── test_payload.py
│       ├── test_sign.py
│       ├── test_verify.py
│       └── test_verify_import_isolation.py
├── unit/controller/state/
│   └── test_eu_exit_set.py
├── integration/
│   └── test_descriptor_roundtrip.py
└── property/
    └── test_descriptor_chain_integrity.py
```

Modified files:
- `src/mthydra/controller/state/schema.py` — SCHEMA_VERSION 1→2, eu_exit_set DDL, migrate_v1_to_v2.
- `src/mthydra/controller/state/descriptor.py` — add signature column awareness.
- `src/mthydra/controller/state/invariants.py` — checks 13–16.
- `src/mthydra/controller/startup.py` — wire new invariants.
- `src/mthydra/controller/bootstrap.py` — replace placeholder with real Ed25519.
- `src/mthydra/controller/cli.py` — seven new subcommands.
- `packaging/etc/mthydra/controller.toml.example` — `[descriptor]` section + `descriptor_signing_key_rotation` obligation.
- `pyproject.toml` — explicit `cryptography>=41` dep.

---

## Phase 1 — Foundation (Tasks 1–3)

### Task 1: `state/eu_exit_set.py` + schema v2 migration

**Files:**
- Modify `src/mthydra/controller/state/schema.py`
- Create `src/mthydra/controller/state/eu_exit_set.py`
- Modify tests: `tests/unit/controller/state/test_eu_exit_set.py` (new)
- Modify `tests/unit/controller/state/test_schema.py`

- [ ] **Step 1: Update `schema.py`**

Bump `SCHEMA_VERSION = 2`. Add to `_STATEMENTS`:

```python
"""
CREATE TABLE IF NOT EXISTS eu_exit_set (
  fingerprint  TEXT PRIMARY KEY,
  endpoint     TEXT NOT NULL,
  weight       INTEGER NOT NULL DEFAULT 1,
  added_at     TEXT NOT NULL,
  retired_at   TEXT
)
""",
"""
ALTER TABLE descriptor_history ADD COLUMN signature BLOB NOT NULL DEFAULT X''
""",
```

Add migration function:

```python
def migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """Idempotent v1 → v2 migration: add eu_exit_set and descriptor_history.signature."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS eu_exit_set (
          fingerprint  TEXT PRIMARY KEY,
          endpoint     TEXT NOT NULL,
          weight       INTEGER NOT NULL DEFAULT 1,
          added_at     TEXT NOT NULL,
          retired_at   TEXT
        )
    """)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(descriptor_history)").fetchall()]
    if "signature" not in cols:
        conn.execute("ALTER TABLE descriptor_history ADD COLUMN signature BLOB NOT NULL DEFAULT X''")
    conn.execute("UPDATE schema_version SET version=2, applied_at=? WHERE rowid=1", (_now(),))
    conn.commit()
```

Update `apply_schema` to detect v1 and call `migrate_v1_to_v2`:

```python
def apply_schema(conn: sqlite3.Connection) -> None:
    for stmt in _STATEMENTS:
        try:
            conn.execute(stmt)
        except Exception:
            pass  # ALTER TABLE fails silently if column already exists
    existing = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
    if existing == 0:
        conn.execute(
            "INSERT INTO schema_version (rowid, version, applied_at) VALUES (1, ?, ?)",
            (SCHEMA_VERSION, _now()),
        )
    else:
        current = conn.execute("SELECT version FROM schema_version WHERE rowid=1").fetchone()[0]
        if current < 2:
            migrate_v1_to_v2(conn)
    conn.commit()
```

- [ ] **Step 2: Write `eu_exit_set.py`**

```python
"""EU exit-set repository — consumed by spec B (descriptor signing) and spec F (node setup)."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class EUExitRow:
    fingerprint: str
    endpoint: str
    weight: int
    added_at: str
    retired_at: str | None


def add_exit(conn: sqlite3.Connection, fingerprint: str, endpoint: str,
             weight: int, added_at: str) -> None:
    conn.execute(
        "INSERT INTO eu_exit_set (fingerprint, endpoint, weight, added_at) VALUES (?,?,?,?)",
        (fingerprint, endpoint, weight, added_at),
    )
    conn.commit()


def retire_exit(conn: sqlite3.Connection, fingerprint: str, *, at: str) -> None:
    cur = conn.execute(
        "UPDATE eu_exit_set SET retired_at=? WHERE fingerprint=? AND retired_at IS NULL",
        (at, fingerprint),
    )
    if cur.rowcount == 0:
        raise ValueError(f"fingerprint {fingerprint!r} not found or already retired")
    conn.commit()


def list_active(conn: sqlite3.Connection) -> list[EUExitRow]:
    rows = conn.execute(
        "SELECT fingerprint, endpoint, weight, added_at, retired_at "
        "FROM eu_exit_set WHERE retired_at IS NULL ORDER BY fingerprint"
    ).fetchall()
    return [EUExitRow(*r) for r in rows]


def list_all(conn: sqlite3.Connection) -> list[EUExitRow]:
    rows = conn.execute(
        "SELECT fingerprint, endpoint, weight, added_at, retired_at "
        "FROM eu_exit_set ORDER BY fingerprint"
    ).fetchall()
    return [EUExitRow(*r) for r in rows]
```

- [ ] **Step 3: Write tests for `eu_exit_set.py`**

Verify: add/list_active/retire/list_all; weight default; duplicate fingerprint raises UNIQUE violation; retire unknown raises ValueError; ordering stability.

- [ ] **Step 4: Update `test_schema.py`**

Add: fresh DB has schema_version=2 and eu_exit_set exists; migration from v1 produces v2 + eu_exit_set + descriptor_history.signature without losing data.

- [ ] **Step 5: Run tests — expect all pass.**

- [ ] **Step 6: Commit `state(B): eu_exit_set table + repository + schema v2 migration`**

---

### Task 2: `descriptor/keys.py` — Ed25519 I/O

**Files:**
- Create `src/mthydra/descriptor/__init__.py`
- Create `src/mthydra/descriptor/keys.py`
- Create `tests/unit/descriptor/__init__.py`
- Create `tests/unit/descriptor/test_keys.py`

- [ ] **Step 1: Write `keys.py`**

```python
"""Ed25519 keypair I/O — thin wrapper around PyCA cryptography."""
from __future__ import annotations

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding, NoEncryption, PrivateFormat, PublicFormat,
)
from cryptography.exceptions import InvalidSignature


PLACEHOLDER_PREFIX = b"PRIV-DESC-"


def generate_keypair() -> tuple[bytes, bytes]:
    """Return (privkey_raw_32, pubkey_raw_32)."""
    priv = Ed25519PrivateKey.generate()
    priv_bytes = priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    pub_bytes = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return priv_bytes, pub_bytes


def public_from_private(priv: bytes) -> bytes:
    return Ed25519PrivateKey.from_private_bytes(priv).public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw
    )


def sign(priv: bytes, msg: bytes) -> bytes:
    """Return 64-byte Ed25519 signature."""
    if len(priv) != 32:
        raise ValueError(f"private key must be 32 bytes, got {len(priv)}")
    return Ed25519PrivateKey.from_private_bytes(priv).sign(msg)


def verify(pub: bytes, msg: bytes, sig: bytes) -> bool:
    """Return True if signature is valid; never raises (returns False on failure)."""
    if len(pub) != 32 or len(sig) != 64:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(pub).verify(sig, msg)
        return True
    except (InvalidSignature, ValueError):
        return False


def is_placeholder(priv: bytes) -> bool:
    """True if privkey bytes look like a spec A placeholder."""
    return priv.startswith(PLACEHOLDER_PREFIX)
```

- [ ] **Step 2: Write `test_keys.py`**

Tests: generate produces 32+32 bytes; public_from_private consistent; sign+verify round-trip; tamper msg → False; tamper sig → False; tamper pub → False; wrong-length inputs → ValueError / False; is_placeholder True for prefixed bytes.

- [ ] **Step 3: Update `pyproject.toml`** — add `cryptography>=41` to `[project.dependencies]`.

- [ ] **Step 4: Run tests.**

- [ ] **Step 5: Commit `descriptor(B): Ed25519 keys.py + tests`**

---

### Task 3: `descriptor/payload.py` — payload model + canonical JSON

**Files:**
- Create `src/mthydra/descriptor/payload.py`
- Create `tests/unit/descriptor/test_payload.py`

- [ ] **Step 1: Write `payload.py`**

```python
"""Descriptor payload dataclass and canonical JSON encoding (spec B §4)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

SCHEMA = "mthydra.descriptor.v1"
_KNOWN_FIELDS = frozenset({
    "schema", "generation", "signing_key_gen", "issued_at", "valid_until",
    "eu_exit_set", "previous_generation_hash", "next_signing_pubkey",
})


@dataclass(frozen=True)
class EUExit:
    fingerprint: str
    endpoint: str
    weight: int


@dataclass(frozen=True)
class DescriptorPayload:
    generation: int
    signing_key_gen: int
    issued_at: str
    valid_until: str
    eu_exit_set: tuple[EUExit, ...]
    previous_generation_hash: str | None
    next_signing_pubkey: str | None

    @classmethod
    def from_canonical_bytes(cls, blob: bytes) -> "DescriptorPayload":
        obj = json.loads(blob.decode("utf-8"))
        unknown = set(obj.keys()) - _KNOWN_FIELDS
        if unknown:
            raise ValueError(f"unknown fields in descriptor payload: {unknown}")
        if obj.get("schema") != SCHEMA:
            raise ValueError(f"schema mismatch: expected {SCHEMA!r}, got {obj.get('schema')!r}")
        exits = tuple(
            EUExit(e["fingerprint"], e["endpoint"], e["weight"])
            for e in obj.get("eu_exit_set", [])
        )
        return cls(
            generation=int(obj["generation"]),
            signing_key_gen=int(obj["signing_key_gen"]),
            issued_at=str(obj["issued_at"]),
            valid_until=str(obj["valid_until"]),
            eu_exit_set=exits,
            previous_generation_hash=obj.get("previous_generation_hash"),
            next_signing_pubkey=obj.get("next_signing_pubkey"),
        )


def canonical_bytes(payload: DescriptorPayload) -> bytes:
    """Deterministic JSON bytes (spec B §4, B-D2).  Floats are prohibited."""
    obj: dict[str, Any] = {
        "schema": SCHEMA,
        "generation": payload.generation,
        "signing_key_gen": payload.signing_key_gen,
        "issued_at": payload.issued_at,
        "valid_until": payload.valid_until,
        "eu_exit_set": [
            {"endpoint": e.endpoint, "fingerprint": e.fingerprint, "weight": e.weight}
            for e in payload.eu_exit_set
        ],
        "previous_generation_hash": payload.previous_generation_hash,
        "next_signing_pubkey": payload.next_signing_pubkey,
    }
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def payload_hash(payload_bytes: bytes) -> str:
    """Hex sha256 of canonical bytes — used for the chain field."""
    return hashlib.sha256(payload_bytes).hexdigest()
```

- [ ] **Step 2: Write `test_payload.py`**

Tests: round-trip (from_canonical_bytes ∘ canonical_bytes == identity); encoding stability (two calls produce identical bytes); keys are sorted in output; unknown field raises ValueError; schema mismatch raises ValueError; generation-1 with null previous_generation_hash round-trips; eu_exit_set ordering preserved; payload_hash is a 64-char hex string.

- [ ] **Step 3: Run tests.**

- [ ] **Step 4: Commit `descriptor(B): payload model + canonical JSON encoding`**

---

## Phase 2 — Sign path (Task 4)

### Task 4: `descriptor/sign.py`

**Files:**
- Create `src/mthydra/descriptor/sign.py`
- Update `src/mthydra/controller/state/descriptor.py` — add insert/query functions for `signature` column.
- Create `tests/unit/descriptor/test_sign.py`

- [ ] **Step 1: Update `state/descriptor.py`**

Add:

```python
def insert_descriptor(
    conn: sqlite3.Connection,
    generation: int,
    payload: str,          # UTF-8 decoded canonical bytes
    signed_at: str,
    valid_until: str,
    signing_key_generation: int,
    signature: bytes,
) -> None:
    conn.execute(
        "INSERT INTO descriptor_history "
        "(generation, payload, signed_at, valid_until, signing_key_generation, signature) "
        "VALUES (?,?,?,?,?,?)",
        (generation, payload, signed_at, valid_until, signing_key_generation, signature),
    )
    conn.commit()


def latest_descriptor(conn: sqlite3.Connection) -> tuple[int, bytes, bytes] | None:
    """Return (generation, payload_bytes, signature) for the highest generation, or None."""
    row = conn.execute(
        "SELECT generation, payload, signature FROM descriptor_history ORDER BY generation DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    gen, payload_text, sig = row
    return gen, payload_text.encode("utf-8"), bytes(sig)
```

- [ ] **Step 2: Write `sign.py`**

```python
"""Controller-side descriptor signing (spec B §6)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from mthydra.controller.state.audit import log_event
from mthydra.controller.state.db import connect
from mthydra.controller.state.descriptor import insert_descriptor, latest_descriptor
from mthydra.controller.state.eu_exit_set import list_active
from mthydra.descriptor.keys import is_placeholder, sign as ed_sign
from mthydra.descriptor.payload import (
    DescriptorPayload, EUExit, canonical_bytes, payload_hash,
)
from mthydra.controller.state.authority import (  # reuse for signing key query
    # NOTE: descriptor_signing_key is different from credential_authority
    # We query directly below.
)
import sqlite3


class SignError(RuntimeError):
    pass


def _active_signing_key(conn: sqlite3.Connection) -> tuple[int, bytes, bytes]:
    """Return (generation, privkey, pubkey) for the active descriptor signing key."""
    row = conn.execute(
        "SELECT generation, privkey, pubkey FROM descriptor_signing_key "
        "WHERE retired_at IS NULL ORDER BY generation DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise SignError("no active descriptor_signing_key in DB")
    gen, priv, pub = row
    priv_bytes = bytes(priv)
    pub_bytes = bytes(pub)
    if is_placeholder(priv_bytes):
        raise SignError(
            "active descriptor_signing_key is a spec A placeholder; "
            "run: mthydra-controller descriptor-migrate-placeholder"
        )
    return gen, priv_bytes, pub_bytes


def _next_generation(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(generation), 0) FROM descriptor_history").fetchone()
    return int(row[0]) + 1


def sign_new_descriptor(
    conn: sqlite3.Connection,
    *,
    now_iso: str,
    valid_until_iso: str,
    next_signing_pubkey_hex: str | None = None,
) -> tuple[int, bytes, bytes]:
    """Assemble payload from DB, sign, persist. Returns (generation, payload_bytes, signature)."""
    key_gen, priv, _pub = _active_signing_key(conn)
    prev = latest_descriptor(conn)
    prev_hash: str | None = None
    if prev is not None:
        prev_hash = payload_hash(prev[1])

    exits_raw = list_active(conn)
    exits = tuple(EUExit(e.fingerprint, e.endpoint, e.weight) for e in exits_raw)
    gen = _next_generation(conn)

    payload = DescriptorPayload(
        generation=gen,
        signing_key_gen=key_gen,
        issued_at=now_iso,
        valid_until=valid_until_iso,
        eu_exit_set=exits,
        previous_generation_hash=prev_hash,
        next_signing_pubkey=next_signing_pubkey_hex,
    )
    blob = canonical_bytes(payload)
    sig = ed_sign(priv, blob)

    insert_descriptor(
        conn,
        generation=gen,
        payload=blob.decode("utf-8"),
        signed_at=now_iso,
        valid_until=valid_until_iso,
        signing_key_generation=key_gen,
        signature=sig,
    )
    log_event(conn, ts=now_iso, actor="controller", action="descriptor_signed",
              target=str(gen), details_json=None)
    return gen, blob, sig
```

- [ ] **Step 3: Write `test_sign.py`**

Tests: sign with seeded DB (real Ed25519 key) produces generation=1, valid signature; second sign produces generation=2 with correct previous_generation_hash; sign with placeholder key raises SignError; sign with empty eu_exit_set succeeds; generation increments monotonically.

- [ ] **Step 4: Run tests.**

- [ ] **Step 5: Commit `descriptor(B): sign path + descriptor_history.signature column`**

---

## Phase 3 — Verify path (Tasks 5–6)

### Task 5: `descriptor/verify.py` — pure verifier

**Files:**
- Create `src/mthydra/descriptor/verify.py`
- Create `tests/unit/descriptor/test_verify.py`
- Create `tests/unit/descriptor/test_verify_import_isolation.py`

- [ ] **Step 1: Write `verify.py`**

```python
"""Pure-Python Ed25519 descriptor verifier — RU-callable (spec B §7, B-D6).

ZERO imports from mthydra.controller.  Spec E copies this module into RU images.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Sequence

from mthydra.descriptor.keys import verify as ed_verify
from mthydra.descriptor.payload import DescriptorPayload, canonical_bytes, payload_hash


class VerifyError(ValueError):
    pass


@dataclass(frozen=True)
class TrustedKey:
    generation: int
    pubkey: bytes       # 32 raw bytes


def verify_descriptor(
    blob: bytes,
    signature: bytes,
    trusted_keys: Sequence[TrustedKey],
    now_iso: str,
    *,
    previous_descriptor_hash: str | None = None,
    grace_hours: int = 24,
) -> DescriptorPayload:
    """Verify a signed descriptor. Returns parsed payload on success. Raises VerifyError on any failure."""
    # 1. Parse + structural validation (schema, unknown fields)
    try:
        payload = DescriptorPayload.from_canonical_bytes(blob)
    except (ValueError, KeyError, TypeError) as e:
        raise VerifyError(f"payload parse error: {e}") from e

    # 2. Re-serialise to canonical and verify the blob IS canonical
    #    (protects against injection of non-canonical JSON that happens to parse)
    if canonical_bytes(payload) != blob:
        raise VerifyError("payload bytes are not in canonical form")

    # 3. Find the trusted key for this descriptor's signing_key_gen
    key_map = {tk.generation: tk.pubkey for tk in trusted_keys}
    pub = key_map.get(payload.signing_key_gen)
    if pub is None:
        raise VerifyError(
            f"signing_key_gen={payload.signing_key_gen} not in trusted key set "
            f"(trusted generations: {sorted(key_map)})"
        )

    # 4. Signature verification
    if not ed_verify(pub, blob, signature):
        raise VerifyError("Ed25519 signature verification failed")

    # 5. Expiry check
    now_dt = datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
    valid_until_dt = datetime.fromisoformat(payload.valid_until.replace("Z", "+00:00"))
    if now_dt > valid_until_dt + timedelta(hours=grace_hours):
        raise VerifyError(
            f"descriptor expired: valid_until={payload.valid_until}, "
            f"now={now_iso}, grace={grace_hours}h"
        )

    # 6. Chain verification
    if previous_descriptor_hash is not None:
        if payload.previous_generation_hash != previous_descriptor_hash:
            raise VerifyError(
                f"chain break: payload.previous_generation_hash={payload.previous_generation_hash!r} "
                f"does not match expected={previous_descriptor_hash!r}"
            )
    else:
        # Cold start: no known previous hash
        if payload.generation > 1 and payload.previous_generation_hash is None:
            raise VerifyError(
                "generation > 1 but previous_generation_hash is null (missing chain link)"
            )
        # If generation==1 and previous_generation_hash is None: OK (genesis)
        # If generation>1 and previous_generation_hash is not None and no prior given:
        #   we cannot verify the chain link — but we MUST NOT accept silently.
        #   This is the TOFU-rollback defence: if caller provides no prior, we only
        #   accept genesis (generation==1, previous_generation_hash==null).
        if payload.generation > 1 and payload.previous_generation_hash is not None:
            raise VerifyError(
                "cannot verify chain: descriptor has generation>1 and a previous hash, "
                "but caller provided no previous_descriptor_hash (TOFU-rollback defence)"
            )

    return payload
```

- [ ] **Step 2: Write `test_verify.py`**

Tests (each condition is a separate test):
1. Happy path (generation 1, no chain).
2. Happy path (generation 2, chain provided).
3. Tamper payload byte → VerifyError.
4. Non-canonical payload (extra whitespace) → VerifyError.
5. Tamper signature → VerifyError.
6. Wrong pubkey → VerifyError.
7. signing_key_gen not in trusted_keys → VerifyError.
8. Expired beyond grace → VerifyError.
9. Expired but within grace → passes.
10. Chain mismatch → VerifyError.
11. Cold-start genesis (gen=1, prev_hash=None, no prior given) → passes.
12. Cold-start TOFU defence (gen=2, prev in payload, no prior given) → VerifyError.
13. Schema mismatch → VerifyError.
14. Unknown field in payload → VerifyError.
15. Multi-key trust: signing_key_gen=2, trusted=[gen=1, gen=2] → passes.

- [ ] **Step 3: Write `test_verify_import_isolation.py`**

```python
import ast, pathlib

def test_verify_has_no_controller_imports():
    src = (pathlib.Path(__file__).parent.parent.parent.parent.parent /
           "src/mthydra/descriptor/verify.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in getattr(node, "names", [])]
            module = getattr(node, "module", "") or ""
            full = module + " ".join(names)
            assert "controller" not in full, \
                f"verify.py must not import from mthydra.controller, found: {full!r}"
```

- [ ] **Step 4: Run tests.**

- [ ] **Step 5: Commit `descriptor(B): verify path — pure-Python, no controller imports`**

---

### Task 6: Chain helper + multi-key trust

**Files:**
- Add `verify_chain` to `src/mthydra/descriptor/verify.py`
- Extend `tests/unit/descriptor/test_verify.py`

- [ ] **Step 1: Add `verify_chain`**

```python
def verify_chain(
    descriptors: Sequence[tuple[bytes, bytes]],   # [(payload_blob, sig), ...] in generation order
    trusted_keys: Sequence[TrustedKey],
    now_iso: str,
) -> list[DescriptorPayload]:
    """Verify a chain of descriptors in order.

    Each descriptor's previous_generation_hash must match the sha256 of the previous blob.
    Returns list of parsed payloads.  Raises VerifyError on first failure.
    """
    results: list[DescriptorPayload] = []
    prev_hash: str | None = None
    for blob, sig in descriptors:
        p = verify_descriptor(
            blob, sig, trusted_keys, now_iso,
            previous_descriptor_hash=prev_hash,
        )
        prev_hash = payload_hash(blob)
        results.append(p)
    return results
```

- [ ] **Step 2: Add chain tests**

- Chain of 3: all pass.
- Chain with one tampered blob in the middle: VerifyError at position 2.
- Chain out of order: VerifyError.
- Multi-key rotation: chain where gen 1 signed with key-gen 1, gen 2 signed with key-gen 2. Trusted keys = [key1, key2]. Passes.

- [ ] **Step 3: Run tests.**

- [ ] **Step 4: Commit `descriptor(B): chain + multi-key trust verifier`**

---

## Phase 4 — Bootstrap & placeholders (Task 7)

### Task 7: Replace `_placeholder_keypair_bytes` in `bootstrap.py`

**Files:**
- Modify `src/mthydra/controller/bootstrap.py`
- Update `tests/unit/controller/test_bootstrap.py`

- [ ] **Step 1: Replace the placeholder function**

```python
# Remove _placeholder_keypair_bytes entirely.
# Replace call site in init_state with:
from mthydra.descriptor.keys import generate_keypair
dpriv, dpub = generate_keypair()
```

- [ ] **Step 2: Update `test_bootstrap.py`**

Add assertion: `current_signing_key(conn).pubkey` is exactly 32 bytes; `is_placeholder(current_signing_key(conn).privkey)` is False.

- [ ] **Step 3: Seed `descriptor_signing_key_rotation` obligation in `init_state`**

Add to the `obligation_timer_hours` dict inside `init_state` (or have the caller pass it — the CLI already passes a hardcoded dict; update that dict in `cli.py`).

- [ ] **Step 4: Update `controller.toml.example`**

Add:
```toml
[descriptor]
rotation_interval_hours = 1
validity_window_hours   = 24

[obligations.timers_hours]
# ...existing...
descriptor_signing_key_rotation = 8760
```

- [ ] **Step 5: Run full suite.**

- [ ] **Step 6: Commit `bootstrap(B): replace placeholder with real Ed25519`**

---

## Phase 5 — Rotation scheduler (Task 8)

### Task 8: `descriptor/scheduler.py` + `serve` integration

**Files:**
- Create `src/mthydra/descriptor/scheduler.py`
- Modify `src/mthydra/controller/cli.py` (`_cmd_serve`)

- [ ] **Step 1: Write `scheduler.py`**

```python
"""Routine descriptor rotation via APScheduler (spec B §8 R1)."""
from __future__ import annotations

import threading
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.triggers.interval import IntervalTrigger


class DescriptorRotator:
    def __init__(self, db_path, rotation_interval_seconds: float,
                 validity_window_seconds: float, clock=None,
                 timer_factory=None) -> None:
        self.db_path = db_path
        self.rotation_interval_seconds = rotation_interval_seconds
        self.validity_window_seconds = validity_window_seconds
        self._clock = clock
        self._scheduler: BackgroundScheduler | None = None

    def arm(self) -> None:
        executors = {"default": ThreadPoolExecutor(max_workers=1)}
        self._scheduler = BackgroundScheduler(executors=executors, daemon=True)
        self._scheduler.add_job(
            self._rotate,
            trigger=IntervalTrigger(seconds=self.rotation_interval_seconds),
        )
        self._scheduler.start()

    def disarm(self) -> None:
        if self._scheduler:
            self._scheduler.shutdown(wait=False)
            self._scheduler = None

    def sign_now(self) -> int:
        """Force an immediate sign; return the new generation number."""
        return self._rotate()

    def _rotate(self) -> int:
        from datetime import datetime, timedelta, timezone
        from mthydra.controller.state.db import connect
        from mthydra.descriptor.sign import sign_new_descriptor

        def now_fn():
            if self._clock:
                return self._clock()
            return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        now = now_fn()
        now_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
        valid_until = (now_dt + timedelta(seconds=self.validity_window_seconds)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        conn = connect(self.db_path)
        try:
            gen, _, _ = sign_new_descriptor(conn, now_iso=now, valid_until_iso=valid_until)
            return gen
        except Exception:
            pass  # log via audit_log inside sign_new_descriptor; loop continues
        finally:
            conn.close()
        return -1
```

- [ ] **Step 2: Wire into `_cmd_serve`**

After arming `BackupOrchestrator`, also arm a `DescriptorRotator` (if mode != "offline"):

```python
from mthydra.descriptor.scheduler import DescriptorRotator
rotator = DescriptorRotator(
    db_path=args.db_path,
    rotation_interval_seconds=cfg.descriptor.rotation_interval_hours * 3600,
    validity_window_seconds=cfg.descriptor.validity_window_hours * 3600,
)
if args.mode != "offline":
    rotator.arm()
```

Add `descriptor` section to `Config` dataclass in `config.py`.

- [ ] **Step 3: Update `config.py`**

```python
@dataclass(frozen=True)
class DescriptorConfig:
    rotation_interval_hours: int
    validity_window_hours: int

# In Config:
descriptor: DescriptorConfig

# In load_config:
desc = raw.get("descriptor", {})
descriptor=DescriptorConfig(
    rotation_interval_hours=int(desc.get("rotation_interval_hours", 1)),
    validity_window_hours=int(desc.get("validity_window_hours", 24)),
),
```

- [ ] **Step 4: Tests for `DescriptorRotator`**

Add `tests/unit/descriptor/test_scheduler.py` — arm + trigger via short interval (0.1s), assert a descriptor row appeared in the DB; disarm; offline mode no-op.

- [ ] **Step 5: Run full suite.**

- [ ] **Step 6: Commit `descriptor(B): rotation scheduler + serve integration`**

---

## Phase 6 — CLI surface (Tasks 9–11)

### Task 9: `descriptor-sign-now`, `descriptor-show`, `descriptor-verify`

**Files:** Modify `src/mthydra/controller/cli.py`

- [ ] **Step 1: Add subcommands to `build_parser()`**

```python
# descriptor-sign-now
dsn = sub.add_parser("descriptor-sign-now", help="force-sign a new descriptor immediately")
dsn.add_argument("--db-path", default=DEFAULT_DB)
dsn.add_argument("--config", default="/etc/mthydra/controller.toml")

# descriptor-show
dsh = sub.add_parser("descriptor-show", help="print descriptor payload (pretty JSON)")
dsh.add_argument("--generation", type=int, default=None)
dsh.add_argument("--db-path", default=DEFAULT_DB)

# descriptor-verify
dvf = sub.add_parser("descriptor-verify",
                      help="verify a descriptor file against trusted keys in DB")
dvf.add_argument("payload_file")
dvf.add_argument("sig_file")
dvf.add_argument("--db-path", default=DEFAULT_DB)
dvf.add_argument("--now", default=None, help="ISO-8601 timestamp (default: now)")
```

- [ ] **Step 2: Add dispatch in `run()`**

```python
if args.cmd == "descriptor-sign-now":
    from mthydra.controller.config import ConfigError, load_config
    from mthydra.descriptor.sign import SignError, sign_new_descriptor
    from datetime import datetime, timedelta, timezone
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        print(f"descriptor-sign-now: config error: {e}", file=sys.stderr); return 2
    now = _now()
    now_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
    valid_until = (now_dt + timedelta(hours=cfg.descriptor.validity_window_hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    conn = connect(args.db_path)
    try:
        gen, _, _ = sign_new_descriptor(conn, now_iso=now, valid_until_iso=valid_until)
        print(f"signed descriptor generation {gen}")
        return 0
    except SignError as e:
        print(f"descriptor-sign-now: {e}", file=sys.stderr); return 3
    finally:
        conn.close()

if args.cmd == "descriptor-show":
    import json as _json
    from mthydra.controller.state.descriptor import latest_descriptor
    conn = connect(args.db_path)
    try:
        if args.generation is not None:
            row = conn.execute(
                "SELECT generation, payload, signature FROM descriptor_history WHERE generation=?",
                (args.generation,)
            ).fetchone()
            if row is None:
                print(f"generation {args.generation} not found", file=sys.stderr); return 3
            blob = row[1].encode("utf-8")
        else:
            result = latest_descriptor(conn)
            if result is None:
                print("no descriptors in DB", file=sys.stderr); return 3
            _, blob, _ = result
        obj = _json.loads(blob)
        print(_json.dumps(obj, indent=2, sort_keys=True))
        return 0
    finally:
        conn.close()

if args.cmd == "descriptor-verify":
    from mthydra.descriptor.payload import DescriptorPayload
    from mthydra.descriptor.verify import TrustedKey, VerifyError, verify_descriptor
    blob = Path(args.payload_file).read_bytes()
    sig = Path(args.sig_file).read_bytes()
    now_str = args.now or _now()
    conn = connect(args.db_path)
    try:
        rows = conn.execute(
            "SELECT generation, pubkey FROM descriptor_signing_key "
            "WHERE retired_at IS NULL OR retired_at > ?", (now_str,)
        ).fetchall()
        trusted = [TrustedKey(generation=r[0], pubkey=bytes(r[1])) for r in rows]
    finally:
        conn.close()
    try:
        p = verify_descriptor(blob, sig, trusted, now_str)
        print(f"PASS  generation={p.generation} valid_until={p.valid_until}")
        return 0
    except VerifyError as e:
        print(f"FAIL  {e}", file=sys.stderr); return 1
```

- [ ] **Step 3: Tests for these three subcommands in `test_cli.py`**

- `descriptor-sign-now` on freshly-init'd DB succeeds and prints "generation 1".
- `descriptor-show` with no args on empty DB exits non-zero.
- `descriptor-show` after a sign prints valid JSON.
- `descriptor-verify` on a freshly-signed descriptor returns 0.

- [ ] **Step 4: Run full suite.**

- [ ] **Step 5: Commit `cli(B): descriptor-sign-now, descriptor-show, descriptor-verify`**

---

### Task 10: `signing-key-rotate`, `eu-add`, `eu-retire`

**Files:** Modify `src/mthydra/controller/cli.py`

- [ ] **Step 1: Add subcommands to `build_parser()`**

```python
# signing-key-rotate
skr = sub.add_parser("signing-key-rotate",
                      help="generate new signing key, activate, sign (single-step, B-D11)")
skr.add_argument("--db-path", default=DEFAULT_DB)
skr.add_argument("--config", default="/etc/mthydra/controller.toml")

# eu-add
eua = sub.add_parser("eu-add", help="add an EU exit node to the descriptor exit set")
eua.add_argument("fingerprint")
eua.add_argument("endpoint")
eua.add_argument("--weight", type=int, default=1)
eua.add_argument("--db-path", default=DEFAULT_DB)
eua.add_argument("--config", default="/etc/mthydra/controller.toml")

# eu-retire
eur = sub.add_parser("eu-retire", help="retire an EU exit node from the descriptor exit set")
eur.add_argument("fingerprint")
eur.add_argument("--db-path", default=DEFAULT_DB)
eur.add_argument("--config", default="/etc/mthydra/controller.toml")
```

- [ ] **Step 2: Add dispatch**

`signing-key-rotate`:
1. Generate new Ed25519 keypair.
2. Get current active key generation N.
3. Compute `outgoing_retired_at = now + validity_window_hours`.
4. UPDATE `descriptor_signing_key SET retired_at=outgoing_retired_at WHERE retired_at IS NULL`.
5. INSERT new row (generation=N+1, retired_at IS NULL).
6. Call `sign_new_descriptor` (now signed by N+1).
7. Update `obligation_clocks.descriptor_signing_key_rotation`.
8. Audit-log.

`eu-add`:
1. `eu_exit_set.add_exit(conn, fingerprint, endpoint, weight, now)`.
2. `sign_new_descriptor(conn, ...)`.
3. Audit-log.

`eu-retire`:
1. `eu_exit_set.retire_exit(conn, fingerprint, at=now)`.
2. `sign_new_descriptor(conn, ...)`.
3. Audit-log.

- [ ] **Step 3: Tests**

- `signing-key-rotate` produces a new signing key row + descriptor gen increments.
- `eu-add` inserts into eu_exit_set + new descriptor contains the new fingerprint.
- `eu-retire` removes from active set + new descriptor lacks the fingerprint.

- [ ] **Step 4: Run full suite.**

- [ ] **Step 5: Commit `cli(B): signing-key-rotate (single-step), eu-add, eu-retire`**

---

### Task 11: `descriptor-migrate-placeholder`

**Files:** Modify `src/mthydra/controller/cli.py`

- [ ] **Step 1: Add subcommand**

```python
dmp = sub.add_parser(
    "descriptor-migrate-placeholder",
    help="one-shot: replace spec A placeholder signing key with real Ed25519",
)
dmp.add_argument("--db-path", default=DEFAULT_DB)
dmp.add_argument("--config", default="/etc/mthydra/controller.toml")
```

- [ ] **Step 2: Add dispatch**

```python
if args.cmd == "descriptor-migrate-placeholder":
    from mthydra.descriptor.keys import generate_keypair, is_placeholder
    from mthydra.descriptor.sign import SignError, sign_new_descriptor
    from mthydra.controller.config import ConfigError, load_config
    conn = connect(args.db_path)
    try:
        row = conn.execute(
            "SELECT generation, privkey FROM descriptor_signing_key "
            "WHERE retired_at IS NULL ORDER BY generation DESC LIMIT 1"
        ).fetchone()
        if row is None:
            print("no active descriptor_signing_key — run init first", file=sys.stderr)
            return 3
        gen, priv = row[0], bytes(row[1])
        if not is_placeholder(priv):
            print("active descriptor_signing_key is already a real Ed25519 key — nothing to do")
            return 0
        # Generate real key
        new_priv, new_pub = generate_keypair()
        new_gen = gen + 1
        now = _now()
        # Retire placeholder
        conn.execute("UPDATE descriptor_signing_key SET retired_at=? WHERE generation=?",
                     (now, gen))
        # Insert real key
        conn.execute(
            "INSERT INTO descriptor_signing_key (generation, privkey, pubkey, created_at) "
            "VALUES (?,?,?,?)", (new_gen, new_priv, new_pub, now)
        )
        conn.commit()
        # Sign first real descriptor
        try:
            cfg = load_config(args.config)
        except ConfigError:
            cfg = None
        from datetime import datetime, timedelta, timezone
        now_dt = datetime.fromisoformat(now.replace("Z", "+00:00"))
        vh = cfg.descriptor.validity_window_hours if cfg else 24
        valid_until = (now_dt + timedelta(hours=vh)).strftime("%Y-%m-%dT%H:%M:%SZ")
        from mthydra.controller.state.audit import log_event
        log_event(conn, ts=now, actor="operator", action="descriptor_migrated_from_placeholder",
                  target=str(new_gen), details_json=None)
        gen_out, _, _ = sign_new_descriptor(conn, now_iso=now, valid_until_iso=valid_until)
        print(f"migrated: new signing key gen={new_gen}, signed descriptor gen={gen_out}")
        return 0
    finally:
        conn.close()
```

- [ ] **Step 3: Tests**

- On a spec-A bootstrap DB (placeholder key), `descriptor-migrate-placeholder` succeeds; new key is not placeholder; a descriptor was signed.
- On a DB already having a real key, prints "nothing to do" and exits 0.

- [ ] **Step 4: Run full suite.**

- [ ] **Step 5: Commit `cli(B): descriptor-migrate-placeholder one-shot`**

---

## Phase 7 — Invariants, property test, polish (Task 12)

### Task 12: `invariants.py` additions + property test + Makefile

**Files:**
- Modify `src/mthydra/controller/state/invariants.py`
- Modify `src/mthydra/controller/startup.py`
- Create `tests/property/test_descriptor_chain_integrity.py`
- Modify `Makefile`

- [ ] **Step 1: Add invariant checks 13–16 to `invariants.py`**

```python
def _check_13_at_most_two_active_signing_keys(conn, now_iso: str) -> None:
    count = conn.execute(
        "SELECT COUNT(*) FROM descriptor_signing_key "
        "WHERE retired_at IS NULL OR retired_at > ?", (now_iso,)
    ).fetchone()[0]
    if count > 2:
        raise InvariantViolation(f"check 13: {count} active descriptor_signing_key rows (max 2)")

def _check_14_descriptor_history_fk(conn) -> None:
    row = conn.execute(
        "SELECT dh.generation FROM descriptor_history dh "
        "LEFT JOIN descriptor_signing_key dsk ON dh.signing_key_generation=dsk.generation "
        "WHERE dsk.generation IS NULL LIMIT 1"
    ).fetchone()
    if row:
        raise InvariantViolation(
            f"check 14: descriptor_history.generation={row[0]} references missing signing_key"
        )

def _check_15_chain_integrity(conn) -> None:
    rows = conn.execute(
        "SELECT generation, payload FROM descriptor_history ORDER BY generation"
    ).fetchall()
    import hashlib, json
    prev_hash = None
    for gen, payload_text in rows:
        blob = payload_text.encode("utf-8")
        obj = json.loads(blob)
        ph = obj.get("previous_generation_hash")
        if gen == 1:
            if ph is not None:
                raise InvariantViolation(
                    f"check 15: generation 1 has non-null previous_generation_hash"
                )
        else:
            if ph != prev_hash:
                raise InvariantViolation(
                    f"check 15: chain break at generation {gen}: "
                    f"stored={ph!r} expected={prev_hash!r}"
                )
        prev_hash = hashlib.sha256(blob).hexdigest()

def _check_16_no_placeholder_key_in_production(conn, mode: str) -> None:
    if mode in ("dryrun", "offline"):
        return
    row = conn.execute(
        "SELECT generation FROM descriptor_signing_key WHERE retired_at IS NULL "
        "ORDER BY generation DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return
    priv = conn.execute(
        "SELECT privkey FROM descriptor_signing_key WHERE generation=?", (row[0],)
    ).fetchone()
    if priv and bytes(priv[0]).startswith(b"PRIV-DESC-"):
        raise InvariantViolation(
            "check 16: active descriptor_signing_key is a spec A placeholder; "
            "run: mthydra-controller descriptor-migrate-placeholder"
        )
```

Wire into `check_all(conn, expected_schema_version, mode="production", now_iso=None)`.

- [ ] **Step 2: Update `startup.py`** — pass `mode` and `now_iso` through to `check_all`.

- [ ] **Step 3: Write `test_descriptor_chain_integrity.py` (Hypothesis)**

Strategy: sequence of operations — sign / eu-add / eu-retire / signing-key-rotate — applied to a seeded DB. At each step verify that `verify_chain(all_descriptors_so_far, trusted_keys, now)` succeeds and all chain hashes link.

- [ ] **Step 4: Add `smoke-descriptor` to `Makefile`**

```make
smoke-descriptor:
	@echo "--- descriptor smoke test ---"
	@echo "1. mthydra-controller init (if not already done)"
	@echo "2. mthydra-controller eu-add <fingerprint> <endpoint>"
	@echo "3. mthydra-controller descriptor-sign-now"
	@echo "4. mthydra-controller descriptor-show"
	@echo "5. mthydra-controller descriptor-verify <payload-file> <sig-file>"
```

- [ ] **Step 5: Run full suite. Confirm ≥ 90% coverage on `mthydra.descriptor.*`.**

- [ ] **Step 6: Commit `invariants(B): startup checks 13–16 + property test + smoke-descriptor`**

---

## Cross-spec contracts (deliberate, not gaps)

- Spec E will copy `mthydra/descriptor/verify.py` into the RU image build. Until E lands, the verifier exists but is never invoked by RU boxes.
- Spec F will automate `eu-add` / `eu-retire` as EU nodes are provisioned and decommissioned. Until F lands, operators call these commands directly.
- Spec G will embed the initial trusted pubkey(s) in the RU provisioning seed bundle.
- Spec J will attach the `self_alarm_unreachable` condition from `sign_new_descriptor` failures to its outbound alerting channel.

---

## Execution handoff

Plan complete. 12 tasks across 7 phases, ~14 commits. Ready to execute.
