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
    # mtg FakeTLS secret must be ee-prefixed + carry the cover domain hex,
    # else mtg rejects it ("incorrect first byte of secret"). Caught by the
    # agent-boot harness, 2026-06-03.
    import re
    m = re.search(r'secret = "([0-9a-f]+)"', text)
    assert m, text
    secret = m.group(1)
    assert secret.startswith("ee")
    assert secret.endswith(b"cover.example".hex())
    assert len(secret) == 2 + 32 + len(b"cover.example".hex())


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
        "eu_exit_set": [
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
    # sing-box's Reality client requires uTLS, else it exits "uTLS is required
    # by reality client". Caught by the agent-boot harness, 2026-06-03.
    for o in vless_outbounds:
        assert o["tls"]["utls"]["enabled"] is True
        assert o["tls"]["utls"]["fingerprint"]
        assert o["tls"]["reality"]["enabled"] is True
    selector = next(o for o in payload["outbounds"] if o["type"] == "selector")
    assert set(selector["outbounds"]) == {"exit-fp1", "exit-fp2"}
    inbound = payload["inbounds"][0]
    assert inbound["type"] == "redirect"
    assert inbound["listen_port"] == 12345
    # sing-box 1.13 'redirect' inbound rejects 'network'/'sniff' (hard parse
    # error). Caught by the agent-boot harness, 2026-06-03.
    assert "network" not in inbound
    assert "sniff" not in inbound


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
        render_sing_box_config(seed, {"eu_exit_set": []}, tproxy_port=12345)


def test_render_sing_box_config_consumes_real_controller_descriptor(tmp_path):
    """Regression (2026-06-02, first RU box): the agent must read the descriptor
    shape the controller actually signs. canonical_bytes emits key 'eu_exit_set';
    the agent read 'exits', so every real box saw zero exits and refused. Build
    the descriptor via the controller's own canonical_bytes so any future
    key/field drift between the two sides fails here instead of on a live box."""
    from mthydra.descriptor.payload import (
        DescriptorPayload,
        EUExit,
        canonical_bytes,
    )
    from mthydra.ru_agent.config_gen import render_sing_box_config
    from mthydra.ru_agent.seed import Seed

    payload = DescriptorPayload(
        generation=5, signing_key_gen=1,
        issued_at="2026-06-02T00:00:00Z", valid_until="2026-06-03T00:00:00Z",
        eu_exit_set=(
            EUExit(fingerprint="fp1", endpoint="1.2.3.4:443", weight=1,
                   cover_sni="eu1.example", reality_pubkey="PUB1"),
        ),
        previous_generation_hash=None, next_signing_pubkey=None,
    )
    descriptor_payload = json.loads(canonical_bytes(payload))
    seed = Seed(
        box_id="b1", sni="cover.example", transport_role="ru_relay",
        reality_uuid="u1", onward_credential=b"", authority_pubkey_pem="",
        descriptor_trust_anchors=(), initial_descriptor=b"", image={},
        descriptor_refresh_url="", agent_source_url="", agent_source_sha256="",
        telegram_dcs={}, issued_at="", issued_by_authority_generation=1,
    )
    out = render_sing_box_config(seed, descriptor_payload, tproxy_port=12345)
    p = json.loads(out)
    vless = [o for o in p["outbounds"] if o["type"] == "vless"]
    assert len(vless) == 1
    assert vless[0]["tag"] == "exit-fp1"
    assert vless[0]["tls"]["server_name"] == "eu1.example"
