"""Tests for observability.heartbeat — dead-man's switch publisher."""
from __future__ import annotations

import pytest

from mthydra.controller.observability.heartbeat import ObsHeartbeatPublisher
from mthydra.controller.observability.sinks import (
    AlertPayload,
    DryRunSink,
    SinkResult,
)
from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema


NOW = "2026-05-25T12:00:00Z"


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "state.sqlite"
    c = connect(p)
    apply_schema(c)
    c.close()
    return p


class _FailingSink:
    def __init__(self, err="connection refused"):
        self.err = err
        self.calls = 0

    def __call__(self, payload):
        self.calls += 1
        return SinkResult(sink="email", success=False, error=self.err)


def _pub(db, *, sink, clock=NOW, threshold=3):
    return ObsHeartbeatPublisher(
        db_path=db,
        email_sink=sink,
        interval_seconds=60,
        breach_threshold=threshold,
        mode="production",
        clock=lambda: clock,
    )


def test_success_dispatches_to_email_only_and_proves(db):
    em = DryRunSink(label="email")
    pub = _pub(db, sink=em)
    res = pub.run_once()
    assert res["success"]
    assert len(em.calls) == 1
    assert em.calls[0].severity == "heartbeat"
    conn = connect(db)
    row = conn.execute(
        "SELECT last_proven_at FROM obligation_clocks "
        "WHERE obligation_id='obs_heartbeat_proven'"
    ).fetchone()
    assert row[0] == NOW
    conn.close()


def test_success_clears_prior_breach(db):
    """If a breach row was already present, a successful tick clears it."""
    conn = connect(db)
    conn.execute(
        "INSERT INTO obligation_clocks (obligation_id, last_proven_at, "
        "proven_by, next_due_at) "
        "VALUES ('obs_dead_mans_switch_breach', ?, 'heartbeat', ?)",
        (NOW, NOW),
    )
    conn.commit()
    conn.close()
    pub = _pub(db, sink=DryRunSink(label="email"))
    pub.run_once()
    conn = connect(db)
    n = conn.execute(
        "SELECT COUNT(*) FROM obligation_clocks "
        "WHERE obligation_id='obs_dead_mans_switch_breach'"
    ).fetchone()[0]
    assert n == 0
    conn.close()


def test_single_failure_no_breach_yet(db):
    pub = _pub(db, sink=_FailingSink(), threshold=3)
    res = pub.run_once()
    assert not res["success"]
    assert res["consecutive_failures"] == 1
    conn = connect(db)
    n = conn.execute(
        "SELECT COUNT(*) FROM obligation_clocks "
        "WHERE obligation_id='obs_dead_mans_switch_breach'"
    ).fetchone()[0]
    assert n == 0
    conn.close()


def test_three_failures_set_breach(db):
    pub = _pub(db, sink=_FailingSink(), threshold=3)
    pub.run_once()
    pub.run_once()
    pub.run_once()
    assert pub._consecutive_failures == 3
    conn = connect(db)
    row = conn.execute(
        "SELECT details FROM obligation_clocks "
        "WHERE obligation_id='obs_dead_mans_switch_breach'"
    ).fetchone()
    assert row is not None
    conn.close()


def test_success_after_failure_streak_clears_breach(db):
    failing = _FailingSink()
    pub = _pub(db, sink=failing, threshold=3)
    pub.run_once()
    pub.run_once()
    pub.run_once()
    # Swap to a passing sink for the next tick.
    passing = DryRunSink(label="email")
    pub.email_sink = passing
    pub.run_once()
    assert pub._consecutive_failures == 0
    conn = connect(db)
    n = conn.execute(
        "SELECT COUNT(*) FROM obligation_clocks "
        "WHERE obligation_id='obs_dead_mans_switch_breach'"
    ).fetchone()[0]
    assert n == 0
    conn.close()


def test_failure_records_alert_log_with_error(db):
    pub = _pub(db, sink=_FailingSink(err="smtp 530"), threshold=99)
    pub.run_once()
    conn = connect(db)
    row = conn.execute(
        "SELECT delivered_at, error FROM alert_log "
        "WHERE severity='heartbeat' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row[0] is None
    assert "smtp 530" in row[1]
    conn.close()


def test_sink_exception_handled(db):
    class _Boom:
        def __call__(self, payload):
            raise RuntimeError("boom")

    pub = _pub(db, sink=_Boom(), threshold=99)
    res = pub.run_once()
    assert not res["success"]
    assert res["consecutive_failures"] == 1


def test_offline_mode_does_not_arm(db):
    pub = ObsHeartbeatPublisher(
        db_path=db, email_sink=DryRunSink(label="email"),
        interval_seconds=60, breach_threshold=3, mode="offline",
        clock=lambda: NOW,
    )
    pub.arm()
    assert pub._scheduler is None
    pub.disarm()


def test_arm_and_disarm_production(db):
    pub = ObsHeartbeatPublisher(
        db_path=db, email_sink=DryRunSink(label="email"),
        interval_seconds=86400, breach_threshold=3, mode="production",
        clock=lambda: NOW,
    )
    pub.arm()
    assert pub._scheduler is not None
    pub.disarm()
    assert pub._scheduler is None
