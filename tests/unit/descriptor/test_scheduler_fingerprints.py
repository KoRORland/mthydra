"""Tests for tls_fingerprints threading through DescriptorRotator (V1 Task 7)."""
import pytest

from mthydra.controller.state.db import connect
from mthydra.controller.state.descriptor import (
    insert_signing_key,
    latest_descriptor_with_signature,
)
from mthydra.controller.state.schema import apply_schema
from mthydra.descriptor.keys import generate_keypair
from mthydra.descriptor.payload import DescriptorPayload
from mthydra.descriptor.scheduler import DescriptorRotator


@pytest.fixture(autouse=True)
def _teardown_rotators():
    rotators: list[DescriptorRotator] = []
    _orig_arm = DescriptorRotator.arm

    def _tracking_arm(self, *a, **kw):
        rotators.append(self)
        return _orig_arm(self, *a, **kw)

    DescriptorRotator.arm = _tracking_arm
    yield
    DescriptorRotator.arm = _orig_arm
    for r in rotators:
        try:
            r.disarm()
        except Exception:
            pass


def _seeded_db(tmp_path):
    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    priv, pub = generate_keypair()
    insert_signing_key(conn, 1, priv, pub, "2026-05-19T00:00:00Z")
    conn.close()
    return db


def test_rotator_threads_tls_fingerprints_into_signed_descriptor(tmp_path):
    db = _seeded_db(tmp_path)
    fps = (("chrome", 60), ("safari", 40))
    r = DescriptorRotator(
        db,
        rotation_interval_seconds=3600,
        validity_window_seconds=86400,
        mode="offline",
        tls_fingerprints=fps,
    )
    gen = r.sign_now()
    assert gen == 1

    conn = connect(db)
    try:
        row = latest_descriptor_with_signature(conn)
        assert row is not None
        payload = DescriptorPayload.from_canonical_bytes(row[1])
        assert payload.tls_fingerprints == fps
    finally:
        conn.close()
