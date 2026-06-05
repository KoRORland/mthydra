"""Tests for state.probe_key — controller_probe_key row accessors."""
from __future__ import annotations

import pytest

from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema
from mthydra.controller.state import probe_key


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "state.sqlite")
    apply_schema(c)
    yield c
    c.close()


def test_get_returns_none_when_empty(conn):
    assert probe_key.get(conn) is None


def test_put_then_get_round_trips(conn):
    probe_key.put(conn, private_key="PRIV", public_key="ssh-ed25519 PUB x",
                  comment="mthydra-probe-runner", at="2026-06-05T00:00:00Z")
    row = probe_key.get(conn)
    assert row.private_key == "PRIV"
    assert row.public_key == "ssh-ed25519 PUB x"
    assert row.comment == "mthydra-probe-runner"


def test_put_is_single_row_upsert(conn):
    probe_key.put(conn, private_key="A", public_key="pa",
                  comment=None, at="2026-06-05T00:00:00Z")
    probe_key.put(conn, private_key="B", public_key="pb",
                  comment=None, at="2026-06-05T00:00:01Z")
    row = probe_key.get(conn)
    assert row.private_key == "B"
    n = conn.execute("SELECT COUNT(*) FROM controller_probe_key").fetchone()[0]
    assert n == 1
