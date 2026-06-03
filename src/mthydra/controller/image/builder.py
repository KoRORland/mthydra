"""Spec D — image builder.

build_image() downloads the upstream release artifact + checksum file from
GitHub, verifies sha256, uploads to B2, and inserts a ru_images candidate row.
B2 upload happens BEFORE the DB insert so a failure only leaves a possibly-
orphaned B2 object (visible via head_image), never a phantom catalog row.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import sqlite3
import struct
import tarfile
import urllib.request
from collections.abc import Callable
from pathlib import Path

from mthydra.controller.state.ru_images import insert_candidate


class BuildError(RuntimeError):
    """Raised when image-build cannot complete safely."""


_CHECKSUM_ASSET_CANDIDATES = ("SHA256SUMS", "checksums.txt")


# ELF e_machine values for the arch tokens used in upstream asset names.
_ELF_E_MACHINE = {"amd64": 0x3E, "arm64": 0xB7, "armv7": 0x28,
                  "armv6": 0x28, "386": 0x03}


def _verify_elf(binary: bytes, asset_filename: str) -> None:
    """Assert `binary` is an ELF whose architecture matches the arch token in
    `asset_filename` (e.g. 'linux-amd64'). Raises BuildError otherwise. Catches a
    gzip blob (not extracted) or a wrong-arch artifact at build time, before it
    ever ships to a box. Unknown arch token → ELF-magic check only."""
    if binary[:4] != b"\x7fELF":
        raise BuildError(
            f"extracted binary from {asset_filename!r} is not an ELF "
            f"(magic {binary[:4]!r}) — wrong asset or failed extraction?")
    if len(binary) < 20:
        raise BuildError(f"{asset_filename!r} ELF too short to read e_machine")
    endian = "<" if binary[5] == 1 else ">"
    (e_machine,) = struct.unpack(endian + "H", binary[18:20])
    stem = asset_filename.replace(".tar.gz", "").replace(".tgz", "").replace(".gz", "")
    token = next((p for p in stem.split("-") if p in _ELF_E_MACHINE), None)
    if token is not None and e_machine != _ELF_E_MACHINE[token]:
        raise BuildError(
            f"arch mismatch for {asset_filename!r}: ELF e_machine={e_machine:#x} "
            f"!= expected {token} ({_ELF_E_MACHINE[token]:#x})")


def _extract_runnable(asset_bytes: bytes, asset_filename: str, *, member: str) -> bytes:
    """Return the runnable binary from a release asset.

    mtg ships as `mtg-<ver>-<arch>.tar.gz` containing `mtg-<ver>-<arch>/mtg`;
    the RU agent execs the binary directly, so we must hand it the ELF, not the
    archive. Handles `.tar.gz`/`.tgz` (extract the named member), bare `.gz`
    (decompress), and raw binaries (returned unchanged). Detection is by
    filename, with a gzip-magic fallback. Extracted binaries are ELF/arch-checked
    (the real mtg path); a raw passthrough asset is returned unverified."""
    is_gzip = asset_bytes[:2] == b"\x1f\x8b"
    if asset_filename.endswith((".tar.gz", ".tgz")) or (
        is_gzip and asset_filename.endswith(".tar")
    ):
        try:
            with tarfile.open(fileobj=io.BytesIO(asset_bytes), mode="r:*") as tf:
                for m in tf.getmembers():
                    if m.isfile() and m.name.rsplit("/", 1)[-1] == member:
                        f = tf.extractfile(m)
                        if f is not None:
                            binary = f.read()
                            _verify_elf(binary, asset_filename)
                            return binary
        except tarfile.TarError as e:
            raise BuildError(f"could not read archive {asset_filename!r}: {e}") from e
        raise BuildError(
            f"archive {asset_filename!r} contains no {member!r} file"
        )
    if asset_filename.endswith(".gz") or is_gzip:
        try:
            binary = gzip.decompress(asset_bytes)
        except OSError as e:
            raise BuildError(f"could not gunzip {asset_filename!r}: {e}") from e
        _verify_elf(binary, asset_filename)
        return binary
    return asset_bytes


def _default_http_get(url: str):
    """urllib.request stdlib client; returns a response-like object with
    .status (int) and .read() -> bytes.

    No Accept header: the same function is called for GitHub release
    METADATA (wants JSON — sending Accept: octet-stream made GitHub
    return 415 Unsupported Media Type, breaking image-build) AND for
    the binary asset download (browser_download_url, which redirects
    through to GitHub's CDN and ignores Accept). Default — no Accept
    header — works for both: GitHub API returns JSON by default; the
    asset download follows redirects to objects.githubusercontent.com
    which serves the bytes regardless.
    """
    req = urllib.request.Request(url)
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
    force: bool = False,
) -> str:
    """Download upstream binary, verify sha256, upload to B2, insert ru_images.

    Returns the new image_version (hex sha256). Raises BuildError on any
    failure path; never partially writes (B2 upload precedes DB insert).

    force=True bypasses the same-release idempotency shortcut — needed to
    rebuild when an earlier build registered a bad artifact for this release.
    """
    # S-2: idempotent — skip download + insert if this release already exists.
    # --force bypasses this to rebuild a release whose prior artifact was bad.
    if not force:
        existing = conn.execute(
            "SELECT image_version FROM ru_images "
            "WHERE upstream_release=? AND upstream_repo=? "
            "LIMIT 1",
            (upstream_release, upstream_repo),
        ).fetchone()
        if existing is not None:
            return existing[0]

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
    # Exact-match the conventional names first (preserves existing behavior),
    # then fall back to a substring scan for any asset that looks like a
    # checksum manifest. mtg ships its checksums as
    # 'mtg-<version>-checksums.txt' which doesn't match any of the canonical
    # names; the substring fallback handles project-specific naming without
    # forcing us to maintain an exhaustive candidate list.
    checksum_url: str | None = None
    for name in (f"{asset_filename}.sha256", *_CHECKSUM_ASSET_CANDIDATES):
        if name in assets:
            checksum_url = assets[name]
            break
    if checksum_url is None:
        for name in sorted(assets):
            lower = name.lower()
            if "checksum" in lower or "sha256sums" in lower:
                checksum_url = assets[name]
                break
    if checksum_url is None:
        raise BuildError(
            f"checksum file not in release {upstream_release!r}; "
            f"looked for: {asset_filename}.sha256, SHA256SUMS, checksums.txt, "
            f"or any asset whose name contains 'checksum' / 'sha256sums'. "
            f"Available assets: {sorted(assets)}"
        )

    # 4. Download both.
    try:
        binary_bytes = get(binary_url).read()
        checksum_bytes = get(checksum_url).read()
    except Exception as e:
        raise BuildError(f"asset download failed: {e}") from e

    # 5. Verify sha256.
    expected_sha = _parse_checksum_for(
        asset_filename, checksum_bytes.decode("utf-8", errors="replace"))
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
    # The upstream asset is verified — but mtg ships as a .tar.gz, and the RU
    # agent execs the binary directly. Extract the actual ELF so the image is a
    # runnable binary, not the archive. image_version is the sha of what the
    # agent runs.
    binary_bytes = _extract_runnable(binary_bytes, asset_filename, member="mtg")
    image_version = hashlib.sha256(binary_bytes).hexdigest()

    # Content-addressed idempotency: if this exact binary is already registered
    # (e.g. a prior --force rebuild produced the same ELF), return it instead of
    # colliding on the ru_images primary key. This is distinct from the
    # release-level skip above (which --force bypasses to allow rebuilding a
    # release into different content).
    already = conn.execute(
        "SELECT image_version FROM ru_images WHERE image_version=?",
        (image_version,),
    ).fetchone()
    if already is not None:
        return already[0]

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
