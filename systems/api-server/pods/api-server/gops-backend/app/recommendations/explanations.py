from __future__ import annotations

import json
import logging
import os
import re
import urllib.request
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any


EXPLANATION_VERSION = "recommendation-explanation.v1"
PROMPT_VERSION = "recommendation-narrative.ko.v2"
DETERMINISTIC_RENDERER_VERSION = "recommendation-grounded-renderer.ko.v1"
CONFIDENCE_MEANING = "evidence_reliability_not_success_probability"
LOGGER = logging.getLogger(__name__)

BLOCKS = (
    ("trendStrength", "trend_strength", "추세 강도"),
    ("participationConfirmation", "participation_confirmation", "거래 참여 확인"),
    ("priceStructure", "price_structure", "가격 구조"),
    ("catalystQuality", "catalyst_quality", "촉매 품질"),
    ("executionQuality", "execution_quality", "체결 품질"),
    ("qualityStability", "quality_stability", "품질 안정성"),
)

PENALTY_LABELS = {
    "overextension": "가격 과열",
    "volatilityMismatch": "변동성 부적합",
    "weakConfirmation": "근거 불일치",
    "limitedPortfolioEvidence": "포트폴리오 근거 부족",
    "concentrationProximity": "집중도 한도 근접",
}
OPTIONAL_LABELS = {
    "catalystQuality": "촉매 품질",
    "valueQuality": "가치 품질",
    "companyQuality": "기업 품질",
    "growthQuality": "성장 품질",
    "earningsRevisionQuality": "실적 추정 변화",
}


def compose_explanations(
    items: list[dict[str, Any]],
    *,
    provider: Any = None,
    context: dict[str, Any] | None = None,
    required: bool = False,
) -> list[dict[str, Any]]:
    """Attach authoritative deterministic explanations and optional audited prose."""
    composed = [dict(item, explanation=deterministic_explanation(item)) for item in items]
    if not composed:
        return composed
    if os.getenv("RECOMMENDATION_NARRATIVE_PROVIDER", "deterministic").strip().lower() != "openai":
        if required:
            raise RuntimeError("OpenAI recommendation narrative is required")
        return composed
    try:
        narratives = _generate_narratives(composed, provider=provider, context=context)
    except Exception as exc:
        if required:
            raise
        LOGGER.warning("recommendation narrative fallback: %s", exc.__class__.__name__)
        return composed
    generated_at = datetime.now(timezone.utc).isoformat()
    model = os.getenv("RECOMMENDATION_NARRATIVE_MODEL") or os.getenv("OPENAI_MODEL") or ""
    for item in composed:
        narrative = narratives.get(str(item.get("symbol") or ""))
        if narrative is None:
            continue
        primary = item["explanation"]["primary"]
        primary.update({
            "source": "llm",
            "status": "ready",
            "headline": narrative["headline"],
            "body": narrative["body"],
            "model": model,
            "promptVersion": PROMPT_VERSION,
            "generatedAt": generated_at,
        })
    return composed


