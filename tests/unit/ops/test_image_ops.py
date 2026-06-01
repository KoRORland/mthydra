from __future__ import annotations

import argparse
import json
import subprocess

from mthydra.ops import image_ops


class _FakeResp:
    def __init__(self, status, body):
        self.status = status
        self._body = body
    def read(self):
        return self._body
    def getcode(self):
        return self.status


def test_resolve_latest_tag_uses_releases_latest(monkeypatch):
    seen = {}
    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        return _FakeResp(200, json.dumps({"tag_name": "v2.2.8"}).encode())
    monkeypatch.setattr(image_ops.urllib.request, "urlopen", fake_urlopen)
    tag = image_ops.resolve_latest_tag(
        upstream_repo="9seconds/mtg",
        github_api_url="https://api.github.com",
    )
    assert tag == "v2.2.8"
    assert seen["url"] == "https://api.github.com/repos/9seconds/mtg/releases/latest"


def test_resolve_latest_tag_raises_on_non_200(monkeypatch):
    monkeypatch.setattr(
        image_ops.urllib.request, "urlopen",
        lambda req, timeout=None: _FakeResp(500, b'{"message":"server error"}'),
    )
    import pytest
    with pytest.raises(image_ops.ImageOpsError, match="500"):
        image_ops.resolve_latest_tag(upstream_repo="x/y",
                                     github_api_url="https://api.github.com")


def test_resolve_latest_tag_falls_back_to_git_ls_remote_on_404(monkeypatch):
    """S-Task 2: GitHub 404 means the repo has no Releases (capital R).
    Many private projects ship via `git tag` + `git push origin <tag>`
    without ever creating a Release. Fall back to git ls-remote --tags
    and return the highest version-shaped tag.

    Discovered 2026-06-01: user's first prod `mthydra-ops upgrade`
    (with default --ref) hit 404 because v0.0.3 was a git tag, not a
    GitHub Release."""
    import urllib.error
    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 404, "Not Found", {}, None)
    monkeypatch.setattr(image_ops.urllib.request, "urlopen", fake_urlopen)

    fake_ls_remote_output = (
        "abc123\trefs/tags/v0.0.1\n"
        "def456\trefs/tags/v0.0.2\n"
        "789abc\trefs/tags/v0.0.3\n"
        "deadbe\trefs/tags/v0.1.0\n"   # higher minor wins
        "feedf0\trefs/tags/not-a-version\n"
    )
    def fake_run(argv, **kw):
        assert argv[0:3] == ["git", "ls-remote", "--tags"]
        return subprocess.CompletedProcess(argv, 0, fake_ls_remote_output, "")
    monkeypatch.setattr(image_ops.subprocess, "run", fake_run)

    tag = image_ops.resolve_latest_tag(
        upstream_repo="KoRORland/mthydra",
        github_api_url="https://api.github.com",
    )
    assert tag == "v0.1.0"


def test_resolve_latest_tag_fallback_raises_when_no_version_tags(monkeypatch):
    import urllib.error
    monkeypatch.setattr(
        image_ops.urllib.request, "urlopen",
        lambda req, timeout=None: (_ for _ in ()).throw(
            urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)),
    )
    monkeypatch.setattr(
        image_ops.subprocess, "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "abc\trefs/tags/nightly\n", ""),
    )
    import pytest
    with pytest.raises(image_ops.ImageOpsError, match="no version-shaped tags"):
        image_ops.resolve_latest_tag(
            upstream_repo="x/y", github_api_url="https://api.github.com")


def test_default_profile_json_has_required_schema_fields():
    p = image_ops.default_profile_json("v2.2.8", "linux-amd64")
    assert p["image_version"] == "iv-v2.2.8"
    assert p["transport_build_hash"]
    assert "tls_handshake" in p
    assert "expected_surface" in p
    assert p["expected_surface"] == [443]
    assert "baseline_latency_ms" in p
    assert p["notes"].startswith("MVP placeholder")


def _prepare_args(tmp_path, **over):
    base = dict(
        release="latest", arch="linux-amd64", profile_json="auto",
        yes=True, non_interactive=True,
        db_path=str(tmp_path / "x.sqlite"),
        config=str(tmp_path / "c.toml"),
        upstream_repo="9seconds/mtg",
        github_api_url="https://api.github.com",
        verbose=False, quiet=True, dry_run=False,
    )
    base.update(over)
    return argparse.Namespace(**base)


