from __future__ import annotations

import json
import os
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from app.market_data.fill.service import get_on_demand_fill_service
from app.market_data.query.canonical import CanonicalCandleQuery
from app.services.alfaka_market_data import get_market_data_provider, normalize_market_symbol
from market_data.alpaca.feed_profiles import MARKET_TIMEZONE, market_session_for_timestamp


COMPARE_RANGE_VALUES = {"1D", "1M", "6M", "1Y", "5Y"}
COMPARE_BASE_MODES = {"first_close"}
COMPARE_SESSIONS = {"regular"}
COMPARE_ADJUSTMENTS = {"split"}
DEFAULT_COMPARE_MAX_SYMBOLS = 10
DEFAULT_COMPARE_CACHE_TTLS = {
    "1D": 60,
    "1M": 900,
    "6M": 21_600,
    "1Y": 21_600,
    "5Y": 86_400,
}
COMPARE_COLORS = [
    "#33adff",
    "#ff7a3d",
    "#7bd88f",
    "#b890ff",
    "#ff5f6d",
    "#42d7d0",
    "#f2c94c",
    "#ff8bd1",
    "#9aa5b1",
    "#c99a6b",
]


@dataclass(frozen=True)
class CompareRangeConfig:
    range_id: str
    timeframe: str
    lookback: timedelta
    latest_regular_day: bool = False
    filter_regular_session: bool = False


COMPARE_RANGE_CONFIGS = {
    "1D": CompareRangeConfig("1D", "1Min", timedelta(days=7), latest_regular_day=True, filter_regular_session=True),
    "1M": CompareRangeConfig("1M", "1Hour", timedelta(days=31), filter_regular_session=True),
    "6M": CompareRangeConfig("6M", "1Day", timedelta(days=183)),
    "1Y": CompareRangeConfig("1Y", "1Day", timedelta(days=366)),
    "5Y": CompareRangeConfig("5Y", "1Week", timedelta(days=366 * 5 + 7)),
}


