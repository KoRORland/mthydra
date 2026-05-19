import boto3
import pytest
from moto import mock_aws

from mthydra.controller.backup.s3_dest import S3Destination


BUCKET = "mthydra-test"


@pytest.fixture
def s3_env():
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
                "Rule": {
                    "DefaultRetention": {
                        "Mode": "COMPLIANCE",
                        "Days": 365,
                    }
                },
            },
        )
        yield client


def test_put_blob_uploads_with_object_lock_header(s3_env, tmp_path):
    blob = tmp_path / "snap.age"
    blob.write_bytes(b"ENCRYPTED")
    dest = S3Destination(
        endpoint_url=None,
        bucket=BUCKET,
        access_key_id="x",
        secret_access_key="y",
        region="us-east-1",
        object_lock_days=30,
    )
    dest._client = s3_env  # inject mocked client
    dest.put_blob(generation=42, blob_path=blob)
    obj = s3_env.get_object(Bucket=BUCKET, Key="gen-0000000042.age")
    assert obj["Body"].read() == b"ENCRYPTED"


def test_put_index_writes_json(s3_env, tmp_path):
    dest = S3Destination(None, BUCKET, "x", "y", "us-east-1", object_lock_days=30)
    dest._client = s3_env
    dest.put_index(highest_gen=42, sha256="abc", size_bytes=1024, ts="2026-05-18T00:00:00Z")
    obj = s3_env.get_object(Bucket=BUCKET, Key="index.json")
    import json
    body = json.loads(obj["Body"].read())
    assert body == {"highest_gen": 42, "sha256": "abc", "size_bytes": 1024, "ts": "2026-05-18T00:00:00Z"}


def test_head_index_returns_none_when_absent(s3_env):
    dest = S3Destination(None, BUCKET, "x", "y", "us-east-1", object_lock_days=30)
    dest._client = s3_env
    assert dest.head_index() is None


def test_head_index_returns_payload_when_present(s3_env):
    dest = S3Destination(None, BUCKET, "x", "y", "us-east-1", object_lock_days=30)
    dest._client = s3_env
    dest.put_index(highest_gen=7, sha256="z", size_bytes=10, ts="2026-05-18T00:00:00Z")
    payload = dest.head_index()
    assert payload["highest_gen"] == 7