def deterministic_explanation(item: dict[str, Any]) -> dict[str, Any]:
    metrics = item.get("metricsSnapshot") or item.get("metrics_snapshot") or {}
    scores = metrics.get("blockScores") or {}
    contributions = metrics.get("blockContributions") or {}
    evidence = []
    for key, code, label in BLOCKS:
        score = _number(scores.get(key))
        contribution = _number(contributions.get(key))
        if score is None or contribution is None:
            continue
        if score >= 70:
            effect = "순위 상승을 뒷받침했습니다"
        elif score >= 55:
            effect = "순위에 제한적으로 긍정 기여했습니다"
        elif score >= 45:
            effect = "방향성이 뚜렷하지 않아 영향이 중립적이었습니다"
        else:
            effect = "근거가 약해 최종 평가를 제한했습니다"
        evidence.append({
            "code": code,
            "label": label,
            "sentence": f"{label}는 {score:.1f}/100으로 {effect}.",
            "score": score,
            "contribution": contribution,
        })

    penalties = metrics.get("softPenalties") or {}
    risks = []
    for key, value in penalties.items():
        penalty = _number(value)
        if penalty is None or penalty <= 0:
            continue
        label = PENALTY_LABELS.get(str(key), str(key))
        risks.append({
            "code": _snake_case(str(key)) + "_penalty",
            "sentence": f"{label} 요인으로 종합 점수가 {penalty:.1f}점 낮아졌습니다.",
            "penalty": penalty,
        })

    missing = list(metrics.get("missingOptionalFactors") or [])
    reliability = _number(metrics.get("evidenceReliability"))
    reliability = reliability if reliability is not None else round((_number(item.get("confidence")) or 0) * 100, 4)
    stale = bool(metrics.get("stale", False))
    cutoff = metrics.get("cutoff")
    data_sentence = (
        f"근거 신뢰도는 {reliability:.1f}/100이며, 이는 수익 성공 확률이 아니라 "
        "사용된 데이터의 완전성과 신선도를 나타냅니다."
    )
    if missing:
        data_sentence += " 확인되지 않은 선택 근거는 " + ", ".join(
            OPTIONAL_LABELS.get(value, value) for value in missing
        ) + "입니다."
    if stale:
        data_sentence += " 일부 입력이 허용된 신선도 기준을 벗어났습니다."

    ranked = sorted(evidence, key=lambda row: (row["score"], row["contribution"]), reverse=True)
    strongest = ranked[0] if ranked else None
    weakest = ranked[-1] if ranked else None
    if strongest and weakest and strongest["code"] != weakest["code"]:
        summary = (
            f"{strongest['label']} 근거가 순위를 가장 강하게 뒷받침했지만, "
            f"{weakest['label']} 근거가 상대적으로 약해 최종 점수가 제한되었습니다."
        )
    elif strongest:
        summary = f"{strongest['label']} 근거가 현재 평가를 뒷받침했습니다."
    else:
        summary = "평가에 필요한 결정론적 근거가 충분히 구성되지 않았습니다."
    if risks:
        summary += " " + risks[0]["sentence"]

    headline, body = grounded_primary_narrative(
        item,
        metrics=metrics,
        strongest=strongest,
        weakest=weakest,
        reliability=reliability,
        risks=risks,
    )
    return {
        "version": EXPLANATION_VERSION,
        "locale": "ko-KR",
        "decisionLabel": "매수 관찰",
        "primary": {
            "source": "deterministic",
            "status": "ready",
            "headline": headline,
            "body": body,
            "model": None,
            "promptVersion": DETERMINISTIC_RENDERER_VERSION,
            "generatedAt": datetime.now(timezone.utc).isoformat(),
        },
        "deterministic": {
            "summary": summary,
            "evidence": evidence,
            "risks": risks,
            "dataQuality": {
                "sentence": data_sentence,
                "evidenceReliability": reliability,
                "confidenceMeaning": CONFIDENCE_MEANING,
                "cutoff": cutoff,
                "missingFactors": missing,
                "stale": stale,
            },
        },
        "provenance": {
            "algorithmVersion": metrics.get("algorithmVersion"),
            "ruleSetVersion": metrics.get("ruleSetVersion"),
            "evidenceSnapshotId": str(metrics.get("evidenceSnapshotId") or ""),
            "inputDigest": str(metrics.get("inputDigest") or ""),
        },
    }


def grounded_primary_narrative(
    item: dict[str, Any],
    *,
    metrics: dict[str, Any],
    strongest: dict[str, Any] | None,
    weakest: dict[str, Any] | None,
    reliability: float,
    risks: list[dict[str, Any]],
) -> tuple[str, str]:
    symbol = str(item.get("symbol") or "종목")
    raw = metrics.get("rawFactors") or {}
    current_relative = _number(raw.get("currentSessionRelativeStrength"))
    last_hour_relative = _number(raw.get("last60MinuteRelativeStrength"))
    volume_ratio = _number(raw.get("clockAdjustedVolumeRatio"))
    spread = _number(raw.get("quotedSpreadBps"))
    score = _number(item.get("score")) or 0.0

    relative_parts = []
    if current_relative is not None:
        direction = "높았고" if current_relative >= 0 else "낮았고"
        relative_parts.append(f"정규장 수익률은 SPY보다 {abs(current_relative):.2f}%p {direction}")
    if last_hour_relative is not None:
        direction = "강했습니다" if last_hour_relative >= 0 else "약했습니다"
        relative_parts.append(f"마지막 60분 상대강도는 {last_hour_relative:+.2f}%p로 {direction}")
    first = "7월 14일 마감 기준, " + (
        " ".join(relative_parts) + "."
        if relative_parts
        else "SPY 비교를 포함한 가격 근거를 평가했습니다."
    )

    evidence_parts = []
    if volume_ratio is not None:
        evidence_parts.append(f"동시간대 거래량은 직전 정규장의 {volume_ratio:.2f}배였습니다.")
    if strongest:
        evidence_parts.append(f"가장 강한 근거는 {strongest['label']}({strongest['score']:.1f}점)이었습니다.")
    if weakest and (not strongest or weakest["code"] != strongest["code"]):
        evidence_parts.append(
            f"반면 상대적으로 약한 근거는 {weakest['label']}({weakest['score']:.1f}점)이었습니다."
        )
    second = " ".join(evidence_parts) if evidence_parts else "근거 블록의 강약을 함께 비교했습니다."

    execution = f"호가 스프레드는 {spread:.2f}bp였습니다. " if spread is not None else ""
    risk_text = f" {risks[0]['sentence']}" if risks else ""
    third = (
        f"{execution}종합 점수는 {score:.1f}점, 근거 신뢰도는 {reliability:.1f}점입니다."
        f"{risk_text} 이는 성공확률이 아니라 7월 15일 매수 관찰 우선순위입니다."
    )
    headline_basis = strongest["label"] if strongest else "구조화 근거"
    return f"{symbol}, {headline_basis} 중심의 7월 15일 관찰 후보", " ".join((first, second, third))