class ChartCompareService:
    def __init__(
        self,
        provider: Any | None = None,
        fetcher: Callable[[str, str, str, str, str], list[dict[str, Any]]] | None = None,
        now: Callable[[], datetime] | None = None,
        candle_query: CanonicalCandleQuery | None = None,
    ):
        self.provider = provider or get_market_data_provider()
        self.fetcher = fetcher
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.candle_query = candle_query or CanonicalCandleQuery(
            self.provider,
            get_on_demand_fill_service(self.provider),
        )

    def snapshot(
        self,
        symbols: Iterable[str],
        range_value: str = "1D",
        *,
        base_mode: str = "first_close",
        adjustment: str = "split",
        session: str = "regular",
    ) -> dict[str, Any]:
        normalized_symbols = normalize_compare_symbols(list(symbols))
        if not normalized_symbols:
            raise ValueError("At least one symbol is required.")
        max_symbols = compare_max_symbols()
        if len(normalized_symbols) > max_symbols:
            raise ValueError(f"At most {max_symbols} comparison symbols are allowed.")

        range_id = normalize_range(range_value)
        if base_mode not in COMPARE_BASE_MODES:
            raise ValueError(f"Unsupported comparison baseMode: {base_mode}")
        if adjustment not in COMPARE_ADJUSTMENTS:
            raise ValueError(f"Unsupported comparison adjustment: {adjustment}")
        if session not in COMPARE_SESSIONS:
            raise ValueError(f"Unsupported comparison session: {session}")

        config = COMPARE_RANGE_CONFIGS[range_id]
        cache_key = compare_cache_key(normalized_symbols, range_id, base_mode, adjustment, session)
        ttl_seconds = compare_cache_ttl(range_id)
        cached = self._cache_get(cache_key) if compare_cache_enabled() else None
        if cached:
            cached["cache"] = {"hit": True, "ttlSeconds": ttl_seconds, "key": cache_key}
            return cached

        end = ensure_utc(self.now())
        start = end - config.lookback
        items: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []

        query_interval, query_limit = compare_query_contract(range_id)
        max_workers = min(3, len(normalized_symbols))
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="chart-compare") as executor:
            futures = [
                executor.submit(
                    self.candle_query.query,
                    symbol,
                    query_interval,
                    query_limit,
                    from_time=isoformat_z(start),
                    to_time=isoformat_z(end),
                    ma_windows=(),
                )
                for symbol in normalized_symbols
            ]

        for index, (symbol, future) in enumerate(zip(normalized_symbols, futures)):
            metadata = symbol_metadata(self.provider, symbol)
            try:
                candle_payload = future.result()
                raw_bars = candle_payload.get("candles") or []
                points = compare_points_from_bars(raw_bars, config)
                item = build_compare_item(symbol, metadata, points, COMPARE_COLORS[index % len(COMPARE_COLORS)])
                if item.get("error"):
                    warnings.append({"symbol": symbol, "code": item["error"], "message": item.get("message")})
                items.append(item)
            except Exception as exc:
                message = str(exc) or exc.__class__.__name__
                warnings.append({"symbol": symbol, "code": "provider_error", "message": message})
                items.append(empty_compare_item(symbol, metadata, COMPARE_COLORS[index % len(COMPARE_COLORS)], "provider_error", message))

        payload = {
            "range": range_id,
            "timeframe": config.timeframe,
            "baseMode": base_mode,
            "session": session,
            "adjustment": adjustment,
            "asOf": isoformat_z(end),
            "items": items,
            "warnings": warnings,
            "cache": {"hit": False, "ttlSeconds": ttl_seconds, "key": cache_key},
        }
        if compare_cache_enabled():
            self._cache_set(cache_key, payload, ttl_seconds)
        return payload

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
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            payload = json.loads(raw)
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    def _cache_set(self, key: str, payload: dict[str, Any], ttl_seconds: int) -> None:
        redis_client = self._redis()
        if redis_client is None:
            return
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        try:
            redis_client.set(key, encoded, ex=ttl_seconds)
        except TypeError:
            try:
                redis_client.set(key, encoded)
                redis_client.expire(key, ttl_seconds)
            except Exception:
                return
        except Exception:
            return


def get_chart_compare_service(provider=None) -> ChartCompareService:
    return ChartCompareService(provider=provider)


def compare_query_contract(range_id: str) -> tuple[str, int]:
    return {
        "1D": ("1m", 10_000),
        "1M": ("1h", 1_000),
        "6M": ("1D", 250),
        "1Y": ("1D", 500),
        "5Y": ("1W", 300),
    }[range_id]


def normalize_compare_symbols(symbols: list[str]) -> list[str]:
    normalized: list[str] = []
    for value in symbols:
        symbol = normalize_market_symbol(value)
        if symbol not in normalized:
            normalized.append(symbol)
    return normalized


def normalize_range(value: str) -> str:
    normalized = str(value or "1D").strip().upper()
    if normalized not in COMPARE_RANGE_VALUES:
        raise ValueError(f"Unsupported comparison range: {value}")
    return normalized


def compare_points_from_bars(raw_bars: list[dict[str, Any]], config: CompareRangeConfig) -> list[dict[str, Any]]:
    points = sorted(
        (point for point in (point_from_bar(row) for row in raw_bars or []) if point is not None),
        key=lambda point: point["time"],
    )
    if config.filter_regular_session:
        points = [point for point in points if market_session_for_timestamp(point["time"]) == "regular"]
    if config.latest_regular_day:
        points = latest_local_trading_day_points(points)
    return points


def point_from_bar(row: dict[str, Any]) -> dict[str, Any] | None:
    timestamp = row.get("t") or row.get("timestamp") or row.get("time")
    close = row.get("c", row.get("close"))
    if not isinstance(timestamp, str):
        return None
    price = read_float(close)
    if price is None:
        return None
    return {"time": normalize_timestamp(timestamp), "price": price}


