"""Spec D — image builder.

build_image() downloads the upstream release artifact + checksum file from
GitHub, verifies sha256, uploads to B2, and inserts a ru_images candidate row.
B2 upload happens BEFORE the DB insert so a failure only leaves a possibly-
orphaned B2 object (visible via head_image), never a phantom catalog row.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from mthydra.controller.state.ru_images import insert_candidate


class BuildError(RuntimeError):
    """Raised when image-build cannot complete safely."""


_CHECKSUM_ASSET_CANDIDATES = ("SHA256SUMS", "checksums.txt")


def _default_http_get(url: str):
    """urllib.request stdlib client; returns a response-like object with
    .status (int) and .read() -> bytes."""
    req = urllib.request.Request(url, headers={"Accept": "application/octet-stream"})
    resp = urllib.request.urlopen(req, timeout=30)
    class _R:
        def __init__(self, r):
            self.status = r.getcode()
            self._r = r
        def read(self):
            return self._r.read()
    return _R(resp)


def build_image(
    *,
    conn: sqlite3.Connection,
    b2_destination,
    upstream_repo: str,
    upstream_release: str,
    asset_filename: str,
    github_api_url: str,
    tmp_dir: Path,
    now: str,
    actor: str = "operator",
    http_client: Callable | None = None,
) -> str:
    """Download upstream binary, verify sha256, upload to B2, insert ru_images.

    Returns the new image_version (hex sha256). Raises BuildError on any
    failure path; never partially writes (B2 upload precedes DB insert).
    """
    get = http_client or _default_http_get
    tmp_dir = Path(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    # 1. Fetch release metadata.
    release_url = f"{github_api_url}/repos/{upstream_repo}/releases/tags/{upstream_release}"
    try:
        resp = get(release_url)
        if resp.status != 200:
            raise BuildError(
                f"release not found: GET {release_url} -> {resp.status}"
            )
        release = json.loads(resp.read())
    except BuildError:
        raise
    except Exception as e:
        raise BuildError(f"GitHub API request failed: {e}") from e

    assets = {a["name"]: a["browser_download_url"] for a in release.get("assets", [])}

    # 2. Locate the binary asset.
    if asset_filename not in assets:
        raise BuildError(
            f"asset {asset_filename!r} not present in release {upstream_release!r}; "
            f"available: {sorted(assets)}"
        )
    binary_url = assets[asset_filename]

    # 3. Locate the checksum file.
    checksum_url: str | None = None
    for name in (f"{asset_filename}.sha256", *_CHECKSUM_ASSET_CANDIDATES):
        if name in assets:
            checksum_url = assets[name]
            break
    if checksum_url is None:
        raise BuildError(
            f"checksum file not in release {upstream_release!r}; "
            f"expected one of: {asset_filename}.sha256, SHA256SUMS, checksums.txt"
        )

    # 4. Download both.
    try:
        binary_bytes = get(binary_url).read()
        checksum_bytes = get(checksum_url).read()
    except Exception as e:
        raise BuildError(f"asset download failed: {e}") from e

    # 5. Verify sha256.
    expected_sha = _parse_checksum_for(asset_filename, checksum_bytes.decode("utf-8", errors="replace"))
    if expected_sha is None:
        raise BuildError(
            f"checksum file does not contain a line for {asset_filename!r}"
        )
    actual_sha = hashlib.sha256(binary_bytes).hexdigest()
    if actual_sha != expected_sha:
        raise BuildError(
            f"sha256 mismatch for {asset_filename!r}: "
            f"upstream={expected_sha} actual={actual_sha}"
        )
    image_version = actual_sha

    # 6. Write the binary into tmp_dir.
    binary_path = tmp_dir / f"image-{image_version}.bin"
    binary_path.write_bytes(binary_bytes)
    binary_path.chmod(0o600)

    # 7. Build manifest.
    manifest_dict = {
        "schema": "mthydra.ru_image.v1",
        "image_version": image_version,
        "upstream_repo": upstream_repo,
        "upstream_release": upstream_release,
        "binary_filename": asset_filename,
        "binary_sha256": image_version,
        "binary_size_bytes": len(binary_bytes),
        "built_at": now,
        "built_by": actor,
    }
    manifest_bytes = json.dumps(manifest_dict, separators=(",", ":")).encode("utf-8")

    # 8. Upload to B2 BEFORE inserting the DB row.
    try:
        b2_destination.put_image(
            image_version=image_version,
            binary_path=binary_path,
            manifest=manifest_bytes,
        )
    except Exception as e:
        raise BuildError(f"B2 upload failed: {e}") from e

    # 9. Insert ru_images candidate row.
    insert_candidate(
        conn,
        image_version=image_version,
        upstream_release=upstream_release,
        upstream_repo=upstream_repo,
        binary_url=f"images/{image_version}/mtg",
        manifest_url=f"images/{image_version}/manifest.json",
        binary_sha256=image_version,
        binary_size_bytes=len(binary_bytes),
        built_at=now,
        actor=actor,
    )
    return image_version


def _parse_checksum_for(asset_filename: str, checksum_text: str) -> str | None:
    """Find the sha256 line for `asset_filename` in a checksum file.

    Supports both `<sha>  <filename>` (SHA256SUMS) and bare-hash (.sha256) formats.
    """
    for line in checksum_text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) == 1 and len(parts[0]) == 64:
            return parts[0].lower()
        if len(parts) >= 2:
            sha, name = parts[0], parts[-1].lstrip("*")
            if name == asset_filename and len(sha) == 64:
                return sha.lower()
    return None
