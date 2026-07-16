from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from statistics import fmean, pstdev
from typing import Any

from gops_agents.contracts import EvidenceItem


@dataclass(frozen=True)
class MetricSpec:
    id: str
    label: str
    unit: str
    signed: bool = False


QUANTITATIVE_SECTIONS: tuple[tuple[str, str, tuple[MetricSpec, ...]], ...] = (
    (
        "growth_style",
        "성장 스타일",
        (
            MetricSpec("revenue_growth_yoy", "매출 성장률", "percent", signed=True),
            MetricSpec("operating_income_growth_yoy", "영업이익 성장률", "percent", signed=True),
            MetricSpec("net_income_growth_yoy", "순이익 성장률", "percent", signed=True),
        ),
    ),
    (
        "profit_structure",
        "수익 구조",
        (
            MetricSpec("net_margin", "순이익률", "percent"),
            MetricSpec("operating_margin", "영업이익률", "percent"),
            MetricSpec("roe", "자기자본이익률(ROE)", "percent"),
        ),
    ),
    (
        "financial_health",
        "재무 체질",
        (
            MetricSpec("total_debt_to_assets", "총부채/자산", "percent"),
            MetricSpec("total_debt_to_equity", "총부채/자본", "percent"),
            MetricSpec("current_ratio", "유동비율", "ratio"),
            MetricSpec("free_cash_flow", "잉여현금흐름", "currency", signed=True),
            MetricSpec("interest_coverage", "이자보상배율", "ratio"),
        ),
    ),
    (
        "earnings_stability",
        "실적 안정성",
        (
            MetricSpec("eps_surprise_mean", "평균 EPS 서프라이즈", "percent", signed=True),
            MetricSpec("eps_surprise_volatility", "EPS 서프라이즈 변동성", "percent"),
            MetricSpec("eps_beat_rate", "EPS 예상 상회 비율", "percent"),
            MetricSpec("earnings_observations", "비교 분기 수", "count"),
        ),
    ),
)


FRAME_FACTS: dict[str, tuple[str, str, int]] = {
    "RevenueFromContractWithCustomerExcludingAssessedTax": ("revenue", "매출", 0),
    "RevenueFromContractWithCustomerIncludingAssessedTax": ("revenue", "매출", 1),
    "Revenues": ("revenue", "매출", 2),
    "SalesRevenueNet": ("revenue", "매출", 3),
    "OperatingIncomeLoss": ("operating_income", "영업이익", 0),
    "NetIncomeLoss": ("net_income", "순이익", 0),
    "Assets": ("assets", "자산", 0),
    "Liabilities": ("liabilities", "부채", 0),
    "StockholdersEquity": ("equity", "자본", 0),
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest": ("equity", "자본", 1),
}

QUALITATIVE_SECTION_HEADINGS = {
    "business_model": "돈 버는 방식",
    "risk_profile": "리스크 체질",
    "relationship": "관계 맥락",
    "recent_flow": "최근 흐름",
}
RISK_NEWS_EVENT_TYPES = {"regulation", "legal", "macro"}


def build_quantitative_context(
    symbols: list[str],
    summaries: dict[str, dict[str, Any] | None],
    peer_summary: dict[str, Any] | None,
    earnings_series: dict[str, list[dict[str, Any]]],
    *,
    provider_gaps: list[str] | None = None,
) -> dict[str, Any]:
    metric_rows = {symbol: summary_metric_map(summaries.get(symbol)) for symbol in symbols}
    earnings_metrics = {
        symbol: earnings_stability_metrics(earnings_series.get(symbol, []))
        for symbol in symbols
    }
    missing_fundamentals = [symbol for symbol in symbols if not metric_rows[symbol]]
    data_gaps = list(provider_gaps or [])
    sections: list[dict[str, Any]] = []

    for section_id, heading, specs in QUANTITATIVE_SECTIONS:
        metrics: list[dict[str, Any]] = []
        for spec in specs:
            values = []
            for symbol in symbols:
                if section_id == "earnings_stability":
                    row = earnings_metrics[symbol].get(spec.id)
                    source_ref = f"earnings:{symbol}"
                else:
                    row = metric_rows[symbol].get(spec.id)
                    source_ref = f"financial:{symbol}"
                numeric = read_number((row or {}).get("value"))
                if numeric is None:
                    data_gaps.append(f"{symbol}: {spec.label} 데이터 없음")
                values.append({
                    "symbol": symbol,
                    "value": numeric,
                    "display": format_metric_value(numeric, spec),
                    "asOf": read_text((row or {}).get("asOf") or (row or {}).get("periodEnd")),
                    "sourceRef": source_ref,
                    "quality": read_text((row or {}).get("quality")) or None,
                })
            if any(value["value"] is not None for value in values):
                metrics.append({
                    "id": spec.id,
                    "label": spec.label,
                    "unit": spec.unit,
                    "values": values,
                })
        if metrics:
            sections.append({"id": section_id, "heading": heading, "metrics": metrics})

    aligned_facts, period_alignment = build_aligned_facts(symbols, peer_summary)
    if not aligned_facts:
        data_gaps.append("동일 회계기간 SEC frames 비교 데이터 없음")

    sources = build_sources(symbols, summaries, earnings_series)
    companies = [build_company_metadata(symbol, summaries.get(symbol)) for symbol in symbols]
    unique_gaps = unique_strings(data_gaps)
    return {
        "status": "partial" if unique_gaps else "ready",
        "companies": companies,
        "sections": sections,
        "growthChart": build_growth_chart(symbols, sections),
        "alignedFacts": aligned_facts,
        "periodAlignment": period_alignment,
        "missingFundamentals": missing_fundamentals,
        "dataGaps": unique_gaps,
        "sources": sources,
    }


