"""Spec E Task 7 — data_exit.config_writer unit tests."""
from __future__ import annotations

import json


def _seed_authority(conn):
    """Insert a real Ed25519 authority row at generation 1."""
    from mthydra.controller.state.authority import insert_authority
    from mthydra.descriptor.authority import generate_authority_keypair
    priv, pub = generate_authority_keypair()
    insert_authority(conn, 1, priv, pub, "2026-05-23T00:00:00Z")


def test_render_minimal_sing_box_config(tmp_path):
    """Empty allowlist + cover SNI + Telegram DC list -> stable byte output."""
    from mthydra.controller.config import DataExitConfig
    from mthydra.controller.data_exit.config_writer import render_sing_box_config
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema

    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    cfg = DataExitConfig(
        listen_port=443,
        sing_box_socket="/run/sb.sock",
        config_path="/etc/sb.json",
        reality_key_path="/etc/r.key",
        telegram_dcs_v4=("149.154.160.0/20",),
        telegram_dcs_v6=("2001:b28:f23d::/48",),
        cover_sni_default="cover.example",
        cover_sni_per_node={},
    )
    out = render_sing_box_config(
        conn, cfg, node_id="eu1",
        cover_sni="cover.example",
        reality_private_key="PRIVKEY",
    )
    payload = json.loads(out)
    assert payload["inbounds"][0]["listen_port"] == 443
    assert payload["inbounds"][0]["tls"]["server_name"] == "cover.example"
    assert payload["inbounds"][0]["tls"]["reality"]["private_key"] == "PRIVKEY"
    assert payload["inbounds"][0]["users"] == []  # no boxes yet
    # canonical: indent=2, sort_keys=True
    assert out == json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")


def test_render_with_live_boxes(tmp_path):
    """Live boxes with active credentials appear in users[] array."""
    from mthydra.controller.config import DataExitConfig
    from mthydra.controller.data_exit.config_writer import render_sing_box_config
    from mthydra.controller.state.credentials import issue_credential
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_boxes import (
        insert_box, mark_live, set_reality_uuid,
    )
    from mthydra.controller.state.schema import apply_schema

    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    _seed_authority(conn)
    insert_box(conn, "b1", "p", "r", None, "sni1", "v1", "2026-05-23T00:00:00Z")
    set_reality_uuid(conn, "b1", "9a8b-uuid-1")
    issue_credential(conn, "b1", b"...", "2026-05-23T00:00:00Z", 1)
    mark_live(conn, "b1", public_ip="1.2.3.4", at="2026-05-23T00:01:00Z")

    cfg = DataExitConfig(
        listen_port=443, sing_box_socket="/run/sb.sock", config_path="/etc/sb.json",
        reality_key_path="/etc/r.key",
        telegram_dcs_v4=(), telegram_dcs_v6=(),
        cover_sni_default="c.example", cover_sni_per_node={},
    )
    out = render_sing_box_config(conn, cfg, node_id="eu1",
                                 cover_sni="c.example", reality_private_key="K")
    payload = json.loads(out)
    users = payload["inbounds"][0]["users"]
    assert len(users) == 1
    assert users[0]["name"] == "b1"
    assert users[0]["uuid"] == "9a8b-uuid-1"
    assert users[0]["flow"] == "xtls-rprx-vision"


