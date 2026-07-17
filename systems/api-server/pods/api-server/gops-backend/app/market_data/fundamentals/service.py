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
class EarningsSeriesPoint:
    period: str
    periodEndDate: str | None = None
    actualEps: float | None = None
    estimatedEps: float | None = None
    actualRevenue: float | None = None
    estimatedRevenue: float | None = None
    source: str | None = None
    estimateSource: str | None = None
    filedAt: str | None = None
    collectedAt: str | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "periodEndDate": self.periodEndDate,
            "actualEps": self.actualEps,
            "estimatedEps": self.estimatedEps,
            "actualRevenue": self.actualRevenue,
            "estimatedRevenue": self.estimatedRevenue,
            "source": self.source,
            "estimateSource": self.estimateSource,
            "filedAt": self.filedAt,
            "collectedAt": self.collectedAt,
        }


@dataclass(frozen=True)
class FinancialSeriesPoint:
    period: str
    periodEndDate: str | None = None
    revenue: float | None = None
    operatingIncome: float | None = None
    netIncome: float | None = None
    eps: float | None = None
    totalAssets: float | None = None
    totalLiabilities: float | None = None
    totalEquity: float | None = None
    currentAssets: float | None = None
    currentLiabilities: float | None = None
    cashAndCashEquivalents: float | None = None
    interestExpense: float | None = None
    operatingCashFlow: float | None = None
    freeCashFlow: float | None = None
    sharesOutstanding: float | None = None
    debtRatio: float | None = None
    currentLiabilityRatio: float | None = None
    noncurrentLiabilityRatio: float | None = None
    currentRatio: float | None = None
    totalDebt: float | None = None
    interestCoverage: float | None = None
    financialCostBurdenRatio: float | None = None
    netDebt: float | None = None
    source: str | None = None
    filedAt: str | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "periodEndDate": self.periodEndDate,
            "revenue": self.revenue,
            "operatingIncome": self.operatingIncome,
            "netIncome": self.netIncome,
            "eps": self.eps,
            "totalAssets": self.totalAssets,
            "totalLiabilities": self.totalLiabilities,
            "totalEquity": self.totalEquity,
            "currentAssets": self.currentAssets,
            "currentLiabilities": self.currentLiabilities,
            "cashAndCashEquivalents": self.cashAndCashEquivalents,
            "interestExpense": self.interestExpense,
            "operatingCashFlow": self.operatingCashFlow,
            "freeCashFlow": self.freeCashFlow,
            "sharesOutstanding": self.sharesOutstanding,
            "debtRatio": self.debtRatio,
            "currentLiabilityRatio": self.currentLiabilityRatio,
            "noncurrentLiabilityRatio": self.noncurrentLiabilityRatio,
            "currentRatio": self.currentRatio,
            "totalDebt": self.totalDebt,
            "interestCoverage": self.interestCoverage,
            "financialCostBurdenRatio": self.financialCostBurdenRatio,
            "netDebt": self.netDebt,
            "source": self.source,
            "filedAt": self.filedAt,
        }


@dataclass(frozen=True)
class FundamentalsRecord:
    symbol: str
    cik: str | None = None
    companyName: str | None = None
    sector: str | None = None
    industry: str | None = None
    sharesOutstanding: float | None = None
    revenue: float | None = None
    operatingIncome: float | None = None
    netIncome: float | None = None
    eps: float | None = None
    totalAssets: float | None = None
    totalLiabilities: float | None = None
    totalEquity: float | None = None
    operatingCashFlow: float | None = None
    freeCashFlow: float | None = None
    currency: str | None = None
    fiscalPeriod: str | None = None
    periodEndDate: str | None = None
    filedAt: str | None = None
    source: str | None = None
    asOf: str | None = None
    earningsSeries: list[EarningsSeriesPoint] | None = None
    raw: dict[str, Any] | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "cik": self.cik,
            "companyName": self.companyName,
            "sector": self.sector,
            "industry": self.industry,
            "sharesOutstanding": self.sharesOutstanding,
            "revenue": self.revenue,
            "operatingIncome": self.operatingIncome,
            "netIncome": self.netIncome,
            "eps": self.eps,
            "totalAssets": self.totalAssets,
            "totalLiabilities": self.totalLiabilities,
            "totalEquity": self.totalEquity,
            "operatingCashFlow": self.operatingCashFlow,
            "freeCashFlow": self.freeCashFlow,
            "currency": self.currency,
            "fiscalPeriod": self.fiscalPeriod,
            "periodEndDate": self.periodEndDate,
            "filedAt": self.filedAt,
            "source": self.source,
            "asOf": self.asOf,
            "earningsSeries": [point.to_public_dict() for point in self.earningsSeries or []],
        }


