"""리포트 화면용 `view` (D16).

AnalysisReport 의 기존 필드(finalAnswer·finalResponse·findings·providerEvidence·
tradeConditionProposals·agentTrace)만으로 화면이 바로 그릴 수 있는 모양을 만든다.
LLM 을 다시 부르지 않고, 같은 리포트에서는 언제나 같은 결과가 나온다.

- 글은 모두 평문이다. RichText 미니 문법(**굵게**, [[TICKER]])만 허용하고 HTML 은 지운다.
- 원문에 없는 전망·매수/매도 권유 문장을 새로 만들지 않는다. 새로 쓰는 글은 단계 이름,
  지표 이름, "N 아래로 가면 알리기" 같은 알림 규칙 이름뿐이다.
- 리포트 안의 bullish/bearish 포인트와 final_stance 는 view 에 옮기지 않는다.
"""

from __future__ import annotations

import re
from typing import Any

CONFIDENCE_HIGH = "높음"
CONFIDENCE_MEDIUM = "보통"
CONFIDENCE_LOW = "낮음"

MAX_SECTIONS = 6
MAX_EVIDENCE_PER_SECTION = 5
MAX_TEXT_LENGTH = 600
MAX_SUGGESTIONS = 4

UNAVAILABLE_EVIDENCE_STATUSES = frozenset({"no-data", "error", "failed", "unavailable", "skipped"})
NON_TICKER_SYMBOLS = frozenset({"", "MARKET", "PORTFOLIO", "UNKNOWN", "ALERT", "NONE"})

# (역할 키, 섹션 제목, 단계 이름) 순서가 섹션 순서다.
ROLE_ORDER: tuple[tuple[str, str, str], ...] = (
    ("chart", "차트", "차트 해석"),
    ("news", "뉴스", "뉴스 확인"),
    ("financial", "재무", "재무 자료 확인"),
    ("macro", "시장 흐름", "시장 지표 확인"),
    ("relationship", "기업 관계", "기업 관계 확인"),
    ("risk", "위험 점검", "위험 점검"),
    ("unusual", "특이 움직임", "특이 움직임 확인"),
)
INTERNAL_ROLES = frozenset({"summary", "guardrail"})

EVIDENCE_KIND_BY_PROVIDER = {
    "news": "news",
    "chart": "chart",
    "chart-analysis": "chart",
    "market-data": "chart",
    "financial": "fin",
    "ten-k-profile": "fin",
}

MARKET_EVENT_METRIC_LABELS: tuple[tuple[str, str, str], ...] = (
    ("volumeRatio", "20일 평균 대비 거래량", "x"),
    ("relativeVolume", "20일 평균 대비 거래량", "x"),
    ("changePercent", "당일 등락", "%"),
    ("changePct", "당일 등락", "%"),
    ("priceChangePercent", "당일 등락", "%"),
)

_BLOCK_TAG_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]{0,200}>")
_URL_RE = re.compile(r"https?://\S+|\b\w+://\S+")
_TECH_ERROR_RE = re.compile(
    r"\s*[:(]?\s*(HTTPConnectionPool|HTTPSConnectionPool|Max retries|Traceback|NewConnectionError|"
    r"ConnectionError|ConnectionRefused|TimeoutError|Errno|\w+Exception\b|\w+Error\b).*",
    re.S,
)
_SPACE_RE = re.compile(r"[ \t]+")


def build_report_view(report: dict[str, Any] | None) -> dict[str, Any] | None:
    """리포트 dict 에서 view 를 만든다. 보여 줄 내용이 하나도 없으면 None."""
    if not isinstance(report, dict):
        return None
    final_answer = _dict(report.get("finalAnswer"))
    final_response = _dict(report.get("finalResponse"))
    findings = [item for item in _list(report.get("findings")) if isinstance(item, dict)]
    conclusion = (
        clean_text(final_answer.get("summary"))
        or clean_text(final_response.get("summary"))
        or clean_text(report.get("summary"))
    )
    sections = _sections(report, findings, final_answer)
    if not conclusion and not sections:
        return None
    return {
        "conclusion": conclusion,
        "sections": sections,
        "kv": _kv(report, findings, final_response),
        "steps": _steps(report, findings, final_answer, final_response),
        "confidence": _confidence(final_response, findings, report),
        "suggestions": _suggestions(report),
    }


