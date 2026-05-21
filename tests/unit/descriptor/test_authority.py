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
