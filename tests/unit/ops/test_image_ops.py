from __future__ import annotations

import json

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
