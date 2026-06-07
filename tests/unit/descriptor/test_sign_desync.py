"""Tests for desync_strategy emission in sign_new_descriptor (V2 Task 7, invariant #36)."""
from __future__ import annotations

from mthydra.controller.state import desync_strategy as ds
from mthydra.controller.state.db import connect
from mthydra.controller.state.descriptor import insert_signing_key
from mthydra.controller.state.schema import apply_schema
from mthydra.descriptor.keys import generate_keypair
from mthydra.descriptor.payload import DescriptorPayload
from mthydra.descriptor.sign import sign_new_descriptor


def _seeded_db(tmp_path):
    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    priv, pub = generate_keypair()
    insert_signing_key(conn, 1, priv, pub, "2026-05-19T00:00:00Z")
    return conn, pub


def test_sign_emits_live_canary_proven_desync_strategy(tmp_path):
    """A staged-then-proven-then-promoted strategy is emitted as live in the descriptor."""
    conn, _ = _seeded_db(tmp_path)
    strategy = "fake_tcp;param=1"
    ds.stage(conn, strategy, at="2026-06-06T00:00:00Z")
    ds.mark_canary_proven(conn, strategy, at="2026-06-06T00:01:00Z")
    ds.promote(conn, at="2026-06-06T00:02:00Z")

    _, blob, _ = sign_new_descriptor(
        conn,
        now_iso="2026-06-06T00:03:00Z",
        valid_until_iso="2026-06-07T00:00:00Z",
    )
    parsed = DescriptorPayload.from_canonical_bytes(blob)
    assert parsed.desync_strategy == strategy


def test_sign_without_live_strategy_omits_desync_strategy(tmp_path):
    """No live strategy → parsed payload has desync_strategy None and key absent from blob."""
    conn, _ = _seeded_db(tmp_path)

    _, blob, _ = sign_new_descriptor(
        conn,
        now_iso="2026-06-06T00:03:00Z",
        valid_until_iso="2026-06-07T00:00:00Z",
    )
    parsed = DescriptorPayload.from_canonical_bytes(blob)
    assert parsed.desync_strategy is None
    assert b"desync_strategy" not in blob
