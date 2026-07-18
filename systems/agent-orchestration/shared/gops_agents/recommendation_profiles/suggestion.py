from __future__ import annotations

import hashlib
import json
import math
import os
import re
import urllib.request
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from gops_agents.query_understanding.korean_text import compact_text, normalize_query_text, query_fragments


PROFILE_SUGGESTION_SCHEMA_VERSION = "recommendation-score-suggestion.v1"
PROFILE_SCHEMA_VERSION = "recommendation-score-profile.v1"
PROMPT_VERSION = "recommendation-score-profile-rag.ko.v1"

BLOCK_KEYS = (
    "trendStrength",
    "participationConfirmation",
    "priceStructure",
    "catalystQuality",
    "executionQuality",
    "qualityStability",
)
FACTOR_KEYS = {
    "trendStrength": (
        "currentSessionRelativeStrength", "last60MinuteRelativeStrength", "oneDayRelativeStrength",
        "fiveDayRelativeStrength", "high52WeekProximity",
    ),
    "participationConfirmation": (
        "clockAdjustedVolumeRatio", "abnormalDollarVolume", "closingLocationValue", "participationPersistence",
    ),
    "priceStructure": ("confirmedBreakoutSupport", "vwapHoldQuality", "higherLowQuality", "gapAcceptance"),
    "catalystQuality": ("catalystQuality",),
    "executionQuality": ("medianDollarVolume", "quotedSpreadBps", "freshnessScore"),
    "qualityStability": (
        "realizedVolatility", "downsideVolatility", "valueQuality", "companyQuality",
        "growthQuality", "earningsRevisionQuality",
    ),
}
PORTFOLIO_FACTOR_KEYS = (
    "sectorDiversification", "correlationBenefit", "marginalVariance", "liquidityCashCompatibility",
)

INTENT_DOCUMENTS: tuple[dict[str, Any], ...] = (
    {
        "id": "fast-momentum",
        "title": "단기 모멘텀과 급등 지속",
        "keywords": ("급등", "모멘텀", "강한 추세", "추세 추종", "상승 탄력", "단기 강세", "momentum"),
        "reason": "현재와 최근 60분 상대강도, 거래 참여 지속성을 더 크게 반영합니다.",
        "blockDelta": {"trendStrength": 14, "participationConfirmation": 8, "qualityStability": -12, "executionQuality": -5},
        "factorDelta": {
            "trendStrength": {"currentSessionRelativeStrength": 10, "last60MinuteRelativeStrength": 8, "fiveDayRelativeStrength": -8},
            "participationConfirmation": {"participationPersistence": 8, "clockAdjustedVolumeRatio": 5},
        },
    },
    {
        "id": "volume-liquidity",
        "title": "거래대금과 유동성 확인",
        "keywords": ("거래대금", "거래량", "수급", "유동성", "체결", "스프레드", "volume", "liquidity"),
        "reason": "비정상 거래대금과 시간 보정 거래량, 체결 가능성을 우선 확인합니다.",
        "blockDelta": {"participationConfirmation": 12, "executionQuality": 9, "catalystQuality": -5, "qualityStability": -4},
        "factorDelta": {
            "participationConfirmation": {"abnormalDollarVolume": 12, "clockAdjustedVolumeRatio": 8},
            "executionQuality": {"medianDollarVolume": 10, "quotedSpreadBps": 7},
        },
    },
    {
        "id": "breakout-structure",
        "title": "돌파와 가격 구조",
        "keywords": ("돌파", "지지", "vwap", "갭", "안착", "저점 상승", "가격 구조", "breakout"),
        "reason": "돌파 지지, VWAP 유지와 저점 상승 같은 가격 구조를 우선합니다.",
        "blockDelta": {"priceStructure": 18, "trendStrength": 5, "catalystQuality": -5, "qualityStability": -7},
        "factorDelta": {
            "priceStructure": {"confirmedBreakoutSupport": 12, "vwapHoldQuality": 8, "higherLowQuality": 5},
        },
    },
    {
        "id": "news-catalyst",
        "title": "뉴스와 실적 촉매",
        "keywords": ("뉴스", "실적", "발표", "가이던스", "상향", "이벤트", "촉매", "earnings", "news"),
        "reason": "최신 뉴스와 실적 전망 변화가 확인되는 종목의 촉매 품질을 높게 봅니다.",
        "blockDelta": {"catalystQuality": 20, "qualityStability": 5, "trendStrength": -6, "priceStructure": -5},
        "factorDelta": {
            "qualityStability": {"earningsRevisionQuality": 14, "growthQuality": 7},
        },
    },
    {
        "id": "quality-value",
        "title": "기업 품질과 가치",
        "keywords": ("가치", "저평가", "기업 품질", "재무", "성장", "장기", "실적 전망", "quality", "value"),
        "reason": "가치, 기업 품질, 성장과 실적 전망을 가격 신호보다 안정적으로 반영합니다.",
        "blockDelta": {"qualityStability": 22, "executionQuality": 4, "trendStrength": -9, "participationConfirmation": -6},
        "factorDelta": {
            "qualityStability": {"valueQuality": 12, "companyQuality": 10, "growthQuality": 6, "earningsRevisionQuality": 6},
        },
    },
    {
        "id": "defensive-volatility",
        "title": "저변동성과 하방 방어",
        "keywords": ("안정", "보수", "저변동", "하방", "변동성", "방어", "리스크", "손실", "defensive", "stable"),
        "reason": "실현·하방 변동성과 체결 품질, 포트폴리오 위험 적합도를 우선합니다.",
        "blockDelta": {"qualityStability": 18, "executionQuality": 12, "trendStrength": -10, "catalystQuality": -6},
        "factorDelta": {
            "qualityStability": {"realizedVolatility": 12, "downsideVolatility": 15},
            "executionQuality": {"quotedSpreadBps": 8, "freshnessScore": 5},
        },
        "portfolioDelta": 12,
        "portfolioFactorDelta": {"marginalVariance": 12, "liquidityCashCompatibility": 7},
    },
    {
        "id": "portfolio-diversification",
        "title": "포트폴리오 분산",
        "keywords": ("분산", "섹터 분산", "상관", "집중", "포트폴리오", "현금", "비중", "diversification"),
        "reason": "섹터 집중과 상관 개선, 한계 변동성을 추천 점수에 더 반영합니다.",
        "blockDelta": {"qualityStability": 7, "executionQuality": 4, "trendStrength": -4},
        "portfolioDelta": 20,
        "portfolioFactorDelta": {"sectorDiversification": 12, "correlationBenefit": 12, "marginalVariance": 8},
    },
)


