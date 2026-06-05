"""Tests for the single-sourced mtg FakeTLS secret + proxy-link derivation."""
from __future__ import annotations

import hashlib

from mthydra import proxy_link


def test_derive_mtg_secret_matches_fakeTLS_formula():
    uuid = "11111111-2222-3333-4444-555555555555"
    sni = "www.cloudflare.com"
    expected = ("ee"
                + hashlib.sha256(uuid.encode()).digest()[:16].hex()
                + sni.encode("utf-8").hex())
    assert proxy_link.derive_mtg_secret(uuid, sni) == expected
    assert proxy_link.derive_mtg_secret(uuid, sni).startswith("ee")


def test_build_proxy_url_shape():
    url = proxy_link.build_proxy_url("185.207.66.216", 443, "eeDEADBEEF")
    assert url == ("https://t.me/proxy?server=185.207.66.216&port=443&secret=eeDEADBEEF")


def test_config_gen_uses_shared_secret(monkeypatch):
    """The secret rendered into mtg.toml must equal proxy_link.derive_mtg_secret
    for the same (reality_uuid, sni) — they must never drift."""
    from types import SimpleNamespace
    from mthydra.ru_agent import config_gen

    seed = SimpleNamespace(reality_uuid="abc-123", sni="discord.com")
    toml = config_gen.render_mtg_config(seed, sing_box_socks_port=1080).decode()
    expected = proxy_link.derive_mtg_secret("abc-123", "discord.com")
    assert f'secret = "{expected}"' in toml
