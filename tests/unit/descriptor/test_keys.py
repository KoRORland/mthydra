"""Tests for Ed25519 key primitives (spec B §2 B-D1)."""
import pytest

from mthydra.descriptor.keys import (
    PLACEHOLDER_PREFIX,
    generate_keypair,
    is_placeholder,
    public_from_private,
    sign,
    verify,
)


def test_generate_produces_32_byte_keys():
    priv, pub = generate_keypair()
    assert len(priv) == 32
    assert len(pub) == 32


def test_public_from_private_consistent():
    priv, pub = generate_keypair()
    assert public_from_private(priv) == pub


def test_sign_verify_roundtrip():
    priv, pub = generate_keypair()
    msg = b"hello descriptor"
    sig = sign(priv, msg)
    assert len(sig) == 64
    assert verify(pub, msg, sig) is True


def test_tamper_message_fails():
    priv, pub = generate_keypair()
    msg = b"original message"
    sig = sign(priv, msg)
    assert verify(pub, b"tampered message", sig) is False


def test_tamper_signature_fails():
    priv, pub = generate_keypair()
    msg = b"message"
    sig = bytearray(sign(priv, msg))
    sig[0] ^= 0xFF
    assert verify(pub, msg, bytes(sig)) is False


def test_wrong_pub_fails():
    priv, _ = generate_keypair()
    _, other_pub = generate_keypair()
    msg = b"message"
    sig = sign(priv, msg)
    assert verify(other_pub, msg, sig) is False


def test_wrong_length_private_key_raises():
    with pytest.raises(ValueError):
        sign(b"tooshort", b"msg")


def test_wrong_length_private_key_public_from_private_raises():
    with pytest.raises(ValueError):
        public_from_private(b"tooshort")


def test_wrong_length_pub_returns_false():
    assert verify(b"tooshort", b"msg", b"\x00" * 64) is False


def test_wrong_length_sig_returns_false():
    _, pub = generate_keypair()
    assert verify(pub, b"msg", b"\x00" * 32) is False


def test_is_placeholder_true_for_prefixed():
    assert is_placeholder(PLACEHOLDER_PREFIX + b"something") is True


def test_is_placeholder_false_for_real_key():
    priv, _ = generate_keypair()
    assert is_placeholder(priv) is False


def test_is_placeholder_false_for_empty():
    assert is_placeholder(b"") is False
