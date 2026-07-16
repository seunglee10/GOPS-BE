from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


ALLOWED_INTERVALS = ("1m", "5m", "10m", "1h", "4h", "1D", "1W")
BUILD_INTERVALS = ("1m", "1D")
BUILD_INTERVAL_ORDER = ("1W", "1D", "4h", "1h", "10m", "5m", "1m")
BUILD_SOURCES = ("manual", "scheduled")
BUILD_PRIORITY_BY_SOURCE = {"manual": 100, "scheduled": 10}


@dataclass(frozen=True)
class ChartAssetBuildEnvelope:
    job_id: str
    requested_by: str
    submitted_at: str
    symbols: tuple[str, ...]
    intervals: tuple[str, ...] = BUILD_INTERVALS
    force: bool = False
    source: str = "manual"

    @property
    def priority(self) -> int:
        return BUILD_PRIORITY_BY_SOURCE[self.source]

    def to_dict(self) -> dict[str, Any]:
        return {
            "jobId": self.job_id,
            "requestedBy": self.requested_by,
            "submittedAt": self.submitted_at,
            "symbols": list(self.symbols),
            "intervals": list(self.intervals),
            "force": self.force,
            "source": self.source,
            "priority": self.priority,
        }

    @classmethod
    def create(
        cls,
        *,
        requested_by: str,
        symbols: list[str] | tuple[str, ...],
        intervals: list[str] | tuple[str, ...] = BUILD_INTERVALS,
        job_id: str | None = None,
        submitted_at: str | None = None,
        force: bool = False,
        source: str = "manual",
    ) -> "ChartAssetBuildEnvelope":
        normalized_symbols = tuple(dict.fromkeys(str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()))
        normalized_intervals = tuple(dict.fromkeys(str(interval).strip() for interval in intervals))
        if not normalized_symbols:
            raise ValueError("At least one chart asset symbol is required.")
        invalid = sorted(set(normalized_intervals).difference(ALLOWED_INTERVALS))
        if invalid or not normalized_intervals:
            raise ValueError(f"Unsupported chart asset intervals: {invalid or normalized_intervals}")
        normalized_source = str(source or "").strip().lower()
        if normalized_source not in BUILD_SOURCES:
            raise ValueError(f"Unsupported chart asset build source: {normalized_source}")
        return cls(
            job_id=job_id or f"cab-{uuid.uuid4()}",
            requested_by=str(requested_by or "unknown"),
            submitted_at=submitted_at or utc_now_iso(),
            symbols=normalized_symbols,
            intervals=normalized_intervals,
            force=bool(force),
            source=normalized_source,
        )


def envelope_from_dict(value: Any) -> ChartAssetBuildEnvelope:
    if not isinstance(value, dict):
        raise ValueError("Chart asset build envelope must be an object.")
    return ChartAssetBuildEnvelope.create(
        job_id=str(value.get("jobId") or "").strip() or None,
        requested_by=str(value.get("requestedBy") or "unknown"),
        submitted_at=str(value.get("submittedAt") or "").strip() or None,
        symbols=value.get("symbols") or [],
        intervals=value.get("intervals") or BUILD_INTERVALS,
        force=value.get("force", False),
        source=str(value.get("source") or "manual"),
    )


def request_fingerprint(envelope: ChartAssetBuildEnvelope) -> str:
    canonical = json.dumps(
        {
            "source": envelope.source,
            "force": envelope.force,
            "symbols": sorted(envelope.symbols),
            "intervals": sorted(envelope.intervals),
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
