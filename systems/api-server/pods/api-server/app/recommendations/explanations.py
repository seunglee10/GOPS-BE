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
PROMPT_VERSION = "recommendation-narrative-atoms.ko.v3"
DETERMINISTIC_RENDERER_VERSION = "recommendation-grounded-renderer.ko.v2"
DECISION_RENDERER_VERSION = "recommendation-decision-renderer.ko.v8"
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
    composed = []
    for source in items:
        item = dict(source)
        atoms = narrative_atoms(item)
        narrative_context = dict(item.get("narrativeContext") or {})
        narrative_context["narrativeAtoms"] = dict(atoms)
        item["narrativeContext"] = narrative_context
        item["explanation"] = deterministic_explanation(item, atoms=atoms)
        composed.append(item)
    if not composed:
        return composed
    if os.getenv("RECOMMENDATION_NARRATIVE_PROVIDER", "deterministic").strip().lower() != "openai":
        if required:
            raise RuntimeError("OpenAI recommendation narrative is required")
        return composed
    try:
        narratives = _generate_narrative_atoms(composed, provider=provider, context=context)
    except Exception as exc:
        if required:
            raise
        LOGGER.warning("recommendation narrative fallback: %s", exc.__class__.__name__)
        return composed
    generated_at = datetime.now(timezone.utc).isoformat()
    model = os.getenv("RECOMMENDATION_NARRATIVE_MODEL") or os.getenv("OPENAI_MODEL") or ""
    for item in composed:
        atoms = narratives.get(str(item.get("symbol") or ""))
        if atoms is None:
            continue
        atoms = {**atoms, "source": "llm", "model": model, "generatedAt": generated_at}
        item["narrativeContext"] = {
            **(item.get("narrativeContext") or {}),
            "narrativeAtoms": dict(atoms),
        }
        item["explanation"] = deterministic_explanation(item, atoms=atoms)
    _deduplicate_primary(composed)
    return composed


def deterministic_explanation(
    item: dict[str, Any], *, atoms: dict[str, Any] | None = None
) -> dict[str, Any]:
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

    atoms = atoms or narrative_atoms(item)
    primary = render_observation_primary(item, atoms)
    return {
        "version": EXPLANATION_VERSION,
        "locale": "ko-KR",
        "decisionLabel": "매수 관찰",
        "primary": {
            "source": primary["source"],
            "status": "ready",
            "listSummary": primary["listSummary"],
            "headline": primary["headline"],
            "body": primary["body"],
            "model": atoms.get("model"),
            "promptVersion": PROMPT_VERSION if primary["source"] == "llm" else DETERMINISTIC_RENDERER_VERSION,
            "generatedAt": atoms.get("generatedAt") or datetime.now(timezone.utc).isoformat(),
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
            "companyContextStatus": str((item.get("narrativeContext") or {}).get("status") or "partial"),
            "companyContextDigest": str((item.get("narrativeContext") or {}).get("digest") or ""),
            "companyProfileAccession": str(((item.get("narrativeContext") or {}).get("tenK") or {}).get("sourceAccession") or ""),
            "usedCompanyRefs": list(atoms.get("companyRefs") or []),
            "usedEvidenceRefs": list(atoms.get("evidenceRefs") or []),
        },
    }


