# Unit V1 — uTLS Fingerprint Freshness + Diversity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the RU Reality client's uTLS fingerprint out of a hardcoded `"chrome"` into a signed-descriptor weighted list, so each box deterministically self-picks a stable-but-diverse fingerprint the controller can roll fleet-wide without re-imaging.

**Architecture:** The signed descriptor gains an optional top-level `tls_fingerprints` weighted list (schema `v2 → v3`). The RU `config_gen` deterministically maps `box_id` onto that list when rendering the sing-box client outbound. The controller reads a weighted map from `controller.toml` and the signer emits it. Verifier accepts both v2 and v3.

**Tech Stack:** Python 3.14, stdlib (`hashlib`, `json`), Ed25519 via `cryptography`, sqlite3, pytest. No new dependencies.

**Spec:** `doc/specs/2026-06-06-V-ru-egress-obfuscation.md` §3, §6.1, §6.3, §7 (#33).

---

## File Structure

- `src/mthydra/descriptor/payload.py` — MODIFY: add `SCHEMA_V3`, `tls_fingerprints` field, parse + canonical emit.
- `src/mthydra/ru_agent/config_gen.py` — MODIFY: `_pick_fingerprint` + use it at the outbound render.
- `src/mthydra/descriptor/sign.py` — MODIFY: `sign_new_descriptor` accepts `tls_fingerprints` and threads it into the payload.
- `src/mthydra/controller/config.py` — MODIFY: `DescriptorConfig` gains `tls_fingerprints`; loader parses `[descriptor.tls_fingerprints]`.
- `src/mthydra/descriptor/scheduler.py` — MODIFY: `DescriptorRotator` carries `tls_fingerprints` and passes it to `sign_new_descriptor`.
- `src/mthydra/controller/cli.py` — MODIFY: add `tls-fingerprints-show`.
- `packaging/etc/mthydra/controller.toml.example` — MODIFY: document `[descriptor.tls_fingerprints]`.
- Tests under `tests/unit/descriptor/`, `tests/unit/ru_agent/`, `tests/unit/controller/`.

**Invariant #33** (every published `fp` ∈ known set) is enforced at sign time (Task 5).

---

## Task 1: Descriptor payload — schema v3 + `tls_fingerprints`

**Files:**
- Modify: `src/mthydra/descriptor/payload.py`
- Test: `tests/unit/descriptor/test_payload_v3.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/descriptor/test_payload_v3.py
from mthydra.descriptor.payload import (
    DescriptorPayload, EUExit, SCHEMA_V3, canonical_bytes,
)


def _payload(fps):
    return DescriptorPayload(
        generation=2,
        signing_key_gen=1,
        issued_at="2026-06-06T00:00:00Z",
        valid_until="2026-06-07T00:00:00Z",
        eu_exit_set=(EUExit("fp1", "1.2.3.4:443", 1, "cover.example", "pub=="),),
        previous_generation_hash="abc",
        next_signing_pubkey=None,
        schema=SCHEMA_V3,
        tls_fingerprints=(("chrome", 60), ("firefox", 40)),
    )


def test_v3_roundtrips_fingerprints():
    blob = canonical_bytes(_payload((("chrome", 60), ("firefox", 40))))
    parsed = DescriptorPayload.from_canonical_bytes(blob)
    assert parsed.schema == SCHEMA_V3
    assert parsed.tls_fingerprints == (("chrome", 60), ("firefox", 40))
    # canonical form is stable
    assert canonical_bytes(parsed) == blob


def test_v3_fingerprints_sorted_canonically():
    # Input order must not affect canonical bytes — sorted by fp name.
    a = canonical_bytes(_payload((("firefox", 40), ("chrome", 60))))
    b = canonical_bytes(_payload((("chrome", 60), ("firefox", 40))))
    assert a == b


def test_v3_omitted_fingerprints_is_none():
    p = DescriptorPayload(
        generation=1, signing_key_gen=1,
        issued_at="2026-06-06T00:00:00Z", valid_until="2026-06-07T00:00:00Z",
        eu_exit_set=(), previous_generation_hash=None, next_signing_pubkey=None,
        schema=SCHEMA_V3, tls_fingerprints=None,
    )
    blob = canonical_bytes(p)
    assert DescriptorPayload.from_canonical_bytes(blob).tls_fingerprints is None


def test_v2_blob_parses_with_none_fingerprints():
    # A legacy v2 blob (no tls_fingerprints key) still parses.
    from mthydra.descriptor.payload import SCHEMA_V2
    p = DescriptorPayload(
        generation=1, signing_key_gen=1,
        issued_at="2026-06-06T00:00:00Z", valid_until="2026-06-07T00:00:00Z",
        eu_exit_set=(), previous_generation_hash=None, next_signing_pubkey=None,
        schema=SCHEMA_V2,
    )
    blob = canonical_bytes(p)
    parsed = DescriptorPayload.from_canonical_bytes(blob)
    assert parsed.schema == SCHEMA_V2
    assert parsed.tls_fingerprints is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/descriptor/test_payload_v3.py -v`
Expected: FAIL — `ImportError: cannot import name 'SCHEMA_V3'` / `TypeError: unexpected keyword argument 'tls_fingerprints'`.

- [ ] **Step 3: Modify `payload.py`**

Add the schema constant and accept it:

```python
SCHEMA_V1 = "mthydra.descriptor.v1"
SCHEMA_V2 = "mthydra.descriptor.v2"
SCHEMA_V3 = "mthydra.descriptor.v3"
SCHEMA = SCHEMA_V3  # default schema for new payloads
_ACCEPTED_SCHEMAS = frozenset({SCHEMA_V1, SCHEMA_V2, SCHEMA_V3})
```

Add `tls_fingerprints` to known top-level fields:

```python
_KNOWN_FIELDS = frozenset({
    "schema",
    "generation",
    "signing_key_gen",
    "issued_at",
    "valid_until",
    "eu_exit_set",
    "previous_generation_hash",
    "next_signing_pubkey",
    "tls_fingerprints",
})
```

v3 carries the same per-exit fields as v2:

```python
_KNOWN_EXIT_FIELDS_V1 = frozenset({"fingerprint", "endpoint", "weight"})
_KNOWN_EXIT_FIELDS_V2 = _KNOWN_EXIT_FIELDS_V1 | {"cover_sni", "reality_pubkey"}
_KNOWN_EXIT_FIELDS_V3 = _KNOWN_EXIT_FIELDS_V2
```

Add the dataclass field (default `None` so v1/v2 construction is unchanged):

```python
@dataclass(frozen=True)
class DescriptorPayload:
    generation: int
    signing_key_gen: int
    issued_at: str
    valid_until: str
    eu_exit_set: tuple[EUExit, ...]
    previous_generation_hash: str | None
    next_signing_pubkey: str | None
    schema: str = SCHEMA_V3
    tls_fingerprints: tuple[tuple[str, int], ...] | None = None
```

In `from_canonical_bytes`, after computing `schema`, select the exit-field set so v3 is accepted, and parse the fingerprints:

```python
        allowed_exit_fields = _KNOWN_EXIT_FIELDS_V1
        if schema in (SCHEMA_V2, SCHEMA_V3):
            allowed_exit_fields = _KNOWN_EXIT_FIELDS_V2

        # ... existing exit parsing, but gate cover_sni/reality_pubkey on v2-or-v3:
        cover_sni = e.get("cover_sni") if schema in (SCHEMA_V2, SCHEMA_V3) else None
        reality_pubkey = e.get("reality_pubkey") if schema in (SCHEMA_V2, SCHEMA_V3) else None

        # tls_fingerprints (v3 only; tolerated-absent)
        fps_raw = obj.get("tls_fingerprints")
        tls_fingerprints = None
        if fps_raw is not None:
            tls_fingerprints = tuple(
                (str(item["fp"]), int(item["weight"])) for item in fps_raw
            )
```

and pass `tls_fingerprints=tls_fingerprints` into the `cls(...)` return.

In `canonical_bytes`, build the exit list for v2-or-v3 (the v2 branch already emits cover_sni/reality_pubkey — extend its guard to `in (SCHEMA_V2, SCHEMA_V3)`), then conditionally include the sorted fingerprint list:

```python
    if payload.schema in (SCHEMA_V2, SCHEMA_V3):
        exits = [ ... cover_sni/endpoint/fingerprint/reality_pubkey/weight ... ]
    else:
        exits = [ ... v1 ... ]

    obj: dict[str, Any] = {
        "schema": payload.schema,
        "generation": payload.generation,
        "signing_key_gen": payload.signing_key_gen,
        "issued_at": payload.issued_at,
        "valid_until": payload.valid_until,
        "eu_exit_set": exits,
        "previous_generation_hash": payload.previous_generation_hash,
        "next_signing_pubkey": payload.next_signing_pubkey,
    }
    if payload.schema == SCHEMA_V3 and payload.tls_fingerprints is not None:
        obj["tls_fingerprints"] = [
            {"fp": fp, "weight": w}
            for fp, w in sorted(payload.tls_fingerprints)
        ]
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
```

> Note: `sort_keys=True` orders the dict keys; we additionally sort the fingerprint *list* by `fp` so input order never changes the canonical bytes.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/descriptor/test_payload_v3.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Run the existing payload/verify suite to confirm no regression**

Run: `python -m pytest tests/unit/descriptor/ -q`
Expected: PASS. (Existing v2 round-trip tests still hold; new signs now default to v3 — if a golden v2 test asserts the literal `SCHEMA` constant, update it to `SCHEMA_V3`.)

- [ ] **Step 6: Commit**

```bash
git add src/mthydra/descriptor/payload.py tests/unit/descriptor/test_payload_v3.py
git commit -m "feat(V1): descriptor schema v3 — optional tls_fingerprints weighted list"
```

---

## Task 2: Verifier accepts v3

**Files:**
- Modify: `src/mthydra/descriptor/verify.py` (only if it pins a schema — it currently delegates to `from_canonical_bytes`, which already accepts v3)
- Test: `tests/unit/descriptor/test_verify_v3.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/descriptor/test_verify_v3.py
from mthydra.descriptor.keys import generate_keypair, sign as ed_sign
from mthydra.descriptor.payload import (
    DescriptorPayload, SCHEMA_V3, canonical_bytes,
)
from mthydra.descriptor.verify import TrustedKey, verify_descriptor


def test_verify_accepts_v3_with_fingerprints():
    priv, pub = generate_keypair()
    p = DescriptorPayload(
        generation=1, signing_key_gen=7,
        issued_at="2026-06-06T00:00:00Z", valid_until="2999-01-01T00:00:00Z",
        eu_exit_set=(), previous_generation_hash=None, next_signing_pubkey=None,
        schema=SCHEMA_V3, tls_fingerprints=(("chrome", 60), ("safari", 40)),
    )
    blob = canonical_bytes(p)
    sig = ed_sign(priv, blob)
    out = verify_descriptor(
        blob, sig, [TrustedKey(generation=7, pubkey=pub)],
        now_iso="2026-06-06T01:00:00Z",
    )
    assert out.tls_fingerprints == (("chrome", 60), ("safari", 40))
```

> Confirm the real helper names in `mthydra.descriptor.keys` (`generate_keypair`, `sign`); adjust the import to match if they differ (grep `def ` in `src/mthydra/descriptor/keys.py`).

- [ ] **Step 2: Run test to verify it fails or passes**

Run: `python -m pytest tests/unit/descriptor/test_verify_v3.py -v`
Expected: PASS already (verify delegates to `from_canonical_bytes`). If it FAILS on an unknown-field/schema error, the cause is in `payload.py` Task 1 — fix there. This task exists to *prove* the verifier path, not to change it.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/descriptor/test_verify_v3.py
git commit -m "test(V1): verifier accepts v3 descriptor with tls_fingerprints"
```

---

## Task 3: RU `config_gen._pick_fingerprint`

**Files:**
- Modify: `src/mthydra/ru_agent/config_gen.py`
- Test: `tests/unit/ru_agent/test_config_gen_fingerprint.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/ru_agent/test_config_gen_fingerprint.py
import pytest
from mthydra.ru_agent.config_gen import (
    _pick_fingerprint, KNOWN_UTLS_FINGERPRINTS, ConfigError,
)


def test_none_list_falls_back_to_chrome():
    assert _pick_fingerprint("box-1", None) == "chrome"
    assert _pick_fingerprint("box-1", []) == "chrome"


def test_pick_is_deterministic_per_box():
    wl = [{"fp": "chrome", "weight": 60}, {"fp": "firefox", "weight": 40}]
    a = _pick_fingerprint("box-abc", wl)
    b = _pick_fingerprint("box-abc", wl)
    assert a == b
    assert a in {"chrome", "firefox"}


def test_pick_varies_across_boxes():
    wl = [{"fp": "chrome", "weight": 1}, {"fp": "firefox", "weight": 1}]
    picks = {_pick_fingerprint(f"box-{i}", wl) for i in range(50)}
    assert picks == {"chrome", "firefox"}  # both appear across the fleet


def test_unknown_fingerprint_raises():
    with pytest.raises(ConfigError):
        _pick_fingerprint("box-1", [{"fp": "nessuno", "weight": 1}])


def test_weight_respected():
    # All weight on firefox -> always firefox regardless of box.
    wl = [{"fp": "chrome", "weight": 0}, {"fp": "firefox", "weight": 5}]
    assert {_pick_fingerprint(f"b{i}", wl) for i in range(20)} == {"firefox"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/ru_agent/test_config_gen_fingerprint.py -v`
Expected: FAIL — `ImportError: cannot import name '_pick_fingerprint'`.

- [ ] **Step 3: Implement in `config_gen.py`**

Add near the top (after imports):

```python
import hashlib

# uTLS fingerprint names sing-box's reality client accepts. An unknown name
# makes sing-box exit at startup ("unknown fingerprint"), so we fail at render
# time instead. Pinned to the set valid for the sing-box version the image ships.
KNOWN_UTLS_FINGERPRINTS = frozenset({
    "chrome", "firefox", "safari", "ios", "android",
    "edge", "360", "qq", "random", "randomized",
})


def _pick_fingerprint(box_id: str, weighted_list) -> str:
    """Deterministically pick a uTLS fingerprint for this box from a signed
    weighted list. Stable per box_id (a mutating ClientHello is itself an
    anomaly), diverse across the fleet, re-pickable when the list changes.

    weighted_list: list of {"fp": str, "weight": int} (from descriptor).
    Falsy list -> "chrome" (v2-descriptor fallback)."""
    if not weighted_list:
        return "chrome"
    pairs = [(str(item["fp"]), int(item["weight"])) for item in weighted_list]
    for fp, _w in pairs:
        if fp not in KNOWN_UTLS_FINGERPRINTS:
            raise ConfigError(f"unknown uTLS fingerprint in descriptor: {fp!r}")
    total = sum(max(0, w) for _fp, w in pairs)
    if total <= 0:
        return "chrome"
    digest = hashlib.sha256(box_id.encode("utf-8")).digest()
    idx = int.from_bytes(digest[:8], "big") % total
    acc = 0
    for fp, w in sorted(pairs):  # sort for determinism independent of input order
        acc += max(0, w)
        if idx < acc:
            return fp
    return sorted(pairs)[-1][0]  # unreachable rounding guard
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/ru_agent/test_config_gen_fingerprint.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/ru_agent/config_gen.py tests/unit/ru_agent/test_config_gen_fingerprint.py
git commit -m "feat(V1): deterministic per-box uTLS fingerprint pick"
```

---

## Task 4: Wire `_pick_fingerprint` into the sing-box render

**Files:**
- Modify: `src/mthydra/ru_agent/config_gen.py` (`render_sing_box_config`)
- Test: `tests/unit/ru_agent/test_config_gen.py` (extend existing)

- [ ] **Step 1: Write the failing test**

```python
# add to tests/unit/ru_agent/test_config_gen.py
import json
from types import SimpleNamespace
from mthydra.ru_agent import config_gen


def _seed(box_id="box-xyz"):
    return SimpleNamespace(box_id=box_id, reality_uuid="uuid-1", sni="x.example")


def _descriptor(fps):
    return {
        "eu_exit_set": [{
            "fingerprint": "fp1", "endpoint": "9.9.9.9:443",
            "cover_sni": "cover.example", "reality_pubkey": "pub==", "weight": 1,
        }],
        "tls_fingerprints": fps,
    }


def test_render_uses_descriptor_fingerprint():
    out = config_gen.render_sing_box_config(
        _seed(), _descriptor([{"fp": "firefox", "weight": 1}]), tproxy_port=12345,
    )
    cfg = json.loads(out)
    vless = [o for o in cfg["outbounds"] if o.get("type") == "vless"][0]
    assert vless["tls"]["utls"]["fingerprint"] == "firefox"


def test_render_falls_back_to_chrome_without_field():
    out = config_gen.render_sing_box_config(
        _seed(), _descriptor(None), tproxy_port=12345,
    )
    cfg = json.loads(out)
    vless = [o for o in cfg["outbounds"] if o.get("type") == "vless"][0]
    assert vless["tls"]["utls"]["fingerprint"] == "chrome"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/ru_agent/test_config_gen.py::test_render_uses_descriptor_fingerprint -v`
Expected: FAIL — current code always emits `"chrome"`.

- [ ] **Step 3: Implement**

In `render_sing_box_config`, before the `for exit in exits:` loop:

```python
    fp = _pick_fingerprint(seed.box_id, descriptor_payload.get("tls_fingerprints"))
```

and change the outbound's utls line (`config_gen.py:62`):

```python
                "utls": {"enabled": True, "fingerprint": fp},
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/unit/ru_agent/test_config_gen.py -v`
Expected: PASS (new + existing; update any existing golden that asserted `"chrome"` to use the fallback descriptor or assert `fp`).

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/ru_agent/config_gen.py tests/unit/ru_agent/test_config_gen.py
git commit -m "feat(V1): render sing-box outbound with per-box descriptor fingerprint"
```

---

## Task 5: Signer emits `tls_fingerprints` + invariant #33

**Files:**
- Modify: `src/mthydra/descriptor/sign.py` (`sign_new_descriptor`)
- Test: `tests/unit/descriptor/test_sign_fingerprints.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/descriptor/test_sign_fingerprints.py
import pytest
from mthydra.descriptor.payload import DescriptorPayload
from mthydra.descriptor.sign import sign_new_descriptor, SignError


def test_sign_includes_fingerprints(controller_conn):  # fixture: a seeded DB conn with an active signing key
    gen, blob, _sig = sign_new_descriptor(
        controller_conn,
        now_iso="2026-06-06T00:00:00Z",
        valid_until_iso="2026-06-07T00:00:00Z",
        tls_fingerprints=(("chrome", 60), ("firefox", 40)),
    )
    p = DescriptorPayload.from_canonical_bytes(blob)
    assert p.tls_fingerprints == (("chrome", 60), ("firefox", 40))


def test_sign_rejects_unknown_fingerprint(controller_conn):
    with pytest.raises(SignError):
        sign_new_descriptor(
            controller_conn,
            now_iso="2026-06-06T00:00:00Z",
            valid_until_iso="2026-06-07T00:00:00Z",
            tls_fingerprints=(("nessuno", 1),),
        )


def test_sign_without_fingerprints_omits_field(controller_conn):
    _gen, blob, _sig = sign_new_descriptor(
        controller_conn,
        now_iso="2026-06-06T00:00:00Z",
        valid_until_iso="2026-06-07T00:00:00Z",
    )
    assert b"tls_fingerprints" not in blob
```

> Reuse the existing signing-key DB fixture from the current `tests/unit/descriptor/` suite (grep for the fixture that calls `insert_signing_key`); name the param to match it. If none exists as a fixture, build the conn inline using `mthydra.controller.state.db` schema init + `insert_signing_key`.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/descriptor/test_sign_fingerprints.py -v`
Expected: FAIL — `sign_new_descriptor` has no `tls_fingerprints` parameter.

- [ ] **Step 3: Implement in `sign.py`**

Import the known set and add the parameter + validation:

```python
from mthydra.ru_agent.config_gen import KNOWN_UTLS_FINGERPRINTS  # single source of truth
```

> If importing from `ru_agent` into `descriptor` is undesirable (keep `descriptor` RU-embeddable and free of `ru_agent`), instead define `KNOWN_UTLS_FINGERPRINTS` in `mthydra/descriptor/payload.py` and import it in BOTH `sign.py` and `config_gen.py`. Prefer this: `payload.py` is already RU-embeddable. Move the frozenset to `payload.py` and have `config_gen.py` do `from mthydra.descriptor.payload import KNOWN_UTLS_FINGERPRINTS`.

```python
def sign_new_descriptor(
    conn: sqlite3.Connection,
    *,
    now_iso: str,
    valid_until_iso: str,
    next_signing_pubkey_hex: str | None = None,
    tls_fingerprints: tuple[tuple[str, int], ...] | None = None,
) -> tuple[int, bytes, bytes]:
    ...
    if tls_fingerprints is not None:
        for fp, _w in tls_fingerprints:
            if fp not in KNOWN_UTLS_FINGERPRINTS:
                raise SignError(f"invariant #33: unknown uTLS fingerprint {fp!r}")
    ...
    payload = DescriptorPayload(
        generation=gen,
        signing_key_gen=key_gen,
        issued_at=now_iso,
        valid_until=valid_until_iso,
        eu_exit_set=exits,
        previous_generation_hash=prev_hash,
        next_signing_pubkey=next_signing_pubkey_hex,
        tls_fingerprints=tls_fingerprints,
    )
```

(`DescriptorPayload`'s `schema` defaults to `SCHEMA_V3`, so the blob is v3.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/descriptor/test_sign_fingerprints.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Run the descriptor suite**

Run: `python -m pytest tests/unit/descriptor/ -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/mthydra/descriptor/sign.py src/mthydra/descriptor/payload.py src/mthydra/ru_agent/config_gen.py tests/unit/descriptor/test_sign_fingerprints.py
git commit -m "feat(V1): signer emits tls_fingerprints; invariant #33 rejects unknown fp"
```

---

## Task 6: Controller config — `[descriptor.tls_fingerprints]`

**Files:**
- Modify: `src/mthydra/controller/config.py` (`DescriptorConfig` + its loader)
- Test: `tests/unit/controller/test_config_tls_fingerprints.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/controller/test_config_tls_fingerprints.py
from mthydra.controller.config import load_config  # use the real loader entrypoint


def test_descriptor_fingerprints_parsed(tmp_path):
    toml = tmp_path / "controller.toml"
    toml.write_text(
        "[descriptor]\n"
        "rotation_interval_hours = 6\n"
        "validity_window_hours = 24\n"
        "[descriptor.tls_fingerprints]\n"
        "chrome = 60\n"
        "firefox = 40\n"
        # ... include whatever other required sections load_config needs ...
    )
    cfg = load_config(toml)
    assert cfg.descriptor.tls_fingerprints == (("chrome", 60), ("firefox", 40))


def test_descriptor_fingerprints_default_empty(tmp_path):
    toml = tmp_path / "controller.toml"
    toml.write_text(
        "[descriptor]\n"
        "rotation_interval_hours = 6\n"
        "validity_window_hours = 24\n"
    )
    cfg = load_config(toml)
    assert cfg.descriptor.tls_fingerprints == ()
```

> Confirm the loader entrypoint name (`load_config` / `Config.load` / `from_toml`) by grepping `def load` in `config.py`; copy a known-good minimal `controller.toml` body from an existing config test so all required sections are present.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/controller/test_config_tls_fingerprints.py -v`
Expected: FAIL — `DescriptorConfig` has no `tls_fingerprints`.

- [ ] **Step 3: Implement**

Extend the dataclass:

```python
@dataclass(frozen=True)
class DescriptorConfig:
    rotation_interval_hours: int
    validity_window_hours: int
    tls_fingerprints: tuple[tuple[str, int], ...] = ()
```

In the `[descriptor]` loader (grep for where `DescriptorConfig(` is constructed), parse the subtable:

```python
    desc_raw = data.get("descriptor", {})
    fp_raw = desc_raw.get("tls_fingerprints", {})
    tls_fingerprints = tuple(
        (str(k), int(v)) for k, v in sorted(fp_raw.items())
    )
    # ... DescriptorConfig(rotation_interval_hours=..., validity_window_hours=...,
    #                      tls_fingerprints=tls_fingerprints)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/controller/test_config_tls_fingerprints.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/controller/config.py tests/unit/controller/test_config_tls_fingerprints.py
git commit -m "feat(V1): controller.toml [descriptor.tls_fingerprints] weighted map"
```

---

## Task 7: Thread fingerprints through the rotator into signing

**Files:**
- Modify: `src/mthydra/descriptor/scheduler.py` (`DescriptorRotator`)
- Modify: the serve wiring that constructs `DescriptorRotator(...)` (grep `DescriptorRotator(` under `src/mthydra/controller/`)
- Test: `tests/unit/descriptor/test_scheduler_fingerprints.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/descriptor/test_scheduler_fingerprints.py
from mthydra.descriptor.payload import DescriptorPayload
from mthydra.descriptor.scheduler import DescriptorRotator


def test_rotator_passes_fingerprints(controller_db_path):  # path to a DB with an active signing key
    rot = DescriptorRotator(
        db_path=controller_db_path,
        rotation_interval_seconds=3600,
        validity_window_seconds=86400,
        mode="offline",
        tls_fingerprints=(("chrome", 60), ("safari", 40)),
    )
    gen = rot.sign_now()
    assert gen > 0
    # read back the latest descriptor blob and assert the field
    import sqlite3
    from mthydra.controller.state.descriptor import latest_descriptor_with_signature
    conn = sqlite3.connect(controller_db_path)
    _g, blob, _sig = latest_descriptor_with_signature(conn)
    assert DescriptorPayload.from_canonical_bytes(blob).tls_fingerprints == (
        ("chrome", 60), ("safari", 40),
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/descriptor/test_scheduler_fingerprints.py -v`
Expected: FAIL — `DescriptorRotator.__init__` has no `tls_fingerprints`.

- [ ] **Step 3: Implement**

Add the field to `__init__`:

```python
    def __init__(
        self,
        db_path,
        rotation_interval_seconds,
        validity_window_seconds,
        mode: str = "production",
        clock=None,
        timer_factory=None,
        tls_fingerprints: tuple[tuple[str, int], ...] | None = None,
    ) -> None:
        ...
        self.tls_fingerprints = tls_fingerprints
```

In `_rotate`, pass it to `sign_new_descriptor`:

```python
        sign_new_descriptor(
            conn,
            now_iso=now_iso,
            valid_until_iso=valid_until_iso,
            tls_fingerprints=self.tls_fingerprints or None,
        )
```

(Read the real `_rotate` body — it currently calls `sign_new_descriptor`; add the kwarg. `() ` empty tuple becomes `None` so the field is omitted when unconfigured.)

In the serve wiring (grep `DescriptorRotator(` under `src/mthydra/controller/`), pass the config value:

```python
    DescriptorRotator(
        ...,
        tls_fingerprints=cfg.descriptor.tls_fingerprints,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/descriptor/test_scheduler_fingerprints.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/mthydra/descriptor/scheduler.py src/mthydra/controller/ tests/unit/descriptor/test_scheduler_fingerprints.py
git commit -m "feat(V1): thread tls_fingerprints config through rotator into signing"
```

---

## Task 8: `tls-fingerprints-show` CLI + controller.toml.example

**Files:**
- Modify: `src/mthydra/controller/cli.py`
- Modify: `packaging/etc/mthydra/controller.toml.example`
- Test: `tests/unit/controller/test_cli_tls_fingerprints_show.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/controller/test_cli_tls_fingerprints_show.py
from mthydra.controller import cli


def test_tls_fingerprints_show(capsys, controller_toml_path):
    rc = cli.main(["tls-fingerprints-show", "--config", str(controller_toml_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "chrome" in out
```

> Match the actual CLI entry signature (grep `def main` / argparse subparser registration in `cli.py`); follow the pattern of an existing read-only subcommand like `data-exit-config-show`.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/controller/test_cli_tls_fingerprints_show.py -v`
Expected: FAIL — unknown subcommand.

- [ ] **Step 3: Implement**

Register a subparser `tls-fingerprints-show` that loads the config and prints the weighted pool plus the canonical `tls_fingerprints` list the signer will emit:

```python
def _cmd_tls_fingerprints_show(args) -> int:
    cfg = load_config(args.config)
    fps = cfg.descriptor.tls_fingerprints
    if not fps:
        print("tls_fingerprints: (none configured — boxes fall back to 'chrome')")
        return 0
    total = sum(w for _fp, w in fps)
    for fp, w in sorted(fps):
        print(f"  {fp:12s} weight={w}  (~{100*w//total}%)")
    return 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/controller/test_cli_tls_fingerprints_show.py -v`
Expected: PASS.

- [ ] **Step 5: Document in `controller.toml.example`**

Add under the `[descriptor]` section:

```toml
[descriptor.tls_fingerprints]
# Weighted uTLS fingerprint pool emitted in the signed descriptor (schema v3).
# Each RU box deterministically self-picks one by box_id, stable per box.
# Update weights as the real browser population moves; the next signed
# descriptor rolls the change fleet-wide (no re-imaging). Names MUST be in
# sing-box's known uTLS set (chrome/firefox/safari/ios/android/edge/...).
# Omit this section entirely to keep every box on the legacy 'chrome' default.
chrome  = 60
firefox = 15
safari  = 10
ios     = 10
edge    = 5
```

- [ ] **Step 6: Commit**

```bash
git add src/mthydra/controller/cli.py packaging/etc/mthydra/controller.toml.example tests/unit/controller/test_cli_tls_fingerprints_show.py
git commit -m "feat(V1): tls-fingerprints-show CLI + controller.toml.example docs"
```

---

## Task 9: Full-suite regression + CHANGELOG

**Files:**
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Run the changed-scope test suites**

Run: `python -m pytest tests/unit/descriptor/ tests/unit/ru_agent/ tests/unit/controller/ -q`
Expected: PASS. (Per the ruff-version memo, scope lint to changed files: `ruff check src/mthydra/descriptor/payload.py src/mthydra/descriptor/sign.py src/mthydra/ru_agent/config_gen.py src/mthydra/controller/config.py`.)

- [ ] **Step 2: CHANGELOG entry**

Add under the unreleased section:

```markdown
- feat(V1): uTLS fingerprint freshness + diversity — signed-descriptor v3
  `tls_fingerprints` weighted list; each RU box deterministically self-picks
  a stable, diverse fingerprint the controller rolls fleet-wide without
  re-imaging. Verifier accepts v2 and v3. Invariant #33 (known-fp at sign).
```

- [ ] **Step 3: Commit + push**

```bash
git add CHANGELOG.md
git commit -m "docs(V1): CHANGELOG — uTLS fingerprint diversity"
git push origin main
```

---

## Self-Review (completed during authoring)

- **Spec coverage:** §3.1 descriptor field (Task 1), §3.3 box self-pick (Tasks 3–4), §3.2 controller config + signer (Tasks 5–7), §3.4/#33 known-fp + v2/v3 accept (Tasks 1,2,5), §8 CLI (Task 8), §6.1/§6.3 amendments (Tasks 1,6,8). Covered.
- **Placeholder scan:** the three "grep for the real name" notes point at *existing* symbols (fixtures, loader entrypoint, rotator construction site) whose exact spelling the implementer confirms locally — the logic code is complete in every step.
- **Type consistency:** `tls_fingerprints` is `tuple[tuple[str, int], ...] | None` end to end (payload, sign, scheduler); controller config holds `tuple[tuple[str,int],...]` (empty default) and is normalized to `None` when empty before signing (Task 7). `KNOWN_UTLS_FINGERPRINTS` lives in `payload.py`, imported by both `config_gen` and `sign` (Task 5 note).
