import pytest

from mthydra.descriptor.payload import (
    SCHEMA_V3,
    DescriptorPayload,
    EUExit,
    canonical_bytes,
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
        tls_fingerprints=fps,
    )


def test_v3_roundtrips_fingerprints():
    blob = canonical_bytes(_payload((("chrome", 60), ("firefox", 40))))
    parsed = DescriptorPayload.from_canonical_bytes(blob)
    assert parsed.schema == SCHEMA_V3
    assert parsed.tls_fingerprints == (("chrome", 60), ("firefox", 40))
    assert canonical_bytes(parsed) == blob


def test_v3_fingerprints_sorted_canonically():
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


def test_v3_empty_fingerprints_roundtrips_to_none():
    p = _payload(())
    assert DescriptorPayload.from_canonical_bytes(canonical_bytes(p)).tls_fingerprints is None


def test_v2_blob_with_tls_fingerprints_rejected():
    blob = (
        b'{"eu_exit_set":[],"generation":1,"issued_at":"x","next_signing_pubkey":null,'
        b'"previous_generation_hash":null,"schema":"mthydra.descriptor.v2",'
        b'"signing_key_gen":1,"tls_fingerprints":[{"fp":"chrome","weight":60}],'
        b'"valid_until":"y"}'
    )
    with pytest.raises(ValueError, match="tls_fingerprints only valid in v3"):
        DescriptorPayload.from_canonical_bytes(blob)


def test_v2_blob_parses_with_none_fingerprints():
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