def narrative_atoms(item: dict[str, Any]) -> dict[str, Any]:
    context = item.get("narrativeContext") if isinstance(item.get("narrativeContext"), dict) else {}
    frozen = context.get("narrativeAtoms") if isinstance(context.get("narrativeAtoms"), dict) else None
    if frozen:
        try:
            candidate_atoms = {
                **dict(frozen),
                "symbol": str(item.get("symbol") or frozen.get("symbol") or ""),
                "action": _effective_action(item),
            }
            return _validated_atoms(candidate_atoms, item)
        except ValueError:
            pass
    company = context.get("company") if isinstance(context.get("company"), dict) else {}
    ten_k = context.get("tenK") if isinstance(context.get("tenK"), dict) else {}
    symbol = str(item.get("symbol") or company.get("symbol") or "종목")
    company_name = str(company.get("companyName") or symbol)
    industry = str(company.get("industry") or item.get("industry") or "해당 업종")
    business = ten_k.get("businessModel")
    structure = str(business.get("structure") or "").strip() if isinstance(business, dict) else str(business or "").strip()
    drivers = [str(value).strip() for value in ten_k.get("revenueDrivers") or [] if str(value).strip()]
    descriptor = structure or f"{industry} 업종"
    company_refs = ["company.industry"]
    if structure:
        company_refs = ["tenK.businessModel"]
    if drivers:
        company_refs.append("tenK.revenueDrivers")

    deterministic = item.get("explanation", {}).get("deterministic") if isinstance(item.get("explanation"), dict) else None
    evidence = deterministic.get("evidence") if isinstance(deterministic, dict) else None
    if not evidence:
        evidence = _deterministic_evidence_rows(item)
    strongest = max(evidence or [], key=lambda row: (_number(row.get("score")) or 0.0), default=None)
    evidence_label = str((strongest or {}).get("label") or "시장 근거")
    evidence_code = str((strongest or {}).get("code") or "market_evidence")
    business_sentence = f"{_with_particle(company_name, '은', '는')} {descriptor} 특성을 가진 기업입니다."
    if drivers:
        business_sentence = f"{_with_particle(company_name, '은', '는')} {descriptor} 구조에서 {drivers[0]}을 주요 매출 동인으로 둔 기업입니다."
    setup_sentence = f"이번 추천에서는 {_with_particle(evidence_label, '이', '가')} 다른 관측 근거보다 상대적으로 강하게 나타났습니다."
    risks = [row for row in ten_k.get("riskFactors") or [] if isinstance(row, dict)]
    if risks:
        risk_sentence = f"10-K에 제시된 {str(risks[0].get('category') or '사업')} 위험도 단기 가격 신호와 별도로 확인해야 합니다."
        company_refs.append("tenK.riskFactors")
    else:
        risk_sentence = f"{company_name}의 업종 특성과 개별 기업 위험은 단기 가격 신호와 분리해 확인해야 합니다."
    return {
        "symbol": symbol,
        "action": _effective_action(item),
        "headlineBase": f"{symbol} {company_name}, {_with_particle(descriptor, '과', '와')} {_with_particle(evidence_label, '이', '가')} 함께 드러난 후보",
        "businessSentence": business_sentence,
        "setupSentence": setup_sentence,
        "companyRiskSentence": risk_sentence,
        "companyRefs": list(dict.fromkeys(company_refs)),
        "evidenceRefs": [evidence_code],
        "source": "deterministic",
    }


def render_observation_primary(item: dict[str, Any], atoms: dict[str, Any]) -> dict[str, str]:
    symbol = str(item.get("symbol") or "종목")
    context = item.get("narrativeContext") if isinstance(item.get("narrativeContext"), dict) else {}
    company = context.get("company") if isinstance(context.get("company"), dict) else {}
    company_name = str(company.get("companyName") or symbol)
    evidence = str(atoms.get("setupSentence") or "구조화된 시장 근거를 확인했습니다.")
    return {
        "source": "llm" if atoms.get("source") == "llm" else "deterministic",
        "listSummary": f"{symbol} · {company_name}의 기업 특성과 종목별 시장 근거를 함께 확인",
        "headline": f"{str(atoms.get('headlineBase') or symbol)}, 매수 관찰 우선순위에 올랐습니다.",
        "body": " ".join((
            str(atoms.get("businessSentence") or ""),
            evidence,
            str(atoms.get("companyRiskSentence") or "기업 고유 위험은 단기 가격 신호와 별도로 확인해야 합니다."),
        )).strip(),
    }


def render_decision_primary(item: dict[str, Any]) -> dict[str, Any]:
    atoms = narrative_atoms(item)
    symbol = str(item.get("symbol") or atoms.get("symbol") or "종목")
    action = str(item.get("action") or "watch")
    counter = item.get("counterEvidence") if isinstance(item.get("counterEvidence"), dict) else {}
    counter_label = _headline_condition_label(counter)
    counter_sentence = str(counter.get("sentence") or "").strip()
    base = str(atoms.get("headlineBase") or f"{symbol}의 종목별 근거")
    if action == "buy":
        headline = f"{base}, 계획된 가격대에서 진입을 검토할 수 있습니다."
        action_sentence = "구조화된 진입 경로와 무효화 기준 안에서만 매수를 검토합니다."
        list_suffix = "계획 진입 검토"
    elif action == "conditional_buy":
        headline = f"{base}, 다만 {counter_label} 확인이 먼저입니다."
        action_sentence = counter_sentence or "남은 직접 매수 조건이 해소되기 전에는 주문하지 않습니다."
        list_suffix = f"{counter_label} 확인 후 접근"
    elif action == "not_suitable":
        headline = f"{base}, 현재 계좌 위험 한도에서는 신규 진입에 적합하지 않습니다."
        action_sentence = counter_sentence or "기업과 시장 근거가 있더라도 현재 계좌에서 한 주 이상 배정할 수 없습니다."
        list_suffix = "계좌 한도상 진입 제외"
    else:
        headline = f"{base}, 직접 매수보다 관찰이 우선입니다."
        action_sentence = counter_sentence or "직접 매수 기준에서 추가 확인이 필요한 조건이 남아 있습니다."
        list_suffix = f"{counter_label} 추가 확인"
    body = " ".join(dict.fromkeys(filter(None, (
        str(atoms.get("businessSentence") or ""),
        str(atoms.get("setupSentence") or ""),
        action_sentence,
        str(atoms.get("companyRiskSentence") or ""),
    ))))
    return {
        "source": "llm" if atoms.get("source") == "llm" else "deterministic",
        "status": "ready",
        "listSummary": f"{str(atoms.get('headlineBase') or f'{symbol}의 종목별 기업·시장 근거')} · {list_suffix}",
        "headline": headline,
        "body": body,
        "model": atoms.get("model"),
        "promptVersion": DECISION_RENDERER_VERSION,
        "generatedAt": atoms.get("generatedAt"),
        "companyRefs": list(atoms.get("companyRefs") or []),
        "evidenceRefs": list(atoms.get("evidenceRefs") or []),
    }


