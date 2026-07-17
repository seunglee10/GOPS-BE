from __future__ import annotations

import json
import os
import re
import hashlib
import urllib.request
from collections import Counter
from typing import Any

from ..contracts import (
    AgentAnswer,
    AgentFinding,
    EvidenceItem,
    FinalAnswer,
    FinalAnswerCitation,
    FinalAnswerSection,
    IntentRoute,
    SynthesisInput,
)
from ..orchestration.routing import parse_openai_text_json
from ..runtime.analysis_cache import final_answer_from_dict
from ..security import sanitize_text, sanitize_url, sanitize_value


PROHIBITED_FOLLOWUP_SECTION_PATTERNS = (
    "다음확인",
    "추가확인",
    "확인포인트",
    "확인할점",
    "체크포인트",
    "nextsteps",
    "nextchecks",
    "followup",
    "follow-up",
)
FOLLOWUP_DELEGATION_VERBS = (
    "비교",
    "확인",
    "검토",
    "살펴",
    "체크",
    "조회",
    "대조",
    "맞춰",
    "추적",
    "보완",
)
FOLLOWUP_DELEGATION_ENDINGS = (
    "하세요",
    "하십시오",
    "해보세요",
    "해보십시오",
    "해야합니다",
    "필요합니다",
    "권합니다",
    "보세요",
)
ENGLISH_FOLLOWUP_DELEGATION_PREFIXES = (
    "check ",
    "compare ",
    "verify ",
    "look up ",
    "review ",
)


def synthesis_runtime_diagnostics(runtime_context: Any | None = None) -> dict[str, Any]:
    timeout_seconds = parse_float(os.getenv("AGENT_SYNTHESIZER_TIMEOUT_SECONDS"))
    if timeout_seconds is None and runtime_context is not None:
        policy = getattr(runtime_context, "policy", None)
        timeout_ms = getattr(policy, "synthesis_timeout_ms", None)
        if isinstance(timeout_ms, (int, float)) and timeout_ms > 0:
            timeout_seconds = round(float(timeout_ms) / 1000, 3)
    if timeout_seconds is None:
        timeout_seconds = 12.0
    return {
        "providerEnv": os.getenv("AGENT_FINAL_ANSWER_PROVIDER", "openai"),
        "financialProviderEnv": os.getenv("AGENT_FINANCIAL_FINAL_ANSWER_PROVIDER", ""),
        "openaiKeyConfigured": bool(os.getenv("OPENAI_API_KEY")),
        "model": os.getenv("AGENT_SYNTHESIZER_MODEL", os.getenv("OPENAI_MODEL", "gpt-5.2")),
        "timeoutSeconds": timeout_seconds,
    }


def record_synthesis_runtime_diagnostics(timing: dict[str, Any], *, runtime_context: Any | None = None) -> None:
    diagnostics = synthesis_runtime_diagnostics(runtime_context)
    timing["synthesisProviderEnv"] = diagnostics["providerEnv"]
    timing["financialSynthesisProviderEnv"] = diagnostics["financialProviderEnv"]
    timing["synthesisOpenAIKeyConfigured"] = diagnostics["openaiKeyConfigured"]
    timing["synthesisModel"] = diagnostics["model"]
    timing["synthesisTimeoutSeconds"] = diagnostics["timeoutSeconds"]


def log_synthesis_runtime_diagnostics(service: str, *, runtime_context: Any | None = None) -> None:
    diagnostics = synthesis_runtime_diagnostics(runtime_context)
    print(
        (
            f"{service} final-answer synthesis config: "
            f"provider={diagnostics['providerEnv']} "
            f"financialProvider={diagnostics['financialProviderEnv'] or 'default'} "
            f"openaiKeyConfigured={diagnostics['openaiKeyConfigured']} "
            f"model={diagnostics['model']} "
            f"timeoutSeconds={diagnostics['timeoutSeconds']}"
        ),
        flush=True,
    )


class FinalAnswerSynthesizer:
    def synthesize(
        self,
        *,
        symbol: str,
        intent: str,
        route: IntentRoute,
        findings: list[AgentFinding],
        provider_evidence: list[EvidenceItem],
        timing: dict[str, Any] | None = None,
        daily_summaries: list[dict[str, Any]] | None = None,
        synthesis_input: SynthesisInput | None = None,
        runtime_context: Any | None = None,
    ) -> FinalAnswer:
        if isinstance(timing, dict):
            timing["synthesisProvider"] = "deterministic"
            timing.pop("synthesisSkippedReason", None)
            timing.pop("synthesisFallbackReason", None)
            record_synthesis_runtime_diagnostics(timing, runtime_context=runtime_context)
        analysis_query_type = analysis_query_type_from_synthesis_input(synthesis_input)
        if analysis_query_type in {"chart_overview", "reference_anchor_analysis"} and chart_explanation_for_answer(
            findings, provider_evidence, synthesis_input
        ):
            mark_synthesis_fallback(timing, "deterministic_chart_route", skipped=True)
            return self._synthesize_deterministic(
                symbol=symbol,
                intent=intent,
                route=route,
                findings=findings,
                provider_evidence=provider_evidence,
                synthesis_input=synthesis_input,
            )
        if is_news_route(route):
            return self._synthesize_deterministic(
                symbol=symbol,
                intent=intent,
                route=route,
                findings=findings,
                provider_evidence=provider_evidence,
                daily_summaries=daily_summaries,
                synthesis_input=synthesis_input,
            )
        if is_ontology_route(route) and os.getenv("AGENT_ONTOLOGY_FINAL_ANSWER_PROVIDER") != "openai":
            return self._synthesize_deterministic(
                symbol=symbol,
                intent=intent,
                route=route,
                findings=findings,
                provider_evidence=provider_evidence,
                synthesis_input=synthesis_input,
            )
        if is_financial_route(route) and os.getenv("AGENT_FINANCIAL_FINAL_ANSWER_PROVIDER") != "openai":
            return self._synthesize_deterministic(
                symbol=symbol,
                intent=intent,
                route=route,
                findings=findings,
                provider_evidence=provider_evidence,
                synthesis_input=synthesis_input,
            )
        if is_financial_route(route):
            openai_answer = self._synthesize_financial_with_openai(
                symbol=symbol,
                intent=intent,
                route=route,
                findings=findings,
                provider_evidence=provider_evidence,
                timing=timing,
                synthesis_input=synthesis_input,
                runtime_context=runtime_context,
            )
        else:
            openai_answer = self._synthesize_with_openai(
                symbol=symbol,
                intent=intent,
                route=route,
                findings=findings,
                provider_evidence=provider_evidence,
                timing=timing,
                synthesis_input=synthesis_input,
                runtime_context=runtime_context,
            )
        if openai_answer:
            if is_status_only_summary(openai_answer.summary):
                openai_answer.summary = build_summary(symbol, route, findings, provider_evidence)
            if isinstance(timing, dict):
                timing["synthesisProvider"] = "openai"
            return openai_answer
        if isinstance(timing, dict) and not timing.get("synthesisFallbackReason"):
            timing["synthesisFallbackReason"] = "openai_unavailable"
        return self._synthesize_deterministic(
            symbol=symbol,
            intent=intent,
            route=route,
            findings=findings,
            provider_evidence=provider_evidence,
            synthesis_input=synthesis_input,
        )

    def _synthesize_deterministic(
        self,
        *,
        symbol: str,
        intent: str,
        route: IntentRoute,
        findings: list[AgentFinding],
        provider_evidence: list[EvidenceItem],
        daily_summaries: list[dict[str, Any]] | None = None,
        synthesis_input: SynthesisInput | None = None,
    ) -> FinalAnswer:
        analysis_query_type = analysis_query_type_from_synthesis_input(synthesis_input)
        if analysis_query_type in {
            "price_move_cause",
            "news_price_mismatch",
            "factor_decomposition",
            "reference_anchor_analysis",
            "chart_overview",
            "impact_mapping",
            "earnings_reaction",
        }:
            return sanitize_final_answer(build_policy_final_answer(
                symbol=symbol,
                route=route,
                findings=findings,
                provider_evidence=provider_evidence,
                analysis_query_type=analysis_query_type,
                intent=intent,
                synthesis_input=synthesis_input,
            ))
        intent_types = {part.strip() for part in str(route.intentType or "").lower().split("+") if part.strip()}
        if route.intentType == "news" or route.selectedRoles == ["news"]:
            return sanitize_final_answer(build_news_final_answer(symbol, findings, provider_evidence, daily_summaries=daily_summaries))
        if route.intentType == "ontology" or route.selectedRoles == ["ontology"]:
            return sanitize_final_answer(build_ontology_final_answer(symbol, findings, provider_evidence))
        if is_financial_route(route):
            return sanitize_final_answer(build_financial_final_answer(symbol, findings, provider_evidence))
        if intent_types & {"market-move", "explain_price_move", "link_news_to_price_move"}:
            return sanitize_final_answer(build_market_move_final_answer(symbol, findings, provider_evidence, intent=intent))
        return sanitize_final_answer(build_general_final_answer(symbol, route, findings, provider_evidence))

    def _synthesize_with_openai(
        self,
        *,
        symbol: str,
        intent: str,
        route: IntentRoute,
        findings: list[AgentFinding],
        provider_evidence: list[EvidenceItem],
        timing: dict[str, Any] | None = None,
        synthesis_input: SynthesisInput | None = None,
        runtime_context: Any | None = None,
    ) -> FinalAnswer | None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            mark_synthesis_fallback(timing, "missing_openai_api_key", skipped=True)
            return None
        if os.getenv("AGENT_FINAL_ANSWER_PROVIDER") == "deterministic":
            mark_synthesis_fallback(timing, "provider_deterministic", skipped=True)
            return None
        if runtime_context is not None and hasattr(runtime_context, "acquire_llm"):
            if not runtime_context.acquire_llm("synthesis"):
                mark_synthesis_fallback(timing, "synthesis_skipped_budget", skipped=True)
                return None
        try:
            if runtime_context is None and isinstance(timing, dict):
                timing["llmCalls"] = int(timing.get("llmCalls") or 0) + 1
                timing["llmCallLabels"] = [*list(timing.get("llmCallLabels") or []), "synthesis"]
            payload = {
                "model": os.getenv("AGENT_SYNTHESIZER_MODEL", os.getenv("OPENAI_MODEL", "gpt-5.2")),
                "input": [
                    {
                        "role": "system",
                        "content": (
                            "You generate stock-analysis answers from retrieved evidence only. "
                            "Do not invent facts, prices, news, macro data, relationships, citations, or recommendations. "
                            "Write Korean report-style prose. Hide internal route, provider, and guardrail diagnostics from users. "
                            "Never mention JSON field names such as providerEvidence, findings, route, selectedRoles, or guardrail. "
                            "The summary must begin with the Korean phrase '제 의견은'. "
                            "Use only two or three short sections and keep evidence bullets to at most three total user-facing bullets. "
                            "Do not create action-item, checklist, or user-to-do sections. "
                            "Do not tell users to compare, check, verify, or look up data themselves. "
                            "Do not explain missing evidence in the user-facing answer. "
                            "Use '판단 근거' for the evidence section title. "
                            "Include a '분석한 지표' section listing only the user-friendly data categories actually used. "
                            "If evidence is missing, omit that missing item from the user-facing answer. Return strict JSON only."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            synthesis_payload(
                                symbol=symbol,
                                intent=intent,
                                route=route,
                                findings=findings,
                                provider_evidence=provider_evidence,
                                synthesis_input=synthesis_input,
                            ),
                            ensure_ascii=False,
                        ),
                    },
                ],
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "agent_final_answer",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "title": {"type": "string"},
                                "summary": {"type": "string"},
                                "sections": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "properties": {
                                            "title": {"type": "string"},
                                            "bullets": {"type": "array", "items": {"type": "string"}},
                                        },
                                        "required": ["title", "bullets"],
                                    },
                                },
                                "citations": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "properties": {
                                            "provider": {"type": "string"},
                                            "title": {"type": "string"},
                                            "url": {"type": ["string", "null"]},
                                            "publishedAt": {"type": ["string", "null"]},
                                        },
                                        "required": ["provider", "title", "url", "publishedAt"],
                                    },
                                },
                                "limitations": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["title", "summary", "sections", "citations", "limitations"],
                        },
                    }
                },
            }
            request = urllib.request.Request(
                "https://api.openai.com/v1/responses",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                method="POST",
            )
            timeout_seconds = float(os.getenv("AGENT_SYNTHESIZER_TIMEOUT_SECONDS", "0"))
            if timeout_seconds <= 0 and runtime_context is not None:
                timeout_seconds = max(0.001, float(getattr(runtime_context.policy, "synthesis_timeout_ms", 1700)) / 1000)
            if timeout_seconds <= 0:
                timeout_seconds = 12.0
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
            answer = final_answer_from_openai_json(parse_openai_text_json(data))
            if answer is None:
                mark_synthesis_fallback(timing, "openai_invalid_response")
                return None
            return enforce_non_delegating_final_answer(answer)
        except Exception as exc:
            mark_synthesis_fallback(timing, f"openai_{exc.__class__.__name__}")
            return None

    def _synthesize_financial_with_openai(
        self,
        *,
        symbol: str,
        intent: str,
        route: IntentRoute,
        findings: list[AgentFinding],
        provider_evidence: list[EvidenceItem],
        timing: dict[str, Any] | None = None,
        synthesis_input: SynthesisInput | None = None,
        runtime_context: Any | None = None,
    ) -> FinalAnswer | None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            mark_synthesis_fallback(timing, "missing_openai_api_key", skipped=True)
            return None
        if os.getenv("AGENT_FINAL_ANSWER_PROVIDER") == "deterministic":
            mark_synthesis_fallback(timing, "provider_deterministic", skipped=True)
            return None
        payload_data = financial_synthesis_payload(
            symbol=symbol,
            intent=intent,
            route=route,
            findings=findings,
            provider_evidence=provider_evidence,
            synthesis_input=synthesis_input,
        )
        cache_key = financial_final_answer_cache_key(symbol=symbol, payload=payload_data)
        cached_answer = get_cached_financial_final_answer(cache_key)
        if cached_answer is not None:
            if isinstance(timing, dict):
                timing["financialFinalAnswerCacheHit"] = True
            return cached_answer
        if isinstance(timing, dict):
            timing["financialFinalAnswerCacheHit"] = False
        if runtime_context is not None and hasattr(runtime_context, "acquire_llm"):
            if not runtime_context.acquire_llm("financial-synthesis"):
                mark_synthesis_fallback(timing, "synthesis_skipped_budget", skipped=True)
                return None
        try:
            if runtime_context is None and isinstance(timing, dict):
                timing["llmCalls"] = int(timing.get("llmCalls") or 0) + 1
                timing["llmCallLabels"] = [*list(timing.get("llmCallLabels") or []), "financial-synthesis"]
            payload = {
                "model": os.getenv(
                    "AGENT_FINANCIAL_SYNTHESIZER_MODEL",
                    os.getenv("AGENT_SYNTHESIZER_MODEL", os.getenv("OPENAI_MODEL", "gpt-5.2")),
                ),
                "input": [
                    {
                        "role": "system",
                        "content": (
                            "You write beginner-friendly Korean SEC financial statement analysis. "
                            "Use only the supplied formatted facts and rule-based signals. "
                            "Do not calculate new numbers, infer missing metrics, mention raw JSON fields, or add prices, PER, PBR, PSR, forecasts, news, or buy/sell recommendations. "
                            "For referenced company-comparison data, do not recalculate supplied values; explain their meaning without judgments or investment recommendations. "
                            "Explain what each important metric means, then state cautious interpretation and data limitations. "
                            "The summary must start with the integrated judgment or conclusion, not with a statement that evidence was retrieved. "
                            "Return strict JSON only."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(payload_data, ensure_ascii=False),
                    },
                ],
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "financial_final_answer",
                        "strict": True,
                        "schema": final_answer_json_schema(),
                    }
                },
            }
            request = urllib.request.Request(
                "https://api.openai.com/v1/responses",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                method="POST",
            )
            timeout_seconds = float(
                os.getenv(
                    "AGENT_FINANCIAL_SYNTHESIZER_TIMEOUT_SECONDS",
                    os.getenv("AGENT_SYNTHESIZER_TIMEOUT_SECONDS", "0"),
                )
            )
            if timeout_seconds <= 0 and runtime_context is not None:
                timeout_seconds = max(0.001, float(getattr(runtime_context.policy, "synthesis_timeout_ms", 1700)) / 1000)
            if timeout_seconds <= 0:
                timeout_seconds = 2.0
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
            answer = final_answer_from_openai_json(parse_openai_text_json(data))
            if answer is not None:
                set_cached_financial_final_answer(cache_key, answer)
            else:
                mark_synthesis_fallback(timing, "openai_invalid_response")
            return answer
        except Exception as exc:
            mark_synthesis_fallback(timing, f"openai_{exc.__class__.__name__}")
            return None

    def synthesize_agent_answer(
        self,
        *,
        symbol: str,
        intent: str,
        finding: AgentFinding,
        timing: dict[str, Any] | None = None,
        runtime_context: Any | None = None,
    ) -> AgentAnswer:
        openai_answer = self._synthesize_agent_answer_with_openai(
            symbol=symbol,
            intent=intent,
            finding=finding,
            timing=timing,
            runtime_context=runtime_context,
        )
        if openai_answer is not None:
            return openai_answer
        if isinstance(timing, dict):
            timing["roleAnswerLlmUnavailable"] = int(timing.get("roleAnswerLlmUnavailable") or 0) + 1
        return sanitize_agent_answer(AgentAnswer(
            agentId=finding.agentId,
            role=finding.role,
            title=f"{finding.role} 답변",
            content=finding.summary,
            confidence=finding.confidence,
            citations=citations_from_evidence(finding.evidence),
        ))

    def _synthesize_agent_answer_with_openai(
        self,
        *,
        symbol: str,
        intent: str,
        finding: AgentFinding,
        timing: dict[str, Any] | None = None,
        runtime_context: Any | None = None,
    ) -> AgentAnswer | None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            return None
        if runtime_context is not None and hasattr(runtime_context, "acquire_llm"):
            if not runtime_context.acquire_llm(f"{finding.role}-answer"):
                return None
        try:
            if runtime_context is None and isinstance(timing, dict):
                timing["llmCalls"] = int(timing.get("llmCalls") or 0) + 1
            payload = {
                "model": os.getenv("AGENT_ROLE_ANSWER_MODEL", os.getenv("AGENT_SYNTHESIZER_MODEL", os.getenv("OPENAI_MODEL", "gpt-5.2"))),
                "input": [
                    {
                        "role": "system",
                        "content": (
                            "Write one Korean stock-analysis answer for a single role agent from its evidence only. "
                            "Do not merge with other agents. Do not invent facts, prices, news, relationships, or recommendations. "
                            "Return strict JSON only."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            sanitize_value({
                                "symbol": symbol,
                                "intent": intent,
                                "roleFinding": {
                                    "agentId": finding.agentId,
                                    "role": finding.role,
                                    "summary": finding.summary,
                                    "rationale": finding.rationale,
                                    "confidence": finding.confidence,
                                    "evidence": compact_evidence(finding.evidence),
                                },
                            }).value,
                            ensure_ascii=False,
                        ),
                    },
                ],
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "role_agent_answer",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "title": {"type": "string"},
                                "content": {"type": "string"},
                            },
                            "required": ["title", "content"],
                        },
                    }
                },
            }
            request = urllib.request.Request(
                "https://api.openai.com/v1/responses",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                method="POST",
            )
            timeout_seconds = float(os.getenv("AGENT_ROLE_ANSWER_TIMEOUT_SECONDS", "0"))
            if timeout_seconds <= 0 and runtime_context is not None:
                timeout_seconds = max(0.001, float(getattr(runtime_context.policy, "synthesis_timeout_ms", 1700)) / 1000)
            if timeout_seconds <= 0:
                timeout_seconds = 12.0
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
            parsed = parse_openai_text_json(data)
            title = parsed.get("title")
            content = parsed.get("content")
            if not isinstance(title, str) or not isinstance(content, str):
                return None
            return sanitize_agent_answer(AgentAnswer(
                agentId=finding.agentId,
                role=finding.role,
                title=title,
                content=content,
                confidence=finding.confidence,
                citations=citations_from_evidence(finding.evidence),
            ))
        except Exception:
            return None


