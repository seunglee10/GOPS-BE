from __future__ import annotations

import json
import os
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .contracts import AgentFinding, EvidenceItem, LayoutProposal, MarketEvent, NotificationDecision, stable_id, utc_now_iso
from .providers import ClickHouseNewsProvider, EmptyMacroProvider, GraphDBOntologyProvider, ProviderRequest
from .router import parse_openai_text_json


@dataclass
class AgentContext:
    symbol: str
    intent: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    chartContext: dict[str, Any] = field(default_factory=dict)
    marketEvents: list[MarketEvent] = field(default_factory=list)
    providerEvidence: list[EvidenceItem] = field(default_factory=list)


class ChartAgent:
    agent_id = "chart-agent"
    role = "chart-analysis"

    def analyze(self, context: AgentContext) -> AgentFinding:
        chart_document = context.chartContext.get("chartDocument") if isinstance(context.chartContext.get("chartDocument"), dict) else {}
        visible_summary = context.chartContext.get("visibleSummary") if isinstance(context.chartContext.get("visibleSummary"), dict) else {}
        data_status = context.chartContext.get("dataStatus") if isinstance(context.chartContext.get("dataStatus"), dict) else {}
        timeframe = chart_document.get("timeframe") or "unknown"
        last_price = visible_summary.get("lastPrice")
        change = visible_summary.get("change")
        candle_count = data_status.get("candleCount", 0)
        summary = f"{context.symbol} chart context is available for {timeframe} with {candle_count} candles."
        if last_price or change:
            summary = f"{context.symbol} chart shows last price {last_price or 'unknown'} and visible change {change or 'unknown'}."
        evidence = [
            EvidenceItem(
                provider="chart",
                status="available" if candle_count else "partial",
                title="Chart context",
                summary=summary,
                raw={
                    "timeframe": timeframe,
                    "visibleSummary": visible_summary,
                    "dataStatus": data_status,
                },
            )
        ]
        return AgentFinding(
            agentId=self.agent_id,
            role=self.role,
            summary=summary,
            rationale="The chart agent reuses the chart context shape produced for the existing Agent 01 flow.",
            confidence=0.72 if candle_count else 0.42,
            evidence=evidence,
            tags=["chart", "agent-01-compatible"],
        )


class ProviderBackedAgent:
    agent_id = "provider-agent"
    role = "provider"
    provider_name = "provider"

    def __init__(self, provider):
        self.provider = provider

    def analyze(self, context: AgentContext) -> AgentFinding:
        evidence = self.provider.fetch(ProviderRequest(context.symbol, context.intent))
        has_data = any(item.status == "available" for item in evidence)
        if has_data:
            summary = f"{self.provider_name} evidence available for {context.symbol}."
        elif evidence:
            summary = f"{self.provider_name} evidence unavailable for {context.symbol}: {evidence[0].summary}"
        else:
            summary = f"{self.provider_name} evidence unavailable for {context.symbol}."
        return AgentFinding(
            agentId=self.agent_id,
            role=self.role,
            summary=summary,
            rationale="Provider-backed agents expose source availability while the conductor keeps no-data evidence transparent.",
            confidence=0.35 if not has_data else 0.65,
            evidence=evidence,
            tags=[self.provider_name, "provider-adapter"],
        )


class NewsAgent(ProviderBackedAgent):
    agent_id = "news-agent"
    role = "news-analysis"
    provider_name = "news"

    def __init__(self, provider=None):
        super().__init__(provider or ClickHouseNewsProvider())

    def analyze(self, context: AgentContext) -> AgentFinding:
        evidence = self.provider.fetch(ProviderRequest(context.symbol, context.intent))
        analysis = analyze_news_evidence(context, evidence)
        openai_analysis = role_analysis_with_openai(
            role="news",
            context=context,
            evidence=evidence,
            fallback=analysis,
            schema_name="news_agent_analysis",
        )
        analysis = openai_analysis or analysis
        return AgentFinding(
            agentId=self.agent_id,
            role=self.role,
            summary=str(analysis["summary"]),
            rationale=str(analysis["rationale"]),
            confidence=float(analysis["confidence"]),
            evidence=evidence,
            tags=[str(item) for item in analysis["tags"]],
        )