def _deterministic_evidence_rows(item: dict[str, Any]) -> list[dict[str, Any]]:
    metrics = item.get("metricsSnapshot") or item.get("metrics_snapshot") or {}
    scores = metrics.get("blockScores") or {}
    contributions = metrics.get("blockContributions") or {}
    rows = []
    for key, code, label in BLOCKS:
        score = _number(scores.get(key))
        if score is not None:
            rows.append({"code": code, "label": label, "score": score, "contribution": _number(contributions.get(key)) or 0.0})
    return rows


def _effective_action(item: dict[str, Any]) -> str:
    decision = item.get("decision") if isinstance(item.get("decision"), dict) else None
    return str((decision or {}).get("action") or "watch")


def _with_particle(value: str, consonant: str, vowel: str) -> str:
    text = str(value or "").strip()
    if not text:
        return text
    code = ord(text[-1])
    if 0xAC00 <= code <= 0xD7A3:
        return text + (consonant if (code - 0xAC00) % 28 else vowel)
    return text + vowel


def _headline_condition_label(counter: dict[str, Any]) -> str:
    mapped = {
        "base_score": "종합 판단 점수",
        "personal_score": "개인화 판단 점수",
        "evidence_reliability": "근거 신뢰도",
        "current_relative_strength": "당일 상대강도",
        "last60_relative_strength": "마감 구간 상대강도",
        "volume_confirmation": "거래량 확인",
        "vwap_hold": "평균 체결가 지지",
        "overextension": "가격 과열",
        "quoted_spread": "체결 여건",
        "material_penalty": "위험 경고",
        "position_size_below_one_share": "추천 가능 수량",
    }.get(str(counter.get("code") or ""))
    if mapped:
        return mapped
    label = re.sub(r"\d+(?:\.\d+)?", "", str(counter.get("label") or "남은 조건"))
    return " ".join(label.split()) or "남은 조건"


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


