"""Tests for ru_agent.config_gen — mtg.toml + sing-box.json rendering."""
import json

import pytest


def test_render_mtg_config_basic(tmp_path):
    """mtg config has the seed's SNI and a secret derived deterministically
    from the reality_uuid."""
    from mthydra.ru_agent.config_gen import render_mtg_config
    from mthydra.ru_agent.seed import Seed

    seed = Seed(
        box_id="b1", sni="cover.example", transport_role="ru_relay",
        reality_uuid="9a8b-uuid", onward_credential=b"x" * 100,
        authority_pubkey_pem="", descriptor_trust_anchors=(),
        initial_descriptor=b"", image={}, descriptor_refresh_url="",
        agent_source_url="", agent_source_sha256="", telegram_dcs={},
        issued_at="", issued_by_authority_generation=1,
    )
    out = render_mtg_config(seed, sing_box_socks_port=1080)
    text = out.decode()
    assert "cover.example" in text
    assert "secret" in text.lower()


def test_render_sing_box_config_basic(tmp_path):
    """sing-box client config contains one outbound per exit in descriptor;
    selector picks among them."""
    from mthydra.ru_agent.config_gen import render_sing_box_config
    from mthydra.ru_agent.seed import Seed

    seed = Seed(
        box_id="b1", sni="cover.example", transport_role="ru_relay",
        reality_uuid="9a8b-uuid-1", onward_credential=b"", authority_pubkey_pem="",
        descriptor_trust_anchors=(), initial_descriptor=b"", image={},
        descriptor_refresh_url="", agent_source_url="", agent_source_sha256="",
        telegram_dcs={"v4": ["149.154.160.0/20"], "v6": []},
        issued_at="", issued_by_authority_generation=1,
    )
    descriptor_payload = {
        "schema": "mthydra.descriptor.v2", "generation": 5,
        "exits": [
            {"fingerprint": "fp1", "endpoint": "1.2.3.4:443",
             "weight": 1, "cover_sni": "eu1cover.example",
             "reality_pubkey": "PUBKEY1"},
            {"fingerprint": "fp2", "endpoint": "5.6.7.8:443",
             "weight": 1, "cover_sni": "eu2cover.example",
             "reality_pubkey": "PUBKEY2"},
        ],
    }
    out = render_sing_box_config(seed, descriptor_payload, tproxy_port=12345)
    payload = json.loads(out)
    outbound_types = {o["type"] for o in payload["outbounds"]}
    assert "vless" in outbound_types
    assert "selector" in outbound_types
    vless_outbounds = [o for o in payload["outbounds"] if o["type"] == "vless"]
    assert len(vless_outbounds) == 2
    assert {o["tag"] for o in vless_outbounds} == {"exit-fp1", "exit-fp2"}
    selector = next(o for o in payload["outbounds"] if o["type"] == "selector")
    assert set(selector["outbounds"]) == {"exit-fp1", "exit-fp2"}
    inbound = payload["inbounds"][0]
    assert inbound["type"] == "tproxy"
    assert inbound["listen_port"] == 12345


def test_render_sing_box_config_empty_exits_raises(tmp_path):
    """A descriptor with no exits is a refusal-worthy condition."""
    from mthydra.ru_agent.config_gen import ConfigError, render_sing_box_config
    from mthydra.ru_agent.seed import Seed

    seed = Seed(
        box_id="b1", sni="cover.example", transport_role="ru_relay",
        reality_uuid="9a8b", onward_credential=b"", authority_pubkey_pem="",
        descriptor_trust_anchors=(), initial_descriptor=b"", image={},
        descriptor_refresh_url="", agent_source_url="", agent_source_sha256="",
        telegram_dcs={}, issued_at="", issued_by_authority_generation=1,
    )
    with pytest.raises(ConfigError, match="no exits"):
        render_sing_box_config(seed, {"exits": []}, tproxy_port=12345)
