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


_FAKE_BUILD_SHA = "f" * 64


def _build_stdout() -> str:
    """Stand-in for image-build's actual stdout; image-prepare parses the
    'candidate <sha> registered' line to drive image-promote."""
    return (
        f"image-build: candidate {_FAKE_BUILD_SHA} registered with pinned "
        "profile (release=v2.2.8, profile_recorded_by='operator')\n"
    )


def _fake_run_with_build_output(calls):
    """Factory for the _run_controller monkeypatch: image-build returns
    canonical stdout, everything else returns empty success."""
    def fake_run(*args, check=True, capture=False, env=None):
        calls.append(list(args))
        stdout = _build_stdout() if args[:1] == ("image-build",) else ""
        return subprocess.CompletedProcess(args, 0, stdout, "")
    return fake_run


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
    monkeypatch.setattr(image_ops, "_run_controller",
                        _fake_run_with_build_output(calls), raising=False)
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
    monkeypatch.setattr(image_ops, "_run_controller",
                        _fake_run_with_build_output(calls), raising=False)
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
                        _fake_run_with_build_output(calls), raising=False)
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


def test_cmd_image_prepare_defaults_arch_to_amd64_for_ru_box(monkeypatch, tmp_path):
    """When --arch is not on the CLI, default to the RU-box arch (amd64), NOT the
    controller's. The image runs on the RU box (commonly amd64), not the builder;
    detecting the controller's arch built an arm64 mtg that failed with ENOEXEC on
    an amd64 RU box (first RU box, 2026-06-02). Even on an arm64 controller the
    default must be amd64."""
    monkeypatch.setattr(image_ops, "resolve_latest_tag",
                        lambda **kw: "v2.2.8")
    # Controller is arm64, but the default image must still target amd64.
    monkeypatch.setattr(image_ops, "detect_host_arch",
                        lambda: "linux-arm64")
    calls = []
    monkeypatch.setattr(image_ops, "_run_controller",
                        _fake_run_with_build_output(calls), raising=False)
    rc = image_ops.cmd_image_prepare(_prepare_args(tmp_path, arch=None))
    assert rc == 0
    ib = next(a for a in calls if a[0] == "image-build")
    asset_idx = ib.index("--asset")
    assert ib[asset_idx + 1] == "mtg-2.2.8-linux-amd64.tar.gz"


def test_cmd_image_prepare_honors_explicit_arch_override(monkeypatch, tmp_path):
    """--arch on the CLI wins over host auto-detect (operator might be on
    arm64 EU controller but want amd64 mtg for RU boxes)."""
    monkeypatch.setattr(image_ops, "resolve_latest_tag",
                        lambda **kw: "v2.2.8")
    # Auto-detect would say arm64 but operator passed amd64 — explicit wins.
    monkeypatch.setattr(image_ops, "detect_host_arch",
                        lambda: "linux-arm64")
    calls = []
    monkeypatch.setattr(image_ops, "_run_controller",
                        _fake_run_with_build_output(calls), raising=False)
    rc = image_ops.cmd_image_prepare(_prepare_args(tmp_path, arch="linux-amd64"))
    assert rc == 0
    ib = next(a for a in calls if a[0] == "image-build")
    asset_idx = ib.index("--asset")
    assert ib[asset_idx + 1] == "mtg-2.2.8-linux-amd64.tar.gz"


def test_cmd_image_prepare_passes_sha_to_promote_not_tag_alias(monkeypatch, tmp_path):
    """Regression: image-prepare used to pass `iv-{tag}` to image-promote,
    but image-build stores the image keyed by the binary's sha256. The two
    names don't match → 'image_profiles row missing' on promote. The fix
    parses the sha from image-build's stdout and passes THAT to promote.
    Discovered 2026-06-01 on a real run."""
    monkeypatch.setattr(image_ops, "resolve_latest_tag", lambda **kw: "v2.2.8")
    calls = []
    monkeypatch.setattr(image_ops, "_run_controller",
                        _fake_run_with_build_output(calls), raising=False)
    rc = image_ops.cmd_image_prepare(_prepare_args(tmp_path))
    assert rc == 0
    promote = next(a for a in calls if a[0] == "image-promote")
    # The argument right after 'image-promote' is the image_version.
    # MUST be the binary's sha256, NOT 'iv-v2.2.8'.
    assert promote[1] == _FAKE_BUILD_SHA
    assert not promote[1].startswith("iv-")


def test_parse_image_version_from_build_output():
    """Helper extracts the sha256 from image-build's canonical line."""
    out = (
        "image-build: candidate "
        + "a" * 64
        + " registered with pinned profile (release=v2.2.8)\n"
    )
    assert image_ops._parse_image_version_from_build_output(out) == "a" * 64
    # Missing line → None (defensive, caller errors clearly).
    assert image_ops._parse_image_version_from_build_output(
        "image-build: something else") is None
    # Wrong shape (not 64 hex chars) → None.
    assert image_ops._parse_image_version_from_build_output(
        "image-build: candidate iv-v2.2.8 registered") is None


def test_cmd_image_prepare_surfaces_build_stderr_on_failure(monkeypatch, tmp_path, capsys):
    """Regression: when image-build is invoked with capture=True, a non-zero
    exit raises CalledProcessError with stdout/stderr in the exception.
    image-prepare must echo those streams before printing 'see above' —
    otherwise the operator sees only 'exit 1: see above' with nothing above
    to actually look at. Discovered 2026-06-01."""
    monkeypatch.setattr(image_ops, "resolve_latest_tag", lambda **kw: "v2.2.8")
    def fake_run(*args, check=True, capture=False, env=None):
        if args[:1] == ("image-build",):
            err = subprocess.CalledProcessError(1, args)
            err.stdout = ""
            err.stderr = "image-build: sha256 mismatch for the binary\n"
            raise err
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(image_ops, "_run_controller", fake_run, raising=False)
    rc = image_ops.cmd_image_prepare(_prepare_args(tmp_path))
    assert rc == 1
    captured = capsys.readouterr()
    assert "sha256 mismatch for the binary" in captured.err
    assert "image-build failed (exit 1)" in captured.err
