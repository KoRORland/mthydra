"""Spec J integration — end-to-end alerter + heartbeat with fake sinks.

  1. Bootstrap; seed an anti-obligation (probe_kill_pending::b1).
  2. AlertSweep.run_once() → fake telegram + email each called once with crit.
  3. Re-run within dedupe window → no new dispatch (counts unchanged).
  4. Fail-fail-fail the heartbeat publisher → breach obligation set.
  5. Swap to passing sink + run again → breach cleared + obs_heartbeat_proven set.
"""
from __future__ import annotations

import pytest


from mthydra.controller.observability.alerter import AlertSweep
from mthydra.controller.observability.heartbeat import ObsHeartbeatPublisher
from mthydra.controller.observability.sinks import DryRunSink, SinkResult
from mthydra.controller.state.db import connect
from mthydra.controller.state.obligations import set_obligation
from mthydra.controller.state.schema import apply_schema


NOW = "2026-05-25T12:00:00Z"
LATER = "2026-05-25T12:01:00Z"
MUCH_LATER = "2026-05-25T15:00:00Z"

_DEDUPE = {"warn": 3600, "crit": 900, "info": 21600}


def test_observability_lifecycle_anti_obligation_then_dedupe(tmp_path):
    db = tmp_path / "state.sqlite"
    c = connect(db)
    apply_schema(c)
    # 1. Seed a crit anti-obligation.
    set_obligation(c,
                   obligation_id="probe_kill_pending::b1",
                   last_proven_at=NOW, proven_by="x",
                   next_due_at=NOW, details='{"verdict":"hard_kill"}')
    c.close()

    tg = DryRunSink(label="telegram")
    em = DryRunSink(label="email")
    sweep = AlertSweep(
        db_path=db,
        telegram_sink=tg, email_sink=em,
        sweep_interval_seconds=120,
        dedupe_window_seconds=_DEDUPE,
        mode="production", clock=lambda: NOW,
    )

    # 2. First run: dispatches crit to both sinks.
    res = sweep.run_once()
    assert res["dispatched"] == 2
    assert len(tg.calls) == 1
    assert len(em.calls) == 1
    assert tg.calls[0].severity == "crit"

    # 3. Re-run within dedupe window (1m, crit window 15m): no new dispatch.
    sweep._clock = lambda: LATER
    sweep.run_once()
    assert len(tg.calls) == 1
    assert len(em.calls) == 1

    # 4. Re-run past window: re-dispatches.
    sweep._clock = lambda: MUCH_LATER
    sweep.run_once()
    crit_calls = [c for c in tg.calls if c.dedupe_key == "probe_kill_pending::b1"]
    assert len(crit_calls) == 2


def test_observability_lifecycle_heartbeat_breach_then_recovery(tmp_path):
    db = tmp_path / "state.sqlite"
    c = connect(db)
    apply_schema(c)
    c.close()

    class _Failing:
        def __call__(self, payload):
            return SinkResult(sink="email", success=False, error="smtp 530")

    pub = ObsHeartbeatPublisher(
        db_path=db,
        email_sink=_Failing(),
        interval_seconds=3600,
        breach_threshold=3,
        mode="production",
        clock=lambda: NOW,
    )
    # 4. 3 consecutive failures -> breach obligation.
    pub.run_once()
    pub.run_once()
    pub.run_once()
    c = connect(db)
    row = c.execute(
        "SELECT details FROM obligation_clocks "
        "WHERE obligation_id='obs_dead_mans_switch_breach'"
    ).fetchone()
    assert row is not None
    c.close()

    # 5. Swap to passing sink, run again -> breach cleared.
    pub.email_sink = DryRunSink(label="email")
    pub.run_once()
    c = connect(db)
    n_breach = c.execute(
        "SELECT COUNT(*) FROM obligation_clocks "
        "WHERE obligation_id='obs_dead_mans_switch_breach'"
    ).fetchone()[0]
    assert n_breach == 0
    proof = c.execute(
        "SELECT last_proven_at FROM obligation_clocks "
        "WHERE obligation_id='obs_heartbeat_proven'"
    ).fetchone()
    assert proof[0] == NOW
    c.close()


def test_alert_log_records_every_attempt(tmp_path):
    db = tmp_path / "state.sqlite"
    c = connect(db)
    apply_schema(c)
    set_obligation(c,
                   obligation_id="probe_kill_pending::b1",
                   last_proven_at=NOW, proven_by="x",
                   next_due_at=NOW, details=None)
    set_obligation(c,
                   obligation_id="cover_pool_rotation_pending::example.com",
                   last_proven_at=NOW, proven_by="x",
                   next_due_at=NOW, details=None)
    c.close()
    sweep = AlertSweep(
        db_path=db,
        telegram_sink=DryRunSink(label="telegram"),
        email_sink=DryRunSink(label="email"),
        sweep_interval_seconds=120,
        dedupe_window_seconds=_DEDUPE,
        mode="production", clock=lambda: NOW,
    )
    sweep.run_once()
    c = connect(db)
    n = c.execute("SELECT COUNT(*) FROM alert_log").fetchone()[0]
    # crit -> 2 routes (telegram+email); warn -> 1 route (telegram). Total 3.
    assert n == 3
    delivered = c.execute(
        "SELECT COUNT(*) FROM alert_log WHERE delivered_at IS NOT NULL"
    ).fetchone()[0]
    assert delivered == 3
    c.close()