def test_render_excludes_revoked_and_terminated(tmp_path):
    """Boxes with revoked credentials or non-live state are excluded."""
    from mthydra.controller.config import DataExitConfig
    from mthydra.controller.data_exit.config_writer import render_sing_box_config
    from mthydra.controller.state.credentials import issue_credential, revoke_credential
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.ru_boxes import (
        insert_box, mark_live, mark_terminated, set_reality_uuid,
    )
    from mthydra.controller.state.schema import apply_schema

    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    _seed_authority(conn)
    # Box 1: live, has credential -> included
    # Box 2: live, credential revoked -> excluded
    # Box 3: terminated -> excluded
    for i, (state_action, revoke) in enumerate(
        [("live", False), ("live", True), ("terminated", False)]
    ):
        bid = f"b{i+1}"
        insert_box(conn, bid, "p", "r", None, f"sni{i+1}", "v1",
                   "2026-05-23T00:00:00Z")
        set_reality_uuid(conn, bid, f"uuid-{bid}")
        cred_id = issue_credential(conn, bid, b"...", "2026-05-23T00:00:00Z", 1)
        if state_action == "live":
            mark_live(conn, bid, public_ip="1.2.3.4", at="2026-05-23T00:01:00Z")
        else:
            mark_live(conn, bid, public_ip="1.2.3.4", at="2026-05-23T00:01:00Z")
            mark_terminated(conn, bid, reason="test", at="2026-05-23T00:02:00Z")
        if revoke:
            revoke_credential(conn, cred_id, at="2026-05-23T00:01:30Z")

    cfg = DataExitConfig(
        listen_port=443, sing_box_socket="/run/sb.sock",
        config_path="/etc/sb.json", reality_key_path="/etc/r.key",
        telegram_dcs_v4=(), telegram_dcs_v6=(),
        cover_sni_default="c.example", cover_sni_per_node={},
    )
    out = render_sing_box_config(conn, cfg, node_id="eu1",
                                 cover_sni="c.example", reality_private_key="K")
    payload = json.loads(out)
    assert {u["name"] for u in payload["inbounds"][0]["users"]} == {"b1"}


def test_render_includes_telegram_dc_route(tmp_path):
    """Non-empty telegram_dcs_v4/v6 produce an ip_cidr route rule."""
    from mthydra.controller.config import DataExitConfig
    from mthydra.controller.data_exit.config_writer import render_sing_box_config
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema

    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    cfg = DataExitConfig(
        listen_port=443, sing_box_socket="/run/sb.sock",
        config_path="/etc/sb.json", reality_key_path="/etc/r.key",
        telegram_dcs_v4=("149.154.160.0/20",),
        telegram_dcs_v6=("2001:b28:f23d::/48",),
        cover_sni_default="c.example", cover_sni_per_node={},
    )
    out = render_sing_box_config(conn, cfg, node_id="eu1",
                                 cover_sni="c.example", reality_private_key="K")
    payload = json.loads(out)
    rules = payload["route"]["rules"]
    assert len(rules) == 1
    assert rules[0]["outbound"] == "telegram-direct"
    assert "149.154.160.0/20" in rules[0]["ip_cidr"]
    assert "2001:b28:f23d::/48" in rules[0]["ip_cidr"]
    assert payload["route"]["final"] == "telegram-direct"


def test_render_no_dcs_yields_empty_rules(tmp_path):
    from mthydra.controller.config import DataExitConfig
    from mthydra.controller.data_exit.config_writer import render_sing_box_config
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema

    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    cfg = DataExitConfig(
        listen_port=443, sing_box_socket="/run/sb.sock",
        config_path="/etc/sb.json", reality_key_path="/etc/r.key",
        telegram_dcs_v4=(), telegram_dcs_v6=(),
        cover_sni_default="c.example", cover_sni_per_node={},
    )
    out = render_sing_box_config(conn, cfg, node_id="eu1",
                                 cover_sni="c.example", reality_private_key="K")
    payload = json.loads(out)
    assert payload["route"]["rules"] == []
    assert payload["route"]["final"] == "telegram-direct"


def test_write_atomic_creates_tempfile_and_renames(tmp_path):
    from mthydra.controller.data_exit.config_writer import write_atomic

    out = tmp_path / "config.json"
    write_atomic(out, b'{"key":"value"}')
    assert out.read_bytes() == b'{"key":"value"}'
    # Tempfile in same dir is gone
    siblings = [p for p in tmp_path.iterdir() if p.name != "config.json"]
    assert siblings == []


def test_write_atomic_overwrites_existing(tmp_path):
    from mthydra.controller.data_exit.config_writer import write_atomic

    out = tmp_path / "config.json"
    out.write_bytes(b"old")
    write_atomic(out, b"new")
    assert out.read_bytes() == b"new"


def test_write_atomic_creates_parent_dirs(tmp_path):
    from mthydra.controller.data_exit.config_writer import write_atomic

    out = tmp_path / "nested" / "dir" / "config.json"
    write_atomic(out, b"x")
    assert out.read_bytes() == b"x"
