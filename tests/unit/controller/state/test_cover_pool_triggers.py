"""Spec C structural enforcement — SQLite triggers on cover_domain_pool / burned_domains."""
import sqlite3

import pytest

from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema


@pytest.fixture
def conn(tmp_db_path):
    c = connect(tmp_db_path)
    apply_schema(c)
    return c


def _insert_burned(c, domain: str) -> None:
    c.execute(
        "INSERT INTO burned_domains (domain, burned_at, reason) VALUES (?, '2026-05-19T00:00:00Z', 'manual')",
        (domain,),
    )
    c.commit()


def test_trigger_blocks_insert_of_burned_domain(conn):
    _insert_burned(conn, "burned.org")
    with pytest.raises(sqlite3.IntegrityError) as exc:
        conn.execute(
            "INSERT INTO cover_domain_pool (domain, state, added_at) "
            "VALUES ('burned.org', 'candidate_unverified', '2026-05-19T01:00:00Z')"
        )
        conn.commit()
    assert "burned_domains" in str(exc.value)
    n = conn.execute("SELECT COUNT(*) FROM cover_domain_pool").fetchone()[0]
    assert n == 0


def test_trigger_blocks_delete_from_burned_domains(conn):
    _insert_burned(conn, "appendonly.org")
    with pytest.raises(sqlite3.IntegrityError) as exc:
        conn.execute("DELETE FROM burned_domains WHERE domain='appendonly.org'")
        conn.commit()
    assert "append-only" in str(exc.value)
    n = conn.execute("SELECT COUNT(*) FROM burned_domains").fetchone()[0]
    assert n == 1


def test_trigger_allows_normal_insert_for_non_burned_domain(conn):
    conn.execute(
        "INSERT INTO cover_domain_pool (domain, state, added_at) "
        "VALUES ('fresh.org', 'candidate_unverified', '2026-05-19T00:00:00Z')"
    )
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM cover_domain_pool").fetchone()[0]
    assert n == 1


def test_trigger_survives_vacuum(conn):
    _insert_burned(conn, "vacuumed.org")
    conn.execute("VACUUM")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO cover_domain_pool (domain, state, added_at) "
            "VALUES ('vacuumed.org', 'candidate_unverified', '2026-05-19T01:00:00Z')"
        )
        conn.commit()
