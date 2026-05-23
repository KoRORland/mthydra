"""Tests for the pure-Python descriptor verifier (spec B §7)."""
import pytest

from mthydra.descriptor.keys import generate_keypair, sign as ed_sign
from mthydra.descriptor.payload import (
    DescriptorPayload,
    EUExit,
    canonical_bytes,
    payload_hash,
)
from mthydra.descriptor.verify import TrustedKey, VerifyError, verify_chain, verify_descriptor

NOW = "2026-05-19T00:30:00Z"
ISSUED = "2026-05-19T00:00:00Z"
VALID_UNTIL = "2026-05-19T01:00:00Z"
EXPIRED_VALID_UNTIL = "2026-05-18T23:00:00Z"  # 1.5h before NOW


def _make_payload(generation=1, prev_hash=None, signing_key_gen=1,
                  valid_until=VALID_UNTIL):
    return DescriptorPayload(
        generation=generation,
        signing_key_gen=signing_key_gen,
        issued_at=ISSUED,
        valid_until=valid_until,
        eu_exit_set=(EUExit("fp1", "eu1.example.org:443", 1),),
        previous_generation_hash=prev_hash,
        next_signing_pubkey=None,
    )


def _sign(priv, payload):
    blob = canonical_bytes(payload)
    sig = ed_sign(priv, blob)
    return blob, sig


def test_happy_path_genesis():
    priv, pub = generate_keypair()
    p = _make_payload(generation=1)
    blob, sig = _sign(priv, p)
    result = verify_descriptor(blob, sig, [TrustedKey(1, pub)], NOW)
    assert result.generation == 1


def test_happy_path_generation_2():
    priv, pub = generate_keypair()
    p1 = _make_payload(generation=1)
    blob1, sig1 = _sign(priv, p1)
    ph1 = payload_hash(blob1)
    p2 = _make_payload(generation=2, prev_hash=ph1)
    blob2, sig2 = _sign(priv, p2)
    result = verify_descriptor(blob2, sig2, [TrustedKey(1, pub)], NOW,
                               previous_descriptor_hash=ph1)
    assert result.generation == 2


def test_tamper_payload_byte():
    priv, pub = generate_keypair()
    p = _make_payload()
    blob, sig = _sign(priv, p)
    bad = bytearray(blob)
    bad[10] ^= 0x01
    with pytest.raises(VerifyError):
        verify_descriptor(bytes(bad), sig, [TrustedKey(1, pub)], NOW)


def test_non_canonical_payload_rejected():
    priv, pub = generate_keypair()
    p = _make_payload()
    import json
    blob = json.dumps(
        json.loads(canonical_bytes(p)), sort_keys=False, indent=2
    ).encode("utf-8")
    sig = ed_sign(priv, canonical_bytes(p))  # sign the canonical form
    with pytest.raises(VerifyError, match="canonical"):
        verify_descriptor(blob, sig, [TrustedKey(1, pub)], NOW)


def test_tamper_signature():
    priv, pub = generate_keypair()
    p = _make_payload()
    blob, sig = _sign(priv, p)
    bad_sig = bytearray(sig)
    bad_sig[0] ^= 0xFF
    with pytest.raises(VerifyError, match="signature"):
        verify_descriptor(blob, bytes(bad_sig), [TrustedKey(1, pub)], NOW)


def test_wrong_pubkey():
    priv, pub = generate_keypair()
    _, other_pub = generate_keypair()
    p = _make_payload()
    blob, sig = _sign(priv, p)
    with pytest.raises(VerifyError, match="signature"):
        verify_descriptor(blob, sig, [TrustedKey(1, other_pub)], NOW)


def test_signing_key_gen_not_in_trust_set():
    priv, pub = generate_keypair()
    p = _make_payload(signing_key_gen=2)
    blob, sig = _sign(priv, p)
    with pytest.raises(VerifyError, match="not in trusted"):
        verify_descriptor(blob, sig, [TrustedKey(1, pub)], NOW)


def test_expired_beyond_grace():
    priv, pub = generate_keypair()
    p = _make_payload(valid_until=EXPIRED_VALID_UNTIL)
    blob, sig = _sign(priv, p)
    # NOW is 1.5h after valid_until; grace=1h → expired
    with pytest.raises(VerifyError, match="expired"):
        verify_descriptor(blob, sig, [TrustedKey(1, pub)], NOW, grace_hours=1)


