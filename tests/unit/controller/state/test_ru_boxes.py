import sqlite3

import pytest

from mthydra.controller.state.db import connect
from mthydra.controller.state.ru_boxes import (
    Box,
    insert_box,
    list_live,
    mark_live,
    mark_terminated,
    set_reality_uuid,
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


def test_set_reality_uuid_assigns_then_reads_back(tmp_db_path):
    conn = _conn(tmp_db_path)
    insert_box(
        conn, "b1", "p", "r", None, "sni-1", "v1", "2026-05-23T00:00:00Z",
    )
    set_reality_uuid(conn, "b1", "9a8b-uuid")
    row = conn.execute(
        "SELECT reality_uuid FROM ru_boxes WHERE box_id='b1'"
    ).fetchone()
    assert row[0] == "9a8b-uuid"


def test_set_reality_uuid_unknown_box_raises(tmp_db_path):
    conn = _conn(tmp_db_path)
    with pytest.raises(KeyError):
        set_reality_uuid(conn, "missing", "u1")


def test_set_reality_uuid_unique(tmp_db_path):
    conn = _conn(tmp_db_path)
    insert_box(conn, "b1", "p", "r", None, "sni-1", "v1", "2026-05-23T00:00:00Z")
    insert_box(conn, "b2", "p", "r", None, "sni-2", "v1", "2026-05-23T00:00:00Z")
    set_reality_uuid(conn, "b1", "same")
    with pytest.raises(sqlite3.IntegrityError):
        set_reality_uuid(conn, "b2", "same")


# --- spec D2: canary helpers ---

def test_insert_box_is_canary_default_false(tmp_db_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_boxes import insert_box
    from mthydra.controller.state.schema import apply_schema
    conn = connect(tmp_db_path)
    apply_schema(conn)
    insert_box(conn, "b1", "p", "r", "10.0.0.1", "sni-b1",
               "v1", "2026-05-25T00:00:00Z")
    row = conn.execute("SELECT is_canary FROM ru_boxes WHERE box_id='b1'").fetchone()
    assert row[0] == 0
    conn.close()


def test_insert_box_is_canary_true(tmp_db_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_boxes import insert_box
    from mthydra.controller.state.schema import apply_schema
    conn = connect(tmp_db_path)
    apply_schema(conn)
    insert_box(conn, "b1", "p", "r", "10.0.0.1", "sni-b1",
               "v1", "2026-05-25T00:00:00Z", is_canary=True)
    row = conn.execute("SELECT is_canary FROM ru_boxes WHERE box_id='b1'").fetchone()
    assert row[0] == 1
    conn.close()


def test_list_canary_boxes_filters(tmp_db_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_boxes import insert_box, list_canary_boxes
    from mthydra.controller.state.schema import apply_schema
    conn = connect(tmp_db_path)
    apply_schema(conn)
    insert_box(conn, "b1", "p", "r", "10.0.0.1", "sni-b1",
               "v1", "2026-05-25T00:00:00Z", is_canary=True)
    insert_box(conn, "b2", "p", "r", "10.0.0.2", "sni-b2",
               "v2", "2026-05-25T00:00:00Z", is_canary=True)
    insert_box(conn, "b3", "p", "r", "10.0.0.3", "sni-b3",
               "v1", "2026-05-25T00:00:00Z", is_canary=False)
    # All canaries.
    assert sorted(list_canary_boxes(conn)) == ["b1", "b2"]
    # Filtered by image_version.
    assert list_canary_boxes(conn, image_version="v1") == ["b1"]
    # State filter.
    conn.execute("UPDATE ru_boxes SET state='live', went_live_at=? WHERE box_id='b1'",
                 ("2026-05-25T00:01:00Z",))
    conn.commit()
    assert list_canary_boxes(conn, state_filter=("live",)) == ["b1"]
    conn.close()


def test_clear_canary_flag_audits(tmp_db_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_boxes import (
        clear_canary_flag, insert_box,
    )
    from mthydra.controller.state.schema import apply_schema
    conn = connect(tmp_db_path)
    apply_schema(conn)
    insert_box(conn, "b1", "p", "r", "10.0.0.1", "sni-b1",
               "v1", "2026-05-25T00:00:00Z", is_canary=True)
    clear_canary_flag(conn, "b1", at="2026-05-25T01:00:00Z", reason="soak done")
    row = conn.execute("SELECT is_canary FROM ru_boxes WHERE box_id='b1'").fetchone()
    assert row[0] == 0
    audits = conn.execute(
        "SELECT action, target FROM audit_log WHERE action='ru_box_canary_clear'"
    ).fetchall()
    assert audits == [("ru_box_canary_clear", "b1")]
    conn.close()


def test_clear_canary_flag_refuses_non_canary(tmp_db_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_boxes import (
        clear_canary_flag, insert_box,
    )
    from mthydra.controller.state.schema import apply_schema
    conn = connect(tmp_db_path)
    apply_schema(conn)
    insert_box(conn, "b1", "p", "r", "10.0.0.1", "sni-b1",
               "v1", "2026-05-25T00:00:00Z", is_canary=False)
    with pytest.raises(ValueError):
        clear_canary_flag(conn, "b1", at="2026-05-25T01:00:00Z", reason="x")
    conn.close()


def test_clear_canary_flag_refuses_missing_box(tmp_db_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_boxes import clear_canary_flag
    from mthydra.controller.state.schema import apply_schema
    conn = connect(tmp_db_path)
    apply_schema(conn)
    with pytest.raises(LookupError):
        clear_canary_flag(conn, "nope", at="2026-05-25T00:00:00Z", reason="x")
    conn.close()


# --- spec D2: ru_images.list_live_boxes_for_image ---

def test_list_live_boxes_for_image_filters_states(tmp_db_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_boxes import insert_box, mark_live, mark_terminated
    from mthydra.controller.state.ru_images import list_live_boxes_for_image
    from mthydra.controller.state.schema import apply_schema
    conn = connect(tmp_db_path)
    apply_schema(conn)
    insert_box(conn, "b1", "p", "r", "10.0.0.1", "sni-b1",
               "v1", "2026-05-25T00:00:00Z")
    insert_box(conn, "b2", "p", "r", "10.0.0.2", "sni-b2",
               "v1", "2026-05-25T00:00:00Z")
    insert_box(conn, "b3", "p", "r", "10.0.0.3", "sni-b3",
               "v2", "2026-05-25T00:00:00Z")
    mark_live(conn, "b1", public_ip="10.0.0.1", at="2026-05-25T00:01:00Z")
    mark_terminated(conn, "b2", reason="test", at="2026-05-25T00:02:00Z")
    # default: provisioning + live for v1 -> b1 only
    assert list_live_boxes_for_image(conn, "v1") == ["b1"]
    # include_terminated for v1 -> b1, b2
    assert sorted(
        list_live_boxes_for_image(conn, "v1", include_terminated=True)
    ) == ["b1", "b2"]
    # different image
    assert list_live_boxes_for_image(conn, "v2") == ["b3"]
    conn.close()