class MacroAgent(ProviderBackedAgent):
    agent_id = "macro-agent"
    role = "macro-analysis"
    provider_name = "macro"

    def __init__(self, provider=None):
        super().__init__(provider or EmptyMacroProvider())


class OntologyAgent(ProviderBackedAgent):
    agent_id = "ontology-agent"
    role = "company-relationship-analysis"
    provider_name = "ontology"

    def __init__(self, provider=None):
        super().__init__(provider or GraphDBOntologyProvider())

    def analyze(self, context: AgentContext) -> AgentFinding:
        evidence = self.provider.fetch(ProviderRequest(context.symbol, context.intent))
        analysis = analyze_ontology_evidence(context, evidence)
        openai_analysis = role_analysis_with_openai(
            role="ontology",
            context=context,
            evidence=evidence,
            fallback=analysis,
            schema_name="ontology_agent_analysis",
        )
        analysis = openai_analysis or analysis
        return AgentFinding(
            agentId=self.agent_id,
            role=self.role,
            summary=str(analysis["summary"]),
            rationale=str(analysis["rationale"]),
            confidence=float(analysis["confidence"]),
            evidence=evidence,
            tags=[str(item) for item in analysis["tags"]],
        )


class UnusualEventExplainerAgent:
    agent_id = "unusual-event-explainer-agent"
    role = "unusual-event-explanation"

    def analyze(self, context: AgentContext) -> AgentFinding:
        if not context.marketEvents:
            return AgentFinding(
                agentId=self.agent_id,
                role=self.role,
                summary="No unusual market event was supplied.",
                rationale="The explainer only expands detected events and stays quiet when the event detector has no signal.",
                confidence=0.5,
                tags=["event-explainer"],
            )

        event = max(context.marketEvents, key=lambda item: severity_rank(item.severity))
        return AgentFinding(
            agentId=self.agent_id,
            role=self.role,
            summary=f"{event.symbol} has a {event.severity} {event.eventType} event.",
            rationale=event.summary,
            confidence=0.74,
            evidence=event.evidence,
            tags=["event-explainer", event.eventType, event.severity],
        )


class MarketSummaryAgent:
    agent_id = "market-summary-agent"
    role = "market-summary"

    def analyze(self, context: AgentContext, findings: list[AgentFinding]) -> AgentFinding:
        event_count = len(context.marketEvents)
        missing_provider_count = sum(
            1
            for finding in findings
            for evidence in finding.evidence
            if evidence.status == "no-data"
        )
        summary = f"{context.symbol} analysis combined {len(findings)} role findings."
        if event_count:
            summary = f"{context.symbol} analysis found {event_count} unusual event signal(s)."
        return AgentFinding(
            agentId=self.agent_id,
            role=self.role,
            summary=summary,
            rationale=f"{missing_provider_count} provider evidence source(s) are intentionally empty in v1.",
            confidence=0.6,
            tags=["summary"],
        )


class VerificationGuardrailAgent:
    agent_id = "verification-guardrail-agent"
    role = "verification-guardrail"

    def analyze(self, context: AgentContext, findings: list[AgentFinding]) -> AgentFinding:
        risk_terms = ("buy now", "sell now", "place order", "automatic order", "auto trade")
        combined = " ".join(f"{finding.summary} {finding.rationale}" for finding in findings).lower()
        blocked_terms = [term for term in risk_terms if term.lower() in combined]
        conflicts = detect_cross_agent_conflicts(findings)
        summary = "No trading-action guardrail violation detected."
        confidence = 0.8
        if blocked_terms:
            summary = "Trading-action language was detected and must be removed before display."
            confidence = 0.95
        elif conflicts:
            summary = " ".join(conflicts)
            confidence = 0.86
        return AgentFinding(
            agentId=self.agent_id,
            role=self.role,
            summary=summary,
            rationale="The verification agent checks for unsupported order execution language and no-data provider transparency.",
            confidence=confidence,
            tags=["verification", "guardrail", *blocked_terms, *(["cross-agent-conflict"] if conflicts else [])],
        )


