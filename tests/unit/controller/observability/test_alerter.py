"""Tests for observability.alerter — AlertSweep."""
from __future__ import annotations

import pytest

from mthydra.controller.observability.alerter import AlertSweep
from mthydra.controller.observability.sinks import (
    AlertPayload,
    DryRunSink,
    SinkResult,
)
from mthydra.controller.state.db import connect
from mthydra.controller.state.obligations import set_obligation
from mthydra.controller.state.schema import apply_schema


NOW = "2026-05-25T12:00:00Z"
LATER = "2026-05-25T12:01:00Z"
MUCH_LATER = "2026-05-25T15:00:00Z"

_DEDUPE = {"warn": 3600, "crit": 900, "info": 21600}


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "state.sqlite"
    c = connect(p)
    apply_schema(c)
    c.close()
    return p


def _sweep(db, tg=None, em=None, clock=NOW, mode="production"):
    return AlertSweep(
        db_path=db,
        telegram_sink=tg if tg is not None else DryRunSink(label="telegram"),
        email_sink=em if em is not None else DryRunSink(label="email"),
        sweep_interval_seconds=120,
        dedupe_window_seconds=_DEDUPE,
        mode=mode, clock=lambda: clock,
    )


def test_sweep_emits_crit_to_both_sinks(db):
    conn = connect(db)
    set_obligation(conn,
                   obligation_id="probe_kill_pending::b1",
                   last_proven_at=NOW, proven_by="x",
                   next_due_at=NOW, details=None)
    conn.close()
    tg = DryRunSink(label="telegram")
    em = DryRunSink(label="email")
    sw = _sweep(db, tg=tg, em=em)
    res = sw.run_once()
    assert res["dispatched"] == 2  # one to each sink
    assert len(tg.calls) == 1
    assert len(em.calls) == 1
    assert tg.calls[0].severity == "crit"


def test_sweep_emits_warn_telegram_only(db):
    conn = connect(db)
    set_obligation(conn,
                   obligation_id="cover_pool_rotation_pending::example.com",
                   last_proven_at=NOW, proven_by="x",
                   next_due_at=NOW, details=None)
    conn.close()
    tg = DryRunSink(label="telegram")
    em = DryRunSink(label="email")
    sw = _sweep(db, tg=tg, em=em)
    sw.run_once()
    assert len(tg.calls) == 1
    assert tg.calls[0].severity == "warn"
    assert len(em.calls) == 0


def test_sweep_skips_info(db):
    conn = connect(db)
    set_obligation(conn,
                   obligation_id="shard_unassigned_pending::u1",
                   last_proven_at=NOW, proven_by="x",
                   next_due_at=NOW, details=None)
    conn.close()
    tg = DryRunSink(label="telegram")
    em = DryRunSink(label="email")
    sw = _sweep(db, tg=tg, em=em)
    sw.run_once()
    assert tg.calls == []
    assert em.calls == []


def test_dedupe_blocks_immediate_repeat(db):
    conn = connect(db)
    set_obligation(conn,
                   obligation_id="probe_kill_pending::b1",
                   last_proven_at=NOW, proven_by="x",
                   next_due_at=NOW, details=None)
    conn.close()
    tg = DryRunSink(label="telegram")
    em = DryRunSink(label="email")
    sw = _sweep(db, tg=tg, em=em)
    sw.run_once()
    # Second run within crit dedupe window (15m) — no new dispatch.
    sw._clock = lambda: LATER
    sw.run_once()
    assert len(tg.calls) == 1
    assert len(em.calls) == 1


def test_dedupe_window_expiry_re_emits(db):
    conn = connect(db)
    set_obligation(conn,
                   obligation_id="probe_kill_pending::b1",
                   last_proven_at=NOW, proven_by="x",
                   next_due_at=NOW, details=None)
    conn.close()
    tg = DryRunSink(label="telegram")
    em = DryRunSink(label="email")
    sw = _sweep(db, tg=tg, em=em)
    sw.run_once()
    sw._clock = lambda: MUCH_LATER  # 3h later, past 15m crit window
    sw.run_once()
    crit = [c for c in tg.calls if c.dedupe_key == "probe_kill_pending::b1"]
    assert len(crit) == 2  # re-emitted the same key after window expiry


def test_sink_failure_recorded(db):
    conn = connect(db)
    set_obligation(conn,
                   obligation_id="probe_kill_pending::b1",
                   last_proven_at=NOW, proven_by="x",
                   next_due_at=NOW, details=None)
    conn.close()

    class _FailingSink:
        def __call__(self, payload):
            return SinkResult(sink="telegram", success=False,
                              error="http 401")

    sw = _sweep(db, tg=_FailingSink(), em=DryRunSink(label="email"))
    res = sw.run_once()
    assert res["dispatched"] == 1  # email succeeded; telegram failed
    conn = connect(db)
    row = conn.execute(
        "SELECT sink, delivered_at, error FROM alert_log "
        "WHERE sink='telegram' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row[1] is None         # delivered_at NULL on failure
    assert "http 401" in row[2]
    conn.close()


def test_heartbeat_obligation_proven(db):
    sw = _sweep(db)
    sw.run_once()
    conn = connect(db)
    row = conn.execute(
        "SELECT last_proven_at FROM obligation_clocks "
        "WHERE obligation_id='obs_alerter_sweep_ran'"
    ).fetchone()
    assert row[0] == NOW
    conn.close()


def test_overdue_obligation_emits_warn(db):
    conn = connect(db)
    set_obligation(conn,
                   obligation_id="probe_audit_sweep_ran",
                   last_proven_at="2026-05-25T10:00:00Z",
                   proven_by="x",
                   next_due_at="2026-05-25T11:30:00Z",
                   details=None)
    conn.close()
    tg = DryRunSink(label="telegram")
    em = DryRunSink(label="email")
    sw = _sweep(db, tg=tg, em=em)  # NOW = 12:00 -> overdue 30m, cadence 1h -> warn
    sw.run_once()
    assert len(tg.calls) == 1
    assert tg.calls[0].severity == "warn"
    assert tg.calls[0].kind == "obligation_overdue"


def test_offline_mode_does_not_arm(db):
    sw = _sweep(db, mode="offline")
    sw.arm()
    assert sw._scheduler is None
    sw.disarm()


def test_offline_mode_dispatches_via_dryrun(db):
    """In offline mode, sinks are replaced with a DryRunSink — alert_log still records the attempt."""
    conn = connect(db)
    set_obligation(conn,
                   obligation_id="probe_kill_pending::b1",
                   last_proven_at=NOW, proven_by="x",
                   next_due_at=NOW, details=None)
    conn.close()
    sw = _sweep(db, mode="offline")
    res = sw.run_once()
    # crit -> two routes (telegram + email) both routed to the offline dryrun.
    assert res["dispatched"] == 2
    conn = connect(db)
    n = conn.execute(
        "SELECT COUNT(*) FROM alert_log WHERE sink='telegram'"
    ).fetchone()[0]
    assert n == 1
    conn.close()


def test_arm_and_disarm_production(db):
    sw = AlertSweep(
        db_path=db,
        telegram_sink=DryRunSink(label="telegram"),
        email_sink=DryRunSink(label="email"),
        sweep_interval_seconds=86400,
        dedupe_window_seconds=_DEDUPE,
        mode="production",
        clock=lambda: NOW,
    )
    sw.arm()
    assert sw._scheduler is not None
    sw.disarm()
    assert sw._scheduler is None
