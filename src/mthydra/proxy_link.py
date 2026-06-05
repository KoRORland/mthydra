"""Single source of the MTProto FakeTLS secret + tg proxy link (spec K2-D1).

Imported by BOTH the RU box config generator (ru_agent.config_gen) and the EU
distribution payload builder, so the link the user receives is always exactly
what the box's mtg accepts. Do not duplicate this formula elsewhere.
"""
from __future__ import annotations

import hashlib
from urllib.parse import urlencode


def derive_mtg_secret(reality_uuid: str, sni: str) -> str:
    """mtg FakeTLS secret: `ee` + 16 secret bytes (hex) + cover SNI (hex).

    mtg rejects a bare 16-byte hex digest ("incorrect first byte of secret");
    the `ee` type byte selects FakeTLS. The 16 secret bytes are derived
    deterministically from the box's reality_uuid."""
    secret16 = hashlib.sha256(reality_uuid.encode()).digest()[:16]
    return "ee" + secret16.hex() + sni.encode("utf-8").hex()


def build_proxy_url(public_ip: str, port: int, secret: str) -> str:
    """Clickable Telegram proxy link (https scheme works on mobile + desktop)."""
    q = urlencode({"server": public_ip, "port": port, "secret": secret})
    return f"https://t.me/proxy?{q}"
