from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.services.alfaka_market_data import normalize_market_symbol


@dataclass(frozen=True)
class FundamentalsRecord:
    symbol: str
    cik: str | None = None
    companyName: str | None = None
    sector: str | None = None
    industry: str | None = None
    sharesOutstanding: float | None = None
    currency: str | None = None
    fiscalPeriod: str | None = None
    periodEndDate: str | None = None
    filedAt: str | None = None
    source: str | None = None
    asOf: str | None = None
    raw: dict[str, Any] | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "cik": self.cik,
            "companyName": self.companyName,
            "sector": self.sector,
            "industry": self.industry,
            "sharesOutstanding": self.sharesOutstanding,
            "currency": self.currency,
            "fiscalPeriod": self.fiscalPeriod,
            "periodEndDate": self.periodEndDate,
            "filedAt": self.filedAt,
            "source": self.source,
            "asOf": self.asOf,
        }


class FundamentalsAdapter:
    def latest_for_symbols(self, symbols: list[str]) -> dict[str, FundamentalsRecord]:
        raise NotImplementedError


class EmptyFundamentalsAdapter(FundamentalsAdapter):
    def latest_for_symbols(self, symbols: list[str]) -> dict[str, FundamentalsRecord]:
        return {}


class StoreFundamentalsAdapter(FundamentalsAdapter):
    def __init__(self, provider=None):
        self.provider = provider

    def latest_for_symbols(self, symbols: list[str]) -> dict[str, FundamentalsRecord]:
        normalized = normalize_symbols(symbols)
        if not normalized:
            return {}
        records = self._from_redis(normalized)
        missing = [symbol for symbol in normalized if symbol not in records]
        if missing:
            records.update(self._from_clickhouse(missing))
        return records

    def _provider(self):
        if self.provider is None:
            from app.services.alfaka_market_data import get_market_data_provider

            self.provider = get_market_data_provider()
        return self.provider

    def _redis(self):
        redis_provider = getattr(self._provider(), "redis_provider", None)
        return getattr(redis_provider, "redis", None)

    def _clickhouse(self):
        return getattr(self._provider(), "clickhouse_provider", None)

    def _from_redis(self, symbols: list[str]) -> dict[str, FundamentalsRecord]:
        redis_client = self._redis()
        if redis_client is None:
            return {}
        records: dict[str, FundamentalsRecord] = {}
        for symbol in symbols:
            try:
                raw = redis_client.get(fundamentals_summary_key(symbol))
            except Exception:
                continue
            payload = decode_json_object(raw)
            if not payload:
                continue
            record = record_from_summary_payload(payload, fallback_symbol=symbol, source_hint="redis")
            if record is not None:
                records[record.symbol] = record
        return records

    def _from_clickhouse(self, symbols: list[str]) -> dict[str, FundamentalsRecord]:
        provider = self._clickhouse()
        query_json_each_row = getattr(provider, "query_json_each_row", None)
        if not callable(query_json_each_row):
            return {}
        facts_table = provider.table("sec_financial_facts") if hasattr(provider, "table") else "market_data.sec_financial_facts"
        tickers_table = provider.table("sec_company_tickers") if hasattr(provider, "table") else "market_data.sec_company_tickers"
        try:
            fact_rows = query_json_each_row(
                f"""
                SELECT
                  symbol,
                  cik,
                  value AS sharesOutstanding,
                  fiscal_year AS fiscalYear,
                  fiscal_period AS fiscalPeriod,
                  period_end AS periodEndDate,
                  filed_at AS filedAt,
                  version_filed_at AS versionFiledAt,
                  raw
                FROM {facts_table}
                WHERE symbol IN {{symbols:Array(String)}}
                  AND metric = 'shares_outstanding'
                  AND value IS NOT NULL
                ORDER BY symbol ASC, version_filed_at DESC, fiscal_year DESC, period_end DESC
                LIMIT 1 BY symbol
                FORMAT JSONEachRow
                """,
                {"symbols": symbols},
            )
        except Exception:
            fact_rows = []
        try:
            ticker_rows = query_json_each_row(
                f"""
                SELECT
                  symbol,
                  cik,
                  company_name AS companyName
                FROM {tickers_table}
                WHERE symbol IN {{symbols:Array(String)}}
                ORDER BY symbol ASC, updated_at DESC
                LIMIT 1 BY symbol
                FORMAT JSONEachRow
                """,
                {"symbols": symbols},
            )
        except Exception:
            ticker_rows = []

        tickers = {
            normalize_market_symbol(row["symbol"]): row
            for row in ticker_rows or []
            if isinstance(row, dict) and isinstance(row.get("symbol"), str)
        }
        records: dict[str, FundamentalsRecord] = {}
        for row in fact_rows or []:
            if not isinstance(row, dict) or not isinstance(row.get("symbol"), str):
                continue
            symbol = normalize_market_symbol(row["symbol"])
            ticker = tickers.get(symbol) or {}
            record = record_from_row({
                "symbol": symbol,
                "cik": row.get("cik") or ticker.get("cik"),
                "companyName": ticker.get("companyName"),
                "sharesOutstanding": row.get("sharesOutstanding"),
                "currency": "USD",
                "fiscalPeriod": row.get("fiscalPeriod"),
                "periodEndDate": row.get("periodEndDate"),
                "filedAt": row.get("filedAt"),
                "source": "sec_companyfacts",
                "asOf": row.get("periodEndDate"),
                "raw": row.get("raw"),
            })
            if record is not None:
                records[symbol] = record
        return records