def retrieve_score_intents(query: str, *, limit: int = 4) -> list[dict[str, Any]]:
    normalized = normalize_query_text(query)
    compact = compact_text(normalized)
    fragments = set(query_fragments(normalized, min_length=2, max_length=12))
    ranked: list[tuple[float, dict[str, Any], list[str]]] = []
    for document in INTENT_DOCUMENTS:
        matched: list[str] = []
        score = 0.0
        for keyword in document["keywords"]:
            keyword_normalized = normalize_query_text(keyword)
            keyword_compact = compact_text(keyword_normalized)
            if keyword_normalized in normalized or (keyword_compact and keyword_compact in compact):
                matched.append(keyword)
                score += 4.0 + min(len(keyword_compact), 8) / 8
            elif keyword_compact in fragments:
                matched.append(keyword)
                score += 2.0
        if score > 0:
            ranked.append((score, document, matched))
    ranked.sort(key=lambda row: (-row[0], str(row[1]["id"])))
    return [
        {
            "id": document["id"],
            "title": document["title"],
            "reason": document["reason"],
            "matchedKeywords": matched,
            "score": round(score, 4),
            "blockDelta": deepcopy(document.get("blockDelta") or {}),
            "factorDelta": deepcopy(document.get("factorDelta") or {}),
            "portfolioDelta": float(document.get("portfolioDelta") or 0),
            "portfolioFactorDelta": deepcopy(document.get("portfolioFactorDelta") or {}),
        }
        for score, document, matched in ranked[: max(1, limit)]
    ]


