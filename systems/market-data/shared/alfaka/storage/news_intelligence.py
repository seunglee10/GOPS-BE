import json
from datetime import datetime, timezone

from alfaka.news.relevance import classify_subject_relevance, normalize_subject_level
from alfaka.storage.clickhouse_loader import clickhouse_time, clickhouse_time_or_none


def build_news_intelligence_record(event, intelligence=None, *, locale="ko-KR", model="deterministic", localized_at=None):
    intelligence = intelligence or deterministic_news_intelligence(event)
    localized_at = localized_at or utc_now_iso()
    published_at = event.get("publishedAt") or event.get("createdAt") or event.get("timestamp") or localized_at
    symbols = normalized_symbols(event.get("symbols") or [event.get("symbol")])
    symbol = str(event.get("symbol") or (symbols[0] if symbols else "UNKNOWN")).strip().upper()
    relevance = classify_subject_relevance(
        target_symbol=symbol,
        headline=event.get("headline"),
        summary=event.get("summary"),
        content=event.get("content"),
        symbols=symbols,
    )
    return {
        "articleId": str(event.get("articleId") or event.get("sourceEventId") or ""),
        "symbol": symbol,
        "symbols": symbols or [symbol],
        "targetSymbol": str(intelligence.get("targetSymbol") or relevance["targetSymbol"] or symbol).strip().upper(),
        "subjectRelevance": normalize_subject_level(intelligence.get("subjectRelevance") or relevance["subjectRelevance"]),
        "relevanceScoreV2": float(intelligence.get("relevanceScoreV2") or relevance["relevanceScoreV2"]),
        "relevanceReason": clean_text(intelligence.get("relevanceReason") or relevance["relevanceReason"], 500),
        "directSignals": clean_text_list(intelligence.get("directSignals") or relevance["directSignals"], 12, 80),
        "locale": locale,
        "publishedAt": published_at,
        "headline": event.get("headline") or "Untitled news",
        "summary": event.get("summary"),
        "url": event.get("url"),
        "source": event.get("source") or "alpaca",
        "localizedHeadline": clean_text(intelligence.get("localizedHeadline") or intelligence.get("localizedTitle") or event.get("headline"), 180),
        "localizedSummary": clean_text(intelligence.get("localizedSummary") or event.get("summary") or event.get("headline"), 500),
        "keyPoints": clean_text_list(intelligence.get("keyPoints") or intelligence.get("key_points"), 5, 220),
        "positivePoints": clean_text_list(intelligence.get("positivePoints") or intelligence.get("positive_points"), 5, 220),
        "concerns": clean_text_list(intelligence.get("concerns"), 5, 220),
        "eventType": normalize_event_type(intelligence.get("eventType")),
        "sentiment": normalize_sentiment(intelligence.get("sentiment")),
        "impactDirection": normalize_impact_direction(intelligence.get("impactDirection") or intelligence.get("impact_direction")),
        "whyItMatters": clean_text(intelligence.get("whyItMatters") or intelligence.get("why_it_matters") or "", 800),
        "model": str(model or "deterministic"),
        "localizedAt": localized_at,
        "raw": event,
    }


