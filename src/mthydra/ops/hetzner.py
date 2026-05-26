"""Minimal Hetzner Cloud client — spec M2 (mthydra-ops ru-provision).

Only one endpoint matters: POST /v1/servers. We use stdlib urllib so the
operator-side script doesn't drag in an additional dependency.

Docs: https://docs.hetzner.cloud/#servers-create-a-server
"""
from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class HetznerServerResult:
    server_id: int
    name: str
    public_ipv4: str
    location: str


class HetznerError(RuntimeError):
    pass


def create_server(
    *,
    token: str,
    name: str,
    server_type: str,
    location: str,
    image: str,
    ssh_keys: list[str],
    user_data: str,
    http_post=None,
) -> HetznerServerResult:
    """POST /v1/servers and return the assigned IPv4 address.

    `ssh_keys` is a list of SSH key NAMES or IDs registered in the operator's
    Hetzner project. Required even if you never plan to SSH — Hetzner refuses
    the create without at least one. (You can register a throwaway key just
    for this; the agent doesn't use SSH.)

    `http_post` is injectable for tests. Signature:
        http_post(url, headers: dict, body: dict) -> (status_code, response_text)
    """
    if http_post is None:
        http_post = _default_http_post
    payload = {
        "name": name,
        "server_type": server_type,
        "location": location,
        "image": image,
        "ssh_keys": ssh_keys,
        "user_data": user_data,
        "start_after_create": True,
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    try:
        status, body = http_post(
            "https://api.hetzner.cloud/v1/servers", headers, payload,
        )
    except Exception as e:
        raise HetznerError(f"network error: {e}") from e
    if not 200 <= status < 300:
        raise HetznerError(f"hetzner POST /servers returned {status}: {body[:500]}")
    try:
        obj = json.loads(body)
    except json.JSONDecodeError as e:
        raise HetznerError(f"hetzner response is not JSON: {e}") from e
    try:
        srv = obj["server"]
        return HetznerServerResult(
            server_id=int(srv["id"]),
            name=str(srv["name"]),
            public_ipv4=str(srv["public_net"]["ipv4"]["ip"]),
            location=str(srv["datacenter"]["location"]["name"]),
        )
    except (KeyError, TypeError, ValueError) as e:
        raise HetznerError(
            f"hetzner response missing expected fields: {e}; body={body[:500]}"
        ) from e


def _default_http_post(url: str, headers: dict, body: dict) -> tuple[int, str]:
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return int(resp.status), resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return int(e.code), e.read().decode("utf-8", errors="replace")
