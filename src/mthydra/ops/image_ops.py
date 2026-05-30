"""mthydra-ops image-prepare — automated image fetch/build/promote (spec P)."""
from __future__ import annotations

import json
import urllib.request


class ImageOpsError(RuntimeError):
    pass


def resolve_latest_tag(*, upstream_repo: str, github_api_url: str) -> str:
    """Query GitHub's `releases/latest` endpoint, return the `tag_name`.

    Excludes drafts + prereleases by GitHub's own semantics."""
    url = f"{github_api_url}/repos/{upstream_repo}/releases/latest"
    req = urllib.request.Request(
        url, headers={"Accept": "application/vnd.github+json"})
    resp = urllib.request.urlopen(req, timeout=30)
    status = resp.getcode()
    if status != 200:
        raise ImageOpsError(
            f"GitHub releases/latest returned {status} for {upstream_repo!r}")
    body = json.loads(resp.read())
    tag = body.get("tag_name")
    if not tag:
        raise ImageOpsError(
            f"GitHub releases/latest for {upstream_repo!r} has no tag_name")
    return str(tag)


def default_profile_json(tag: str, arch: str) -> dict:
    """Minimal placeholder profile for MVP image-prepare flows. NOT a real
    captured profile — a real one comes from running probes against a soaked
    canary box and recording the observed handshake/timing fingerprints."""
    return {
        "image_version": f"iv-{tag}",
        "transport_build_hash": f"placeholder-{tag}-{arch}",
        "tls_handshake": {
            "expected_cipher_order": [
                "TLS_AES_128_GCM_SHA256",
                "TLS_AES_256_GCM_SHA384",
                "TLS_CHACHA20_POLY1305_SHA256",
            ],
            "expected_extensions": [
                "server_name", "supported_versions",
                "key_share", "supported_groups",
            ],
        },
        "malformed_input_response": {
            "tcp_reset_within_ms": 250,
            "no_application_layer_response": True,
        },
        "expected_surface": [443],
        "baseline_latency_ms": {"p50": 50, "p95": 200},
        "notes": "MVP placeholder — replace with a real profile captured "
                 "from a soaked canary before relying on probe verdicts.",
    }
