from mthydra.controller.state.backup_log import (
    BackupTrigger,
    list_pending_reconciliation,
    next_generation,
    record_index_updated,
    record_pushed,
    record_started,
)
from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema


def _conn(tmp_db_path):
    conn = connect(tmp_db_path)
    apply_schema(conn)
    return conn


def test_next_generation_starts_at_one(tmp_db_path):
    conn = _conn(tmp_db_path)
    assert next_generation(conn) == 1


def test_record_started_then_pushed_then_index(tmp_db_path):
    conn = _conn(tmp_db_path)
    gen = next_generation(conn)
    record_started(conn, gen, trigger=BackupTrigger.FLOOR_TIMER, created_at="2026-05-18T00:00:00Z")
    record_pushed(conn, gen, sha256="abc", size_bytes=4096, pushed_at="2026-05-18T00:00:10Z")
    record_index_updated(conn, gen, at="2026-05-18T00:00:11Z")
    assert next_generation(conn) == 2
    assert list_pending_reconciliation(conn) == []


def test_pushed_without_index_listed_for_reconciliation(tmp_db_path):
    conn = _conn(tmp_db_path)
    gen = next_generation(conn)
    record_started(conn, gen, BackupTrigger.FLOOR_TIMER, "2026-05-18T00:00:00Z")
    record_pushed(conn, gen, sha256="abc", size_bytes=4096, pushed_at="2026-05-18T00:00:10Z")
    pending = list_pending_reconciliation(conn)
    assert [p.generation for p in pending] == [gen]
