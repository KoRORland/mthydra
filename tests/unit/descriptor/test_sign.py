"""Tests for the controller-side sign path (spec B §6)."""
import pytest

from mthydra.controller.state.db import connect
from mthydra.controller.state.descriptor import insert_signing_key, latest_descriptor_with_signature
from mthydra.controller.state.eu_exit_set import add_exit
from mthydra.controller.state.schema import apply_schema
from mthydra.descriptor.keys import generate_keypair, verify as ed_verify
from mthydra.descriptor.payload import DescriptorPayload, payload_hash
from mthydra.descriptor.sign import SignError, sign_new_descriptor


def _seeded_db(tmp_path, use_placeholder=False):
    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    if use_placeholder:
        priv = b"PRIV-DESC-" + b"\x00" * 22
        pub = b"PUB-DESC-" + b"\x00" * 23
    else:
        priv, pub = generate_keypair()
    insert_signing_key(conn, 1, priv, pub, "2026-05-19T00:00:00Z")
    return conn, pub


def test_sign_produces_valid_signature(tmp_path):
    conn, pub = _seeded_db(tmp_path)
    gen, blob, sig = sign_new_descriptor(
        conn, now_iso="2026-05-19T00:00:00Z", valid_until_iso="2026-05-19T01:00:00Z"
    )
    assert gen == 1
    assert ed_verify(pub, blob, sig)


def test_sign_generation_increments(tmp_path):
    conn, _ = _seeded_db(tmp_path)
    g1, _, _ = sign_new_descriptor(
        conn, now_iso="2026-05-19T00:00:00Z", valid_until_iso="2026-05-19T01:00:00Z"
    )
    g2, _, _ = sign_new_descriptor(
        conn, now_iso="2026-05-19T00:01:00Z", valid_until_iso="2026-05-19T01:01:00Z"
    )
    assert g1 == 1
    assert g2 == 2


def test_second_sign_has_correct_chain_hash(tmp_path):
    conn, _ = _seeded_db(tmp_path)
    _, blob1, _ = sign_new_descriptor(
        conn, now_iso="2026-05-19T00:00:00Z", valid_until_iso="2026-05-19T01:00:00Z"
    )
    _, blob2, _ = sign_new_descriptor(
        conn, now_iso="2026-05-19T00:01:00Z", valid_until_iso="2026-05-19T01:01:00Z"
    )
    p2 = DescriptorPayload.from_canonical_bytes(blob2)
    assert p2.previous_generation_hash == payload_hash(blob1)


def test_sign_with_placeholder_raises(tmp_path):
    conn, _ = _seeded_db(tmp_path, use_placeholder=True)
    with pytest.raises(SignError, match="placeholder"):
        sign_new_descriptor(
            conn, now_iso="2026-05-19T00:00:00Z", valid_until_iso="2026-05-19T01:00:00Z"
        )


def test_sign_with_empty_eu_exit_set_succeeds(tmp_path):
    conn, pub = _seeded_db(tmp_path)
    gen, blob, sig = sign_new_descriptor(
        conn, now_iso="2026-05-19T00:00:00Z", valid_until_iso="2026-05-19T01:00:00Z"
    )
    p = DescriptorPayload.from_canonical_bytes(blob)
    assert p.eu_exit_set == ()
    assert ed_verify(pub, blob, sig)


def test_sign_includes_eu_exits(tmp_path):
    conn, _ = _seeded_db(tmp_path)
    add_exit(conn, "fp1", "eu1.example.org:443", 1, "2026-05-19T00:00:00Z")
    _, blob, _ = sign_new_descriptor(
        conn, now_iso="2026-05-19T00:00:00Z", valid_until_iso="2026-05-19T01:00:00Z"
    )
    p = DescriptorPayload.from_canonical_bytes(blob)
    assert len(p.eu_exit_set) == 1
    assert p.eu_exit_set[0].fingerprint == "fp1"


def test_sign_stores_in_db(tmp_path):
    conn, _ = _seeded_db(tmp_path)
    sign_new_descriptor(
        conn, now_iso="2026-05-19T00:00:00Z", valid_until_iso="2026-05-19T01:00:00Z"
    )
    result = latest_descriptor_with_signature(conn)
    assert result is not None
    gen, blob, sig = result
    assert gen == 1
    assert len(sig) == 64


def test_sign_no_active_key_raises(tmp_path):
    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    with pytest.raises(SignError, match="no active"):
        sign_new_descriptor(
            conn, now_iso="2026-05-19T00:00:00Z", valid_until_iso="2026-05-19T01:00:00Z"
        )