def clean_text(value: Any, *, limit: int = MAX_TEXT_LENGTH) -> str:
    """화면에 실을 평문. HTML 태그·URL·기술 오류 꼬리를 지우고 길이를 자른다."""
    if value is None:
        return ""
    text = str(value)
    text = _BLOCK_TAG_RE.sub("", text)
    text = _TAG_RE.sub("", text)
    text = _TECH_ERROR_RE.sub("", text)
    text = _URL_RE.sub("", text)
    text = text.replace("<", "").replace(">", "")
    lines = [_SPACE_RE.sub(" ", line).strip() for line in text.splitlines()]
    text = "\n".join(line for line in lines if line).strip().rstrip(":：,(").strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _role_key(finding: dict[str, Any]) -> str | None:
    text = f"{finding.get('agentId') or ''} {finding.get('role') or ''}".lower()
    if "guardrail" in text or "verification" in text:
        return "guardrail"
    if "summary" in text:
        return "summary"
    if "unusual" in text or "event-explainer" in text:
        return "unusual"
    if "ontology" in text or "relationship" in text:
        return "relationship"
    for key in ("chart", "news", "financial", "macro", "risk"):
        if key in text:
            return key
    return None


def _sections(report: dict[str, Any], findings: list[dict[str, Any]], final_answer: dict[str, Any]) -> list[dict[str, Any]]:
    by_role: dict[str, list[dict[str, Any]]] = {}
    for finding in findings:
        key = _role_key(finding)
        if key is None or key in INTERNAL_ROLES:
            continue
        if key == "unusual" and not _list(report.get("marketEvents")):
            continue
        by_role.setdefault(key, []).append(finding)
    sections: list[dict[str, Any]] = []
    for key, title, _step in ROLE_ORDER:
        group = by_role.get(key)
        if not group:
            continue
        body = "\n".join(dict.fromkeys(text for text in (clean_text(item.get("summary")) for item in group) if text))
        evidence = _evidence_refs(item for finding in group for item in _list(finding.get("evidence")))
        if body or evidence:
            sections.append({"title": title, "body": body, "evidence": evidence})
    if not sections:
        for section in _list(final_answer.get("sections")):
            if not isinstance(section, dict):
                continue
            title = clean_text(section.get("title"), limit=60)
            body = "\n".join(text for text in (clean_text(bullet) for bullet in _list(section.get("bullets"))) if text)
            if title and body:
                sections.append({"title": title, "body": body, "evidence": []})
        if sections:
            news_refs = _evidence_refs(_list(report.get("providerEvidence")))
            if news_refs:
                sections[0]["evidence"] = news_refs
    return sections[:MAX_SECTIONS]


def _evidence_refs(items: Any) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "").lower() in UNAVAILABLE_EVIDENCE_STATUSES:
            continue
        kind = EVIDENCE_KIND_BY_PROVIDER.get(str(item.get("provider") or "").lower())
        if kind is None:
            continue
        raw = _dict(item.get("raw"))
        if kind == "news":
            ref = raw.get("articleId") or raw.get("article_id") or item.get("title")
        elif kind == "chart":
            ref = raw.get("kind") or raw.get("indicator") or raw.get("snapshotType") or item.get("title")
        else:
            ref = raw.get("metric") or raw.get("concept") or raw.get("formType") or item.get("title")
        ref_text = str(ref or "").strip()[:200]
        if not ref_text or (kind, ref_text) in seen:
            continue
        seen.add((kind, ref_text))
        refs.append({"kind": kind, "ref": ref_text})
        if len(refs) >= MAX_EVIDENCE_PER_SECTION:
            break
    return refs


def _available_news_count(report: dict[str, Any], findings: list[dict[str, Any]]) -> int:
    items = list(_list(report.get("providerEvidence")))
    for finding in findings:
        items.extend(_list(finding.get("evidence")))
    return len([ref for ref in _evidence_refs_unbounded(items) if ref["kind"] == "news"])


def _evidence_refs_unbounded(items: list[Any]) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict) or str(item.get("provider") or "").lower() != "news":
            continue
        if str(item.get("status") or "").lower() in UNAVAILABLE_EVIDENCE_STATUSES:
            continue
        raw = _dict(item.get("raw"))
        key = str(raw.get("articleId") or item.get("url") or item.get("title") or "")
        if key and key not in seen:
            seen.add(key)
            refs.append({"kind": "news", "ref": key})
    return refs


