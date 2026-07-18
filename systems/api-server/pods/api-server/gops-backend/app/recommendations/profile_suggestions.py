from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from statistics import median
from typing import Any

from gops_agents.query_understanding.korean_text import compact_text, query_fragments
from gops_agents.recommendation_profiles import build_score_profile_suggestion

from .score_profiles import normalize_score_profile_payload, public_score_profile, system_score_profile
from .service import RecommendationDataSource


def suggest_score_profile(app: Any, repository: Any, user_sub: str, query: str) -> dict[str, Any]:
    profile = repository.get_profile(user_sub)
    risk_level = str((profile or {}).get("risk_level") or "balanced")
    custom = repository.list_score_profiles(user_sub)
    active_id = (profile or {}).get("active_score_profile_id")
    base_profile = next((public_score_profile(row) for row in custom if row.get("id") == active_id), None)
    if base_profile is None:
        base_profile = system_score_profile(str((profile or {}).get("recommendation_style") or "balanced"), risk_level)

    now_provider = getattr(app.state, "recommendation_now_provider", None)
    now = now_provider() if callable(now_provider) else datetime.now(timezone.utc)
    run = repository.latest_run(user_sub)
    snapshot_id = int((run or {}).get("evidence_snapshot_id") or 0)
    snapshot_method = getattr(repository, "get_evidence_snapshot_by_id", None)
    snapshot = snapshot_method(snapshot_id) if snapshot_id and callable(snapshot_method) else None
    data_source = RecommendationDataSource(app)
    market_items = data_source.market_items()
    symbols = _evidence_symbols(run, snapshot, market_items, limit=60)
    news_by_symbol = data_source.news_for_symbols(symbols, now) if symbols else {}
    evidence_context = _evidence_context(query, run, snapshot, market_items, news_by_symbol, now)
    provider = getattr(app.state, "recommendation_profile_suggestion_provider", None)
    suggestion = build_score_profile_suggestion(
        query,
        base_profile=base_profile,
        evidence_context=evidence_context,
        provider=provider,
    )
    normalized = normalize_score_profile_payload(suggestion["profile"])
    suggestion["profile"].update({
        "blockWeights": normalized["blockWeights"],
        "factorWeights": normalized["factorWeights"],
        "portfolioWeight": normalized["portfolioWeight"],
        "portfolioFactorWeights": normalized["portfolioFactorWeights"],
    })
    return suggestion