class NotificationDecisionAgent:
    def decide(self, analysis_id: str, context: AgentContext) -> NotificationDecision:
        event = max(context.marketEvents, key=lambda item: severity_rank(item.severity), default=None)
        if not event:
            level = "none"
            title = f"{context.symbol} analysis ready"
            message = "No unusual market alert was detected."
            reason = "No event detector signal was attached to this analysis."
            event_id = None
        else:
            level = event.severity if event.severity in {"info", "watch", "alert", "critical"} else "watch"
            title = f"{event.symbol} {event.eventType.replace('_', ' ')}"
            message = event.summary
            reason = "Notification level follows the strongest attached market event severity."
            event_id = event.eventId

        return NotificationDecision(
            decisionId=stable_id("notification", {"analysisId": analysis_id, "eventId": event_id, "level": level}),
            analysisId=analysis_id,
            eventId=event_id,
            symbol=context.symbol,
            level=level,
            showToast=level in {"watch", "alert", "critical"},
            title=title,
            message=message,
            reason=reason,
            createdAt=utc_now_iso(),
        )


class LayoutAgent:
    def propose(self, context: AgentContext) -> LayoutProposal:
        commands: list[dict[str, Any]] = []
        if context.marketEvents:
            commands.append({
                "type": "layout.panel.add",
                "payload": {
                    "panelType": "notifications",
                    "props": {"symbol": context.symbol},
                },
            })
        return LayoutProposal(
            title="Agent analysis workspace",
            rationale="Show notifications when unusual events are present; leave layout commands in proposal review.",
            commands=commands,
        )


def severity_rank(value: str) -> int:
    order = {"none": 0, "info": 1, "watch": 2, "alert": 3, "critical": 4}
    return order.get(value, 1)


def analyze_news_evidence(context: AgentContext, evidence: list[EvidenceItem]) -> dict[str, Any]:
    available = [item for item in evidence if item.status == "available"]
    if not available:
        summary = f"{context.symbol} 관련 저장 뉴스 근거를 확인하지 못했습니다."
        detail = evidence[0].summary if evidence else "뉴스 provider에서 반환된 근거가 없습니다."
        return {
            "summary": summary,
            "rationale": f"데이터 한계: {detail}",
            "confidence": 0.35,
            "tags": ["news", "no-data"],
        }

    directions = Counter(news_raw_value(item, "impactDirection", "unknown") for item in available)
    events = Counter(news_raw_value(item, "eventType", "other") for item in available)
    dominant_direction = dominant_label(directions, fallback="unknown")
    dominant_event = dominant_label(events, fallback="other")
    top_titles = [item.title for item in available[:3]]
    summary = (
        f"{context.symbol} 뉴스 {len(available)}건을 확인했습니다. "
        f"주요 이벤트는 {event_type_label(dominant_event)}이고, "
        f"주가 영향 방향은 {impact_direction_label(dominant_direction)}로 분류했습니다."
    )
    rationale = "핵심 뉴스: " + "; ".join(top_titles)
    return {
        "summary": summary,
        "rationale": rationale,
        "confidence": 0.68,
        "tags": ["news", "analysis", f"impact:{dominant_direction}", f"event:{dominant_event}"],
    }