def _generate_narrative_atoms(
    items: list[dict[str, Any]],
    *,
    provider: Any = None,
    context: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    inputs = [
        {
            "symbol": item["symbol"],
            "action": _effective_action(item),
            "companyContext": item.get("narrativeContext") or {},
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
                    "저장된 10-K 기업 프로필과 결정론적 V3 근거만 사용해 한국어 문장 재료를 만든다. "
                    "추천 순위, action, 수치와 판단을 바꾸지 말고 입력에 없는 사실·숫자·전망·원인을 만들지 않는다. "
                    "headlineBase는 종목과 기업 특성이 드러나는 짧은 명사구로 작성한다. businessSentence는 사업모델, "
                    "setupSentence는 현재 관측 근거, companyRiskSentence는 10-K 또는 업종 위험을 각각 한 문장으로 쓴다. "
                    "companyRefs와 evidenceRefs에는 실제 사용한 입력 ref만 넣는다. 성공확률, 보장, 직접 주문 지시는 금지한다."
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
                                    "action": {"type": "string", "enum": ["buy", "conditional_buy", "watch", "not_suitable"]},
                                    "headlineBase": {"type": "string"},
                                    "businessSentence": {"type": "string"},
                                    "setupSentence": {"type": "string"},
                                    "companyRiskSentence": {"type": "string"},
                                    "companyRefs": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                                    "evidenceRefs": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                                },
                                "required": [
                                    "symbol", "action", "headlineBase", "businessSentence", "setupSentence",
                                    "companyRiskSentence", "companyRefs", "evidenceRefs"
                                ],
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
    validated: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "") if isinstance(row, dict) else ""
        if symbol not in expected:
            continue
        try:
            validated[symbol] = _validated_atoms(dict(row), expected[symbol])
        except ValueError as exc:
            LOGGER.warning("recommendation narrative item fallback for %s: %s", symbol, exc)
    return validated


def _validated_atoms(row: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    required = {
        "symbol", "action", "headlineBase", "businessSentence", "setupSentence",
        "companyRiskSentence", "companyRefs", "evidenceRefs",
    }
    if not required.issubset(row):
        raise ValueError("narrative atom fields are incomplete")
    symbol = str(source.get("symbol") or "")
    if str(row.get("symbol")) != symbol or str(row.get("action")) != _effective_action(source):
        raise ValueError("narrative symbol or action mismatch")
    texts = [str(row.get(key) or "").strip() for key in (
        "headlineBase", "businessSentence", "setupSentence", "companyRiskSentence"
    )]
    if any(not value or not re.search(r"[가-힣]", value) for value in texts):
        raise ValueError("narrative atoms must contain Korean text")
    forbidden = ("성공 확률", "수익 확률", "보장", "매수하세요", "사세요", "주문하세요", "체결하세요")
    if any(token in " ".join(texts) for token in forbidden):
        raise ValueError("unsupported recommendation language")
    company_refs = [str(value) for value in row.get("companyRefs") or []]
    evidence_refs = [str(value) for value in row.get("evidenceRefs") or []]
    allowed_company = set(_company_ref_values(source.get("narrativeContext") or {}))
    allowed_evidence = {
        str(value.get("code"))
        for value in source.get("explanation", {}).get("deterministic", {}).get("evidence", [])
        if isinstance(value, dict) and value.get("code")
    }
    allowed_evidence.update(
        str(value.get("code"))
        for value in source.get("keyEvidence") or []
        if isinstance(value, dict) and value.get("code")
    )
    allowed_evidence.update(str(value.get("code")) for value in _deterministic_evidence_rows(source))
    if not company_refs or not set(company_refs).issubset(allowed_company):
        raise ValueError("unsupported company reference")
    if not evidence_refs or not set(evidence_refs).issubset(allowed_evidence):
        raise ValueError("unsupported evidence reference")
    source_text = json.dumps({
        "company": source.get("narrativeContext") or {},
        "deterministic": source.get("explanation", {}).get("deterministic", {}),
    }, ensure_ascii=False)
    source_numbers = {float(value) for value in re.findall(r"(?<![\w.])-?\d+(?:\.\d+)?", source_text)}
    output_numbers = {float(value) for value in re.findall(r"(?<![\w.])-?\d+(?:\.\d+)?", " ".join(texts))}
    if not output_numbers.issubset(source_numbers):
        raise ValueError("narrative introduced unsupported numbers")
    return {
        "symbol": symbol,
        "action": str(row["action"]),
        "headlineBase": texts[0],
        "businessSentence": texts[1],
        "setupSentence": texts[2],
        "companyRiskSentence": texts[3],
        "companyRefs": list(dict.fromkeys(company_refs)),
        "evidenceRefs": list(dict.fromkeys(evidence_refs)),
        "source": str(row.get("source") or "llm"),
        "model": row.get("model"),
        "generatedAt": row.get("generatedAt"),
    }


def _company_ref_values(context: dict[str, Any]) -> dict[str, Any]:
    refs: dict[str, Any] = {}
    company = context.get("company") if isinstance(context.get("company"), dict) else {}
    if company.get("industry"):
        refs["company.industry"] = company["industry"]
    ten_k = context.get("tenK") if isinstance(context.get("tenK"), dict) else {}
    if ten_k.get("businessModel"):
        refs["tenK.businessModel"] = ten_k["businessModel"]
    if ten_k.get("revenueDrivers"):
        refs["tenK.revenueDrivers"] = ten_k["revenueDrivers"]
    if ten_k.get("competitivePosition"):
        refs["tenK.competitivePosition"] = ten_k["competitivePosition"]
    if ten_k.get("riskFactors"):
        refs["tenK.riskFactors"] = ten_k["riskFactors"]
    for index, catalyst in enumerate(context.get("catalysts") or []):
        refs[f"catalysts.{index}"] = catalyst
    return refs


def _deduplicate_primary(items: list[dict[str, Any]]) -> None:
    seen_headlines: set[str] = set()
    seen_summaries: set[str] = set()
    for item in items:
        primary = item.get("explanation", {}).get("primary", {})
        headline = str(primary.get("headline") or "")
        summary = str(primary.get("listSummary") or "")
        if headline in seen_headlines or summary in seen_summaries:
            item["explanation"] = deterministic_explanation(item, atoms=narrative_atoms(item))
            primary = item["explanation"]["primary"]
            headline = str(primary.get("headline") or "")
            summary = str(primary.get("listSummary") or "")
        seen_headlines.add(headline)
        seen_summaries.add(summary)


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
