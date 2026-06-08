#!/usr/bin/env python3
"""Stage a promoted mtg image WITHOUT hitting GitHub.

The real `image-prepare` resolves the latest 9seconds/mtg release from the
GitHub API, downloads it, uploads to S3 and promotes. In the offline
integration harness we already have the mtg release tarball cached, so this
script does the equivalent against MinIO using the SAME production code paths:

    _extract_runnable  -> the real ELF/arch guard used by image-build
    S3Destination.put_image -> the real S3 upload (Object Lock COMPLIANCE)
    ru_images.insert_candidate + promote -> the real catalog rows

Usage: stage_image.py <mtg_tarball> <image_version> <config_path> <db_path>
The B2/S3 secret is read from B2_APPLICATION_KEY (kept off argv).
"""
import hashlib
import json
import os
import pathlib
import sys
import tempfile

from mthydra.controller.backup.s3_dest import S3Destination, resolve_region
from mthydra.controller.config import load_config
from mthydra.controller.image.builder import _extract_runnable
from mthydra.controller.state.db import connect
from mthydra.controller.state.ru_images import insert_candidate, promote

NOW = "2026-06-08T00:00:00Z"


def main() -> int:
    tarball, image_version, config_path, db_path = sys.argv[1:5]
    secret = os.environ["B2_APPLICATION_KEY"]

    data = pathlib.Path(tarball).read_bytes()
    elf = _extract_runnable(data, pathlib.Path(tarball).name, member="mtg")
    sha = hashlib.sha256(elf).hexdigest()
    size = len(elf)
    print(f"[stage] mtg ELF extracted: sha={sha[:12]}… size={size}")

    cfg = load_config(pathlib.Path(config_path))
    key_id = cfg.backup.access_key_id
    if ":" in secret:
        key_id, _, secret = secret.partition(":")
    dest = S3Destination(
        endpoint_url=cfg.backup.endpoint or None,
        bucket=cfg.backup.bucket,
        access_key_id=key_id,
        secret_access_key=secret,
        region=resolve_region(cfg.backup.endpoint),
        object_lock_days=cfg.backup.retention.object_lock_days,
    )

    with tempfile.NamedTemporaryFile() as tf:
        tf.write(elf)
        tf.flush()
        manifest = json.dumps(
            {"image_version": image_version, "sha256": sha, "size_bytes": size},
            sort_keys=True,
        ).encode()
        dest.put_image(image_version=image_version,
                       binary_path=pathlib.Path(tf.name), manifest=manifest)
    print(f"[stage] uploaded images/{image_version}/mtg to "
          f"{cfg.backup.bucket} @ {cfg.backup.endpoint}")

    conn = connect(db_path)
    insert_candidate(
        conn, image_version=image_version, upstream_release="v2.2.8",
        upstream_repo="9seconds/mtg",
        binary_url=f"images/{image_version}/mtg",
        manifest_url=f"images/{image_version}/manifest.json",
        binary_sha256=sha, binary_size_bytes=size, built_at=NOW,
    )
    promote(conn, image_version, at=NOW, evidence="integration-harness staged image")
    conn.commit()
    print(f"[stage] promoted {image_version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
