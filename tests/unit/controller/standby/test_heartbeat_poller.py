"""Spec F — StandbyHeartbeatPoller."""
from unittest.mock import MagicMock

import pytest

from mthydra.controller.standby.heartbeat import StandbyHeartbeatPoller
from mthydra.controller.state.db import connect
from mthydra.controller.state.eu_nodes import add_eu_node, get_eu_node
from mthydra.controller.state.obligations import list_obligations
from mthydra.controller.state.schema import apply_schema


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "state.sqlite"
    conn = connect(p)
    apply_schema(conn)
    conn.close()
    return p


def _seed_standby(p, node_id="eu-standby-de-1"):
    conn = connect(p)
    add_eu_node(conn, node_id=node_id, hostname="h", provider="hetzner",
                region="de", role="standby", added_at="2026-05-20T00:00:00Z")
    conn.close()


def test_poller_fresh_heartbeat_proves_obligation(db):
    _seed_standby(db)
    b2 = MagicMock()
    b2.head_heartbeat.return_value = {
        "etag": '"abc"',
        "last_modified_iso": "2026-05-20T01:00:00Z",
        "size_bytes": 100,
    }
    poller = StandbyHeartbeatPoller(
        db_path=db, b2_destination=b2, poll_interval_seconds=300,
        staleness_alert_seconds=600, mode="offline",
        clock=lambda: "2026-05-20T01:01:00Z",
    )
    stale = poller.run_once()
    assert stale == []
    conn = connect(db)
    obs = {o.obligation_id: o for o in list_obligations(conn)}
    assert "eu_standby_liveness_seen::eu-standby-de-1" in obs
    n = get_eu_node(conn, "eu-standby-de-1")
    assert n.last_heartbeat_b2_etag == '"abc"'
    conn.close()


def test_poller_stale_heartbeat_emits_anti_obligation(db):
    _seed_standby(db)
    b2 = MagicMock()
    b2.head_heartbeat.return_value = {
        "etag": '"abc"',
        "last_modified_iso": "2026-05-20T01:00:00Z",
        "size_bytes": 100,
    }
    poller = StandbyHeartbeatPoller(
        db_path=db, b2_destination=b2, poll_interval_seconds=300,
        staleness_alert_seconds=600, mode="offline",
        clock=lambda: "2026-05-20T01:30:00Z",
    )
    stale = poller.run_once()
    assert stale == ["eu-standby-de-1"]
    conn = connect(db)
    obs = {o.obligation_id for o in list_obligations(conn)}
    assert "eu_standby_liveness_stale::eu-standby-de-1" in obs
    conn.close()


def test_poller_missing_heartbeat_treated_as_stale(db):
    _seed_standby(db)
    b2 = MagicMock()
    b2.head_heartbeat.return_value = None
    poller = StandbyHeartbeatPoller(
        db_path=db, b2_destination=b2, poll_interval_seconds=300,
        staleness_alert_seconds=600, mode="offline",
        clock=lambda: "2026-05-20T01:00:00Z",
    )
    stale = poller.run_once()
    assert stale == ["eu-standby-de-1"]


def test_poller_skips_retired_nodes(db):
    _seed_standby(db, "eu-standby-old")
    conn = connect(db)
    from mthydra.controller.state.eu_nodes import retire_eu_node
    retire_eu_node(conn, "eu-standby-old", at="2026-05-20T00:30:00Z")
    conn.close()
    b2 = MagicMock()
    poller = StandbyHeartbeatPoller(
        db_path=db, b2_destination=b2, poll_interval_seconds=300,
        staleness_alert_seconds=600, mode="offline",
        clock=lambda: "2026-05-20T01:00:00Z",
    )
    poller.run_once()
    b2.head_heartbeat.assert_not_called()


def test_poller_clears_stale_when_fresh_heartbeat_returns(db):
    _seed_standby(db)
    b2 = MagicMock()
    poller = StandbyHeartbeatPoller(
        db_path=db, b2_destination=b2, poll_interval_seconds=300,
        staleness_alert_seconds=600, mode="offline",
        clock=lambda: "2026-05-20T02:00:00Z",
    )
    b2.head_heartbeat.return_value = None
    poller.run_once()
    conn = connect(db)
    obs = {o.obligation_id for o in list_obligations(conn)}
    assert "eu_standby_liveness_stale::eu-standby-de-1" in obs
    conn.close()
    b2.head_heartbeat.return_value = {
        "etag": '"new"',
        "last_modified_iso": "2026-05-20T01:59:30Z",
        "size_bytes": 100,
    }
    poller.run_once()
    conn = connect(db)
    obs = {o.obligation_id for o in list_obligations(conn)}
    assert "eu_standby_liveness_stale::eu-standby-de-1" not in obs
    assert "eu_standby_liveness_seen::eu-standby-de-1" in obs
    conn.close()
