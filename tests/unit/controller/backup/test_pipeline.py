"""Tests for the do_backup orchestration pipeline (spec A §6.2)."""
import shutil
import sqlite3
from pathlib import Path
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from mthydra.controller.backup.pipeline import BackupPipeline
from mthydra.controller.backup.s3_dest import S3Destination
from mthydra.controller.state.authority import insert_authority
from mthydra.controller.state.backup_log import BackupTrigger, next_generation
from mthydra.controller.state.db import connect
from mthydra.controller.state.descriptor import insert_signing_key
from mthydra.controller.state.schema import apply_schema

needs_age = pytest.mark.skipif(shutil.which("age") is None, reason="age binary not installed")

BUCKET = "mthydra-test"


@pytest.fixture
def keypair(tmp_path):
    import subprocess

    keyfile = tmp_path / "id.key"
    r = subprocess.run(
        ["age-keygen", "-o", str(keyfile)], capture_output=True, text=True, check=True
    )
    recipient = next(
        line.removeprefix("# public key: ").strip()
        for line in r.stderr.splitlines()
        if line.startswith("# public key: ")
    )
    return keyfile, recipient


@pytest.fixture
def seeded_db(tmp_path):
    db = tmp_path / "state.sqlite"
    conn = connect(db)
    apply_schema(conn)
    insert_authority(conn, 1, "P", "K", "2026-05-18T00:00:00Z")
    insert_signing_key(conn, 1, b"P", b"K", "2026-05-18T00:00:00Z")
    conn.close()
    return db


@pytest.fixture
def dest():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        client.put_bucket_versioning(
            Bucket=BUCKET,
            VersioningConfiguration={"Status": "Enabled"},
        )
        client.put_object_lock_configuration(
            Bucket=BUCKET,
            ObjectLockConfiguration={
                "ObjectLockEnabled": "Enabled",
                "Rule": {"DefaultRetention": {"Mode": "COMPLIANCE", "Days": 365}},
            },
        )
        d = S3Destination(None, BUCKET, "x", "y", "us-east-1", object_lock_days=30)
        d._client = client
        yield d


@needs_age
def test_do_backup_uploads_blob_and_index(tmp_path, keypair, seeded_db, dest):
    _, recipient = keypair
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    pipeline = BackupPipeline(
        db_path=seeded_db,
        tmp_dir=tmp_dir,
        recipient=recipient,
        destination=dest,
        clock=lambda: "2026-05-18T00:00:00Z",
    )
    gen = pipeline.do_backup(trigger=BackupTrigger.MANUAL)
    assert gen == 1
    assert dest.head_blob(1)
    payload = dest.head_index()
    assert payload["highest_gen"] == 1
    conn = connect(seeded_db)
    assert next_generation(conn) == 2  # generation 1 consumed


@needs_age
def test_do_backup_cleans_tmp_files(tmp_path, keypair, seeded_db, dest):
    _, recipient = keypair
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    pipeline = BackupPipeline(seeded_db, tmp_dir, recipient, dest, lambda: "2026-05-18T00:00:00Z")
    pipeline.do_backup(trigger=BackupTrigger.FLOOR_TIMER)
    assert list(tmp_dir.iterdir()) == []


@needs_age
def test_do_backup_records_full_lifecycle(tmp_path, keypair, seeded_db, dest):
    """backup_log row should have all three timestamps set after success."""
    from mthydra.controller.state.backup_log import BackupRecord

    _, recipient = keypair
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    pipeline = BackupPipeline(seeded_db, tmp_dir, recipient, dest, lambda: "2026-05-18T00:00:00Z")
    pipeline.do_backup(trigger=BackupTrigger.MANUAL)
    conn = connect(seeded_db)
    row = conn.execute(
        "SELECT generation, pushed_at, index_updated_at FROM backup_log WHERE generation=1"
    ).fetchone()
    assert row[1] is not None  # pushed_at
    assert row[2] is not None  # index_updated_at


def test_do_backup_refused_in_offline_mode(tmp_path, seeded_db, dest):
    pipeline = BackupPipeline(
        seeded_db, tmp_path, "recipient", dest, lambda: "2026-05-18T00:00:00Z", mode="offline"
    )
    with pytest.raises(RuntimeError, match="offline mode"):
        pipeline.do_backup(trigger=BackupTrigger.MANUAL)


@needs_age
def test_do_backup_dryrun_tags_trigger(tmp_path, keypair, seeded_db, dest):
    _, recipient = keypair
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    pipeline = BackupPipeline(
        seeded_db, tmp_dir, recipient, dest, lambda: "2026-05-18T00:00:00Z", mode="dryrun"
    )
    pipeline.do_backup(trigger=BackupTrigger.MANUAL)
    conn = connect(seeded_db)
    row = conn.execute("SELECT trigger FROM backup_log WHERE generation=1").fetchone()
    assert row[0] == "dryrun:manual"


def test_consecutive_failures_write_audit_row(tmp_path, seeded_db):
    """After 3 failures, an audit_log row with action=self_alarm_unreachable is written."""
    dest_mock = S3Destination.__new__(S3Destination)
    dest_mock.bucket = BUCKET
    dest_mock.object_lock_days = 30

    def bad_put_blob(**kwargs):
        raise RuntimeError("S3 down")

    dest_mock.put_blob = bad_put_blob

    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()

    # Use a real recipient-format string (age only needed for encrypt_file, mock that)
    with patch("mthydra.controller.backup.pipeline.encrypt_file") as mock_enc:
        # Make encrypt_file write a dummy file so put_blob can be reached
        def fake_encrypt(src, recipient, out):
            out.write_bytes(b"FAKE")

        mock_enc.side_effect = fake_encrypt

        pipeline = BackupPipeline(
            seeded_db, tmp_dir, "age1recipient", dest_mock, lambda: "2026-05-18T00:00:00Z"
        )
        for _ in range(3):
            with pytest.raises(RuntimeError):
                pipeline.do_backup(trigger=BackupTrigger.MANUAL)

    conn = connect(seeded_db)
    row = conn.execute(
        "SELECT action FROM audit_log WHERE action='self_alarm_unreachable'"
    ).fetchone()
    assert row is not None
