"""Spec E Task 8 — data_exit.exit_set tests."""
from __future__ import annotations


def test_register_started_inserts_eu_exit_set_row(tmp_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    from mthydra.controller.state.eu_nodes import add_eu_node, set_data_exit_identity
    from mthydra.controller.data_exit.exit_set import register_started

    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    add_eu_node(
        conn,
        node_id="eu1",
        hostname="eu1.example",
        provider="p",
        region="r",
        role="active",
        added_at="2026-05-23T00:00:00Z",
    )
    conn.execute("UPDATE eu_nodes SET public_ip='203.0.113.5' WHERE node_id='eu1'")
    set_data_exit_identity(conn, "eu1", cover_sni="c.example", reality_pubkey="PUB")
    conn.commit()
    register_started(conn, node_id="eu1", listen_port=443, at="2026-05-23T00:01:00Z")
    row = conn.execute(
        "SELECT endpoint, cover_sni, reality_pubkey FROM eu_exit_set "
        "WHERE retired_at IS NULL"
    ).fetchone()
    assert row == ("203.0.113.5:443", "c.example", "PUB")


def test_clear_retires_row(tmp_path):
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    from mthydra.controller.state.eu_nodes import add_eu_node, set_data_exit_identity
    from mthydra.controller.data_exit.exit_set import register_started, clear

    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    add_eu_node(
        conn,
        node_id="eu1",
        hostname="eu1.example",
        provider="p",
        region="r",
        role="active",
        added_at="2026-05-23T00:00:00Z",
    )
    conn.execute("UPDATE eu_nodes SET public_ip='203.0.113.5' WHERE node_id='eu1'")
    set_data_exit_identity(conn, "eu1", cover_sni="c.example", reality_pubkey="PUB")
    conn.commit()
    register_started(conn, node_id="eu1", listen_port=443, at="2026-05-23T00:01:00Z")
    clear(conn, node_id="eu1", at="2026-05-23T00:02:00Z")
    row = conn.execute(
        "SELECT retired_at FROM eu_exit_set WHERE retired_at IS NULL"
    ).fetchone()
    assert row is None  # all rows retired


def test_register_started_idempotent(tmp_path):
    """Calling twice doesn't double-insert; updates existing row."""
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    from mthydra.controller.state.eu_nodes import add_eu_node, set_data_exit_identity
    from mthydra.controller.data_exit.exit_set import register_started

    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    add_eu_node(
        conn,
        node_id="eu1",
        hostname="eu1.example",
        provider="p",
        region="r",
        role="active",
        added_at="2026-05-23T00:00:00Z",
    )
    conn.execute("UPDATE eu_nodes SET public_ip='203.0.113.5' WHERE node_id='eu1'")
    set_data_exit_identity(conn, "eu1", cover_sni="c.example", reality_pubkey="PUB")
    conn.commit()
    register_started(conn, node_id="eu1", listen_port=443, at="2026-05-23T00:01:00Z")
    register_started(conn, node_id="eu1", listen_port=443, at="2026-05-23T00:01:00Z")
    n = conn.execute(
        "SELECT COUNT(*) FROM eu_exit_set WHERE retired_at IS NULL"
    ).fetchone()[0]
    assert n == 1


def test_register_started_raises_key_error_for_missing_node(tmp_path):
    """Unknown node_id -> KeyError."""
    import pytest
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    from mthydra.controller.data_exit.exit_set import register_started

    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    with pytest.raises(KeyError, match="not found"):
        register_started(conn, node_id="ghost", listen_port=443,
                         at="2026-05-23T00:00:00Z")


def test_register_started_raises_value_error_for_missing_public_ip(tmp_path):
    """eu_node exists but public_ip is NULL -> ValueError."""
    import pytest
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    from mthydra.controller.state.eu_nodes import add_eu_node
    from mthydra.controller.data_exit.exit_set import register_started

    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    add_eu_node(
        conn, node_id="eu1", hostname="h", provider="p", region="r",
        role="active", added_at="2026-05-23T00:00:00Z",
    )
    # No set_data_exit_identity, no public_ip update.
    with pytest.raises(ValueError, match="no public_ip"):
        register_started(conn, node_id="eu1", listen_port=443,
                         at="2026-05-23T00:00:00Z")


def test_register_started_raises_value_error_for_missing_cover_sni(tmp_path):
    """public_ip present but cover_sni/reality_pubkey missing -> ValueError."""
    import pytest
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    from mthydra.controller.state.eu_nodes import add_eu_node
    from mthydra.controller.data_exit.exit_set import register_started

    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    add_eu_node(
        conn, node_id="eu1", hostname="h", provider="p", region="r",
        role="active", added_at="2026-05-23T00:00:00Z",
    )
    conn.execute("UPDATE eu_nodes SET public_ip='203.0.113.5' WHERE node_id='eu1'")
    conn.commit()
    with pytest.raises(ValueError, match="cover_sni or reality_pubkey"):
        register_started(conn, node_id="eu1", listen_port=443,
                         at="2026-05-23T00:00:00Z")


def test_register_started_unretires_previously_retired_row(tmp_path):
    """If fingerprint exists but was retired, re-registering clears retired_at."""
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    from mthydra.controller.state.eu_nodes import add_eu_node, set_data_exit_identity
    from mthydra.controller.data_exit.exit_set import register_started, clear

    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    add_eu_node(
        conn, node_id="eu1", hostname="h", provider="p", region="r",
        role="active", added_at="2026-05-23T00:00:00Z",
    )
    conn.execute("UPDATE eu_nodes SET public_ip='203.0.113.5' WHERE node_id='eu1'")
    set_data_exit_identity(conn, "eu1", cover_sni="c.example", reality_pubkey="PUB")
    conn.commit()
    register_started(conn, node_id="eu1", listen_port=443, at="2026-05-23T00:01:00Z")
    clear(conn, node_id="eu1", at="2026-05-23T00:02:00Z")
    # Now re-register: un-retires the row.
    register_started(conn, node_id="eu1", listen_port=443, at="2026-05-23T00:03:00Z")
    row = conn.execute(
        "SELECT retired_at, added_at FROM eu_exit_set"
    ).fetchone()
    assert row[0] is None
    assert row[1] == "2026-05-23T00:03:00Z"


def test_clear_noop_when_node_missing_or_public_ip_null(tmp_path):
    """clear() with unknown node OR null public_ip -> no-op, no exception."""
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    from mthydra.controller.state.eu_nodes import add_eu_node
    from mthydra.controller.data_exit.exit_set import clear

    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    # Case 1: missing node entirely.
    clear(conn, node_id="ghost", at="2026-05-23T00:00:00Z")
    # Case 2: node exists but has NULL public_ip.
    add_eu_node(
        conn, node_id="eu1", hostname="h", provider="p", region="r",
        role="active", added_at="2026-05-23T00:00:00Z",
    )
    conn.commit()
    clear(conn, node_id="eu1", at="2026-05-23T00:00:00Z")  # no exception