def analyze_ontology_evidence(context: AgentContext, evidence: list[EvidenceItem]) -> dict[str, Any]:
    available = [item for item in evidence if item.status == "available"]
    no_data = [item for item in evidence if item.status == "no-data"]
    relation_types = Counter(news_raw_value(item, "relationType", "ontology") for item in evidence)
    themes = unique_values(item.raw.get("themeName") for item in available if isinstance(item.raw, dict))
    controls = [
        item
        for item in available
        if news_raw_value(item, "relationType", "") in {"control", "theme-control"}
    ]
    graphdb_unavailable = any(news_raw_value(item, "relationType", "") == "graphdb-unavailable" for item in no_data)
    no_direct = any(news_raw_value(item, "relationType", "") == "no-direct-control" for item in no_data)

    if graphdb_unavailable:
        detail = no_data[0].summary if no_data else "GraphDB 온톨로지 조회에 실패했습니다."
        return {
            "summary": f"{context.symbol} 기업 관계 분석을 완료하지 못했습니다.",
            "rationale": f"GraphDB 연결 실패: {detail}",
            "confidence": 0.25,
            "tags": ["ontology", "graphdb-unavailable"],
        }

    if not available:
        detail = next((item.summary for item in no_data if item.summary), f"{context.symbol} 관계 근거가 없습니다.")
        return {
            "summary": f"{context.symbol} 관련 온톨로지 관계 근거를 확인하지 못했습니다.",
            "rationale": detail,
            "confidence": 0.34,
            "tags": ["ontology", "no-data", *relation_type_tags(relation_types)],
        }

    theme_text = ", ".join(themes[:3]) if themes else "확인된 테마"
    if controls:
        controlled_names = unique_values(
            item.raw.get("controlledName")
            for item in controls
            if isinstance(item.raw, dict) and item.raw.get("controlledName")
        )
        control_text = ", ".join(controlled_names[:3]) if controlled_names else "확인된 기업"
        summary = f"GraphDB 기준으로 {context.symbol}는 {theme_text} 관계와 {control_text} 직접 지배/자회사 관계 근거가 있습니다."
    elif no_direct:
        summary = f"GraphDB 기준으로 {context.symbol}는 {theme_text} 테마에 속합니다. 직접 지배/자회사 관계 근거는 확인되지 않았습니다."
    else:
        summary = f"GraphDB 기준으로 {context.symbol}는 {theme_text} 관계 근거가 있습니다."

    evidence_lines = [item.summary for item in available[:4]]
    if no_direct:
        evidence_lines.extend(item.summary for item in no_data if news_raw_value(item, "relationType", "") == "no-direct-control")
    return {
        "summary": summary,
        "rationale": " / ".join(evidence_lines),
        "confidence": 0.66,
        "tags": ["ontology", "analysis", *relation_type_tags(relation_types)],
    }


