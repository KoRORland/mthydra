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


def test_put_index_uses_governance_retention(s3_env):
    """index.json must use GOVERNANCE (not COMPLIANCE) so operator can override (G7)."""
    dest = S3Destination(None, BUCKET, "x", "y", "us-east-1", object_lock_days=30)
    dest._client = s3_env
    dest.put_index(highest_gen=1, sha256="a", size_bytes=10, ts="2026-05-18T00:00:00Z")
    # head_object returns ObjectLockMode on the stored version
    meta = s3_env.head_object(Bucket=BUCKET, Key="index.json")
    assert meta.get("ObjectLockMode") == "GOVERNANCE"


def test_put_blob_uses_compliance_retention(s3_env, tmp_path):
    """Blobs must use COMPLIANCE — stricter than index (G7 asymmetry)."""
    blob = tmp_path / "snap.age"
    blob.write_bytes(b"DATA")
    dest = S3Destination(None, BUCKET, "x", "y", "us-east-1", object_lock_days=30)
    dest._client = s3_env
    dest.put_blob(generation=1, blob_path=blob)
    meta = s3_env.head_object(Bucket=BUCKET, Key="gen-0000000001.age")
    assert meta.get("ObjectLockMode") == "COMPLIANCE"


def _make_dest(s3_env):
    dest = S3Destination(None, BUCKET, "x", "y", "us-east-1", object_lock_days=30)
    dest._client = s3_env
    return dest


def test_put_and_get_heartbeat_roundtrip(s3_env):
    dest = _make_dest(s3_env)
    payload = b'{"node_id":"eu-standby-de-1","ts":"2026-05-20T00:00:00Z"}'
    dest.put_heartbeat(node_id="eu-standby-de-1", payload=payload)
    body, etag = dest.get_heartbeat(node_id="eu-standby-de-1")
    assert body == payload
    assert etag


def test_head_heartbeat_returns_none_when_absent(s3_env):
    dest = _make_dest(s3_env)
    result = dest.head_heartbeat(node_id="eu-no-such-node")
    assert result is None


def test_head_heartbeat_returns_etag_and_modified(s3_env):
    dest = _make_dest(s3_env)
    dest.put_heartbeat(node_id="eu-standby-de-1", payload=b'{"x":1}')
    info = dest.head_heartbeat(node_id="eu-standby-de-1")
    assert info is not None
    assert "etag" in info
    assert "last_modified_iso" in info


def test_put_image_uploads_binary_and_manifest(s3_env, tmp_path):
    """put_image uploads both binary and manifest under content-addressed prefix."""
    dest = _make_dest(s3_env)
    binary_path = tmp_path / "mtg"
    binary_path.write_bytes(b"\x7fELF" + b"\x00" * 100)
    manifest = b'{"image_version":"abc","schema":"mthydra.ru_image.v1"}'

    dest.put_image(image_version="abc123", binary_path=binary_path, manifest=manifest)

    info = dest.head_image(image_version="abc123")
    assert info is not None
    assert "etag" in info
    assert info["size_bytes"] == binary_path.stat().st_size


def test_head_image_returns_none_when_absent(s3_env):
    dest = _make_dest(s3_env)
    info = dest.head_image(image_version="not-there")
    assert info is None


def test_presigned_image_url_returns_signed_url_and_expiry(s3_env, tmp_path):
    """presigned_image_url returns (url, expires_at_iso) for the image binary."""
    dest = _make_dest(s3_env)
    bp = tmp_path / "mtg"
    bp.write_bytes(b"\x7fELF" + b"\x00" * 100)
    dest.put_image(image_version="ivX", binary_path=bp, manifest=b'{"x":1}')
    url, expires_at = dest.presigned_image_url(image_version="ivX", ttl_seconds=3600)
    assert url.startswith("https://") or url.startswith("http://")
    assert "ivX" in url
    assert "Signature" in url  # X-Amz-Signature or just Signature, depending on signer
    assert expires_at  # ISO-8601 string
