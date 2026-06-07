import json

from tests.unit.ru_agent.test_seed import _make_seed_dict


def test_v3_seed_with_nfqws_fields_loads(tmp_path):
    from mthydra.ru_agent.seed import load
    p = tmp_path / "seed.json"
    p.write_text(json.dumps(_make_seed_dict(
        schema="mthydra.ru_seed.v3",
        nfqws_url="https://b2/nfqws",
        nfqws_sha256="deadbeef" * 8,
    )))
    seed = load(p)
    assert seed.nfqws_url == "https://b2/nfqws"
    assert seed.nfqws_sha256 == "deadbeef" * 8


def test_v2_seed_without_nfqws_fields_loads_with_none(tmp_path):
    from mthydra.ru_agent.seed import load
    p = tmp_path / "seed.json"
    p.write_text(json.dumps(_make_seed_dict(schema="mthydra.ru_seed.v2")))
    seed = load(p)
    assert seed.nfqws_url is None
    assert seed.nfqws_sha256 is None


def test_v3_seed_without_nfqws_fields_loads_with_none(tmp_path):
    from mthydra.ru_agent.seed import load
    p = tmp_path / "seed.json"
    p.write_text(json.dumps(_make_seed_dict(schema="mthydra.ru_seed.v3")))
    seed = load(p)
    assert seed.nfqws_url is None
    assert seed.nfqws_sha256 is None
