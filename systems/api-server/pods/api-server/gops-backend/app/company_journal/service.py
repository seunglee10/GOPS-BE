from __future__ import annotations

import json
import math
import os
import urllib.request
from datetime import date, datetime, timezone
from typing import Any, Protocol

from .models import NarrativeDraft, report_payload, validate_narrative
from .repository import CompanyJournalRepository


COMPANY_JOURNAL_HISTORY_START_YEAR = 2021


def company_journal_history_years(current_year: int | None = None) -> int:
    year = current_year if current_year is not None else datetime.now(timezone.utc).year
    return max(1, year - COMPANY_JOURNAL_HISTORY_START_YEAR + 1)


def company_journal_cutoff_year(cutoff: str | None) -> int | None:
    if not cutoff:
        return None
    parsed = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).year


def company_journal_evidence_date(
    cutoff: str | None,
    performance: list[dict[str, Any]],
    symbol: str,
) -> str:
    if cutoff:
        parsed = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
        return parsed.date().isoformat()
    for series in performance:
        if str(series.get("symbol") or "").upper() != symbol.upper():
            continue
        candles = series.get("candles") or []
        if not candles:
            continue
        timestamp = str(candles[-1].get("timestamp") or "")
        if timestamp:
            return timestamp[:10]
    return date.today().isoformat()


