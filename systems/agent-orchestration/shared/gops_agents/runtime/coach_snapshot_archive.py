from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any


class CoachSnapshotArchiveError(RuntimeError):
    pass


class CoachSnapshotArchive:
    def __init__(self, *, client=None, bucket: str | None = None, prefix: str | None = None) -> None:
        self.client = client
        self.bucket = bucket or os.getenv("AI_COACH_SNAPSHOT_S3_BUCKET")
        self.prefix = (prefix or os.getenv("AI_COACH_SNAPSHOT_S3_PREFIX", "ai-coach/snapshots")).strip("/")

    @property
    def enabled(self) -> bool:
        return _env_bool("AI_COACH_SNAPSHOT_ARCHIVE_ENABLED", False)

    @property
    def required(self) -> bool:
        return _env_bool("AI_COACH_SNAPSHOT_ARCHIVE_REQUIRED", False)

    def put_once(self, snapshot: dict[str, Any], analysis_id: str) -> dict[str, Any] | None:
        if not self.enabled:
            if self.required:
                raise CoachSnapshotArchiveError("coach snapshot archive is required but disabled")
            return None
        if not self.bucket:
            raise CoachSnapshotArchiveError("AI_COACH_SNAPSHOT_S3_BUCKET is required when archive is enabled")
        body = canonical_snapshot_bytes(snapshot)
        digest = hashlib.sha256(body).hexdigest()
        requested_at = _snapshot_time(snapshot)
        key = f"{self.prefix}/v1/date={requested_at:%Y-%m-%d}/{safe_analysis_id(analysis_id)}.json"
        args: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": key,
            "Body": body,
            "ContentType": "application/json",
            "ServerSideEncryption": "aws:kms" if os.getenv("AI_COACH_SNAPSHOT_KMS_KEY_ID") else "AES256",
            "IfNoneMatch": "*",
            "Metadata": {"analysis-id": safe_analysis_id(analysis_id), "sha256": digest, "contract": "coach-input.v1"},
        }
        if os.getenv("AI_COACH_SNAPSHOT_KMS_KEY_ID"):
            args["SSEKMSKeyId"] = os.environ["AI_COACH_SNAPSHOT_KMS_KEY_ID"]
        try:
            (self.client or self._default_client()).put_object(**args)
            status = "stored"
            persisted_digest: str | None = digest
        except Exception as exc:
            if _error_code(exc) in {"PreconditionFailed", "412"}:
                # The write-only IRSA deliberately cannot read financial snapshots. The
                # conditional put guarantees that an existing object was not overwritten,
                # but its digest cannot be asserted without broadening S3 read access.
                status = "already_exists_unverified"
                persisted_digest = None
            else:
                raise CoachSnapshotArchiveError(f"coach snapshot archive failed: {exc.__class__.__name__}") from exc
        return {"bucket": self.bucket, "key": key, "sha256": persisted_digest, "status": status}

    def _default_client(self):
        import boto3

        self.client = boto3.client("s3", region_name=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION"))
        return self.client


def canonical_snapshot_bytes(snapshot: dict[str, Any]) -> bytes:
    return json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def safe_analysis_id(value: str) -> str:
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
    raw = str(value)
    normalized = "".join(char for char in raw if char in allowed)
    if not raw or not normalized:
        raise CoachSnapshotArchiveError("analysis id is empty after sanitization")
    if normalized == raw and len(raw) <= 128:
        return normalized
    suffix = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{normalized[:111]}-{suffix}"


def _snapshot_time(snapshot: dict[str, Any]) -> datetime:
    value = snapshot.get("request", {}).get("requestedAt") if isinstance(snapshot.get("request"), dict) else None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def _error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict) and isinstance(response.get("Error"), dict):
        return str(response["Error"].get("Code") or "")
    return str(getattr(exc, "status_code", ""))


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