def test_expired_within_grace_passes():
    priv, pub = generate_keypair()
    p = _make_payload(valid_until=EXPIRED_VALID_UNTIL)
    blob, sig = _sign(priv, p)
    # NOW is 1.5h after valid_until; grace=2h → still valid
    result = verify_descriptor(blob, sig, [TrustedKey(1, pub)], NOW, grace_hours=2)
    assert result.generation == 1


def test_chain_mismatch():
    priv, pub = generate_keypair()
    p1 = _make_payload(generation=1)
    blob1, _ = _sign(priv, p1)
    p2 = _make_payload(generation=2, prev_hash=payload_hash(blob1))
    blob2, sig2 = _sign(priv, p2)
    with pytest.raises(VerifyError, match="chain break"):
        verify_descriptor(blob2, sig2, [TrustedKey(1, pub)], NOW,
                          previous_descriptor_hash="a" * 64)  # wrong hash


def test_cold_start_genesis_passes():
    priv, pub = generate_keypair()
    p = _make_payload(generation=1)
    blob, sig = _sign(priv, p)
    # No previous_descriptor_hash provided — genesis OK
    result = verify_descriptor(blob, sig, [TrustedKey(1, pub)], NOW)
    assert result.generation == 1


def test_tofu_rollback_defence():
    priv, pub = generate_keypair()
    p1 = _make_payload(generation=1)
    blob1, _ = _sign(priv, p1)
    ph = payload_hash(blob1)
    p2 = _make_payload(generation=2, prev_hash=ph)
    blob2, sig2 = _sign(priv, p2)
    # generation>1 with prev hash in payload but no prior given → TOFU defence
    with pytest.raises(VerifyError, match="TOFU"):
        verify_descriptor(blob2, sig2, [TrustedKey(1, pub)], NOW)


def test_schema_mismatch():
    priv, pub = generate_keypair()
    p = _make_payload()
    import json
    obj = json.loads(canonical_bytes(p))
    obj["schema"] = "wrong.v99"
    bad = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    bad_sig = ed_sign(priv, bad)
    with pytest.raises(VerifyError, match="schema"):
        verify_descriptor(bad, bad_sig, [TrustedKey(1, pub)], NOW)


def test_unknown_field_in_payload():
    priv, pub = generate_keypair()
    p = _make_payload()
    import json
    obj = json.loads(canonical_bytes(p))
    obj["surprise"] = "extra"
    bad = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    bad_sig = ed_sign(priv, bad)
    with pytest.raises(VerifyError, match="unknown"):
        verify_descriptor(bad, bad_sig, [TrustedKey(1, pub)], NOW)


def test_multi_key_trust():
    priv1, pub1 = generate_keypair()
    priv2, pub2 = generate_keypair()
    # gen 2 signed with key 2
    p = _make_payload(generation=1, signing_key_gen=2)
    blob, sig = _sign(priv2, p)
    trusted = [TrustedKey(1, pub1), TrustedKey(2, pub2)]
    result = verify_descriptor(blob, sig, trusted, NOW)
    assert result.signing_key_gen == 2


def test_generation_1_with_non_null_prev_hash_fails():
    priv, pub = generate_keypair()
    p = _make_payload(generation=1, prev_hash="a" * 64)
    blob, sig = _sign(priv, p)
    with pytest.raises(VerifyError, match="generation 1"):
        verify_descriptor(blob, sig, [TrustedKey(1, pub)], NOW)


def test_generation_2_with_null_prev_hash_fails():
    priv, pub = generate_keypair()
    # Force null previous_generation_hash for generation 2
    p = DescriptorPayload(
        generation=2,
        signing_key_gen=1,
        issued_at=ISSUED,
        valid_until=VALID_UNTIL,
        eu_exit_set=(),
        previous_generation_hash=None,  # invalid for gen > 1
        next_signing_pubkey=None,
    )
    blob, sig = _sign(priv, p)
    with pytest.raises(VerifyError, match="missing chain link"):
        verify_descriptor(blob, sig, [TrustedKey(1, pub)], NOW,
                          previous_descriptor_hash="x" * 64)


# ---------------------------------------------------------------------------
# Spec E Task 5: verifier accepts both v1 and v2 schema labels
# ---------------------------------------------------------------------------

