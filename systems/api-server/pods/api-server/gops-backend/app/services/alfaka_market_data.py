# 역할: GOPS backend가 alfaka Redis/ClickHouse provider를 읽게 연결합니다.
# 사용: 과거 캔들은 ClickHouse, 최신/실시간 캔들은 Redis에서 가져옵니다.
# 설정: ALPACA_UNIVERSE, ALPACA_SYMBOLS, REDIS_URL, CLICKHOUSE_* 값을 .env 또는 Docker env에 넣습니다.
from __future__ import annotations

import os
import re
import sys
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException


SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9]{0,9}(?:\.[A-Z])?$")


def _add_alfaka_package_path() -> None:
    # 로컬/컨테이너 모두 systems/market-data/shared 안의 alfaka 패키지를 먼저 확인합니다.
    candidates = [os.getenv("ALFAKA_PACKAGES_PATH"), "/app/systems/market-data/shared"]
    current_file = Path(__file__).resolve()
    candidates.extend(str(parent / "systems" / "market-data" / "shared") for parent in current_file.parents)

    for candidate in candidates:
        if not candidate:
            continue
        package_path = Path(candidate)
        if (package_path / "alfaka").exists() and str(package_path) not in sys.path:
            sys.path.insert(0, str(package_path))
            return


_add_alfaka_package_path()

from alfaka.alpaca.subscription import (  # noqa: E402
    configured_seed_symbols,
    configured_universe_symbols as load_configured_universe_symbols,
)
from alfaka.common.redis_keys import RedisKeyBuilder  # noqa: E402
from alfaka.serving.intervals import candle_count_for_24h  # noqa: E402
from alfaka.serving.hot_symbols import DEFAULT_HOT_LIMIT, build_hot_symbols_payload  # noqa: E402
from alfaka.serving.provider import MarketDataProvider  # noqa: E402
from alfaka.serving.time_utils import parse_utc_time  # noqa: E402


MAX_WATCHLIST_SYMBOLS = 10
MAX_HOT_SYMBOLS = 10
DEFAULT_MARKET_TIMEZONE = "America/New_York"


def configured_symbols() -> list[str]:
    # Legacy/local smoke seed입니다. 프론트 Watch List의 진실은 /api/charts/watchlist입니다.
    # ALPACA_UNIVERSE 전체를 자동 UI Watch List로 확장하지 않습니다.
    try:
        return [normalize_market_symbol(symbol) for symbol in configured_seed_symbols()]
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def configured_universe_symbols() -> list[str]:
    # 검색/검증 후보군은 ALPACA_UNIVERSE를 따릅니다.
    try:
        return [normalize_market_symbol(symbol) for symbol in load_configured_universe_symbols()]
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def normalize_market_symbol(symbol: str) -> str:
    # 프론트에서 들어온 회사명/심볼은 여기서 Alpaca 심볼 형식으로 정리합니다.
    # 한글 회사명 매핑은 subscription.py 쪽 신청 로직에서 관리하고, API는 심볼을 받습니다.
    normalized = symbol.strip().upper()
    if not SYMBOL_PATTERN.match(normalized):
        raise HTTPException(status_code=400, detail=f"Invalid market symbol: {normalized}")
    return normalized


def requested_ma_from_csv(value: str) -> list[int]:
    # 프론트가 ma=5,20,60처럼 요청하면 허용된 이동평균선만 남깁니다.
    # 실제 ma 값은 Flink/local processor가 만든 캔들 payload의 ma5/ma20/ma60을 씁니다.
    requested = []
    for item in value.split(","):
        item = item.strip()
        if item.isdigit() and int(item) in {5, 20, 60}:
            requested.append(int(item))
    return requested or [5, 20, 60]


@lru_cache(maxsize=1)
def get_market_data_provider() -> MarketDataProvider:
    # GOPS API와 WebSocket이 공통으로 쓰는 provider입니다.
    # 내부에서 Redis 최근 캔들 + ClickHouse 과거 캔들을 합쳐 GOPS DTO로 반환합니다.
    return MarketDataProvider()


def symbol_summaries() -> list[dict[str, Any]]:
    # 기존 /api/charts/symbols 호환 요약입니다. 사용자 Watch List는 watchlist_summaries를 씁니다.
    # Redis에 최신 가격이 없으면 ClickHouse serving projection의 최신 1m candle로 보완합니다.
    return symbol_summaries_for(configured_symbols())


def search_symbol_summaries(query: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    requested_limit = min(_read_positive_int(limit) or MAX_WATCHLIST_SYMBOLS, len(configured_universe_symbols()))
    query_text = (query or "").strip()
    if not query_text:
        return symbol_summaries_for(configured_symbols()[:requested_limit])

    provider = get_market_data_provider()
    allowed = set(configured_universe_symbols())
    matches: list[str] = []
    try:
        records = provider.search_symbols(query_text, requested_limit)
    except Exception:
        records = []
    for record in records or []:
        symbol_value = record.get("symbol") if isinstance(record, dict) else None
        if not isinstance(symbol_value, str):
            continue
        symbol = normalize_market_symbol(symbol_value)
        if symbol not in allowed or symbol in matches:
            continue
        matches.append(symbol)
        if len(matches) >= requested_limit:
            break
    if not matches:
        matches = _configured_symbol_search(query_text, requested_limit)
    return symbol_summaries_for(matches, max_items=requested_limit)


def symbol_summaries_for(symbols: list[str], max_items: int | None = MAX_WATCHLIST_SYMBOLS) -> list[dict[str, Any]]:
    return [build_symbol_summary(symbol) for symbol in normalize_symbol_list(symbols, max_items=max_items)]


def watchlist_summaries(symbols: list[str] | None = None) -> dict[str, Any]:
    persisted = False
    if symbols is not None:
        requested_symbols = normalize_watchlist_symbol_list(symbols)
    else:
        requested_symbols = read_watchlist_symbols()
        persisted = bool(requested_symbols)
        if not requested_symbols:
            requested_symbols = default_watchlist_symbols()
    return {
        "source": "alpaca",
        "feed": "configured-market-feed",
        "persisted": persisted,
        "symbols": symbol_summaries_for(requested_symbols),
    }


def replace_watchlist_symbols(symbols: list[str]) -> dict[str, Any]:
    requested_symbols = normalize_watchlist_symbol_list(symbols)
    provider = get_market_data_provider()
    key = RedisKeyBuilder().watchlist_symbols()
    try:
        redis_client = provider.redis_provider.redis
        delete_method = getattr(redis_client, "delete", None)
        if callable(delete_method):
            delete_method(key)
        elif hasattr(redis_client, "sets"):
            redis_client.sets.pop(key, None)
        if requested_symbols:
            redis_client.sadd(key, *requested_symbols)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Watch List sync failed: {exc}") from exc
    return {
        "source": "alpaca",
        "feed": "configured-market-feed",
        "persisted": True,
        "symbols": symbol_summaries_for(requested_symbols),
    }


def read_watchlist_symbols() -> list[str]:
    provider = get_market_data_provider()
    key = RedisKeyBuilder().watchlist_symbols()
    try:
        values = provider.redis_provider.redis.smembers(key)
    except Exception:
        values = []
    return normalize_watchlist_symbol_list(sorted(values), reject_outside=False)


def normalize_symbol_list(symbols: list[str], max_items: int | None = None) -> list[str]:
    normalized_symbols = []
    seen = set()
    for value in symbols:
        if not isinstance(value, str):
            continue
        symbol = normalize_market_symbol(value)
        if symbol in seen:
            continue
        normalized_symbols.append(symbol)
        seen.add(symbol)
        if max_items and len(normalized_symbols) >= max_items:
            break
    return normalized_symbols


def normalize_watchlist_symbol_list(symbols: list[str], reject_outside: bool = True) -> list[str]:
    allowed = set(configured_universe_symbols())
    normalized = []
    seen = set()
    outside = []
    for value in symbols or []:
        if not isinstance(value, str):
            continue
        symbol = normalize_market_symbol(value)
        if symbol not in allowed:
            outside.append(symbol)
            continue
        if symbol in seen:
            continue
        normalized.append(symbol)
        seen.add(symbol)
        if len(normalized) >= MAX_WATCHLIST_SYMBOLS:
            break
    if outside and reject_outside:
        raise HTTPException(status_code=400, detail=f"Symbols outside configured universe: {', '.join(sorted(set(outside)))}")
    return normalized


def default_watchlist_symbols() -> list[str]:
    return normalize_watchlist_symbol_list(configured_symbols(), reject_outside=False)


def _configured_symbol_search(query: str, limit: int) -> list[str]:
    normalized_query = query.strip().upper()
    matches: list[str] = []
    provider = get_market_data_provider()
    for symbol in configured_universe_symbols():
        metadata = _symbol_metadata(provider, symbol)
        haystack = f"{symbol} {metadata.get('name') or ''}".upper()
        if normalized_query in haystack:
            matches.append(symbol)
        if len(matches) >= limit:
            break
    return matches


def hot_symbol_summaries(limit: int | None = None) -> dict[str, Any]:
    requested_limit = min(_read_positive_int(limit) or _read_positive_int(os.getenv("HOT_TIER_SIZE")) or DEFAULT_HOT_LIMIT, MAX_HOT_SYMBOLS)
    provider = get_market_data_provider()
    snapshot = _safe_hot_snapshot(provider)
    if snapshot:
        return _limit_hot_snapshot(provider, snapshot, requested_limit)

    universe = configured_universe_symbols()
    clickhouse_records = _safe_clickhouse_hot_symbols(provider, universe, requested_limit)
    if clickhouse_records:
        return build_hot_symbols_payload(_enrich_hot_symbol_records(provider, clickhouse_records), limit=requested_limit)

    scan_limit = _read_positive_int(os.getenv("HOT_TIER_FALLBACK_SCAN_LIMIT")) or 20
    records = []
    for symbol in universe[:scan_limit]:
        records.append(build_hot_symbol_record(provider, symbol))
    return build_hot_symbols_payload(records, limit=requested_limit)


def build_hot_symbol_record(provider: MarketDataProvider, symbol: str) -> dict[str, Any]:
    symbol = normalize_market_symbol(symbol)
    metadata = _symbol_metadata(provider, symbol)
    latest_price = _safe_latest_price(provider, symbol)
    candles = _safe_recent_candles(provider, symbol, candle_count_for_24h("1m"))
    rank_reason = "redis_1m_session"
    if not candles:
        candles = _safe_clickhouse_candles(provider, symbol, candle_count_for_24h("1m"))
        rank_reason = "clickhouse_1m_session"
    if not candles:
        candles = _safe_clickhouse_candles(provider, symbol, 1, interval="1D")
        rank_reason = "latest_1d_fallback"

    last_candle = candles[-1] if candles else {}
    last_price = _read_float(latest_price.get("price")) or _read_float(last_candle.get("close"))
    change_percent = _change_percent_from_previous_close(
        provider,
        symbol,
        last_price,
        candles,
        anchor_timestamp=latest_price.get("timestamp") or last_candle.get("timestamp"),
    )

    return {
        "symbol": symbol,
        "name": metadata.get("name") or symbol,
        "market": metadata.get("market") or metadata.get("exchange") or "US",
        "lastPrice": last_price,
        "changePercent": change_percent,
        "volume": _read_float(last_candle.get("volume")),
        "candles": candles,
        "rankReason": rank_reason if candles else "no_data_fallback",
    }


def build_symbol_summary(symbol: str) -> dict[str, Any]:
    provider = get_market_data_provider()
    symbol = normalize_market_symbol(symbol)
    metadata = _symbol_metadata(provider, symbol)
    latest_price = _safe_latest_price(provider, symbol)
    candles = _safe_recent_candles(provider, symbol, candle_count_for_24h("1m"))
    if not candles:
        candles = _safe_clickhouse_candles(provider, symbol, candle_count_for_24h("1m"))
    last_candle = candles[-1] if candles else {}
    last_price = _read_float(latest_price.get("price")) or _read_float(last_candle.get("close"))
    change_percent = _change_percent_from_previous_close(
        provider,
        symbol,
        last_price,
        candles,
        anchor_timestamp=latest_price.get("timestamp") or last_candle.get("timestamp"),
    )

    return {
        "symbol": symbol,
        "name": metadata.get("name") or symbol,
        "market": metadata.get("market") or metadata.get("exchange") or "US",
        "lastPrice": last_price,
        "changePercent": change_percent,
        "volume": _read_float(last_candle.get("volume")),
    }


def _symbol_metadata(provider: MarketDataProvider, symbol: str) -> dict[str, Any]:
    try:
        return provider.symbol_detail(symbol)
    except Exception:
        return {}


def _safe_latest_price(provider: MarketDataProvider, symbol: str) -> dict[str, Any]:
    try:
        return provider.redis_provider.latest_price(symbol) or {}
    except Exception:
        return {}


def _safe_recent_candles(provider: MarketDataProvider, symbol: str, limit: int) -> list[dict[str, Any]]:
    try:
        return provider.redis_provider.recent_candles(symbol, "1m", limit)
    except Exception:
        return []


def _safe_clickhouse_candles(provider: MarketDataProvider, symbol: str, limit: int, interval: str = "1m") -> list[dict[str, Any]]:
    try:
        return provider.clickhouse_provider.candles(symbol, interval, limit)
    except Exception:
        return []


def _safe_hot_snapshot(provider: MarketDataProvider) -> dict[str, Any] | None:
    try:
        snapshot = provider.redis_provider.hot_symbols_snapshot()
    except Exception:
        return None
    if isinstance(snapshot, dict) and isinstance(snapshot.get("symbols"), list):
        return snapshot
    return None


def _limit_hot_snapshot(provider: MarketDataProvider, snapshot: dict[str, Any], limit: int) -> dict[str, Any]:
    ranking = dict(snapshot.get("ranking") or {})
    ranking["limit"] = limit
    allowed = set(configured_universe_symbols())
    rows = [
        row for row in list(snapshot.get("symbols") or [])
        if isinstance(row, dict) and isinstance(row.get("symbol"), str) and normalize_market_symbol(row["symbol"]) in allowed
    ][:limit]
    return {
        **snapshot,
        "ranking": ranking,
        "symbols": [_enrich_quote_record(provider, row) for row in rows if isinstance(row, dict)],
    }


def _safe_clickhouse_hot_symbols(provider: MarketDataProvider, symbols: list[str], limit: int) -> list[dict[str, Any]]:
    method = getattr(provider.clickhouse_provider, "hot_symbols_by_dollar_volume", None)
    if not callable(method):
        return []
    try:
        rows = method(symbols, limit=limit)
    except Exception:
        return []
    return rows if isinstance(rows, list) else []


def _enrich_hot_symbol_records(provider: MarketDataProvider, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched = []
    for record in records:
        enriched_record = _enrich_quote_record(provider, record)
        if enriched_record:
            enriched.append(enriched_record)
    return enriched


def _enrich_quote_record(provider: MarketDataProvider, record: dict[str, Any]) -> dict[str, Any] | None:
    symbol = record.get("symbol")
    if not isinstance(symbol, str):
        return None
    normalized_symbol = normalize_market_symbol(symbol)
    metadata = _symbol_metadata(provider, normalized_symbol)
    latest_price = _safe_latest_price(provider, normalized_symbol)
    live_price = _read_float(latest_price.get("price"))
    last_price = live_price if live_price is not None else _read_float(record.get("lastPrice"))
    change_percent = _change_percent_from_previous_close(
        provider,
        normalized_symbol,
        last_price,
        candles=None,
        anchor_timestamp=latest_price.get("timestamp") or record.get("sourceUpdatedAt"),
    )
    return {
        **record,
        "symbol": normalized_symbol,
        "name": record.get("name") or metadata.get("name") or symbol,
        "market": record.get("market") or metadata.get("market") or metadata.get("exchange") or "US",
        "lastPrice": last_price,
        "changePercent": change_percent,
    }


def _change_percent_from_previous_close(
    provider: MarketDataProvider,
    symbol: str,
    last_price: float | None,
    candles: list[dict[str, Any]] | None,
    anchor_timestamp: Any = None,
) -> float | None:
    if last_price is None:
        return None
    baseline = _previous_close_baseline(provider, symbol, candles or [], anchor_timestamp)
    if baseline in (None, 0):
        return None
    return round(((last_price - baseline) / baseline) * 100, 2)


def _previous_close_baseline(
    provider: MarketDataProvider,
    symbol: str,
    candles: list[dict[str, Any]],
    anchor_timestamp: Any = None,
) -> float | None:
    session_date = _market_session_date(anchor_timestamp) or _latest_intraday_session_date(candles)
    daily_candles = _safe_clickhouse_candles(provider, symbol, 4, interval="1D")
    daily_closes_by_date = {}
    for daily_date, close in (_daily_close_entry(candle) for candle in daily_candles):
        if daily_date and close is not None:
            daily_closes_by_date[daily_date] = close
    daily_closes = sorted(daily_closes_by_date.items())
    if session_date:
        previous = [close for daily_date, close in daily_closes if daily_date and daily_date < session_date]
        if previous:
            return previous[-1]
        return _previous_intraday_close_baseline(provider, symbol, session_date)
    if len(daily_closes) >= 2:
        return daily_closes[-2][1]
    return None


def _previous_intraday_close_baseline(provider: MarketDataProvider, symbol: str, session_date: date) -> float | None:
    lookback_limit = candle_count_for_24h("1m") * 5
    candles = _safe_clickhouse_candles(provider, symbol, lookback_limit, interval="1m")
    previous_close = None
    for candle in candles:
        candle_date = _market_session_date(candle.get("timestamp"))
        if candle_date and candle_date < session_date:
            previous_close = _read_float(candle.get("close"))
    return previous_close


def _daily_close_entry(candle: dict[str, Any]) -> tuple[date | None, float | None]:
    timestamp = candle.get("timestamp")
    daily_date = None
    if timestamp:
        try:
            daily_date = date.fromisoformat(str(timestamp)[:10])
        except ValueError:
            daily_date = None
    return daily_date, _read_float(candle.get("close"))


def _latest_intraday_session_date(candles: list[dict[str, Any]]) -> date | None:
    for candle in reversed(candles):
        session_date = _market_session_date(candle.get("timestamp"))
        if session_date:
            return session_date
    return None


def _market_session_date(timestamp: Any) -> date | None:
    parsed = parse_utc_time(timestamp)
    if not parsed:
        return None
    return parsed.astimezone(_market_zone()).date()


@lru_cache(maxsize=4)
def _market_zone() -> ZoneInfo:
    try:
        return ZoneInfo(os.getenv("MARKET_TIMEZONE") or DEFAULT_MARKET_TIMEZONE)
    except Exception:
        return ZoneInfo(DEFAULT_MARKET_TIMEZONE)


def _read_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _read_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
