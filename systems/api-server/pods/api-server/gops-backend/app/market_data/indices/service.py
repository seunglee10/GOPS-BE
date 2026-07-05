from __future__ import annotations

import json
import math
import os
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

DEFAULT_CACHE_TTL_SECONDS = 30
DEFAULT_STALE_CACHE_TTL_SECONDS = 1_800
DEFAULT_REFRESH_LOCK_SECONDS = 15
DEFAULT_REFRESH_SECONDS = 30
DEFAULT_STALE_REFRESH_SECONDS = 300
DEFAULT_UPSTREAM_TIMEOUT_SECONDS = 5
DEFAULT_PERIOD = "5d"
DEFAULT_INTERVAL = "5m"


INDEX_DEFINITIONS: tuple[dict[str, str], ...] = (
    {
        "symbol": "^GSPC",
        "name": "S&P 500",
        "assetClass": "equity_index",
        "group": "US",
        "currency": "USD",
        "unit": "points",
    },
    {
        "symbol": "^IXIC",
        "name": "NASDAQ Composite",
        "assetClass": "equity_index",
        "group": "US",
        "currency": "USD",
        "unit": "points",
    },
    {
        "symbol": "^DJI",
        "name": "Dow Jones Industrial Average",
        "assetClass": "equity_index",
        "group": "US",
        "currency": "USD",
        "unit": "points",
    },
    {
        "symbol": "^RUT",
        "name": "Russell 2000",
        "assetClass": "equity_index",
        "group": "US",
        "currency": "USD",
        "unit": "points",
    },
    {
        "symbol": "^VIX",
        "name": "CBOE Volatility Index",
        "assetClass": "volatility_index",
        "group": "US",
        "currency": "USD",
        "unit": "points",
    },
    {
        "symbol": "^SOX",
        "name": "Philadelphia Semiconductor Index",
        "assetClass": "equity_index",
        "group": "US",
        "currency": "USD",
        "unit": "points",
    },
    {
        "symbol": "^KS11",
        "name": "KOSPI",
        "assetClass": "equity_index",
        "group": "Korea",
        "currency": "KRW",
        "unit": "points",
    },
    {
        "symbol": "^KQ11",
        "name": "KOSDAQ",
        "assetClass": "equity_index",
        "group": "Korea",
        "currency": "KRW",
        "unit": "points",
    },
    {
        "symbol": "^N225",
        "name": "Nikkei 225",
        "assetClass": "equity_index",
        "group": "Asia",
        "currency": "JPY",
        "unit": "points",
    },
    {
        "symbol": "^HSI",
        "name": "Hang Seng Index",
        "assetClass": "equity_index",
        "group": "Asia",
        "currency": "HKD",
        "unit": "points",
    },
    {
        "symbol": "GC=F",
        "name": "Gold Futures",
        "assetClass": "commodity",
        "group": "Commodities",
        "currency": "USD",
        "unit": "USD/oz",
    },
    {
        "symbol": "CL=F",
        "name": "WTI Crude Oil Futures",
        "assetClass": "commodity",
        "group": "Commodities",
        "currency": "USD",
        "unit": "USD/bbl",
    },
    {
        "symbol": "BTC-USD",
        "name": "Bitcoin",
        "assetClass": "crypto",
        "group": "Crypto",
        "currency": "USD",
        "unit": "USD",
    },
    {
        "symbol": "DX-Y.NYB",
        "name": "U.S. Dollar Index",
        "assetClass": "currency_index",
        "group": "FX",
        "currency": "USD",
        "unit": "points",
    },
    {
        "symbol": "KRW=X",
        "name": "USD/KRW",
        "assetClass": "fx",
        "group": "FX",
        "currency": "KRW",
        "unit": "KRW per USD",
    },
)

Fetcher = Callable[..., Any]
_indices_service: Any | None = None


def get_indices_service(provider: Any | None = None) -> "MarketIndicesService":
    global _indices_service
    if provider is not None:
        return MarketIndicesService(provider=provider)
    if _indices_service is None:
        _indices_service = MarketIndicesService()
    return _indices_service


