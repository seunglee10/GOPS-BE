from __future__ import annotations

import json
import math
import os
import urllib.request
from datetime import datetime, timezone
from typing import Any, Protocol

from .models import NarrativeDraft, report_payload, validate_narrative
from .repository import CompanyJournalRepository


COMPANY_JOURNAL_HISTORY_START_YEAR = 2021


def company_journal_history_years(current_year: int | None = None) -> int:
    year = current_year if current_year is not None else datetime.now(timezone.utc).year
    return max(1, year - COMPANY_JOURNAL_HISTORY_START_YEAR + 1)


class JournalWriter(Protocol):
    def generate(self, bundle: dict[str, Any], metrics: dict[str, Any], missing_data: list[str]) -> NarrativeDraft: ...


class OpenAIJournalWriter:
    def __init__(self) -> None:
        self.api_key = os.getenv("OPENAI_API_KEY", "").strip()
        self.model = os.getenv("COMPANY_JOURNAL_OPENAI_MODEL", "gpt-5.2").strip() or "gpt-5.2"
        self.timeout_seconds = float(os.getenv("COMPANY_JOURNAL_OPENAI_TIMEOUT_SECONDS", "30"))

    def generate(self, bundle: dict[str, Any], metrics: dict[str, Any], missing_data: list[str]) -> NarrativeDraft:
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured")
        evidence = {
            "symbol": bundle["symbol"],
            "company": bundle.get("company") or {},
            "analysisAsOf": bundle["analysisAsOf"],
            "serverMetrics": metrics,
            "news": bundle.get("news") or [],
            "financialMetrics": latest_metric_rows(bundle.get("financialMetrics") or []),
            "earningsActuals": (bundle.get("earningsActuals") or [])[:24],
            "earningsEstimates": (bundle.get("earningsEstimates") or [])[:24],
            "filings": bundle.get("filings") or [],
            "graphRelation": bundle.get("graph") or {},
            "missingData": missing_data,
        }
        payload = {
            "model": self.model,
            "instructions": (
                "당신은 GOPS AI 기업저널의 한국어 편집자입니다. 제공된 서버 계산값과 근거만 사용하세요. "
                "숫자를 새로 계산하거나 원인을 단정하지 말고, 데이터가 없으면 확인할 수 없다고 쓰세요. "
                "키워드는 빠른 인식용, 문장은 의미와 다음 확인 행동을 설명하는 자연스러운 존댓말로 작성하세요. "
                "각 탭 문장은 해당 차트에서 먼저 볼 지표, 지표의 의미, 다음 확인 행동을 순서대로 설명하세요."
            ),
            "input": json.dumps(evidence, ensure_ascii=False, default=str),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "company_journal",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "headline": {"type": "string"},
                            "keywords": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 5},
                            "recentMovement": {"type": "string"},
                            "financialStability": {"type": "string"},
                            "watchItems": {"type": "string"},
                            "tabs": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "current": {"type": "string"},
                                    "growth": {"type": "string"},
                                    "profitability": {"type": "string"},
                                    "earnings": {"type": "string"},
                                    "stability": {"type": "string"},
                                    "valuation": {"type": "string"},
                                },
                                "required": ["current", "growth", "profitability", "earnings", "stability", "valuation"],
                            },
                        },
                        "required": ["headline", "keywords", "recentMovement", "financialStability", "watchItems", "tabs"],
                    },
                }
            },
        }
        request = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            response_data = json.loads(response.read().decode("utf-8"))
        parsed = parse_openai_json(response_data)
        return NarrativeDraft(
            headline=str(parsed.get("headline") or "").strip(),
            keywords=[str(value).strip() for value in parsed.get("keywords") or []],
            recent_movement=str(parsed.get("recentMovement") or "").strip(),
            financial_stability=str(parsed.get("financialStability") or "").strip(),
            watch_items=str(parsed.get("watchItems") or "").strip(),
            tabs={str(key): str(value).strip() for key, value in (parsed.get("tabs") or {}).items()},
            model=self.model,
        )


