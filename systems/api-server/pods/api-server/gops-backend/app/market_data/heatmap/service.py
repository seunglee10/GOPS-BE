from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.core.sectors import sector_payload_fields
from app.market_data.fundamentals.service import FundamentalsAdapter, FundamentalsRecord, build_fundamentals_adapter
from app.services.alfaka_market_data import normalize_market_symbol


DEFAULT_HEATMAP_UNIVERSE = "sp500"
DEFAULT_QUOTE_REFRESH_SECONDS = 60
DEFAULT_LAYOUT_REFRESH_SECONDS = 300
DEFAULT_CACHE_TTL_SECONDS = 70
DEFAULT_STALE_CACHE_TTL_SECONDS = 900
DEFAULT_MINIMUM_MARKET_CAP = 1_000_000.0
LAYOUT_SOURCE_PRIORITY = {
    "minimum": 0,
    "seed": 1,
    "cached-projection": 2,
    "fundamentals": 3,
}


class MarketHeatmapService:
    def __init__(self, provider=None, fundamentals_adapter: FundamentalsAdapter | None = None):
        from app.services.alfaka_market_data import get_market_data_provider

        self.provider = provider or get_market_data_provider()
        self.fundamentals_adapter = fundamentals_adapter or build_fundamentals_adapter(provider=self.provider)

    def snapshot(self, universe: str = DEFAULT_HEATMAP_UNIVERSE) -> dict[str, Any]:
        universe_name = normalize_universe(universe)
        fresh_cache_key = heatmap_cache_key(universe_name)
        stale_cache_key = heatmap_stale_cache_key(universe_name)
        cached = self._cache_get(fresh_cache_key)
        if cached:
            return normalize_heatmap_sector_fields(cached)

        previous = self._cache_get(stale_cache_key) or {}
        previous_items = items_by_symbol(previous.get("items"))
        seed_items = load_heatmap_seed_items(universe_name)
        symbols = [item["symbol"] for item in seed_items]
        fundamentals = self.fundamentals_adapter.latest_for_symbols(symbols)
        quote_rows = quote_rows_by_symbol(self.provider, symbols)
        now = utc_now()
        quote_as_of = latest_quote_timestamp(quote_rows.values()) or parse_timestamp(previous.get("quoteAsOf")) or now
        layout_seconds = read_positive_int("HEATMAP_LAYOUT_REFRESH_SECONDS", DEFAULT_LAYOUT_REFRESH_SECONDS)
        layout_as_of = floor_timestamp(now, layout_seconds)
        layout_as_of_text = isoformat_z(layout_as_of)
        keep_previous_layout = read_string(previous.get("layoutAsOf")) == layout_as_of_text

        items = [
            build_heatmap_item(
                seed_item=seed_item,
                quote_row=quote_rows.get(seed_item["symbol"]),
                fundamentals=fundamentals.get(seed_item["symbol"]),
                previous_item=previous_items.get(seed_item["symbol"]),
                keep_previous_layout=keep_previous_layout,
            )
            for seed_item in seed_items
        ]
        payload = {
            "source": "market-heatmap-projection",
            "universe": universe_name,
            "layoutAsOf": layout_as_of_text,
            "quoteAsOf": isoformat_z(quote_as_of),
            "quoteRefreshSeconds": read_positive_int("HEATMAP_QUOTE_REFRESH_SECONDS", DEFAULT_QUOTE_REFRESH_SECONDS),
            "layoutRefreshSeconds": layout_seconds,
            "fundamentalsSource": fundamentals_source(),
            "items": items,
            "coverage": coverage(items, len(fundamentals), len(quote_rows)),
        }
        self._cache_set(fresh_cache_key, payload, read_positive_int("HEATMAP_CACHE_TTL_SECONDS", DEFAULT_CACHE_TTL_SECONDS))
        self._cache_set(stale_cache_key, payload, read_positive_int("HEATMAP_STALE_CACHE_TTL_SECONDS", DEFAULT_STALE_CACHE_TTL_SECONDS))
        return normalize_heatmap_sector_fields(payload)

    def _redis(self):
        redis_provider = getattr(self.provider, "redis_provider", None)
        return getattr(redis_provider, "redis", None)

    def _cache_get(self, key: str) -> dict[str, Any] | None:
        redis_client = self._redis()
        if redis_client is None:
            return None
        try:
            raw = redis_client.get(key)
        except Exception:
            return None
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    def _cache_set(self, key: str, payload: dict[str, Any], ttl: int) -> None:
        redis_client = self._redis()
        if redis_client is None:
            return
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        try:
            redis_client.set(key, encoded, ex=ttl)
        except TypeError:
            try:
                redis_client.set(key, encoded)
                redis_client.expire(key, ttl)
            except Exception:
                return
        except Exception:
            return


