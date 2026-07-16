from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .backfill import build_clickhouse_client, clickhouse_datetime, ensure_sec_clickhouse_schema, insert_batches, load_universe_symbols, parse_csv, unique_symbols


MARKET_TIMEZONE = ZoneInfo("America/New_York")


@dataclass
class YahooEstimatesConfig:
    dry_run: bool = True
    universe_path: str = "systems/market-data/config/sp500-universe.json"
    symbols: list[str] = field(default_factory=list)
    max_companies: int = 0
    batch_size: int = 500

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "YahooEstimatesConfig":
        environ = environ or os.environ
        return cls(
            dry_run=bool_env(environ.get("YAHOO_ESTIMATES_DRY_RUN"), True),
            universe_path=environ.get("YAHOO_ESTIMATES_UNIVERSE_PATH") or environ.get("ALPACA_UNIVERSE_REGISTRY_PATH") or "systems/market-data/config/sp500-universe.json",
            symbols=parse_csv(environ.get("YAHOO_ESTIMATES_SYMBOLS") or ""),
            max_companies=int(environ.get("YAHOO_ESTIMATES_MAX_COMPANIES") or "0"),
            batch_size=max(1, int(environ.get("YAHOO_ESTIMATES_BATCH_SIZE") or "500")),
        )


@dataclass
class YahooEstimatesStats:
    run_id: str
    dry_run: bool
    symbols_requested: int = 0
    symbols_loaded: int = 0
    rows: int = 0
    errors: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "runId": self.run_id,
            "dryRun": self.dry_run,
            "symbolsRequested": self.symbols_requested,
            "symbolsLoaded": self.symbols_loaded,
            "symbolsSucceeded": self.symbols_loaded,
            "rows": self.rows,
            "errors": self.errors,
        }


def run_yahoo_estimates_sync(config: YahooEstimatesConfig | None = None, *, clickhouse_client=None, fetcher=None) -> YahooEstimatesStats:
    config = config or YahooEstimatesConfig.from_env()
    started_at = datetime.now(timezone.utc)
    stats = YahooEstimatesStats(run_id=f"yahoo-estimates-{started_at.strftime('%Y%m%dT%H%M%SZ')}", dry_run=config.dry_run)
    symbols = unique_symbols(config.symbols or load_universe_symbols(config.universe_path))
    if config.max_companies > 0:
        symbols = symbols[: config.max_companies]
    stats.symbols_requested = len(symbols)
    if not symbols:
        print(json.dumps({"status": "failed", "reason": "empty-universe", **stats.to_dict()}, ensure_ascii=False), flush=True)
        raise RuntimeError("Yahoo estimates universe is empty")
    if config.dry_run:
        print(json.dumps({"status": "dry-run", **stats.to_dict(), "symbols": symbols[:10]}, ensure_ascii=False), flush=True)
        return stats

    clickhouse_client = clickhouse_client or build_clickhouse_client()
    ensure_sec_clickhouse_schema(clickhouse_client)
    fetcher = fetcher or fetch_yfinance_estimate_rows
    batch: list[dict[str, Any]] = []
    for symbol in symbols:
        try:
            rows = fetcher(symbol, collected_at=started_at)
        except Exception as exc:
            message = str(exc).strip()
            stats.errors[symbol] = f"{exc.__class__.__name__}: {message}"[:240] if message else exc.__class__.__name__
            continue
        if not rows:
            continue
        stats.symbols_loaded += 1
        stats.rows += len(rows)
        batch.extend(rows)
        if len(batch) >= config.batch_size:
            insert_batches(clickhouse_client, "yahoo_earnings_estimates", batch, config.batch_size)
            batch = []
    if batch:
        insert_batches(clickhouse_client, "yahoo_earnings_estimates", batch, config.batch_size)
    if stats.rows == 0:
        print(json.dumps({"status": "failed", "reason": "zero-rows", **stats.to_dict()}, ensure_ascii=False, default=str), flush=True)
        raise RuntimeError("Yahoo estimates sync produced zero rows")
    print(json.dumps({"status": "success", **stats.to_dict()}, ensure_ascii=False, default=str), flush=True)
    return stats


