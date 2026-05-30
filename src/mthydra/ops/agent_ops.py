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


def package_agent(source_dir: Path | str) -> tuple[bytes, str]:
    """Tar mthydra/__init__.py + mthydra/ru_agent/* (excluding caches),
    return (tar_bytes, sha256_hex). Deterministic across runs: file mtimes
    are zeroed, members are added in sorted-name order, gzip mtime fixed at 0."""
    src = Path(source_dir)
    members: list[Path] = []
    root = src / "mthydra"
    if not root.is_dir():
        raise RuntimeError(f"agent source missing: {root}")
    init = root / "__init__.py"
    if init.is_file():
        members.append(init)
    for path in (root / "ru_agent").rglob("*"):
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


def _get_s3_credentials(cfg) -> tuple[str, str]:
    from mthydra.controller.state.db import connect
    from mthydra.controller.state.tokens import get_provider_credential

    with connect(cfg._db_path) as conn:
        cred = get_provider_credential(conn, "b2")
    key_id, _, secret = cred.partition(":")
    if not secret:
        raise RuntimeError("provider credential malformed (expected KEY:SECRET)")
    return key_id, secret


def _make_s3_client(cfg):
    key_id, secret = _get_s3_credentials(cfg)
    return boto3.client(
        "s3",
        endpoint_url=cfg.backup.endpoint,
        aws_access_key_id=key_id,
        aws_secret_access_key=secret,
        region_name=cfg.backup.region,
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
    client = _make_s3_client(cfg)
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


def _load_cfg(db_path: str, config: str):
    """Load the controller config + stash db_path for _get_s3_credentials."""
    from mthydra.controller.config import load_config
    cfg = load_config(Path(config))
    cfg._db_path = db_path
    return cfg


def cmd_agent_publish(args) -> int:
    cfg = _load_cfg(args.db_path, args.config)
    tar_bytes, sha = package_agent(args.source_dir)
    manifest = publish_agent(cfg, tar_bytes, sha, ttl_days=args.ttl_days)
    print(json.dumps({
        "url": manifest.url, "sha256": manifest.sha256,
        "published_at": manifest.published_at,
        "expires_at": manifest.expires_at,
    }, indent=2, sort_keys=True))
    return 0
