"""mthydra-ops agent-publish — package ru_agent + upload to S3 + presign (spec P)."""
from __future__ import annotations

import contextlib
import gzip
import hashlib
import io
import json
import os
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3

_EXCLUDE_DIRS = {"__pycache__"}
_EXCLUDE_SUFFIXES = (".pyc", ".pyo")


# mthydra subpackages the RU agent needs at runtime. ru_agent/seed.py imports
# mthydra.descriptor.authority (onward-credential + descriptor verification);
# descriptor.authority itself pulls only stdlib + cryptography (installed on the
# box via apt). Shipping only ru_agent left the box dying on boot with
# `ModuleNotFoundError: No module named 'mthydra.descriptor'` (first real RU box,
# 2026-06-02). controller/* is deliberately NOT shipped — the agent never imports
# it, and keeping it off an exposed box is good hygiene.
_AGENT_SUBPACKAGES = ("ru_agent", "descriptor")


def package_agent(source_dir: Path | str) -> tuple[bytes, str]:
    """Tar mthydra/__init__.py + the agent's mthydra subpackages (ru_agent +
    its runtime deps), excluding caches. Return (tar_bytes, sha256_hex).
    Deterministic across runs: file mtimes are zeroed, members are added in
    sorted-name order, gzip mtime fixed at 0."""
    src = Path(source_dir)
    members: list[Path] = []
    root = src / "mthydra"
    if not root.is_dir():
        raise RuntimeError(f"agent source missing: {root}")
    init = root / "__init__.py"
    if init.is_file():
        members.append(init)
    for subpkg in _AGENT_SUBPACKAGES:
        for path in (root / subpkg).rglob("*"):
            if not path.is_file():
                continue
            if any(part in _EXCLUDE_DIRS for part in path.parts):
                continue
            if path.suffix in _EXCLUDE_SUFFIXES:
                continue
            members.append(path)
    members.sort(key=lambda p: p.relative_to(src).as_posix())

    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz, \
            tarfile.open(fileobj=gz, mode="w") as tf:
        for p in members:
            info = tf.gettarinfo(str(p), arcname=p.relative_to(src).as_posix())
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            with open(p, "rb") as fh:
                tf.addfile(info, fh)
    data = buf.getvalue()
    return data, hashlib.sha256(data).hexdigest()


AGENT_MANIFEST_PATH = Path("/var/lib/mthydra/agent.json")
_REPUBLISH_HEADROOM_HOURS = 24


@dataclass(frozen=True)
class AgentManifest:
    url: str
    sha256: str
    published_at: str
    expires_at: str


def read_manifest(path=None) -> AgentManifest | None:
    path = Path(path or AGENT_MANIFEST_PATH)
    if not path.exists():
        return None
    raw = json.loads(path.read_text())
    return AgentManifest(**raw)


def _get_s3_credentials(cfg, db_path: str) -> tuple[str, str]:
    """Return (key_id, secret) for boto3.

    Matches the R-D1 split-or-fallback logic in
    controller.cli._build_destination: stored credential can be either
    'keyid:secret' (the canonical install-time format) OR just 'secret'
    (which legacy installs and the R-D1 workaround flow produce). When
    the colon is absent, use cfg.backup.access_key_id as the key.

    Previously this raised "provider credential malformed (expected
    KEY:SECRET)" on the secret-only form — breaking agent-publish on
    every host where the operator had used the R-D1 workaround to
    rotate to just the secret.
    """
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.tokens import get_provider_credential

    with connect(db_path) as conn:
        cred = get_provider_credential(conn, "b2")
    if ":" in cred:
        key_id, _, secret = cred.partition(":")
        if not secret:
            raise RuntimeError(
                "provider credential malformed: 'KEY:' with empty secret")
        return key_id, secret
    # No colon → secret-only form; key comes from config.
    if not cfg.backup.access_key_id:
        raise RuntimeError(
            "provider credential is secret-only and "
            "[backup].access_key_id is unset; cannot derive the AWS access "
            "key id")
    return cfg.backup.access_key_id, cred


def _make_s3_client(cfg, db_path: str):
    # Match controller.cli._build_destination exactly: BackupConfig has NO
    # `region` field (region is derived from the endpoint via R-D2), and an
    # empty endpoint must become None so boto3 uses the default AWS endpoint.
    from mthydra.controller.backup.s3_dest import resolve_region

    key_id, secret = _get_s3_credentials(cfg, db_path)
    return boto3.client(
        "s3",
        endpoint_url=cfg.backup.endpoint or None,
        aws_access_key_id=key_id,
        aws_secret_access_key=secret,
        region_name=resolve_region(cfg.backup.endpoint),
    )


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            with contextlib.suppress(OSError):
                os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def publish_agent(
    cfg,
    tar_bytes: bytes,
    sha: str,
    db_path: str,
    *,
    ttl_days: int = 7,
    bucket: str | None = None,
) -> AgentManifest:
    """Upload tar_bytes to s3://<bucket>/agent/mthydra-ru-agent-<sha12>.tar.gz
    (idempotent — content-addressed), presign, write manifest. If a fresh
    manifest with matching sha already exists, return that without re-uploading."""
    existing = read_manifest(AGENT_MANIFEST_PATH)
    if existing and existing.sha256 == sha:
        expires = datetime.strptime(existing.expires_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=UTC
        )
        if expires - datetime.now(UTC) > timedelta(hours=_REPUBLISH_HEADROOM_HOURS):
            return existing

    bucket = bucket or cfg.backup.bucket
    key = f"agent/mthydra-ru-agent-{sha[:12]}.tar.gz"
    client = _make_s3_client(cfg, db_path)
    client.put_object(Bucket=bucket, Key=key, Body=tar_bytes)
    url = client.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=ttl_days * 86400,
    )
    now = datetime.now(UTC)
    manifest = AgentManifest(
        url=url,
        sha256=sha,
        published_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        expires_at=(now + timedelta(days=ttl_days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    _atomic_write_json(
        AGENT_MANIFEST_PATH,
        {
            "url": manifest.url,
            "sha256": manifest.sha256,
            "published_at": manifest.published_at,
            "expires_at": manifest.expires_at,
        },
    )
    return manifest


def cmd_agent_publish(args) -> int:
    from mthydra.controller.config import load_config
    cfg = load_config(Path(args.config))
    tar_bytes, sha = package_agent(args.source_dir)
    manifest = publish_agent(cfg, tar_bytes, sha, args.db_path, ttl_days=args.ttl_days)
    print(json.dumps({
        "url": manifest.url, "sha256": manifest.sha256,
        "published_at": manifest.published_at,
        "expires_at": manifest.expires_at,
    }, indent=2, sort_keys=True))
    return 0