def build_qualitative_context(
    symbols: list[str],
    ten_k_evidence: list[EvidenceItem],
    ontology_evidence: list[EvidenceItem],
    news_evidence: list[EvidenceItem],
    *,
    provider_gaps: list[str] | None = None,
    news_per_symbol: int = 3,
) -> dict[str, Any]:
    """Builds the four M3 qualitative materials without calling an LLM.

    Every item carries a sourceRef that also exists in the returned source list.
    Missing providers omit only their section material and surface a dataGap.
    """
    data_gaps = list(provider_gaps or [])
    sources: list[dict[str, Any]] = []
    business_items: list[dict[str, Any]] = []
    risk_items: list[dict[str, Any]] = []
    relationship_items: list[dict[str, Any]] = []
    recent_items: list[dict[str, Any]] = []

    profiles_by_symbol: dict[str, EvidenceItem] = {}
    for evidence in ten_k_evidence:
        raw = evidence.raw if isinstance(evidence.raw, dict) else {}
        symbol = read_text(raw.get("symbol")).upper()
        if evidence.status == "available" and symbol in symbols:
            profiles_by_symbol[symbol] = evidence
        elif evidence.status != "available" and symbol in symbols:
            data_gaps.append(f"{symbol}: 10-K 프로파일 데이터 없음")

    for symbol in symbols:
        profile = profiles_by_symbol.get(symbol)
        if profile is None:
            data_gaps.append(f"{symbol}: 돈 버는 방식·리스크 체질용 10-K 프로파일 없음")
            continue
        raw = profile.raw
        source_ref = f"tenk:{symbol}"
        append_source(sources, {
            "id": source_ref,
            "label": read_text(raw.get("sourceFiling")) or "SEC 10-K profile",
            "symbol": symbol,
            "asOf": read_text(raw.get("reportDate") or raw.get("filingDate") or raw.get("generatedAt")),
            "accession": read_text(raw.get("sourceAccession")) or None,
            "url": profile.url,
        })
        business_model = read_text(raw.get("businessModel"))
        revenue_drivers = unique_strings(raw.get("revenueDrivers") or [])
        competitive_position = read_text(raw.get("competitivePosition"))
        if business_model:
            business_items.append({
                "kind": "10k-business",
                "symbol": symbol,
                "title": f"{symbol} 사업 모델",
                "summary": business_model,
                "details": [*revenue_drivers, *([competitive_position] if competitive_position else [])],
                "sourceRef": source_ref,
                "observedAt": profile.observedAt,
                "url": profile.url,
            })
        else:
            data_gaps.append(f"{symbol}: 10-K 사업 모델 요약 없음")
        risk_factors = [item for item in raw.get("riskFactors") or [] if isinstance(item, dict)]
        if not risk_factors:
            data_gaps.append(f"{symbol}: 10-K 리스크 요약 없음")
        for factor in risk_factors:
            category = read_text(factor.get("category"))
            summary = read_text(factor.get("summary"))
            if not category or not summary:
                continue
            severity = read_text(factor.get("severityHint"))
            risk_items.append({
                "kind": "10k-risk",
                "symbol": symbol,
                "title": f"{symbol} · {category}",
                "summary": summary,
                "details": [f"문서 강조도: {severity}"] if severity else [],
                "sourceRef": source_ref,
                "observedAt": profile.observedAt,
                "url": profile.url,
            })

    ontology_index = 0
    for evidence in ontology_evidence:
        raw = evidence.raw if isinstance(evidence.raw, dict) else {}
        raw_type = read_text(raw.get("type"))
        relation_type = read_text(raw.get("relationType"))
        is_cross = raw_type.startswith("cross-symbol-") or relation_type in {
            "shared-theme", "cross-control", "no-shared-relationship"
        }
        is_business_theme = raw_type == "ticker-theme" or relation_type == "theme"
        if evidence.status != "available":
            if is_cross or relation_type in {"graphdb-unavailable", "no-ontology-evidence"}:
                data_gaps.append(evidence.summary)
            continue
        if not is_cross and not is_business_theme:
            continue
        ontology_index += 1
        relation_symbols = [read_text(value).upper() for value in raw.get("symbols") or [] if read_text(value)]
        ticker = read_text(raw.get("ticker")).upper()
        if ticker and ticker not in relation_symbols:
            relation_symbols.insert(0, ticker)
        source_ref = f"ontology:{ontology_index}"
        append_source(sources, {
            "id": source_ref,
            "label": "GraphDB 온톨로지",
            "symbol": "·".join(relation_symbols) or symbols[0],
            "asOf": evidence.observedAt,
            "accession": read_text(raw.get("accession")) or None,
            "url": evidence.url,
        })
        item = {
            "kind": "ontology-relationship" if is_cross else "ontology-theme",
            "symbol": "·".join(relation_symbols) or None,
            "title": evidence.title,
            "summary": evidence.summary,
            "details": unique_strings([
                raw.get("themeName"),
                raw.get("controlledName"),
                f"관계 유형: {relation_type}" if relation_type else "",
            ]),
            "sourceRef": source_ref,
            "observedAt": evidence.observedAt,
            "url": evidence.url,
        }
        if is_cross:
            relationship_items.append(item)
        if is_business_theme or relation_type == "shared-theme":
            business_items.append(item)

    news_by_symbol: dict[str, list[EvidenceItem]] = {symbol: [] for symbol in symbols}
    for evidence in news_evidence:
        if evidence.status != "available":
            continue
        raw = evidence.raw if isinstance(evidence.raw, dict) else {}
        symbol = read_text(raw.get("targetSymbol") or raw.get("symbol")).upper()
        if symbol in news_by_symbol:
            news_by_symbol[symbol].append(evidence)
    for symbol, evidence_items in news_by_symbol.items():
        ordered = sorted(evidence_items, key=news_evidence_sort_key, reverse=True)[: max(1, news_per_symbol)]
        if not ordered:
            data_gaps.append(f"{symbol}: 최근 관련 뉴스 없음")
            continue
        for index, evidence in enumerate(ordered, start=1):
            raw = evidence.raw if isinstance(evidence.raw, dict) else {}
            article_id = read_text(raw.get("articleId")) or str(index)
            source_ref = f"news:{symbol}:{article_id}"
            event_type = read_text(raw.get("eventType")) or "other"
            impact_direction = read_text(raw.get("impactDirection")) or "neutral"
            append_source(sources, {
                "id": source_ref,
                "label": read_text(raw.get("source")) or "저장 뉴스",
                "symbol": symbol,
                "asOf": read_text(raw.get("publishedAt") or raw.get("receivedAt") or evidence.observedAt),
                "accession": None,
                "url": evidence.url,
            })
            item = {
                "kind": "news-event",
                "symbol": symbol,
                "title": evidence.title,
                "summary": evidence.summary,
                "details": [f"이벤트: {event_type}", f"영향 방향: {impact_direction}"],
                "sourceRef": source_ref,
                "observedAt": evidence.observedAt,
                "url": evidence.url,
            }
            recent_items.append(item)
            if event_type in RISK_NEWS_EVENT_TYPES or impact_direction == "negative":
                risk_items.append(item)

    if not business_items:
        data_gaps.append("돈 버는 방식 섹션 근거 없음")
    if not risk_items:
        data_gaps.append("리스크 체질 섹션 근거 없음")
    if not relationship_items:
        data_gaps.append("비교 기업 간 공통 테마·지배관계 근거 없음")
    if not recent_items:
        data_gaps.append("최근 흐름 섹션 뉴스 근거 없음")

    material_by_id = {
        "business_model": business_items,
        "risk_profile": risk_items,
        "relationship": relationship_items,
        "recent_flow": recent_items,
    }
    sections = [
        {
            "id": section_id,
            "heading": QUALITATIVE_SECTION_HEADINGS[section_id],
            "items": items,
            "evidenceRefs": unique_strings(item.get("sourceRef") for item in items),
        }
        for section_id, items in material_by_id.items()
        if items
    ]
    gaps = unique_strings(data_gaps)
    return {
        "status": "partial" if gaps else "ready",
        "sections": sections,
        "dataGaps": gaps,
        "sources": sources,
    }


