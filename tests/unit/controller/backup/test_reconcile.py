"""Tests for crash-recovery reconciler (spec A §9)."""
import boto3
import pytest
from moto import mock_aws

from mthydra.controller.backup.reconcile import reconcile_pending
from mthydra.controller.backup.s3_dest import S3Destination
from mthydra.controller.state.backup_log import (
    BackupTrigger,
    list_pending_reconciliation,
    next_generation,
    record_pushed,
    record_started,
)
from mthydra.controller.state.db import connect
from mthydra.controller.state.schema import apply_schema

BUCKET = "mthydra-reconcile-test"
CLOCK = lambda: "2026-05-18T01:00:00Z"  # noqa: E731


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "state.sqlite"
    conn = connect(p)
    apply_schema(conn)
    conn.close()
    return p


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


def _stage_partial(db_path, dest, generation: int) -> None:
    """Mark a backup as started+pushed but never index-updated, with blob in S3."""
    conn = connect(db_path)
    record_started(conn, generation, BackupTrigger.FLOOR_TIMER, "2026-05-18T00:00:00Z")
    record_pushed(conn, generation, "abc123", 100, "2026-05-18T00:00:10Z")
    conn.close()
    dest._client.put_object(Bucket=BUCKET, Key=f"gen-{generation:010d}.age", Body=b"X")


def test_reconcile_writes_missing_index(db, dest):
    _stage_partial(db, dest, 1)
    count = reconcile_pending(db, dest, clock=CLOCK)
    assert count == 1
    payload = dest.head_index()
    assert payload is not None
    assert payload["highest_gen"] == 1
    conn = connect(db)
    assert list_pending_reconciliation(conn) == []


def test_reconcile_no_op_when_already_indexed(db, dest):
    _stage_partial(db, dest, 1)
    dest.put_index(highest_gen=1, sha256="abc123", size_bytes=100, ts="2026-05-18T00:00:11Z")
    count = reconcile_pending(db, dest, clock=CLOCK)
    assert count == 1
    conn = connect(db)
    assert list_pending_reconciliation(conn) == []


def test_reconcile_advances_index_for_higher_generation(db, dest):
    _stage_partial(db, dest, 5)
    dest.put_index(highest_gen=2, sha256="old", size_bytes=10, ts="2026-05-17T00:00:00Z")
    reconcile_pending(db, dest, clock=CLOCK)
    assert dest.head_index()["highest_gen"] == 5


def test_reconcile_skips_row_when_blob_absent(db, dest):
    """If the blob is not in S3, the row is left pending (no crash)."""
    conn = connect(db)
    record_started(conn, 1, BackupTrigger.FLOOR_TIMER, "2026-05-18T00:00:00Z")
    record_pushed(conn, 1, "abc123", 100, "2026-05-18T00:00:10Z")
    conn.close()
    # No blob in S3
    count = reconcile_pending(db, dest, clock=CLOCK)
    assert count == 0
    conn = connect(db)
    assert len(list_pending_reconciliation(conn)) == 1  # still pending


def test_reconcile_returns_zero_when_nothing_pending(db, dest):
    count = reconcile_pending(db, dest, clock=CLOCK)
    assert count == 0
