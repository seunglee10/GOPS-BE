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


def yahoo_provider_symbol(symbol: str) -> str:
    """Translate GOPS class-share symbols to Yahoo Finance's ticker format."""
    return symbol.strip().upper().replace(".", "-")


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
    analyst_symbols_loaded: int = 0
    analyst_summary_rows: int = 0
    errors: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "runId": self.run_id,
            "dryRun": self.dry_run,
            "symbolsRequested": self.symbols_requested,
            "symbolsLoaded": self.symbols_loaded,
            "symbolsSucceeded": self.symbols_loaded,
            "rows": self.rows,
            "analystSymbolsLoaded": self.analyst_symbols_loaded,
            "analystSummaryRows": self.analyst_summary_rows,
            "errors": self.errors,
        }


def run_yahoo_estimates_sync(
    config: YahooEstimatesConfig | None = None,
    *,
    clickhouse_client=None,
    fetcher=None,
    analyst_fetcher=None,
) -> YahooEstimatesStats:
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
    analyst_fetcher = analyst_fetcher or fetch_yfinance_analyst_summary
    batch: list[dict[str, Any]] = []
    analyst_summary_batch: list[dict[str, Any]] = []
    for symbol in symbols:
        try:
            rows = fetcher(symbol, collected_at=started_at)
        except Exception as exc:
            message = str(exc).strip()
            append_sync_error(stats, symbol, "estimates", exc.__class__.__name__, message)
            rows = []
        if rows:
            stats.symbols_loaded += 1
            stats.rows += len(rows)
            batch.extend(rows)
            if len(batch) >= config.batch_size:
                insert_batches(clickhouse_client, "yahoo_earnings_estimates", batch, config.batch_size)
                batch = []
        try:
            analyst_summary = analyst_fetcher(symbol, collected_at=started_at)
        except Exception as exc:
            message = str(exc).strip()
            append_sync_error(stats, symbol, "analysts", exc.__class__.__name__, message)
            analyst_summary = None
        if analyst_summary:
            stats.analyst_symbols_loaded += 1
            stats.analyst_summary_rows += 1
            analyst_summary_batch.append(analyst_summary)
        if len(analyst_summary_batch) >= config.batch_size:
            insert_batches(clickhouse_client, "yahoo_analyst_summaries", analyst_summary_batch, config.batch_size)
            analyst_summary_batch = []
    if batch:
        insert_batches(clickhouse_client, "yahoo_earnings_estimates", batch, config.batch_size)
    if analyst_summary_batch:
        insert_batches(clickhouse_client, "yahoo_analyst_summaries", analyst_summary_batch, config.batch_size)
    if stats.rows == 0:
        print(json.dumps({"status": "failed", "reason": "zero-rows", **stats.to_dict()}, ensure_ascii=False, default=str), flush=True)
        raise RuntimeError("Yahoo estimates sync produced zero rows")
    finalize_analyst_summary_storage(clickhouse_client)
    print(json.dumps({"status": "success", **stats.to_dict()}, ensure_ascii=False, default=str), flush=True)
    return stats


def fetch_yfinance_estimate_rows(symbol: str, *, collected_at: datetime) -> list[dict[str, Any]]:
    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError("yfinance is required for Yahoo estimates sync") from exc

    ticker = yf.Ticker(yahoo_provider_symbol(symbol))
    rows: list[dict[str, Any]] = []
    rows.extend(rows_from_estimate_frame(symbol, "eps", safe_call(ticker, "get_earnings_estimate"), collected_at=collected_at))
    rows.extend(rows_from_estimate_frame(symbol, "revenue", safe_call(ticker, "get_revenue_estimate"), collected_at=collected_at))
    rows.extend(rows_from_earnings_dates(symbol, safe_call(ticker, "get_earnings_dates", limit=16), collected_at=collected_at))
    return dedupe_rows(rows)


def fetch_yfinance_analyst_summary(
    symbol: str,
    *,
    collected_at: datetime,
) -> dict[str, Any] | None:
    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError("yfinance is required for Yahoo analyst sync") from exc

    ticker = yf.Ticker(yahoo_provider_symbol(symbol))
    actions = rows_from_upgrades_downgrades(
        symbol,
        safe_call(ticker, "get_upgrades_downgrades"),
        collected_at=collected_at,
    )
    consensus = rows_from_analyst_consensus(
        symbol,
        safe_call(ticker, "get_analyst_price_targets"),
        safe_call(ticker, "get_recommendations_summary"),
        collected_at=collected_at,
    )
    return build_analyst_summary_row(
        symbol,
        dedupe_analyst_actions(actions),
        consensus,
        collected_at=collected_at,
    )


