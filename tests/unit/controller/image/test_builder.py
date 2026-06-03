"""Spec D — image.builder unit tests."""
import hashlib
import json
from unittest.mock import MagicMock

import pytest

from mthydra.controller.image.builder import BuildError, build_image
from mthydra.controller.state.db import connect
from mthydra.controller.state.ru_images import get_image, list_images
from mthydra.controller.state.schema import apply_schema


@pytest.fixture
def conn(tmp_db_path):
    c = connect(tmp_db_path)
    apply_schema(c)
    return c


def _mock_http(release_json, asset_bytes, checksum_text):
    """Build a MagicMock http_client whose .get(url) returns sensible responses."""
    def _get(url):
        resp = MagicMock()
        if url.endswith("/releases/tags/v2.1.7"):
            resp.status = 200
            resp.read.return_value = json.dumps(release_json).encode("utf-8")
        elif url.endswith("/mtg-linux-amd64"):
            resp.status = 200
            resp.read.return_value = asset_bytes
        elif url.endswith("/SHA256SUMS") or url.endswith(".sha256"):
            resp.status = 200
            resp.read.return_value = checksum_text.encode("utf-8")
        else:
            resp.status = 404
            resp.read.return_value = b""
        return resp
    return _get


def test_build_image_happy_path(conn, tmp_path):
    asset_bytes = b"mtg-binary-bytes" * 100
    sha = hashlib.sha256(asset_bytes).hexdigest()
    checksum_text = f"{sha}  mtg-linux-amd64\n"
    release_json = {
        "tag_name": "v2.1.7",
        "assets": [
            {"name": "mtg-linux-amd64", "browser_download_url": "https://example/mtg-linux-amd64"},
            {"name": "SHA256SUMS", "browser_download_url": "https://example/SHA256SUMS"},
        ],
    }
    b2 = MagicMock()

    image_version = build_image(
        conn=conn,
        b2_destination=b2,
        upstream_repo="9seconds/mtg",
        upstream_release="v2.1.7",
        asset_filename="mtg-linux-amd64",
        github_api_url="https://api.github.com",
        tmp_dir=tmp_path,
        now="2026-05-21T00:00:00Z",
        http_client=_mock_http(release_json, asset_bytes, checksum_text),
    )

    assert image_version == sha
    b2.put_image.assert_called_once()
    kwargs = b2.put_image.call_args.kwargs
    assert kwargs["image_version"] == sha
    assert kwargs["binary_path"].exists()

    n = get_image(conn, sha)
    assert n.state == "candidate"
    assert n.upstream_release == "v2.1.7"
    assert n.binary_sha256 == sha


def test_build_image_extracts_binary_from_tarball(conn, tmp_path):
    """mtg ships as mtg-<ver>-<arch>.tar.gz containing .../mtg. build_image must
    verify the tarball against the upstream checksum, then extract the ELF and
    register THAT (the agent execs the binary directly — a tarball can't run).
    Regression: the first RU box got a gzip blob at /run/mthydra/mtg.
    Discovered 2026-06-02 (`file` reported gzip data, not ELF)."""
    import io
    import tarfile

    elf_bytes = b"\x7fELF" + b"fake-mtg-binary" * 100
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("mtg-2.2.8-linux-amd64/mtg")
        info.size = len(elf_bytes)
        tf.addfile(info, io.BytesIO(elf_bytes))
    tarball = buf.getvalue()
    tar_sha = hashlib.sha256(tarball).hexdigest()
    elf_sha = hashlib.sha256(elf_bytes).hexdigest()
    asset = "mtg-2.2.8-linux-amd64.tar.gz"
    release_json = {
        "tag_name": "v2.2.8",
        "assets": [
            {"name": asset, "browser_download_url": f"https://example/{asset}"},
            {"name": "SHA256SUMS", "browser_download_url": "https://example/SHA256SUMS"},
        ],
    }

    def _get(url):
        resp = MagicMock()
        if url.endswith("/releases/tags/v2.2.8"):
            resp.status, resp.read.return_value = 200, json.dumps(release_json).encode()
        elif url.endswith(f"/{asset}"):
            resp.status, resp.read.return_value = 200, tarball
        elif url.endswith("/SHA256SUMS"):
            resp.status = 200
            resp.read.return_value = f"{tar_sha}  {asset}\n".encode()
        else:
            resp.status, resp.read.return_value = 404, b""
        return resp

    b2 = MagicMock()
    image_version = build_image(
        conn=conn, b2_destination=b2,
        upstream_repo="9seconds/mtg", upstream_release="v2.2.8",
        asset_filename=asset, github_api_url="https://api.github.com",
        tmp_dir=tmp_path, now="2026-06-02T00:00:00Z", http_client=_get,
    )
    # image_version is the sha of the EXTRACTED ELF, not the tarball.
    assert image_version == elf_sha
    assert image_version != tar_sha
    # and the bytes uploaded are the ELF, not the archive.
    uploaded = b2.put_image.call_args.kwargs["binary_path"].read_bytes()
    assert uploaded == elf_bytes


