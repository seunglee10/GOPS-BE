from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any


CONTEXT_VERSION = "recommendation-narrative-context.v1"


def build_narrative_context(
    *,
    symbol: str,
    market_item: dict[str, Any],
    company_profile: dict[str, Any] | None,
    news: list[dict[str, Any]],
    raw_factors: dict[str, Any],
    cutoff: datetime,
) -> dict[str, Any]:
    profile = _profile_not_after(company_profile, cutoff)
    company = {
        "symbol": symbol,
        "companyName": _text((profile or {}).get("companyName"))
        or _text(market_item.get("companyName"))
        or _text(market_item.get("name"))
        or symbol,
        "sector": _text(market_item.get("sector")) or "Unclassified",
        "industry": _text(market_item.get("industry")) or "Unclassified",
    }
    ten_k = _bounded_profile(profile) if profile else None
    catalysts = [
        row
        for source in news
        if (row := _bounded_news(source, cutoff)) is not None
    ][:2]
    fundamentals = {
        key: value
        for key, source_key in (
            ("companyQuality", "companyQuality"),
            ("valueQuality", "valueQuality"),
            ("growthQuality", "growthQuality"),
            ("earningsRevisionQuality", "earningsRevisionQuality"),
        )
        if (value := _number(raw_factors.get(source_key))) is not None
    }
    context = {
        "version": CONTEXT_VERSION,
        "status": "ready" if ten_k else "partial",
        "company": company,
        "tenK": ten_k,
        "catalysts": catalysts,
        "fundamentals": fundamentals,
        "cutoff": _iso(cutoff),
    }
    context["digest"] = narrative_context_digest(context)
    return context


def narrative_context_digest(value: dict[str, Any]) -> str:
    payload = {key: child for key, child in value.items() if key != "digest"}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _profile_not_after(value: dict[str, Any] | None, cutoff: datetime) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    filing = _datetime(value.get("filingDate"))
    if filing is None or filing > _aware(cutoff):
        return None
    return value


def _bounded_profile(value: dict[str, Any]) -> dict[str, Any]:
    business = value.get("businessModel")
    if isinstance(business, dict):
        segments = []
        for source in business.get("segments") or []:
            if not isinstance(source, dict):
                continue
            name = _text(source.get("name"), 40)
            detail = _text(source.get("detail"), 160)
            if name and detail:
                segments.append({"name": name, "detail": detail})
        business_model: dict[str, Any] | str = {
            "structure": _text(business.get("structure"), 160),
            "segments": segments[:3],
            "revenueModel": _text_list(business.get("revenueModel"), 3, 200),
            "platform": _text(business.get("platform"), 200) or None,
        }
    else:
        business_model = _text(business, 600)
    risks = []
    for source in value.get("riskFactors") or []:
        if not isinstance(source, dict):
            continue
        category = _text(source.get("category"), 40)
        summary = _text(source.get("summary"), 360)
        if category and summary:
            risks.append({
                "category": category,
                "summary": summary,
                "severityHint": _text(source.get("severityHint"), 16),
            })
    return {
        "sourceAccession": _text(value.get("sourceAccession"), 40),
        "sourceFiling": _text(value.get("sourceFiling"), 120),
        "sourceUrl": _text(value.get("sourceUrl"), 500) or None,
        "filingDate": _text(value.get("filingDate"), 32),
        "reportDate": _text(value.get("reportDate"), 32),
        "businessModel": business_model,
        "revenueDrivers": _text_list(value.get("revenueDrivers"), 4, 260),
        "competitivePosition": _text(value.get("competitivePosition"), 600),
        "riskFactors": risks[:3],
    }


def _bounded_news(value: dict[str, Any], cutoff: datetime) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    published = _datetime(
        value.get("publishedAt")
        or value.get("published_at")
        or value.get("createdAt")
        or value.get("eventTime")
        or value.get("timestamp")
    )
    available = _datetime(
        value.get("availableAt")
        or value.get("receivedAt")
        or value.get("received_at")
    ) or published
    if published is None or available is None or published > _aware(cutoff) or available > _aware(cutoff):
        return None
    headline = _text(value.get("headline") or value.get("title"), 240)
    summary = _text(value.get("summary") or value.get("content") or value.get("description"), 420)
    if not headline and not summary:
        return None
    return {
        "id": _text(value.get("articleId") or value.get("id") or value.get("url"), 300),
        "headline": headline,
        "summary": summary,
        "publishedAt": _iso(published),
        "availableAt": _iso(available),
        "url": _text(value.get("url"), 500) or None,
    }


def _text_list(value: Any, limit: int, max_length: int) -> list[str]:
    result: list[str] = []
    for source in value if isinstance(value, list) else []:
        text = _text(source, max_length)
        if text and text not in result:
            result.append(text)
    return result[:limit]


def _text(value: Any, max_length: int = 200) -> str:
    text = " ".join(str(value or "").split()).strip()
    return text[:max_length]


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _aware(value)
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) == 10:
        text += "T00:00:00+00:00"
    try:
        return _aware(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return _aware(value).isoformat()
