"""Property: descriptor chain integrity across random operations (spec B §13.3)."""
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from mthydra.controller.state.db import connect
from mthydra.controller.state.descriptor import insert_signing_key
from mthydra.controller.state.eu_exit_set import add_exit, list_active, retire_exit
from mthydra.controller.state.schema import apply_schema
from mthydra.descriptor.keys import generate_keypair
from mthydra.descriptor.payload import canonical_bytes, payload_hash
from mthydra.descriptor.sign import sign_new_descriptor
from mthydra.descriptor.verify import TrustedKey, verify_chain

NOW = "2026-05-19T00:00:00Z"
VALID_UNTIL = "2026-05-19T12:00:00Z"

# Simple operations to apply to the DB
_OPERATIONS = st.sampled_from(["sign", "eu-add", "eu-retire"])


@settings(max_examples=30, deadline=None)
@given(ops=st.lists(_OPERATIONS, min_size=2, max_size=8))
def test_descriptor_chain_integrity(tmp_path_factory, ops):
    db = tmp_path_factory.mktemp("chain") / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    priv, pub = generate_keypair()
    insert_signing_key(conn, 1, priv, pub, NOW)

    blobs_sigs: list[tuple[bytes, bytes]] = []
    counter = 0

    for op in ops:
        if op == "sign":
            _, blob, sig = sign_new_descriptor(conn, now_iso=NOW, valid_until_iso=VALID_UNTIL)
            blobs_sigs.append((blob, sig))
        elif op == "eu-add":
            fp = f"fp{counter:04d}"
            counter += 1
            add_exit(conn, fp, f"eu{counter}.example.org:443", 1, NOW)
            _, blob, sig = sign_new_descriptor(conn, now_iso=NOW, valid_until_iso=VALID_UNTIL)
            blobs_sigs.append((blob, sig))
        elif op == "eu-retire":
            active = list_active(conn)
            if active:
                retire_exit(conn, active[0].fingerprint, at=NOW)
                _, blob, sig = sign_new_descriptor(conn, now_iso=NOW, valid_until_iso=VALID_UNTIL)
                blobs_sigs.append((blob, sig))

    if not blobs_sigs:
        return  # no operations produced descriptors

    trusted = [TrustedKey(generation=1, pubkey=pub)]
    chain = verify_chain(blobs_sigs, trusted, NOW)

    # Invariant 1: chain length matches number of descriptors produced
    assert len(chain) == len(blobs_sigs)

    # Invariant 2: generations are monotonically increasing
    gens = [p.generation for p in chain]
    assert gens == sorted(gens)
    assert gens == list(range(1, len(chain) + 1))

    # Invariant 3: each descriptor's previous_generation_hash links correctly
    for i, (p, (blob, _)) in enumerate(zip(chain, blobs_sigs)):
        if i == 0:
            assert p.previous_generation_hash is None
        else:
            prev_blob = blobs_sigs[i - 1][0]
            assert p.previous_generation_hash == payload_hash(prev_blob)