def is_ontology_route(route: IntentRoute) -> bool:
    return route.intentType == "ontology" or route.selectedRoles == ["ontology"]


def is_news_route(route: IntentRoute) -> bool:
    return route.intentType == "news" or route.selectedRoles == ["news"]


def is_financial_route(route: IntentRoute) -> bool:
    intent_type = str(route.intentType or "").lower()
    return "financial" in intent_type or route.selectedRoles == ["financial"] or "financial" in route.selectedRoles


def mark_synthesis_fallback(timing: dict[str, Any] | None, reason: str, *, skipped: bool = False) -> None:
    if not isinstance(timing, dict):
        return
    if skipped:
        timing["synthesisSkippedReason"] = reason
    timing["synthesisFallbackReason"] = reason


def build_summary(symbol: str, route: IntentRoute, findings: list[AgentFinding], evidence: list[EvidenceItem]) -> str:
    if evidence or findings:
        sources = evidence_source_label(evidence)
        direction = infer_price_direction(findings, evidence)
        return (
            f"{symbol}{direction}에 대해서는 {sources} 근거를 함께 보더라도 단일 원인으로 확정하기는 어렵습니다. "
            "현재 결론은 확인된 신호를 종합해 가장 가능성 높은 설명을 제시하는 것입니다."
        )
    return f"{symbol} 요청은 현재 확보된 데이터만으로 결론을 내리기 어렵습니다."


def is_status_only_summary(summary: str) -> bool:
    normalized = str(summary or "").replace(" ", "")
    return any(
        pattern in normalized
        for pattern in (
            "근거를확인했습니다",
            "외부근거를확인했습니다",
            "분석에사용할",
            "확인된근거를바탕으로요약했습니다",
        )
    )


def evidence_source_label(evidence: list[EvidenceItem]) -> str:
    labels = {
        "chart": "차트",
        "market": "시장",
        "news": "뉴스",
        "ontology": "기업 관계",
        "financial": "재무",
        "macro": "거시",
    }
    providers: list[str] = []
    for item in evidence:
        provider = str(item.provider or "").strip().lower()
        if provider and provider not in providers:
            providers.append(provider)
    if not providers:
        return "역할별 분석"
    return ", ".join(labels.get(provider, provider) for provider in providers[:4])


def infer_price_direction(findings: list[AgentFinding], evidence: list[EvidenceItem], *, intent: str = "") -> str:
    chart_move = extract_chart_move(findings, evidence)
    direction = price_direction_from_chart_or_intent(chart_move, intent, findings)
    if direction == "하락":
        return " 하락"
    if direction == "상승":
        return " 상승"
    return "의 가격 움직임"


def compact_findings(findings: list[AgentFinding]) -> list[dict[str, Any]]:
    return [
        {
            "agentId": item.agentId,
            "role": item.role,
            "summary": sanitize_text(item.summary).value,
            "rationale": sanitize_text(item.rationale).value,
            "confidence": item.confidence,
            "tags": item.tags,
        }
        for item in findings[:12]
    ]


def compact_evidence(items: list[EvidenceItem]) -> list[dict[str, Any]]:
    compacted = []
    for item in items[:20]:
        raw = item.raw if isinstance(item.raw, dict) else {}
        title = display_title(item)
        summary = display_summary(item)
        compacted.append(sanitize_value({
            "provider": item.provider,
            "status": item.status,
            "title": title,
            "summary": summary,
            "observedAt": item.observedAt,
            "url": item.url,
            "raw": {
                key: raw.get(key)
                for key in [
                    "publishedAt",
                    "source",
                    "impactDirection",
                    "eventType",
                    "subjectRelevance",
                    "relevanceScore",
                    "relevanceScoreV2",
                    "relevanceReason",
                    "directSignals",
                    "importanceScore",
                    "originalTitle",
                    "originalSummary",
                    "localizedTitle",
                    "localizedSummary",
                    "themeName",
                    "themeCategory",
                    "controlledName",
                    "confidence",
                    "accession",
                    "sourceUrl",
                    "ticker",
                    "companyName",
                    "sector",
                    "type",
                    "relationType",
                ]
                if key in raw
            },
        }).value)
    return compacted


def synthesis_payload(
    *,
    symbol: str,
    intent: str,
    route: IntentRoute,
    findings: list[AgentFinding],
    provider_evidence: list[EvidenceItem],
    synthesis_input: SynthesisInput | None,
) -> dict[str, Any]:
    if synthesis_input is not None:
        return sanitize_value({
            "symbol": symbol,
            "intent": intent,
            "synthesisInput": synthesis_input.to_dict(),
        }).value
    return sanitize_value({
        "symbol": symbol,
        "intent": intent,
        "route": route.to_dict(),
        "findings": compact_findings(findings),
        "providerEvidence": compact_evidence(provider_evidence),
    }).value


def final_answer_json_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "title": {"type": "string"},
            "summary": {"type": "string"},
            "sections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "title": {"type": "string"},
                        "bullets": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["title", "bullets"],
                },
            },
            "citations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "provider": {"type": "string"},
                        "title": {"type": "string"},
                        "url": {"type": ["string", "null"]},
                        "publishedAt": {"type": ["string", "null"]},
                    },
                    "required": ["provider", "title", "url", "publishedAt"],
                },
            },
            "limitations": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["title", "summary", "sections", "citations", "limitations"],
    }