class CompanyJournalService:
    def __init__(self, repository: CompanyJournalRepository | None = None, writer: JournalWriter | None = None) -> None:
        self.repository = repository or CompanyJournalRepository()
        self.writer = writer or OpenAIJournalWriter()

    def latest(self, symbol: str) -> dict[str, Any] | None:
        row = self.repository.latest_verified(symbol)
        return report_payload(row) if row else None

    def panel_evidence(self, symbol: str, benchmark_symbols: list[str]) -> dict[str, Any]:
        from app.market_data.fundamentals.service import build_fundamentals_adapter

        adapter = build_fundamentals_adapter()
        history_years = company_journal_history_years()
        financial = adapter.financial_series(symbol, years=history_years, period="quarterly")
        earnings = adapter.earnings_series(symbol, years=history_years)
        performance = self.repository.load_performance_series([symbol, *benchmark_symbols])
        missing: list[str] = []
        if not financial:
            missing.append("sec_financial_series")
        if not earnings:
            missing.append("sec_yahoo_earnings_series")
        if not any(series.get("symbol") == symbol and series.get("candles") for series in performance):
            missing.append("company_daily_prices")
        source_dates = [
            str(point.periodEndDate)
            for point in financial
            if point.periodEndDate
        ]
        source_dates.extend(
            str(point.periodEndDate)
            for point in earnings
            if point.periodEndDate
        )
        return {
            "contractVersion": "company-journal-evidence.v1",
            "symbol": symbol,
            "sourceAsOf": max(source_dates, default=None),
            "financialSeries": [point.to_public_dict() for point in financial],
            "earningsSeries": [point.to_public_dict() for point in earnings],
            "performanceSeries": performance,
            "missingData": missing,
        }

    def enqueue_if_stale(self, symbol: str, source: str = "panel") -> bool:
        bundle = self.repository.load_source_bundle(symbol)
        digest = self.repository.input_digest(bundle)
        if self.repository.latest_verified_for_digest(symbol, digest):
            return False
        self.repository.enqueue(bundle, digest, source)
        return True

    def enqueue_daily(self, limit: int) -> int:
        count = 0
        for symbol in self.repository.daily_candidates(limit):
            if self.enqueue_if_stale(symbol, source="post_market"):
                count += 1
        return count

    def process_pending(self, limit: int) -> dict[str, int]:
        stats = {"completed": 0, "failed": 0, "superseded": 0}
        for request in self.repository.pending_requests(limit):
            self.repository.append_request_event(request, "processing")
            try:
                if self.repository.latest_verified_for_digest(request.symbol, request.input_digest):
                    self.repository.append_request_event(request, "completed")
                    stats["completed"] += 1
                    continue
                bundle = self.repository.load_source_bundle(request.symbol)
                current_digest = self.repository.input_digest(bundle)
                if current_digest != request.input_digest:
                    self.repository.append_request_event(request, "superseded")
                    self.repository.enqueue(bundle, current_digest, request.requested_source)
                    stats["superseded"] += 1
                    continue
                metrics, missing_data = calculate_server_metrics(bundle)
                draft = self.writer.generate(bundle, metrics, missing_data)
                errors = validate_narrative(draft)
                if errors:
                    raise ValueError("; ".join(errors))
                self.repository.insert_verified_report(
                    request,
                    draft,
                    metrics,
                    source_receipt(bundle),
                    missing_data,
                )
                self.repository.append_request_event(request, "completed")
                stats["completed"] += 1
            except Exception as exc:
                self.repository.append_request_event(request, "failed", error=str(exc)[:1000])
                stats["failed"] += 1
        return stats


