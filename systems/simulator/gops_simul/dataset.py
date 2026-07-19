"""Immutable contract for the 2026-07-15 KST full S&P 500 replay."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final


DATASET_ID: Final = "sp500-full-20260715-kst-v3"
DATASET_SCHEMA_VERSION: Final = 3
DATASET_S3_PREFIX: Final = "simulator/replay/v3/dataset=sp500-full-20260715-kst"
DATASET_START: Final = datetime(2026, 7, 14, 15, 0, tzinfo=UTC)
DATASET_END: Final = datetime(2026, 7, 15, 15, 0, tzinfo=UTC)
MARKET_CAP_SNAPSHOT_DATE: Final = "2026-06-30"
EXPECTED_SYMBOL_COUNT: Final = 502
UNIVERSE_SYMBOLS_SHA256: Final = "c1e72d49557182d11cd64d33bba16778f7b4184e5dfd58b921f2b46fe0d10cef"


def _config_path(filename: str, env_name: str) -> Path:
    candidates: list[Path | None] = [
        Path(configured) if (configured := os.getenv(env_name, "").strip()) else None,
        Path("/app/config") / filename,
    ]
    module_parents = Path(__file__).resolve().parents
    if len(module_parents) > 3:
        candidates.append(module_parents[3] / "systems" / "market-data" / "config" / filename)
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return candidate
    raise RuntimeError(f"simulator config is unavailable: {filename}")


def _load_replay_universe() -> tuple[tuple[str, ...], dict[str, str], dict[str, str]]:
    universe_path = _config_path("sp500-universe.json", "SIM_REPLAY_UNIVERSE_PATH")
    payload = json.loads(universe_path.read_text(encoding="utf-8"))
    symbols = tuple(str(symbol).strip().upper() for symbol in payload.get("symbols", []))
    digest = hashlib.sha256("\n".join(symbols).encode("utf-8")).hexdigest()
    if len(symbols) != EXPECTED_SYMBOL_COUNT or len(set(symbols)) != EXPECTED_SYMBOL_COUNT:
        raise RuntimeError(f"fixed replay universe must contain {EXPECTED_SYMBOL_COUNT} unique symbols")
    if payload.get("sourceRetrievedAt") != MARKET_CAP_SNAPSHOT_DATE or digest != UNIVERSE_SYMBOLS_SHA256:
        raise RuntimeError("fixed replay universe changed; create a new immutable dataset version")

    company_path = _config_path("sp500-heatmap-seed.json", "SIM_REPLAY_COMPANY_SEED_PATH")
    company_payload = json.loads(company_path.read_text(encoding="utf-8"))
    company_names = {
        str(item.get("symbol") or "").strip().upper(): str(item.get("companyName") or "").strip()
        for item in company_payload.get("items", [])
        if isinstance(item, dict)
    }
    companies = {symbol: company_names.get(symbol) or symbol for symbol in symbols}
    provenance = {
        "source": str(payload.get("source") or ""),
        "sourceRetrievedAt": str(payload.get("sourceRetrievedAt") or ""),
        "symbolsSha256": digest,
    }
    return symbols, companies, provenance


REPLAY_SYMBOLS, COMPANY_BY_SYMBOL, UNIVERSE_PROVENANCE = _load_replay_universe()
REPLAY_SYMBOL_SET: Final = frozenset(REPLAY_SYMBOLS)


@dataclass(frozen=True)
class FeedSegment:
    feed: str
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None or self.start >= self.end:
            raise ValueError("feed segment requires an ordered timezone-aware interval")


FEED_SEGMENTS: Final = (
    FeedSegment("sip", DATASET_START, datetime(2026, 7, 15, 0, 0, tzinfo=UTC)),
    FeedSegment("boats", datetime(2026, 7, 15, 0, 0, tzinfo=UTC), datetime(2026, 7, 15, 8, 0, tzinfo=UTC)),
    FeedSegment("sip", datetime(2026, 7, 15, 8, 0, tzinfo=UTC), DATASET_END),
)

ALLOWED_SPEEDS: Final = (1, 2, 5, 10)


def in_half_open_window(value: datetime, start: datetime, end: datetime) -> bool:
    if value.tzinfo is None or start.tzinfo is None or end.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return start <= value < end


def parse_timestamp(value: object) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError("event timestamp is required")
    parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    if parsed.tzinfo is None:
        raise ValueError("event timestamp must include a timezone")
    return parsed.astimezone(UTC)


def isoformat_z(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def dataset_manifest_template() -> dict[str, object]:
    return {
        "schemaVersion": DATASET_SCHEMA_VERSION,
        "datasetId": DATASET_ID,
        "status": "BUILDING",
        "marketCapSnapshotDate": MARKET_CAP_SNAPSHOT_DATE,
        "universe": {**UNIVERSE_PROVENANCE, "symbolCount": len(REPLAY_SYMBOLS)},
        "startTime": isoformat_z(DATASET_START),
        "endTimeExclusive": isoformat_z(DATASET_END),
        "symbols": list(REPLAY_SYMBOLS),
        "companies": dict(COMPANY_BY_SYMBOL),
        "segments": [
            {"feed": item.feed, "start": isoformat_z(item.start), "endExclusive": isoformat_z(item.end)}
            for item in FEED_SEGMENTS
        ],
        "files": [],
        "counts": {"events": 0, "trades": 0, "quotes": 0, "bySymbol": {}},
    }