def _evidence_context(
    query: str,
    run: dict[str, Any] | None,
    snapshot: dict[str, Any] | None,
    market_items: list[dict[str, Any]],
    news_by_symbol: dict[str, list[dict[str, Any]]],
    now: datetime,
) -> dict[str, Any]:
    candidates = [row for row in (snapshot or {}).get("candidates") or [] if isinstance(row, dict)]
    qualified = [row for row in candidates if not row.get("rejectionReasons")]
    reliability_values = [_number(row.get("evidenceReliability")) for row in candidates]
    reliability_values = [value for value in reliability_values if value is not None]
    block_medians = {
        block: round(median(values), 2)
        for block in (
            "trendStrength", "participationConfirmation", "priceStructure",
            "catalystQuality", "executionQuality", "qualityStability",
        )
        if (values := [
            value for row in candidates
            if (value := _number((row.get("blockScores") or {}).get(block))) is not None
        ])
    }
    news_rows = _rank_news(query, news_by_symbol, candidates, limit=24)
    news_as_of = max((str(row.get("publishedAt") or "") for row in news_rows), default=None) or None
    changes = [value for row in market_items if (value := _number(row.get("changePercent"))) is not None]
    sectors = Counter(str(row.get("sector") or "미분류") for row in market_items)
    catalyst_covered = sum(
        _number((row.get("rawFactors") or {}).get("catalystQuality")) is not None
        or bool((row.get("narrativeContext") or {}).get("catalysts"))
        for row in candidates
    )
    evidence_snapshot_id = (snapshot or {}).get("id") or (run or {}).get("evidence_snapshot_id")
    evidence_as_of = (snapshot or {}).get("cutoff") or (run or {}).get("market_snapshot_time")
    top_candidates = [
        {
            "symbol": str(item.get("symbol") or ""),
            "score": _number(item.get("customRankScore") or item.get("score")),
            "confidence": _number(item.get("confidence")),
            "sector": item.get("sector"),
            "blockScores": (item.get("metricsSnapshot") or {}).get("effectiveBlockScores")
            or (item.get("metricsSnapshot") or {}).get("blockScores") or {},
        }
        for item in (run or {}).get("items") or [] if isinstance(item, dict)
    ][:15]
    summary_lines = [
        f"최신 evidence snapshot에서 {len(candidates)}개 후보 중 {len(qualified)}개가 고정 gate를 통과했습니다.",
        f"후보 근거 신뢰도 중앙값은 {round(median(reliability_values), 1) if reliability_values else 0}점입니다.",
        f"현재 시장 상승 종목 비율은 {round(sum(value > 0 for value in changes) / len(changes) * 100, 1) if changes else 0}%입니다.",
        f"관련 최신 뉴스·촉매 문서 {len(news_rows)}건을 제안 근거로 검색했습니다.",
    ]
    evidence_refs = []
    if evidence_snapshot_id:
        evidence_refs.append(f"evidence-snapshot:{evidence_snapshot_id}")
    evidence_refs.extend(str(row["ref"]) for row in news_rows if row.get("ref"))
    digest_payload = {
        "snapshot": evidence_snapshot_id,
        "asOf": str(evidence_as_of or ""),
        "blockMedians": block_medians,
        "newsRefs": evidence_refs[1:],
        "marketCount": len(market_items),
    }
    return {
        "digest": hashlib.sha256(json.dumps(digest_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest(),
        "evidenceSnapshotId": evidence_snapshot_id,
        "evidenceAsOf": str(evidence_as_of or now.isoformat()),
        "newsAsOf": news_as_of,
        "summaryLines": summary_lines,
        "dataSignals": {
            "candidateCount": len(candidates),
            "qualifiedCount": len(qualified),
            "medianEvidenceReliability": round(median(reliability_values), 2) if reliability_values else 0,
            "newsCoverageRatio": round(catalyst_covered / len(candidates), 4) if candidates else 0,
            "advancingRatio": round(sum(value > 0 for value in changes) / len(changes), 4) if changes else 0,
            "medianChangePercent": round(median(changes), 4) if changes else None,
            "blockScoreMedians": block_medians,
            "sectorCounts": dict(sectors.most_common(8)),
            "sourceStatus": (snapshot or {}).get("sourceStatus") or {},
        },
        "topCandidates": top_candidates,
        "news": [{key: row.get(key) for key in ("ref", "symbol", "headline", "summary", "sentiment", "publishedAt")} for row in news_rows[:12]],
        "evidenceRefs": list(dict.fromkeys(evidence_refs))[:24],
    }


def _evidence_symbols(
    run: dict[str, Any] | None,
    snapshot: dict[str, Any] | None,
    market_items: list[dict[str, Any]],
    *,
    limit: int,
) -> list[str]:
    symbols: list[str] = []
    sources = [
        [row.get("symbol") for row in (run or {}).get("items") or [] if isinstance(row, dict)],
        [row.get("symbol") for row in (snapshot or {}).get("candidates") or [] if isinstance(row, dict)],
        [row.get("symbol") for row in market_items],
    ]
    for source in sources:
        for value in source:
            symbol = str(value or "").strip().upper()
            if symbol and symbol not in symbols:
                symbols.append(symbol)
            if len(symbols) >= limit:
                return symbols
    return symbols


def _rank_news(
    query: str,
    news_by_symbol: dict[str, list[dict[str, Any]]],
    candidates: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    query_compact = compact_text(query)
    fragments = set(query_fragments(query, min_length=2, max_length=12))
    rows: list[dict[str, Any]] = []
    for symbol, articles in news_by_symbol.items():
        for index, article in enumerate(articles or []):
            if not isinstance(article, dict):
                continue
            headline = _text(article.get("headline") or article.get("title"), 180)
            summary = _text(article.get("summary") or article.get("content"), 280)
            published_at = str(article.get("publishedAt") or article.get("published_at") or article.get("timestamp") or "")
            text = compact_text(f"{headline} {summary}")
            relevance = sum(1 for fragment in fragments if fragment and fragment in text)
            if query_compact and query_compact in text:
                relevance += 8
            rows.append({
                "ref": f"news:{symbol}:{published_at or index}",
                "symbol": symbol,
                "headline": headline,
                "summary": summary,
                "sentiment": article.get("sentiment"),
                "publishedAt": published_at,
                "relevance": relevance,
            })
    for candidate in candidates:
        symbol = str(candidate.get("symbol") or "")
        for index, catalyst in enumerate((candidate.get("narrativeContext") or {}).get("catalysts") or []):
            catalyst_row = catalyst if isinstance(catalyst, dict) else {"summary": catalyst}
            headline = _text(catalyst_row.get("headline") or catalyst_row.get("title") or "저장된 촉매", 180)
            summary = _text(catalyst_row.get("summary") or catalyst_row.get("text"), 280)
            text = compact_text(f"{headline} {summary}")
            relevance = sum(1 for fragment in fragments if fragment and fragment in text)
            rows.append({
                "ref": f"snapshot-catalyst:{symbol}:{index}",
                "symbol": symbol,
                "headline": headline,
                "summary": summary,
                "sentiment": catalyst_row.get("sentiment"),
                "publishedAt": str(catalyst_row.get("publishedAt") or candidate.get("evaluatedAt") or ""),
                "relevance": relevance,
            })
    deduplicated = {
        (row["symbol"], row["headline"], row["publishedAt"]): row
        for row in rows if row["headline"] or row["summary"]
    }
    ranked = sorted(deduplicated.values(), key=lambda row: (-int(row["relevance"]), str(row["publishedAt"])), reverse=False)
    return ranked[:limit]


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None
