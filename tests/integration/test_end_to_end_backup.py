"""Integration: backup → decrypt → summarize roundtrip (spec A §13.2)."""
import shutil
import subprocess

import boto3
import pytest
from moto import mock_aws

from mthydra.controller.backup.pipeline import BackupPipeline
from mthydra.controller.backup.s3_dest import S3Destination
from mthydra.controller.bootstrap import init_state
from mthydra.controller.restore.decrypt import decrypt_blob
from mthydra.controller.restore.summary import summarize_db
from mthydra.controller.state.backup_log import BackupTrigger
from mthydra.controller.state.burned import mark_burned
from mthydra.controller.state.cover_pool import add_candidate, attest_verified, assign_to_box
from mthydra.controller.state.db import connect
from mthydra.controller.state.ru_boxes import insert_box

pytestmark = pytest.mark.skipif(
    shutil.which("age") is None, reason="age binary not installed"
)

BUCKET = "mthydra-e2e-test"


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


def test_backup_then_decrypt_then_summarize(tmp_path, keypair):
    keyfile, recipient = keypair
    db = tmp_path / "state.sqlite"
    init_state(
        db_path=db,
        age_recipient=recipient,
        provider_credentials={"aws": "x", "b2": "y"},
        obligation_timer_hours={"backup_restore_dryrun": 720},
        now="2026-05-18T00:00:00Z",
    )

    # Produce some non-trivial state
    conn = connect(db)
    add_candidate(conn, "alpha.example", added_at="2026-05-18T00:00:00Z")
    attest_verified(conn, "alpha.example", from_vantage="v", at="2026-05-18T00:00:01Z")
    insert_box(conn, "b1", "h", "fsn1", None, "alpha.example", "img1", "2026-05-18T00:00:02Z")
    assign_to_box(conn, "alpha.example", box_id="b1", at="2026-05-18T00:00:02Z")
    add_candidate(conn, "beta.example", added_at="2026-05-18T00:00:03Z")
    attest_verified(conn, "beta.example", from_vantage="v", at="2026-05-18T00:00:04Z")
    insert_box(conn, "b2", "h", "fsn1", None, "beta.example", "img1", "2026-05-18T00:00:05Z")
    assign_to_box(conn, "beta.example", box_id="b2", at="2026-05-18T00:00:05Z")
    mark_burned(conn, "beta.example", "job2_kill", "b2", "2026-05-18T00:00:06Z", None)
    conn.close()

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
        assert gen == 1

        # Pull blob from moto S3, decrypt, summarize
        body = client.get_object(Bucket=BUCKET, Key=f"gen-{gen:010d}.age")["Body"].read()
        blob = tmp_path / "blob.age"
        blob.write_bytes(body)
        restored = tmp_path / "restored.sqlite"
        decrypt_blob(blob, identity_path=keyfile, out=restored)
        s = summarize_db(restored)
        assert s["burned_domains_count"] == 1
        assert s["cover_pool_in_use"] == 1
