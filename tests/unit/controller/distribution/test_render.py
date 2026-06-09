"""Tests for distribution.render — human message + QR (spec K2)."""
from __future__ import annotations

from mthydra.controller.distribution import render
from mthydra.controller.distribution.payload import SubsetBox, SubsetPayload


def _payload(boxes):
    return SubsetPayload(user_id="u1", shard_id="default_shard",
                         generated_at="2026-06-05T00:00:00Z",
                         boxes=tuple(boxes), subset_hash="h")


def _box(n):
    url = f"https://t.me/proxy?server=10.0.0.{n}&port=443&secret=ee{n:02d}"
    return SubsetBox(box_id=f"b{n}", public_ip=f"10.0.0.{n}", port=443,
                     sni="www.cloudflare.com", credential_b64="x", proxy_url=url)


def test_render_numbers_all_boxes_and_makes_one_qr_each():
    msg = render.render_user_message(_payload([_box(1), _box(2)]))
    assert "Proxy 1" in msg.text and "Proxy 2" in msg.text
    assert "https://t.me/proxy?server=10.0.0.1" in msg.text
    assert "https://t.me/proxy?server=10.0.0.2" in msg.text
    assert len(msg.qr) == 2
    for caption, png in msg.qr:
        assert caption.startswith("Proxy ")
        assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_empty_payload_has_no_qr():
    msg = render.render_user_message(_payload([]))
    assert msg.qr == ()
    assert "no prox" in msg.text.lower()
