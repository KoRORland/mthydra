"""Tests for state.alert_acks — spec J2."""
from __future__ import annotations

import pytest

from mthydra.controller.state.alert_acks import (
    ack, is_acked, list_active, list_all,
)
from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema


NOW = "2026-05-26T12:00:00Z"
LATER = "2026-05-26T13:00:00Z"
EXP = "2026-05-26T14:00:00Z"


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "state.sqlite")
    apply_schema(c)
    yield c
    c.close()


def test_ack_appends_row_and_audits(conn):
    rid = ack(conn, dedupe_key="probe_kill_pending::b1",
              acked_by="op", evidence="aware, replacing box",
              at=NOW, expires_at=EXP)
    assert rid >= 1
    audits = conn.execute(
        "SELECT action, target FROM audit_log WHERE action='alert_ack'"
    ).fetchall()
    assert audits == [("alert_ack", "probe_kill_pending::b1")]


def test_ack_requires_evidence(conn):
    with pytest.raises(ValueError, match="evidence"):
        ack(conn, dedupe_key="k", acked_by="op", evidence="",
            at=NOW, expires_at=EXP)


def test_is_acked_within_window(conn):
    ack(conn, dedupe_key="k", acked_by="op", evidence="ev",
        at=NOW, expires_at=EXP)
    assert is_acked(conn, "k", now=LATER)


def test_is_acked_past_expiry(conn):
    ack(conn, dedupe_key="k", acked_by="op", evidence="ev",
        at=NOW, expires_at="2026-05-26T12:30:00Z")
    assert not is_acked(conn, "k", now=LATER)


def test_is_acked_different_key(conn):
    ack(conn, dedupe_key="k1", acked_by="op", evidence="ev",
        at=NOW, expires_at=EXP)
    assert not is_acked(conn, "k2", now=LATER)


def test_multiple_acks_most_permissive_wins(conn):
    """If two acks exist with different expires_at, the longer one keeps is_acked True."""
    ack(conn, dedupe_key="k", acked_by="op1", evidence="ev1",
        at=NOW, expires_at="2026-05-26T12:30:00Z")
    ack(conn, dedupe_key="k", acked_by="op2", evidence="ev2",
        at=NOW, expires_at=EXP)
    assert is_acked(conn, "k", now=LATER)


def test_list_active_excludes_expired(conn):
    ack(conn, dedupe_key="alive", acked_by="op", evidence="ev",
        at=NOW, expires_at=EXP)
    ack(conn, dedupe_key="dead", acked_by="op", evidence="ev",
        at=NOW, expires_at="2026-05-26T12:30:00Z")
    rows = list_active(conn, now=LATER)
    assert [r.dedupe_key for r in rows] == ["alive"]


def test_list_all_includes_expired(conn):
    ack(conn, dedupe_key="alive", acked_by="op", evidence="ev",
        at=NOW, expires_at=EXP)
    ack(conn, dedupe_key="dead", acked_by="op", evidence="ev",
        at=NOW, expires_at="2026-05-26T12:30:00Z")
    rows = list_all(conn)
    assert {r.dedupe_key for r in rows} == {"alive", "dead"}