class FundamentalsAdapter:
    def latest_for_symbols(self, symbols: list[str]) -> dict[str, FundamentalsRecord]:
        raise NotImplementedError

    def financial_series(self, symbol: str, *, years: int = 3, period: str = "quarterly") -> list[FinancialSeriesPoint]:
        return []

    def earnings_series(self, symbol: str, *, years: int = 3) -> list[EarningsSeriesPoint]:
        return []


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
        needs_clickhouse = [
            symbol
            for symbol in normalized
            if symbol not in records or record_needs_clickhouse_supplement(records[symbol])
        ]
        if needs_clickhouse:
            clickhouse_records = self._from_clickhouse(needs_clickhouse)
            for symbol, record in clickhouse_records.items():
                records[symbol] = merge_fundamentals_records(records.get(symbol), record)
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

    def _series_from_clickhouse(self, symbols: list[str], *, years: int = 3) -> dict[str, list[EarningsSeriesPoint]]:
        provider = self._clickhouse()
        query_json_each_row = getattr(provider, "query_json_each_row", None)
        if not callable(query_json_each_row):
            return {}
        facts_table = provider.table("sec_financial_facts") if hasattr(provider, "table") else "market_data.sec_financial_facts"
        estimates_table = provider.table("yahoo_earnings_estimates") if hasattr(provider, "table") else "market_data.yahoo_earnings_estimates"
        months = max(18, min(120, int(years) * 12 + 6))
        try:
            actual_rows = query_json_each_row(
                f"""
                SELECT
                  symbol,
                  metric,
                  value,
                  fiscal_year AS fiscalYear,
                  fiscal_period AS fiscalPeriod,
                  period_end AS periodEndDate,
                  filed_at AS filedAt,
                  version_filed_at AS versionFiledAt
                FROM {facts_table}
                WHERE symbol IN {{symbols:Array(String)}}
                  AND metric IN {{metrics:Array(String)}}
                  AND value IS NOT NULL
                  AND fiscal_period IN ('Q1', 'Q2', 'Q3', 'Q4')
                  AND period_end >= addMonths(today(), -{{months:UInt16}})
                ORDER BY symbol ASC, metric ASC, period_end DESC, version_filed_at DESC
                LIMIT 1 BY symbol, metric, fiscal_year, fiscal_period
                FORMAT JSONEachRow
                """,
                {"symbols": symbols, "metrics": list(EARNINGS_SERIES_METRICS), "months": months},
            )
        except Exception:
            actual_rows = []
        try:
            estimate_rows = query_json_each_row(
                f"""
                SELECT
                  symbol,
                  metric,
                  average AS value,
                  low,
                  high,
                  analyst_count AS analystCount,
                  fiscal_year AS fiscalYear,
                  fiscal_period AS fiscalPeriod,
                  period_end AS periodEndDate,
                  collected_at AS collectedAt,
                  raw
                FROM {estimates_table}
                WHERE symbol IN {{symbols:Array(String)}}
                  AND metric IN {{metrics:Array(String)}}
                  AND average IS NOT NULL
                  AND fiscal_period IN ('Q1', 'Q2', 'Q3', 'Q4')
                  AND period_end >= addMonths(today(), -{{months:UInt16}})
                ORDER BY symbol ASC, metric ASC, period_end DESC, collected_at DESC
                LIMIT 1 BY symbol, metric, fiscal_year, fiscal_period
                FORMAT JSONEachRow
                """,
                {"symbols": symbols, "metrics": list(EARNINGS_SERIES_METRICS), "months": months},
            )
        except Exception:
            estimate_rows = []
        return earnings_series_from_rows(actual_rows or [], estimate_rows or [])

    def earnings_series(self, symbol: str, *, years: int = 3) -> list[EarningsSeriesPoint]:
        normalized = normalize_market_symbol(symbol)
        return self._series_from_clickhouse([normalized], years=years).get(normalized, [])

    def financial_series(self, symbol: str, *, years: int = 3, period: str = "quarterly") -> list[FinancialSeriesPoint]:
        normalized = normalize_market_symbol(symbol)
        provider = self._clickhouse()
        query_json_each_row = getattr(provider, "query_json_each_row", None)
        if not callable(query_json_each_row):
            return []
        facts_table = provider.table("sec_financial_facts") if hasattr(provider, "table") else "market_data.sec_financial_facts"
        derived_table = provider.table("sec_derived_metrics") if hasattr(provider, "table") else "market_data.sec_derived_metrics"
        fiscal_periods = ("FY",) if period == "annual" else ("Q1", "Q2", "Q3", "Q4")
        months = max(12, min(120, int(years) * 12 + 6))
        try:
            fact_rows = query_json_each_row(
                f"""
                SELECT
                  symbol,
                  cik,
                  metric,
                  value,
                  fiscal_year AS fiscalYear,
                  fiscal_period AS fiscalPeriod,
                  period_end AS periodEndDate,
                  filed_at AS filedAt,
                  version_filed_at AS versionFiledAt
                FROM {facts_table}
                WHERE symbol = {{symbol:String}}
                  AND metric IN {{metrics:Array(String)}}
                  AND fiscal_period IN {{fiscalPeriods:Array(String)}}
                  AND value IS NOT NULL
                  AND period_end >= addMonths(today(), -{{months:UInt16}})
                ORDER BY metric ASC, period_end DESC, version_filed_at DESC
                LIMIT 1 BY metric, fiscal_year, fiscal_period
                FORMAT JSONEachRow
                """,
                {
                    "symbol": normalized,
                    "metrics": list(FINANCIAL_SERIES_METRICS),
                    "fiscalPeriods": list(fiscal_periods),
                    "months": months,
                },
            )
        except Exception:
            fact_rows = []
        try:
            derived_rows = query_json_each_row(
                f"""
                SELECT
                  symbol,
                  metric,
                  value,
                  fiscal_year AS fiscalYear,
                  fiscal_period AS fiscalPeriod,
                  period_end AS periodEndDate,
                  filed_at AS filedAt,
                  version_filed_at AS versionFiledAt
                FROM {derived_table}
                WHERE symbol = {{symbol:String}}
                  AND metric IN {{metrics:Array(String)}}
                  AND fiscal_period IN {{fiscalPeriods:Array(String)}}
                  AND value IS NOT NULL
                  AND period_end >= addMonths(today(), -{{months:UInt16}})
                ORDER BY metric ASC, period_end DESC, version_filed_at DESC
                LIMIT 1 BY metric, fiscal_year, fiscal_period
                FORMAT JSONEachRow
                """,
                {
                    "symbol": normalized,
                    "metrics": list(FINANCIAL_DERIVED_SERIES_METRICS),
                    "fiscalPeriods": list(fiscal_periods),
                    "months": months,
                },
            )
        except Exception:
            derived_rows = []
        return financial_series_from_rows([*(fact_rows or []), *(derived_rows or [])]).get(normalized, [])

    def _from_clickhouse(self, symbols: list[str]) -> dict[str, FundamentalsRecord]:
        provider = self._clickhouse()
        query_json_each_row = getattr(provider, "query_json_each_row", None)
        if not callable(query_json_each_row):
            return {}
        facts_table = provider.table("sec_financial_facts") if hasattr(provider, "table") else "market_data.sec_financial_facts"
        derived_table = provider.table("sec_derived_metrics") if hasattr(provider, "table") else "market_data.sec_derived_metrics"
        tickers_table = provider.table("sec_company_tickers") if hasattr(provider, "table") else "market_data.sec_company_tickers"
        try:
            fact_rows = query_json_each_row(
                f"""
                SELECT
                  symbol,
                  cik,
                  metric,
                  value,
                  fiscal_year AS fiscalYear,
                  fiscal_period AS fiscalPeriod,
                  period_end AS periodEndDate,
                  filed_at AS filedAt,
                  version_filed_at AS versionFiledAt,
                  raw
                FROM {facts_table}
                WHERE symbol IN {{symbols:Array(String)}}
                  AND metric IN {{metrics:Array(String)}}
                  AND value IS NOT NULL
                ORDER BY symbol ASC, metric ASC, version_filed_at DESC, fiscal_year DESC, period_end DESC
                LIMIT 1 BY symbol, metric
                FORMAT JSONEachRow
                """,
                {"symbols": symbols, "metrics": list(FUNDAMENTAL_FACT_METRICS)},
            )
        except Exception:
            fact_rows = []
        try:
            derived_rows = query_json_each_row(
                f"""
                SELECT
                  symbol,
                  metric,
                  value,
                  fiscal_year AS fiscalYear,
                  fiscal_period AS fiscalPeriod,
                  period_end AS periodEndDate,
                  filed_at AS filedAt,
                  version_filed_at AS versionFiledAt,
                  raw
                FROM {derived_table}
                WHERE symbol IN {{symbols:Array(String)}}
                  AND metric IN {{metrics:Array(String)}}
                  AND value IS NOT NULL
                ORDER BY symbol ASC, metric ASC, version_filed_at DESC, fiscal_year DESC, period_end DESC
                LIMIT 1 BY symbol, metric
                FORMAT JSONEachRow
                """,
                {"symbols": symbols, "metrics": list(FUNDAMENTAL_DERIVED_METRICS)},
            )
        except Exception:
            derived_rows = []
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
        metrics_by_symbol = group_metric_rows([*(fact_rows or []), *(derived_rows or [])])
        for symbol, metric_rows in metrics_by_symbol.items():
            ticker = tickers.get(symbol) or {}
            anchor = metric_rows.get("shares_outstanding") or first_metric_row(metric_rows)
            record = record_from_row({
                "symbol": symbol,
                "cik": (anchor or {}).get("cik") or ticker.get("cik"),
                "companyName": ticker.get("companyName"),
                **record_values_from_metric_rows(metric_rows),
                "currency": "USD",
                "fiscalPeriod": (anchor or {}).get("fiscalPeriod"),
                "periodEndDate": (anchor or {}).get("periodEndDate"),
                "filedAt": (anchor or {}).get("filedAt"),
                "source": "sec_companyfacts",
                "asOf": (anchor or {}).get("periodEndDate"),
                "raw": {"metrics": list(metric_rows.values())},
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

    def financial_series(self, symbol: str, *, years: int = 3, period: str = "quarterly") -> list[FinancialSeriesPoint]:
        for adapter in self.adapters:
            series = adapter.financial_series(symbol, years=years, period=period)
            if series:
                return series
        return []

    def earnings_series(self, symbol: str, *, years: int = 3) -> list[EarningsSeriesPoint]:
        for adapter in self.adapters:
            series = adapter.earnings_series(symbol, years=years)
            if series:
                return series
        return []


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


FUNDAMENTAL_FACT_METRICS = (
    "shares_outstanding",
    "revenue",
    "operating_income",
    "net_income",
    "eps",
    "assets",
    "liabilities",
    "equity",
    "operating_cash_flow",
)

FUNDAMENTAL_DERIVED_METRICS = (
    "free_cash_flow",
)

EARNINGS_SERIES_METRICS = (
    "eps",
    "revenue",
)

FINANCIAL_SERIES_METRICS = (
    "shares_outstanding",
    "revenue",
    "operating_income",
    "net_income",
    "eps",
    "assets",
    "liabilities",
    "equity",
    "current_assets",
    "current_liabilities",
    "cash_and_cash_equivalents",
    "interest_expense",
    "operating_cash_flow",
)

FINANCIAL_DERIVED_SERIES_METRICS = (
    "free_cash_flow",
    "liabilities_to_equity",
    "current_liabilities_to_equity",
    "noncurrent_liabilities_to_equity",
    "current_ratio",
    "total_debt",
    "interest_coverage",
    "financial_cost_burden_ratio",
    "net_debt",
)

METRIC_FIELD_MAP = {
    "shares_outstanding": "sharesOutstanding",
    "revenue": "revenue",
    "operating_income": "operatingIncome",
    "net_income": "netIncome",
    "eps": "eps",
    "assets": "totalAssets",
    "liabilities": "totalLiabilities",
    "equity": "totalEquity",
    "current_assets": "currentAssets",
    "current_liabilities": "currentLiabilities",
    "cash_and_cash_equivalents": "cashAndCashEquivalents",
    "interest_expense": "interestExpense",
    "operating_cash_flow": "operatingCashFlow",
    "free_cash_flow": "freeCashFlow",
    "liabilities_to_equity": "debtRatio",
    "current_liabilities_to_equity": "currentLiabilityRatio",
    "noncurrent_liabilities_to_equity": "noncurrentLiabilityRatio",
    "current_ratio": "currentRatio",
    "total_debt": "totalDebt",
    "interest_coverage": "interestCoverage",
    "financial_cost_burden_ratio": "financialCostBurdenRatio",
    "net_debt": "netDebt",
}

SUPPLEMENTAL_FUNDAMENTAL_FIELDS = (
    "sharesOutstanding",
    "revenue",
    "eps",
    "totalEquity",
    "freeCashFlow",
)


def record_needs_clickhouse_supplement(record: FundamentalsRecord) -> bool:
    return any(getattr(record, field) is None for field in SUPPLEMENTAL_FUNDAMENTAL_FIELDS)


def merge_fundamentals_records(primary: FundamentalsRecord | None, supplement: FundamentalsRecord) -> FundamentalsRecord:
    if primary is None:
        return supplement
    merged_source = primary.source
    if supplement.source and supplement.source not in {primary.source, None}:
        merged_source = f"{primary.source or 'unknown'}+{supplement.source}"
    return FundamentalsRecord(
        symbol=primary.symbol or supplement.symbol,
        cik=primary.cik or supplement.cik,
        companyName=primary.companyName or supplement.companyName,
        sector=primary.sector or supplement.sector,
        industry=primary.industry or supplement.industry,
        sharesOutstanding=primary.sharesOutstanding if primary.sharesOutstanding is not None else supplement.sharesOutstanding,
        revenue=primary.revenue if primary.revenue is not None else supplement.revenue,
        operatingIncome=primary.operatingIncome if primary.operatingIncome is not None else supplement.operatingIncome,
        netIncome=primary.netIncome if primary.netIncome is not None else supplement.netIncome,
        eps=primary.eps if primary.eps is not None else supplement.eps,
        totalAssets=primary.totalAssets if primary.totalAssets is not None else supplement.totalAssets,
        totalLiabilities=primary.totalLiabilities if primary.totalLiabilities is not None else supplement.totalLiabilities,
        totalEquity=primary.totalEquity if primary.totalEquity is not None else supplement.totalEquity,
        operatingCashFlow=primary.operatingCashFlow if primary.operatingCashFlow is not None else supplement.operatingCashFlow,
        freeCashFlow=primary.freeCashFlow if primary.freeCashFlow is not None else supplement.freeCashFlow,
        currency=primary.currency or supplement.currency,
        fiscalPeriod=primary.fiscalPeriod or supplement.fiscalPeriod,
        periodEndDate=primary.periodEndDate or supplement.periodEndDate,
        filedAt=primary.filedAt or supplement.filedAt,
        source=merged_source,
        asOf=primary.asOf or supplement.asOf,
        earningsSeries=primary.earningsSeries or supplement.earningsSeries,
        raw=primary.raw or supplement.raw,
    )


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
        revenue=read_float(row.get("revenue")),
        operatingIncome=read_float(row.get("operatingIncome") or row.get("operating_income")),
        netIncome=read_float(row.get("netIncome") or row.get("net_income")),
        eps=read_float(row.get("eps")),
        totalAssets=read_float(row.get("totalAssets") or row.get("total_assets") or row.get("assets")),
        totalLiabilities=read_float(row.get("totalLiabilities") or row.get("total_liabilities") or row.get("liabilities")),
        totalEquity=read_float(row.get("totalEquity") or row.get("total_equity") or row.get("equity")),
        operatingCashFlow=read_float(row.get("operatingCashFlow") or row.get("operating_cash_flow")),
        freeCashFlow=read_float(row.get("freeCashFlow") or row.get("free_cash_flow")),
        currency=read_string(row.get("currency")),
        fiscalPeriod=read_string(row.get("fiscalPeriod") or row.get("fiscal_period")),
        periodEndDate=read_string(row.get("periodEndDate") or row.get("period_end_date")),
        filedAt=read_string(row.get("filedAt") or row.get("filed_at")),
        source=read_string(row.get("source")),
        asOf=read_string(row.get("asOf") or row.get("as_of")),
        earningsSeries=parse_earnings_series(row.get("earningsSeries") or row.get("earnings_series")),
        raw=raw,
    )


def record_from_summary_payload(payload: dict[str, Any], *, fallback_symbol: str, source_hint: str | None = None) -> FundamentalsRecord | None:
    symbol = read_string(payload.get("symbol")) or fallback_symbol
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), list) else []
    shares_metric = latest_metric(metrics, "shares_outstanding")
    metric_values = values_from_summary_metrics(metrics)
    if not metric_values:
        return None
    anchor_metric = shares_metric or first_summary_metric(metrics)
    source = read_string(payload.get("source")) or read_string((anchor_metric or {}).get("source")) or "sec_companyfacts"
    if source_hint:
        source = f"{source}:{source_hint}"
    return record_from_row({
        "symbol": symbol,
        "cik": payload.get("cik") or (anchor_metric or {}).get("cik"),
        "companyName": payload.get("companyName") or payload.get("company_name"),
        "sector": payload.get("sector"),
        "industry": payload.get("industry"),
        **metric_values,
        "currency": payload.get("currency") or "USD",
        "fiscalPeriod": (anchor_metric or {}).get("fiscalPeriod") or payload.get("latest_period"),
        "periodEndDate": (anchor_metric or {}).get("periodEnd") or (anchor_metric or {}).get("asOf") or payload.get("as_of"),
        "filedAt": (anchor_metric or {}).get("filedAt") or payload.get("source_filed_at"),
        "source": source,
        "asOf": payload.get("as_of") or (anchor_metric or {}).get("asOf") or (anchor_metric or {}).get("periodEnd"),
        "raw": payload,
    })