def calculate_server_metrics(bundle: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    missing: list[str] = []
    stock_return = trailing_return(bundle.get("prices") or [], sessions=3)
    benchmark_return = trailing_return(bundle.get("benchmarkPrices") or [], sessions=3)
    if stock_return is None:
        missing.append("recent_stock_return")
    if benchmark_return is None:
        missing.append("benchmark_return")
    prices = bundle.get("prices") or []
    benchmark_prices = bundle.get("benchmarkPrices") or []
    if len(prices) < 120:
        missing.append("company_price_history_under_120_sessions")
    if len(benchmark_prices) < 120:
        missing.append("benchmark_price_history_under_120_sessions")
    earnings_actuals = bundle.get("earningsActuals") or []
    earnings_estimates = bundle.get("earningsEstimates") or []
    if not earnings_actuals:
        missing.append("earnings_actuals")
    if not earnings_estimates:
        missing.append("earnings_estimates")
    metrics_by_name = latest_metric_values(bundle.get("financialMetrics") or [])
    requested = {
        "revenueGrowthYoY": "revenue_growth_yoy",
        "netIncomeGrowthYoY": "net_income_growth_yoy",
        "operatingMargin": "operating_margin",
        "netMargin": "net_margin",
        "returnOnEquity": "roe",
        "liabilitiesToEquity": "liabilities_to_equity",
        "currentRatio": "current_ratio",
        "freeCashFlow": "free_cash_flow",
        "interestCoverage": "interest_coverage",
    }
    financial: dict[str, float | None] = {}
    for output_name, metric_name in requested.items():
        value = finite_number(metrics_by_name.get(metric_name))
        financial[output_name] = value
        if value is None:
            missing.append(metric_name)
    relative = None if stock_return is None or benchmark_return is None else round(stock_return - benchmark_return, 4)
    return ({
        "recentSessions": 3,
        "stockReturnPercent": stock_return,
        "benchmarkSymbol": "SPY",
        "benchmarkReturnPercent": benchmark_return,
        "relativeReturnPercentagePoints": relative,
        "evidenceCoverage": {
            "companyPriceSessions": len(prices),
            "benchmarkPriceSessions": len(benchmark_prices),
            "earningsActualRows": len(earnings_actuals),
            "earningsEstimateRows": len(earnings_estimates),
        },
        "financial": financial,
    }, sorted(set(missing)))


def trailing_return(rows: list[dict[str, Any]], sessions: int) -> float | None:
    if len(rows) < sessions + 1:
        return None
    start = finite_number(rows[-(sessions + 1)].get("close"))
    end = finite_number(rows[-1].get("close"))
    if start in {None, 0} or end is None:
        return None
    return round((end / start - 1) * 100, 4)


def latest_metric_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        metric = str(row.get("metric") or "")
        if metric and metric not in latest:
            latest[metric] = row
    return list(latest.values())


def latest_metric_values(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {str(row.get("metric")): row.get("value") for row in latest_metric_rows(rows)}


def source_receipt(bundle: dict[str, Any]) -> dict[str, Any]:
    news_ids = sorted({str(article_id) for row in bundle.get("news") or [] for article_id in row.get("article_ids") or []})
    filing_ids = sorted({str(row.get("accession")) for row in bundle.get("filings") or [] if row.get("accession")})
    graph = bundle.get("graph") or {}
    relation_ids = [str(graph["relation_version"])] if graph.get("relation_version") else []
    prices = bundle.get("prices") or []
    earnings_periods = sorted({
        f"{row.get('fiscal_year') or ''}-{row.get('fiscal_period') or ''}"
        for row in [*(bundle.get("earningsActuals") or []), *(bundle.get("earningsEstimates") or [])]
        if row.get("fiscal_year") and row.get("fiscal_period")
    })
    return {
        "newsIds": news_ids,
        "secFilingIds": filing_ids,
        "priceAsOf": prices[-1].get("date") if prices else None,
        "graphRelationIds": relation_ids,
        "financialAccessions": sorted({str(row.get("accession")) for row in bundle.get("financialMetrics") or [] if row.get("accession")}),
        "earningsPeriods": earnings_periods,
        "earningsEstimateAsOf": max(
            (str(row.get("collected_at")) for row in bundle.get("earningsEstimates") or [] if row.get("collected_at")),
            default=None,
        ),
    }


def finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def parse_openai_json(response_data: dict[str, Any]) -> dict[str, Any]:
    candidates: list[str] = []
    if isinstance(response_data.get("output_text"), str):
        candidates.append(response_data["output_text"])
    for output in response_data.get("output") or []:
        for content in output.get("content") or []:
            if isinstance(content.get("text"), str):
                candidates.append(content["text"])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("OpenAI response did not contain valid journal JSON")
