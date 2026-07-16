"""Immutable contract for the 2026-07-15 KST S&P top-company replay."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final


DATASET_ID: Final = "sp500-top20-20260715-kst-v1"
DATASET_SCHEMA_VERSION: Final = 1
DATASET_START: Final = datetime(2026, 7, 14, 15, 0, tzinfo=UTC)
DATASET_END: Final = datetime(2026, 7, 15, 15, 0, tzinfo=UTC)
MARKET_CAP_SNAPSHOT_DATE: Final = "2026-06-30"

REPLAY_SYMBOLS: Final = (
    "NVDA", "MSFT", "AAPL", "AMZN", "META", "AVGO", "GOOGL", "GOOG", "BRK.B",
    "TSLA", "LLY", "JPM", "WMT", "V", "ORCL", "MA", "NFLX", "XOM", "COST", "HD", "JNJ",
)

COMPANY_BY_SYMBOL: Final = {
    "NVDA": "NVIDIA", "MSFT": "Microsoft", "AAPL": "Apple", "AMZN": "Amazon",
    "META": "Meta Platforms", "AVGO": "Broadcom", "GOOGL": "Alphabet", "GOOG": "Alphabet",
    "BRK.B": "Berkshire Hathaway", "TSLA": "Tesla", "LLY": "Eli Lilly",
    "JPM": "JPMorgan Chase", "WMT": "Walmart", "V": "Visa", "ORCL": "Oracle",
    "MA": "Mastercard", "NFLX": "Netflix", "XOM": "Exxon Mobil", "COST": "Costco",
    "HD": "Home Depot", "JNJ": "Johnson & Johnson",
}


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

ALLOWED_SPEEDS: Final = (1, 5, 20, 60, 300)


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
