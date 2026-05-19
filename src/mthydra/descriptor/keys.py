"""Ed25519 keypair I/O — thin wrapper around PyCA cryptography (spec B §2 B-D1)."""
from __future__ import annotations

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

# Spec A used this prefix for placeholder keys in descriptor_signing_key.privkey.
# spec B uses this constant to detect placeholders that need migration.
PLACEHOLDER_PREFIX = b"PRIV-DESC-"


def generate_keypair() -> tuple[bytes, bytes]:
    """Return (privkey_raw_32, pubkey_raw_32) for a fresh Ed25519 keypair."""
    priv = Ed25519PrivateKey.generate()
    priv_bytes = priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    pub_bytes = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return priv_bytes, pub_bytes


def public_from_private(priv: bytes) -> bytes:
    """Derive the 32-byte public key from a 32-byte private key."""
    if len(priv) != 32:
        raise ValueError(f"private key must be 32 bytes, got {len(priv)}")
    return Ed25519PrivateKey.from_private_bytes(priv).public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw
    )


def sign(priv: bytes, msg: bytes) -> bytes:
    """Return a 64-byte Ed25519 signature over msg."""
    if len(priv) != 32:
        raise ValueError(f"private key must be 32 bytes, got {len(priv)}")
    return Ed25519PrivateKey.from_private_bytes(priv).sign(msg)


def verify(pub: bytes, msg: bytes, sig: bytes) -> bool:
    """Return True if sig is a valid Ed25519 signature of msg under pub.

    Never raises — returns False on any failure (wrong key, wrong sig, bad lengths).
    """
    if len(pub) != 32 or len(sig) != 64:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(pub).verify(sig, msg)
        return True
    except (InvalidSignature, ValueError):
        return False


def is_placeholder(priv: bytes) -> bool:
    """True if priv looks like a spec A placeholder (not a real Ed25519 key)."""
    return priv.startswith(PLACEHOLDER_PREFIX)