class CompositeFundamentalsAdapter(FundamentalsAdapter):
    def __init__(self, adapters: list[FundamentalsAdapter]):
        self.adapters = adapters

    def latest_for_symbols(self, symbols: list[str]) -> dict[str, FundamentalsRecord]:
        normalized = normalize_symbols(symbols)
        records: dict[str, FundamentalsRecord] = {}
        for adapter in self.adapters:
            missing = [symbol for symbol in normalized if symbol not in records]
            if not missing:
                break
            records.update(adapter.latest_for_symbols(missing))
        return records


class FileFundamentalsAdapter(FundamentalsAdapter):
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.is_absolute():
            self.path = repo_root() / self.path

    def latest_for_symbols(self, symbols: list[str]) -> dict[str, FundamentalsRecord]:
        records = load_fundamentals_file(str(self.path))
        wanted = set(normalize_symbols(symbols))
        return {symbol: record for symbol, record in records.items() if symbol in wanted}


class HttpFundamentalsAdapter(FundamentalsAdapter):
    def __init__(self, url: str, timeout_seconds: float = 2.0):
        self.url = url
        self.timeout_seconds = timeout_seconds

    def latest_for_symbols(self, symbols: list[str]) -> dict[str, FundamentalsRecord]:
        normalized = normalize_symbols(symbols)
        if not normalized:
            return {}
        separator = "&" if "?" in self.url else "?"
        url = f"{self.url}{separator}{urllib.parse.urlencode({'symbols': ','.join(normalized)})}"
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception:
            return {}
        return records_from_payload(payload, normalized)


def build_fundamentals_adapter(provider=None) -> FundamentalsAdapter:
    source = (os.getenv("FUNDAMENTALS_SOURCE") or "auto").strip().lower()
    url = (os.getenv("FUNDAMENTALS_LATEST_URL") or "").strip()
    path = (os.getenv("FUNDAMENTALS_LATEST_FILE") or os.getenv("HEATMAP_FUNDAMENTALS_PATH") or "").strip()
    if source in {"store", "redis", "clickhouse"}:
        return StoreFundamentalsAdapter(provider=provider)
    if source in {"http", "api"} and url:
        return HttpFundamentalsAdapter(url, timeout_seconds=read_positive_float("FUNDAMENTALS_TIMEOUT_SECONDS", 2.0))
    if source == "file" and path:
        return FileFundamentalsAdapter(path)
    if source == "auto":
        adapters: list[FundamentalsAdapter] = [StoreFundamentalsAdapter(provider=provider)]
        if url:
            adapters.append(HttpFundamentalsAdapter(url, timeout_seconds=read_positive_float("FUNDAMENTALS_TIMEOUT_SECONDS", 2.0)))
        if path:
            adapters.append(FileFundamentalsAdapter(path))
        return CompositeFundamentalsAdapter(adapters)
    return EmptyFundamentalsAdapter()


def normalize_symbols(symbols: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in symbols:
        if not isinstance(value, str):
            continue
        symbol = normalize_market_symbol(value)
        if symbol in seen:
            continue
        seen.add(symbol)
        normalized.append(symbol)
    return normalized


def load_fundamentals_file(path: str) -> dict[str, FundamentalsRecord]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}
    return records_from_payload(payload)