def latest_source_as_of(values: list[str]) -> str | None:
    parsed_values: list[tuple[datetime, str]] = []
    for value in values:
        try:
            parsed = datetime.fromisoformat(value.replace(" ", "T").replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        parsed_values.append((parsed.astimezone(timezone.utc), value))
    if not parsed_values:
        return None
    return max(parsed_values, key=lambda item: item[0])[1]


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
                "각 탭 문장은 해당 차트에서 먼저 볼 지표, 지표의 의미, 다음 확인 행동을 순서대로 설명하세요. "
                "기관 의견은 24시간 만료되는 별도 조회 projection에서만 표시하므로 기업저널 본문에는 만들거나 포함하지 마세요."
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

    def latest(self, symbol: str, cutoff: datetime | None = None) -> dict[str, Any] | None:
        if cutoff is not None:
            bundle = self.repository.load_source_bundle(symbol, cutoff=cutoff)
            return build_point_in_time_report(
                bundle,
                cutoff=cutoff,
                input_digest=self.repository.input_digest(bundle),
            )
        row = self.repository.latest_verified(symbol)
        return report_payload(row) if row else None

    def panel_evidence(
        self,
        symbol: str,
        benchmark_symbols: list[str],
        cutoff: datetime | None = None,
    ) -> dict[str, Any]:
        from app.market_data.fundamentals.service import (
            build_fundamentals_adapter,
            earnings_series_from_rows,
            financial_series_from_rows,
        )

        if cutoff is None:
            adapter = build_fundamentals_adapter()
            history_years = company_journal_history_years()
            financial = adapter.financial_series(symbol, years=history_years, period="quarterly")
            earnings = adapter.earnings_series(symbol, years=history_years)
        else:
            financial_rows = self.repository.load_financial_series_rows(
                symbol,
                cutoff,
                COMPANY_JOURNAL_HISTORY_START_YEAR,
            )
            actual_rows, estimate_rows = self.repository.load_earnings_series_rows(
                symbol,
                cutoff,
                COMPANY_JOURNAL_HISTORY_START_YEAR,
            )
            financial = financial_series_from_rows(financial_rows).get(symbol, [])
            earnings = earnings_series_from_rows(actual_rows, estimate_rows).get(symbol, [])
        performance = (
            self.repository.load_performance_series([symbol, *benchmark_symbols])
            if cutoff is None
            else self.repository.load_performance_series([symbol, *benchmark_symbols], cutoff=cutoff)
        )
        analyst_summary = compact_analyst_summary(
            self.repository.load_analyst_summary(symbol, cutoff=cutoff) or {}
        )
        missing: list[str] = []
        if not financial:
            missing.append("sec_financial_series")
        if not earnings:
            missing.append("sec_yahoo_earnings_series")
        if not any(series.get("symbol") == symbol and series.get("candles") for series in performance):
            missing.append("company_daily_prices")
        if analyst_summary is None:
            missing.append("yahoo_analyst_summary")
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
        source_dates.extend(
            str(candle.get("timestamp"))
            for series in performance
            for candle in series.get("candles") or []
            if candle.get("timestamp")
        )
        if analyst_summary:
            source_value = analyst_summary.get("sourceAsOf") or analyst_summary.get("collectedAt")
            if source_value:
                source_dates.append(str(source_value))
        result = {
            "contractVersion": "company-journal-evidence.v1",
            "symbol": symbol,
            "sourceAsOf": latest_source_as_of(source_dates),
            "cutoff": cutoff,
            "financialSeries": [point.to_public_dict() for point in financial],
            "earningsSeries": [point.to_public_dict() for point in earnings],
            "performanceSeries": performance,
            "analystSummary": analyst_summary,
            "missingData": missing,
        }
        if cutoff is not None:
            result.update({
                "simulation": True,
                "sourceMode": "historical_reconstruction",
                "cutoff": cutoff.astimezone(timezone.utc).isoformat(),
            })
        return result

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


def build_point_in_time_report(
    bundle: dict[str, Any],
    *,
    cutoff: datetime,
    input_digest: str,
) -> dict[str, Any]:
    metrics, missing_data = calculate_server_metrics(bundle)
    symbol = str(bundle.get("symbol") or "").upper()
    company = bundle.get("company") or {}
    company_name = str(company.get("company_name") or company.get("name") or symbol)
    analysis_as_of = str(bundle.get("analysisAsOf") or cutoff.date().isoformat())
    stock_return = finite_number(metrics.get("stockReturnPercent"))
    relative_return = finite_number(metrics.get("relativeReturnPercentagePoints"))
    financial = metrics.get("financial") or {}
    liabilities_to_equity = finite_number(financial.get("liabilitiesToEquity"))
    revenue_growth = finite_number(financial.get("revenueGrowthYoY"))
    operating_margin = finite_number(financial.get("operatingMargin"))
    estimate_rows = bundle.get("earningsEstimates") or []
    actual_rows = bundle.get("earningsActuals") or []

    if stock_return is None:
        movement = "가상시각 이전의 완료 일봉이 부족해 최근 수익률은 계산하지 않았습니다."
    elif relative_return is None:
        movement = f"최근 3개 완료 거래일 수익률은 {stock_return:+.2f}%입니다. 비교지수 근거는 부족합니다."
    else:
        movement = (
            f"최근 3개 완료 거래일 수익률은 {stock_return:+.2f}%이고, "
            f"S&P 500 대비 상대수익률은 {relative_return:+.2f}%p입니다."
        )

    if liabilities_to_equity is None:
        stability = "가상시각 전에 공개된 부채비율 근거가 없어 재무 안정성을 판단하지 않았습니다."
    else:
        stability = (
            f"가상시각 전에 공개된 부채 대비 자본 비율은 {liabilities_to_equity * 100:.1f}%입니다. "
            "현금흐름과 이자보상배율을 함께 확인해야 합니다."
        )

    growth_text = (
        f"확인 가능한 최근 매출 성장률은 {revenue_growth * 100:+.1f}%입니다."
        if revenue_growth is not None
        else "가상시각 전에 공개된 매출 성장률 근거가 부족합니다."
    )
    profitability_text = (
        f"확인 가능한 최근 영업이익률은 {operating_margin * 100:.1f}%입니다."
        if operating_margin is not None
        else "가상시각 전에 공개된 영업이익률 근거가 부족합니다."
    )
    earnings_text = (
        f"공개 실적 {len(actual_rows)}건과 당시 저장된 예상치 {len(estimate_rows)}건을 비교할 수 있습니다."
        if actual_rows or estimate_rows
        else "가상시각 전에 함께 비교할 수 있는 공개 실적과 예상치가 없습니다."
    )
    missing_summary = ", ".join(missing_data[:4])
    watch_items = (
        f"누락된 근거({missing_summary})는 최신값으로 대체하지 않았습니다."
        if missing_summary
        else "다음 공개 실적과 완료 일봉에서 같은 흐름이 이어지는지 확인해야 합니다."
    )
    cutoff_text = cutoff.astimezone(timezone.utc).isoformat()
    receipt = {
        **source_receipt(bundle),
        "sourceMode": "historical_reconstruction",
        "sourceCutoff": cutoff_text,
    }
    return {
        "contractVersion": "company-journal.v2",
        "symbol": symbol,
        "analysisAsOf": analysis_as_of,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "inputDigest": input_digest,
        "headline": f"{company_name}의 최근 사업 흐름이 실적과 현금흐름으로 이어지는지 살펴볼 구간입니다.",
        "keywords": ["시점 재현", "완료 일봉", "공개 재무"],
        "recentMovement": movement,
        "financialStability": stability,
        "watchItems": watch_items,
        "tabs": {
            "current": f"{analysis_as_of}까지 공개된 자료만 사용했습니다. {movement}",
            "growth": growth_text,
            "profitability": profitability_text,
            "earnings": earnings_text,
            "stability": stability,
            "valuation": "가상시각 이전의 완료 일봉과 공개 재무자료가 있는 범위에서만 가치 지표를 확인합니다.",
        },
        "serverMetrics": metrics,
        "sourceReceipt": receipt,
        "missingData": missing_data,
        "validationStatus": "verified",
        "sourceMode": "historical_reconstruction",
        "sourceCutoff": cutoff_text,
        "simulation": True,
    }


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


def compact_analyst_summary(row: dict[str, Any]) -> dict[str, Any] | None:
    statement = str(row.get("statement") or "").strip()
    if not statement:
        return None
    raw_tone = str(row.get("tone") or "neutral").strip().lower()
    return {
        "statement": statement[:2000],
        "tone": raw_tone if raw_tone in {"positive", "negative"} else "neutral",
        "sourceAsOf": str(row.get("source_as_of") or row.get("sourceAsOf") or "") or None,
        "collectedAt": str(row.get("collected_at") or row.get("collectedAt") or "") or None,
        "source": str(row.get("source") or "yahoo-finance"),
    }


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
