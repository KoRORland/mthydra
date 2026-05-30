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
        lambda req, timeout=None: _FakeResp(404, b'{"message":"Not Found"}'),
    )
    import pytest
    with pytest.raises(image_ops.ImageOpsError, match="404"):
        image_ops.resolve_latest_tag(upstream_repo="x/y",
                                     github_api_url="https://api.github.com")


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
    assert "--asset" in ib and "mtg-v2.2.8-linux-amd64.tar.gz" in ib
    assert "--profile-json" in ib


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