def get_heatmap_service(provider=None, fundamentals_adapter: FundamentalsAdapter | None = None) -> MarketHeatmapService:
    return MarketHeatmapService(provider=provider, fundamentals_adapter=fundamentals_adapter)


def build_heatmap_item(
    seed_item: dict[str, Any],
    quote_row: dict[str, Any] | None,
    fundamentals: FundamentalsRecord | None,
    previous_item: dict[str, Any] | None = None,
    keep_previous_layout: bool = False,
) -> dict[str, Any]:
    symbol = seed_item["symbol"]
    quote_row = quote_row or {}
    previous_item = previous_item or {}
    last_price = read_float(quote_row.get("lastPrice"))
    price_from_previous = False
    if last_price is None:
        last_price = read_float(previous_item.get("lastPrice"))
        price_from_previous = last_price is not None

    shares_outstanding = fundamentals.sharesOutstanding if fundamentals else None
    seed_market_cap = read_float(seed_item.get("marketCap"))
    market_cap, market_cap_source = project_market_cap(
        price=last_price,
        shares_outstanding=shares_outstanding,
        seed_market_cap=seed_market_cap,
        previous_item=previous_item,
        previous_cap_key="marketCap",
        previous_source_key="marketCapSource",
    )

    price_source = "cached-projection" if price_from_previous else read_string(quote_row.get("rankReason") or quote_row.get("priceSource"))
    price_updated_at = read_string(quote_row.get("sourceUpdatedAt") or quote_row.get("priceUpdatedAt")) or read_string(previous_item.get("priceUpdatedAt"))
    layout_price = last_price
    layout_price_source = price_source
    layout_price_updated_at = price_updated_at
    layout_market_cap = None
    layout_market_cap_source = None
    if keep_previous_layout and should_keep_previous_layout(previous_item, market_cap_source):
        layout_market_cap = read_float(previous_item.get("layoutMarketCap"))
        layout_market_cap_source = read_string(previous_item.get("layoutMarketCapSource"))
        layout_price = read_float(previous_item.get("layoutPrice"))
        layout_price_source = read_string(previous_item.get("layoutPriceSource"))
        layout_price_updated_at = read_string(previous_item.get("layoutPriceUpdatedAt"))
    if layout_market_cap is None:
        layout_price = last_price
        layout_price_source = price_source
        layout_price_updated_at = price_updated_at
        layout_market_cap, layout_market_cap_source = project_market_cap(
            price=layout_price,
            shares_outstanding=shares_outstanding,
            seed_market_cap=seed_market_cap,
            previous_item=previous_item,
            previous_cap_key="layoutMarketCap",
            previous_source_key="layoutMarketCapSource",
        )

    if price_from_previous:
        previous_close = read_previous_close(previous_item.get("previousClose"))
        change_percent = read_float(previous_item.get("changePercent"))
    else:
        previous_close = read_previous_close(quote_row.get("previousClose"))
        change_percent = previous_close_change_percent(last_price, previous_close)

    public_fundamentals = fundamentals.to_public_dict() if fundamentals else {}
    sector_fields = sector_payload_fields(public_fundamentals.get("sector") or seed_item.get("sector") or "Unclassified")
    rsi14 = read_float(quote_row.get("rsi14"))
    if rsi14 is None:
        rsi14 = read_float(previous_item.get("rsi14"))
    return {
        "symbol": symbol,
        "companyName": public_fundamentals.get("companyName") or seed_item.get("companyName") or symbol,
        **sector_fields,
        "industry": public_fundamentals.get("industry") or seed_item.get("industry") or "Unclassified",
        "cik": public_fundamentals.get("cik"),
        "lastPrice": last_price,
        "previousClose": previous_close,
        "changePercent": change_percent,
        "sharesOutstanding": shares_outstanding,
        "marketCap": market_cap,
        "marketCapSource": market_cap_source,
        "layoutPrice": layout_price,
        "layoutMarketCap": layout_market_cap,
        "layoutMarketCapSource": layout_market_cap_source,
        "layoutPriceSource": layout_price_source,
        "layoutPriceUpdatedAt": layout_price_updated_at,
        "fundamentalsAsOf": public_fundamentals.get("asOf"),
        "fundamentalsSource": public_fundamentals.get("source"),
        "fiscalPeriod": public_fundamentals.get("fiscalPeriod"),
        "periodEndDate": public_fundamentals.get("periodEndDate"),
        "filedAt": public_fundamentals.get("filedAt"),
        "revenue": public_fundamentals.get("revenue"),
        "operatingIncome": public_fundamentals.get("operatingIncome"),
        "netIncome": public_fundamentals.get("netIncome"),
        "eps": public_fundamentals.get("eps"),
        "totalAssets": public_fundamentals.get("totalAssets"),
        "totalLiabilities": public_fundamentals.get("totalLiabilities"),
        "totalEquity": public_fundamentals.get("totalEquity"),
        "operatingCashFlow": public_fundamentals.get("operatingCashFlow"),
        "freeCashFlow": public_fundamentals.get("freeCashFlow"),
        "currency": public_fundamentals.get("currency") or "USD",
        "volume": read_float(quote_row.get("volume")) or read_float(previous_item.get("volume")),
        "sessionDollarVolume": read_float(quote_row.get("sessionDollarVolume")) or read_float(previous_item.get("sessionDollarVolume")),
        "rsi14": rsi14,
        "priceSource": price_source,
        "priceUpdatedAt": price_updated_at,
    }


