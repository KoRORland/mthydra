"""mthydra-ops image-prepare — automated image fetch/build/promote (spec P)."""
from __future__ import annotations

import json
import os
import subprocess
import urllib.request

from . import main as _main

_run_controller = _main._run_controller
_DEFAULT_DB = _main._DEFAULT_DB
_DEFAULT_CONFIG = _main._DEFAULT_CONFIG


def _say(msg: str) -> None:
    _main._say(f"image-prepare: {msg}")


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


def cmd_image_prepare(args) -> int:
    """Resolve latest → build → (optionally) promote, in one wizard."""
    tag = args.release
    if tag == "latest":
        _say(f"resolving latest from {args.upstream_repo}")
        try:
            tag = resolve_latest_tag(upstream_repo=args.upstream_repo,
                                     github_api_url=args.github_api_url)
        except ImageOpsError as e:
            _main._err(str(e))
            return 2
        _say(f"latest = {tag}")

    asset = f"mtg-{tag}-{args.arch}.tar.gz"
    _say(f"asset = {asset}")

    if args.profile_json == "auto":
        import json as _j
        import tempfile
        profile = default_profile_json(tag, args.arch)
        fd, profile_path = tempfile.mkstemp(prefix="profile-", suffix=".json")
        with os.fdopen(fd, "w") as f:
            _j.dump(profile, f, indent=2, sort_keys=True)
        _say(f"placeholder profile written to {profile_path}")
    else:
        profile_path = args.profile_json

    try:
        _run_controller(
            "image-build", "--release", tag, "--asset", asset,
            "--profile-json", profile_path,
            "--db-path", args.db_path, "--config", args.config,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        _main._err(f"image-build failed (exit {e.returncode}): see above")
        return e.returncode

    if not args.yes:
        if args.non_interactive:
            _say(f"non-interactive without --yes — image iv-{tag} stays candidate")
            return 0
        ans = input(f"Promote iv-{tag}? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            _say(f"promotion declined — iv-{tag} stays candidate")
            return 0
    try:
        _run_controller(
            "image-promote", f"iv-{tag}",
            "--evidence", f"mthydra-ops image-prepare auto-promote {tag}",
            "--db-path", args.db_path, "--config", args.config,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        _main._err(f"image-promote failed (exit {e.returncode}): see above")
        return e.returncode
    _say(f"iv-{tag} promoted")
    return 0
