"""S3-compatible backup destination (works for AWS S3, Backblaze B2, MinIO)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError


class S3Destination:
    def __init__(
        self,
        endpoint_url: str | None,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
        region: str,
        object_lock_days: int,
    ) -> None:
        self.endpoint_url = endpoint_url
        self.bucket = bucket
        self.region = region
        self.object_lock_days = object_lock_days
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name=region,
        )

    @staticmethod
    def _key_for_gen(generation: int) -> str:
        return f"gen-{generation:010d}.age"

    def put_blob(self, generation: int, blob_path: Path) -> None:
        retain_until = datetime.now(timezone.utc) + timedelta(days=self.object_lock_days)
        with open(blob_path, "rb") as fh:
            self._client.put_object(
                Bucket=self.bucket,
                Key=self._key_for_gen(generation),
                Body=fh,
                ObjectLockMode="COMPLIANCE",
                ObjectLockRetainUntilDate=retain_until,
            )

    def put_index(self, highest_gen: int, sha256: str, size_bytes: int, ts: str) -> None:
        body = json.dumps(
            {"highest_gen": highest_gen, "sha256": sha256, "size_bytes": size_bytes, "ts": ts},
            sort_keys=True,
        ).encode("utf-8")
        retain_until = datetime.now(timezone.utc) + timedelta(days=self.object_lock_days)
        self._client.put_object(
            Bucket=self.bucket,
            Key="index.json",
            Body=body,
            ContentType="application/json",
            # GOVERNANCE (not COMPLIANCE) so operator can override a corrupted index version
            # while blobs remain under COMPLIANCE (plan §16 G7 resolution).
            ObjectLockMode="GOVERNANCE",
            ObjectLockRetainUntilDate=retain_until,
        )

    def head_index(self) -> dict[str, Any] | None:
        try:
            obj = self._client.get_object(Bucket=self.bucket, Key="index.json")
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                return None
            raise
        return json.loads(obj["Body"].read())

    def head_blob(self, generation: int) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=self._key_for_gen(generation))
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
                return False
            raise

    @staticmethod
    def _heartbeat_key(node_id: str) -> str:
        return f"standby/{node_id}/heartbeat.json"

    def put_heartbeat(self, *, node_id: str, payload: bytes) -> None:
        """Upload a standby heartbeat object.

        Goes into the same bucket (Object Lock COMPLIANCE required); the
        accumulated versions are accepted residual (spec F §11).
        """
        retain_until = datetime.now(timezone.utc) + timedelta(days=self.object_lock_days)
        self._client.put_object(
            Bucket=self.bucket,
            Key=self._heartbeat_key(node_id),
            Body=payload,
            ContentType="application/json",
            ObjectLockMode="COMPLIANCE",
            ObjectLockRetainUntilDate=retain_until,
        )

    def get_heartbeat(self, *, node_id: str) -> tuple[bytes, str]:
        """Returns (payload, etag). Raises ClientError on absence."""
        obj = self._client.get_object(
            Bucket=self.bucket, Key=self._heartbeat_key(node_id)
        )
        return obj["Body"].read(), obj["ETag"]

    def head_heartbeat(self, *, node_id: str) -> dict[str, Any] | None:
        """Returns {'etag', 'last_modified_iso', 'size_bytes'} or None if absent."""
        try:
            obj = self._client.head_object(
                Bucket=self.bucket, Key=self._heartbeat_key(node_id)
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
                return None
            raise
        return {
            "etag": obj["ETag"],
            "last_modified_iso": obj["LastModified"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "size_bytes": int(obj["ContentLength"]),
        }

    @staticmethod
    def _image_binary_key(image_version: str) -> str:
        return f"images/{image_version}/mtg"

    @staticmethod
    def _image_manifest_key(image_version: str) -> str:
        return f"images/{image_version}/manifest.json"

    def put_image(
        self, *, image_version: str, binary_path: Path, manifest: bytes,
    ) -> None:
        """Upload binary + manifest to B2, both under Object Lock COMPLIANCE."""
        retain_until = datetime.now(timezone.utc) + timedelta(days=self.object_lock_days)
        with open(binary_path, "rb") as fh:
            self._client.put_object(
                Bucket=self.bucket,
                Key=self._image_binary_key(image_version),
                Body=fh,
                ObjectLockMode="COMPLIANCE",
                ObjectLockRetainUntilDate=retain_until,
            )
        self._client.put_object(
            Bucket=self.bucket,
            Key=self._image_manifest_key(image_version),
            Body=manifest,
            ContentType="application/json",
            ObjectLockMode="COMPLIANCE",
            ObjectLockRetainUntilDate=retain_until,
        )

    def head_image(self, *, image_version: str) -> dict[str, Any] | None:
        """Returns binary head info or None if absent."""
        try:
            obj = self._client.head_object(
                Bucket=self.bucket, Key=self._image_binary_key(image_version)
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
                return None
            raise
        return {
            "etag": obj["ETag"],
            "last_modified_iso": obj["LastModified"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "size_bytes": int(obj["ContentLength"]),
        }
