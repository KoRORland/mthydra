"""Systematic catalogue of the spec I v8 probe triggers.

Schema-level smoke coverage lives in test_schema.py; this file exercises
every state combination the four triggers care about.
"""
from __future__ import annotations

import sqlite3

import pytest

from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "state.sqlite")
    apply_schema(c)
    # Seed an image + vantage + box so probe_results inserts are FK-valid.
    c.execute(
        "INSERT INTO ru_images (image_version, upstream_release, upstream_repo, "
        "binary_url, manifest_url, binary_sha256, binary_size_bytes, state, built_at) "
        "VALUES ('v1', 'r', 'r', 'u', 'm', 'sha', 1, 'candidate', ?)",
        ("2026-05-25T00:00:00Z",),
    )
    c.execute(
        "INSERT INTO ru_boxes (box_id, provider, region, sni, state, image_version, created_at) "
        "VALUES ('b1', 'p', 'r', 'sni-b1', 'live', 'v1', ?)",
        ("2026-05-25T00:00:00Z",),
    )
    c.execute(
        "INSERT INTO probe_vantages (vantage_id, label, source_kind, state, added_at) "
        "VALUES ('vk', 'kz1', 'cloud-cis', 'active', ?)",
        ("2026-05-25T00:00:00Z",),
    )
    c.commit()
    yield c
    c.close()


def test_no_relabel_burned(conn):
    conn.execute(
        "INSERT INTO probe_vantages (vantage_id, label, source_kind, state, added_at) "
        "VALUES ('v1', 'old', 'x', 'burned', ?)",
        ("2026-05-25T00:00:00Z",),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO probe_vantages (vantage_id, label, source_kind, state, added_at) "
            "VALUES ('v2', 'old', 'x', 'candidate', ?)",
            ("2026-05-25T00:00:00Z",),
        )
        conn.commit()


def test_burned_to_active_blocked(conn):
    conn.execute(
        "INSERT INTO probe_vantages (vantage_id, label, source_kind, state, added_at) "
        "VALUES ('v1', 'l1', 'x', 'burned', ?)",
        ("2026-05-25T00:00:00Z",),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE probe_vantages SET state='active' WHERE vantage_id='v1'")
        conn.commit()


def test_burned_to_retired_blocked(conn):
    conn.execute(
        "INSERT INTO probe_vantages (vantage_id, label, source_kind, state, added_at) "
        "VALUES ('v1', 'l1', 'x', 'burned', ?)",
        ("2026-05-25T00:00:00Z",),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE probe_vantages SET state='retired' WHERE vantage_id='v1'")
        conn.commit()


def test_active_to_burned_allowed(conn):
    conn.execute(
        "INSERT INTO probe_vantages (vantage_id, label, source_kind, state, added_at) "
        "VALUES ('v1', 'l1', 'x', 'active', ?)",
        ("2026-05-25T00:00:00Z",),
    )
    conn.commit()
    conn.execute("UPDATE probe_vantages SET state='burned' WHERE vantage_id='v1'")
    conn.commit()
    row = conn.execute(
        "SELECT state FROM probe_vantages WHERE vantage_id='v1'"
    ).fetchone()
    assert row[0] == "burned"


def test_probe_results_update_blocked(conn):
    conn.execute(
        "INSERT INTO probe_results (box_id, vantage_id, cycle_at, check_type, status, "
        "image_version, recorded_at) VALUES ('b1', 'vk', ?, 'surface_scan', 'pass', 'v1', ?)",
        ("2026-05-25T01:00:00Z", "2026-05-25T01:00:05Z"),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE probe_results SET status='hard_fail' WHERE id=1")
        conn.commit()


def test_probe_results_delete_blocked(conn):
    conn.execute(
        "INSERT INTO probe_results (box_id, vantage_id, cycle_at, check_type, status, "
        "image_version, recorded_at) VALUES ('b1', 'vk', ?, 'surface_scan', 'pass', 'v1', ?)",
        ("2026-05-25T01:00:00Z", "2026-05-25T01:00:05Z"),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM probe_results")
        conn.commit()


def test_probe_results_insert_succeeds_normally(conn):
    """Sanity: the triggers only block UPDATE / DELETE; INSERT is fine."""
    for i in range(3):
        conn.execute(
            "INSERT INTO probe_results (box_id, vantage_id, cycle_at, check_type, status, "
            "image_version, recorded_at) "
            "VALUES ('b1', 'vk', ?, 'surface_scan', 'pass', 'v1', ?)",
            (f"2026-05-25T0{i}:00:00Z", f"2026-05-25T0{i}:00:05Z"),
        )
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM probe_results").fetchone()[0]
    assert n == 3
