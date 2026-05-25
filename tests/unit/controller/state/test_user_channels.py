"""Tests for state.user_channels."""
from __future__ import annotations

import pytest

from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema
from mthydra.controller.state.user_channels import (
    UserChannelRow,
    get_channels,
    list_channels,
    set_channels,
)


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "state.sqlite")
    apply_schema(c)
    c.execute(
        "INSERT INTO users (user_id, display_name, out_of_band_channel, added_at) "
        "VALUES ('u1', NULL, 'email', '2026-05-25T00:00:00Z')"
    )
    c.execute(
        "INSERT INTO users (user_id, display_name, out_of_band_channel, added_at) "
        "VALUES ('u2', NULL, 'email', '2026-05-25T00:00:00Z')"
    )
    c.commit()
    yield c
    c.close()


def test_set_channels_insert_writes_audit(conn):
    set_channels(
        conn, "u1", telegram_chat_id="12345", email_addr="u1@example.org",
        at="2026-05-25T00:00:00Z",
    )
    row = get_channels(conn, "u1")
    assert isinstance(row, UserChannelRow)
    assert row.telegram_chat_id == "12345"
    assert row.email_addr == "u1@example.org"
    audits = conn.execute(
        "SELECT action, target FROM audit_log WHERE action='user_channels_register'"
    ).fetchall()
    assert audits == [("user_channels_register", "u1")]


def test_set_channels_update_path_writes_update_audit(conn):
    set_channels(conn, "u1", telegram_chat_id="111", email_addr=None,
                 at="2026-05-25T00:00:00Z")
    set_channels(conn, "u1", telegram_chat_id="222", email_addr="u1@x",
                 at="2026-05-25T00:01:00Z")
    row = get_channels(conn, "u1")
    assert row.telegram_chat_id == "222"
    assert row.email_addr == "u1@x"
    actions = {
        a[0]: a[1] for a in conn.execute(
            "SELECT action, target FROM audit_log "
            "WHERE action IN ('user_channels_register','user_channels_update')"
        ).fetchall()
    }
    assert actions == {"user_channels_register": "u1",
                       "user_channels_update": "u1"}


def test_set_channels_refuses_both_null(conn):
    with pytest.raises(ValueError, match="at least one"):
        set_channels(conn, "u1", telegram_chat_id=None, email_addr=None,
                     at="2026-05-25T00:00:00Z")


def test_set_channels_telegram_only_ok(conn):
    set_channels(conn, "u1", telegram_chat_id="t", email_addr=None,
                 at="2026-05-25T00:00:00Z")
    row = get_channels(conn, "u1")
    assert row.telegram_chat_id == "t"
    assert row.email_addr is None


def test_get_channels_missing(conn):
    assert get_channels(conn, "u-nope") is None


def test_list_channels_ordering(conn):
    set_channels(conn, "u2", telegram_chat_id="2", email_addr=None,
                 at="2026-05-25T00:00:00Z")
    set_channels(conn, "u1", telegram_chat_id="1", email_addr=None,
                 at="2026-05-25T00:00:00Z")
    rows = list_channels(conn)
    assert [r.user_id for r in rows] == ["u1", "u2"]