class MarketIndicesService:
    def __init__(self, provider: Any | None = None, fetcher: Fetcher | None = None) -> None:
        if provider is None:
            from app.services.alfaka_market_data import get_market_data_provider

            provider = get_market_data_provider()
        self.provider = provider
        self.fetcher = fetcher or fetch_yahoo_indices

    def snapshot(self, background_tasks: Any | None = None) -> dict[str, Any]:
        fresh_key = indices_cache_key()
        stale_key = indices_stale_cache_key()
        fresh = self._cache_get(fresh_key)
        if fresh is not None:
            return with_cache_status(fresh, "fresh")

        stale = self._cache_get(stale_key)
        lock_key = indices_lock_key()
        lock_token = uuid.uuid4().hex
        lock_acquired = self._acquire_lock(lock_key, lock_token)
        if not lock_acquired:
            if stale is not None:
                return with_cache_status(
                    stale,
                    "stale",
                    "Refresh already in progress; serving the last successful snapshot.",
                )
            raise RuntimeError("Market indices refresh is already in progress and no cache is available.")

        if stale is not None and background_tasks is not None:
            background_tasks.add_task(self._refresh_locked, fresh_key, stale_key, lock_key, lock_token)
            return with_cache_status(
                stale,
                "stale",
                "Refreshing in background; serving the last successful snapshot.",
            )

        try:
            return with_cache_status(self._refresh_locked(fresh_key, stale_key, lock_key, lock_token), "fresh")
        except RuntimeError as exc:
            if stale is not None:
                return with_cache_status(stale, "stale", str(exc))
            raise

    def _refresh_locked(self, fresh_key: str, stale_key: str, lock_key: str, lock_token: str) -> dict[str, Any]:
        try:
            payload = self._build_snapshot()
            self._cache_set(fresh_key, payload, cache_ttl_seconds())
            self._cache_set(stale_key, payload, stale_cache_ttl_seconds())
        except Exception as exc:
            raise RuntimeError(f"Yahoo Finance refresh failed: {exc.__class__.__name__}") from exc
        finally:
            self._release_lock(lock_key, lock_token)

        return payload

    def _build_snapshot(self) -> dict[str, Any]:
        definitions = list(INDEX_DEFINITIONS)
        symbols = [item["symbol"] for item in definitions]
        period = os.getenv("MARKET_INDICES_PERIOD", DEFAULT_PERIOD)
        interval = os.getenv("MARKET_INDICES_INTERVAL", DEFAULT_INTERVAL)
        timeout = read_positive_int("MARKET_INDICES_UPSTREAM_TIMEOUT_SECONDS", DEFAULT_UPSTREAM_TIMEOUT_SECONDS)
        raw = self.fetcher(symbols=symbols, period=period, interval=interval, timeout=timeout)
        rows_by_symbol = normalize_index_rows(raw, symbols)
        items = [build_index_item(definition, rows_by_symbol.get(definition["symbol"], [])) for definition in definitions]
        priced_count = sum(1 for item in items if item.get("price") is not None)
        return {
            "source": "yahoo-finance",
            "cacheStatus": "fresh",
            "warning": None,
            "updatedAt": isoformat_z(utc_now()),
            "refreshSeconds": read_positive_int("MARKET_INDICES_REFRESH_SECONDS", DEFAULT_REFRESH_SECONDS),
            "staleRefreshSeconds": read_positive_int(
                "MARKET_INDICES_STALE_REFRESH_SECONDS",
                DEFAULT_STALE_REFRESH_SECONDS,
            ),
            "period": period,
            "interval": interval,
            "coverage": {
                "total": len(items),
                "priced": priced_count,
                "missing": [item["symbol"] for item in items if item.get("price") is None],
            },
            "items": items,
        }

    def _redis(self) -> Any | None:
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
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if not isinstance(raw, str):
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if isinstance(data, dict):
            return data
        return None

    def _cache_set(self, key: str, payload: Mapping[str, Any], ttl: int) -> None:
        redis_client = self._redis()
        if redis_client is None:
            return
        try:
            redis_client.set(key, json.dumps(payload, separators=(",", ":"), ensure_ascii=False), ex=ttl)
        except Exception:
            return

    def _acquire_lock(self, key: str, token: str) -> bool:
        redis_client = self._redis()
        if redis_client is None:
            return True
        ttl = read_positive_int("MARKET_INDICES_REFRESH_LOCK_SECONDS", DEFAULT_REFRESH_LOCK_SECONDS)
        try:
            acquired = redis_client.set(key, token, ex=ttl, nx=True)
        except TypeError:
            existing = redis_client.get(key)
            if existing is not None:
                return False
            redis_client.set(key, token, ex=ttl)
            acquired = True
        except Exception:
            return True
        return bool(acquired)

    def _release_lock(self, key: str, token: str) -> None:
        redis_client = self._redis()
        if redis_client is None:
            return
        try:
            current = redis_client.get(key)
            if isinstance(current, bytes):
                current = current.decode("utf-8")
            if current == token:
                redis_client.delete(key)
        except Exception:
            return


