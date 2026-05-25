"""Tests for state.probe_results — spec I §5."""
from __future__ import annotations

import pytest

from mthydra.controller.state.db import connect
from mthydra.controller.state.probe_results import (
    distinct_vantages_in_window,
    last_cycle_at,
    recent_for_box,
    record,
)
from mthydra.controller.state.probe_vantages import add_candidate, attest_active
from mthydra.controller.state.schema import apply_schema


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "state.sqlite")
    apply_schema(c)
    # Seed an image + a box.
    c.execute(
        "INSERT INTO ru_images (image_version, upstream_release, upstream_repo, "
        "binary_url, manifest_url, binary_sha256, binary_size_bytes, state, built_at) "
        "VALUES ('v1', 'r', 'r', 'u', 'm', 'sha', 1, 'candidate', ?)",
        ("2026-05-25T00:00:00Z",),
    )
    c.execute(
        "INSERT INTO shards (shard_id, members_json, target_size, last_reshuffled_at, created_at) "
        "VALUES ('s1', '[]', 2, ?, ?)",
        ("2026-05-25T00:00:00Z", "2026-05-25T00:00:00Z"),
    )
    c.execute(
        "INSERT INTO ru_boxes (box_id, provider, region, sni, shard_id, state, "
        "image_version, created_at) VALUES ('b1', 'p', 'r', 'sni-b1', 's1', 'live', 'v1', ?)",
        ("2026-05-25T00:00:00Z",),
    )
    c.commit()
    yield c
    c.close()


def _seed_active_vantage(conn, vid="v1", label="kz1"):
    add_candidate(conn, vantage_id=vid, label=label, source_kind="x",
                  at="2026-05-25T00:00:00Z")
    attest_active(conn, vid, at="2026-05-25T00:00:30Z")


def test_record_inserts_and_updates_last_used(conn):
    _seed_active_vantage(conn)
    rid = record(
        conn, box_id="b1", vantage_id="v1",
        cycle_at="2026-05-25T01:00:00Z",
        check_type="tls_fall_through", status="pass",
        evidence_json=None, image_version="v1",
        recorded_at="2026-05-25T01:00:05Z",
    )
    assert rid >= 1
    row = conn.execute(
        "SELECT last_used_at FROM probe_vantages WHERE vantage_id='v1'"
    ).fetchone()
    assert row[0] == "2026-05-25T01:00:05Z"


def test_record_refuses_non_active_vantage(conn):
    add_candidate(conn, vantage_id="v1", label="kz1", source_kind="x",
                  at="2026-05-25T00:00:00Z")
    # state=candidate; record should refuse
    with pytest.raises(ValueError, match="active"):
        record(
            conn, box_id="b1", vantage_id="v1",
            cycle_at="2026-05-25T01:00:00Z",
            check_type="tls_fall_through", status="pass",
            evidence_json=None, image_version="v1",
            recorded_at="2026-05-25T01:00:05Z",
        )


def test_record_refuses_unknown_vantage(conn):
    with pytest.raises(LookupError):
        record(
            conn, box_id="b1", vantage_id="nope",
            cycle_at="2026-05-25T01:00:00Z",
            check_type="tls_fall_through", status="pass",
            evidence_json=None, image_version="v1",
            recorded_at="2026-05-25T01:00:05Z",
        )


def test_record_validates_check_type_and_status(conn):
    _seed_active_vantage(conn)
    with pytest.raises(ValueError, match="check_type"):
        record(conn, box_id="b1", vantage_id="v1",
               cycle_at="2026-05-25T01:00:00Z", check_type="bogus",
               status="pass", evidence_json=None, image_version="v1",
               recorded_at="2026-05-25T01:00:05Z")
    with pytest.raises(ValueError, match="status"):
        record(conn, box_id="b1", vantage_id="v1",
               cycle_at="2026-05-25T01:00:00Z", check_type="surface_scan",
               status="bogus", evidence_json=None, image_version="v1",
               recorded_at="2026-05-25T01:00:05Z")


def test_recent_for_box_desc_order(conn):
    _seed_active_vantage(conn)
    for i in range(3):
        record(
            conn, box_id="b1", vantage_id="v1",
            cycle_at=f"2026-05-25T0{i}:00:00Z",
            check_type="surface_scan", status="pass",
            evidence_json=None, image_version="v1",
            recorded_at=f"2026-05-25T0{i}:00:05Z",
        )
    rows = recent_for_box(conn, "b1", limit=2)
    assert len(rows) == 2
    assert rows[0].cycle_at == "2026-05-25T02:00:00Z"
    assert rows[1].cycle_at == "2026-05-25T01:00:00Z"


def test_last_cycle_at(conn):
    _seed_active_vantage(conn)
    assert last_cycle_at(conn, "b1") is None
    record(
        conn, box_id="b1", vantage_id="v1",
        cycle_at="2026-05-25T01:00:00Z",
        check_type="tls_fall_through", status="pass",
        evidence_json=None, image_version="v1",
        recorded_at="2026-05-25T01:00:05Z",
    )
    assert last_cycle_at(conn, "b1") == "2026-05-25T01:00:00Z"


def test_distinct_vantages_in_window(conn):
    _seed_active_vantage(conn, "v1", "kz1")
    _seed_active_vantage(conn, "v2", "by1")
    record(conn, box_id="b1", vantage_id="v1",
           cycle_at="2026-05-25T01:00:00Z", check_type="surface_scan",
           status="pass", evidence_json=None, image_version="v1",
           recorded_at="2026-05-25T01:00:05Z")
    record(conn, box_id="b1", vantage_id="v2",
           cycle_at="2026-05-25T01:30:00Z", check_type="surface_scan",
           status="pass", evidence_json=None, image_version="v1",
           recorded_at="2026-05-25T01:30:05Z")
    # Pre-window probe should be filtered out.
    record(conn, box_id="b1", vantage_id="v1",
           cycle_at="2026-05-24T00:00:00Z", check_type="surface_scan",
           status="pass", evidence_json=None, image_version="v1",
           recorded_at="2026-05-24T00:00:05Z")
    vs = distinct_vantages_in_window(
        conn, "b1", window_seconds=3600,
        now="2026-05-25T02:00:00Z",
    )
    assert sorted(vs) == ["v1", "v2"]