def test_cmd_image_prepare_end_to_end(monkeypatch, tmp_path):
    monkeypatch.setattr(image_ops, "resolve_latest_tag",
                        lambda **kw: "v2.2.8")
    calls = []
    def fake_run(*args, check=True, capture=False, env=None):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(image_ops, "_run_controller", fake_run, raising=False)
    rc = image_ops.cmd_image_prepare(_prepare_args(tmp_path))
    assert rc == 0
    subs = [a[0] for a in calls]
    assert "image-build" in subs and "image-promote" in subs
    ib = next(a for a in calls if a[0] == "image-build")
    assert "--release" in ib and "v2.2.8" in ib
    # Asset filename must use the version WITHOUT the leading 'v'.
    # Upstream's actual release assets are named e.g. mtg-2.2.8-linux-amd64.tar.gz
    # (no 'v'). Discovered 2026-06-01 — using 'v2.2.8' here yields a
    # "asset not present in release" error.
    asset_idx = ib.index("--asset")
    assert ib[asset_idx + 1] == "mtg-2.2.8-linux-amd64.tar.gz"


def test_cmd_image_prepare_handles_tag_without_v_prefix(monkeypatch, tmp_path):
    """Defensive: if upstream tags a release without the 'v' prefix
    (e.g. '2.3.0' instead of 'v2.3.0'), the asset name is still right."""
    monkeypatch.setattr(image_ops, "resolve_latest_tag",
                        lambda **kw: "2.3.0")
    calls = []
    def fake_run(*args, check=True, capture=False, env=None):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(image_ops, "_run_controller", fake_run, raising=False)
    rc = image_ops.cmd_image_prepare(_prepare_args(tmp_path))
    assert rc == 0
    ib = next(a for a in calls if a[0] == "image-build")
    asset_idx = ib.index("--asset")
    assert ib[asset_idx + 1] == "mtg-2.3.0-linux-amd64.tar.gz"


def test_cmd_image_prepare_skips_promote_without_yes(monkeypatch, tmp_path):
    monkeypatch.setattr(image_ops, "resolve_latest_tag", lambda **kw: "v1.0")
    monkeypatch.setattr("builtins.input", lambda _p: "n")
    calls = []
    monkeypatch.setattr(image_ops, "_run_controller",
        lambda *a, **k: calls.append(list(a))
        or subprocess.CompletedProcess(a, 0, "", ""), raising=False)
    rc = image_ops.cmd_image_prepare(_prepare_args(tmp_path, yes=False,
                                                   non_interactive=False))
    assert rc == 0
    assert [a[0] for a in calls] == ["image-build"]


def test_detect_host_arch_mapping():
    """Auto-detect maps platform.machine() to mtg release-asset arch suffix.
    Covers the common Linux machines an EU controller might run on."""
    import platform
    from mthydra.ops import image_ops as io
    real = platform.machine
    try:
        platform.machine = lambda: "aarch64"
        assert io.detect_host_arch() == "linux-arm64"
        platform.machine = lambda: "x86_64"
        assert io.detect_host_arch() == "linux-amd64"
        platform.machine = lambda: "armv7l"
        assert io.detect_host_arch() == "linux-armv7"
        platform.machine = lambda: "unknown-cpu"  # fallback
        assert io.detect_host_arch() == "linux-amd64"
    finally:
        platform.machine = real


def test_cmd_image_prepare_auto_detects_arch_when_not_passed(monkeypatch, tmp_path):
    """When --arch is not on the CLI (default=None), the wizard uses
    detect_host_arch(). Fixes the 'wrong default arch on arm64 EC2' trap."""
    monkeypatch.setattr(image_ops, "resolve_latest_tag",
                        lambda **kw: "v2.2.8")
    monkeypatch.setattr(image_ops, "detect_host_arch",
                        lambda: "linux-arm64")
    calls = []
    def fake_run(*args, check=True, capture=False, env=None):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(image_ops, "_run_controller", fake_run, raising=False)
    # Pass arch=None to simulate "operator didn't specify".
    rc = image_ops.cmd_image_prepare(_prepare_args(tmp_path, arch=None))
    assert rc == 0
    ib = next(a for a in calls if a[0] == "image-build")
    asset_idx = ib.index("--asset")
    assert ib[asset_idx + 1] == "mtg-2.2.8-linux-arm64.tar.gz"


def test_cmd_image_prepare_honors_explicit_arch_override(monkeypatch, tmp_path):
    """--arch on the CLI wins over host auto-detect (operator might be on
    arm64 EU controller but want amd64 mtg for RU boxes)."""
    monkeypatch.setattr(image_ops, "resolve_latest_tag",
                        lambda **kw: "v2.2.8")
    # Auto-detect would say arm64 but operator passed amd64 — explicit wins.
    monkeypatch.setattr(image_ops, "detect_host_arch",
                        lambda: "linux-arm64")
    calls = []
    def fake_run(*args, check=True, capture=False, env=None):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(image_ops, "_run_controller", fake_run, raising=False)
    rc = image_ops.cmd_image_prepare(_prepare_args(tmp_path, arch="linux-amd64"))
    assert rc == 0
    ib = next(a for a in calls if a[0] == "image-build")
    asset_idx = ib.index("--asset")
    assert ib[asset_idx + 1] == "mtg-2.2.8-linux-amd64.tar.gz"