def news_intelligence_to_clickhouse_row(record):
    return {
        "published_at": clickhouse_time(record.get("publishedAt")),
        "symbol": str(record.get("symbol") or "UNKNOWN").upper(),
        "article_id": str(record.get("articleId") or ""),
        "locale": record.get("locale") or "ko-KR",
        "symbols": normalized_symbols(record.get("symbols") or [record.get("symbol")]),
        "target_symbol": str(record.get("targetSymbol") or record.get("symbol") or "UNKNOWN").upper(),
        "subject_relevance": normalize_subject_level(record.get("subjectRelevance")),
        "relevance_score_v2": float_or_zero(record.get("relevanceScoreV2")),
        "relevance_reason": record.get("relevanceReason") or "",
        "direct_signals": clean_text_list(record.get("directSignals"), 12, 80),
        "headline": record.get("headline"),
        "summary": record.get("summary"),
        "url": record.get("url"),
        "source": record.get("source") or "alpaca",
        "localized_headline": record.get("localizedHeadline") or record.get("headline") or "Untitled news",
        "localized_summary": record.get("localizedSummary") or record.get("summary") or record.get("headline") or "",
        "key_points": clean_text_list(record.get("keyPoints"), 5, 220),
        "positive_points": clean_text_list(record.get("positivePoints"), 5, 220),
        "concerns": clean_text_list(record.get("concerns"), 5, 220),
        "event_type": record.get("eventType") or "general",
        "sentiment": record.get("sentiment") or "neutral",
        "impact_direction": record.get("impactDirection") or "neutral",
        "why_it_matters": record.get("whyItMatters") or "",
        "model": record.get("model") or "deterministic",
        "localized_at": clickhouse_time_or_none(record.get("localizedAt")) or clickhouse_time(None),
        "raw": json.dumps(record.get("raw") or record, ensure_ascii=False, separators=(",", ":")),
    }


def deterministic_news_intelligence(event):
    headline = str(event.get("headline") or "Untitled news")
    summary = str(event.get("summary") or event.get("content") or headline)
    text = f"{headline} {summary}".lower()
    sentiment = classify_sentiment(text)
    return {
        "localizedHeadline": headline,
        "localizedSummary": summary,
        "keyPoints": [summary[:220]] if summary else [],
        "positivePoints": [summary[:220]] if sentiment == "positive" and summary else [],
        "concerns": [summary[:220]] if sentiment == "negative" and summary else [],
        "eventType": classify_event_type(text),
        "sentiment": sentiment,
        "impactDirection": classify_impact_direction(text),
        "whyItMatters": summary[:300],
    }


def classify_event_type(text):
    if any(term in text for term in ("earnings", "revenue", "profit", "guidance", "실적", "매출", "이익", "가이던스")):
        return "earnings"
    if any(term in text for term in ("analyst", "upgrade", "downgrade", "rating", "price target", "목표가", "등급", "애널리스트")):
        return "analyst"
    if any(term in text for term in ("merger", "acquisition", "ipo", "m&a", "인수", "합병", "상장")):
        return "corporate-action"
    if any(term in text for term in ("lawsuit", "probe", "investigation", "regulator", "소송", "조사", "규제")):
        return "legal-regulatory"
    if any(term in text for term in ("product", "launch", "chip", "ai", "제품", "출시", "반도체")):
        return "product-market"
    return "general"


def classify_sentiment(text):
    if any(term in text for term in ("beats", "surge", "rally", "upgrade", "record", "positive", "호조", "급등", "상향", "긍정")):
        return "positive"
    if any(term in text for term in ("miss", "fall", "drop", "downgrade", "lawsuit", "risk", "negative", "하락", "악재", "소송", "리스크")):
        return "negative"
    return "neutral"


def classify_impact_direction(text):
    sentiment = classify_sentiment(text)
    if sentiment == "positive":
        return "positive"
    if sentiment == "negative":
        return "negative"
    return "neutral"


def normalize_event_type(value):
    normalized = str(value or "general").strip().lower()
    return normalized if normalized else "general"


def normalize_sentiment(value):
    normalized = str(value or "neutral").strip().lower()
    return normalized if normalized in {"positive", "negative", "neutral", "mixed"} else "neutral"


def normalize_impact_direction(value):
    normalized = str(value or "neutral").strip().lower()
    return normalized if normalized in {"positive", "negative", "neutral", "mixed"} else "neutral"


def normalized_symbols(values):
    result = []
    for value in values or []:
        symbol = str(value or "").strip().upper()
        if symbol and symbol not in result:
            result.append(symbol)
    return result


def float_or_zero(value):
    try:
        return float(value)
    except Exception:
        return 0.0


def clean_text(value, max_length):
    text = " ".join(str(value or "").split())
    return text[:max_length]


def clean_text_list(value, max_items, max_length):
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        values = []
    result = []
    for item in values:
        text = clean_text(item, max_length)
        if text:
            result.append(text)
        if len(result) >= max_items:
            break
    return result


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
