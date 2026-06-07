"""Tests for controller desync-strategy state + canary gate (spec V V-D6, invariant #36)."""
from __future__ import annotations

import pytest

from mthydra.controller.state import desync_strategy as ds
from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema


def _db(tmp_path):
    c = connect(tmp_path / "s.sqlite")
    apply_schema(c)
    return c


def test_stage_then_promote_with_proof_succeeds(tmp_path):
    c = _db(tmp_path)
    assert ds.staged(c) is None
    assert ds.live(c) is None

    ds.stage(c, "fake_tcp;param=1", at="2026-06-06T10:00:00Z")
    assert ds.staged(c) == "fake_tcp;param=1"
    assert ds.live(c) is None

    ds.mark_canary_proven(c, "fake_tcp;param=1", at="2026-06-06T10:05:00Z")
    ds.promote(c, at="2026-06-06T10:10:00Z")
    assert ds.live(c) == "fake_tcp;param=1"


def test_promote_without_proof_raises_canary_gate_error(tmp_path):
    c = _db(tmp_path)
    ds.stage(c, "fake_tcp;param=1", at="2026-06-06T10:00:00Z")
    with pytest.raises(ds.CanaryGateError, match="invariant #36"):
        ds.promote(c, at="2026-06-06T10:10:00Z")
    assert ds.live(c) is None


def test_promote_refused_when_proven_hash_is_for_different_strategy(tmp_path):
    c = _db(tmp_path)
    ds.stage(c, "fake_tcp;param=1", at="2026-06-06T10:00:00Z")
    ds.mark_canary_proven(c, "different_strategy;param=2", at="2026-06-06T10:05:00Z")
    with pytest.raises(ds.CanaryGateError, match="invariant #36"):
        ds.promote(c, at="2026-06-06T10:10:00Z")
    assert ds.live(c) is None


def test_restaging_after_proof_invalidates_promotion(tmp_path):
    c = _db(tmp_path)
    ds.stage(c, "fake_tcp;param=1", at="2026-06-06T10:00:00Z")
    ds.mark_canary_proven(c, "fake_tcp;param=1", at="2026-06-06T10:05:00Z")
    ds.stage(c, "fake_tcp;param=2", at="2026-06-06T10:06:00Z")
    with pytest.raises(ds.CanaryGateError, match="invariant #36"):
        ds.promote(c, at="2026-06-06T10:10:00Z")
    assert ds.live(c) is None
