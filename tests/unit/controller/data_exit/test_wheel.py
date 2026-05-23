"""Spec E Task 9 — data_exit.wheel tests."""
from __future__ import annotations

import json


def _seed_authority(conn):
    """Insert a real Ed25519 authority row at generation 1."""
    from mthydra.controller.state.authority import insert_authority
    from mthydra.descriptor.authority import generate_authority_keypair
    priv, pub = generate_authority_keypair()
    insert_authority(conn, 1, priv, pub, "2026-05-23T00:00:00Z")


def _seed_active_eu_node(conn):
    from mthydra.controller.state.eu_nodes import (
        add_eu_node, set_data_exit_identity,
    )
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


def _make_cfg(tmp_path):
    from mthydra.controller.config import DataExitConfig
    return DataExitConfig(
        listen_port=443,
        sing_box_socket="/run/sb.sock",
        config_path=str(tmp_path / "sb.json"),
        reality_key_path=str(tmp_path / "r.key"),
        telegram_dcs_v4=(), telegram_dcs_v6=(),
        cover_sni_default="c.example", cover_sni_per_node={},
    )


def test_wheel_tick_writes_initial_config(tmp_path):
    """First tick on a fresh DB renders config + writes it + SIGHUPs."""
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    from mthydra.controller.data_exit.wheel import DataExitWheel

    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    _seed_active_eu_node(conn)
    conn.close()

    cfg = _make_cfg(tmp_path)
    (tmp_path / "r.key").write_text("PRIVKEY")

    sighup_calls = []
    wheel = DataExitWheel(
        db_path=db, cfg=cfg, node_id="eu1",
        unit_name="sing-box.service",
        sighup_fn=lambda u: sighup_calls.append(u),
        now_fn=lambda: "2026-05-23T00:05:00Z",
        mode="offline",
    )
    wheel.tick()

    assert (tmp_path / "sb.json").exists()
    assert sighup_calls == ["sing-box.service"]
    payload = json.loads((tmp_path / "sb.json").read_text())
    assert payload["inbounds"][0]["tls"]["server_name"] == "c.example"


def test_wheel_tick_skips_unchanged_config(tmp_path):
    """Second tick with no DB change does not re-write the config or SIGHUP."""
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    from mthydra.controller.data_exit.wheel import DataExitWheel

    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    _seed_active_eu_node(conn)
    conn.close()

    cfg = _make_cfg(tmp_path)
    (tmp_path / "r.key").write_text("PRIVKEY")
    sighup_calls = []
    wheel = DataExitWheel(
        db_path=db, cfg=cfg, node_id="eu1",
        unit_name="sing-box.service",
        sighup_fn=lambda u: sighup_calls.append(u),
        now_fn=lambda: "2026-05-23T00:05:00Z",
        mode="offline",
    )
    wheel.tick()
    wheel.tick()
    assert sighup_calls == ["sing-box.service"]  # only one


def test_wheel_tick_rewrites_after_credential_revoke(tmp_path):
    """Revoking a credential triggers a new config render + SIGHUP."""
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    from mthydra.controller.state.ru_boxes import (
        insert_box, mark_live, set_reality_uuid,
    )
    from mthydra.controller.state.credentials import (
        issue_credential, revoke_credential,
    )
    from mthydra.controller.data_exit.wheel import DataExitWheel

    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    _seed_authority(conn)
    _seed_active_eu_node(conn)
    insert_box(conn, "b1", "p", "r", None, "sni1", "v1", "2026-05-23T00:00:00Z")
    set_reality_uuid(conn, "b1", "uuid-b1")
    cred_id = issue_credential(conn, "b1", b"...", "2026-05-23T00:00:00Z", 1)
    mark_live(conn, "b1", public_ip="1.2.3.4", at="2026-05-23T00:01:00Z")
    conn.commit()
    conn.close()

    cfg = _make_cfg(tmp_path)
    (tmp_path / "r.key").write_text("PRIVKEY")
    sighup_calls = []
    wheel = DataExitWheel(
        db_path=db, cfg=cfg, node_id="eu1",
        sighup_fn=lambda u: sighup_calls.append(u),
        now_fn=lambda: "2026-05-23T00:05:00Z",
        mode="offline",
    )
    wheel.tick()
    payload = json.loads((tmp_path / "sb.json").read_text())
    assert len(payload["inbounds"][0]["users"]) == 1

    # Revoke credential and tick again.
    conn = connect(db)
    revoke_credential(conn, cred_id, at="2026-05-23T00:06:00Z")
    conn.close()
    wheel.tick()
    payload = json.loads((tmp_path / "sb.json").read_text())
    assert len(payload["inbounds"][0]["users"]) == 0
    assert len(sighup_calls) == 2  # first render + after revoke


def test_wheel_tick_registers_eu_exit_set_on_first_render(tmp_path):
    """First successful tick also registers the eu_exit_set row."""
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    from mthydra.controller.data_exit.wheel import DataExitWheel

    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    _seed_active_eu_node(conn)
    conn.close()

    cfg = _make_cfg(tmp_path)
    (tmp_path / "r.key").write_text("PRIVKEY")
    wheel = DataExitWheel(
        db_path=db, cfg=cfg, node_id="eu1",
        sighup_fn=lambda u: None,
        now_fn=lambda: "2026-05-23T00:05:00Z",
        mode="offline",
    )
    wheel.tick()
    conn = connect(db)
    n = conn.execute(
        "SELECT COUNT(*) FROM eu_exit_set WHERE retired_at IS NULL"
    ).fetchone()[0]
    conn.close()
    assert n == 1


def test_wheel_tick_no_reality_key_no_write(tmp_path):
    """Missing reality key file -> tick is a no-op (no config, no SIGHUP)."""
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    from mthydra.controller.data_exit.wheel import DataExitWheel

    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    _seed_active_eu_node(conn)
    conn.close()

    cfg = _make_cfg(tmp_path)
    # Do NOT create r.key.
    sighup_calls = []
    wheel = DataExitWheel(
        db_path=db, cfg=cfg, node_id="eu1",
        sighup_fn=lambda u: sighup_calls.append(u),
        now_fn=lambda: "2026-05-23T00:05:00Z",
        mode="offline",
    )
    wheel.tick()
    assert not (tmp_path / "sb.json").exists()
    assert sighup_calls == []