def fetch_yfinance_estimate_rows(symbol: str, *, collected_at: datetime) -> list[dict[str, Any]]:
    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError("yfinance is required for Yahoo estimates sync") from exc

    ticker = yf.Ticker(symbol)
    rows: list[dict[str, Any]] = []
    rows.extend(rows_from_estimate_frame(symbol, "eps", safe_call(ticker, "get_earnings_estimate"), collected_at=collected_at))
    rows.extend(rows_from_estimate_frame(symbol, "revenue", safe_call(ticker, "get_revenue_estimate"), collected_at=collected_at))
    rows.extend(rows_from_earnings_dates(symbol, safe_call(ticker, "get_earnings_dates", limit=16), collected_at=collected_at))
    return dedupe_rows(rows)


def safe_call(obj: Any, method_name: str, **kwargs: Any) -> Any:
    method = getattr(obj, method_name, None)
    if not callable(method):
        return None
    try:
        return method(**kwargs)
    except TypeError:
        try:
            return method()
        except Exception:
            return None
    except Exception:
        return None


def rows_from_estimate_frame(symbol: str, metric: str, frame: Any, *, collected_at: datetime) -> list[dict[str, Any]]:
    if frame is None or not hasattr(frame, "iterrows"):
        return []
    rows = []
    for period_label, row in frame.iterrows():
        average = first_number(row, "avg", "average", "Average")
        if average is None:
            continue
        period = period_from_yahoo_label(str(period_label), collected_at.date())
        rows.append(estimate_row(
            symbol=symbol,
            metric=metric,
            period=period,
            average=average,
            low=first_number(row, "low", "Low"),
            high=first_number(row, "high", "High"),
            analyst_count=first_int(row, "numberOfAnalysts", "number_of_analysts", "analystCount", "Analysts"),
            collected_at=collected_at,
            raw={"periodLabel": str(period_label), "sourceFrame": "estimate"},
        ))
    return rows


def rows_from_earnings_dates(symbol: str, frame: Any, *, collected_at: datetime) -> list[dict[str, Any]]:
    if frame is None or not hasattr(frame, "iterrows"):
        return []
    rows = []
    for index, row in frame.iterrows():
        estimate = first_number(row, "EPS Estimate", "epsEstimate", "eps_estimate")
        actual = first_number(row, "Reported EPS", "reportedEPS", "reported_eps")
        surprise_percent = first_number(row, "Surprise(%)", "Surprise (%)", "surprisePercent", "surprise_percent")
        if estimate is None and actual is None:
            continue
        event_at, event_session = earnings_event_datetime(index)
        period_end = event_at.astimezone(MARKET_TIMEZONE).date() if event_at else date_from_any(index) or collected_at.date()
        if surprise_percent is None and estimate not in {None, 0} and actual is not None:
            surprise_percent = ((actual - estimate) / abs(estimate)) * 100
        rows.append(estimate_row(
            symbol=symbol,
            metric="eps",
            period=(period_end.year, "EVENT", period_end),
            average=estimate,
            low=None,
            high=None,
            analyst_count=None,
            collected_at=collected_at,
            event_at=event_at,
            actual_value=actual,
            surprise_percent=surprise_percent,
            event_session=event_session,
            event_status="reported" if actual is not None else "scheduled",
            raw={
                "date": str(index),
                "sourceFrame": "earnings_dates",
                "epsEstimate": estimate,
                "reportedEps": actual,
                "surprisePercent": surprise_percent,
            },
        ))
    return rows