def test_build_image_checksum_mismatch_raises(conn, tmp_path):
    asset_bytes = b"some bytes"
    checksum_text = "deadbeef" * 8 + "  mtg-linux-amd64\n"  # wrong sha
    release_json = {
        "tag_name": "v2.1.7",
        "assets": [
            {"name": "mtg-linux-amd64", "browser_download_url": "https://example/mtg-linux-amd64"},
            {"name": "SHA256SUMS", "browser_download_url": "https://example/SHA256SUMS"},
        ],
    }
    b2 = MagicMock()

    with pytest.raises(BuildError, match="sha256 mismatch"):
        build_image(
            conn=conn, b2_destination=b2,
            upstream_repo="9seconds/mtg",
            upstream_release="v2.1.7",
            asset_filename="mtg-linux-amd64",
            github_api_url="https://api.github.com",
            tmp_dir=tmp_path,
            now="2026-05-21T00:00:00Z",
            http_client=_mock_http(release_json, asset_bytes, checksum_text),
        )
    b2.put_image.assert_not_called()
    assert list_images(conn) == []


def test_build_image_release_not_found(conn, tmp_path):
    def _get(url):
        resp = MagicMock()
        resp.status = 404
        resp.read.return_value = b'{"message":"Not Found"}'
        return resp
    with pytest.raises(BuildError, match="release"):
        build_image(
            conn=conn, b2_destination=MagicMock(),
            upstream_repo="9seconds/mtg",
            upstream_release="v9.99.99",
            asset_filename="mtg-linux-amd64",
            github_api_url="https://api.github.com",
            tmp_dir=tmp_path,
            now="2026-05-21T00:00:00Z",
            http_client=_get,
        )


def test_build_image_asset_missing(conn, tmp_path):
    release_json = {
        "tag_name": "v2.1.7",
        "assets": [{"name": "OTHER", "browser_download_url": "https://example/OTHER"}],
    }
    def _get(url):
        resp = MagicMock()
        if url.endswith("/releases/tags/v2.1.7"):
            resp.status = 200
            resp.read.return_value = json.dumps(release_json).encode()
        else:
            resp.status = 404
            resp.read.return_value = b""
        return resp
    with pytest.raises(BuildError, match="asset"):
        build_image(
            conn=conn, b2_destination=MagicMock(),
            upstream_repo="9seconds/mtg",
            upstream_release="v2.1.7",
            asset_filename="mtg-linux-amd64",
            github_api_url="https://api.github.com",
            tmp_dir=tmp_path,
            now="2026-05-21T00:00:00Z",
            http_client=_get,
        )


def test_build_image_resolves_project_specific_checksum_filename(conn, tmp_path):
    """Upstream mtg ships its checksums as 'mtg-2.2.8-checksums.txt' — none of
    the canonical names (SHA256SUMS, checksums.txt, <asset>.sha256). The
    fallback substring match picks it up by 'checksum' in the filename.

    Discovered 2026-06-01 on a real mthydra-ops image-prepare run."""
    import io
    import tarfile
    elf_bytes = b"\x7fELF" + b"arm64-mtg-binary-bytes" * 100
    _buf = io.BytesIO()
    with tarfile.open(fileobj=_buf, mode="w:gz") as _tf:
        _info = tarfile.TarInfo("mtg-2.2.8-linux-arm64/mtg")
        _info.size = len(elf_bytes)
        _tf.addfile(_info, io.BytesIO(elf_bytes))
    asset_bytes = _buf.getvalue()             # real tarball (extracted at build)
    sha = hashlib.sha256(asset_bytes).hexdigest()        # tarball sha (checksum)
    elf_sha = hashlib.sha256(elf_bytes).hexdigest()      # what image_version becomes
    asset_name = "mtg-2.2.8-linux-arm64.tar.gz"
    checksum_text = f"{sha}  {asset_name}\n"
    release_json = {
        "tag_name": "v2.2.8",
        "assets": [
            {"name": asset_name,
             "browser_download_url": f"https://example/{asset_name}"},
            {"name": "mtg-2.2.8-checksums.txt",
             "browser_download_url": "https://example/mtg-2.2.8-checksums.txt"},
        ],
    }
    def _get(url):
        resp = MagicMock()
        if url.endswith("/releases/tags/v2.2.8"):
            resp.status = 200
            resp.read.return_value = json.dumps(release_json).encode()
        elif url.endswith(f"/{asset_name}"):
            resp.status = 200
            resp.read.return_value = asset_bytes
        elif url.endswith("/mtg-2.2.8-checksums.txt"):
            resp.status = 200
            resp.read.return_value = checksum_text.encode()
        else:
            resp.status = 404
            resp.read.return_value = b""
        return resp
    # Should not raise — fallback substring matcher picks up the file.
    iv = build_image(
        conn=conn, b2_destination=MagicMock(),
        upstream_repo="9seconds/mtg",
        upstream_release="v2.2.8",
        asset_filename=asset_name,
        github_api_url="https://api.github.com",
        tmp_dir=tmp_path,
        now="2026-06-01T00:00:00Z",
        http_client=_get,
    )
    assert iv == elf_sha