def test_verifier_accepts_v1_descriptor():
    """RU-side rolling deployment: v1 blobs must still verify."""
    from mthydra.descriptor.payload import SCHEMA_V1
    priv, pub = generate_keypair()
    p = DescriptorPayload(
        generation=1,
        signing_key_gen=1,
        issued_at=ISSUED,
        valid_until=VALID_UNTIL,
        eu_exit_set=(EUExit("fp1", "eu1.example.org:443", 1),),
        previous_generation_hash=None,
        next_signing_pubkey=None,
        schema=SCHEMA_V1,
    )
    blob, sig = _sign(priv, p)
    result = verify_descriptor(blob, sig, [TrustedKey(1, pub)], NOW)
    assert result.schema == SCHEMA_V1
    assert result.generation == 1


def test_verifier_accepts_v2_descriptor_with_per_exit_fields():
    """v2 carries cover_sni + reality_pubkey per exit."""
    from mthydra.descriptor.payload import SCHEMA_V2
    priv, pub = generate_keypair()
    p = DescriptorPayload(
        generation=1,
        signing_key_gen=1,
        issued_at=ISSUED,
        valid_until=VALID_UNTIL,
        eu_exit_set=(
            EUExit("fp1", "eu1.example.org:443", 1,
                   cover_sni="cover.example", reality_pubkey="PUBKEY"),
        ),
        previous_generation_hash=None,
        next_signing_pubkey=None,
        schema=SCHEMA_V2,
    )
    blob, sig = _sign(priv, p)
    result = verify_descriptor(blob, sig, [TrustedKey(1, pub)], NOW)
    assert result.schema == SCHEMA_V2
    assert result.eu_exit_set[0].cover_sni == "cover.example"
    assert result.eu_exit_set[0].reality_pubkey == "PUBKEY"


def test_v1_descriptor_with_cover_sni_field_rejected():
    """v1 must not carry the new per-exit fields (would be silently ignored otherwise)."""
    import json as _json
    from mthydra.descriptor.payload import SCHEMA_V1
    priv, pub = generate_keypair()
    p = DescriptorPayload(
        generation=1,
        signing_key_gen=1,
        issued_at=ISSUED,
        valid_until=VALID_UNTIL,
        eu_exit_set=(EUExit("fp1", "eu1.example.org:443", 1),),
        previous_generation_hash=None,
        next_signing_pubkey=None,
        schema=SCHEMA_V1,
    )
    blob = canonical_bytes(p)
    obj = _json.loads(blob)
    obj["eu_exit_set"][0]["cover_sni"] = "leaked"
    bad = _json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    bad_sig = ed_sign(priv, bad)
    with pytest.raises(VerifyError, match="unknown"):
        verify_descriptor(bad, bad_sig, [TrustedKey(1, pub)], NOW)


# ---------------------------------------------------------------------------
# Chain tests
# ---------------------------------------------------------------------------

def test_verify_chain_happy():
    priv, pub = generate_keypair()
    p1 = _make_payload(generation=1)
    b1, s1 = _sign(priv, p1)
    p2 = _make_payload(generation=2, prev_hash=payload_hash(b1))
    b2, s2 = _sign(priv, p2)
    p3 = _make_payload(generation=3, prev_hash=payload_hash(b2))
    b3, s3 = _sign(priv, p3)
    chain = verify_chain([(b1, s1), (b2, s2), (b3, s3)], [TrustedKey(1, pub)], NOW)
    assert [p.generation for p in chain] == [1, 2, 3]


def test_verify_chain_tampered_middle_fails():
    priv, pub = generate_keypair()
    p1 = _make_payload(generation=1)
    b1, s1 = _sign(priv, p1)
    p2 = _make_payload(generation=2, prev_hash=payload_hash(b1))
    b2_orig, s2 = _sign(priv, p2)
    # Corrupt generation 2 blob
    b2_bad = bytearray(b2_orig)
    b2_bad[5] ^= 0x01
    p3 = _make_payload(generation=3, prev_hash=payload_hash(b2_orig))
    b3, s3 = _sign(priv, p3)
    with pytest.raises(VerifyError):
        verify_chain([(b1, s1), (bytes(b2_bad), s2), (b3, s3)], [TrustedKey(1, pub)], NOW)


def test_verify_chain_multikey_rotation():
    priv1, pub1 = generate_keypair()
    priv2, pub2 = generate_keypair()
    p1 = _make_payload(generation=1, signing_key_gen=1)
    b1, s1 = _sign(priv1, p1)
    p2 = _make_payload(generation=2, prev_hash=payload_hash(b1), signing_key_gen=2)
    b2, s2 = _sign(priv2, p2)
    trusted = [TrustedKey(1, pub1), TrustedKey(2, pub2)]
    chain = verify_chain([(b1, s1), (b2, s2)], trusted, NOW)
    assert chain[1].signing_key_gen == 2
