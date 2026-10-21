"""Immutable S3 storage for validated per-page extraction checkpoints."""
from __future__ import annotations

import json
import hashlib
import re
import uuid
from typing import Any


CHECKPOINT_PREFIX = "checkpoints"


class S3PageCheckpointStore:
    """Store each checkpoint under a unique key so workers never overwrite one another."""

    def __init__(
        self,
        s3_client,
        bucket: str,
        job_id: str,
        processing_owner: str,
        *,
        manifest_key: str | None = None,
        commit_manifest=None,
    ):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", job_id or ""):
            raise ValueError("Invalid job id for checkpoint storage")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", processing_owner or ""):
            raise ValueError("Invalid processing owner for checkpoint storage")
        self.s3 = s3_client
        self.bucket = bucket
        self.job_id = job_id
        self.processing_owner = processing_owner
        self.prefix = f"{CHECKPOINT_PREFIX}/{job_id}/"
        self.manifest_key = manifest_key
        self.manifest: dict[str, Any] | None = None
        self.commit_manifest = commit_manifest

    def list_objects(self) -> list[dict[str, Any]]:
        objects: list[dict[str, Any]] = []
        request: dict[str, Any] = {"Bucket": self.bucket, "Prefix": self.prefix}
        while True:
            response = self.s3.list_objects_v2(**request)
            objects.extend(response.get("Contents", []))
            token = response.get("NextContinuationToken")
            if not response.get("IsTruncated") or not token:
                return objects
            request["ContinuationToken"] = token

    def load(self, key: str) -> dict[str, Any]:
        if not key.startswith(self.prefix):
            raise ValueError("Checkpoint key is outside the job prefix")
        body = self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        return json.loads(body)

    def load_manifest(self) -> dict[str, Any] | None:
        if not self.manifest_key:
            return None
        if not self.manifest_key.startswith(self.prefix):
            raise ValueError("Checkpoint manifest key is outside the job prefix")
        body = self.s3.get_object(Bucket=self.bucket, Key=self.manifest_key)["Body"].read()
        head = self.s3.head_object(Bucket=self.bucket, Key=self.manifest_key)
        actual_sha256 = hashlib.sha256(body).hexdigest()
        if head.get("Metadata", {}).get("manifest-sha256") != actual_sha256:
            raise ValueError("Checkpoint manifest checksum mismatch")
        manifest = json.loads(body)
        if not isinstance(manifest, dict):
            raise ValueError("Checkpoint manifest is not an object")
        self.manifest = manifest
        return manifest

    def load_checkpoint(self, entry: dict[str, Any]) -> tuple[dict[str, Any], str]:
        key = entry.get("key")
        if not isinstance(key, str) or not key.startswith(self.prefix):
            raise ValueError("Checkpoint key is outside the job prefix")
        body = self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        body_sha256 = hashlib.sha256(body).hexdigest()
        if entry.get("sha256") != body_sha256:
            raise ValueError("Checkpoint checksum mismatch")
        return json.loads(body), key

    def save(self, page_number: int, checkpoint: dict[str, Any]) -> tuple[str, int]:
        key = (
            f"{self.prefix}page-{page_number:04d}/"
            f"{self.processing_owner}-{uuid.uuid4().hex}.json"
        )
        body = json.dumps(checkpoint, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        body_sha256 = hashlib.sha256(body).hexdigest()
        self.s3.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=body,
            ContentType="application/json; charset=utf-8",
            Metadata={
                "job-id": self.job_id,
                "page-number": str(page_number),
                "processing-owner": self.processing_owner,
                "checkpoint-sha256": body_sha256,
            },
        )
        head = self.s3.head_object(Bucket=self.bucket, Key=key)
        if head.get("ContentLength") != len(body):
            raise RuntimeError("Checkpoint write verification failed")
        metadata = head.get("Metadata", {})
        if (
            metadata.get("job-id") != self.job_id
            or metadata.get("page-number") != str(page_number)
            or metadata.get("checkpoint-sha256") != body_sha256
        ):
            raise RuntimeError("Checkpoint metadata verification failed")

        # A checkpoint is recoverable only after its manifest pointer is
        # committed conditionally to the current DynamoDB lease owner.
        previous_pages = (self.manifest or {}).get("pages", {})
        pages = {
            number: entry
            for number, entry in previous_pages.items()
            if int(number) < page_number
        }
        pages[str(page_number)] = {"key": key, "sha256": body_sha256}
        manifest = {
            "manifest_version": 1,
            "job_id": self.job_id,
            "pdf_sha256": checkpoint["pdf_sha256"],
            "total_pages": checkpoint["total_pages"],
            "schema_version": checkpoint["schema_version"],
            "pipeline_fingerprint": checkpoint["pipeline_fingerprint"],
            "pages": pages,
        }
        manifest_key = f"{self.prefix}manifest/{self.processing_owner}-{uuid.uuid4().hex}.json"
        manifest_body = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        manifest_sha256 = hashlib.sha256(manifest_body).hexdigest()
        self.s3.put_object(
            Bucket=self.bucket,
            Key=manifest_key,
            Body=manifest_body,
            ContentType="application/json; charset=utf-8",
            Metadata={"job-id": self.job_id, "manifest-sha256": manifest_sha256},
        )
        manifest_head = self.s3.head_object(Bucket=self.bucket, Key=manifest_key)
        if (
            manifest_head.get("ContentLength") != len(manifest_body)
            or manifest_head.get("Metadata", {}).get("manifest-sha256") != manifest_sha256
        ):
            raise RuntimeError("Checkpoint manifest write verification failed")
        if self.commit_manifest:
            self.commit_manifest(manifest_key)
        self.manifest = manifest
        self.manifest_key = manifest_key
        return key, len(body)

    def delete_all(self) -> int:
        keys = [
            item.get("Key") for item in self.list_objects()
            if item.get("Key") and item["Key"].startswith(self.prefix)
        ]
        for key in keys:
            self.s3.delete_object(Bucket=self.bucket, Key=key)
        return len(keys)