def build_score_profile_suggestion(
    query: str,
    *,
    base_profile: dict[str, Any],
    evidence_context: dict[str, Any],
    provider: Any = None,
) -> dict[str, Any]:
    intents = retrieve_score_intents(query)
    deterministic = _deterministic_profile(query, base_profile, intents, evidence_context)
    proposal = {
        "name": deterministic["name"],
        "rationale": _deterministic_rationale(intents, evidence_context),
        "confidence": 0.58 if intents else 0.42,
        "evidenceRefs": list(evidence_context.get("evidenceRefs") or [])[:8],
        "profile": deterministic["profile"],
    }
    source = "deterministic"
    model: str | None = None
    fallback_reason: str | None = None
    if provider is not None or os.getenv("OPENAI_API_KEY"):
        try:
            model = os.getenv("RECOMMENDATION_PROFILE_SUGGESTION_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-5.2"
            llm_payload = _call_provider(
                _provider_payload(query, intents, evidence_context, deterministic["profile"], model),
                provider=provider,
            )
            proposal = _validated_llm_proposal(llm_payload, deterministic["profile"], evidence_context)
            source = "llm"
        except Exception as exc:  # provider failure must not mutate the active profile
            fallback_reason = exc.__class__.__name__
            if _bool_env("RECOMMENDATION_PROFILE_SUGGESTION_LLM_REQUIRED", False):
                raise

    generated_at = datetime.now(timezone.utc).isoformat()
    retrieval_payload = {
        "query": normalize_query_text(query),
        "intentIds": [row["id"] for row in intents],
        "evidenceDigest": evidence_context.get("digest"),
        "profile": proposal["profile"],
    }
    retrieval_digest = hashlib.sha256(
        json.dumps(retrieval_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "schemaVersion": PROFILE_SUGGESTION_SCHEMA_VERSION,
        "query": query.strip(),
        "name": _clean_name(proposal.get("name"), query),
        "rationale": str(proposal.get("rationale") or "현재 근거와 입력 의도를 함께 반영한 가중치 초안입니다.").strip(),
        "confidence": round(_clamp_number(proposal.get("confidence"), 0, 1, default=0.5), 4),
        "intent": {
            "matchedKeywords": list(dict.fromkeys(keyword for row in intents for keyword in row["matchedKeywords"])),
            "documents": [{key: row[key] for key in ("id", "title", "reason", "matchedKeywords")} for row in intents],
        },
        "profile": {
            "type": "custom",
            "id": None,
            "name": _clean_name(proposal.get("name"), query),
            "revision": 0,
            "schemaVersion": PROFILE_SCHEMA_VERSION,
            **proposal["profile"],
        },
        "evidence": {
            "summary": list(evidence_context.get("summaryLines") or [])[:6],
            "news": list(evidence_context.get("news") or [])[:8],
        },
        "provenance": {
            "source": source,
            "model": model if source == "llm" else None,
            "promptVersion": PROMPT_VERSION,
            "generatedAt": generated_at,
            "evidenceSnapshotId": evidence_context.get("evidenceSnapshotId"),
            "evidenceAsOf": evidence_context.get("evidenceAsOf"),
            "newsAsOf": evidence_context.get("newsAsOf"),
            "retrievalDigest": retrieval_digest,
            "evidenceRefs": _allowed_evidence_refs(proposal.get("evidenceRefs"), evidence_context),
            "fallbackReason": fallback_reason,
        },
    }


def _deterministic_profile(
    query: str,
    base_profile: dict[str, Any],
    intents: list[dict[str, Any]],
    evidence_context: dict[str, Any],
) -> dict[str, Any]:
    blocks = _complete_group(base_profile.get("blockWeights"), BLOCK_KEYS)
    factors = {
        block: _complete_group((base_profile.get("factorWeights") or {}).get(block), keys)
        for block, keys in FACTOR_KEYS.items()
    }
    portfolio_factors = _complete_group(base_profile.get("portfolioFactorWeights"), PORTFOLIO_FACTOR_KEYS)
    portfolio_weight = _clamp_number(base_profile.get("portfolioWeight"), 0, 100, default=25)
    for index, intent in enumerate(intents):
        strength = max(0.35, 1 - index * 0.2)
        blocks = _apply_deltas(blocks, intent.get("blockDelta") or {}, strength)
        for block, delta in (intent.get("factorDelta") or {}).items():
            if block in factors and isinstance(delta, dict):
                factors[block] = _apply_deltas(factors[block], delta, strength)
        portfolio_weight += float(intent.get("portfolioDelta") or 0) * strength
        portfolio_factors = _apply_deltas(portfolio_factors, intent.get("portfolioFactorDelta") or {}, strength)

    data_signals = evidence_context.get("dataSignals") if isinstance(evidence_context.get("dataSignals"), dict) else {}
    if float(data_signals.get("newsCoverageRatio") or 0) >= 0.2:
        blocks = _apply_deltas(blocks, {"catalystQuality": 2}, 1)
    if float(data_signals.get("medianEvidenceReliability") or 100) < 70:
        blocks = _apply_deltas(blocks, {"executionQuality": 2, "qualityStability": 2}, 1)
        portfolio_weight += 3
    return {
        "name": _clean_name(None, query),
        "profile": {
            "blockWeights": _normalize_group(blocks, BLOCK_KEYS),
            "factorWeights": {block: _normalize_group(values, FACTOR_KEYS[block]) for block, values in factors.items()},
            "portfolioWeight": round(max(0, min(100, portfolio_weight)), 2),
            "portfolioFactorWeights": _normalize_group(portfolio_factors, PORTFOLIO_FACTOR_KEYS),
        },
    }


def _provider_payload(query: str, intents: list[dict[str, Any]], evidence: dict[str, Any], seed: dict[str, Any], model: str) -> dict[str, Any]:
    allowed_refs = list(evidence.get("evidenceRefs") or [])[:24]
    context = {
        "userQuery": query,
        "retrievedIntentDocuments": [
            {key: row[key] for key in ("id", "title", "reason", "matchedKeywords")}
            for row in intents
        ],
        "latestEvidence": {
            key: evidence.get(key)
            for key in ("evidenceSnapshotId", "evidenceAsOf", "summaryLines", "dataSignals", "topCandidates", "news")
        },
        "allowedEvidenceRefs": allowed_refs,
        "deterministicSeed": seed,
    }
    return {
        "model": model,
        "instructions": (
            "당신은 GOPS 추천 점수 프로필 설계기입니다. 사용자의 표현 의도와 제공된 최신 immutable evidence/news 요약만 사용해 "
            "가중치 초안을 제안하세요. hard gate, 데이터 신뢰도 계산, soft penalty, 직접 매수 조건은 절대 변경하지 마세요. "
            "각 blockWeights, 각 factorWeights 그룹, portfolioFactorWeights 합계는 정확히 100이어야 하고 값은 0~100, 소수 둘째 자리까지입니다. "
            "portfolioWeight만 독립적인 0~100 반영률입니다. 현재 데이터가 강하다는 이유만으로 해당 신호를 무조건 추종하지 말고 사용자 의도를 우선하되, "
            "근거의 커버리지와 신선도를 이용해 과도한 비중을 피하세요. evidenceRefs는 allowedEvidenceRefs에서만 고르세요. 한국어로 간결하게 작성하세요."
        ),
        "input": json.dumps(context, ensure_ascii=False, separators=(",", ":")),
        "text": {"format": {"type": "json_schema", "name": "score_profile_suggestion", "strict": True, "schema": _response_schema(allowed_refs)}},
    }


def _response_schema(allowed_refs: list[str]) -> dict[str, Any]:
    def weight_group(keys: tuple[str, ...]) -> dict[str, Any]:
        return {
            "type": "object", "additionalProperties": False,
            "properties": {key: {"type": "number", "minimum": 0, "maximum": 100} for key in keys},
            "required": list(keys),
        }

    return {
        "type": "object", "additionalProperties": False,
        "properties": {
            "name": {"type": "string", "minLength": 1, "maxLength": 40},
            "rationale": {"type": "string", "minLength": 1, "maxLength": 600},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "evidenceRefs": {"type": "array", "items": {"type": "string", "enum": allowed_refs or ["none"]}, "maxItems": 8},
            "profile": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "blockWeights": weight_group(BLOCK_KEYS),
                    "factorWeights": {
                        "type": "object", "additionalProperties": False,
                        "properties": {block: weight_group(keys) for block, keys in FACTOR_KEYS.items()},
                        "required": list(BLOCK_KEYS),
                    },
                    "portfolioWeight": {"type": "number", "minimum": 0, "maximum": 100},
                    "portfolioFactorWeights": weight_group(PORTFOLIO_FACTOR_KEYS),
                },
                "required": ["blockWeights", "factorWeights", "portfolioWeight", "portfolioFactorWeights"],
            },
        },
        "required": ["name", "rationale", "confidence", "evidenceRefs", "profile"],
    }


