import json

from mthydra.controller.state.audit import log_event, recent_events, set_audit_mirror
from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema


def test_log_then_recent(tmp_db_path):
    conn = connect(tmp_db_path)
    apply_schema(conn)
    log_event(conn, ts="2026-05-18T00:00:00Z", actor="controller", action="mark_burned", target="example.org", details_json='{"reason":"job2_kill"}')
    events = recent_events(conn, limit=10)
    assert events[0].action == "mark_burned"


def test_mirror_file_written_when_configured(tmp_path, tmp_db_path):
    mirror = tmp_path / "logs" / "audit.log"
    set_audit_mirror(mirror)
    try:
        conn = connect(tmp_db_path)
        apply_schema(conn)
        log_event(conn, ts="2026-05-18T00:00:00Z", actor="op", action="test_action",
                  target="x", details_json=None)
        assert mirror.exists()
        line = json.loads(mirror.read_text().strip())
        assert line["action"] == "test_action"
        assert line["actor"] == "op"
    finally:
        set_audit_mirror(None)  # reset for other tests


def test_mirror_file_not_written_when_not_configured(tmp_path, tmp_db_path):
    mirror = tmp_path / "should_not_exist.log"
    set_audit_mirror(None)
    conn = connect(tmp_db_path)
    apply_schema(conn)
    log_event(conn, ts="2026-05-18T00:00:00Z", actor="op", action="noop", target=None,
              details_json=None)
    # Mirror file must not have been created
    assert not mirror.exists()


def test_mirror_appends_multiple_lines(tmp_path, tmp_db_path):
    mirror = tmp_path / "audit.log"
    set_audit_mirror(mirror)
    try:
        conn = connect(tmp_db_path)
        apply_schema(conn)
        for i in range(3):
            log_event(conn, ts=f"2026-05-18T00:00:0{i}Z", actor="op",
                      action=f"action_{i}", target=None, details_json=None)
        lines = [json.loads(l) for l in mirror.read_text().strip().splitlines()]
        assert len(lines) == 3
        assert [l["action"] for l in lines] == ["action_0", "action_1", "action_2"]
    finally:
        set_audit_mirror(None)
