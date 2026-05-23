"""Tests for descriptor payload model and canonical encoding (spec B §4)."""
import json

import pytest

from mthydra.descriptor.payload import (
    SCHEMA,
    SCHEMA_V1,
    SCHEMA_V2,
    DescriptorPayload,
    EUExit,
    canonical_bytes,
    payload_hash,
)

_GEN1 = DescriptorPayload(
    generation=1,
    signing_key_gen=1,
    issued_at="2026-05-19T00:00:00Z",
    valid_until="2026-05-19T01:00:00Z",
    eu_exit_set=(EUExit("fp1", "eu1.example.org:443", 1),),
    previous_generation_hash=None,
    next_signing_pubkey=None,
)


def test_roundtrip():
    blob = canonical_bytes(_GEN1)
    p2 = DescriptorPayload.from_canonical_bytes(blob)
    assert p2 == _GEN1


def test_encoding_stability():
    b1 = canonical_bytes(_GEN1)
    b2 = canonical_bytes(_GEN1)
    assert b1 == b2


def test_keys_are_sorted():
    blob = canonical_bytes(_GEN1)
    text = blob.decode("utf-8")
    obj = json.loads(text)
    keys = list(obj.keys())
    assert keys == sorted(keys)


def test_eu_exit_set_keys_sorted():
    blob = canonical_bytes(_GEN1)
    obj = json.loads(blob)
    exit_keys = list(obj["eu_exit_set"][0].keys())
    assert exit_keys == sorted(exit_keys)


def test_no_whitespace_in_encoding():
    blob = canonical_bytes(_GEN1)
    assert b" " not in blob
    assert b"\n" not in blob


def test_unknown_field_raises():
    blob = canonical_bytes(_GEN1)
    obj = json.loads(blob)
    obj["unexpected_field"] = "oops"
    bad_blob = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    with pytest.raises(ValueError, match="unknown fields"):
        DescriptorPayload.from_canonical_bytes(bad_blob)


def test_schema_mismatch_raises():
    blob = canonical_bytes(_GEN1)
    obj = json.loads(blob)
    obj["schema"] = "wrong.schema.v99"
    bad_blob = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    with pytest.raises(ValueError, match="schema mismatch"):
        DescriptorPayload.from_canonical_bytes(bad_blob)


def test_payload_hash_is_64_char_hex():
    blob = canonical_bytes(_GEN1)
    h = payload_hash(blob)
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_payload_hash_changes_with_content():
    gen2 = DescriptorPayload(
        generation=2,
        signing_key_gen=1,
        issued_at="2026-05-19T00:01:00Z",
        valid_until="2026-05-19T01:01:00Z",
        eu_exit_set=(),
        previous_generation_hash=payload_hash(canonical_bytes(_GEN1)),
        next_signing_pubkey=None,
    )
    assert payload_hash(canonical_bytes(_GEN1)) != payload_hash(canonical_bytes(gen2))


def test_empty_eu_exit_set_round_trips():
    p = DescriptorPayload(
        generation=1,
        signing_key_gen=1,
        issued_at="2026-05-19T00:00:00Z",
        valid_until="2026-05-19T01:00:00Z",
        eu_exit_set=(),
        previous_generation_hash=None,
        next_signing_pubkey=None,
    )
    assert DescriptorPayload.from_canonical_bytes(canonical_bytes(p)) == p


def test_default_schema_is_v2():
    assert SCHEMA == SCHEMA_V2
    assert _GEN1.schema == SCHEMA_V2


def test_v2_per_exit_includes_cover_sni_and_reality_pubkey_keys():
    blob = canonical_bytes(_GEN1)
    obj = json.loads(blob)
    keys = set(obj["eu_exit_set"][0].keys())
    assert {"cover_sni", "reality_pubkey"} <= keys


def test_v2_round_trips_with_per_exit_fields():
    p = DescriptorPayload(
        generation=1,
        signing_key_gen=1,
        issued_at="2026-05-19T00:00:00Z",
        valid_until="2026-05-19T01:00:00Z",
        eu_exit_set=(
            EUExit("fp1", "eu1.example.org:443", 1,
                   cover_sni="cov.example", reality_pubkey="PK"),
        ),
        previous_generation_hash=None,
        next_signing_pubkey=None,
        schema=SCHEMA_V2,
    )
    p2 = DescriptorPayload.from_canonical_bytes(canonical_bytes(p))
    assert p2 == p
    assert p2.eu_exit_set[0].cover_sni == "cov.example"
    assert p2.eu_exit_set[0].reality_pubkey == "PK"


def test_v1_round_trips_without_per_exit_fields():
    p = DescriptorPayload(
        generation=1,
        signing_key_gen=1,
        issued_at="2026-05-19T00:00:00Z",
        valid_until="2026-05-19T01:00:00Z",
        eu_exit_set=(EUExit("fp1", "eu1.example.org:443", 1),),
        previous_generation_hash=None,
        next_signing_pubkey=None,
        schema=SCHEMA_V1,
    )
    blob = canonical_bytes(p)
    obj = json.loads(blob)
    assert obj["schema"] == SCHEMA_V1
    assert "cover_sni" not in obj["eu_exit_set"][0]
    assert "reality_pubkey" not in obj["eu_exit_set"][0]
    p2 = DescriptorPayload.from_canonical_bytes(blob)
    assert p2 == p


def test_unknown_eu_exit_field_raises():
    blob = canonical_bytes(_GEN1)
    obj = json.loads(blob)
    obj["eu_exit_set"][0]["mystery"] = "field"
    bad_blob = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    with pytest.raises(ValueError, match="unknown fields in eu_exit entry"):
        DescriptorPayload.from_canonical_bytes(bad_blob)
