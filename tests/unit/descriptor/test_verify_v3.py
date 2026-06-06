"""Task 2 — Verifier accepts v3 descriptor with tls_fingerprints."""
from mthydra.descriptor.keys import generate_keypair, sign as ed_sign
from mthydra.descriptor.payload import DescriptorPayload, SCHEMA_V3, canonical_bytes
from mthydra.descriptor.verify import TrustedKey, verify_descriptor


def test_verify_accepts_v3_with_fingerprints():
    priv, pub = generate_keypair()
    p = DescriptorPayload(
        generation=1, signing_key_gen=7,
        issued_at="2026-06-06T00:00:00Z", valid_until="2999-01-01T00:00:00Z",
        eu_exit_set=(), previous_generation_hash=None, next_signing_pubkey=None,
        schema=SCHEMA_V3, tls_fingerprints=(("chrome", 60), ("safari", 40)),
    )
    blob = canonical_bytes(p)
    sig = ed_sign(priv, blob)
    out = verify_descriptor(blob, sig, [TrustedKey(generation=7, pubkey=pub)],
                            now_iso="2026-06-06T01:00:00Z")
    assert out.tls_fingerprints == (("chrome", 60), ("safari", 40))
