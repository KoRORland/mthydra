from mthydra.controller.state.audit import log_event, recent_events
from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema


def test_log_then_recent(tmp_db_path):
    conn = connect(tmp_db_path)
    apply_schema(conn)
    log_event(conn, ts="2026-05-18T00:00:00Z", actor="controller", action="mark_burned", target="example.org", details_json='{"reason":"job2_kill"}')
    events = recent_events(conn, limit=10)
    assert events[0].action == "mark_burned"
