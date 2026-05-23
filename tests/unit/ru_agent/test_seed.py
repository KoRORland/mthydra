import base64
import json
import pytest


def _make_seed_dict(**overrides):
    base = {
        "schema": "mthydra.ru_seed.v2",
        "box_id": "01HXAA",
        "sni": "cover.example",
        "transport_role": "ru_relay",
        "reality_uuid": "9a8b-uuid",
        "onward_credential": "BASE64==",
        "authority_pubkey_pem": "-----BEGIN PUBLIC KEY-----\nABC\n-----END PUBLIC KEY-----\n",
        "descriptor_trust_anchors": ["BASE64TRUST=="],
        "initial_descriptor": "BASE64DESC==",
        "image": {
            "version": "abcdef", "url": "https://b2/img", "url_expires_at": "2026-05-23T01:00:00Z",
            "sha256": "abcdef", "size_bytes": 10485760,
        },
        "descriptor_refresh_url": "https://b2/descriptors/current",
        "agent_source_url": "https://b2/agent.tar.gz",
        "agent_source_sha256": "deadbeef" * 8,
        "telegram_dcs": {"v4": ["149.154.160.0/20"], "v6": []},
        "issued_at": "2026-05-23T00:00:00Z",
        "issued_by_authority_generation": 2,
    }
    base.update(overrides)
    return base


def test_load_valid_seed(tmp_path):
    from mthydra.ru_agent.seed import load
    p = tmp_path / "seed.json"
    p.write_text(json.dumps(_make_seed_dict()))
    seed = load(p)
    assert seed.box_id == "01HXAA"
    assert seed.reality_uuid == "9a8b-uuid"
    assert seed.telegram_dcs == {"v4": ["149.154.160.0/20"], "v6": []}


def test_load_rejects_missing_file(tmp_path):
    from mthydra.ru_agent.seed import load, SeedError
    with pytest.raises(SeedError, match="not found"):
        load(tmp_path / "missing.json")


def test_load_rejects_malformed_json(tmp_path):
    from mthydra.ru_agent.seed import load, SeedError
    p = tmp_path / "bad.json"
    p.write_text("not-json")
    with pytest.raises(SeedError, match="not valid JSON"):
        load(p)


def test_load_rejects_wrong_schema(tmp_path):
    from mthydra.ru_agent.seed import load, SeedError
    p = tmp_path / "seed.json"
    p.write_text(json.dumps(_make_seed_dict(schema="mthydra.ru_seed.v99")))
    with pytest.raises(SeedError, match="unsupported seed schema"):
        load(p)


def test_load_rejects_missing_required_field(tmp_path):
    from mthydra.ru_agent.seed import load, SeedError
    d = _make_seed_dict()
    del d["reality_uuid"]
    p = tmp_path / "seed.json"
    p.write_text(json.dumps(d))
    with pytest.raises(SeedError, match="missing required field"):
        load(p)


def test_verify_credential_round_trip(tmp_path):
    """A seed whose onward_credential validates against authority_pubkey_pem
    passes verify_credential()."""
    from mthydra.descriptor.authority import (
        generate_authority_keypair, sign_onward_credential,
    )
    from mthydra.ru_agent.seed import load, verify_credential
    priv, pub = generate_authority_keypair()
    cred = sign_onward_credential(
        priv, box_id="01HXAA", issued_at="2026-05-23T00:00:00Z",
        authority_generation=2,
    )
    d = _make_seed_dict(
        authority_pubkey_pem=pub,
        onward_credential=base64.b64encode(cred).decode(),
    )
    p = tmp_path / "seed.json"
    p.write_text(json.dumps(d))
    seed = load(p)
    payload = verify_credential(seed)
    assert payload.box_id == "01HXAA"


def test_verify_credential_rejects_mismatched_box_id(tmp_path):
    from mthydra.descriptor.authority import (
        generate_authority_keypair, sign_onward_credential,
    )
    from mthydra.ru_agent.seed import load, verify_credential, SeedError
    priv, pub = generate_authority_keypair()
    cred = sign_onward_credential(
        priv, box_id="WRONG", issued_at="2026-05-23T00:00:00Z",
        authority_generation=2,
    )
    d = _make_seed_dict(
        authority_pubkey_pem=pub,
        onward_credential=base64.b64encode(cred).decode(),
    )
    p = tmp_path / "seed.json"
    p.write_text(json.dumps(d))
    seed = load(p)
    with pytest.raises(SeedError, match="box_id mismatch"):
        verify_credential(seed)