def _call_provider(payload: dict[str, Any], *, provider: Any) -> dict[str, Any]:
    if callable(provider):
        result = provider(deepcopy(payload))
    else:
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY is not configured")
        request = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            method="POST",
        )
        timeout = float(os.getenv("RECOMMENDATION_PROFILE_SUGGESTION_TIMEOUT_SECONDS", "12"))
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, dict):
        raise ValueError("invalid score profile suggestion response")
    if isinstance(result.get("profile"), dict):
        return result
    output_text = result.get("output_text")
    if not isinstance(output_text, str):
        texts = [
            content.get("text")
            for output in result.get("output") or [] if isinstance(output, dict)
            for content in output.get("content") or [] if isinstance(content, dict) and content.get("type") == "output_text"
        ]
        output_text = "".join(text for text in texts if isinstance(text, str))
    if not output_text:
        raise ValueError("score profile suggestion response has no output text")
    parsed = json.loads(output_text)
    if not isinstance(parsed, dict):
        raise ValueError("score profile suggestion output must be an object")
    return parsed


def _validated_llm_proposal(value: dict[str, Any], seed: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    source_profile = value.get("profile") if isinstance(value.get("profile"), dict) else {}
    block_source = source_profile.get("blockWeights") if isinstance(source_profile.get("blockWeights"), dict) else seed["blockWeights"]
    factor_source = source_profile.get("factorWeights") if isinstance(source_profile.get("factorWeights"), dict) else seed["factorWeights"]
    portfolio_source = source_profile.get("portfolioFactorWeights") if isinstance(source_profile.get("portfolioFactorWeights"), dict) else seed["portfolioFactorWeights"]
    profile = {
        "blockWeights": _normalize_group(_complete_group(block_source, BLOCK_KEYS), BLOCK_KEYS),
        "factorWeights": {
            block: _normalize_group(_complete_group(factor_source.get(block), keys), keys)
            for block, keys in FACTOR_KEYS.items()
        },
        "portfolioWeight": round(_clamp_number(source_profile.get("portfolioWeight"), 0, 100, default=seed["portfolioWeight"]), 2),
        "portfolioFactorWeights": _normalize_group(_complete_group(portfolio_source, PORTFOLIO_FACTOR_KEYS), PORTFOLIO_FACTOR_KEYS),
    }
    return {
        "name": value.get("name"),
        "rationale": value.get("rationale"),
        "confidence": value.get("confidence"),
        "evidenceRefs": _allowed_evidence_refs(value.get("evidenceRefs"), evidence),
        "profile": profile,
    }


def _complete_group(value: Any, keys: tuple[str, ...]) -> dict[str, float]:
    source = value if isinstance(value, dict) else {}
    fallback = 100 / len(keys)
    return {key: _clamp_number(source.get(key), 0, 100, default=fallback) for key in keys}


def _apply_deltas(values: dict[str, float], deltas: dict[str, Any], strength: float) -> dict[str, float]:
    return _normalize_group({
        key: max(0.01, value + _clamp_number(deltas.get(key), -100, 100, default=0) * strength)
        for key, value in values.items()
    }, tuple(values))


def _normalize_group(values: dict[str, float], keys: tuple[str, ...]) -> dict[str, float]:
    safe = {key: max(0, float(values.get(key) or 0)) for key in keys}
    total = sum(safe.values())
    if total <= 0:
        safe = {key: 1 for key in keys}
        total = float(len(keys))
    normalized = {key: round(safe[key] / total * 100, 2) for key in keys}
    difference = round(100 - sum(normalized.values()), 2)
    anchor = max(keys, key=lambda key: (normalized[key], -keys.index(key)))
    normalized[anchor] = round(normalized[anchor] + difference, 2)
    return normalized


def _deterministic_rationale(intents: list[dict[str, Any]], evidence: dict[str, Any]) -> str:
    intent_text = " ".join(row["reason"] for row in intents[:3]) if intents else "입력 문장에서 특정 신호가 뚜렷하지 않아 현재 활성 로직을 기준으로 구성했습니다."
    summary = list(evidence.get("summaryLines") or [])
    return f"{intent_text}{' ' + summary[0] if summary else ''}".strip()


def _allowed_evidence_refs(value: Any, evidence: dict[str, Any]) -> list[str]:
    allowed = list(evidence.get("evidenceRefs") or [])
    requested = [str(item) for item in value or []] if isinstance(value, list) else []
    selected = [item for item in requested if item in allowed]
    return list(dict.fromkeys(selected or allowed[:3]))


def _clean_name(value: Any, query: str) -> str:
    name = re.sub(r"\s+", " ", str(value or "").strip())
    if not name:
        words = re.findall(r"[a-zA-Z0-9가-힣]+", query)
        name = " ".join(words[:4]) or "AI 추천 로직"
    if not name.endswith(" 로직"):
        name = f"{name} 로직"
    return name[:40].strip()


def _clamp_number(value: Any, minimum: float, maximum: float, *, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(number):
        return float(default)
    return max(minimum, min(maximum, number))


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}
