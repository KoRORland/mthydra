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
