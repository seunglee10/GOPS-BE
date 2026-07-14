from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any


class CoachSnapshotArchiveError(RuntimeError):
    pass


class CoachSnapshotReuseError(CoachSnapshotArchiveError):
    """Raised when an immutable existing input cannot be verified and reused."""


class CoachReportArchive:
    """Durable, post-market CoachReport reader/writer backed only by S3.

    The immutable daily report is addressed through a small per-user latest
    pointer, so the API never needs S3 ListBucket permission or Redis state.
    """

    def __init__(self, *, client=None, bucket: str | None = None, prefix: str | None = None) -> None:
        self.client = client
        self.bucket = bucket or os.getenv("AI_COACH_SNAPSHOT_S3_BUCKET")
        self.prefix = (prefix or os.getenv("AI_COACH_REPORT_S3_PREFIX", "ai-coach/reports/v1")).strip("/")

    @property
    def enabled(self) -> bool:
        return _env_bool("AI_COACH_SNAPSHOT_ARCHIVE_ENABLED", False)

    def put_daily(self, report: dict[str, Any], *, user_id: str, trading_date: str) -> dict[str, str] | None:
        if not self.enabled:
            return None
        if not self.bucket:
            raise CoachSnapshotArchiveError("AI_COACH_SNAPSHOT_S3_BUCKET is required when archive is enabled")
        if not isinstance(report, dict) or not report.get("contractVersion"):
            raise CoachSnapshotArchiveError("coach report contract is invalid")
        subject = _subject_hash(user_id)
        key = f"{self.prefix}/user={subject}/date={trading_date}/report.json"
        body = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        digest = hashlib.sha256(body).hexdigest()
        client = self.client or self._default_client()
        report_was_already_stored = False
        try:
            client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=body,
                ContentType="application/json",
                ServerSideEncryption="AES256",
                IfNoneMatch="*",
                Metadata={"sha256": digest, "contract": str(report["contractVersion"])},
            )
        except Exception as exc:
            if _error_code(exc) not in {"PreconditionFailed", "412"}:
                raise CoachSnapshotArchiveError(f"coach report archive failed: {exc.__class__.__name__}") from exc
            report_was_already_stored = True
        if report_was_already_stored:
            # An immutable daily object wins on a retry.  Never publish a
            # pointer digest for retry bytes that were not actually stored.
            try:
                existing = _json_object(client.get_object(Bucket=self.bucket, Key=key))
                body = json.dumps(existing, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
                digest = hashlib.sha256(body).hexdigest()
            except Exception as exc:
                raise CoachSnapshotArchiveError(f"stored coach report could not be verified: {exc.__class__.__name__}") from exc
        latest_key = f"{self.prefix}/user={subject}/latest.json"
        pointer = json.dumps({"reportKey": key, "tradingDate": trading_date, "sha256": digest}, separators=(",", ":")).encode("utf-8")
        try:
            client.put_object(
                Bucket=self.bucket,
                Key=latest_key,
                Body=pointer,
                ContentType="application/json",
                ServerSideEncryption="AES256",
            )
        except Exception as exc:
            raise CoachSnapshotArchiveError(f"coach report latest pointer failed: {exc.__class__.__name__}") from exc
        return {"key": key, "latestKey": latest_key, "sha256": digest}

    def get_latest(self, *, user_id: str) -> dict[str, Any] | None:
        if not self.enabled or not self.bucket:
            return None
        client = self.client or self._default_client()
        subject = _subject_hash(user_id)
        try:
            pointer_response = client.get_object(Bucket=self.bucket, Key=f"{self.prefix}/user={subject}/latest.json")
            pointer = _json_object(pointer_response)
            report_key = str(pointer.get("reportKey") or "")
            if not report_key.startswith(f"{self.prefix}/user={subject}/"):
                raise CoachSnapshotArchiveError("coach report pointer is outside the user prefix")
            report_response = client.get_object(Bucket=self.bucket, Key=report_key)
            report = _json_object(report_response)
        except Exception as exc:
            if _error_code(exc) in {"NoSuchKey", "404", "NotFound"}:
                return None
            if isinstance(exc, CoachSnapshotArchiveError):
                raise
            raise CoachSnapshotArchiveError(f"coach report lookup failed: {exc.__class__.__name__}") from exc
        if not report.get("contractVersion") or not report.get("analysisId"):
            raise CoachSnapshotArchiveError("stored coach report contract is invalid")
        return report

    def _default_client(self):
        import boto3

        self.client = boto3.client("s3", region_name=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION"))
        return self.client


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
        key = self._key(analysis_id, requested_at)
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
                # Conditional put proves an existing object was not overwritten. This
                # method has not read it yet; the worker must call get_existing and use
                # only the verified first object after this intermediate status.
                status = "already_exists_unverified"
                persisted_digest = None
            else:
                raise CoachSnapshotArchiveError(f"coach snapshot archive failed: {exc.__class__.__name__}") from exc
        return {"bucket": self.bucket, "key": key, "sha256": persisted_digest, "status": status}

    def get_existing(
        self,
        analysis_id: str,
        requested_at: str | datetime | None,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Return the first immutable snapshot for a Kafka retry, if present."""

        if not self.enabled:
            return None
        if not self.bucket:
            raise CoachSnapshotArchiveError("AI_COACH_SNAPSHOT_S3_BUCKET is required when archive is enabled")
        timestamp = _parse_time(requested_at)
        key = self._key(analysis_id, timestamp)
        try:
            response = (self.client or self._default_client()).get_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            if _error_code(exc) in {"NoSuchKey", "404", "NotFound"}:
                return None
            raise CoachSnapshotReuseError(f"existing coach snapshot lookup failed: {exc.__class__.__name__}") from exc
        body = response.get("Body") if isinstance(response, dict) else None
        try:
            raw = body.read() if hasattr(body, "read") else body
            payload = bytes(raw) if isinstance(raw, (bytes, bytearray)) else str(raw or "").encode("utf-8")
            digest = hashlib.sha256(payload).hexdigest()
            parsed = json.loads(payload.decode("utf-8"))
        except Exception as exc:
            raise CoachSnapshotReuseError("existing coach snapshot is unreadable") from exc
        metadata = response.get("Metadata") if isinstance(response, dict) and isinstance(response.get("Metadata"), dict) else {}
        expected_digest = str(metadata.get("sha256") or "")
        if expected_digest and expected_digest != digest:
            raise CoachSnapshotReuseError("existing coach snapshot digest mismatch")
        if not isinstance(parsed, dict) or parsed.get("schemaVersion") != "coach-input.v1":
            raise CoachSnapshotReuseError("existing coach snapshot contract is invalid")
        request = parsed.get("request") if isinstance(parsed.get("request"), dict) else {}
        if str(request.get("analysisId") or "") != str(analysis_id):
            raise CoachSnapshotReuseError("existing coach snapshot analysis id mismatch")
        return parsed, {
            "bucket": self.bucket,
            "key": key,
            "sha256": digest,
            "status": "already_exists_reused",
        }

    def _key(self, analysis_id: str, requested_at: datetime) -> str:
        return f"{self.prefix}/v1/date={requested_at:%Y-%m-%d}/{safe_analysis_id(analysis_id)}.json"

    def _default_client(self):
        import boto3

        self.client = boto3.client("s3", region_name=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION"))
        return self.client


def canonical_snapshot_bytes(snapshot: dict[str, Any]) -> bytes:
    return json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _subject_hash(user_id: str) -> str:
    return hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()[:24]


def _json_object(response: Any) -> dict[str, Any]:
    body = response.get("Body") if isinstance(response, dict) else None
    raw = body.read() if hasattr(body, "read") else body
    payload = bytes(raw).decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw or "")
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise CoachSnapshotArchiveError("stored coach archive payload is not an object")
    return parsed


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
    return _parse_time(value)


def _parse_time(value: Any) -> datetime:
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