def rows_from_upgrades_downgrades(symbol: str, frame: Any, *, collected_at: datetime) -> list[dict[str, Any]]:
    if frame is None or not hasattr(frame, "iterrows"):
        return []
    rows: list[dict[str, Any]] = []
    for index, row in frame.iterrows():
        firm = first_text(row, "Firm", "firm", "Company", "company")
        if not firm:
            continue
        action_date_value = first_value(row, "GradeDate", "gradeDate", "Date", "date")
        action_at = datetime_from_any(
            index if action_date_value is None else action_date_value,
            fallback=collected_at,
        )
        action = first_text(row, "Action", "action") or "unknown"
        from_grade = first_text(row, "FromGrade", "fromGrade", "from_grade") or ""
        to_grade = first_text(row, "ToGrade", "toGrade", "to_grade") or ""
        prior_target = first_number(
            row,
            "PriorPriceTarget",
            "priorPriceTarget",
            "OldPriceTarget",
            "oldPriceTarget",
            "priceTargetPrior",
        )
        target = first_number(
            row,
            "PriceTarget",
            "priceTarget",
            "CurrentPriceTarget",
            "currentPriceTarget",
            "NewPriceTarget",
            "newPriceTarget",
            "priceTargetCurrent",
        )
        rows.append({
            "symbol": symbol.strip().upper(),
            "action_at": clickhouse_datetime(action_at),
            "firm": firm,
            "action": action.lower(),
            "from_grade": from_grade,
            "to_grade": to_grade,
            "prior_price_target": prior_target,
            "price_target": target,
            "source": "yahoo-finance",
            "collected_at": clickhouse_datetime(collected_at),
        })
    return rows


def rows_from_analyst_consensus(
    symbol: str,
    price_targets: Any,
    recommendations: Any,
    *,
    collected_at: datetime,
) -> list[dict[str, Any]]:
    targets = price_targets if isinstance(price_targets, dict) else {}
    counts = recommendation_counts(recommendations)
    values = {
        "current_price": first_mapping_number(targets, "current", "currentPrice", "regularMarketPrice"),
        "target_low": first_mapping_number(targets, "low", "targetLowPrice"),
        "target_high": first_mapping_number(targets, "high", "targetHighPrice"),
        "target_mean": first_mapping_number(targets, "mean", "targetMeanPrice"),
        "target_median": first_mapping_number(targets, "median", "targetMedianPrice"),
    }
    if all(value is None for value in values.values()) and all(value is None for value in counts.values()):
        return []
    return [{
        "symbol": symbol.strip().upper(),
        "snapshot_date": collected_at.date().isoformat(),
        **values,
        **counts,
        "source": "yahoo-finance",
        "collected_at": clickhouse_datetime(collected_at),
    }]


def build_analyst_summary_row(
    symbol: str,
    actions: list[dict[str, Any]],
    consensus_rows: list[dict[str, Any]],
    *,
    collected_at: datetime,
) -> dict[str, Any] | None:
    """Collapse Yahoo analyst payloads to one display sentence per symbol."""

    normalized_symbol = symbol.strip().upper()
    latest_action = max(actions, key=lambda row: str(row.get("action_at") or ""), default=None)
    consensus = consensus_rows[0] if consensus_rows else {}
    sentences: list[str] = []
    tone = "neutral"

    if latest_action:
        firm = str(latest_action.get("firm") or "").strip()
        action = normalize_analyst_action(str(latest_action.get("action") or ""))
        from_grade = str(latest_action.get("from_grade") or "").strip()
        to_grade = str(latest_action.get("to_grade") or "").strip()
        prior_target = parse_float(latest_action.get("prior_price_target"))
        current_target = parse_float(latest_action.get("price_target"))
        action_date = str(latest_action.get("action_at") or "")[:10]
        if action == "upgrade":
            tone = "positive"
        elif action == "downgrade":
            tone = "negative"
        rating_phrase = analyst_rating_phrase(action, from_grade, to_grade)
        target_phrase = analyst_target_phrase(prior_target, current_target)
        detail = "하고 ".join(value for value in (rating_phrase, target_phrase) if value)
        if firm and detail:
            prefix = f"{action_date} " if action_date else ""
            sentences.append(f"{prefix}{firm}은 {normalized_symbol}의 {detail}했습니다.")

    target_mean = parse_float(consensus.get("target_mean"))
    if target_mean is not None:
        sentences.append(f"시장 평균 목표주가는 {format_usd(target_mean)}입니다.")
    strong_buy = int_or_none(consensus.get("strong_buy")) or 0
    buy = int_or_none(consensus.get("buy")) or 0
    hold = int_or_none(consensus.get("hold")) or 0
    sell = int_or_none(consensus.get("sell")) or 0
    strong_sell = int_or_none(consensus.get("strong_sell")) or 0
    if strong_buy + buy + hold + sell + strong_sell > 0:
        sentences.append(
            f"의견 분포는 매수 {strong_buy + buy}·보유 {hold}·매도 {sell + strong_sell}입니다."
        )
    if not sentences:
        return None
    return {
        "symbol": normalized_symbol,
        "statement": " ".join(sentences),
        "tone": tone,
        "source_as_of": clickhouse_datetime(collected_at),
        "source": "yahoo-finance",
        "collected_at": clickhouse_datetime(collected_at),
    }


