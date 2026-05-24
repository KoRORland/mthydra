import hashlib
import pytest


def test_download_and_verify_writes_chmod_executable(tmp_path, monkeypatch):
    from mthydra.ru_agent import binary
    payload = b"binary-bytes" * 100
    sha = hashlib.sha256(payload).hexdigest()

    def fake_urlopen(req, timeout=None):
        class _R:
            status = 200
            def read(self_inner): return payload
            def __enter__(self_inner): return self_inner
            def __exit__(self_inner, *a): pass
        return _R()
    monkeypatch.setattr(binary.urllib.request, "urlopen", fake_urlopen)

    out = tmp_path / "mtg"
    binary.download_and_verify(
        url="https://x/mtg", expected_sha256=sha, out_path=out,
    )
    assert out.read_bytes() == payload
    assert out.stat().st_mode & 0o111  # executable bit set


def test_download_rejects_sha_mismatch(tmp_path, monkeypatch):
    from mthydra.ru_agent import binary

    def fake_urlopen(req, timeout=None):
        class _R:
            status = 200
            def read(self_inner): return b"actual-bytes"
            def __enter__(self_inner): return self_inner
            def __exit__(self_inner, *a): pass
        return _R()
    monkeypatch.setattr(binary.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(binary.BinaryError, match="sha256 mismatch"):
        binary.download_and_verify(
            url="https://x/mtg", expected_sha256="0" * 64,
            out_path=tmp_path / "mtg",
        )


def test_download_rejects_http_error(tmp_path, monkeypatch):
    from mthydra.ru_agent import binary
    import urllib.error

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url if hasattr(req, "full_url") else "x", 403,
            "Forbidden", None, None,
        )
    monkeypatch.setattr(binary.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(binary.BinaryError, match="HTTP"):
        binary.download_and_verify(
            url="https://x/mtg", expected_sha256="0" * 64,
            out_path=tmp_path / "mtg",
        )
