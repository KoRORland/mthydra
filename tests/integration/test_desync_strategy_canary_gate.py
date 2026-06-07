"""Integration: desync-strategy canary gate end-to-end — staging, proving,
promotion, and the resulting effect on signed descriptors (spec V V-D6,
invariant #36)."""
from __future__ import annotations

import pytest

from mthydra.controller.state import desync_strategy as ds
from mthydra.controller.state.db import connect
from mthydra.controller.state.descriptor import insert_signing_key
from mthydra.controller.state.eu_exit_set import add_exit
from mthydra.controller.state.schema import apply_schema
from mthydra.descriptor.keys import generate_keypair
from mthydra.descriptor.payload import DescriptorPayload
from mthydra.descriptor.sign import sign_new_descriptor

NOW = "2026-06-06T00:00:00Z"
VALID_UNTIL = "2026-06-07T00:00:00Z"


def _seeded_db(tmp_path):
    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    priv, pub = generate_keypair()
    insert_signing_key(conn, 1, priv, pub, NOW)
    add_exit(conn, "fp1", "eu1.example.org:443", 1, NOW)
    return conn


def test_sign_before_promote_omits_strategy_then_carries_it_after_canary_gate(tmp_path):
    conn = _seeded_db(tmp_path)
    strategy = "--dpi-desync=fake --dpi-desync-fooling=md5sig"

    # Before any staging/promotion: signed descriptor carries no desync_strategy.
    _, blob_before, _ = sign_new_descriptor(conn, now_iso=NOW, valid_until_iso=VALID_UNTIL)
    payload_before = DescriptorPayload.from_canonical_bytes(blob_before)
    assert payload_before.desync_strategy is None
    assert b"desync_strategy" not in blob_before

    # Stage a candidate, run the canary, mark it proven, then promote (#36).
    ds.stage(conn, strategy, at="2026-06-06T01:00:00Z")
    ds.mark_canary_proven(conn, strategy, at="2026-06-06T02:00:00Z")
    ds.promote(conn, at="2026-06-06T03:00:00Z")

    # The next signed descriptor now carries the promoted (live) strategy.
    _, blob_after, _ = sign_new_descriptor(
        conn, now_iso="2026-06-06T04:00:00Z", valid_until_iso=VALID_UNTIL
    )
    payload_after = DescriptorPayload.from_canonical_bytes(blob_after)
    assert payload_after.desync_strategy == strategy


def test_promote_of_non_proven_candidate_raises_canary_gate_error(tmp_path):
    conn = _seeded_db(tmp_path)

    ds.stage(conn, "--dpi-desync=fake", at="2026-06-06T01:00:00Z")
    # Never marked canary-proven for this candidate.
    with pytest.raises(ds.CanaryGateError, match="invariant #36"):
        ds.promote(conn, at="2026-06-06T02:00:00Z")
    assert ds.live(conn) is None

    # And a descriptor signed at this point still carries no strategy.
    _, blob, _ = sign_new_descriptor(
        conn, now_iso="2026-06-06T03:00:00Z", valid_until_iso=VALID_UNTIL
    )
    payload = DescriptorPayload.from_canonical_bytes(blob)
    assert payload.desync_strategy is None
