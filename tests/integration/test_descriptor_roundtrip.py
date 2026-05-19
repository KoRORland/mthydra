"""Integration: full sign → store → fetch → verify cycle (spec B §13.2)."""
from mthydra.controller.state.db import connect
from mthydra.controller.state.descriptor import insert_signing_key, latest_descriptor_with_signature
from mthydra.controller.state.eu_exit_set import add_exit
from mthydra.controller.state.schema import apply_schema
from mthydra.descriptor.keys import generate_keypair
from mthydra.descriptor.payload import DescriptorPayload, payload_hash
from mthydra.descriptor.sign import sign_new_descriptor
from mthydra.descriptor.verify import TrustedKey, verify_descriptor

NOW = "2026-05-19T00:00:00Z"
VALID_UNTIL = "2026-05-19T01:00:00Z"


def test_sign_and_verify_roundtrip(tmp_path):
    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    priv, pub = generate_keypair()
    insert_signing_key(conn, 1, priv, pub, NOW)
    add_exit(conn, "fp1", "eu1.example.org:443", 1, NOW)
    add_exit(conn, "fp2", "eu2.example.org:443", 2, NOW)

    gen1, blob1, sig1 = sign_new_descriptor(conn, now_iso=NOW, valid_until_iso=VALID_UNTIL)
    assert gen1 == 1

    trusted = [TrustedKey(generation=1, pubkey=pub)]
    p1 = verify_descriptor(blob1, sig1, trusted, NOW)
    assert p1.generation == 1
    assert len(p1.eu_exit_set) == 2
    assert p1.previous_generation_hash is None

    gen2, blob2, sig2 = sign_new_descriptor(conn, now_iso=NOW, valid_until_iso=VALID_UNTIL)
    assert gen2 == 2
    p2 = verify_descriptor(blob2, sig2, trusted, NOW,
                           previous_descriptor_hash=payload_hash(blob1))
    assert p2.generation == 2
    assert p2.previous_generation_hash == payload_hash(blob1)


def test_chain_links_across_three_signs(tmp_path):
    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    priv, pub = generate_keypair()
    insert_signing_key(conn, 1, priv, pub, NOW)
    trusted = [TrustedKey(generation=1, pubkey=pub)]

    blobs_sigs = []
    for _ in range(3):
        _, blob, sig = sign_new_descriptor(conn, now_iso=NOW, valid_until_iso=VALID_UNTIL)
        blobs_sigs.append((blob, sig))

    from mthydra.descriptor.verify import verify_chain
    chain = verify_chain(blobs_sigs, trusted, NOW)
    assert [p.generation for p in chain] == [1, 2, 3]