def records_from_payload(payload: Any, symbols: list[str] | None = None) -> dict[str, FundamentalsRecord]:
    wanted = set(normalize_symbols(symbols or []))
    rows = payload_rows(payload)
    records: dict[str, FundamentalsRecord] = {}
    for row in rows:
        record = record_from_row(row)
        if record is None:
            continue
        if wanted and record.symbol not in wanted:
            continue
        records[record.symbol] = record
    return records


def payload_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("items", "fundamentals", "symbols", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    if isinstance(payload.get("symbol") or payload.get("ticker"), str):
        return [payload]
    keyed_rows: list[dict[str, Any]] = []
    for key, value in payload.items():
        if not isinstance(value, dict):
            continue
        row = dict(value)
        row.setdefault("symbol", key)
        keyed_rows.append(row)
    if keyed_rows:
        return keyed_rows
    return []


def record_from_row(row: dict[str, Any]) -> FundamentalsRecord | None:
    raw_symbol = read_string(row.get("symbol") or row.get("ticker"))
    if not raw_symbol:
        return None
    symbol = normalize_market_symbol(raw_symbol)
    shares_outstanding = read_float(row.get("sharesOutstanding") or row.get("shares_outstanding"))
    raw_value = row.get("raw")
    raw = dict(row)
    if isinstance(raw_value, str):
        parsed_raw = decode_json_object(raw_value)
        if parsed_raw:
            raw["raw"] = parsed_raw
    return FundamentalsRecord(
        symbol=symbol,
        cik=read_string(row.get("cik")),
        companyName=read_string(row.get("companyName") or row.get("company_name") or row.get("name")),
        sector=read_string(row.get("sector")),
        industry=read_string(row.get("industry")),
        sharesOutstanding=shares_outstanding if shares_outstanding and shares_outstanding > 0 else None,
        currency=read_string(row.get("currency")),
        fiscalPeriod=read_string(row.get("fiscalPeriod") or row.get("fiscal_period")),
        periodEndDate=read_string(row.get("periodEndDate") or row.get("period_end_date")),
        filedAt=read_string(row.get("filedAt") or row.get("filed_at")),
        source=read_string(row.get("source")),
        asOf=read_string(row.get("asOf") or row.get("as_of")),
        raw=raw,
    )


def record_from_summary_payload(payload: dict[str, Any], *, fallback_symbol: str, source_hint: str | None = None) -> FundamentalsRecord | None:
    symbol = read_string(payload.get("symbol")) or fallback_symbol
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), list) else []
    shares_metric = latest_metric(metrics, "shares_outstanding")
    if not shares_metric:
        return None
    source = read_string(payload.get("source")) or read_string(shares_metric.get("source")) or "sec_companyfacts"
    if source_hint:
        source = f"{source}:{source_hint}"
    return record_from_row({
        "symbol": symbol,
        "cik": payload.get("cik") or shares_metric.get("cik"),
        "companyName": payload.get("companyName") or payload.get("company_name"),
        "sector": payload.get("sector"),
        "industry": payload.get("industry"),
        "sharesOutstanding": shares_metric.get("value"),
        "currency": payload.get("currency") or "USD",
        "fiscalPeriod": shares_metric.get("fiscalPeriod") or payload.get("latest_period"),
        "periodEndDate": shares_metric.get("periodEnd") or shares_metric.get("asOf") or payload.get("as_of"),
        "filedAt": shares_metric.get("filedAt") or payload.get("source_filed_at"),
        "source": source,
        "asOf": payload.get("as_of") or shares_metric.get("asOf") or shares_metric.get("periodEnd"),
        "raw": payload,
    })


def latest_metric(metrics: list[Any], metric_name: str) -> dict[str, Any] | None:
    for metric in metrics:
        if not isinstance(metric, dict):
            continue
        if read_string(metric.get("metric")) == metric_name and read_float(metric.get("value")) is not None:
            return metric
    return None


def fundamentals_summary_key(symbol: str) -> str:
    return f"gops:fundamentals:summary:v1:{normalize_market_symbol(symbol)}"


def decode_json_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        decoded = json.loads(value)
    except Exception:
        return None
    return decoded if isinstance(decoded, dict) else None


def repo_root() -> Path:
    current = Path(__file__).resolve()
    for parent in current.parents:
        if (parent / "systems").exists():
            return parent
        if parent.name == "systems":
            return parent.parent
    return current.parents[-1]


def read_string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def read_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed


def read_positive_float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default
