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
