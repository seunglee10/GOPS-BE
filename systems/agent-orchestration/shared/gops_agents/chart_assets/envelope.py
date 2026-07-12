from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


ALLOWED_INTERVALS = ("1m", "5m", "10m", "1h", "4h", "1D", "1W")
BUILD_INTERVAL_ORDER = ("1W", "1D", "4h", "1h", "10m", "5m", "1m")


@dataclass(frozen=True)
class ChartAssetBuildEnvelope:
    job_id: str
    requested_by: str
    submitted_at: str
    symbols: tuple[str, ...]
    intervals: tuple[str, ...] = ALLOWED_INTERVALS
    force: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "jobId": self.job_id,
            "requestedBy": self.requested_by,
            "submittedAt": self.submitted_at,
            "symbols": list(self.symbols),
            "intervals": list(self.intervals),
            "force": self.force,
        }

    @classmethod
    def create(
        cls,
        *,
        requested_by: str,
        symbols: list[str] | tuple[str, ...],
        intervals: list[str] | tuple[str, ...] = ALLOWED_INTERVALS,
        job_id: str | None = None,
        submitted_at: str | None = None,
        force: bool = False,
    ) -> "ChartAssetBuildEnvelope":
        normalized_symbols = tuple(dict.fromkeys(str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()))
        normalized_intervals = tuple(dict.fromkeys(str(interval).strip() for interval in intervals))
        if not normalized_symbols:
            raise ValueError("At least one chart asset symbol is required.")
        invalid = sorted(set(normalized_intervals).difference(ALLOWED_INTERVALS))
        if invalid or not normalized_intervals:
            raise ValueError(f"Unsupported chart asset intervals: {invalid or normalized_intervals}")
        return cls(
            job_id=job_id or f"cab-{uuid.uuid4()}",
            requested_by=str(requested_by or "unknown"),
            submitted_at=submitted_at or utc_now_iso(),
            symbols=normalized_symbols,
            intervals=normalized_intervals,
            force=bool(force),
        )


def envelope_from_dict(value: Any) -> ChartAssetBuildEnvelope:
    if not isinstance(value, dict):
        raise ValueError("Chart asset build envelope must be an object.")
    return ChartAssetBuildEnvelope.create(
        job_id=str(value.get("jobId") or "").strip() or None,
        requested_by=str(value.get("requestedBy") or "unknown"),
        submitted_at=str(value.get("submittedAt") or "").strip() or None,
        symbols=value.get("symbols") or [],
        intervals=value.get("intervals") or ALLOWED_INTERVALS,
        force=value.get("force", False),
    )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
