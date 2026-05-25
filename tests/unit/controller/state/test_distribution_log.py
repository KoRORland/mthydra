"""Tests for state.distribution_log — append-only ingest + read helpers."""
from __future__ import annotations

import pytest

from mthydra.controller.state.db import connect
from mthydra.controller.state.distribution_log import (
    DistributionLogEntry,
    append,
    last_subset_hash,
    recent,
)
from mthydra.controller.state.schema import apply_schema


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "state.sqlite")
    apply_schema(c)
    c.execute(
        "INSERT INTO users (user_id, display_name, out_of_band_channel, added_at) "
        "VALUES ('u1', NULL, 'email', '2026-05-25T00:00:00Z')"
    )
    c.commit()
    yield c
    c.close()


def test_append_returns_id(conn):
    rid = append(
        conn, user_id="u1", channel="telegram", kind="subset_delta",
        attempted_at="2026-05-25T00:00:00Z",
        delivered_at="2026-05-25T00:00:01Z",
        subset_hash="abc", payload_json='[]', error=None,
    )
    assert rid >= 1


def test_append_rejects_unknown_channel(conn):
    with pytest.raises(ValueError):
        append(
            conn, user_id="u1", channel="bogus", kind="subset_delta",
            attempted_at="2026-05-25T00:00:00Z",
            delivered_at=None,
            subset_hash="abc", payload_json='[]', error=None,
        )


def test_last_subset_hash_returns_most_recent_delivered(conn):
    append(conn, user_id="u1", channel="telegram", kind="subset_delta",
           attempted_at="2026-05-25T00:00:00Z",
           delivered_at="2026-05-25T00:00:01Z",
           subset_hash="h1", payload_json='[]', error=None)
    # Failure after success — last_subset_hash must still be h1.
    append(conn, user_id="u1", channel="telegram", kind="subset_delta",
           attempted_at="2026-05-25T01:00:00Z", delivered_at=None,
           subset_hash="h2", payload_json='[]', error="http 4xx")
    assert last_subset_hash(conn, "u1", "telegram") == "h1"


def test_last_subset_hash_filters_by_kind(conn):
    """Heartbeat rows must NOT influence last_subset_hash."""
    append(conn, user_id="u1", channel="telegram", kind="heartbeat",
           attempted_at="2026-05-25T01:00:00Z",
           delivered_at="2026-05-25T01:00:01Z",
           subset_hash=None, payload_json='ping', error=None)
    assert last_subset_hash(conn, "u1", "telegram") is None


def test_last_subset_hash_filters_by_channel(conn):
    append(conn, user_id="u1", channel="email", kind="subset_delta",
           attempted_at="2026-05-25T01:00:00Z",
           delivered_at="2026-05-25T01:00:01Z",
           subset_hash="em-h", payload_json='[]', error=None)
    assert last_subset_hash(conn, "u1", "telegram") is None
    assert last_subset_hash(conn, "u1", "email") == "em-h"


def test_recent_desc_and_limit(conn):
    for i in range(5):
        append(conn, user_id="u1", channel="telegram", kind="subset_delta",
               attempted_at=f"2026-05-25T0{i}:00:00Z",
               delivered_at=f"2026-05-25T0{i}:00:01Z",
               subset_hash=f"h{i}", payload_json='[]', error=None)
    rows = recent(conn, limit=3)
    assert len(rows) == 3
    assert rows[0].subset_hash == "h4"
    assert rows[2].subset_hash == "h2"


def test_recent_filters_user(conn):
    conn.execute(
        "INSERT INTO users (user_id, display_name, out_of_band_channel, added_at) "
        "VALUES ('u2', NULL, 'email', '2026-05-25T00:00:00Z')"
    )
    append(conn, user_id="u1", channel="telegram", kind="subset_delta",
           attempted_at="2026-05-25T00:00:00Z",
           delivered_at="2026-05-25T00:00:01Z",
           subset_hash="h-u1", payload_json='[]', error=None)
    append(conn, user_id="u2", channel="telegram", kind="subset_delta",
           attempted_at="2026-05-25T00:01:00Z",
           delivered_at="2026-05-25T00:01:01Z",
           subset_hash="h-u2", payload_json='[]', error=None)
    rows = recent(conn, user_id="u1")
    assert [r.subset_hash for r in rows] == ["h-u1"]
