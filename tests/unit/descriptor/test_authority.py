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

    priv_obj = ed25519.Ed25519PrivateKey.generate()
    priv_pem = priv_obj.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    pub_pem = priv_obj.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    payload = json.dumps(
        {"schema": "mthydra.onward_credential.v99", "box_id": "x",
         "issued_at": "t", "authority_generation": 1},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    sig = priv_obj.sign(payload)
    blob = struct.pack(">H", len(payload)) + payload + sig
    with pytest.raises(VerifyError, match="schema"):
        verify_onward_credential(blob, pub_pem)


def test_sign_rejects_non_ed25519_key():
    """sign_onward_credential must refuse RSA / non-Ed25519 PKCS#8 keys."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    rsa_pem = rsa_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    with pytest.raises(ValueError, match="Ed25519"):
        sign_onward_credential(rsa_pem, box_id="b", issued_at="t", authority_generation=1)


def test_verify_rejects_length_mismatch():
    """Wire format: header N must equal actual payload length."""
    import struct
    priv, pub = generate_authority_keypair()
    blob = sign_onward_credential(priv, box_id="b", issued_at="t", authority_generation=1)
    # Rewrite header to claim a larger payload than actually present.
    bogus = struct.pack(">H", 9999) + blob[2:]
    with pytest.raises(VerifyError, match="length mismatch"):
        verify_onward_credential(bogus, pub)


def test_verify_rejects_malformed_pubkey():
    priv, _ = generate_authority_keypair()
    blob = sign_onward_credential(priv, box_id="b", issued_at="t", authority_generation=1)
    with pytest.raises(VerifyError, match="invalid authority pubkey"):
        verify_onward_credential(blob, "-----BEGIN PUBLIC KEY-----\ngarbage\n-----END PUBLIC KEY-----\n")


def test_verify_rejects_non_ed25519_pubkey():
    """An RSA SPKI PEM must be rejected as not-Ed25519."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    priv, _ = generate_authority_keypair()
    blob = sign_onward_credential(priv, box_id="b", issued_at="t", authority_generation=1)
    rsa_pub_pem = rsa.generate_private_key(
        public_exponent=65537, key_size=2048
    ).public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    with pytest.raises(VerifyError, match="not Ed25519"):
        verify_onward_credential(blob, rsa_pub_pem)


def _craft_signed(payload_bytes: bytes) -> tuple[bytes, str]:
    """Sign arbitrary bytes with a fresh keypair; return (blob, pubkey_pem)."""
    import struct
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    priv_obj = ed25519.Ed25519PrivateKey.generate()
    pub_pem = priv_obj.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    sig = priv_obj.sign(payload_bytes)
    return struct.pack(">H", len(payload_bytes)) + payload_bytes + sig, pub_pem


def test_verify_rejects_non_json_payload():
    """A signature-valid payload that isn't JSON must fail."""
    blob, pub_pem = _craft_signed(b"not-json-at-all")
    with pytest.raises(VerifyError, match="not valid JSON"):
        verify_onward_credential(blob, pub_pem)


def test_verify_rejects_missing_field():
    """Valid JSON + valid signature but missing required field."""
    import json
    payload = json.dumps(
        {"schema": "mthydra.onward_credential.v1", "box_id": "x"},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    blob, pub_pem = _craft_signed(payload)
    with pytest.raises(VerifyError, match="missing or malformed"):
        verify_onward_credential(blob, pub_pem)


def test_verify_rejects_bad_field_type():
    """authority_generation that can't be coerced to int must fail."""
    import json
    payload = json.dumps(
        {"schema": "mthydra.onward_credential.v1", "box_id": "x",
         "issued_at": "t", "authority_generation": "not-an-int"},
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    blob, pub_pem = _craft_signed(payload)
    with pytest.raises(VerifyError, match="missing or malformed"):
        verify_onward_credential(blob, pub_pem)


def test_authority_module_has_no_controller_imports():
    """RU-embeddability: spec F2 copies this module verbatim. Zero
    mthydra.controller.* imports means it can run on the RU box without
    the controller package present."""
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
    assert not bad, (
        "authority.py must not import from mthydra.controller.*:\n  "
        + "\n  ".join(bad)
    )