def normalize_heatmap_sector_fields(payload: dict[str, Any]) -> dict[str, Any]:
    items = payload.get("items")
    if not isinstance(items, list):
        return payload
    normalized_items = []
    for item in items:
        if not isinstance(item, dict):
            continue
        normalized_items.append({**item, **sector_payload_fields(item.get("sector"))})
    payload["items"] = normalized_items
    return payload


def should_keep_previous_layout(previous_item: dict[str, Any], current_market_cap_source: str) -> bool:
    previous_source = read_string(previous_item.get("layoutMarketCapSource"))
    if not previous_source:
        return False
    previous_priority = LAYOUT_SOURCE_PRIORITY.get(previous_source, -1)
    current_priority = LAYOUT_SOURCE_PRIORITY.get(current_market_cap_source, -1)
    return previous_priority >= current_priority


def project_market_cap(
    *,
    price: float | None,
    shares_outstanding: float | int | None,
    seed_market_cap: float | None,
    previous_item: dict[str, Any],
    previous_cap_key: str,
    previous_source_key: str,
) -> tuple[float, str]:
    if price is not None and shares_outstanding:
        return price * shares_outstanding, "fundamentals"
    if read_string(previous_item.get(previous_source_key)) == "fundamentals":
        previous_cap = read_float(previous_item.get(previous_cap_key))
        if previous_cap is not None:
            return previous_cap, "cached-projection"
    if seed_market_cap is not None:
        return seed_market_cap, "seed"
    return DEFAULT_MINIMUM_MARKET_CAP, "minimum"


def quote_rows_by_symbol(provider: Any, symbols: list[str]) -> dict[str, dict[str, Any]]:
    clickhouse_provider = getattr(provider, "clickhouse_provider", None)
    latest_quotes = getattr(clickhouse_provider, "latest_quotes", None)
    rank_symbols = getattr(clickhouse_provider, "rank_symbols", None)
    rows: list[dict[str, Any]] = []
    if callable(latest_quotes):
        try:
            rows = latest_quotes(symbols, limit=len(symbols))
        except Exception:
            rows = []
    if callable(rank_symbols):
        if not rows:
            try:
                rows = rank_symbols(symbols, kind="dollar-volume", limit=len(symbols))
            except Exception:
                rows = []
    by_symbol = {
        normalize_market_symbol(row["symbol"]): row
        for row in rows or []
        if isinstance(row, dict) and isinstance(row.get("symbol"), str)
    }
    latest_daily_rsi14 = getattr(clickhouse_provider, "latest_daily_rsi14", None)
    if callable(latest_daily_rsi14):
        try:
            rsi_by_symbol = latest_daily_rsi14(symbols)
        except Exception:
            rsi_by_symbol = {}
        for symbol, rsi14 in (rsi_by_symbol or {}).items():
            normalized = normalize_market_symbol(symbol)
            by_symbol.setdefault(normalized, {"symbol": normalized})["rsi14"] = rsi14
    overlay_redis_latest_prices(provider, symbols, by_symbol)
    return by_symbol


def overlay_redis_latest_prices(provider: Any, symbols: list[str], by_symbol: dict[str, dict[str, Any]]) -> None:
    redis_provider = getattr(provider, "redis_provider", None)
    latest_price = getattr(redis_provider, "latest_price", None)
    if not callable(latest_price):
        return
    for symbol in symbols:
        try:
            row = latest_price(symbol) or {}
        except Exception:
            continue
        price = read_float(row.get("price") or row.get("lastPrice"))
        if price is None:
            continue
        normalized = normalize_market_symbol(symbol)
        merged = dict(by_symbol.get(normalized) or {})
        merged.update({
            "symbol": normalized,
            "lastPrice": price,
            "sourceUpdatedAt": read_string(row.get("timestamp") or row.get("eventTime") or row.get("sourceUpdatedAt"))
                or merged.get("sourceUpdatedAt"),
            "rankReason": "redis_live",
        })
        by_symbol[normalized] = merged