def _generate_narratives(
    items: list[dict[str, Any]],
    *,
    provider: Any = None,
    context: dict[str, Any] | None = None,
) -> dict[str, dict[str, str]]:
    inputs = [
        {
            "symbol": item["symbol"],
            "score": item["score"],
            "deterministic": item["explanation"]["deterministic"],
        }
        for item in items
    ]
    payload = {
        "model": os.getenv("RECOMMENDATION_NARRATIVE_MODEL") or os.getenv("OPENAI_MODEL"),
        "store": False,
        "input": [
            {
                "role": "system",
                "content": (
                    "결정론적 주식 추천 근거를 자연스러운 한국어로만 설명한다. 추천 판단과 수치는 "
                    "입력의 결정론적 V3가 전적으로 소유한다. 순위, 점수, 벌점, 신뢰도를 바꾸거나 "
                    "새 숫자·새 주장·성공확률·직접 주문 지시를 만들지 않는다. 이 결과는 예측 확정이 "
                    "아닌 매수 관찰 목록이다. 역사 재구성 문맥이 있으면 실시간 추천처럼 표현하지 않는다. "
                    "각 종목은 짧은 제목과 2~3문장 본문으로 작성한다."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {"context": context or {}, "items": inputs},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "recommendation_narratives",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "narratives": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "symbol": {"type": "string"},
                                    "headline": {"type": "string"},
                                    "body": {"type": "string"},
                                },
                                "required": ["symbol", "headline", "body"],
                                "additionalProperties": False,
                            },
                        }
                    },
                    "required": ["narratives"],
                    "additionalProperties": False,
                },
            }
        },
    }
    response = _call_provider(payload, provider=provider)
    parsed = json.loads(_response_output_text(response))
    rows = parsed.get("narratives") if isinstance(parsed, dict) else None
    if not isinstance(rows, list):
        raise ValueError("missing narrative list")
    expected = {str(item["symbol"]): item for item in items}
    validated: dict[str, dict[str, str]] = {}
    for row in rows:
        if not isinstance(row, dict) or str(row.get("symbol")) not in expected:
            raise ValueError("unexpected narrative symbol")
        symbol = str(row["symbol"])
        headline = str(row.get("headline") or "").strip()
        body = str(row.get("body") or "").strip()
        _validate_narrative(headline, body, expected[symbol])
        validated[symbol] = {"headline": headline, "body": body}
    if set(validated) != set(expected):
        raise ValueError("incomplete narrative batch")
    return validated


def _call_provider(payload: dict[str, Any], *, provider: Any) -> dict[str, Any]:
    if callable(provider):
        result = provider(deepcopy(payload))
        if not isinstance(result, dict):
            raise ValueError("invalid narrative provider response")
        return result
    key = os.getenv("OPENAI_API_KEY")
    if not key or not payload.get("model"):
        raise RuntimeError("OpenAI narrative configuration unavailable")
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    timeout = float(os.getenv("RECOMMENDATION_NARRATIVE_TIMEOUT_SECONDS", "3.5"))
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _response_output_text(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    texts = []
    for output in payload.get("output") or []:
        for content in output.get("content") or []:
            if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                texts.append(content["text"])
    if not texts:
        raise ValueError("Responses API returned no output text")
    return "".join(texts)


def _validate_narrative(headline: str, body: str, source: dict[str, Any]) -> None:
    if not headline or not body or not re.search(r"[가-힣]", headline + body):
        raise ValueError("narrative must be Korean")
    sentences = [row for row in re.split(r"(?<=[.!?])\s+", body) if row.strip()]
    if len(sentences) not in {2, 3}:
        raise ValueError("narrative body must contain two or three sentences")
    forbidden = ("성공 확률", "수익 확률", "보장", "매수하세요", "사세요", "주문하세요", "체결하세요")
    if any(value in headline + body for value in forbidden):
        raise ValueError("unsupported recommendation language")
    source_text = json.dumps(source["explanation"]["deterministic"], ensure_ascii=False)
    source_numbers = {float(value) for value in re.findall(r"(?<![\w.])-?\d+(?:\.\d+)?", source_text)}
    output_numbers = {float(value) for value in re.findall(r"(?<![\w.])-?\d+(?:\.\d+)?", headline + " " + body)}
    if not output_numbers.issubset(source_numbers):
        raise ValueError("narrative introduced unsupported numbers")


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def _snake_case(value: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower()
