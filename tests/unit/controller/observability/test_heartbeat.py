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


_FIXED_IDENTITY = {
    "version": "0.0.3",
    "hostname": "test-host",
    "schema_version": "15",
    "head_sha": "abcdef012345",
}


def _pub(db, *, sink, clock=NOW, threshold=3, identity=None, smtp_smoke_fn=None):
    return ObsHeartbeatPublisher(
        db_path=db,
        email_sink=sink,
        interval_seconds=60,
        breach_threshold=threshold,
        mode="production",
        clock=lambda: clock,
        identity=identity if identity is not None else _FIXED_IDENTITY,
        smtp_smoke_fn=smtp_smoke_fn,
    )


def test_heartbeat_subject_and_body_carry_identity(db):
    """R-D8: heartbeat emails must identify the running version + host so
    operators can tell which controller went silent. Previously the subject
    was just 'mthydra heartbeat @ <iso>' with no host/version, making fleet
    observability blind."""
    em = DryRunSink(label="email")
    pub = _pub(db, sink=em)
    pub.run_once()
    payload = em.calls[0]
    assert "test-host" in payload.subject
    assert "v0.0.3" in payload.subject
    assert "version: 0.0.3" in payload.body
    assert "hostname: test-host" in payload.body
    assert "schema: v15" in payload.body
    assert "HEAD: abcdef012345" in payload.body


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


# ---------------------------------------------------------------------------
# U-D4 — heartbeat-breach self-diagnosis
# ---------------------------------------------------------------------------


def test_breach_details_carries_recent_errors_and_smtp_smoke(db):
    """U-D4: when the breach threshold is hit, details_json must include
    deduplicated recent error strings + the SMTP smoke verdict, so the
    operator triages from the alert body."""
    sink = _FailingSink(err="SMTP connect timeout: mail.example.com:587")
    smoke_calls = {"n": 0}
    def fake_smoke():
        smoke_calls["n"] += 1
        return {"ok": False, "error": "timeout connecting to mail.example.com:587"}
    pub = _pub(db, sink=sink, threshold=3, smtp_smoke_fn=fake_smoke)
    # Three failures → breach raised.
    pub.run_once(); pub.run_once(); pub.run_once()

    conn = connect(db)
    row = conn.execute(
        "SELECT details FROM obligation_clocks "
        "WHERE obligation_id='obs_dead_mans_switch_breach'"
    ).fetchone()
    conn.close()
    assert row is not None
    import json as _json
    details = _json.loads(row[0])
    assert details["consecutive_failures"] >= 3
    assert "SMTP connect timeout" in details["last_error"]
    # The recent_distinct_errors list must contain the repeated SMTP error
    # (deduplicated; one entry even though there were 3 ticks).
    assert details["recent_distinct_errors"] == [
        "SMTP connect timeout: mail.example.com:587",
    ]
    # The smoke verdict must be included.
    assert details["smtp_smoke"]["ok"] is False
    assert "mail.example.com:587" in details["smtp_smoke"]["error"]
    assert smoke_calls["n"] == 1  # smoke runs only on breach, not per tick


def test_breach_details_smtp_smoke_failure_is_captured(db):
    """U-D4: if the smtp_smoke_fn ITSELF raises, the breach diagnosis
    must still land — the smoke result just records the exception."""
    sink = _FailingSink(err="anything")
    def boom_smoke():
        raise RuntimeError("smoke broke")
    pub = _pub(db, sink=sink, threshold=1, smtp_smoke_fn=boom_smoke)
    pub.run_once()
    conn = connect(db)
    row = conn.execute(
        "SELECT details FROM obligation_clocks "
        "WHERE obligation_id='obs_dead_mans_switch_breach'"
    ).fetchone()
    conn.close()
    import json as _json
    details = _json.loads(row[0])
    assert details["smtp_smoke"]["ok"] is False
    assert "RuntimeError" in details["smtp_smoke"]["error"]


def test_breach_no_smoke_fn_still_includes_recent_errors(db):
    """U-D4: smtp_smoke_fn is optional. Without it, breach details still
    include the recent_distinct_errors list — partial diagnosis is better
    than none."""
    sink = _FailingSink(err="random sink err")
    pub = _pub(db, sink=sink, threshold=1)  # no smtp_smoke_fn
    pub.run_once()
    conn = connect(db)
    row = conn.execute(
        "SELECT details FROM obligation_clocks "
        "WHERE obligation_id='obs_dead_mans_switch_breach'"
    ).fetchone()
    conn.close()
    import json as _json
    details = _json.loads(row[0])
    assert "smtp_smoke" not in details
    assert details["recent_distinct_errors"] == ["random sink err"]


def test_smtp_smoke_helper_handles_unreachable_host():
    """U-D4: the smtp_smoke helper must return a small dict, never raise,
    even when the host is unresolvable / unreachable."""
    from mthydra.controller.observability.heartbeat import smtp_smoke
    result = smtp_smoke("this-host-cannot-exist-12345.invalid", 587, timeout_s=1.0)
    assert result["ok"] is False
    assert "error" in result
    assert "\n" not in result["error"]
