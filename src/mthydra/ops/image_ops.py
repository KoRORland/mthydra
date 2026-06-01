"""mthydra-ops image-prepare — automated image fetch/build/promote (spec P)."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

from . import main as _main

_run_controller = _main._run_controller
_DEFAULT_DB = _main._DEFAULT_DB
_DEFAULT_CONFIG = _main._DEFAULT_CONFIG


def _say(msg: str) -> None:
    _main._say(f"image-prepare: {msg}")


class ImageOpsError(RuntimeError):
    pass


_SEMVER_TAG_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def resolve_latest_tag(*, upstream_repo: str, github_api_url: str) -> str:
    """Query GitHub's `releases/latest` endpoint, return the `tag_name`.

    Excludes drafts + prereleases by GitHub's own semantics.

    S-Task 2: on 404 (repo has tags but no GitHub Releases — common for
    private projects that ship via `git tag` + `git push origin <tag>`),
    fall back to `git ls-remote --tags` and return the highest
    version-shaped tag. Lets `mthydra-ops upgrade` resolve a default
    target without requiring the operator to cut a Release per version.
    """
    url = f"{github_api_url}/repos/{upstream_repo}/releases/latest"
    req = urllib.request.Request(
        url, headers={"Accept": "application/vnd.github+json"})
    try:
        resp = urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return _resolve_latest_tag_via_git_ls_remote(upstream_repo)
        raise ImageOpsError(
            f"GitHub releases/latest returned HTTP {e.code} for "
            f"{upstream_repo!r}: {e.reason}") from e
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


def _resolve_latest_tag_via_git_ls_remote(upstream_repo: str) -> str:
    """Fallback for repos that have no GitHub Releases but do have tags.
    Lists remote tags via git ls-remote, parses version-shaped names
    (v1.2.3 / 1.2.3), and returns the highest by semver tuple."""
    git_url = f"https://github.com/{upstream_repo}.git"
    res = subprocess.run(
        ["git", "ls-remote", "--tags", "--refs", git_url, "v*"],
        capture_output=True, text=True, timeout=30,
    )
    if res.returncode != 0:
        raise ImageOpsError(
            f"git ls-remote fallback failed for {upstream_repo!r}: "
            f"{res.stderr.strip() or res.stdout.strip()}")
    candidates: list[tuple[tuple[int, int, int], str]] = []
    for line in res.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        ref = parts[1].removeprefix("refs/tags/")
        m = _SEMVER_TAG_RE.match(ref)
        if m:
            candidates.append(
                ((int(m.group(1)), int(m.group(2)), int(m.group(3))), ref))
    if not candidates:
        raise ImageOpsError(
            f"no version-shaped tags (vN.N.N) found in {upstream_repo!r}; "
            "create one with `git tag v0.0.1 && git push origin v0.0.1`")
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


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


_MACHINE_TO_MTG_ARCH = {
    "x86_64": "linux-amd64", "amd64": "linux-amd64",
    "aarch64": "linux-arm64", "arm64": "linux-arm64",
    "armv7l": "linux-armv7", "armv7": "linux-armv7",
    "armv6l": "linux-armv6", "armv6": "linux-armv6",
    "i386": "linux-386", "i686": "linux-386",
}


def detect_host_arch() -> str:
    """Map platform.machine() to mtg's release-asset arch suffix.
    Falls back to linux-amd64 (most common RU VPS arch) on unknown
    platforms — operator can always override with --arch.
    """
    import platform
    return _MACHINE_TO_MTG_ARCH.get(platform.machine().lower(), "linux-amd64")


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

    # If --arch wasn't passed (None from new default), auto-detect from
    # the host so an operator on a non-amd64 EC2 (e.g. arm64 t4g.small)
    # doesn't have to know the magic asset-name dialect.
    arch = args.arch or detect_host_arch()
    if args.arch is None:
        _say(f"arch (auto-detected from host) = {arch}")

    # Upstream tags are 'v2.2.8' but the release asset filenames use the
    # version *without* the v prefix: 'mtg-2.2.8-linux-amd64.tar.gz'. Strip
    # the leading 'v' when building the asset name; keep the original tag
    # for the GitHub API call (which uses the tag verbatim).
    asset_version = tag[1:] if tag.startswith("v") and len(tag) > 1 else tag
    asset = f"mtg-{asset_version}-{arch}.tar.gz"
    _say(f"asset = {asset}")

    if args.profile_json == "auto":
        import json as _j
        import tempfile
        profile = default_profile_json(tag, arch)
        fd, profile_path = tempfile.mkstemp(prefix="profile-", suffix=".json")
        with os.fdopen(fd, "w") as f:
            _j.dump(profile, f, indent=2, sort_keys=True)
        _say(f"placeholder profile written to {profile_path}")
    else:
        profile_path = args.profile_json

    # image-build's stdout is the only place that carries the canonical
    # image_version — the sha256 of the actual binary, NOT the upstream tag.
    # image-promote needs THAT sha (it's the primary key in ru_images);
    # passing `iv-{tag}` as we used to fails with "image_profiles row missing".
    try:
        res = _run_controller(
            "image-build", "--release", tag, "--asset", asset,
            "--profile-json", profile_path,
            "--db-path", args.db_path, "--config", args.config,
            check=True, capture=True,
        )
    except subprocess.CalledProcessError as e:
        # capture=True swallows the child's output streams into the
        # exception; surface them so "see above" actually points at
        # something. (Previously the operator just saw "exit N: see
        # above" with no above.)
        if e.stdout:
            print(e.stdout, end="")
        if e.stderr:
            print(e.stderr, end="", file=sys.stderr)
        _main._err(f"image-build failed (exit {e.returncode}): see above")
        return e.returncode
    # Echo the captured output so the operator can still see what happened.
    if res.stdout:
        print(res.stdout, end="")
    image_version = _parse_image_version_from_build_output(res.stdout or "")
    if image_version is None:
        _main._err(
            "image-build succeeded but stdout did not contain the canonical "
            "image_version line ('candidate <sha> registered'); cannot drive "
            "image-promote automatically. Run image-promote by hand using "
            "the version printed above.")
        return 4
    _say(f"image_version = {image_version}")

    if not args.yes:
        if args.non_interactive:
            _say(f"non-interactive without --yes — image {image_version} stays candidate")
            return 0
        ans = input(f"Promote {image_version}? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            _say(f"promotion declined — {image_version} stays candidate")
            return 0
    try:
        _run_controller(
            "image-promote", image_version,
            "--evidence", f"mthydra-ops image-prepare auto-promote {tag}",
            "--db-path", args.db_path, "--config", args.config,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        _main._err(f"image-promote failed (exit {e.returncode}): see above")
        return e.returncode
    _say(f"{image_version} promoted")
    return 0


_BUILD_OUTPUT_RE = re.compile(
    r"^image-build: candidate ([0-9a-f]{64}) registered", re.MULTILINE,
)


def _parse_image_version_from_build_output(text: str) -> str | None:
    """image-build prints 'image-build: candidate <sha256> registered ...'
    The sha is the canonical primary key in ru_images. Returns it or None
    when the expected line is absent (defensive against output reshaping)."""
    m = _BUILD_OUTPUT_RE.search(text)
    return m.group(1) if m else None