def test_build_image_checksum_file_missing(conn, tmp_path):
    asset_bytes = b"binary"
    release_json = {
        "tag_name": "v2.1.7",
        "assets": [
            {"name": "mtg-linux-amd64", "browser_download_url": "https://example/mtg-linux-amd64"},
        ],
    }
    def _get(url):
        resp = MagicMock()
        if url.endswith("/releases/tags/v2.1.7"):
            resp.status = 200
            resp.read.return_value = json.dumps(release_json).encode()
        elif url.endswith("/mtg-linux-amd64"):
            resp.status = 200
            resp.read.return_value = asset_bytes
        else:
            resp.status = 404
            resp.read.return_value = b""
        return resp
    with pytest.raises(BuildError, match="checksum"):
        build_image(
            conn=conn, b2_destination=MagicMock(),
            upstream_repo="9seconds/mtg",
            upstream_release="v2.1.7",
            asset_filename="mtg-linux-amd64",
            github_api_url="https://api.github.com",
            tmp_dir=tmp_path,
            now="2026-05-21T00:00:00Z",
            http_client=_get,
        )


def test_build_image_b2_upload_failure_no_db_row(conn, tmp_path):
    asset_bytes = b"some bytes"
    sha = hashlib.sha256(asset_bytes).hexdigest()
    checksum_text = f"{sha}  mtg-linux-amd64\n"
    release_json = {
        "tag_name": "v2.1.7",
        "assets": [
            {"name": "mtg-linux-amd64", "browser_download_url": "https://example/mtg-linux-amd64"},
            {"name": "SHA256SUMS", "browser_download_url": "https://example/SHA256SUMS"},
        ],
    }
    b2 = MagicMock()
    b2.put_image.side_effect = RuntimeError("B2 upload failed")

    with pytest.raises(BuildError, match="B2 upload"):
        build_image(
            conn=conn, b2_destination=b2,
            upstream_repo="9seconds/mtg",
            upstream_release="v2.1.7",
            asset_filename="mtg-linux-amd64",
            github_api_url="https://api.github.com",
            tmp_dir=tmp_path,
            now="2026-05-21T00:00:00Z",
            http_client=_mock_http(release_json, asset_bytes, checksum_text),
        )
    assert list_images(conn) == []


def _make_conn(tmp_path):
    """Standalone helper: open a fresh in-memory-like DB in tmp_path with full schema."""
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.schema import apply_schema
    c = connect(tmp_path / "state.sqlite")
    apply_schema(c)
    return c


def test_build_image_idempotent_skip_existing(tmp_path):
    """If a ru_images row exists for the same upstream_release+repo, skip download."""
    conn = _make_conn(tmp_path)
    # Pre-insert a candidate row for the same upstream_release + repo
    conn.execute(
        "INSERT INTO ru_images "
        "(image_version, upstream_release, upstream_repo, binary_url, manifest_url, "
        " binary_sha256, binary_size_bytes, state, built_at) "
        "VALUES ('existing_sha256', 'v2.2.8', '9seconds/mtg', '', '', '', 0, 'candidate', 'now')"
    )
    conn.commit()

    calls = []

    class FakeB2:
        def put_image(self, **kw):
            calls.append(("put_image", kw))

    def never_called_http(url):
        raise AssertionError(f"http should not be called, but got: {url}")

    result = build_image(
        conn=conn,
        b2_destination=FakeB2(),
        upstream_repo="9seconds/mtg",
        upstream_release="v2.2.8",
        asset_filename="mtg-2.2.8-linux-arm64.tar.gz",
        github_api_url="https://api.github.com",
        tmp_dir=tmp_path,
        now="now",
        http_client=never_called_http,
    )

    assert result == "existing_sha256"
    assert len(calls) == 0  # B2 upload not called


def test_default_http_get_omits_accept_header(monkeypatch):
    """Regression: previously _default_http_get sent
    Accept: application/octet-stream for all requests, which made GitHub
    return 415 Unsupported Media Type on the release-metadata API call.
    Removing the header lets GitHub default to JSON for /repos endpoints
    while binary asset downloads (which redirect to GitHub's CDN) work
    regardless of Accept.

    Discovered 2026-06-01 on a real mthydra-ops image-prepare run."""
    from mthydra.controller.image import builder as builder_mod
    captured: dict[str, object] = {}

    def fake_urlopen(req, timeout=None):
        captured["headers"] = dict(req.header_items())
        class _R:
            def getcode(self_):
                return 200
            def read(self_):
                return b'{}'
        return _R()

    monkeypatch.setattr(builder_mod.urllib.request, "urlopen", fake_urlopen)
    builder_mod._default_http_get("https://api.github.com/repos/x/y/releases/tags/v1")
    # No Accept header at all — let GitHub use its default content negotiation.
    assert "Accept" not in captured["headers"]
