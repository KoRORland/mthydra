from __future__ import annotations

from mthydra.controller.state import eu_exit_observed as obs
from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema


def _db(tmp_path):
    c = connect(tmp_path / "s.sqlite")
    apply_schema(c)
    return c


def test_record_then_last_seen_roundtrip(tmp_path):
    c = _db(tmp_path)
    assert obs.last_seen(c, "box-1") is None
    obs.record_seen(c, "box-1", "2026-06-06T10:00:00Z")
    assert obs.last_seen(c, "box-1") == "2026-06-06T10:00:00Z"


def test_record_seen_is_upsert_keeps_latest(tmp_path):
    c = _db(tmp_path)
    obs.record_seen(c, "box-1", "2026-06-06T10:00:00Z")
    obs.record_seen(c, "box-1", "2026-06-06T10:05:00Z")
    assert obs.last_seen(c, "box-1") == "2026-06-06T10:05:00Z"
    n = c.execute(
        "SELECT COUNT(*) FROM eu_exit_observed WHERE box_id='box-1'"
    ).fetchone()[0]
    assert n == 1