def latest_local_trading_day_points(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_date: dict[str, list[dict[str, Any]]] = {}
    for point in points:
        parsed = parse_timestamp(point["time"])
        if parsed is None:
            continue
        local_date = parsed.astimezone(MARKET_TIMEZONE).date().isoformat()
        by_date.setdefault(local_date, []).append(point)
    if not by_date:
        return []
    latest_date = max(by_date)
    return by_date[latest_date]


def build_compare_item(symbol: str, metadata: dict[str, Any], points: list[dict[str, Any]], color: str) -> dict[str, Any]:
    base = next((point for point in points if read_float(point.get("price")) not in (None, 0)), None)
    if not base:
        return empty_compare_item(symbol, metadata, color, "no_data", "No historical bars were returned for this comparison range.")
    base_price = base["price"]
    normalized_points = []
    for point in points:
        price = read_float(point.get("price"))
        if price is None:
            continue
        normalized_points.append({
            "time": point["time"],
            "price": price,
            "returnPercent": ((price - base_price) / base_price) * 100,
        })
    if not normalized_points:
        return empty_compare_item(symbol, metadata, color, "no_data", "No valid close prices were returned for this comparison range.")
    last_price = normalized_points[-1]["price"]
    change = last_price - base_price
    change_percent = ((last_price - base_price) / base_price) * 100
    return {
        "symbol": symbol,
        "companyName": read_string(metadata.get("name")) or symbol,
        "exchange": read_string(metadata.get("exchange")),
        "color": color,
        "basePrice": base_price,
        "lastPrice": last_price,
        "change": change,
        "changePercent": change_percent,
        "points": normalized_points,
    }


def empty_compare_item(symbol: str, metadata: dict[str, Any], color: str, error: str, message: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "companyName": read_string(metadata.get("name")) or symbol,
        "exchange": read_string(metadata.get("exchange")),
        "color": color,
        "basePrice": None,
        "lastPrice": None,
        "change": None,
        "changePercent": None,
        "points": [],
        "error": error,
        "message": message,
    }


def symbol_metadata(provider: Any, symbol: str) -> dict[str, Any]:
    try:
        detail = provider.symbol_detail(symbol)
    except Exception:
        return {}
    return detail if isinstance(detail, dict) else {}


def compare_cache_key(symbols: list[str], range_id: str, base_mode: str, adjustment: str, session: str) -> str:
    key_symbols = ",".join(symbols)
    prefix = os.getenv("REDIS_KEY_PREFIX", "gops:market:on-demand:v1").strip().strip(":")
    namespace = f"{prefix}:" if prefix else ""
    return f"{namespace}chart:compare:v1:range={range_id}:symbols={key_symbols}:base={base_mode}:session={session}:adjustment={adjustment}"


def compare_cache_ttl(range_id: str) -> int:
    default = DEFAULT_COMPARE_CACHE_TTLS[range_id]
    env_key = f"CHART_COMPARE_CACHE_TTL_{range_id}_SECONDS"
    return read_positive_int(os.getenv(env_key), default)


def compare_max_symbols() -> int:
    return read_positive_int(os.getenv("CHART_COMPARE_MAX_SYMBOLS"), DEFAULT_COMPARE_MAX_SYMBOLS)


def compare_cache_enabled() -> bool:
    return str(os.getenv("CHART_COMPARE_CACHE_ENABLED", "true")).strip().lower() not in {"0", "false", "no", "off"}


def read_positive_int(raw: Any, default: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def read_float(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value == value else None


def read_string(raw: Any) -> str | None:
    value = str(raw).strip() if raw is not None else ""
    return value or None


def parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_timestamp(value: str) -> str:
    parsed = parse_timestamp(value)
    return isoformat_z(parsed) if parsed else value


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def isoformat_z(value: datetime) -> str:
    return ensure_utc(value).isoformat(timespec="seconds").replace("+00:00", "Z")