def role_analysis_with_openai(
    *,
    role: str,
    context: AgentContext,
    evidence: list[EvidenceItem],
    fallback: dict[str, Any],
    schema_name: str,
) -> dict[str, Any] | None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key or os.getenv("AGENT_ROLE_ANALYSIS_PROVIDER") == "deterministic":
        return None
    try:
        payload = {
            "model": os.getenv("AGENT_ROLE_ANALYSIS_MODEL", os.getenv("OPENAI_MODEL", "gpt-5.2")),
            "input": [
                {
                    "role": "system",
                    "content": (
                        "You are a stock-analysis role agent. Analyze only the supplied evidence. "
                        "Do not invent news, relationships, prices, sources, recommendations, or citations. "
                        "Write concise Korean. Return strict JSON only."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "role": role,
                            "symbol": context.symbol,
                            "intent": context.intent,
                            "evidence": compact_role_evidence(evidence),
                            "deterministicFallback": fallback,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "summary": {"type": "string"},
                            "rationale": {"type": "string"},
                            "confidence": {"type": "number"},
                            "tags": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["summary", "rationale", "confidence", "tags"],
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
        with urllib.request.urlopen(request, timeout=float(os.getenv("AGENT_ROLE_ANALYSIS_TIMEOUT_SECONDS", "10"))) as response:
            data = json.loads(response.read().decode("utf-8"))
        parsed = parse_openai_text_json(data)
        summary = parsed.get("summary")
        rationale = parsed.get("rationale")
        tags = parsed.get("tags")
        confidence = parsed.get("confidence")
        if not isinstance(summary, str) or not isinstance(rationale, str) or not isinstance(tags, list):
            return None
        return {
            "summary": summary,
            "rationale": rationale,
            "confidence": float(confidence) if isinstance(confidence, (int, float)) else fallback["confidence"],
            "tags": [str(item) for item in tags],
        }
    except Exception:
        return None


def compact_role_evidence(items: list[EvidenceItem]) -> list[dict[str, Any]]:
    compacted = []
    for item in items[:12]:
        raw = item.raw if isinstance(item.raw, dict) else {}
        compacted.append({
            "provider": item.provider,
            "status": item.status,
            "title": item.title,
            "summary": item.summary,
            "url": item.url,
            "raw": {
                key: raw.get(key)
                for key in [
                    "impactDirection",
                    "eventType",
                    "relevanceScore",
                    "publishedAt",
                    "source",
                    "relationType",
                    "themeName",
                    "controlledName",
                    "confidence",
                    "accession",
                    "sourceUrl",
                ]
                if key in raw
            },
        })
    return compacted


def detect_cross_agent_conflicts(findings: list[AgentFinding]) -> list[str]:
    chart_direction = chart_price_direction(findings)
    news_direction = news_impact_direction(findings)
    conflicts = []
    if chart_direction == "down" and news_direction == "positive":
        conflicts.append("뉴스 방향은 긍정적이지만 차트 가격 반응은 하락으로 나타나 불일치가 있습니다.")
    elif chart_direction == "up" and news_direction == "negative":
        conflicts.append("뉴스 방향은 부정적이지만 차트 가격 반응은 상승으로 나타나 불일치가 있습니다.")
    return conflicts


def chart_price_direction(findings: list[AgentFinding]) -> str | None:
    chart_finding = next((item for item in findings if item.role == "chart-analysis"), None)
    if not chart_finding:
        return None
    for evidence in chart_finding.evidence:
        raw = evidence.raw if isinstance(evidence.raw, dict) else {}
        visible_summary = raw.get("visibleSummary")
        change = visible_summary.get("change") if isinstance(visible_summary, dict) else None
        direction = parse_change_direction(change)
        if direction:
            return direction
    return None


def news_impact_direction(findings: list[AgentFinding]) -> str | None:
    news_finding = next((item for item in findings if item.role == "news-analysis"), None)
    if not news_finding:
        return None
    directions = Counter(
        news_raw_value(evidence, "impactDirection", "unknown")
        for evidence in news_finding.evidence
        if evidence.status == "available"
    )
    direction = dominant_label(directions, fallback="unknown")
    return direction if direction in {"positive", "negative"} else None


def parse_change_direction(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().replace("%", "").replace(",", "")
    if not text:
        return None
    try:
        parsed = float(text)
    except ValueError:
        return None
    if parsed > 0:
        return "up"
    if parsed < 0:
        return "down"
    return None


def news_raw_value(item: EvidenceItem, key: str, fallback: str) -> str:
    raw = item.raw if isinstance(item.raw, dict) else {}
    value = raw.get(key)
    return str(value) if value else fallback


def dominant_label(counter: Counter, *, fallback: str) -> str:
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


def relation_type_tags(counter: Counter) -> list[str]:
    return [f"relation:{label}" for label in sorted(counter) if label]


def impact_direction_label(value: str) -> str:
    labels = {
        "positive": "긍정",
        "negative": "부정",
        "mixed": "혼재",
        "unknown": "판단 보류",
    }
    return labels.get(value, value)


def event_type_label(value: str) -> str:
    labels = {
        "earnings": "실적",
        "guidance": "가이던스",
        "product": "제품/기술",
        "regulation": "규제",
        "analyst": "애널리스트 의견",
        "macro": "거시 지표",
        "mna": "인수합병",
        "legal": "법무 이슈",
        "partnership": "제휴",
        "other": "기타",
    }
    return labels.get(value, value)
