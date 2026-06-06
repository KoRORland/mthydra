"""Tests for tls_fingerprints in sign_new_descriptor (V1 Task 5, invariant #33)."""
import pytest

from mthydra.controller.state.db import connect
from mthydra.controller.state.descriptor import insert_signing_key
from mthydra.controller.state.schema import apply_schema
from mthydra.descriptor.keys import generate_keypair
from mthydra.descriptor.payload import DescriptorPayload
from mthydra.descriptor.sign import SignError, sign_new_descriptor


def _seeded_db(tmp_path):
    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    priv, pub = generate_keypair()
    insert_signing_key(conn, 1, priv, pub, "2026-05-19T00:00:00Z")
    return conn, pub


def test_sign_with_known_fingerprints(tmp_path):
    """Sign with valid fingerprints; parsed payload carries them back."""
    conn, _ = _seeded_db(tmp_path)
    fps = (("chrome", 60), ("firefox", 40))
    _, blob, _ = sign_new_descriptor(
        conn,
        now_iso="2026-06-06T00:00:00Z",
        valid_until_iso="2026-06-07T00:00:00Z",
        tls_fingerprints=fps,
    )
    parsed = DescriptorPayload.from_canonical_bytes(blob)
    assert parsed.tls_fingerprints == fps


def test_sign_with_unknown_fingerprint_raises(tmp_path):
    """Invariant #33: unknown uTLS fingerprint must raise SignError before touching DB."""
    conn, _ = _seeded_db(tmp_path)
    with pytest.raises(SignError, match="invariant #33"):
        sign_new_descriptor(
            conn,
            now_iso="2026-06-06T00:00:00Z",
            valid_until_iso="2026-06-07T00:00:00Z",
            tls_fingerprints=(("nessuno", 1),),
        )


def test_sign_without_fingerprints_omits_key(tmp_path):
    """When tls_fingerprints kwarg is omitted, the canonical blob must not contain the key."""
    conn, _ = _seeded_db(tmp_path)
    _, blob, _ = sign_new_descriptor(
        conn,
        now_iso="2026-06-06T00:00:00Z",
        valid_until_iso="2026-06-07T00:00:00Z",
    )
    assert b"tls_fingerprints" not in blob
