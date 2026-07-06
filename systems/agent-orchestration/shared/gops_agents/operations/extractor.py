from __future__ import annotations

import re
from typing import Any


CHART_REFERENCE_TYPES = {"chart.candle", "chart.range"}
NEWS_REFERENCE_TYPES = {"news.article", "news.dailySummary"}
ONTOLOGY_REFERENCE_TYPES = {"ontology.entity"}
FINANCIAL_REFERENCE_TYPES = {"financial.metric"}


def normalize_operation_references(
    references: Any,
    ui_context: Any = None,
    chart_context: Any = None,
) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for item in references if isinstance(references, list) else []:
        if isinstance(item, dict) and isinstance(item.get("type"), str):
            values.append(dict(item))
    for context in (ui_context, chart_context):
        if not isinstance(context, dict):
            continue
        for key in ("selectedReference", "hoverReference"):
            item = context.get(key)
            if isinstance(item, dict) and isinstance(item.get("type"), str):
                values.append(dict(item))
    deduped: list[dict[str, Any]] = []
    seen = set()
    for item in values:
        key = reference_fingerprint(item)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def build_agent_operation_ir(
    *,
    intent: str,
    symbol: str,
    references: list[dict[str, Any]],
    ui_context: dict[str, Any] | None = None,
    chart_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    text = normalize_text(intent)
    refs = normalize_operation_references(references, ui_context, chart_context)
    ref_types = {str(item.get("type") or "") for item in refs}
    dates = extract_date_hints(text)
    layers = extract_layer_hints(text)
    price_sources = extract_price_sources(text)
    operations: list[dict[str, Any]] = []
    ambiguities: list[dict[str, Any]] = []

    if should_link_news_to_price(text, ref_types, dates):
        operations.append(analysis_operation(
            "link_news_to_price_move",
            symbol=symbol,
            references=refs,
            required_sources=["market", "news", "ontology"],
            confidence=0.92 if has_news_ref(ref_types) and has_chart_ref(ref_types) else 0.7,
        ))
        if not has_news_ref(ref_types) and not dates:
            ambiguities.append(ambiguity("newsAnchor", "연결할 뉴스 기사나 날짜가 필요합니다."))
        if not has_chart_ref(ref_types) and not has_move_terms(text):
            ambiguities.append(ambiguity("priceMoveAnchor", "연결할 차트 봉이나 가격 움직임이 필요합니다."))
    elif has_chart_ref(ref_types) and has_explain_terms(text):
        operations.append(analysis_operation(
            "explain_price_move",
            symbol=symbol,
            references=refs,
            required_sources=["market", "news", "macro"],
            confidence=0.86,
        ))
    elif has_news_ref(ref_types) and has_explain_terms(text):
        operations.append(analysis_operation(
            "explain_news",
            symbol=symbol,
            references=refs,
            required_sources=["news", "market", "ontology"],
            confidence=0.82,
        ))
    elif has_news_terms(text):
        operations.append(analysis_operation(
            "summarize_news",
            symbol=symbol,
            references=refs,
            required_sources=["news"],
            confidence=0.74,
        ))
    elif has_relation_terms(text) or ref_types & ONTOLOGY_REFERENCE_TYPES:
        operations.append(analysis_operation(
            "explain_relationship",
            symbol=symbol,
            references=refs,
            required_sources=["ontology", "news"] if has_news_terms(text) or has_news_ref(ref_types) else ["ontology"],
            confidence=0.76,
        ))

    chart_operations = extract_chart_operations(text, layers, price_sources, dates, refs)
    operations.extend(chart_operations)
    suggested_roles = roles_for_operations(operations)
    confidence = operation_ir_confidence(operations, ambiguities)
    return {
        "version": 1,
        "source": "operation-extractor",
        "operations": operations,
        "entities": {
            "symbols": [symbol] if symbol and symbol != "UNKNOWN" else [],
            "dates": dates,
            "layers": layers,
            "priceSources": price_sources,
        },
        "references": refs,
        "contextWindow": build_context_window_spec(refs, dates, suggested_roles, chart_context or {}),
        "ambiguities": ambiguities,
        "suggestedRoles": suggested_roles,
        "confidence": confidence,
    }


def extract_chart_operations(
    text: str,
    layers: list[str],
    price_sources: list[str],
    dates: list[str],
    references: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    if has_any(text, ("수평선", "horizontal line", "horizontal-line")):
        operations.append({
            "kind": "chart",
            "type": "add_horizontal_line",
            "target": "price",
            "timeRange": {"dateHints": dates},
            "priceSource": price_sources[0] if price_sources else "close",
            "applyMode": "persistent",
            "confidence": 0.88 if dates or has_chart_reference(references) else 0.62,
        })
    if layers:
        show = not has_any(text, ("숨겨", "숨김", "꺼", "끄", "제거", "hide", "off"))
        operations.append({
            "kind": "chart",
            "type": "set_layer_visibility",
            "target": layers[0],
            "visible": show,
            "applyMode": "persistent",
            "confidence": 0.9,
        })
        if show and has_any(text, ("만", "only")):
            operations.append({
                "kind": "chart",
                "type": "hide_other_indicator_layers",
                "target": layers[0],
                "applyMode": "persistent",
                "confidence": 0.88,
            })
    return operations


def analysis_operation(
    op_type: str,
    *,
    symbol: str,
    references: list[dict[str, Any]],
    required_sources: list[str],
    confidence: float,
) -> dict[str, Any]:
    return {
        "kind": "analysis",
        "type": op_type,
        "subject": {"symbol": symbol if symbol and symbol != "UNKNOWN" else None},
        "anchorReferences": [reference_summary(item) for item in references],
        "requiredSources": required_sources,
        "confidence": confidence,
    }


def build_context_window_spec(
    references: list[dict[str, Any]],
    dates: list[str],
    roles: list[str],
    chart_context: dict[str, Any],
) -> dict[str, Any]:
    anchors = [reference_summary(item) for item in references]
    chart_document = chart_context.get("chartDocument") if isinstance(chart_context.get("chartDocument"), dict) else {}
    return {
        "anchors": anchors,
        "dateHints": dates,
        "requiredSnapshots": roles_to_snapshots(roles),
        "chart": {
            "symbol": chart_document.get("symbol"),
            "timeframe": chart_document.get("timeframe"),
        },
        "policy": {
            "preferExplicitReferences": True,
            "llmReceivesEvidenceOnly": True,
        },
    }


def roles_for_operations(operations: list[dict[str, Any]]) -> list[str]:
    roles: list[str] = []
    for operation in operations:
        if operation.get("kind") == "chart":
            append_unique(roles, "chart")
            continue
        for source in operation.get("requiredSources", []):
            if source == "market":
                append_unique(roles, "chart")
            elif source == "news":
                append_unique(roles, "news")
            elif source == "ontology":
                append_unique(roles, "ontology")
            elif source == "financial":
                append_unique(roles, "financial")
            elif source == "macro":
                append_unique(roles, "macro")
    return roles


def roles_to_snapshots(roles: list[str]) -> list[str]:
    mapping = {
        "chart": "market_snapshot",
        "news": "news_snapshot",
        "ontology": "relationship_snapshot",
        "financial": "financial_snapshot",
        "macro": "macro_snapshot",
    }
    return [mapping[role] for role in roles if role in mapping]


def operation_ir_confidence(operations: list[dict[str, Any]], ambiguities: list[dict[str, Any]]) -> float:
    if not operations:
        return 0.0
    base = max(float(operation.get("confidence") or 0.0) for operation in operations)
    if ambiguities:
        return min(base, 0.58)
    return base


def extract_date_hints(text: str) -> list[str]:
    values: list[str] = []
    for match in re.finditer(r"(?:(20[0-9]{2})\s*년\s*)?([0-9]{1,2})\s*월\s*([0-9]{1,2})\s*일", text):
        values.append(match.group(0))
    for match in re.finditer(r"(?:(20[0-9]{2})[./-])?([0-9]{1,2})[./-]([0-9]{1,2})", text):
        values.append(match.group(0))
    return dedupe_strings(values)


def extract_layer_hints(text: str) -> list[str]:
    if has_any(text, ("볼린저", "볼밴", "bollinger", "bb")):
        return ["bollinger:20:2"]
    if has_any(text, ("rsi", "알에스아이")):
        return ["rsi:14"]
    if has_any(text, ("macd", "맥디")):
        return ["macd:12:26:9"]
    if has_any(text, ("스토캐스틱", "stochastic")):
        return ["stochastic:14:3:3"]
    if has_any(text, ("거래량 프로파일", "volume profile", "vpvr")):
        return ["volume-profile"]
    ma = re.search(r"(?:ma|sma|이평|이동평균)\s*([0-9]{1,3})", text)
    if ma:
        return [f"sma:{ma.group(1)}"]
    return []


def extract_price_sources(text: str) -> list[str]:
    values: list[str] = []
    if has_any(text, ("종가", "close")):
        values.append("close")
    if has_any(text, ("시가", "open")):
        values.append("open")
    if has_any(text, ("고가", "high")):
        values.append("high")
    if has_any(text, ("저가", "low")):
        values.append("low")
    return values


def should_link_news_to_price(text: str, ref_types: set[str], dates: list[str]) -> bool:
    if has_news_ref(ref_types) and has_chart_ref(ref_types):
        return has_any(text, ("관련", "연관", "연결", "영향", "때문", "원인", "connected", "related", "because"))
    return has_news_terms(text) and (has_move_terms(text) or bool(dates)) and has_any(text, ("관련", "연관", "연결", "영향", "원인", "때문"))


def has_news_ref(ref_types: set[str]) -> bool:
    return bool(ref_types & NEWS_REFERENCE_TYPES)


def has_chart_ref(ref_types: set[str]) -> bool:
    return bool(ref_types & CHART_REFERENCE_TYPES)


def has_chart_reference(references: list[dict[str, Any]]) -> bool:
    return any(str(item.get("type") or "") in CHART_REFERENCE_TYPES for item in references)


def has_explain_terms(text: str) -> bool:
    return has_any(text, ("왜", "원인", "이유", "설명", "분석", "why", "explain"))


def has_news_terms(text: str) -> bool:
    return has_any(text, ("뉴스", "기사", "헤드라인", "news", "headline"))


def has_move_terms(text: str) -> bool:
    return has_any(text, ("하락", "내려", "빠졌", "상승", "올랐", "급등", "급락", "차트", "봉", "move", "down", "up"))


def has_relation_terms(text: str) -> bool:
    return has_any(text, ("관계", "관련 기업", "공급망", "경쟁", "테마", "relationship", "peer", "supply chain"))


def ambiguity(slot: str, question: str) -> dict[str, Any]:
    return {"slot": slot, "candidates": [], "question": question}


def reference_summary(reference: dict[str, Any]) -> dict[str, Any]:
    data = reference.get("data") if isinstance(reference.get("data"), dict) else {}
    return {
        "type": reference.get("type"),
        "sourcePanelId": reference.get("sourcePanelId"),
        "displayLabel": reference.get("displayLabel"),
        "symbol": data.get("symbol"),
        "timestamp": data.get("timestamp") or data.get("publishedAt") or data.get("date") or data.get("from"),
    }


def reference_fingerprint(reference: dict[str, Any]) -> str:
    summary = reference_summary(reference)
    return "|".join(str(summary.get(key) or "") for key in ("type", "sourcePanelId", "displayLabel", "symbol", "timestamp"))


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def has_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def dedupe_strings(values: list[str]) -> list[str]:
    deduped: list[str] = []
    for value in values:
        if value not in deduped:
            deduped.append(value)
    return deduped
