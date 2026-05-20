"""Integration: restore → adopt Case-B rotates authority (spec A §13.2)."""
import shutil
import subprocess

import boto3
import pytest
from moto import mock_aws

from mthydra.controller.backup.pipeline import BackupPipeline
from mthydra.controller.backup.s3_dest import S3Destination
from mthydra.controller.bootstrap import init_state
from mthydra.controller.restore.adopt import adopt_restored_state
from mthydra.controller.restore.decrypt import decrypt_blob
from mthydra.controller.state.authority import list_authorities
from mthydra.controller.state.backup_log import BackupTrigger
from mthydra.controller.state.db import connect

pytestmark = pytest.mark.skipif(
    shutil.which("age") is None, reason="age binary not installed"
)

BUCKET = "mthydra-restore-test"


@pytest.fixture
def keypair(tmp_path):
    keyfile = tmp_path / "id.key"
    r = subprocess.run(
        ["age-keygen", "-o", str(keyfile)], capture_output=True, text=True, check=True
    )
    recipient = next(
        line.removeprefix("# public key: ").strip()
        for line in keyfile.read_text().splitlines()
        if line.startswith("# public key: ")
    )
    return keyfile, recipient


def test_restore_then_adopt_case_b_rotates_authority(tmp_path, keypair):
    keyfile, recipient = keypair
    db = tmp_path / "state.sqlite"
    init_state(
        db_path=db,
        age_recipient=recipient,
        provider_credentials={"aws": "x"},
        obligation_timer_hours={},
        now="2026-05-18T00:00:00Z",
    )

    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        client.put_bucket_versioning(
            Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"}
        )
        client.put_object_lock_configuration(
            Bucket=BUCKET,
            ObjectLockConfiguration={
                "ObjectLockEnabled": "Enabled",
                "Rule": {"DefaultRetention": {"Mode": "COMPLIANCE", "Days": 365}},
            },
        )
        dest = S3Destination(None, BUCKET, "x", "y", "us-east-1", object_lock_days=30)
        dest._client = client
        tmp_dir = tmp_path / "tmp"
        tmp_dir.mkdir()
        pipeline = BackupPipeline(db, tmp_dir, recipient, dest, lambda: "2026-05-18T01:00:00Z")
        gen = pipeline.do_backup(BackupTrigger.MANUAL)

        body = client.get_object(Bucket=BUCKET, Key=f"gen-{gen:010d}.age")["Body"].read()
        blob = tmp_path / "blob.age"
        blob.write_bytes(body)

    restored = tmp_path / "restored.sqlite"
    decrypt_blob(blob, identity_path=keyfile, out=restored)

    live_target = tmp_path / "live.sqlite"
    adopt_restored_state(
        live_path=live_target,
        restored_path=restored,
        case="B",
        rotate_published_subset=True,
        at="2026-05-18T02:00:00Z",
    )

    conn = connect(live_target)
    auths = list_authorities(conn)
    assert len(auths) == 2
    assert auths[0].retired_at is not None   # original retired
    assert auths[1].retired_at is None       # fresh authority active
