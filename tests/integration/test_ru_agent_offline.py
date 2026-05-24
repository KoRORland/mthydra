"""Spec E — RU agent offline integration test.

Builds a stub seed.json in a tmp dir, mocks all subprocess + iptables + HTTP
calls, runs the agent's startup sequence end-to-end, asserts config files
render correctly and a descriptor change triggers a config rewrite.
"""
import base64
import hashlib
import json
import struct

import pytest


def _build_test_seed(tmp_path):
    """Build a seed dict with real Ed25519 keys + signed credential + signed descriptor."""
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from mthydra.descriptor.authority import (
        generate_authority_keypair, sign_onward_credential,
    )

    priv_auth, pub_auth = generate_authority_keypair()
    cred = sign_onward_credential(
        priv_auth, box_id="01HXAA", issued_at="2026-05-23T00:00:00Z",
        authority_generation=2,
    )

    # Descriptor: signed with an Ed25519 key whose pubkey we put in trust anchors.
    desc_priv = ed25519.Ed25519PrivateKey.generate()
    desc_pub_raw = desc_priv.public_key().public_bytes_raw()
    desc_payload = {
        "schema": "mthydra.descriptor.v2", "generation": 5,
        "signed_at": "2026-05-23T00:00:00Z",
        "valid_until": "2026-05-24T00:00:00Z",
        "exits": [
            {"fingerprint": "fp1", "endpoint": "1.2.3.4:443",
             "weight": 1, "cover_sni": "eu1cover.example",
             "reality_pubkey": "EUPUB1"},
        ],
    }
    desc_payload_bytes = json.dumps(
        desc_payload, sort_keys=True, separators=(",", ":")).encode()
    desc_sig = desc_priv.sign(desc_payload_bytes)
    desc_blob = struct.pack(">H", len(desc_payload_bytes)) + desc_payload_bytes + desc_sig

    binary_bytes = b"fake-mtg-binary" * 1000
    sha = hashlib.sha256(binary_bytes).hexdigest()

    seed = {
        "schema": "mthydra.ru_seed.v2",
        "box_id": "01HXAA",
        "sni": "cover.example",
        "transport_role": "ru_relay",
        "reality_uuid": "9a8b-uuid",
        "onward_credential": base64.b64encode(cred).decode(),
        "authority_pubkey_pem": pub_auth,
        "descriptor_trust_anchors": [base64.b64encode(desc_pub_raw).decode()],
        "initial_descriptor": base64.b64encode(desc_blob).decode(),
        "image": {
            "version": sha, "url": "https://b2/mtg",
            "url_expires_at": "2026-05-23T01:00:00Z",
            "sha256": sha, "size_bytes": len(binary_bytes),
        },
        "descriptor_refresh_url": "https://b2/descriptors/current",
        "agent_source_url": "https://b2/agent.tar.gz",
        "agent_source_sha256": "deadbeef" * 8,
        "telegram_dcs": {"v4": ["149.154.160.0/20"], "v6": []},
        "issued_at": "2026-05-23T00:00:00Z",
        "issued_by_authority_generation": 2,
    }
    seed_path = tmp_path / "seed.json"
    seed_path.write_text(json.dumps(seed))
    return seed_path, binary_bytes, desc_priv, desc_pub_raw


def test_ru_agent_startup_renders_configs_and_installs_iptables(tmp_path, monkeypatch):
    seed_path, binary_bytes, _, _ = _build_test_seed(tmp_path)
    from mthydra.ru_agent import (
        binary as bin_mod, config_gen, iptables, seed as seed_mod,
    )

    # Patch HTTP fetch for the binary.
    def fake_urlopen(req, timeout=None):
        class _R:
            status = 200
            def read(self): return binary_bytes
            def __enter__(self): return self
            def __exit__(self, *a): pass
        return _R()
    monkeypatch.setattr(bin_mod.urllib.request, "urlopen", fake_urlopen)

    # Patch iptables subprocess.
    iptables_calls = []
    monkeypatch.setattr(iptables.subprocess, "run",
                         lambda cmd, **kw: iptables_calls.append(cmd)
                         or type("R", (), {"returncode": 0})())

    # Step 1: load + verify seed.
    seed = seed_mod.load(seed_path)
    seed_mod.verify_credential(seed)

    # Step 2: download binary.
    out_bin = tmp_path / "mtg"
    bin_mod.download_and_verify(
        url=seed.image["url"], expected_sha256=seed.image["sha256"],
        out_path=out_bin,
    )
    assert out_bin.read_bytes() == binary_bytes

    # Step 3: render configs.
    n = struct.unpack(">H", seed.initial_descriptor[:2])[0]
    desc = json.loads(seed.initial_descriptor[2:2 + n])
    mtg_toml = config_gen.render_mtg_config(seed, sing_box_socks_port=12345)
    sb_json = config_gen.render_sing_box_config(seed, desc, tproxy_port=12345)
    assert b"cover.example" in mtg_toml
    sb_payload = json.loads(sb_json)
    assert sb_payload["inbounds"][0]["type"] == "tproxy"
    assert sb_payload["inbounds"][0]["listen_port"] == 12345

    # Step 4: install iptables.
    iptables.install(
        dc_cidrs_v4=seed.telegram_dcs["v4"], dc_cidrs_v6=seed.telegram_dcs["v6"],
        tproxy_port=12345,
    )
    assert any("149.154.160.0/20" in " ".join(c) for c in iptables_calls)


def test_descriptor_refresh_triggers_config_rewrite(tmp_path, monkeypatch):
    """Simulate a new descriptor arriving over B2; agent rewrites sing-box.json."""
    seed_path, _, desc_priv, desc_pub_raw = _build_test_seed(tmp_path)
    from mthydra.ru_agent import config_gen, descriptor_refresh, seed as seed_mod

    seed = seed_mod.load(seed_path)
    n = struct.unpack(">H", seed.initial_descriptor[:2])[0]
    initial_desc = json.loads(seed.initial_descriptor[2:2 + n])

    # Build a NEW descriptor with an additional exit.
    new_payload = {
        **initial_desc,
        "generation": 6,
        "exits": initial_desc["exits"] + [
            {"fingerprint": "fp2", "endpoint": "9.9.9.9:443",
             "weight": 1, "cover_sni": "eu2cover.example",
             "reality_pubkey": "EUPUB2"},
        ],
    }
    new_payload_bytes = json.dumps(
        new_payload, sort_keys=True, separators=(",", ":")).encode()
    new_sig = desc_priv.sign(new_payload_bytes)
    new_blob = struct.pack(">H", len(new_payload_bytes)) + new_payload_bytes + new_sig

    rewrites = []
    def rewrite(blob):
        m = struct.unpack(">H", blob[:2])[0]
        p = json.loads(blob[2:2 + m])
        sb = config_gen.render_sing_box_config(seed, p, tproxy_port=12345)
        rewrites.append(sb)

    loop = descriptor_refresh.RefreshLoop(
        url="https://b2/desc", trust_anchors=[desc_pub_raw],
        initial_descriptor=seed.initial_descriptor,
        rewrite_fn=rewrite,
        fetch_fn=lambda url, ims: (new_blob, "2026-05-23T01:00:00Z"),
        terminate_fn=lambda r: pytest.fail(f"unexpected terminate: {r}"),
    )
    loop.tick()
    assert len(rewrites) == 1
    payload = json.loads(rewrites[0])
    # New descriptor has 2 exits -> 2 vless outbounds.
    vless = [o for o in payload["outbounds"] if o["type"] == "vless"]
    assert len(vless) == 2