def fetch_yahoo_indices(*, symbols: Sequence[str], period: str, interval: str, timeout: int) -> Any:
    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError("yfinance is not installed") from exc

    return yf.download(
        list(symbols),
        period=period,
        interval=interval,
        group_by="ticker",
        auto_adjust=False,
        progress=False,
        threads=True,
        timeout=timeout,
    )


def normalize_index_rows(raw: Any, symbols: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
    if isinstance(raw, Mapping):
        normalized: dict[str, list[dict[str, Any]]] = {}
        for symbol in symbols:
            normalized[symbol] = normalize_row_sequence(raw.get(symbol))
        return normalized

    return {symbol: normalize_dataframe_rows(dataframe_for_symbol(raw, symbol, symbols)) for symbol in symbols}


def dataframe_for_symbol(raw: Any, symbol: str, symbols: Sequence[str]) -> Any:
    columns = getattr(raw, "columns", None)
    if columns is None:
        return None
    nlevels = getattr(columns, "nlevels", 1)
    if nlevels and nlevels > 1:
        try:
            return raw[symbol]
        except Exception:
            return None
    if len(symbols) == 1:
        return raw
    return None


def normalize_dataframe_rows(frame: Any) -> list[dict[str, Any]]:
    if frame is None or not hasattr(frame, "iterrows"):
        return []
    rows: list[dict[str, Any]] = []
    try:
        iterator = frame.iterrows()
    except Exception:
        return []
    for timestamp, row in iterator:
        close = first_float(row, "Close", "Adj Close")
        if close is None:
            continue
        rows.append(
            {
                "timestamp": parse_timestamp(timestamp),
                "open": first_float(row, "Open"),
                "high": first_float(row, "High"),
                "low": first_float(row, "Low"),
                "close": close,
                "volume": first_float(row, "Volume"),
            }
        )
    return rows


def normalize_row_sequence(rows: Any) -> list[dict[str, Any]]:
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
        return []
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        close = first_float(row, "close", "Close", "adjClose", "Adj Close")
        if close is None:
            continue
        normalized.append(
            {
                "timestamp": parse_timestamp(row.get("timestamp") or row.get("date") or row.get("Datetime")),
                "open": first_float(row, "open", "Open"),
                "high": first_float(row, "high", "High"),
                "low": first_float(row, "low", "Low"),
                "close": close,
                "volume": first_float(row, "volume", "Volume"),
            }
        )
    return normalized


def build_index_item(definition: Mapping[str, str], rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    valid_rows = [
        row
        for row in rows
        if isinstance(row.get("timestamp"), datetime) and finite_float(row.get("close")) is not None
    ]
    valid_rows.sort(key=lambda row: row["timestamp"])
    latest = valid_rows[-1] if valid_rows else None
    previous_close = previous_session_close(valid_rows) if latest is not None else None
    price = finite_float(latest.get("close")) if latest is not None else None
    change = price - previous_close if price is not None and previous_close not in (None, 0) else None
    change_percent = (change / previous_close * 100) if change is not None and previous_close else None
    sparkline = [finite_float(row.get("close")) for row in valid_rows[-28:]]
    sparkline = [value for value in sparkline if value is not None]
    return {
        "symbol": definition["symbol"],
        "name": definition["name"],
        "assetClass": definition["assetClass"],
        "group": definition["group"],
        "currency": definition["currency"],
        "unit": definition["unit"],
        "price": round_float(price),
        "open": round_float(finite_float(latest.get("open")) if latest is not None else None),
        "high": round_float(finite_float(latest.get("high")) if latest is not None else None),
        "low": round_float(finite_float(latest.get("low")) if latest is not None else None),
        "previousClose": round_float(previous_close),
        "change": round_float(change),
        "changePercent": round_float(change_percent),
        "sparkline": [round_float(value) for value in sparkline],
        "updatedAt": isoformat_z(latest["timestamp"]) if latest is not None else None,
        "status": "ok" if price is not None else "no-data",
    }


def previous_session_close(rows: Sequence[Mapping[str, Any]]) -> float | None:
    if not rows:
        return None
    latest_timestamp = rows[-1]["timestamp"]
    latest_date = latest_timestamp.date()
    for row in reversed(rows[:-1]):
        timestamp = row.get("timestamp")
        value = finite_float(row.get("close"))
        if isinstance(timestamp, datetime) and timestamp.date() < latest_date and value is not None:
            return value
    for row in reversed(rows[:-1]):
        value = finite_float(row.get("close"))
        if value is not None:
            return value
    return None


def with_cache_status(payload: Mapping[str, Any], status: str, warning: str | None = None) -> dict[str, Any]:
    data = dict(payload)
    data["cacheStatus"] = status
    data["warning"] = warning
    if status == "stale":
        data["refreshSeconds"] = read_positive_int("MARKET_INDICES_STALE_REFRESH_SECONDS", DEFAULT_STALE_REFRESH_SECONDS)
    else:
        data["refreshSeconds"] = read_positive_int("MARKET_INDICES_REFRESH_SECONDS", DEFAULT_REFRESH_SECONDS)
    return data


def redis_key(value: str) -> str:
    prefix = os.getenv("REDIS_KEY_PREFIX", "gops:market:on-demand:v1")
    return f"{prefix}:{value}"


def indices_cache_key() -> str:
    return redis_key("indices:snapshot")


def indices_stale_cache_key() -> str:
    return redis_key("indices:last")


def indices_lock_key() -> str:
    return redis_key("indices:refresh-lock")


def cache_ttl_seconds() -> int:
    return read_positive_int("MARKET_INDICES_CACHE_TTL_SECONDS", DEFAULT_CACHE_TTL_SECONDS)


def stale_cache_ttl_seconds() -> int:
    return read_positive_int("MARKET_INDICES_STALE_CACHE_TTL_SECONDS", DEFAULT_STALE_CACHE_TTL_SECONDS)


def read_positive_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def first_float(row: Any, *keys: str) -> float | None:
    for key in keys:
        if isinstance(row, Mapping):
            value = row.get(key)
        else:
            try:
                value = row.get(key)
            except Exception:
                continue
        parsed = finite_float(value)
        if parsed is not None:
            return parsed
    return None


def finite_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def round_float(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value, 6)


def parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if hasattr(value, "to_pydatetime"):
        converted = value.to_pydatetime()
        if converted.tzinfo is None:
            return converted.replace(tzinfo=timezone.utc)
        return converted.astimezone(timezone.utc)
    if isinstance(value, str):
        raw = value.strip()
        if raw.endswith("Z"):
            raw = f"{raw[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return utc_now()
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return utc_now()


def isoformat_z(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