def append_source(sources: list[dict[str, Any]], source: dict[str, Any]) -> None:
    source_id = read_text(source.get("id"))
    if source_id and not any(read_text(item.get("id")) == source_id for item in sources):
        sources.append(source)


def news_evidence_sort_key(evidence: EvidenceItem) -> tuple[float, float, str]:
    raw = evidence.raw if isinstance(evidence.raw, dict) else {}
    return (
        read_number(raw.get("importanceScore")) or 0.0,
        read_number(raw.get("relevanceScore")) or 0.0,
        read_text(raw.get("publishedAt") or raw.get("receivedAt") or evidence.observedAt),
    )


def summary_metric_map(summary: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(summary, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for raw in summary.get("metrics") or []:
        if not isinstance(raw, dict):
            continue
        metric = read_text(raw.get("metric"))
        if not metric or read_number(raw.get("value")) is None:
            continue
        current = result.get(metric)
        if current is None or metric_period_key(raw) > metric_period_key(current):
            result[metric] = raw
    latest_date = max((metric_date(row) for row in result.values()), default=None)
    if latest_date is None:
        return result
    return {
        metric: row
        for metric, row in result.items()
        if (observed := metric_date(row)) is None or (latest_date - observed).days <= 550
    }


def metric_period_key(metric: dict[str, Any]) -> tuple[str, int, str]:
    period_end = read_text(metric.get("periodEnd") or metric.get("asOf"))
    try:
        fiscal_year = int(metric.get("fiscalYear") or 0)
    except (TypeError, ValueError):
        fiscal_year = 0
    return period_end, fiscal_year, read_text(metric.get("fiscalPeriod"))


def metric_date(metric: dict[str, Any]) -> date | None:
    value = read_text(metric.get("periodEnd") or metric.get("asOf"))
    try:
        return date.fromisoformat(value[:10])
    except (TypeError, ValueError):
        return None


def earnings_stability_metrics(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    surprises: list[float] = []
    beats = 0
    as_of = ""
    for row in rows:
        if not isinstance(row, dict):
            continue
        actual = read_number(row.get("actualEps"))
        estimated = read_number(row.get("estimatedEps"))
        if actual is None or estimated is None or estimated == 0:
            continue
        surprises.append((actual - estimated) / abs(estimated))
        if actual >= estimated:
            beats += 1
        as_of = max(as_of, read_text(row.get("periodEndDate") or row.get("collectedAt")))
    if not surprises:
        return {}
    count = len(surprises)
    common = {"asOf": as_of, "quality": "actual-vs-consensus"}
    return {
        "eps_surprise_mean": {"value": fmean(surprises), **common},
        "eps_surprise_volatility": {"value": pstdev(surprises) if count > 1 else 0.0, **common},
        "eps_beat_rate": {"value": beats / count, **common},
        "earnings_observations": {"value": float(count), **common},
    }


def build_growth_chart(symbols: list[str], sections: list[dict[str, Any]]) -> dict[str, Any]:
    growth = next((section for section in sections if section.get("id") == "growth_style"), None)
    metrics = list((growth or {}).get("metrics") or [])
    return {
        "type": "grouped-bar",
        "unit": "percent",
        "categories": [{"id": item["id"], "label": item["label"]} for item in metrics],
        "series": [
            {
                "symbol": symbol,
                "values": [
                    next((value for value in metric["values"] if value["symbol"] == symbol), {
                        "symbol": symbol,
                        "value": None,
                        "display": "데이터 없음",
                        "asOf": "",
                    })
                    for metric in metrics
                ],
            }
            for symbol in symbols
        ],
    }


def build_aligned_facts(
    symbols: list[str],
    peer_summary: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not isinstance(peer_summary, dict):
        return [], {"basis": "sec-frames", "status": "unavailable", "framePeriods": []}
    frames = peer_summary.get("frames")
    if not isinstance(frames, list):
        frames = [{
            "concept": peer_summary.get("concept"),
            "unit": peer_summary.get("unit"),
            "display_period": peer_summary.get("frame_period"),
            "peers": peer_summary.get("peers"),
        }]
    best_by_metric: dict[str, tuple[int, dict[str, Any]]] = {}
    for frame in frames:
        if not isinstance(frame, dict):
            continue
        concept = read_text(frame.get("concept"))
        mapping = FRAME_FACTS.get(concept)
        if mapping is None:
            continue
        metric_id, label, priority = mapping
        values_by_symbol = {
            read_text(peer.get("symbol")).upper(): peer
            for peer in frame.get("peers") or []
            if isinstance(peer, dict) and read_text(peer.get("symbol"))
        }
        if any(symbol not in values_by_symbol or read_number(values_by_symbol[symbol].get("value")) is None for symbol in symbols):
            continue
        unit = read_text(frame.get("unit")) or read_text(values_by_symbol[symbols[0]].get("unit")) or "USD"
        fact = {
            "id": metric_id,
            "label": label,
            "unit": unit,
            "framePeriod": read_text(frame.get("display_period") or frame.get("frame_period")),
            "values": [
                {
                    "symbol": symbol,
                    "value": read_number(values_by_symbol[symbol].get("value")),
                    "display": format_compact_number(read_number(values_by_symbol[symbol].get("value")), unit),
                }
                for symbol in symbols
            ],
        }
        current = best_by_metric.get(metric_id)
        if current is None or priority < current[0]:
            best_by_metric[metric_id] = (priority, fact)
    ordered_ids = ("revenue", "operating_income", "net_income", "assets", "liabilities", "equity")
    facts = [best_by_metric[metric_id][1] for metric_id in ordered_ids if metric_id in best_by_metric]
    periods = unique_strings(fact["framePeriod"] for fact in facts if fact.get("framePeriod"))
    return facts, {
        "basis": "sec-frames",
        "status": "aligned" if facts else "unavailable",
        "framePeriods": periods,
    }


def build_sources(
    symbols: list[str],
    summaries: dict[str, dict[str, Any] | None],
    earnings_series: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for symbol in symbols:
        summary = summaries.get(symbol) or {}
        if summary:
            sources.append({
                "id": f"financial:{symbol}",
                "label": "SEC companyfacts",
                "symbol": symbol,
                "asOf": read_text(summary.get("as_of") or summary.get("source_filed_at")),
                "accession": read_text(summary.get("source_accession")) or None,
            })
        if earnings_series.get(symbol):
            sources.append({
                "id": f"earnings:{symbol}",
                "label": "SEC actual EPS + Yahoo analyst consensus",
                "symbol": symbol,
                "asOf": max(
                    (read_text(row.get("periodEndDate") or row.get("collectedAt")) for row in earnings_series[symbol]),
                    default="",
                ),
                "accession": None,
            })
    return sources


def build_company_metadata(symbol: str, summary: dict[str, Any] | None) -> dict[str, Any]:
    payload = summary or {}
    return {
        "symbol": symbol,
        "companyName": read_text(payload.get("companyName") or payload.get("company_name")) or None,
        "fiscalPeriod": read_text(payload.get("latest_period")) or None,
        "periodEnd": read_text(payload.get("as_of")) or None,
    }


def format_metric_value(value: float | None, spec: MetricSpec) -> str:
    if value is None:
        return "데이터 없음"
    if spec.unit == "percent":
        prefix = "+" if spec.signed and value > 0 else ""
        return f"{prefix}{value * 100:.1f}%"
    if spec.unit == "ratio":
        return f"{value:.2f}x"
    if spec.unit == "currency":
        return format_compact_number(value, "USD")
    if spec.unit == "count":
        return f"{int(round(value))}개 분기"
    return f"{value:,.2f}"


def format_compact_number(value: float | None, unit: str) -> str:
    if value is None:
        return "데이터 없음"
    absolute = abs(value)
    suffix = ""
    divisor = 1.0
    for threshold, candidate_suffix in ((1_000_000_000_000, "T"), (1_000_000_000, "B"), (1_000_000, "M")):
        if absolute >= threshold:
            divisor = float(threshold)
            suffix = candidate_suffix
            break
    prefix = "$" if unit.upper() == "USD" else ""
    return f"{prefix}{value / divisor:,.1f}{suffix}"


def read_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if numeric == numeric and numeric not in {float("inf"), float("-inf")} else None


def read_text(value: Any) -> str:
    return str(value or "").strip()


def unique_strings(values: list[str] | tuple[str, ...] | Any) -> list[str]:
    result: list[str] = []
    for value in values:
        text = read_text(value)
        if text and text not in result:
            result.append(text)
    return result
