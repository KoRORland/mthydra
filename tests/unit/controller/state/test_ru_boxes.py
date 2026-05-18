import pytest

from mthydra.controller.state.db import connect
from mthydra.controller.state.ru_boxes import (
    Box,
    insert_box,
    list_live,
    mark_live,
    mark_terminated,
)
from mthydra.controller.state.schema import apply_schema


def _conn(tmp_db_path):
    conn = connect(tmp_db_path)
    apply_schema(conn)
    return conn


def test_insert_starts_in_provisioning(tmp_db_path):
    conn = _conn(tmp_db_path)
    insert_box(
        conn,
        box_id="box-1",
        provider="hetzner",
        region="fsn1",
        public_ip=None,
        sni="example.org",
        image_version="abc123",
        created_at="2026-05-18T00:00:00Z",
    )
    assert list_live(conn) == []


def test_mark_live_transitions(tmp_db_path):
    conn = _conn(tmp_db_path)
    insert_box(conn, "box-1", "hetzner", "fsn1", None, "example.org", "abc123", "2026-05-18T00:00:00Z")
    mark_live(conn, "box-1", public_ip="1.2.3.4", at="2026-05-18T00:10:00Z")
    live = list_live(conn)
    assert [b.box_id for b in live] == ["box-1"]
    assert live[0].public_ip == "1.2.3.4"


def test_mark_terminated_removes_from_live(tmp_db_path):
    conn = _conn(tmp_db_path)
    insert_box(conn, "box-1", "hetzner", "fsn1", None, "example.org", "abc123", "2026-05-18T00:00:00Z")
    mark_live(conn, "box-1", public_ip="1.2.3.4", at="2026-05-18T00:10:00Z")
    mark_terminated(conn, "box-1", reason="job2_kill", at="2026-05-18T01:00:00Z")
    assert list_live(conn) == []


def test_sni_uniqueness_enforced(tmp_db_path):
    conn = _conn(tmp_db_path)
    insert_box(conn, "box-1", "hetzner", "fsn1", None, "example.org", "abc123", "2026-05-18T00:00:00Z")
    with pytest.raises(Exception):
        insert_box(conn, "box-2", "hetzner", "fsn1", None, "example.org", "abc123", "2026-05-18T00:01:00Z")