def values_from_summary_metrics(metrics: list[Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for metric_name, field_name in METRIC_FIELD_MAP.items():
        metric = latest_metric(metrics, metric_name)
        if metric:
            values[field_name] = metric.get("value")
    return values


def first_summary_metric(metrics: list[Any]) -> dict[str, Any] | None:
    for metric in metrics:
        if isinstance(metric, dict) and read_float(metric.get("value")) is not None:
            return metric
    return None


def latest_metric(metrics: list[Any], metric_name: str) -> dict[str, Any] | None:
    for metric in metrics:
        if not isinstance(metric, dict):
            continue
        if read_string(metric.get("metric")) == metric_name and read_float(metric.get("value")) is not None:
            return metric
    return None


def group_metric_rows(rows: list[Any]) -> dict[str, dict[str, dict[str, Any]]]:
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("symbol"), str):
            continue
        metric = read_string(row.get("metric"))
        value = read_float(row.get("value"))
        if not metric or value is None:
            continue
        symbol = normalize_market_symbol(row["symbol"])
        grouped.setdefault(symbol, {})
        grouped[symbol].setdefault(metric, row)
    return grouped


def record_values_from_metric_rows(metric_rows: dict[str, dict[str, Any]]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for metric_name, field_name in METRIC_FIELD_MAP.items():
        row = metric_rows.get(metric_name)
        if row:
            values[field_name] = row.get("value")
    return values


def first_metric_row(metric_rows: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    for row in metric_rows.values():
        return row
    return None


def earnings_series_from_rows(actual_rows: list[Any], estimate_rows: list[Any]) -> dict[str, list[EarningsSeriesPoint]]:
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for row in actual_rows:
        if not isinstance(row, dict):
            continue
        symbol = read_string(row.get("symbol"))
        metric = read_string(row.get("metric"))
        value = read_float(row.get("value"))
        if not symbol or metric not in EARNINGS_SERIES_METRICS or value is None:
            continue
        normalized = normalize_market_symbol(symbol)
        period_key = period_key_from_row(row)
        point = grouped.setdefault(normalized, {}).setdefault(period_key, {
            "period": period_label_from_row(row),
            "periodEndDate": read_string(row.get("periodEndDate")),
            "source": "sec",
            "filedAt": read_string(row.get("filedAt")),
        })
        if metric == "eps" and "actualEps" not in point:
            point["actualEps"] = value
        elif metric == "revenue" and "actualRevenue" not in point:
            point["actualRevenue"] = value

    for row in estimate_rows:
        if not isinstance(row, dict):
            continue
        symbol = read_string(row.get("symbol"))
        metric = read_string(row.get("metric"))
        value = read_float(row.get("value") or row.get("average") or row.get("avg"))
        if not symbol or metric not in EARNINGS_SERIES_METRICS or value is None:
            continue
        normalized = normalize_market_symbol(symbol)
        period_key = period_key_from_row(row)
        point = grouped.setdefault(normalized, {}).setdefault(period_key, {
            "period": period_label_from_row(row),
            "periodEndDate": read_string(row.get("periodEndDate")),
        })
        point.setdefault("period", period_label_from_row(row))
        point.setdefault("periodEndDate", read_string(row.get("periodEndDate")))
        point["estimateSource"] = "yahoo"
        point["collectedAt"] = read_string(row.get("collectedAt"))
        if metric == "eps" and "estimatedEps" not in point:
            point["estimatedEps"] = value
        elif metric == "revenue" and "estimatedRevenue" not in point:
            point["estimatedRevenue"] = value

    return {
        symbol: [
            point for point in (
                earnings_series_point_from_row(row)
                for row in sorted(points.values(), key=series_sort_key)
            )
            if point is not None
        ]
        for symbol, points in grouped.items()
    }


def financial_series_from_rows(rows: list[Any]) -> dict[str, list[FinancialSeriesPoint]]:
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = read_string(row.get("symbol"))
        metric = read_string(row.get("metric"))
        value = read_float(row.get("value"))
        field = METRIC_FIELD_MAP.get(metric or "")
        if not symbol or not field or value is None:
            continue
        normalized = normalize_market_symbol(symbol)
        period_key = period_key_from_row(row)
        point = grouped.setdefault(normalized, {}).setdefault(period_key, {
            "period": period_label_from_row(row),
            "periodEndDate": read_string(row.get("periodEndDate")),
            "source": "sec",
            "filedAt": read_string(row.get("filedAt")),
        })
        point.setdefault(field, value)
    return {
        symbol: [
            point for point in (
                financial_series_point_from_row(row)
                for row in sorted(points.values(), key=series_sort_key)
            )
            if point is not None
        ]
        for symbol, points in grouped.items()
    }


def financial_series_point_from_row(row: dict[str, Any]) -> FinancialSeriesPoint | None:
    period = read_string(row.get("period")) or period_label_from_row(row)
    if not period:
        return None
    return FinancialSeriesPoint(
        period=period,
        periodEndDate=read_string(row.get("periodEndDate") or row.get("period_end_date")),
        revenue=read_float(row.get("revenue")),
        operatingIncome=read_float(row.get("operatingIncome") or row.get("operating_income")),
        netIncome=read_float(row.get("netIncome") or row.get("net_income")),
        eps=read_float(row.get("eps")),
        totalAssets=read_float(row.get("totalAssets") or row.get("total_assets") or row.get("assets")),
        totalLiabilities=read_float(row.get("totalLiabilities") or row.get("total_liabilities") or row.get("liabilities")),
        totalEquity=read_float(row.get("totalEquity") or row.get("total_equity") or row.get("equity")),
        currentAssets=read_float(first_present(row, "currentAssets", "current_assets")),
        currentLiabilities=read_float(first_present(row, "currentLiabilities", "current_liabilities")),
        cashAndCashEquivalents=read_float(first_present(row, "cashAndCashEquivalents", "cash_and_cash_equivalents")),
        interestExpense=read_float(first_present(row, "interestExpense", "interest_expense")),
        operatingCashFlow=read_float(row.get("operatingCashFlow") or row.get("operating_cash_flow")),
        freeCashFlow=read_float(row.get("freeCashFlow") or row.get("free_cash_flow")),
        sharesOutstanding=read_float(row.get("sharesOutstanding") or row.get("shares_outstanding")),
        debtRatio=read_float(first_present(row, "debtRatio", "liabilities_to_equity")),
        currentLiabilityRatio=read_float(first_present(row, "currentLiabilityRatio", "current_liabilities_to_equity")),
        noncurrentLiabilityRatio=read_float(first_present(row, "noncurrentLiabilityRatio", "noncurrent_liabilities_to_equity")),
        currentRatio=read_float(first_present(row, "currentRatio", "current_ratio")),
        totalDebt=read_float(first_present(row, "totalDebt", "total_debt")),
        interestCoverage=read_float(first_present(row, "interestCoverage", "interest_coverage")),
        financialCostBurdenRatio=read_float(first_present(row, "financialCostBurdenRatio", "financial_cost_burden_ratio")),
        netDebt=read_float(first_present(row, "netDebt", "net_debt")),
        source=read_string(row.get("source")),
        filedAt=read_string(row.get("filedAt") or row.get("filed_at")),
    )


def parse_earnings_series(value: Any) -> list[EarningsSeriesPoint] | None:
    if not isinstance(value, list):
        return None
    points = [earnings_series_point_from_row(row) for row in value if isinstance(row, dict)]
    parsed = [point for point in points if point is not None]
    return parsed or None


def earnings_series_point_from_row(row: dict[str, Any]) -> EarningsSeriesPoint | None:
    period = read_string(row.get("period")) or period_label_from_row(row)
    if not period:
        return None
    return EarningsSeriesPoint(
        period=period,
        periodEndDate=read_string(row.get("periodEndDate") or row.get("period_end_date")),
        actualEps=read_float(row.get("actualEps") or row.get("actual_eps")),
        estimatedEps=read_float(row.get("estimatedEps") or row.get("estimated_eps")),
        actualRevenue=read_float(row.get("actualRevenue") or row.get("actual_revenue")),
        estimatedRevenue=read_float(row.get("estimatedRevenue") or row.get("estimated_revenue")),
        source=read_string(row.get("source")),
        estimateSource=read_string(row.get("estimateSource") or row.get("estimate_source")),
        filedAt=read_string(row.get("filedAt") or row.get("filed_at")),
        collectedAt=read_string(row.get("collectedAt") or row.get("collected_at")),
    )


def period_key_from_row(row: dict[str, Any]) -> str:
    year = read_text(row.get("fiscalYear") or row.get("fiscal_year"))
    period = read_string(row.get("fiscalPeriod") or row.get("fiscal_period"))
    period_end = read_string(row.get("periodEndDate") or row.get("period_end_date"))
    if year and period:
        return f"{year}{period}"
    if period_end:
        return period_end
    return read_string(row.get("period")) or "unknown"


def period_label_from_row(row: dict[str, Any]) -> str:
    period = read_string(row.get("period"))
    if period:
        return period
    year = read_text(row.get("fiscalYear") or row.get("fiscal_year"))
    fiscal_period = read_string(row.get("fiscalPeriod") or row.get("fiscal_period"))
    if year and fiscal_period:
        return f"{year}{fiscal_period}"
    return read_string(row.get("periodEndDate") or row.get("period_end_date")) or ""


def series_sort_key(row: dict[str, Any]) -> tuple[str, str]:
    return (
        read_string(row.get("periodEndDate")) or read_string(row.get("period")) or "",
        read_string(row.get("period")) or "",
    )


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


def read_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, int):
        return str(value)
    return None


def read_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed


def first_present(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return None


def read_positive_float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default
