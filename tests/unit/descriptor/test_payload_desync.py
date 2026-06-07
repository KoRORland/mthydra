import pytest

from mthydra.descriptor.payload import (
    DescriptorPayload, EUExit, SCHEMA_V3, canonical_bytes,
)


def _payload(desync_strategy):
    return DescriptorPayload(
        generation=2,
        signing_key_gen=1,
        issued_at="2026-06-06T00:00:00Z",
        valid_until="2026-06-07T00:00:00Z",
        eu_exit_set=(EUExit("fp1", "1.2.3.4:443", 1, "cover.example", "pub=="),),
        previous_generation_hash="abc",
        next_signing_pubkey=None,
        schema=SCHEMA_V3,
        desync_strategy=desync_strategy,
    )


def test_v3_roundtrips_desync_strategy():
    strategy = "--dpi-desync=fake,split2 --dpi-desync-ttl=4"
    blob = canonical_bytes(_payload(strategy))
    parsed = DescriptorPayload.from_canonical_bytes(blob)
    assert parsed.schema == SCHEMA_V3
    assert parsed.desync_strategy == strategy
    assert canonical_bytes(parsed) == blob


def test_v3_none_desync_strategy_omitted_from_blob():
    blob = canonical_bytes(_payload(None))
    assert b"desync_strategy" not in blob
    assert DescriptorPayload.from_canonical_bytes(blob).desync_strategy is None


def test_v2_blob_with_desync_strategy_rejected():
    blob = (
        '{"desync_strategy":"--dpi-desync=fake","eu_exit_set":[],"generation":1,'
        '"issued_at":"x","next_signing_pubkey":null,"previous_generation_hash":null,'
        '"schema":"mthydra.descriptor.v2","signing_key_gen":1,"valid_until":"y"}'
    ).encode("utf-8")
    with pytest.raises(ValueError, match="desync_strategy only valid in v3"):
        DescriptorPayload.from_canonical_bytes(blob)
