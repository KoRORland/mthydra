"""Tests for state.alert_log — append-only repository."""
from __future__ import annotations

import pytest

from mthydra.controller.state.alert_log import (
    AlertLogEntry,
    append,
    last_for_key,
    last_successful_heartbeat,
    recent,
)
from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "state.sqlite")
    apply_schema(c)
    yield c
    c.close()


def test_append_inserts_and_returns_id(conn):
    rid = append(
        conn, attempted_at="2026-05-25T00:00:00Z",
        delivered_at="2026-05-25T00:00:01Z",
        sink="telegram", severity="crit",
        kind="probe_kill_pending", target="b1",
        dedupe_key="probe_kill_pending::b1",
        payload="box b1 needs termination",
        error=None,
    )
    assert rid >= 1
    rows = conn.execute("SELECT COUNT(*) FROM alert_log").fetchone()[0]
    assert rows == 1


def test_append_rejects_unknown_severity(conn):
    with pytest.raises(ValueError):
        append(
            conn, attempted_at="2026-05-25T00:00:00Z",
            delivered_at=None, sink="telegram", severity="bogus",
            kind="k", target=None, dedupe_key="d", payload="p", error=None,
        )


def test_recent_desc_order_and_limit(conn):
    for i in range(5):
        append(conn, attempted_at=f"2026-05-25T0{i}:00:00Z",
               delivered_at=f"2026-05-25T0{i}:00:01Z",
               sink="telegram", severity="warn",
               kind="x", target=None,
               dedupe_key=f"k{i}", payload="p", error=None)
    rows = recent(conn, limit=3)
    assert len(rows) == 3
    assert rows[0].dedupe_key == "k4"
    assert rows[2].dedupe_key == "k2"


def test_recent_filters_severity(conn):
    append(conn, attempted_at="2026-05-25T00:00:00Z",
           delivered_at=None, sink="telegram", severity="warn",
           kind="x", target=None, dedupe_key="k1", payload="p", error=None)
    append(conn, attempted_at="2026-05-25T00:01:00Z",
           delivered_at=None, sink="telegram", severity="crit",
           kind="y", target=None, dedupe_key="k2", payload="p", error=None)
    rows = recent(conn, severity="crit")
    assert [r.dedupe_key for r in rows] == ["k2"]


def test_last_for_key_returns_most_recent(conn):
    for i in range(3):
        append(conn, attempted_at=f"2026-05-25T0{i}:00:00Z",
               delivered_at=None, sink="telegram", severity="warn",
               kind="x", target=None, dedupe_key="same", payload=f"p{i}",
               error=None)
    r = last_for_key(conn, "same")
    assert r is not None
    assert r.payload == "p2"


def test_last_for_key_missing(conn):
    assert last_for_key(conn, "nope") is None


def test_last_successful_heartbeat(conn):
    # Failed heartbeat first.
    append(conn, attempted_at="2026-05-25T00:00:00Z",
           delivered_at=None, sink="email", severity="heartbeat",
           kind="heartbeat", target=None, dedupe_key="h1",
           payload="all-green", error="smtp 530")
    # Successful one.
    append(conn, attempted_at="2026-05-25T01:00:00Z",
           delivered_at="2026-05-25T01:00:02Z", sink="email",
           severity="heartbeat", kind="heartbeat", target=None,
           dedupe_key="h2", payload="all-green", error=None)
    # Another failure later (should not shadow the successful one).
    append(conn, attempted_at="2026-05-25T02:00:00Z",
           delivered_at=None, sink="email", severity="heartbeat",
           kind="heartbeat", target=None, dedupe_key="h3",
           payload="all-green", error="connection refused")
    r = last_successful_heartbeat(conn)
    assert r is not None
    assert r.dedupe_key == "h2"


def test_last_successful_heartbeat_none(conn):
    assert last_successful_heartbeat(conn) is None
