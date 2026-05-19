"""Tests for DescriptorRotator (spec B §8 R1)."""
import threading
import time

import pytest

from mthydra.controller.state.db import connect
from mthydra.controller.state.descriptor import insert_signing_key, next_descriptor_generation
from mthydra.controller.state.schema import apply_schema
from mthydra.descriptor.keys import generate_keypair
from mthydra.descriptor.scheduler import DescriptorRotator


@pytest.fixture(autouse=True)
def _teardown_rotators():
    """Guarantee any DescriptorRotator started in a test is disarmed afterwards."""
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


def test_arm_and_disarm_no_error(tmp_path):
    db = _seeded_db(tmp_path)
    r = DescriptorRotator(db, rotation_interval_seconds=3600, validity_window_seconds=86400)
    r.arm()
    assert r._scheduler is not None
    r.disarm()
    assert r._scheduler is None


def test_offline_mode_arm_is_noop(tmp_path):
    db = _seeded_db(tmp_path)
    r = DescriptorRotator(db, rotation_interval_seconds=3600, validity_window_seconds=86400,
                           mode="offline")
    r.arm()
    assert r._scheduler is None


def test_sign_now_creates_descriptor(tmp_path):
    db = _seeded_db(tmp_path)
    r = DescriptorRotator(db, rotation_interval_seconds=3600, validity_window_seconds=86400)
    gen = r.sign_now()
    assert gen == 1
    conn = connect(db)
    assert next_descriptor_generation(conn) == 2  # generation 1 consumed


def test_floor_timer_fires_via_real_scheduler(tmp_path):
    """Integration: a very short interval actually triggers a descriptor sign."""
    db = _seeded_db(tmp_path)
    fired = threading.Event()

    class TrackingRotator(DescriptorRotator):
        def _rotate(self):
            result = super()._rotate()
            fired.set()
            return result

    r = TrackingRotator(db, rotation_interval_seconds=0.1, validity_window_seconds=86400)
    r.arm()
    fired.wait(timeout=3.0)
    r.disarm()
    assert fired.is_set(), "descriptor rotator never fired"
    conn = connect(db)
    assert next_descriptor_generation(conn) >= 2