def normalize_analyst_action(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in {"up", "upgrade", "upgraded"}:
        return "upgrade"
    if normalized in {"down", "downgrade", "downgraded"}:
        return "downgrade"
    if normalized in {"main", "maintain", "maintained", "reit", "reiterate", "reiterated"}:
        return "maintain"
    if normalized in {"init", "initiate", "initiated"}:
        return "initiate"
    return normalized or "unknown"


def analyst_rating_phrase(action: str, from_grade: str, to_grade: str) -> str:
    grade = to_grade or from_grade
    if action == "upgrade" and from_grade and to_grade:
        return f"투자의견을 {from_grade}에서 {to_grade}로 상향"
    if action == "upgrade" and to_grade:
        return f"투자의견을 {to_grade}로 상향"
    if action == "downgrade" and from_grade and to_grade:
        return f"투자의견을 {from_grade}에서 {to_grade}로 하향"
    if action == "downgrade" and to_grade:
        return f"투자의견을 {to_grade}로 하향"
    if action == "initiate" and to_grade:
        return f"투자의견 {to_grade}로 분석을 시작"
    if action == "maintain" and grade:
        return f"투자의견 {grade}를 유지"
    if grade:
        return f"투자의견 {grade}를 제시"
    return ""


def analyst_target_phrase(prior_target: float | None, current_target: float | None) -> str:
    if prior_target is not None and current_target is not None:
        direction = "상향" if current_target > prior_target else "하향" if current_target < prior_target else "유지"
        return f"목표주가를 {format_usd(prior_target)}에서 {format_usd(current_target)}로 {direction}"
    if current_target is not None:
        return f"목표주가를 {format_usd(current_target)}로 제시"
    return ""


def format_usd(value: float) -> str:
    return f"${value:,.2f}".rstrip("0").rstrip(".")


def int_or_none(value: Any) -> int | None:
    number = parse_float(value)
    return int(number) if number is not None and number >= 0 else None


def finalize_analyst_summary_storage(clickhouse_client: Any) -> None:
    """Remove legacy raw analyst tables and compact the 24-hour projection."""

    clickhouse_client.execute("DROP TABLE IF EXISTS market_data.yahoo_analyst_actions")
    clickhouse_client.execute("DROP TABLE IF EXISTS market_data.yahoo_analyst_consensus")
    clickhouse_client.execute("OPTIMIZE TABLE market_data.yahoo_analyst_summaries FINAL")


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


def first_value(row: Any, *keys: str) -> Any:
    for key in keys:
        try:
            value = row.get(key)
        except Exception:
            value = None
        if value is not None and str(value).strip() not in {"", "nan", "NaT", "<NA>"}:
            return value
    return None


def first_text(row: Any, *keys: str) -> str | None:
    value = first_value(row, *keys)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def first_mapping_number(mapping: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        parsed = parse_float(mapping.get(key))
        if parsed is not None:
            return parsed
    return None


def recommendation_counts(frame: Any) -> dict[str, int | None]:
    empty = {"strong_buy": None, "buy": None, "hold": None, "sell": None, "strong_sell": None}
    if frame is None or not hasattr(frame, "iterrows"):
        return empty
    rows = list(frame.iterrows())
    if not rows:
        return empty
    _, row = next((entry for entry in rows if str(entry[0]).strip().lower() in {"0m", "current"}), rows[0])
    return {
        "strong_buy": first_int(row, "strongBuy", "strong_buy", "Strong Buy"),
        "buy": first_int(row, "buy", "Buy"),
        "hold": first_int(row, "hold", "Hold"),
        "sell": first_int(row, "sell", "Sell"),
        "strong_sell": first_int(row, "strongSell", "strong_sell", "Strong Sell"),
    }


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


def datetime_from_any(value: Any, *, fallback: datetime) -> datetime:
    raw = value
    to_python = getattr(value, "to_pydatetime", None)
    if callable(to_python):
        try:
            raw = to_python()
        except Exception:
            raw = value
    if isinstance(raw, datetime):
        parsed = raw
    elif isinstance(raw, date):
        parsed = datetime(raw.year, raw.month, raw.day)
    else:
        try:
            parsed = datetime.fromisoformat(str(raw or "").strip().replace("Z", "+00:00"))
        except ValueError:
            return fallback.astimezone(timezone.utc)
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


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


def dedupe_analyst_actions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (
            str(row.get("symbol")),
            str(row.get("action_at")),
            str(row.get("firm")),
            str(row.get("action")),
        )
        deduped[key] = row
    return list(deduped.values())


def append_sync_error(stats: YahooEstimatesStats, symbol: str, source: str, error_type: str, message: str) -> None:
    detail = f"{error_type}: {message}" if message else error_type
    existing = stats.errors.get(symbol)
    stats.errors[symbol] = f"{existing}; {source}={detail}"[:480] if existing else f"{source}={detail}"[:480]


def bool_env(value: Any, default: bool) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