def estimate_row(
    *,
    symbol: str,
    metric: str,
    period: tuple[int, str, date],
    average: float | None,
    low: float | None,
    high: float | None,
    analyst_count: int | None,
    collected_at: datetime,
    raw: dict[str, Any],
    event_at: datetime | None = None,
    actual_value: float | None = None,
    surprise_percent: float | None = None,
    event_session: str = "unknown",
    event_status: str = "scheduled",
) -> dict[str, Any]:
    fiscal_year, fiscal_period, period_end = period
    return {
        "symbol": symbol.strip().upper(),
        "metric": metric,
        "fiscal_year": fiscal_year,
        "fiscal_period": fiscal_period,
        "period_end": period_end.isoformat(),
        "average": average,
        "low": low,
        "high": high,
        "analyst_count": analyst_count,
        "event_at": clickhouse_datetime(event_at) if event_at else None,
        "actual_value": actual_value,
        "surprise_percent": surprise_percent,
        "event_session": event_session,
        "event_status": event_status,
        "source": "yahoo-finance",
        "collected_at": clickhouse_datetime(collected_at),
        "raw": json.dumps(raw, ensure_ascii=False, separators=(",", ":"), default=str),
    }


def period_from_yahoo_label(label: str, today: date) -> tuple[int, str, date]:
    text = label.strip().lower()
    offset = parse_period_offset(text)
    if "y" in text or "year" in text:
        year = today.year + offset
        return year, "FY", date(year, 12, 31)
    quarter_index = ((today.month - 1) // 3) + offset
    year = today.year + quarter_index // 4
    quarter = quarter_index % 4 + 1
    return year, f"Q{quarter}", quarter_end_date(year, quarter)


def period_from_date(value: date) -> tuple[int, str, date]:
    quarter = (value.month - 1) // 3 + 1
    return value.year, f"Q{quarter}", quarter_end_date(value.year, quarter)


def parse_period_offset(label: str) -> int:
    match = re.search(r"([+-]?\d+)", label)
    return int(match.group(1)) if match else 0


def quarter_end_date(year: int, quarter: int) -> date:
    month = quarter * 3
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1).replace(day=1) - date.resolution


def first_number(row: Any, *keys: str) -> float | None:
    for key in keys:
        try:
            value = row.get(key)
        except Exception:
            value = None
        parsed = parse_float(value)
        if parsed is not None:
            return parsed
    return None


def first_int(row: Any, *keys: str) -> int | None:
    value = first_number(row, *keys)
    return int(value) if value is not None else None


def parse_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if numeric == numeric else None


def date_from_any(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text[:10]).date()
    except ValueError:
        return None


def earnings_event_datetime(value: Any) -> tuple[datetime | None, str]:
    raw_value = value
    to_python = getattr(value, "to_pydatetime", None)
    if callable(to_python):
        try:
            raw_value = to_python()
        except Exception:
            raw_value = value

    precise_time = isinstance(raw_value, datetime)
    parsed: datetime | None
    if isinstance(raw_value, datetime):
        parsed = raw_value
    elif isinstance(raw_value, date):
        parsed = datetime(raw_value.year, raw_value.month, raw_value.day)
    else:
        text = str(raw_value or "").strip()
        if not text:
            return None, "unknown"
        precise_time = len(text) > 10
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            parsed_date = date_from_any(text)
            parsed = datetime(parsed_date.year, parsed_date.month, parsed_date.day) if parsed_date else None
            precise_time = False
    if parsed is None:
        return None, "unknown"
    localized = parsed.replace(tzinfo=MARKET_TIMEZONE) if parsed.tzinfo is None else parsed.astimezone(MARKET_TIMEZONE)
    session = earnings_event_session(localized) if precise_time else "unknown"
    return localized.astimezone(timezone.utc), session


def earnings_event_session(localized: datetime) -> str:
    minutes = localized.hour * 60 + localized.minute
    if minutes == 0:
        return "unknown"
    if minutes < 9 * 60 + 30:
        return "pre"
    if minutes >= 16 * 60:
        return "after"
    return "regular"


def dedupe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[tuple[str, str, int, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (
            str(row.get("symbol")),
            str(row.get("metric")),
            int(row.get("fiscal_year") or 0),
            str(row.get("fiscal_period")),
            str(row.get("period_end")),
        )
        deduped[key] = row
    return list(deduped.values())


def bool_env(value: Any, default: bool) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
