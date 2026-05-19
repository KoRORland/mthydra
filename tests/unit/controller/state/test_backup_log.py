from mthydra.controller.state.backup_log import (
    BackupTrigger,
    count_consecutive_failures,
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


# ---------------------------------------------------------------------------
# count_consecutive_failures
# ---------------------------------------------------------------------------

def test_consecutive_failures_zero_on_empty(tmp_db_path):
    conn = _conn(tmp_db_path)
    assert count_consecutive_failures(conn) == 0


def test_consecutive_failures_counts_unpushed_streak(tmp_db_path):
    conn = _conn(tmp_db_path)
    now = "2026-05-18T12:00:00Z"
    for i in range(1, 4):
        record_started(conn, i, BackupTrigger.FLOOR_TIMER, now)
    assert count_consecutive_failures(conn) == 3


def test_consecutive_failures_stops_at_success(tmp_db_path):
    conn = _conn(tmp_db_path)
    now = "2026-05-18T12:00:00Z"
    # gen 1 succeeded
    record_started(conn, 1, BackupTrigger.FLOOR_TIMER, now)
    record_pushed(conn, 1, sha256="ok", size_bytes=1, pushed_at=now)
    # gen 2 and 3 failed (no push)
    record_started(conn, 2, BackupTrigger.FLOOR_TIMER, now)
    record_started(conn, 3, BackupTrigger.FLOOR_TIMER, now)
    # streak should be 2 (gens 3 and 2 — scan desc; stop at gen 1's success)
    assert count_consecutive_failures(conn) == 2


def test_consecutive_failures_resets_after_success(tmp_db_path):
    conn = _conn(tmp_db_path)
    now = "2026-05-18T12:00:00Z"
    record_started(conn, 1, BackupTrigger.FLOOR_TIMER, now)
    record_started(conn, 2, BackupTrigger.FLOOR_TIMER, now)
    # success at gen 3 resets streak
    record_started(conn, 3, BackupTrigger.FLOOR_TIMER, now)
    record_pushed(conn, 3, sha256="ok", size_bytes=1, pushed_at=now)
    assert count_consecutive_failures(conn) == 0


def test_consecutive_failures_simulates_restart(tmp_db_path):
    """A fresh connection (simulating restart) should see same streak from DB."""
    conn = _conn(tmp_db_path)
    now = "2026-05-18T12:00:00Z"
    for i in range(1, 4):
        record_started(conn, i, BackupTrigger.FLOOR_TIMER, now)
    conn.close()
    # New connection — simulates controller restart
    conn2 = connect(tmp_db_path)
    assert count_consecutive_failures(conn2) == 3


# ---------------------------------------------------------------------------
# abandon_zombie_starts
# ---------------------------------------------------------------------------

def test_abandon_zombie_tags_old_unpushed_rows(tmp_db_path):
    from mthydra.controller.state.backup_log import abandon_zombie_starts
    conn = _conn(tmp_db_path)
    # Row started 2 hours ago — older than max_age_hours=1
    record_started(conn, 1, BackupTrigger.FLOOR_TIMER, "2026-05-18T10:00:00Z")
    n = abandon_zombie_starts(conn, now_iso="2026-05-18T12:00:00Z", max_age_hours=1)
    assert n == 1
    row = conn.execute("SELECT trigger FROM backup_log WHERE generation=1").fetchone()
    assert row[0].endswith(":abandoned")


def test_abandon_zombie_ignores_recent_rows(tmp_db_path):
    from mthydra.controller.state.backup_log import abandon_zombie_starts
    conn = _conn(tmp_db_path)
    # Row started 30 minutes ago — within max_age_hours=1
    record_started(conn, 1, BackupTrigger.FLOOR_TIMER, "2026-05-18T11:30:00Z")
    n = abandon_zombie_starts(conn, now_iso="2026-05-18T12:00:00Z", max_age_hours=1)
    assert n == 0
    row = conn.execute("SELECT trigger FROM backup_log WHERE generation=1").fetchone()
    assert ":abandoned" not in row[0]


def test_abandon_zombie_ignores_already_pushed(tmp_db_path):
    from mthydra.controller.state.backup_log import abandon_zombie_starts
    conn = _conn(tmp_db_path)
    record_started(conn, 1, BackupTrigger.FLOOR_TIMER, "2026-05-18T10:00:00Z")
    record_pushed(conn, 1, sha256="ok", size_bytes=1, pushed_at="2026-05-18T10:00:01Z")
    n = abandon_zombie_starts(conn, now_iso="2026-05-18T12:00:00Z", max_age_hours=1)
    assert n == 0


def test_abandon_zombie_idempotent(tmp_db_path):
    from mthydra.controller.state.backup_log import abandon_zombie_starts
    conn = _conn(tmp_db_path)
    record_started(conn, 1, BackupTrigger.FLOOR_TIMER, "2026-05-18T10:00:00Z")
    abandon_zombie_starts(conn, now_iso="2026-05-18T12:00:00Z", max_age_hours=1)
    n2 = abandon_zombie_starts(conn, now_iso="2026-05-18T12:00:00Z", max_age_hours=1)
    assert n2 == 0  # already tagged
