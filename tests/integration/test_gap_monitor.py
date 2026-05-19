"""Integration: gap-monitor full cycle (observe → alarm → clear) (spec A §13.2)."""
import boto3
import pytest
from moto import mock_aws

from mthydra.controller.backup.s3_dest import S3Destination
from mthydra_backup_monitor.poller import GapMonitorState, evaluate_gap

BUCKET = "mthydra-gap-test"


def test_gap_monitor_against_moto_bucket():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        dest = S3Destination(None, BUCKET, "x", "y", "us-east-1", object_lock_days=30)
        dest._client = client

        # Put initial index
        dest.put_index(highest_gen=1, sha256="a", size_bytes=10, ts="2026-05-18T00:00:00Z")

        state = GapMonitorState(None, None, None)
        state, should_alarm = evaluate_gap(
            dest.head_index(), state, "2026-05-18T01:00:00Z", 48, 24
        )
        assert state.last_seen_gen == 1
        assert not should_alarm

        # 50 hours later — no advancement → alarm fires
        state, should_alarm = evaluate_gap(
            dest.head_index(), state, "2026-05-20T03:00:00Z", 48, 24
        )
        assert should_alarm

        # Index advances → alarm clears
        dest.put_index(highest_gen=2, sha256="b", size_bytes=20, ts="2026-05-20T03:01:00Z")
        state, should_alarm = evaluate_gap(
            dest.head_index(), state, "2026-05-20T03:02:00Z", 48, 24
        )
        assert state.last_seen_gen == 2
        assert state.last_alarm_at is None
        assert not should_alarm
