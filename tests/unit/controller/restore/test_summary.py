"""Tests for summarize_db (spec A §7.1 step 6)."""
from mthydra.controller.restore.summary import summarize_db
from mthydra.controller.state.authority import insert_authority
from mthydra.controller.state.burned import mark_burned
from mthydra.controller.state.cover_pool import add_candidate, mark_verified, move_to_in_use
from mthydra.controller.state.db import connect
from mthydra.controller.state.descriptor import insert_signing_key
from mthydra.controller.state.ru_boxes import insert_box, mark_live
from mthydra.controller.state.schema import SCHEMA_VERSION, apply_schema


def test_summary_reports_expected_counts(tmp_db_path):
    conn = connect(tmp_db_path)
    apply_schema(conn)
    insert_authority(conn, 1, "P", "K", "2026-05-18T00:00:00Z")
    insert_signing_key(conn, 1, b"P", b"K", "2026-05-18T00:00:00Z")

    # pool: one in_use (b1/a.org), one will be burned (b2/z.org)
    add_candidate(conn, "a.org", added_at="2026-05-18T00:00:00Z")
    mark_verified(conn, "a.org", from_vantage="v", at="2026-05-18T01:00:00Z")
    insert_box(conn, "b1", "h", "fsn1", None, "a.org", "img1", "2026-05-18T00:00:00Z")
    move_to_in_use(conn, "a.org", box_id="b1")
    mark_live(conn, "b1", public_ip="1.2.3.4", at="2026-05-18T02:00:00Z")

    add_candidate(conn, "z.org", added_at="2026-05-18T00:00:00Z")
    mark_verified(conn, "z.org", from_vantage="v", at="2026-05-18T01:00:00Z")
    insert_box(conn, "b2", "h", "fsn1", None, "z.org", "img1", "2026-05-18T00:00:00Z")
    move_to_in_use(conn, "z.org", box_id="b2")
    mark_burned(conn, "z.org", "job2_kill", "b2", "2026-05-18T03:00:00Z", None)
    conn.close()

    s = summarize_db(tmp_db_path)
    assert s["schema_version"] == SCHEMA_VERSION
    assert s["burned_domains_count"] == 1
    assert s["cover_pool_in_use"] == 1
    assert s["ru_boxes_live"] == 1
    assert s["latest_backup_generation"] is None  # no backups recorded
    assert s["latest_descriptor_generation"] is None


def test_summary_empty_db(tmp_db_path):
    conn = connect(tmp_db_path)
    apply_schema(conn)
    insert_authority(conn, 1, "P", "K", "2026-05-18T00:00:00Z")
    insert_signing_key(conn, 1, b"P", b"K", "2026-05-18T00:00:00Z")
    conn.close()
    s = summarize_db(tmp_db_path)
    assert s["schema_version"] == SCHEMA_VERSION
    assert s["burned_domains_count"] == 0
    assert s["ru_boxes_live"] == 0
    assert s["cover_pool_in_use"] == 0
