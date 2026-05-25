"""Tests for distribution.user_heartbeat — per-user dead-man's switch."""
from __future__ import annotations

import pytest

from mthydra.controller.distribution.sinks import DryRunDistributionSink
from mthydra.controller.distribution.user_heartbeat import (
    DistUserHeartbeatPublisher,
)
from mthydra.controller.observability.sinks import SinkResult
from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema
from mthydra.controller.state.user_channels import set_channels


NOW = "2026-05-25T12:00:00Z"


class _FailingTgSink:
    def __init__(self, err="http 401"):
        self.err = err
        self.calls = 0

    def __call__(self, **kwargs):
        self.calls += 1
        return SinkResult(sink="telegram", success=False, error=self.err)


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "state.sqlite"
    c = connect(p)
    apply_schema(c)
    # One user with Telegram only; one with email only; one with both.
    for u in ["u1", "u2", "u3"]:
        c.execute(
            "INSERT INTO users (user_id, display_name, out_of_band_channel, added_at) "
            "VALUES (?, NULL, 'email', ?)", (u, NOW),
        )
    set_channels(c, "u1", telegram_chat_id="1", email_addr=None, at=NOW)
    set_channels(c, "u2", telegram_chat_id=None, email_addr="u2@x", at=NOW)
    set_channels(c, "u3", telegram_chat_id="3", email_addr="u3@x", at=NOW)
    c.commit()
    c.close()
    return p


def _pub(db, *, sink, threshold=3, clock=NOW):
    return DistUserHeartbeatPublisher(
        db_path=db, telegram_sink=sink,
        interval_seconds=86400, breach_threshold=threshold,
        mode="production", clock=lambda: clock,
    )


def test_only_telegram_registered_users_get_heartbeat(db):
    tg = DryRunDistributionSink(label="telegram")
    pub = _pub(db, sink=tg)
    res = pub.run_once()
    assert res["sent"] == 2  # u1 and u3 have telegram
    chat_ids = sorted(c["chat_id"] for c in tg.calls)
    assert chat_ids == ["1", "3"]


def test_heartbeat_proves_per_user_obligation(db):
    pub = _pub(db, sink=DryRunDistributionSink(label="telegram"))
    pub.run_once()
    conn = connect(db)
    ids = {
        r[0] for r in conn.execute(
            "SELECT obligation_id FROM obligation_clocks "
            "WHERE obligation_id LIKE 'dist_user_heartbeat_proven::%'"
        ).fetchall()
    }
    assert ids == {
        "dist_user_heartbeat_proven::u1",
        "dist_user_heartbeat_proven::u3",
    }
    conn.close()


def test_failure_increments_per_user_counter(db):
    failing = _FailingTgSink()
    pub = _pub(db, sink=failing, threshold=5)
    pub.run_once()
    assert pub._failures["u1"] == 1
    assert pub._failures["u3"] == 1
    pub.run_once()
    assert pub._failures["u1"] == 2


def test_threshold_failures_set_breach_anti(db):
    failing = _FailingTgSink()
    pub = _pub(db, sink=failing, threshold=3)
    for _ in range(3):
        pub.run_once()
    conn = connect(db)
    breached = {
        r[0] for r in conn.execute(
            "SELECT obligation_id FROM obligation_clocks "
            "WHERE obligation_id LIKE 'dist_user_heartbeat_breach::%'"
        ).fetchall()
    }
    assert breached == {
        "dist_user_heartbeat_breach::u1",
        "dist_user_heartbeat_breach::u3",
    }
    conn.close()


def test_success_after_failures_clears_breach(db):
    failing = _FailingTgSink()
    pub = _pub(db, sink=failing, threshold=3)
    for _ in range(3):
        pub.run_once()
    pub.telegram_sink = DryRunDistributionSink(label="telegram")
    pub.run_once()
    conn = connect(db)
    n = conn.execute(
        "SELECT COUNT(*) FROM obligation_clocks "
        "WHERE obligation_id='dist_user_heartbeat_breach::u1'"
    ).fetchone()[0]
    assert n == 0
    assert pub._failures["u1"] == 0
    conn.close()


def test_sink_exception_treated_as_failure(db):
    class _Boom:
        def __call__(self, **kwargs):
            raise RuntimeError("boom")

    pub = _pub(db, sink=_Boom(), threshold=99)
    res = pub.run_once()
    assert res["failed"] == 2
    conn = connect(db)
    row = conn.execute(
        "SELECT delivered_at, error FROM distribution_log "
        "WHERE user_id='u1' AND kind='heartbeat' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row[0] is None
    assert "boom" in row[1]
    conn.close()


def test_offline_mode_does_not_arm(db):
    pub = DistUserHeartbeatPublisher(
        db_path=db, telegram_sink=DryRunDistributionSink(label="telegram"),
        interval_seconds=86400, breach_threshold=3, mode="offline",
        clock=lambda: NOW,
    )
    pub.arm()
    assert pub._scheduler is None
    pub.disarm()


def test_arm_and_disarm_production(db):
    pub = DistUserHeartbeatPublisher(
        db_path=db, telegram_sink=DryRunDistributionSink(label="telegram"),
        interval_seconds=86400, breach_threshold=3, mode="production",
        clock=lambda: NOW,
    )
    pub.arm()
    assert pub._scheduler is not None
    pub.disarm()
    assert pub._scheduler is None
