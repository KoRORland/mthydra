"""mthydra-ops agent-publish — package ru_agent + upload to S3 + presign (spec P)."""
from __future__ import annotations

import gzip
import hashlib
import io
import tarfile
from pathlib import Path

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
