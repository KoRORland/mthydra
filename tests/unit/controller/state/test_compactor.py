"""Tests for state.compactor — spec M."""
from __future__ import annotations

import sqlite3

import pytest

from mthydra.controller.state.compactor import (
    compact_alert_log,
    compact_distribution_log,
    compact_probe_results,
)
from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "state.sqlite")
    apply_schema(c)
    # Common parents.
    c.execute(
        "INSERT INTO users (user_id, display_name, out_of_band_channel, added_at) "
        "VALUES ('u1', NULL, 'email', '2026-05-26T00:00:00Z')"
    )
    c.execute(
        "INSERT INTO ru_images (image_version, upstream_release, upstream_repo, "
        "binary_url, manifest_url, binary_sha256, binary_size_bytes, state, built_at) "
        "VALUES ('v1', 'r', 'r', 'u', 'm', 'sha', 1, 'candidate', '2026-05-26T00:00:00Z')"
    )
    c.execute(
        "INSERT INTO ru_boxes (box_id, provider, region, sni, state, image_version, created_at) "
        "VALUES ('b1', 'p', 'r', 'sni-b1', 'live', 'v1', '2026-05-26T00:00:00Z')"
    )
    c.execute(
        "INSERT INTO probe_vantages (vantage_id, label, source_kind, state, added_at) "
        "VALUES ('vk', 'kz', 'cloud-cis', 'active', '2026-05-26T00:00:00Z')"
    )
    c.commit()
    yield c
    c.close()


def _seed_alert_log(conn, day_offsets):
    for i, off in enumerate(day_offsets):
        ts = f"2026-05-{20 + off:02d}T00:00:00Z"
        conn.execute(
            "INSERT INTO alert_log (attempted_at, delivered_at, sink, severity, "
            "kind, target, dedupe_key, payload) "
            "VALUES (?, ?, 'telegram', 'warn', 'k', NULL, ?, 'p')",
            (ts, ts, f"d{i}"),
        )
    conn.commit()


# --- alert_log ---


def test_compact_alert_log_dry_run_counts(conn):
    _seed_alert_log(conn, day_offsets=[0, 1, 2, 5, 6])
    res = compact_alert_log(
        conn, before="2026-05-23T00:00:00Z", dry_run=True, actor="op",
    )
    assert res.deleted == 3   # offsets 0, 1, 2 (May 20, 21, 22)
    assert res.dry_run is True
    # Nothing actually deleted.
    n = conn.execute("SELECT COUNT(*) FROM alert_log").fetchone()[0]
    assert n == 5
    # Audit row recorded.
    row = conn.execute(
        "SELECT details_json FROM audit_log WHERE action='log_compact_dry_run'"
    ).fetchone()
    assert row is not None


def test_compact_alert_log_deletes(conn):
    _seed_alert_log(conn, day_offsets=[0, 1, 2, 5, 6])
    res = compact_alert_log(
        conn, before="2026-05-23T00:00:00Z", dry_run=False, actor="op",
    )
    assert res.deleted == 3
    assert res.dry_run is False
    n = conn.execute("SELECT COUNT(*) FROM alert_log").fetchone()[0]
    assert n == 2
    # Marker was released.
    marker = conn.execute("SELECT COUNT(*) FROM compactor_marker").fetchone()[0]
    assert marker == 0
    # Audit row for the real compaction.
    row = conn.execute(
        "SELECT details_json FROM audit_log WHERE action='log_compact'"
    ).fetchone()
    assert row is not None


def test_compact_alert_log_empty_cutoff_no_op(conn):
    res = compact_alert_log(
        conn, before="2026-05-23T00:00:00Z", dry_run=False, actor="op",
    )
    assert res.deleted == 0


def test_compact_alert_log_marker_acquired_and_released(conn):
    """Successful run: marker is set then released; assert post-state empty."""
    _seed_alert_log(conn, day_offsets=[0, 1])
    compact_alert_log(
        conn, before="2026-05-23T00:00:00Z", dry_run=False, actor="op",
    )
    marker = conn.execute(
        "SELECT COUNT(*) FROM compactor_marker WHERE table_name='alert_log'"
    ).fetchone()[0]
    assert marker == 0


def test_compact_alert_log_double_marker_refuses(conn):
    """Another compaction already holding the marker for the same table."""
    _seed_alert_log(conn, day_offsets=[0])
    conn.execute(
        "INSERT INTO compactor_marker (table_name, acquired_at, acquired_by) "
        "VALUES ('alert_log', ?, 'other')",
        ("2026-05-26T00:00:00Z",),
    )
    conn.commit()
    with pytest.raises(RuntimeError, match="another compaction"):
        compact_alert_log(
            conn, before="2026-05-23T00:00:00Z", dry_run=False, actor="op",
        )


# --- probe_results ---


def test_compact_probe_results(conn):
    for off in (0, 1, 5):
        ts = f"2026-05-{20 + off:02d}T00:00:00Z"
        conn.execute(
            "INSERT INTO probe_results (box_id, vantage_id, cycle_at, check_type, status, "
            "image_version, recorded_at) VALUES ('b1', 'vk', ?, 'surface_scan', 'pass', 'v1', ?)",
            (ts, ts),
        )
    conn.commit()
    res = compact_probe_results(
        conn, before="2026-05-23T00:00:00Z", dry_run=False, actor="op",
    )
    assert res.deleted == 2
    n = conn.execute("SELECT COUNT(*) FROM probe_results").fetchone()[0]
    assert n == 1


# --- distribution_log ---


def test_compact_distribution_log(conn):
    for i in range(4):
        ts = f"2026-05-{20 + i:02d}T00:00:00Z"
        conn.execute(
            "INSERT INTO distribution_log (user_id, channel, kind, attempted_at, "
            "delivered_at, subset_hash, payload_json) "
            "VALUES ('u1', 'telegram', 'subset_delta', ?, ?, ?, '[]')",
            (ts, ts, f"h{i}"),
        )
    conn.commit()
    res = compact_distribution_log(
        conn, before="2026-05-22T00:00:00Z", dry_run=False, actor="op",
    )
    assert res.deleted == 2  # 20, 21


def test_unknown_table_refuses(conn):
    from mthydra.controller.state.compactor import _compact
    with pytest.raises(ValueError, match="unknown compactable table"):
        _compact(conn, "nope", before="x", dry_run=True, actor="op")