def final_answer_from_openai_json(data: dict[str, Any]) -> FinalAnswer | None:
    if not isinstance(data, dict):
        return None
    title = data.get("title")
    summary = data.get("summary")
    if not isinstance(title, str) or not isinstance(summary, str):
        return None
    sections = [
        FinalAnswerSection(
            title=str(item.get("title") or "근거"),
            bullets=[str(bullet) for bullet in item.get("bullets", []) if isinstance(bullet, (str, int, float))],
        )
        for item in data.get("sections", [])
        if isinstance(item, dict)
    ]
    citations = [
        FinalAnswerCitation(
            provider=str(item.get("provider") or "provider"),
            title=str(item.get("title") or "Evidence"),
            url=item.get("url") if isinstance(item.get("url"), str) else None,
            publishedAt=item.get("publishedAt") if isinstance(item.get("publishedAt"), str) else None,
        )
        for item in data.get("citations", [])
        if isinstance(item, dict)
    ]
    limitations = [str(item) for item in data.get("limitations", []) if isinstance(item, (str, int, float))]
    return sanitize_final_answer(FinalAnswer(
        title=title,
        summary=summary,
        sections=sections,
        citations=[item for item in citations if item.url],
        limitations=limitations,
    ))


def sanitize_final_answer(answer: FinalAnswer) -> FinalAnswer:
    return FinalAnswer(
        title=clean_user_text(answer.title),
        summary=clean_user_text(answer.summary),
        sections=[
            FinalAnswerSection(
                title=clean_user_text(section.title),
                bullets=[clean_user_text(bullet) for bullet in section.bullets],
            )
            for section in answer.sections
        ],
        citations=[sanitize_final_answer_citation(citation) for citation in answer.citations if citation.url],
        limitations=[clean_user_text(item) for item in answer.limitations],
    )


def enforce_non_delegating_final_answer(answer: FinalAnswer) -> FinalAnswer:
    sections: list[FinalAnswerSection] = []
    for section in answer.sections:
        if is_prohibited_followup_section_title(section.title):
            continue
        bullets = [
            bullet
            for bullet in section.bullets
            if not is_followup_delegation_text(bullet)
            and not is_prohibited_followup_section_title(bullet)
        ]
        if bullets or not section.bullets:
            sections.append(FinalAnswerSection(title=normalize_analysis_section_title(section.title), bullets=bullets))
    summary = ensure_opinion_summary(answer.summary)
    if is_prohibited_followup_section_title(summary):
        summary = re.sub(r"다음\s*확인\s*포인트[:：]?", "", summary)
        summary = re.sub(r"확인할\s*점[:：]?", "", summary)
        summary = re.sub(r"체크\s*포인트[:：]?", "", summary)
    return sanitize_final_answer(FinalAnswer(
        title=answer.title,
        summary=summary,
        sections=sections,
        citations=answer.citations,
        limitations=[],
    ))


def is_prohibited_followup_section_title(value: str) -> bool:
    compacted = re.sub(r"\s+", "", str(value or "")).lower()
    return any(pattern in compacted for pattern in PROHIBITED_FOLLOWUP_SECTION_PATTERNS)


def normalize_analysis_section_title(value: str) -> str:
    text = str(value or "").strip()
    compacted = re.sub(r"\s+", "", text)
    if compacted in {"왜그렇게보나", "왜그런가", "왜그렇게봤나"}:
        return "판단 근거"
    return text