def _kv(report: dict[str, Any], findings: list[dict[str, Any]], final_response: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    used: set[str] = set()
    for event in _list(report.get("marketEvents")):
        metrics = _dict(_dict(event).get("metrics"))
        for key, label, unit in MARKET_EVENT_METRIC_LABELS:
            value = _number(metrics.get(key))
            if value is None or label in used:
                continue
            used.add(label)
            if unit == "x":
                rows.append({"k": label, "v": f"{value:.1f}배"})
            else:
                rows.append({"k": label, "v": f"{value:+.2f}%"})
    news_count = _available_news_count(report, findings)
    if news_count:
        rows.append({"k": "확인한 뉴스", "v": f"{news_count}건"})
    citations = _list(_dict(report.get("finalAnswer")).get("citations"))
    if citations:
        rows.append({"k": "인용한 자료", "v": f"{len(citations)}건"})
    if final_response.get("partial_data_used"):
        rows.append({"k": "자료 상태", "v": "일부 자료 없음"})
    return rows


def _steps(
    report: dict[str, Any],
    findings: list[dict[str, Any]],
    final_answer: dict[str, Any],
    final_response: dict[str, Any],
) -> list[str]:
    steps = ["질문 이해"]
    roles = {_role_key(finding) for finding in findings}
    news_count = _available_news_count(report, findings)
    for key, _title, step in ROLE_ORDER:
        if key not in roles:
            continue
        if key == "unusual" and not _list(report.get("marketEvents")):
            continue
        if key == "news":
            steps.append(f"뉴스 {news_count}건 확인" if news_count else "뉴스 확인(관련 기사 없음)")
        else:
            steps.append(step)
    if final_answer or final_response:
        steps.append("답변 정리")
    return steps


def _confidence(final_response: dict[str, Any], findings: list[dict[str, Any]], report: dict[str, Any]) -> str:
    value = _number(final_response.get("confidence"))
    if value is None:
        scores = [
            score
            for score in (_number(item.get("confidence")) for item in findings if _role_key(item) not in INTERNAL_ROLES)
            if score is not None
        ]
        value = sum(scores) / len(scores) if scores else _number(_dict(report.get("route")).get("confidence"))
    if value is None:
        return CONFIDENCE_LOW
    label = CONFIDENCE_HIGH if value >= 0.7 else CONFIDENCE_MEDIUM if value >= 0.45 else CONFIDENCE_LOW
    if label == CONFIDENCE_HIGH and final_response.get("partial_data_used"):
        return CONFIDENCE_MEDIUM
    return label


def _suggestions(report: dict[str, Any]) -> list[dict[str, Any]]:
    suggestions: list[dict[str, Any]] = []
    for proposal in _list(report.get("tradeConditionProposals")):
        proposal = _dict(proposal)
        price = _number(proposal.get("triggerPrice"))
        symbol = _ticker(proposal.get("symbol"))
        if price is None or not symbol:
            continue
        direction = str(proposal.get("direction") or "").lower()
        where = "아래로" if direction in {"below", "down", "lte", "le"} else "위로"
        suggestions.append({
            "kind": "rule",
            "label": f"{price:.2f} {where} 가면 알리기",
            "payload": {
                "symbol": symbol,
                "direction": "below" if where == "아래로" else "above",
                "triggerPrice": price,
                "proposalId": proposal.get("proposalId"),
            },
        })
    symbol = _ticker(report.get("symbol"))
    compare = _compare_symbols(report, symbol)
    if len(compare) >= 2:
        suggestions.append({
            "kind": "compare",
            "label": f"{' · '.join(compare)} 비교",
            "symbols": compare,
            "payload": {"symbols": compare},
        })
    if symbol:
        suggestions.append({
            "kind": "watch",
            "label": f"[[{symbol}]] 관심 종목에 추가",
            "payload": {"symbol": symbol},
        })
    return suggestions[:MAX_SUGGESTIONS]


def _compare_symbols(report: dict[str, Any], primary: str) -> list[str]:
    symbols: list[str] = [primary] if primary else []
    for entity in _list(report.get("resolvedEntities")):
        ticker = _ticker(_dict(entity).get("ticker"))
        if ticker and ticker not in symbols:
            symbols.append(ticker)
    trace = _dict(report.get("agentTrace"))
    retrieval = _dict(trace.get("retrievalContext"))
    for related in _list(retrieval.get("related_symbols") or retrieval.get("relatedSymbols")):
        ticker = _ticker(_dict(related).get("symbol") if isinstance(related, dict) else related)
        if ticker and ticker not in symbols:
            symbols.append(ticker)
    return symbols[:3]


def _ticker(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text in NON_TICKER_SYMBOLS or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", text):
        return ""
    return text


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []
