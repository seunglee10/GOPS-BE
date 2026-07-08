from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from app.market_data.fundamentals.service import build_fundamentals_adapter, normalize_symbols


@dataclass(frozen=True)
class PortfolioMarketStats:
    symbol: str
    low52: float | None = None
    high52: float | None = None
    as_of: str | None = None
    source: str | None = None


def enrich_holdings_with_market_stats(app: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Attach SEC valuation inputs and 52-week price bounds to portfolio rows.

    This keeps frontend portfolio holdings on the GOPS API boundary: SEC values
    come from the fundamentals adapter and 52-week highs/lows from ClickHouse,
    with Redis used only as a short-lived cache for the expensive range scan.
    """
    if not isinstance(payload, dict):
        return payload
    positions = payload.get("positions")
    if not isinstance(positions, list):
        return payload

    symbols = normalize_symbols([
        str(position.get("symbol") or "")
        for position in positions
        if isinstance(position, dict)
    ])
    if not symbols:
        return payload

    provider = _market_data_provider_from_app(app)
    fundamentals_adapter = _fundamentals_adapter_from_app(app, provider)
    fundamentals = _latest_fundamentals(fundamentals_adapter, symbols)
    eps_ttm_by_symbol = _ttm_eps_by_symbol(fundamentals_adapter, symbols)
    market_stats = _portfolio_52w_stats(provider, symbols)

    enriched_positions: list[dict[str, Any]] = []
    for position in positions:
        if not isinstance(position, dict):
            continue
        symbol = str(position.get("symbol") or "").strip().upper()
        record = fundamentals.get(symbol)
        stats = market_stats.get(symbol)
        current_price = _read_float(position.get("currentPrice"))
        eps = eps_ttm_by_symbol.get(symbol)
        if eps is None and record is not None:
            eps = _positive_float(getattr(record, "eps", None))
        pe_ratio = _safe_ratio(current_price, eps)

        enriched = dict(position)
        if pe_ratio is not None:
            enriched["peRatio"] = pe_ratio
        if eps is not None:
            enriched["epsTtm"] = eps
        if record is not None:
            if getattr(record, "source", None):
                enriched["fundamentalsSource"] = record.source
            if getattr(record, "asOf", None):
                enriched["fundamentalsAsOf"] = record.asOf
            elif getattr(record, "periodEndDate", None):
                enriched["fundamentalsAsOf"] = record.periodEndDate
        if stats is not None:
            if stats.low52 is not None:
                enriched["low52"] = stats.low52
            if stats.high52 is not None:
                enriched["high52"] = stats.high52
            if stats.as_of:
                enriched["marketStatsAsOf"] = stats.as_of
            if stats.source:
                enriched["stats52wSource"] = stats.source
        enriched_positions.append(enriched)

    return {**payload, "positions": enriched_positions}


def _market_data_provider_from_app(app: Any):
    state = getattr(app, "state", None)
    if state is not None and hasattr(state, "portfolio_market_data_provider"):
        return getattr(state, "portfolio_market_data_provider", None)
    try:
        from app.services.alfaka_market_data import get_market_data_provider

        return get_market_data_provider()
    except Exception:
        return None


def _fundamentals_adapter_from_app(app: Any, provider: Any):
    state = getattr(app, "state", None)
    adapter = getattr(state, "portfolio_fundamentals_adapter", None) if state is not None else None
    if adapter is not None:
        return adapter
    try:
        return build_fundamentals_adapter(provider=provider)
    except Exception:
        return None


def _latest_fundamentals(adapter: Any, symbols: list[str]) -> dict[str, Any]:
    try:
        latest_for_symbols = getattr(adapter, "latest_for_symbols", None)
        if callable(latest_for_symbols):
            return latest_for_symbols(symbols)
    except Exception:
        return {}
    return {}


def _ttm_eps_by_symbol(adapter: Any, symbols: list[str]) -> dict[str, float]:
    financial_series = getattr(adapter, "financial_series", None)
    if not callable(financial_series):
        return {}
    result: dict[str, float] = {}
    for symbol in symbols:
        try:
            rows = financial_series(symbol, years=2, period="quarterly")
        except Exception:
            continue
        eps_values = [
            value
            for value in (_positive_float(getattr(row, "eps", None)) for row in rows or [])
            if value is not None
        ]
        if len(eps_values) >= 4:
            result[symbol] = sum(eps_values[-4:])
    return result


def _portfolio_52w_stats(provider: Any, symbols: list[str]) -> dict[str, PortfolioMarketStats]:
    if provider is None:
        return {}
    redis = _redis_from_provider(provider)
    cached = _portfolio_52w_stats_from_redis(redis, symbols)
    missing = [symbol for symbol in symbols if symbol not in cached]
    if missing:
        fetched = _portfolio_52w_stats_from_clickhouse(provider, missing)
        cached.update(fetched)
        _write_portfolio_52w_stats_to_redis(redis, fetched)
    return cached


def _portfolio_52w_stats_from_clickhouse(provider: Any, symbols: list[str]) -> dict[str, PortfolioMarketStats]:
    clickhouse = getattr(provider, "clickhouse_provider", None)
    if clickhouse is None:
        return {}
    latest_chart_candles_source = getattr(clickhouse, "latest_chart_candles_source", None)
    query_json_each_row = getattr(clickhouse, "query_json_each_row", None)
    if not callable(latest_chart_candles_source) or not callable(query_json_each_row):
        return {}
    days = _read_positive_int(os.environ.get("PORTFOLIO_52W_LOOKBACK_DAYS"), default=370)
    try:
        source_query = latest_chart_candles_source("""
            symbol IN {symbols:Array(String)}
            AND interval IN ('1D', '1d')
            AND event_time >= subtractDays(now(), {days:UInt16})
            AND is_closed = 1
            AND market_session = 'regular'
        """)
        query = f"""
        SELECT
          symbol,
          min(low) AS low52,
          max(high) AS high52,
          formatDateTime(max(event_time), '%Y-%m-%dT%H:%i:%S.000Z', 'UTC') AS marketStatsAsOf
        FROM (
          {source_query}
        )
        GROUP BY symbol
        FORMAT JSONEachRow
        """
        rows = query_json_each_row(query, {"symbols": symbols, "days": days})
    except Exception:
        return {}

    result: dict[str, PortfolioMarketStats] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        result[symbol] = PortfolioMarketStats(
            symbol=symbol,
            low52=_read_float(row.get("low52")),
            high52=_read_float(row.get("high52")),
            as_of=_read_string(row.get("marketStatsAsOf")),
            source="clickhouse.chart_candles",
        )
    return result


def _portfolio_52w_stats_from_redis(redis: Any, symbols: list[str]) -> dict[str, PortfolioMarketStats]:
    if redis is None:
        return {}
    result: dict[str, PortfolioMarketStats] = {}
    for symbol in symbols:
        try:
            raw = redis.get(_redis_key(symbol))
        except Exception:
            continue
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        result[symbol] = PortfolioMarketStats(
            symbol=symbol,
            low52=_read_float(payload.get("low52")),
            high52=_read_float(payload.get("high52")),
            as_of=_read_string(payload.get("asOf")),
            source=_read_string(payload.get("source")) or "redis.cache",
        )
    return result


def _write_portfolio_52w_stats_to_redis(redis: Any, stats: dict[str, PortfolioMarketStats]) -> None:
    if redis is None or not stats:
        return
    ttl = _read_positive_int(os.environ.get("PORTFOLIO_MARKET_STATS_TTL_SECONDS"), default=6 * 60 * 60)
    for symbol, stat in stats.items():
        payload = {
            "symbol": symbol,
            "low52": stat.low52,
            "high52": stat.high52,
            "asOf": stat.as_of,
            "source": stat.source,
        }
        try:
            redis.setex(_redis_key(symbol), ttl, json.dumps(payload, separators=(",", ":")))
        except Exception:
            continue


def _redis_from_provider(provider: Any):
    redis_provider = getattr(provider, "redis_provider", None)
    return getattr(redis_provider, "redis", None)


def _redis_key(symbol: str) -> str:
    return f"gops:portfolio:market-stats:v1:{symbol}"


def _read_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and parsed not in (float("inf"), float("-inf")) else None


def _positive_float(value: Any) -> float | None:
    parsed = _read_float(value)
    return parsed if parsed is not None and parsed > 0 else None


def _safe_ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def _read_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _read_positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default