def ensure_opinion_summary(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "제 의견은 확인된 신호를 종합해 판단해야 한다는 것입니다."
    text = re.sub(r"^제\s*의견은\s*[,，]\s*", "제 의견은 ", text)
    text = re.sub(r"^제\s*의견은\s+", "제 의견은 ", text)
    if text.startswith("제 의견은"):
        return text
    return f"제 의견은 {text}"


def is_followup_delegation_text(value: str) -> bool:
    text = re.sub(r"\s+", "", str(value or ""))
    if any(verb in text and ending in text for verb in FOLLOWUP_DELEGATION_VERBS for ending in FOLLOWUP_DELEGATION_ENDINGS):
        return True
    lowered = str(value or "").strip().lower()
    return any(lowered.startswith(prefix) for prefix in ENGLISH_FOLLOWUP_DELEGATION_PREFIXES)


def sanitize_final_answer_citation(citation: FinalAnswerCitation) -> FinalAnswerCitation:
    title_result = sanitize_text(citation.title)
    url_result = sanitize_url(citation.url)
    return FinalAnswerCitation(
        provider=citation.provider,
        title=title_result.value,
        url=url_result.value,
        publishedAt=citation.publishedAt,
    )


def sanitize_agent_answer(answer: AgentAnswer) -> AgentAnswer:
    return AgentAnswer(
        agentId=answer.agentId,
        role=answer.role,
        title=clean_user_text(answer.title),
        content=clean_user_text(answer.content),
        confidence=answer.confidence,
        citations=[sanitize_final_answer_citation(citation) for citation in answer.citations if citation.url],
        createdAt=answer.createdAt,
    )


def clean_user_text(value: str) -> str:
    text = str(value)
    replacements = {
        "providerEvidence": "근거",
        "provider evidence": "근거",
        "Provider evidence": "근거",
        "Provider status": "데이터 상태",
        "provider 근거": "근거",
        "provider": "데이터",
        "GraphDB": "관계 데이터",
        "ClickHouse": "저장 데이터",
        "Redis": "캐시 데이터",
        "스냅샷": "자료",
        "snapshot": "자료",
        "Snapshot": "자료",
        "OHLCV": "시가·고가·저가·종가·거래량",
        "evidence": "근거",
        "Evidence": "근거",
        "Agent findings": "역할별 분석",
        "agent findings": "역할별 분석",
        "route.selectedRoles": "선택된 분석 역할",
        "route": "분석 경로",
        "guardrail": "검증",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    text = re.sub(r"자료이\b", "자료가", text)
    text = re.sub(r"자료은\b", "자료는", text)
    text = re.sub(r"자료을\b", "자료를", text)
    text = text.replace("거래량를", "거래량을")
    text = re.sub(r"증거\((근거|evidence)\)", "근거", text)
    text = re.sub(r"근거\((근거|evidence)\)", "근거", text)
    return sanitize_text(text).value


def build_news_final_answer(
    symbol: str,
    findings: list[AgentFinding],
    provider_evidence: list[EvidenceItem],
    *,
    daily_summaries: list[dict[str, Any]] | None = None,
) -> FinalAnswer:
    news_items = [item for item in provider_evidence if item.provider == "news" and item.status == "available"]
    no_data = [item for item in provider_evidence if item.provider == "news" and item.status == "no-data"]
    title = "뉴스를 가져왔습니다"
    daily_summaries = [item for item in daily_summaries or [] if isinstance(item, dict)]
    if daily_summaries:
        latest = daily_summaries[0]
        return FinalAnswer(
            title=title,
            summary=str(latest.get("summary") or f"{symbol} 일일 뉴스 요약을 가져왔습니다."),
            sections=[],
            citations=[],
            limitations=[],
        )
    if not news_items:
        summary = no_data[0].summary if no_data and no_data[0].summary else f"{symbol} 관련 저장 뉴스가 없습니다."
        return FinalAnswer(
            title=title,
            summary=summary,
            sections=[],
            citations=[],
            limitations=[],
        )

    major_items = sorted(
        news_items,
        key=lambda item: (
            raw_number(item, "importanceScore"),
            raw_number(item, "relevanceScore"),
            raw_text(item, "publishedAt", ""),
        ),
        reverse=True,
    )
    direct_items = [item for item in major_items if raw_text(item, "subjectRelevance", "") in {"primary", "secondary"}]
    mention_items = [item for item in major_items if raw_text(item, "subjectRelevance", "") == "mention"]
    headline_items = direct_items or mention_items or major_items
    sections = [
        FinalAnswerSection(
            title="핵심 뉴스",
            bullets=[f"{display_title(item)}: {display_summary(item)}" for item in headline_items[:3]],
        )
    ]
    if not direct_items and mention_items:
        sections = []
    return FinalAnswer(
        title=title,
        summary=f"{symbol} 관련 뉴스 {len(news_items)}건을 가져왔습니다.",
        sections=sections,
        citations=[],
        limitations=[],
    )


def build_ontology_final_answer(symbol: str, findings: list[AgentFinding], provider_evidence: list[EvidenceItem]) -> FinalAnswer:
    ontology_items = [item for item in provider_evidence if item.provider == "ontology" and item.status == "available"]
    no_data = [item for item in provider_evidence if item.provider == "ontology" and item.status == "no-data"]
    graphdb_unavailable = any(raw_text(item, "relationType", "") == "graphdb-unavailable" for item in no_data)
    no_direct = [item for item in no_data if raw_text(item, "relationType", "") == "no-direct-control"]
    themes = unique_values(
        item.raw.get("themeName")
        for item in ontology_items
        if isinstance(item.raw, dict) and item.raw.get("themeName")
    )
    controls = [
        item
        for item in ontology_items
        if raw_text(item, "relationType", "") in {"control", "theme-control"}
    ]
    title = f"{symbol} 기업 관계 분석"

    if graphdb_unavailable:
        return FinalAnswer(
            title=title,
            summary=f"{symbol} 기업 관계 분석을 완료하지 못했습니다.",
            sections=[],
            citations=[],
            limitations=[hide_internal_terms(item.summary) for item in no_data[:5]],
        )
    if not ontology_items:
        return FinalAnswer(
            title=title,
            summary=f"관계 데이터에서 {symbol} 관련 기업 관계 근거를 확인하지 못했습니다.",
            sections=[
                FinalAnswerSection(
                    title="확인되지 않은 내용",
                    bullets=[hide_internal_terms(item.summary) for item in no_data[:5]],
                )
            ],
            citations=[],
            limitations=["현재 관계 데이터에서 요청 종목의 관계 근거가 충분하지 않습니다."],
        )

    if controls:
        control_summary = " 직접 지배/자회사 관계 근거도 확인했습니다."
    elif no_direct:
        control_summary = " 직접 지배/자회사 관계 근거는 확인되지 않았습니다."
    else:
        control_summary = ""
    theme_summary = f"{symbol}는 {', '.join(themes[:3])} 테마에 속합니다." if themes else f"{symbol} 관련 기업 관계 근거를 확인했습니다."

    sections = [
        FinalAnswerSection(
            title="확인된 관계",
            bullets=[item.summary for item in ontology_items[:5]],
        )
    ]
    if themes:
        sections.append(FinalAnswerSection(title="관련 테마", bullets=themes[:5]))
    if no_direct:
        sections.append(FinalAnswerSection(title="확인되지 않은 내용", bullets=[hide_internal_terms(item.summary) for item in no_direct[:3]]))

    limitations = [hide_internal_terms(item.summary) for item in no_data if raw_text(item, "relationType", "") not in {"no-direct-control"}]
    if not limitations:
        limitations = ["현재 적재된 기업 관계 근거 기준으로만 분석했습니다."]
    return FinalAnswer(
        title=title,
        summary=f"관계 데이터 기준으로 {theme_summary}{control_summary}",
        sections=sections,
        citations=citations_from_evidence(ontology_items),
        limitations=limitations,
    )


FINANCIAL_METRIC_LABELS = {
    "revenue": "매출",
    "operating_income": "영업이익",
    "net_income": "순이익",
    "eps": "EPS",
    "assets": "총자산",
    "liabilities": "총부채",
    "equity": "자기자본",
    "current_assets": "유동자산",
    "current_liabilities": "유동부채",
    "cash_and_cash_equivalents": "현금성자산",
    "interest_expense": "이자비용",
    "operating_cash_flow": "영업활동 현금흐름",
    "free_cash_flow": "잉여현금흐름",
    "shares_outstanding": "발행주식수",
    "current_ratio": "유동비율",
    "net_margin": "순이익률",
    "operating_margin": "영업이익률",
    "revenue_growth": "매출 성장률",
    "net_income_growth": "순이익 성장률",
    "operating_income_growth": "영업이익 성장률",
    "liabilities_to_assets": "총부채/총자산",
    "liabilities_to_equity": "총부채/자기자본",
    "current_liabilities_to_equity": "유동부채/자기자본",
    "noncurrent_liabilities_to_equity": "비유동부채/자기자본",
    "total_debt": "이자성 부채",
    "total_debt_to_assets": "이자성 부채/총자산",
    "total_debt_to_equity": "이자성 부채/자기자본",
    "interest_coverage": "이자보상배율",
    "financial_cost_burden_ratio": "금융비용부담률",
    "net_debt": "순부채",
}
FINANCIAL_MONEY_METRICS = {
    "revenue",
    "operating_income",
    "net_income",
    "assets",
    "liabilities",
    "equity",
    "current_assets",
    "current_liabilities",
    "cash_and_cash_equivalents",
    "interest_expense",
    "operating_cash_flow",
    "free_cash_flow",
    "total_debt",
    "net_debt",
}
FINANCIAL_PERCENT_METRICS = {
    "current_ratio",
    "net_margin",
    "operating_margin",
    "revenue_growth",
    "net_income_growth",
    "operating_income_growth",
    "liabilities_to_assets",
    "liabilities_to_equity",
    "current_liabilities_to_equity",
    "noncurrent_liabilities_to_equity",
    "financial_cost_burden_ratio",
    "total_debt_to_assets",
    "total_debt_to_equity",
}
FINANCIAL_DISPLAY_METRICS = set(FINANCIAL_METRIC_LABELS)
FINANCIAL_METRIC_ORDER = {
    "revenue": 0,
    "operating_income": 1,
    "net_income": 2,
    "eps": 3,
    "assets": 4,
    "liabilities": 5,
    "equity": 6,
    "current_assets": 7,
    "current_liabilities": 8,
    "cash_and_cash_equivalents": 9,
    "current_ratio": 10,
    "operating_cash_flow": 11,
    "free_cash_flow": 12,
    "shares_outstanding": 13,
    "net_margin": 14,
    "operating_margin": 15,
    "liabilities_to_assets": 16,
    "liabilities_to_equity": 17,
    "current_liabilities_to_equity": 18,
    "noncurrent_liabilities_to_equity": 19,
    "total_debt": 20,
    "net_debt": 21,
    "total_debt_to_assets": 22,
    "total_debt_to_equity": 23,
    "interest_expense": 24,
    "interest_coverage": 25,
    "financial_cost_burden_ratio": 26,
}
FINANCIAL_QUALITY_MESSAGES = {
    "missing_source": "일부 지표는 SEC 원천 항목이 부족해 표시하지 않았습니다.",
    "equity_includes_nci": "자기자본 일부 값은 비지배지분 포함 기준일 수 있습니다.",
    "frame_as_reported": "Peer 비교 값은 SEC frames 원 보고 기준이라 정정 공시와 차이가 날 수 있습니다.",
    "frame_coverage_gap": "일부 회사는 fiscal calendar 차이로 같은 SEC frame에 포함되지 않을 수 있습니다.",
    "stale": "캐시가 최신 공시와 다를 수 있어 재수집이 필요합니다.",
    "cash_includes_restricted": "현금성자산에 제한성 현금이 포함될 수 있어 순부채 해석에 주의가 필요합니다.",
    "partial_source": "이자성 부채는 확인 가능한 일부 SEC 부채 계정으로 계산했습니다.",
    "invalid_source_relationship": "SEC 원천 계정 간 관계가 일치하지 않아 해당 비율을 표시하지 않았습니다.",
    "zero_denominator": "분모가 0인 지표는 계산하지 않았습니다.",
}


def build_financial_final_answer(symbol: str, findings: list[AgentFinding], provider_evidence: list[EvidenceItem]) -> FinalAnswer:
    financial_items = [item for item in provider_evidence if item.provider == "financial" and item.status == "available"]
    no_data = [item for item in provider_evidence if item.provider == "financial" and item.status == "no-data"]
    comparison_items = [item for item in financial_items if is_company_compare_reference_evidence(item)]
    if comparison_items:
        return build_company_compare_reference_final_answer(symbol, comparison_items)
    title = f"{symbol} SEC 재무 분석"
    if not financial_items:
        return FinalAnswer(
            title=title,
            summary=hide_internal_terms(no_data[0].summary) if no_data else f"{symbol} SEC 재무 근거가 없습니다.",
            sections=[],
            citations=[],
            limitations=["사용자 요청 시점에는 외부 SEC API를 호출하지 않고 사전 계산된 재무 snapshot만 사용합니다."],
        )

    summary_items = [item for item in financial_items if "peer" not in str(item.title).lower()]
    peer_items = [item for item in financial_items if "peer" in str(item.title).lower()]
    sections = []
    if summary_items:
        summary_item = summary_items[0]
        sections.append(FinalAnswerSection(
            title="공시 기반 재무 요약",
            bullets=financial_metric_bullets(summary_item) or [summary_item.summary],
        ))
        interpretation = financial_interpretation_bullets(summary_item)
        if interpretation:
            sections.append(FinalAnswerSection(title="해석 포인트", bullets=interpretation[:5]))
        sections.append(FinalAnswerSection(title="해석 한계", bullets=financial_next_step_bullets()))
    if peer_items:
        sections.append(FinalAnswerSection(
            title="Peer 비교",
            bullets=financial_peer_bullets(peer_items[0]) or [peer_items[0].summary],
        ))
    limitations = [
        "SEC companyfacts/frames 기반 사전 계산 snapshot만 사용했습니다.",
        "PER, PBR, PSR, 예상 실적처럼 주가나 외부 컨센서스가 필요한 지표는 이번 범위에서 제외했습니다.",
    ]
    warnings = unique_financial_warnings(financial_items)
    if warnings:
        limitations.extend(warnings[:4])
    return FinalAnswer(
        title=title,
        summary=financial_summary_text(symbol, summary_items[0]) if summary_items else financial_items[0].summary,
        sections=sections,
        citations=citations_from_evidence(financial_items),
        limitations=limitations,
    )


def build_company_compare_reference_final_answer(
    symbol: str,
    items: list[EvidenceItem],
) -> FinalAnswer:
    item = items[0]
    raw = item.raw if isinstance(item.raw, dict) else {}
    reference = raw.get("reference") if isinstance(raw.get("reference"), dict) else {}
    data = reference.get("data") if isinstance(reference.get("data"), dict) else {}
    ref_type = str(reference.get("type") or "")
    symbols = [
        str(value).strip().upper()
        for value in data.get("symbols", [])
        if isinstance(value, str) and value.strip()
    ] if isinstance(data.get("symbols"), list) else []
    symbol_label = " × ".join(symbols) or symbol
    if ref_type == "financial.metric":
        metric = str(data.get("metric") or "비교 지표")
        values = data.get("values") if isinstance(data.get("values"), list) else []
        bullets = []
        for value in values:
            if not isinstance(value, dict):
                continue
            value_symbol = str(value.get("symbol") or "").strip().upper()
            display = str(value.get("display") or "데이터 없음").strip()
            if value_symbol:
                bullets.append(f"{value_symbol}: {display}")
        meaning = comparison_metric_meaning(metric)
        return FinalAnswer(
            title=f"{symbol_label} {metric} 비교",
            summary=f"참조된 {metric} 값은 기업별 {meaning} 차이를 보여주지만, 이 값만으로 차이의 원인을 단정할 수는 없습니다.",
            sections=[
                FinalAnswerSection(title="참조된 비교 값", bullets=bullets or [item.summary]),
                FinalAnswerSection(
                    title="의미",
                    bullets=[f"{metric}은 {meaning}을 읽는 지표입니다. 제공된 값을 그대로 비교했으며 새로 계산하지 않았습니다."],
                ),
            ],
            citations=[],
            limitations=["참조된 비교 패널 조각만 사용했으며 외부 데이터를 다시 조회하지 않았습니다."],
        )
    heading = str(data.get("heading") or "기업 비교")
    analysis = str(data.get("analysis") or data.get("summary") or item.summary)
    return FinalAnswer(
        title=f"{symbol_label} {heading}",
        summary=analysis,
        sections=[
            FinalAnswerSection(
                title="참조 근거 해설",
                bullets=[analysis],
            )
        ],
        citations=[],
        limitations=["참조된 비교 패널 근거를 그대로 사용했으며 별도 판정이나 투자 권유를 추가하지 않았습니다."],
    )


def is_company_compare_reference_evidence(item: EvidenceItem) -> bool:
    raw = item.raw if isinstance(item.raw, dict) else {}
    reference = raw.get("reference") if isinstance(raw.get("reference"), dict) else {}
    return str(reference.get("type") or "") in {"financial.metric", "compare.axis", "compare.context"}


def comparison_metric_meaning(metric: str) -> str:
    normalized = metric.replace(" ", "").lower()
    if "마진" in normalized or "margin" in normalized:
        return "매출에서 비용을 제외하고 이익으로 남기는 구조"
    if "성장" in normalized or "growth" in normalized:
        return "같은 기준 기간 대비 사업 규모가 변한 속도"
    if "부채" in normalized or "debt" in normalized:
        return "자산이나 자본 대비 부채 부담"
    if "유동" in normalized or "currentratio" in normalized:
        return "단기 지급 의무를 감당할 수 있는 여력"
    if "현금" in normalized or "cashflow" in normalized or "fcf" in normalized:
        return "영업과 투자 이후 남는 현금 창출력"
    if "roe" in normalized or "자기자본" in normalized:
        return "주주 자본을 이익으로 전환한 효율"
    return "같은 기준으로 측정한 재무 상태와 성과"


def analysis_query_type_from_synthesis_input(synthesis_input: SynthesisInput | None) -> str:
    if synthesis_input is None or not isinstance(synthesis_input.output_policy, dict):
        return "general"
    value = synthesis_input.output_policy.get("analysis_query_type") or synthesis_input.output_policy.get("analysisQueryType")
    return str(value or "general")


def deterministic_evidence_items(
    findings: list[AgentFinding],
    provider_evidence: list[EvidenceItem],
    synthesis_input: SynthesisInput | None = None,
) -> list[EvidenceItem]:
    items: list[EvidenceItem] = []
    items.extend(provider_evidence)
    for finding in findings:
        items.extend(list(getattr(finding, "evidence", []) or []))
    if synthesis_input is not None:
        for snapshot in list(getattr(synthesis_input, "snapshots", []) or []):
            items.extend(list(getattr(snapshot, "evidence", []) or []))
    return dedupe_evidence_items(items)


def dedupe_evidence_items(items: list[EvidenceItem]) -> list[EvidenceItem]:
    seen: set[tuple[str, str, str, str, str]] = set()
    deduped: list[EvidenceItem] = []
    for item in items:
        key = (
            str(item.provider or ""),
            str(item.status or ""),
            str(item.title or ""),
            str(item.summary or ""),
            str(item.url or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def deterministic_policy_context(
    symbol: str,
    intent: str,
    findings: list[AgentFinding],
    provider_evidence: list[EvidenceItem],
    synthesis_input: SynthesisInput | None = None,
) -> dict[str, Any]:
    evidence = deterministic_evidence_items(findings, provider_evidence, synthesis_input)
    available = [item for item in evidence if item.status == "available"]
    chart_move = extract_chart_move(findings, available)
    direction = price_direction_from_chart_or_intent(chart_move, intent, findings)
    news_items = sorted(
        [item for item in available if str(item.provider or "").lower() == "news"],
        key=news_sort_key,
        reverse=True,
    )
    peer_items = [
        item
        for item in available
        if str(item.provider or "").lower() in {"market", "market-data", "chart"}
        and isinstance(item.raw, dict)
        and (item.raw.get("peerSummary") or item.raw.get("peerSymbol"))
    ]
    relationship_items = [
        item
        for item in available
        if str(item.provider or "").lower() in {"ontology", "relationship"}
    ]
    financial_items = [item for item in available if str(item.provider or "").lower() == "financial"]
    return {
        "evidence": evidence,
        "available": available,
        "chartMove": chart_move,
        "direction": direction,
        "newsItems": news_items,
        "peerItems": peer_items,
        "relationshipItems": relationship_items,
        "financialItems": financial_items,
        "warnings": verification_warnings(findings),
        "usedIndicators": used_indicator_bullets(evidence, synthesis_input),
    }


def used_indicator_bullets(evidence: list[EvidenceItem], synthesis_input: SynthesisInput | None = None) -> list[str]:
    labels: list[str] = []
    if synthesis_input is not None:
        for snapshot in list(getattr(synthesis_input, "snapshots", []) or []):
            if getattr(snapshot, "snapshot_type", "") == "risk_policy_snapshot":
                continue
            if getattr(snapshot, "status", "") not in {"success", "partial"}:
                continue
            if not list(getattr(snapshot, "evidence", []) or []):
                continue
            append_unique(labels, indicator_label_for_snapshot(getattr(snapshot, "snapshot_type", "")))
    for item in evidence:
        if item.status != "available":
            continue
        append_unique(labels, indicator_label_for_evidence(item))
    return [label for label in labels if label][:5]


def indicator_label_for_snapshot(snapshot_type: str) -> str:
    mapping = {
        "market_snapshot": "차트 가격·일자별 거래량",
        "news_snapshot": "뉴스",
        "relationship_snapshot": "기업 관계·테마",
        "financial_snapshot": "SEC 재무 지표",
        "financial_peer_snapshot": "재무 peer 비교",
    }
    return mapping.get(str(snapshot_type or ""), "")


def indicator_label_for_evidence(item: EvidenceItem) -> str:
    provider = str(item.provider or "").lower()
    raw = item.raw if isinstance(item.raw, dict) else {}
    if provider in {"chart", "market", "market-data"}:
        if raw.get("peerSummary") or raw.get("peerSymbol"):
            return "peer/섹터 가격 비교"
        return "차트 가격·일자별 거래량"
    if provider == "news":
        return "뉴스"
    if provider in {"ontology", "relationship"}:
        return "기업 관계·테마"
    if provider == "financial":
        return "SEC 재무 지표"
    if provider == "macro":
        return "시장·거시 지표"
    return ""


def append_used_indicators_section(
    sections: list[FinalAnswerSection],
    context: dict[str, Any],
    *,
    max_sections: int = 3,
) -> list[FinalAnswerSection]:
    indicators = [str(item) for item in context.get("usedIndicators", []) if str(item).strip()]
    if not indicators:
        return sections[:max_sections]
    section = FinalAnswerSection(title="분석한 지표", bullets=indicators[:5])
    if len(sections) >= max_sections:
        return [*sections[: max_sections - 1], section]
    return [*sections, section]


def append_unique(values: list[str], value: str) -> None:
    text = str(value or "").strip()
    if text and text not in values:
        values.append(text)


def extract_chart_move(findings: list[AgentFinding], evidence: list[EvidenceItem]) -> dict[str, Any]:
    for item in evidence:
        raw = item.raw if isinstance(item.raw, dict) else {}
        provider = str(item.provider or "").lower()
        if provider not in {"chart", "market", "market-data"}:
            continue
        visible = raw.get("visibleSummary") if isinstance(raw.get("visibleSummary"), dict) else {}
        price_move = raw.get("priceMove") if isinstance(raw.get("priceMove"), dict) else {}
        peer_summary = raw.get("peerSummary") if isinstance(raw.get("peerSummary"), dict) else {}
        if peer_summary and not visible and not price_move:
            continue
        change = first_present(
            visible.get("change"),
            visible.get("changePercent"),
            visible.get("percentChange"),
            price_move.get("change"),
            price_move.get("changePercent"),
            price_move.get("percentChange"),
        )
        last_price = first_present(
            visible.get("lastPrice"),
            visible.get("close"),
            visible.get("latestClose"),
            price_move.get("lastPrice"),
            price_move.get("close"),
        )
        volume = first_present(visible.get("volume"), price_move.get("volume"))
        if change is not None or last_price is not None:
            return {
                "changeText": format_change_value(change),
                "changeNumber": parse_change_number(change),
                "lastPrice": last_price,
                "volume": volume,
                "sourceTitle": display_title(item),
            }
    for finding in findings:
        if getattr(finding, "role", "") != "chart-analysis":
            continue
        match = re.search(r"([+-]?\d+(?:\.\d+)?)\s*%", str(finding.summary or ""))
        if match:
            change = f"{match.group(1)}%"
            return {
                "changeText": change,
                "changeNumber": parse_change_number(change),
                "lastPrice": None,
                "volume": None,
                "sourceTitle": "Chart analysis",
            }
    return {"changeText": "", "changeNumber": None, "lastPrice": None, "volume": None, "sourceTitle": ""}


def price_direction_from_chart_or_intent(
    chart_move: dict[str, Any],
    intent: str,
    findings: list[AgentFinding],
) -> str:
    change_number = chart_move.get("changeNumber")
    if isinstance(change_number, (int, float)):
        if change_number < 0:
            return "하락"
        if change_number > 0:
            return "상승"
    text = " ".join([str(intent or ""), *[str(item.summary or "") for item in findings if getattr(item, "role", "") == "chart-analysis"]]).lower()
    if any(token in text for token in ("내려", "떨어", "하락", "빠졌", "밀렸", "lower", "down", "decline")):
        return "하락"
    if any(token in text for token in ("올랐", "상승", "급등", "rise", "up", "higher")):
        return "상승"
    return "가격 움직임"


def chart_move_bullet(symbol: str, context: dict[str, Any]) -> str | None:
    chart_move = context.get("chartMove") if isinstance(context.get("chartMove"), dict) else {}
    change = str(chart_move.get("changeText") or "").strip()
    if not change:
        return None
    details = [f"보이는 차트 구간 변화는 {change}"]
    last_price = chart_move.get("lastPrice")
    if last_price is not None and str(last_price).strip():
        details.append(f"마지막 가격은 {last_price}")
    return f"{symbol} {', '.join(details)}입니다."


def policy_news_bullets(context: dict[str, Any], *, limit: int = 2) -> list[str]:
    direction = str(context.get("direction") or "")
    bullets: list[str] = []
    for item in context.get("newsItems", [])[:limit]:
        raw = item.raw if isinstance(item.raw, dict) else {}
        impact = str(raw.get("impactDirection") or "").lower()
        title = display_title(item)
        summary = display_summary(item)
        if direction == "하락" and impact in {"positive", "bullish"}:
            bullet = (
                f"{title}: 확인된 뉴스는 긍정 신호에 가까워, 이 뉴스 하나로 하락을 설명하기보다 "
                "기대 선반영, 차익 실현, 시장/섹터 압력 가능성을 함께 봐야 합니다."
            )
        elif direction == "상승" and impact in {"negative", "bearish"}:
            bullet = (
                f"{title}: 확인된 뉴스는 부정 신호에 가까워, 상승 원인으로 단정하기 어렵고 "
                "다른 가격·시장 신호와의 불일치로 봐야 합니다."
            )
        else:
            bullet = f"{title}: {summary}"
        if bullet not in bullets:
            bullets.append(bullet)
    return bullets


def peer_market_bullets(context: dict[str, Any], *, limit: int = 2) -> list[str]:
    main_direction = str(context.get("direction") or "")
    bullets: list[str] = []
    for item in context.get("peerItems", [])[:limit]:
        raw = item.raw if isinstance(item.raw, dict) else {}
        peer_summary = raw.get("peerSummary") if isinstance(raw.get("peerSummary"), dict) else {}
        symbol = str(raw.get("peerSymbol") or peer_summary.get("symbol") or peer_summary.get("ticker") or "").upper()
        change = first_present(peer_summary.get("change"), peer_summary.get("changePercent"), peer_summary.get("percentChange"))
        change_text = format_change_value(change)
        change_number = parse_change_number(change)
        peer_direction = "하락" if isinstance(change_number, (int, float)) and change_number < 0 else "상승" if isinstance(change_number, (int, float)) and change_number > 0 else "변동"
        if symbol and change_text:
            if main_direction in {"하락", "상승"} and peer_direction == main_direction:
                bullet = f"{symbol}도 같은 구간 {change_text}로 {peer_direction}해 공통 시장/섹터 요인 가능성을 높입니다."
            else:
                bullet = f"{symbol} peer 움직임은 {change_text}입니다."
        else:
            bullet = f"{display_title(item)}: {display_summary(item)}"
        if bullet not in bullets:
            bullets.append(bullet)
    return bullets


def relationship_policy_bullets(context: dict[str, Any], *, limit: int = 2) -> list[str]:
    bullets: list[str] = []
    for item in [*context.get("relationshipItems", []), *context.get("financialItems", [])][:limit]:
        bullet = f"{display_title(item)}: {display_summary(item)}"
        if bullet not in bullets:
            bullets.append(bullet)
    return bullets


def integrated_price_move_summary(symbol: str, context: dict[str, Any]) -> str:
    direction = str(context.get("direction") or "가격 움직임")
    chart_move = context.get("chartMove") if isinstance(context.get("chartMove"), dict) else {}
    change = str(chart_move.get("changeText") or "").strip()
    news_bullets = policy_news_bullets(context, limit=1)
    peer_bullets = peer_market_bullets(context, limit=1)
    if direction == "하락" and news_bullets and any(token in news_bullets[0] for token in ("긍정", "불일치")):
        return (
            f"제 의견은 {symbol} 하락이 확인된 긍정 뉴스 자체보다 기대 선반영, 차익 실현, "
            "또는 시장/섹터 압력 가능성과 더 잘 맞습니다."
        )
    if direction in {"하락", "상승"} and change:
        source = "뉴스/관계 신호"
        if peer_bullets:
            source = "peer 움직임과 뉴스/관계 신호"
        return f"제 의견은 {symbol} {direction}을 차트상 {change} 움직임과 {source}를 함께 본 결과로 해석하는 것입니다."
    return f"제 의견은 {symbol} 가격 움직임을 확인된 차트·뉴스·관계 신호를 종합해 해석해야 한다는 것입니다."


def policy_core_bullets(symbol: str, context: dict[str, Any], *, limit: int = 3) -> list[str]:
    bullets: list[str] = []
    chart_bullet = chart_move_bullet(symbol, context)
    if chart_bullet:
        bullets.append(chart_bullet)
    bullets.extend(policy_news_bullets(context, limit=2))
    bullets.extend(peer_market_bullets(context, limit=2))
    bullets.extend(relationship_policy_bullets(context, limit=2))
    return unique_strings(bullets)[:limit]


def parse_float(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except Exception:
        return None


def parse_change_number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("%", "").replace("+", "").replace("−", "-")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    return parse_float(match.group(0))


def format_change_value(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if isinstance(value, (int, float)):
        return f"{value:+.2f}%"
    return text


def first_present(*values: Any) -> Any | None:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def news_sort_key(item: EvidenceItem) -> tuple[float, float, float]:
    raw = item.raw if isinstance(item.raw, dict) else {}
    relevance = parse_float(raw.get("relevanceScoreV2")) or parse_float(raw.get("relevanceScore")) or 0.0
    importance = parse_float(raw.get("importanceScore")) or 0.0
    subject = str(raw.get("subjectRelevance") or "").lower()
    direct_bonus = 1.0 if subject in {"primary", "direct", "exact"} else 0.0
    return (direct_bonus, relevance, importance)


def build_policy_final_answer(
    *,
    symbol: str,
    route: IntentRoute,
    findings: list[AgentFinding],
    provider_evidence: list[EvidenceItem],
    analysis_query_type: str,
    intent: str = "",
    synthesis_input: SynthesisInput | None = None,
) -> FinalAnswer:
    if analysis_query_type == "chart_overview":
        explanation = chart_explanation_for_answer(findings, provider_evidence, synthesis_input)
        if explanation:
            from ..chart_intelligence import build_chart_final_answer

            return build_chart_final_answer(symbol, explanation)
    if analysis_query_type == "news_price_mismatch":
        return build_news_price_mismatch_final_answer(symbol, findings, provider_evidence, intent=intent, synthesis_input=synthesis_input)
    if analysis_query_type == "factor_decomposition":
        return build_factor_decomposition_final_answer(symbol, findings, provider_evidence, intent=intent, synthesis_input=synthesis_input)
    if analysis_query_type == "reference_anchor_analysis":
        return build_reference_anchor_final_answer(symbol, route, findings, provider_evidence, intent=intent, synthesis_input=synthesis_input)
    if analysis_query_type == "impact_mapping":
        return build_impact_mapping_final_answer(symbol, findings, provider_evidence)
    if analysis_query_type == "earnings_reaction":
        return build_earnings_reaction_final_answer(symbol, findings, provider_evidence)
    return build_price_move_cause_final_answer(symbol, findings, provider_evidence, intent=intent, synthesis_input=synthesis_input)


def build_price_move_cause_final_answer(
    symbol: str,
    findings: list[AgentFinding],
    provider_evidence: list[EvidenceItem],
    *,
    intent: str = "",
    synthesis_input: SynthesisInput | None = None,
) -> FinalAnswer:
    context = deterministic_policy_context(symbol, intent, findings, provider_evidence, synthesis_input)
    available = list(context["available"])
    warnings = list(context["warnings"])
    core_bullets = policy_core_bullets(symbol, context, limit=4)
    sections = [
        FinalAnswerSection(
            title="핵심 판단",
            bullets=core_bullets or ["현재 snapshot 근거만으로는 단일 원인을 확정하기 어렵습니다."],
        )
    ]
    reason_bullets = [
        *peer_market_bullets(context, limit=2),
        *relationship_policy_bullets(context, limit=2),
    ]
    reason_bullets = [item for item in unique_strings(reason_bullets) if item not in sections[0].bullets]
    if reason_bullets:
        sections.append(FinalAnswerSection(title="판단 근거", bullets=reason_bullets[:3]))
    if warnings:
        sections.append(FinalAnswerSection(title="반대로 볼 점", bullets=warnings[:3]))
    return FinalAnswer(
        title=f"{symbol} 주가 변동 원인 분석",
        summary=integrated_price_move_summary(symbol, context),
        sections=append_used_indicators_section(sections, context, max_sections=3),
        citations=citations_from_evidence(available),
        limitations=[],
    )


def build_news_price_mismatch_final_answer(
    symbol: str,
    findings: list[AgentFinding],
    provider_evidence: list[EvidenceItem],
    *,
    intent: str = "",
    synthesis_input: SynthesisInput | None = None,
) -> FinalAnswer:
    context = deterministic_policy_context(symbol, intent, findings, provider_evidence, synthesis_input)
    available = list(context["available"])
    chart_bullet = chart_move_bullet(symbol, context)
    mismatch_bullets = policy_news_bullets(context, limit=3)
    core_bullets = unique_strings([*([chart_bullet] if chart_bullet else []), *mismatch_bullets])
    sections = [
        FinalAnswerSection(
            title="핵심 판단",
            bullets=core_bullets[:3] or ["뉴스 headline 감성과 실제 가격 반응을 직접 대조할 snapshot이 제한적입니다."],
        ),
        FinalAnswerSection(
            title="판단 근거",
            bullets=peer_market_bullets(context, limit=2) or relationship_policy_bullets(context, limit=2) or ["선택 뉴스와 가격 반응을 직접 연결할 추가 비교 근거가 제한적입니다."],
        ),
    ]
    return FinalAnswer(
        title=f"{symbol} 뉴스-가격 반응 불일치 분석",
        summary=integrated_price_move_summary(symbol, context),
        sections=append_used_indicators_section(sections, context, max_sections=3),
        citations=citations_from_evidence(available),
        limitations=[],
    )


def build_factor_decomposition_final_answer(
    symbol: str,
    findings: list[AgentFinding],
    provider_evidence: list[EvidenceItem],
    *,
    intent: str = "",
    synthesis_input: SynthesisInput | None = None,
) -> FinalAnswer:
    context = deterministic_policy_context(symbol, intent, findings, provider_evidence, synthesis_input)
    available = list(context["available"])
    core_bullets = policy_core_bullets(symbol, context, limit=4)
    peer_bullets = peer_market_bullets(context, limit=3)
    sections = [
        FinalAnswerSection(
            title="핵심 판단",
            bullets=core_bullets or ["시장/섹터/개별 요인을 나눌 비교 근거가 충분하지 않습니다."],
        ),
    ]
    if peer_bullets:
        sections.append(FinalAnswerSection(title="시장/peer 비교", bullets=peer_bullets[:3]))
    if context["warnings"]:
        sections.append(FinalAnswerSection(title="반대로 볼 점", bullets=list(context["warnings"])[:3]))
    return FinalAnswer(
        title=f"{symbol} 시장/섹터/개별 요인 분해",
        summary=integrated_price_move_summary(symbol, context),
        sections=append_used_indicators_section(sections, context, max_sections=3),
        citations=citations_from_evidence(available),
        limitations=[],
    )


def build_reference_anchor_final_answer(
    symbol: str,
    route: IntentRoute,
    findings: list[AgentFinding],
    provider_evidence: list[EvidenceItem],
    *,
    intent: str = "",
    synthesis_input: SynthesisInput | None = None,
) -> FinalAnswer:
    explanation = chart_explanation_for_answer(findings, provider_evidence, synthesis_input)
    if explanation:
        from ..chart_intelligence import build_chart_final_answer

        answer = build_chart_final_answer(symbol, explanation, reference_mode=True)
        aligned_news = [
            item for item in deterministic_evidence_items(findings, provider_evidence, synthesis_input)
            if item.provider == "news" and item.status == "available" and isinstance(item.raw, dict)
        ][:3]
        if aligned_news:
            bullets = []
            for item in aligned_news:
                relation = str(item.raw.get("temporalRelation") or "before")
                label = {"before": "선택 시점 이전", "during": "선택 시점", "after": "후속 뉴스"}.get(relation, "시점 주변")
                bullets.append(f"{label}: {display_title(item)} — 시간상 근접한 정황이며 원인으로 단정하지 않습니다.")
            answer.sections.insert(min(2, len(answer.sections)), FinalAnswerSection(title="시간상 가까운 뉴스", bullets=bullets))
        return answer
    context = deterministic_policy_context(symbol, intent, findings, provider_evidence, synthesis_input)
    available = list(context["available"])
    anchor = selected_reference_label(provider_evidence)
    core_bullets = policy_core_bullets(symbol, context, limit=3)
    sections = [
        FinalAnswerSection(
            title="핵심 판단",
            bullets=core_bullets or [f"{anchor}를 기준으로 볼 직접 근거가 제한적입니다."],
        ),
    ]
    peer_bullets = peer_market_bullets(context, limit=2)
    if peer_bullets:
        sections.append(FinalAnswerSection(title="기준점 주변 비교", bullets=peer_bullets))
    return FinalAnswer(
        title=f"{symbol} 선택 기준 분석",
        summary=f"{anchor} 기준으로 보면 {integrated_price_move_summary(symbol, context)}",
        sections=append_used_indicators_section(sections, context, max_sections=3),
        citations=citations_from_evidence(available),
        limitations=[],
    )


def chart_explanation_for_answer(
    findings: list[AgentFinding],
    provider_evidence: list[EvidenceItem],
    synthesis_input: SynthesisInput | None,
) -> dict[str, Any] | None:
    from ..chart_intelligence import chart_explanation_from_evidence

    return chart_explanation_from_evidence(deterministic_evidence_items(findings, provider_evidence, synthesis_input))


def build_impact_mapping_final_answer(symbol: str, findings: list[AgentFinding], provider_evidence: list[EvidenceItem]) -> FinalAnswer:
    context = deterministic_policy_context(symbol, "", findings, provider_evidence)
    available = list(context["available"])
    candidates = impact_mapping_candidates(provider_evidence)
    candidate_bullets = candidates[:5] or ["관련 종목/섹터 후보를 좁힐 관계 근거가 제한적입니다."]
    sections = [
        FinalAnswerSection(title="핵심 판단", bullets=candidate_bullets[:3]),
        FinalAnswerSection(
            title="판단 근거",
            bullets=policy_evidence_bullets(findings, available, limit=3) or ["뉴스 직접 언급과 관계/테마 신호를 함께 봐야 합니다."],
        ),
    ]
    return FinalAnswer(
        title=f"{symbol} 뉴스 영향 후보 매핑",
        summary="가장 그럴듯한 해석은 직접 언급 종목과 관계/테마로 연결된 종목을 먼저 제한해서 보는 것입니다.",
        sections=append_used_indicators_section(sections, context, max_sections=3),
        citations=citations_from_evidence(available),
        limitations=[],
    )


def build_earnings_reaction_final_answer(symbol: str, findings: list[AgentFinding], provider_evidence: list[EvidenceItem]) -> FinalAnswer:
    context = deterministic_policy_context(symbol, "", findings, provider_evidence)
    available = list(context["available"])
    core_bullets = policy_core_bullets(symbol, context, limit=3)
    sections = [
        FinalAnswerSection(
            title="핵심 판단",
            bullets=core_bullets or policy_evidence_bullets(findings, available, limit=3) or ["실적 숫자, 가이던스, 가격 반응을 함께 볼 근거가 충분하지 않습니다."],
        ),
    ]
    news_bullets = [item for item in policy_news_bullets(context, limit=2) if item not in sections[0].bullets]
    if news_bullets:
        sections.append(FinalAnswerSection(title="실적/뉴스 반응", bullets=news_bullets))
    return FinalAnswer(
        title=f"{symbol} 실적 반응 분석",
        summary="가장 그럴듯한 해석은 실적 headline만으로 반응을 단정할 수 없고 숫자, 가이던스, 가격 반응의 불일치를 함께 봐야 한다는 것입니다.",
        sections=append_used_indicators_section(sections, context, max_sections=3),
        citations=citations_from_evidence(available),
        limitations=[],
    )


def build_market_move_final_answer(
    symbol: str,
    findings: list[AgentFinding],
    provider_evidence: list[EvidenceItem],
    *,
    intent: str = "",
) -> FinalAnswer:
    visible_findings = visible_role_findings(findings)
    available = [item for item in deterministic_evidence_items(findings, provider_evidence) if item.status == "available"]
    warnings = verification_warnings(findings)
    context = deterministic_policy_context(symbol, intent, findings, provider_evidence)
    core_bullets = policy_core_bullets(symbol, context, limit=3)
    sections = [
        FinalAnswerSection(
            title="주가 변동 원인",
            bullets=core_bullets or [finding.summary for finding in visible_findings[:3]] or ["현재 확인 가능한 역할별 근거가 충분하지 않습니다."],
        )
    ]
    if warnings:
        sections.append(FinalAnswerSection(title="반대 근거 또는 불일치", bullets=warnings[:3]))
    return FinalAnswer(
        title=f"{symbol} 주가 변동 원인 분석",
        summary=market_move_summary_text(symbol, visible_findings, available, intent=intent),
        sections=append_used_indicators_section(sections, context, max_sections=3),
        citations=citations_from_evidence(available),
        limitations=[],
    )


def build_general_final_answer(
    symbol: str,
    route: IntentRoute,
    findings: list[AgentFinding],
    provider_evidence: list[EvidenceItem],
) -> FinalAnswer:
    available = [item for item in provider_evidence if item.status == "available"]
    no_data = [item for item in provider_evidence if item.status == "no-data"]
    visible_findings = visible_role_findings(findings)
    sections = []
    if visible_findings:
        sections.append(FinalAnswerSection(
            title="분석 요약",
            bullets=[finding.summary for finding in visible_findings[:4]],
        ))
    if available:
        sections.append(FinalAnswerSection(
            title="확인된 근거",
            bullets=[f"{display_title(item)}: {display_summary(item)}" for item in available[:5]],
        ))
    limitations = [item.summary for item in no_data[:5]]
    if not available and not limitations:
        limitations.append("외부 데이터 근거가 아직 충분하지 않습니다.")
    return FinalAnswer(
        title=f"{symbol} {route.intentType} 분석",
        summary=build_summary(symbol, route, visible_findings, available),
        sections=sections,
        citations=citations_from_evidence(available),
        limitations=limitations,
    )


def market_move_summary_text(symbol: str, findings: list[AgentFinding], evidence: list[EvidenceItem], *, intent: str = "") -> str:
    direction = infer_price_direction(findings, evidence, intent=intent)
    if evidence:
        sources = evidence_source_label(evidence)
        return (
            f"제 의견은 {symbol}{direction}을 {sources} 신호를 종합해 해석해야 한다는 것입니다."
        )
    if findings:
        return (
            f"제 의견은 {symbol}{direction}을 역할별 분석 신호를 종합해 해석해야 한다는 것입니다."
        )
    return f"제 의견은 {symbol}의 가격 움직임을 먼저 차트와 뉴스 신호 중심으로 해석해야 한다는 것입니다."


def policy_evidence_bullets(findings: list[AgentFinding], evidence: list[EvidenceItem], *, limit: int = 3) -> list[str]:
    bullets = []
    for item in evidence:
        bullet = f"{display_title(item)}: {display_summary(item)}"
        if bullet not in bullets:
            bullets.append(bullet)
        if len(bullets) >= limit:
            return bullets
    for finding in visible_role_findings(findings):
        if finding.summary and finding.summary not in bullets:
            bullets.append(finding.summary)
        if len(bullets) >= limit:
            return bullets
    return bullets


def policy_limitations(provider_evidence: list[EvidenceItem], warnings: list[str]) -> list[str]:
    no_data = [item.summary for item in provider_evidence if item.status == "no-data" and item.summary]
    values = []
    for item in [*warnings, *no_data]:
        text = hide_internal_terms(str(item or "")).strip()
        if text and text not in values:
            values.append(text)
    return values[:5]


def selected_reference_label(provider_evidence: list[EvidenceItem]) -> str:
    for item in provider_evidence:
        raw = item.raw if isinstance(item.raw, dict) else {}
        reference = raw.get("reference") if isinstance(raw.get("reference"), dict) else {}
        ref_type = str(reference.get("type") or "")
        if ref_type == "chart.candle":
            return "선택한 봉"
        if ref_type == "chart.range":
            return "선택한 차트 구간"
        if ref_type == "news.article":
            return "선택한 뉴스"
        if ref_type == "news.dailySummary":
            return "선택한 일일 뉴스 요약"
        if ref_type == "recommendation.stock":
            return "선택한 추천 종목"
        if ref_type == "financial.metric":
            return "선택한 기업 비교 지표"
        if ref_type == "compare.axis":
            return "선택한 기업 비교 분석축"
        if ref_type == "compare.context":
            return "선택한 기업 비교 문맥"
    return "선택한 reference"


def impact_mapping_candidates(provider_evidence: list[EvidenceItem]) -> list[str]:
    values = []
    for item in provider_evidence:
        raw = item.raw if isinstance(item.raw, dict) else {}
        for key in ("targetSymbol", "symbol", "ticker"):
            symbol = str(raw.get(key) or "").strip().upper()
            if symbol and symbol not in values:
                values.append(symbol)
        for symbol in raw.get("symbols") or []:
            text = str(symbol or "").strip().upper()
            if text and text not in values:
                values.append(text)
        theme = str(raw.get("themeName") or raw.get("theme") or "").strip()
        if theme and theme not in values:
            values.append(theme)
        controlled = str(raw.get("controlledName") or raw.get("companyName") or "").strip()
        if controlled and controlled.upper() not in values and controlled not in values:
            values.append(controlled)
    return [f"{item}: 뉴스 영향 후보입니다." for item in values[:5]]


def visible_role_findings(findings: list[AgentFinding]) -> list[AgentFinding]:
    return [
        item
        for item in findings
        if item.role in {"chart-analysis", "news-analysis", "macro-analysis", "company-relationship-analysis", "financial-analysis"}
    ]


def financial_metric_bullets(item: EvidenceItem) -> list[str]:
    bullets = []
    for metric in financial_display_metrics(item)[:24]:
        period = f" ({metric['period']})" if metric.get("period") else ""
        bullets.append(f"{metric['label']}: {metric['displayValue']}{period}")
    return bullets


def financial_peer_bullets(item: EvidenceItem) -> list[str]:
    raw = item.raw if isinstance(item.raw, dict) else {}
    bullets = []
    for frame in financial_peer_frames(raw)[:6]:
        concept = financial_concept_label(frame.get("concept"))
        period = str(frame.get("display_period") or frame.get("displayPeriod") or frame.get("frame_period") or frame.get("framePeriod") or raw.get("frame_period") or "").strip()
        parts = []
        for peer in financial_frame_peers(frame)[:5]:
            symbol = str(peer.get("symbol") or "").upper() or "UNKNOWN"
            display = format_financial_value(str(peer.get("metric") or peer.get("concept") or frame.get("concept") or ""), peer.get("value"))
            if display:
                parts.append(f"{symbol} {display}")
        if not parts:
            continue
        suffix = f" ({period})" if period else ""
        bullets.append(f"{concept}: {', '.join(parts)}{suffix}")
    return bullets


def financial_peer_frames(raw: dict[str, Any]) -> list[dict[str, Any]]:
    frames = [frame for frame in raw.get("frames") or [] if isinstance(frame, dict)]
    comparable = [frame for frame in frames if len(financial_frame_peers(frame)) >= 2]
    if comparable:
        return comparable
    if frames:
        return frames
    peers = [peer for peer in raw.get("peers") or [] if isinstance(peer, dict)]
    if not peers:
        return []
    return [{
        "frame_period": raw.get("frame_period"),
        "concept": raw.get("concept") or (peers[0].get("concept") if peers else None),
        "unit": raw.get("unit") or (peers[0].get("unit") if peers else None),
        "peers": peers,
    }]


def financial_frame_peers(frame: dict[str, Any]) -> list[dict[str, Any]]:
    return [peer for peer in frame.get("peers") or [] if isinstance(peer, dict)]


def unique_financial_warnings(items: list[EvidenceItem]) -> list[str]:
    values = []
    for item in items:
        raw = item.raw if isinstance(item.raw, dict) else {}
        for key in ("quality", "warning"):
            value = raw.get(key)
            message = friendly_financial_quality(value)
            if message and message not in values:
                values.append(message)
        for metric in raw.get("metrics") or []:
            if isinstance(metric, dict):
                message = friendly_financial_quality(metric.get("quality"))
                if message and message not in values:
                    values.append(message)
    return values


def financial_summary_text(symbol: str, item: EvidenceItem) -> str:
    metrics = {metric["metric"]: metric for metric in financial_display_metrics(item)}
    period = financial_period_from_item(item)
    prefix = f"{symbol}은 {period} SEC 공시 기준으로" if period else f"{symbol}은 SEC 공시 기준으로"
    assets = metrics.get("assets")
    liabilities = metrics.get("liabilities")
    equity = metrics.get("equity")
    if assets and liabilities and equity:
        return (
            f"{prefix} 총자산 {assets['displayValue']}, 총부채 {liabilities['displayValue']}, "
            f"자기자본 {equity['displayValue']}를 보유하고 있습니다."
        )
    revenue = metrics.get("revenue")
    net_income = metrics.get("net_income")
    if revenue and net_income:
        return f"{prefix} 매출 {revenue['displayValue']}, 순이익 {net_income['displayValue']}를 기록했습니다."
    if metrics:
        first = next(iter(metrics.values()))
        return f"{prefix} {first['label']} {first['displayValue']} 등 주요 재무 지표를 확인했습니다."
    return item.summary


def financial_display_metrics(item: EvidenceItem) -> list[dict[str, Any]]:
    raw = item.raw if isinstance(item.raw, dict) else {}
    metrics = [metric for metric in raw.get("metrics") or [] if isinstance(metric, dict)]
    display_metrics = []
    for metric in sorted(metrics, key=financial_metric_sort_key):
        name = str(metric.get("metric") or "").strip()
        if not name:
            continue
        if name not in FINANCIAL_DISPLAY_METRICS:
            continue
        quality = str(metric.get("quality") or raw.get("quality") or "available")
        value = metric.get("value")
        if value is None or quality == "missing_source":
            continue
        numeric = as_float(value)
        display_value = format_financial_value(name, value)
        if not display_value:
            continue
        display_metrics.append({
            "metric": name,
            "label": FINANCIAL_METRIC_LABELS.get(name, name),
            "value": numeric if numeric is not None else value,
            "displayValue": display_value,
            "period": financial_metric_period(metric) or str(raw.get("latest_period") or "").strip(),
            "quality": quality,
            "selectedConcept": metric.get("selectedConcept") or metric.get("selected_concept") or metric.get("concept"),
        })
    return display_metrics


def financial_metric_sort_key(metric: dict[str, Any]) -> tuple[int, str, str]:
    name = str(metric.get("metric") or "")
    return (FINANCIAL_METRIC_ORDER.get(name, 100), str(metric.get("kind") or ""), name)


def financial_period_from_item(item: EvidenceItem) -> str:
    raw = item.raw if isinstance(item.raw, dict) else {}
    latest = str(raw.get("latest_period") or "").strip()
    if latest:
        return latest
    for metric in raw.get("metrics") or []:
        if isinstance(metric, dict):
            period = financial_metric_period(metric)
            if period:
                return period
    return ""


def financial_metric_period(metric: dict[str, Any]) -> str:
    fiscal_year = metric.get("fiscalYear") or metric.get("fiscal_year")
    fiscal_period = metric.get("fiscalPeriod") or metric.get("fiscal_period")
    parts = [str(part) for part in (fiscal_year, fiscal_period) if part not in (None, "")]
    if not parts:
        return ""
    if len(parts) == 2 and str(parts[0]).isdigit():
        return f"{parts[0]}년 {parts[1]}"
    return " ".join(parts)


def financial_interpretation_bullets(item: EvidenceItem) -> list[str]:
    return [f"{signal['label']}: {signal['explanation']}" for signal in financial_interpretation_signals(item)]


def financial_interpretation_signals(item: EvidenceItem) -> list[dict[str, str]]:
    metrics = {metric["metric"]: metric for metric in financial_display_metrics(item)}
    signals: list[dict[str, str]] = []
    current_ratio = metric_float(metrics, "current_ratio")
    if current_ratio is not None:
        if current_ratio < 1.0:
            signals.append(financial_signal("current_ratio_below_1", "caution", "단기 유동성 확인 필요", "유동비율이 100%보다 낮아 단기 자산만으로 단기 부채를 모두 덮기에는 여유가 크지 않은 편입니다."))
        elif current_ratio < 1.5:
            signals.append(financial_signal("current_ratio_moderate", "watch", "단기 유동성 보통", "유동비율이 100%는 넘지만 아주 넉넉한 수준은 아니어서 현금흐름과 함께 보는 것이 좋습니다."))
        else:
            signals.append(financial_signal("current_ratio_healthy", "positive", "단기 유동성 양호", "유동비율이 150% 이상이면 단기 지급 여력은 비교적 여유 있는 편으로 볼 수 있습니다."))
    liabilities_to_assets = metric_float(metrics, "liabilities_to_assets")
    if liabilities_to_assets is not None:
        if liabilities_to_assets >= 0.8:
            signals.append(financial_signal("liabilities_to_assets_high", "caution", "총부채 비중 높음", "총부채가 총자산의 80% 이상이면 부채 부담이 큰 구조일 수 있어 이자비용과 현금흐름을 함께 확인해야 합니다."))
        elif liabilities_to_assets >= 0.5:
            signals.append(financial_signal("liabilities_to_assets_moderate", "watch", "부채 활용도 보통 이상", "총부채가 총자산의 절반 이상이라 부채를 어느 정도 활용하는 재무 구조입니다."))
        else:
            signals.append(financial_signal("liabilities_to_assets_low", "positive", "부채 부담 낮은 편", "총자산 대비 총부채 비중이 낮아 재무 안정성 측면에서는 비교적 부담이 적은 편입니다."))
    liabilities_to_equity = metric_float(metrics, "liabilities_to_equity")
    if liabilities_to_equity is not None:
        if liabilities_to_equity >= 2.0:
            signals.append(financial_signal("liabilities_to_equity_high", "caution", "자기자본 대비 부채 큼", "총부채가 자기자본의 2배 이상이면 레버리지 부담을 따로 점검할 필요가 있습니다."))
        elif liabilities_to_equity >= 1.0:
            signals.append(financial_signal("liabilities_to_equity_moderate", "watch", "자기자본 대비 부채 보통 이상", "총부채가 자기자본보다 커서 안정성 판단에는 이익과 현금흐름 추세가 중요합니다."))
    interest_coverage = metric_float(metrics, "interest_coverage")
    if interest_coverage is not None:
        if interest_coverage < 1.0:
            signals.append(financial_signal("interest_coverage_below_1", "caution", "이자 지급여력 부족", "영업이익이 이자비용보다 작아 본업 이익만으로 금융비용을 감당하기 어려운 구간입니다."))
        elif interest_coverage < 3.0:
            signals.append(financial_signal("interest_coverage_thin", "watch", "이자 지급여력 제한", "이자보상배율이 3배 미만이면 이익 둔화 시 금융비용 부담이 빠르게 커질 수 있습니다."))
    financial_cost_burden = metric_float(metrics, "financial_cost_burden_ratio")
    if financial_cost_burden is not None and financial_cost_burden >= 0.05:
        signals.append(financial_signal("financial_cost_burden_high", "watch", "매출 대비 금융비용 확인", "이자비용이 매출의 5% 이상이면 수익성 변화와 함께 금융비용 부담을 점검할 필요가 있습니다."))
    net_debt = metric_float(metrics, "net_debt")
    if net_debt is not None and net_debt < 0:
        signals.append(financial_signal("net_cash_position", "positive", "순현금 구조", "현금성자산이 이자성 부채보다 많아 순부채가 마이너스인 구조입니다."))
    net_margin = metric_float(metrics, "net_margin")
    if net_margin is not None:
        if net_margin >= 0.2:
            signals.append(financial_signal("net_margin_strong", "positive", "수익성 강함", "순이익률이 20% 이상이면 매출을 이익으로 남기는 힘이 강한 편입니다."))
        elif net_margin < 0.05:
            signals.append(financial_signal("net_margin_thin", "caution", "순이익률 낮음", "순이익률이 낮으면 매출 변동이나 비용 증가에 이익이 민감하게 흔들릴 수 있습니다."))
    operating_margin = metric_float(metrics, "operating_margin")
    if operating_margin is not None:
        if operating_margin >= 0.2:
            signals.append(financial_signal("operating_margin_strong", "positive", "영업 수익성 강함", "영업이익률이 20% 이상이면 본업에서 이익을 남기는 힘이 강한 편입니다."))
        elif operating_margin < 0.05:
            signals.append(financial_signal("operating_margin_thin", "caution", "영업 수익성 낮음", "영업이익률이 낮으면 본업 수익성이 약하거나 비용 부담이 큰 상태일 수 있습니다."))
    free_cash_flow = metric_float(metrics, "free_cash_flow")
    if free_cash_flow is not None:
        if free_cash_flow > 0:
            signals.append(financial_signal("free_cash_flow_positive", "positive", "현금창출 양호", "잉여현금흐름이 플러스면 투자 지출 이후에도 회사에 남는 현금이 있다는 뜻입니다."))
        elif free_cash_flow < 0:
            signals.append(financial_signal("free_cash_flow_negative", "caution", "현금흐름 확인 필요", "잉여현금흐름이 마이너스면 투자 지출이나 영업 현금흐름 부담을 확인해야 합니다."))
    for metric_name, label in (
        ("revenue_growth", "매출 성장률"),
        ("net_income_growth", "순이익 성장률"),
        ("operating_income_growth", "영업이익 성장률"),
    ):
        growth = metric_float(metrics, metric_name)
        if growth is None:
            continue
        if growth > 0:
            signals.append(financial_signal(f"{metric_name}_positive", "positive", f"{label} 플러스", f"{label}이 플러스라 전년 동기 대비 성장 흐름이 확인됩니다."))
        elif growth < 0:
            signals.append(financial_signal(f"{metric_name}_negative", "watch", f"{label} 마이너스", f"{label}이 마이너스라 성장 둔화 원인을 확인해야 합니다."))
    if not signals:
        signals.append(financial_signal("limited_ratio_context", "info", "해석 지표 제한", "현재 snapshot에서는 비율과 성장률 정보가 충분하지 않아 숫자 자체의 의미 해석은 제한적입니다."))
    return signals[:7]


def financial_signal(signal: str, severity: str, label: str, explanation: str) -> dict[str, str]:
    return {
        "signal": signal,
        "severity": severity,
        "label": label,
        "explanation": explanation,
    }


def financial_next_step_bullets() -> list[str]:
    return [
        "매출 성장률과 영업이익률 추세를 함께 보면 본업 성장성과 수익성을 더 잘 판단할 수 있습니다.",
        "영업활동 현금흐름과 잉여현금흐름을 보면 회계상 이익이 실제 현금으로 이어지는지 확인할 수 있습니다.",
        "PER, PBR, PSR 같은 주가 기반 밸류에이션은 SEC 재무제표만으로는 부족하고 별도 시장 가격 데이터가 필요합니다.",
    ]


def metric_float(metrics: dict[str, dict[str, Any]], name: str) -> float | None:
    metric = metrics.get(name)
    if not metric:
        return None
    return as_float(metric.get("value"))


def as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").strip())
        except ValueError:
            return None
    return None


def format_financial_value(metric: str, value: Any) -> str:
    number = as_float(value)
    if number is None:
        text = str(value or "").strip()
        return text
    metric_key = str(metric or "").strip()
    concept_key = metric_key.lower()
    if metric_key in FINANCIAL_PERCENT_METRICS:
        return f"{format_number(number * 100, 1)}%"
    if metric_key in FINANCIAL_MONEY_METRICS or any(term in concept_key for term in ("revenue", "income", "assets", "liabilities", "cash", "debt")):
        return format_usd_amount(number)
    if metric_key == "eps" or "earningspershare" in concept_key:
        return f"약 {format_number(number, 2)}달러"
    if metric_key == "shares_outstanding" or "shares" in concept_key:
        return format_shares(number)
    if metric_key == "interest_coverage":
        return f"{format_number(number, 1)}배"
    if abs(number) >= 1_000_000:
        return format_usd_amount(number)
    return format_number(number, 2)


def format_usd_amount(value: float) -> str:
    sign = "-" if value < 0 else ""
    amount = abs(value)
    if amount >= 1_000_000_000_000:
        return f"{sign}약 {format_number(amount / 1_000_000_000_000, 2)}조 달러"
    if amount >= 100_000_000:
        return f"{sign}약 {format_number(amount / 100_000_000, 0)}억 달러"
    if amount >= 10_000:
        return f"{sign}약 {format_number(amount / 10_000, 0)}만 달러"
    return f"{sign}약 {format_number(amount, 0)}달러"


def format_shares(value: float) -> str:
    sign = "-" if value < 0 else ""
    amount = abs(value)
    if amount >= 100_000_000:
        return f"{sign}약 {format_number(amount / 100_000_000, 1)}억 주"
    if amount >= 10_000:
        return f"{sign}약 {format_number(amount / 10_000, 0)}만 주"
    return f"{sign}약 {format_number(amount, 0)}주"


def format_number(value: float, digits: int) -> str:
    text = f"{value:,.{digits}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def friendly_financial_quality(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or text == "available":
        return None
    return FINANCIAL_QUALITY_MESSAGES.get(text, text.replace("_", " "))


def financial_concept_label(value: Any) -> str:
    text = str(value or "").strip()
    normalized = text.lower()
    if "revenue" in normalized or text == "Revenues":
        return "매출"
    if "netincome" in normalized or "net income" in normalized:
        return "순이익"
    if "operatingincome" in normalized or "operating income" in normalized:
        return "영업이익"
    if normalized == "assets" or normalized == "assetscurrent":
        return "총자산" if normalized == "assets" else "유동자산"
    if normalized == "liabilities" or normalized == "liabilitiescurrent":
        return "총부채" if normalized == "liabilities" else "유동부채"
    if "stockholdersequity" in normalized or "stockholders equity" in normalized:
        return "자기자본"
    if "sharesoutstanding" in normalized or "shares outstanding" in normalized:
        return "발행주식수"
    return FINANCIAL_METRIC_LABELS.get(text, text or "지표")


def financial_synthesis_payload(
    *,
    symbol: str,
    intent: str,
    route: IntentRoute,
    findings: list[AgentFinding],
    provider_evidence: list[EvidenceItem],
    synthesis_input: SynthesisInput | None,
) -> dict[str, Any]:
    financial_items = [item for item in provider_evidence if item.provider == "financial" and item.status == "available"]
    comparison_references = [
        (item.raw or {}).get("reference")
        for item in financial_items
        if is_company_compare_reference_evidence(item) and isinstance(item.raw, dict)
    ]
    summary_item = next((item for item in financial_items if "peer" not in str(item.title).lower()), None)
    peer_item = next((item for item in financial_items if "peer" in str(item.title).lower()), None)
    facts = financial_display_metrics(summary_item) if summary_item else []
    signals = financial_interpretation_signals(summary_item) if summary_item else []
    limitations = [
        "SEC 공시 기반 사전 계산 데이터만 사용했습니다.",
        "PER, PBR, PSR, 예상 실적처럼 주가나 외부 컨센서스가 필요한 지표는 제외했습니다.",
        *unique_financial_warnings(financial_items),
    ]
    payload = {
        "symbol": symbol,
        "intent": intent,
        "route": route.to_dict(),
        "period": financial_period_from_item(summary_item) if summary_item else "",
        "facts": facts[:16],
        "signals": signals[:8],
        "peerComparisons": financial_peer_payload(peer_item) if peer_item else [],
        "referenceComparisons": comparison_references,
        "limitations": unique_strings(limitations)[:8],
        "answerPolicy": [
            "제공된 facts와 signals만 사용합니다.",
            "숫자를 새로 계산하지 않습니다.",
            "참조된 비교 데이터의 수치를 재계산하지 말고 의미를 설명합니다.",
            "매수/매도/목표가 같은 투자 행동 조언을 하지 않습니다.",
            "초보 투자자가 이해할 수 있게 지표 의미와 해석 한계를 설명합니다.",
        ],
        "fallbackSummary": financial_summary_text(symbol, summary_item) if summary_item else "",
        "findingSummaries": [finding.summary for finding in findings if finding.role == "financial-analysis"][:3],
    }
    if synthesis_input is not None:
        payload["snapshotIntent"] = synthesis_input.intent
    return sanitize_value(payload).value


def financial_peer_payload(item: EvidenceItem | None) -> list[dict[str, Any]]:
    if item is None:
        return []
    raw = item.raw if isinstance(item.raw, dict) else {}
    peers = []
    for frame in financial_peer_frames(raw):
        frame_period = str(frame.get("display_period") or frame.get("displayPeriod") or frame.get("frame_period") or frame.get("framePeriod") or raw.get("frame_period") or "").strip()
        for peer in financial_frame_peers(frame):
            value = peer.get("value")
            display = format_financial_value(str(peer.get("metric") or peer.get("concept") or frame.get("concept") or ""), value)
            if not display:
                continue
            peers.append({
                "symbol": str(peer.get("symbol") or "").upper(),
                "metric": financial_concept_label(peer.get("concept") or peer.get("metric") or frame.get("concept")),
                "displayValue": display,
                "framePeriod": frame_period,
                "quality": friendly_financial_quality(peer.get("quality")),
            })
    return peers[:10]


def financial_final_answer_cache_key(*, symbol: str, payload: dict[str, Any]) -> str:
    normalized_symbol = str(symbol or "UNKNOWN").strip().upper() or "UNKNOWN"
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True, default=str, separators=(",", ":"))
    digest = hashlib.sha1(encoded.encode("utf-8")).hexdigest()[:24]
    prefix = os.getenv("AGENT_FINANCIAL_FINAL_ANSWER_CACHE_PREFIX", "gops:agent:financial-final-answer:v1")
    return f"{prefix}:{normalized_symbol}:{digest}"


def financial_final_answer_cache_enabled() -> bool:
    return str(os.getenv("AGENT_FINANCIAL_FINAL_ANSWER_CACHE_ENABLED", "true")).strip().lower() not in {"0", "false", "no", "off"}


def get_cached_financial_final_answer(key: str) -> FinalAnswer | None:
    if not financial_final_answer_cache_enabled():
        return None
    client = financial_final_answer_cache_client()
    if client is None:
        return None
    try:
        payload = client.get(key)
        if not payload:
            return None
        decoded = json.loads(payload)
        answer = final_answer_from_dict(decoded.get("finalAnswer") if isinstance(decoded, dict) else decoded)
        return sanitize_final_answer(answer) if answer else None
    except Exception:
        return None


def set_cached_financial_final_answer(key: str, answer: FinalAnswer) -> None:
    if not financial_final_answer_cache_enabled():
        return
    ttl_seconds = int(os.getenv("AGENT_FINANCIAL_FINAL_ANSWER_CACHE_TTL_SECONDS", "86400"))
    if ttl_seconds <= 0:
        return
    client = financial_final_answer_cache_client()
    if client is None:
        return
    try:
        client.setex(key, ttl_seconds, json.dumps({"finalAnswer": answer.to_dict()}, ensure_ascii=False, separators=(",", ":")))
    except Exception:
        return


def financial_final_answer_cache_client():
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        return None
    try:
        import redis

        return redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=float(os.getenv("REDIS_CONNECT_TIMEOUT_SECONDS", "0.2")),
            socket_timeout=float(os.getenv("REDIS_SOCKET_TIMEOUT_SECONDS", "0.2")),
            health_check_interval=int(os.getenv("REDIS_HEALTH_CHECK_INTERVAL_SECONDS", "30")),
        )
    except Exception:
        return None


def unique_strings(values: list[str]) -> list[str]:
    unique = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in unique:
            unique.append(text)
    return unique


def verification_warnings(findings: list[AgentFinding]) -> list[str]:
    warnings = []
    for finding in findings:
        if finding.role != "verification-guardrail":
            continue
        normalized = finding.summary.strip().lower()
        if normalized and not normalized.startswith("no trading-action guardrail violation detected"):
            warnings.append(finding.summary)
    return warnings


def citations_from_evidence(items: list[EvidenceItem]) -> list[FinalAnswerCitation]:
    citations: list[FinalAnswerCitation] = []
    seen: set[str] = set()
    for item in items:
        if not item.url:
            continue
        key = str(item.url).strip().lower()
        if key in seen:
            continue
        seen.add(key)
        citations.append(FinalAnswerCitation(
            provider=item.provider,
            title=display_title(item),
            url=item.url,
            publishedAt=item.raw.get("publishedAt") if isinstance(item.raw, dict) else None,
        ))
        if len(citations) >= 5:
            break
    return citations


def raw_text(item: EvidenceItem, key: str, fallback: str) -> str:
    raw = item.raw if isinstance(item.raw, dict) else {}
    value = raw.get(key)
    return str(value) if value else fallback


def display_title(item: EvidenceItem) -> str:
    if item.provider == "news":
        return raw_text(item, "localizedTitle", item.title)
    return item.title


def display_summary(item: EvidenceItem) -> str:
    if item.provider == "news":
        return raw_text(item, "localizedSummary", item.summary)
    return item.summary


def hide_internal_terms(value: str) -> str:
    return (
        str(value or "")
        .replace("GraphDB", "관계 데이터")
        .replace("ClickHouse", "저장 데이터")
        .replace("Redis", "캐시 데이터")
        .replace("스냅샷", "자료")
        .replace("snapshot", "자료")
        .replace("Snapshot", "자료")
        .replace("OHLCV", "시가·고가·저가·종가·거래량")
        .replace("providerEvidence", "근거")
        .replace("Provider status", "데이터 상태")
        .replace("provider", "데이터")
    )


def raw_number(item: EvidenceItem, key: str) -> float:
    raw = item.raw if isinstance(item.raw, dict) else {}
    value = raw.get(key)
    return float(value) if isinstance(value, (int, float)) else 0.0


def dominant_label(counter: Counter, fallback: str) -> str:
    if not counter:
        return fallback
    for label, _count in counter.most_common():
        if label != "unknown":
            return label
    return fallback


def unique_values(values) -> list[str]:
    seen = set()
    result = []
    for value in values:
        text = str(value).strip() if value is not None else ""
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


def impact_direction_label(value: str) -> str:
    labels = {
        "positive": "긍정",
        "negative": "부정",
        "mixed": "혼재",
        "unknown": "판단 보류",
    }
    return labels.get(value, value)
