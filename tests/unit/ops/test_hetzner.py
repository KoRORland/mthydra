"""Tests for mthydra.ops.hetzner — minimal Hetzner Cloud REST client."""
from __future__ import annotations

import json

import pytest

from mthydra.ops.hetzner import HetznerError, create_server


_OK_BODY = json.dumps({
    "server": {
        "id": 12345,
        "name": "mthydra-ru-abc",
        "public_net": {"ipv4": {"ip": "203.0.113.7"}},
        "datacenter": {"location": {"name": "fsn1"}},
    },
})


def _fake_post(status: int, body: str):
    captured = {}

    def post(url, headers, payload):
        captured["url"] = url
        captured["headers"] = headers
        captured["payload"] = payload
        return status, body
    return post, captured


def test_create_server_happy_path():
    post, captured = _fake_post(201, _OK_BODY)
    res = create_server(
        token="tk", name="mthydra-ru-abc", server_type="cx22",
        location="fsn1", image="ubuntu-24.04", ssh_keys=["my-key"],
        user_data="#cloud-config\n", http_post=post,
    )
    assert res.server_id == 12345
    assert res.public_ipv4 == "203.0.113.7"
    assert res.name == "mthydra-ru-abc"
    assert res.location == "fsn1"
    # Auth header + payload shape
    assert captured["headers"]["Authorization"] == "Bearer tk"
    assert captured["payload"]["server_type"] == "cx22"
    assert captured["payload"]["ssh_keys"] == ["my-key"]
    assert captured["payload"]["user_data"].startswith("#cloud-config")
    assert captured["payload"]["start_after_create"] is True


def test_create_server_4xx_raises():
    post, _ = _fake_post(401, '{"error":{"code":"unauthorized"}}')
    with pytest.raises(HetznerError, match="401"):
        create_server(
            token="bad", name="n", server_type="cx22", location="fsn1",
            image="ubuntu-24.04", ssh_keys=["k"], user_data="x", http_post=post,
        )


def test_create_server_5xx_raises():
    post, _ = _fake_post(503, "service unavailable")
    with pytest.raises(HetznerError, match="503"):
        create_server(
            token="tk", name="n", server_type="cx22", location="fsn1",
            image="ubuntu-24.04", ssh_keys=["k"], user_data="x", http_post=post,
        )


def test_create_server_network_error_wrapped():
    def post(url, headers, payload):
        raise ConnectionRefusedError("no route to host")
    with pytest.raises(HetznerError, match="network error"):
        create_server(
            token="tk", name="n", server_type="cx22", location="fsn1",
            image="ubuntu-24.04", ssh_keys=["k"], user_data="x", http_post=post,
        )


def test_create_server_non_json_body():
    post, _ = _fake_post(200, "<html>oops</html>")
    with pytest.raises(HetznerError, match="not JSON"):
        create_server(
            token="tk", name="n", server_type="cx22", location="fsn1",
            image="ubuntu-24.04", ssh_keys=["k"], user_data="x", http_post=post,
        )


def test_create_server_missing_fields():
    post, _ = _fake_post(201, json.dumps({"server": {"id": 1}}))
    with pytest.raises(HetznerError, match="missing expected fields"):
        create_server(
            token="tk", name="n", server_type="cx22", location="fsn1",
            image="ubuntu-24.04", ssh_keys=["k"], user_data="x", http_post=post,
        )