@lru_cache(maxsize=4)
def load_heatmap_seed_items(universe: str = DEFAULT_HEATMAP_UNIVERSE) -> list[dict[str, Any]]:
    path = heatmap_seed_path(universe)
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw_items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(raw_items, list):
        raise ValueError(f"Heatmap seed file has no items: {path}")
    items = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict) or not isinstance(raw_item.get("symbol"), str):
            continue
        item = dict(raw_item)
        item["symbol"] = normalize_market_symbol(item["symbol"])
        items.append(item)
    return items


def heatmap_seed_path(universe: str) -> Path:
    configured = (os.getenv("HEATMAP_UNIVERSE_REGISTRY_PATH") or "").strip()
    if configured:
        path = Path(configured)
        return path if path.is_absolute() else repo_root() / path
    if universe != DEFAULT_HEATMAP_UNIVERSE:
        raise ValueError(f"Unsupported heatmap universe: {universe}")
    return repo_root() / "systems" / "market-data" / "config" / "sp500-heatmap-seed.json"


def repo_root() -> Path:
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "systems").exists():
            return parent
        if parent.name == "systems":
            return parent.parent
    return current.parents[-1]


def heatmap_cache_key(universe: str) -> str:
    return redis_key(f"heatmap:{universe}")


def heatmap_stale_cache_key(universe: str) -> str:
    return redis_key(f"heatmap:{universe}:last")


def redis_key(value: str) -> str:
    prefix = (os.getenv("REDIS_KEY_PREFIX") or "gops:market:on-demand:v1").strip().strip(":")
    return f"{prefix}:{value}" if prefix else value


def normalize_universe(value: str) -> str:
    normalized = (value or DEFAULT_HEATMAP_UNIVERSE).strip().lower()
    if normalized != DEFAULT_HEATMAP_UNIVERSE:
        raise ValueError(f"Unsupported heatmap universe: {normalized}")
    return normalized


def coverage(items: list[dict[str, Any]], fundamentals_count: int, quote_count: int) -> dict[str, Any]:
    return {
        "total": len(items),
        "priced": sum(1 for item in items if item.get("lastPrice") is not None),
        "quoted": quote_count,
        "fundamentals": fundamentals_count,
        "marketCapFromFundamentals": sum(1 for item in items if item.get("marketCapSource") == "fundamentals"),
        "marketCapFromSeed": sum(1 for item in items if item.get("marketCapSource") == "seed"),
        "marketCapFromCache": sum(1 for item in items if item.get("marketCapSource") == "cached-projection"),
        "layoutMarketCapFromFundamentals": sum(1 for item in items if item.get("layoutMarketCapSource") == "fundamentals"),
        "layoutMarketCapFromSeed": sum(1 for item in items if item.get("layoutMarketCapSource") == "seed"),
        "layoutMarketCapFromCache": sum(1 for item in items if item.get("layoutMarketCapSource") == "cached-projection"),
    }


def fundamentals_source() -> dict[str, Any]:
    return {
        "source": os.getenv("FUNDAMENTALS_SOURCE", "auto"),
        "storeConfigured": True,
        "fileConfigured": bool((os.getenv("FUNDAMENTALS_LATEST_FILE") or os.getenv("HEATMAP_FUNDAMENTALS_PATH") or "").strip()),
        "urlConfigured": bool((os.getenv("FUNDAMENTALS_LATEST_URL") or "").strip()),
    }


def latest_quote_timestamp(rows: Any) -> datetime | None:
    latest: datetime | None = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        parsed = parse_timestamp(row.get("sourceUpdatedAt") or row.get("priceUpdatedAt"))
        if parsed and (latest is None or parsed > latest):
            latest = parsed
    return latest


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def floor_timestamp(value: datetime, seconds: int) -> datetime:
    timestamp = int(value.timestamp())
    return datetime.fromtimestamp(timestamp - (timestamp % max(1, seconds)), timezone.utc)


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def isoformat_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def items_by_symbol(items: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(items, list):
        return {}
    return {
        normalize_market_symbol(item["symbol"]): item
        for item in items
        if isinstance(item, dict) and isinstance(item.get("symbol"), str)
    }


def read_string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def read_positive_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def read_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def previous_close_change_percent(last_price: float | None, previous_close: float | None) -> float | None:
    if last_price is None or previous_close is None or previous_close == 0:
        return None
    return round(((last_price - previous_close) / previous_close) * 100, 2)


def read_previous_close(value: Any) -> float | None:
    previous_close = read_float(value)
    return previous_close if previous_close is not None and previous_close > 0 else None
