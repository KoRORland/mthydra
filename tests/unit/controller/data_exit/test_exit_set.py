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
