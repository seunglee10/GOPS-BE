"""종목 AI 요약(D14) 생성기.

출력 형태(docs/MOCK_API_CONTRACT.md D14):
{symbol, generatedAt, head, news:[{text,newsId}], risks:[{text,newsId}], events:[{title,at,note}], chartNote}
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .llm import JsonModelClient, SummaryModelError
from .text_rules import (
    clean_text,
    contains_forbidden,
    finite,
    isoformat_z,
    require_sentence,
    signed_percent,
    utc_now,
)

logger = logging.getLogger(__name__)

PROMPT_VERSION = "stock-summary.ko.v1"
MAX_NEWS = 3
MAX_RISKS = 2
MAX_EVENTS = 4
HEAD_MAX_CHARS = 200
ITEM_MAX_CHARS = 180
NOTE_MAX_CHARS = 80
CHART_NOTE_MAX_CHARS = 40
SMA_WINDOW = 20

INSTRUCTIONS = (
    "당신은 미국 주식을 보는 한국 개인 투자자에게 종목 상황을 정리해 주는 편집자입니다. "
    "입력 JSON 의 기사·차트 수치·일정만 근거로 씁니다. 입력에 없는 사실, 숫자, 회사 입장은 만들지 않습니다. "
    "모든 문장은 해요체로 끝냅니다(예: '데이터센터 수요가 다시 확인되며 거래량이 늘었어요'). "
    "앞으로의 가격, 목표가, 사라·팔라는 말은 쓰지 않습니다. 용어 풀이나 읽는 법도 쓰지 않습니다. "
    "head 는 그날 왜 움직였는지 한두 문장으로 씁니다. news 는 무슨 일이 있었는지, risks 는 걸려 있는 문제를 "
    "기사 하나당 한 문장으로 쓰고 근거 기사의 newsId 를 그대로 붙입니다. "
    "events 는 입력 events 의 title·at 를 그대로 두고 note 에 입력 근거가 있을 때만 짧은 메모를 씁니다(없으면 빈 문자열). "
    "chartNote 는 '20일선 위에서 거래량 늘며 상승' 처럼 40자 이내의 짧은 구절로 씁니다."
)

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "head": {"type": "string"},
        "news": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"text": {"type": "string"}, "newsId": {"type": "string"}},
                "required": ["text", "newsId"],
            },
        },
        "risks": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"text": {"type": "string"}, "newsId": {"type": "string"}},
                "required": ["text", "newsId"],
            },
        },
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"title": {"type": "string"}, "at": {"type": "string"}, "note": {"type": "string"}},
                "required": ["title", "at", "note"],
            },
        },
        "chartNote": {"type": "string"},
    },
    "required": ["head", "news", "risks", "events", "chartNote"],
}


@dataclass
class StockSummaryInput:
    """symbol 하나의 요약 재료.

    news: 최신순 [{newsId, title, summary, publishedAt, impactDirection?, concerns?}]
    candles: 오래된 것부터 일봉 [{timestamp, close, volume, ...}] (최근 20개)
    events: [{title, at(YYYY-MM-DD), note}]
    """

    symbol: str
    news: list[dict[str, Any]] = field(default_factory=list)
    candles: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    name_ko: str | None = None
    sector_label_ko: str | None = None
    sector_change_percent: float | None = None

    def has_data(self) -> bool:
        return bool(self.news) or len(chart_closes(self.candles)) >= 2


class StockSummaryGenerator:
    def __init__(
        self,
        model_client: JsonModelClient | None = None,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.model_client = model_client
        self.now = now or utc_now

    def generate(self, data: StockSummaryInput) -> dict[str, Any]:
        return self.generate_with_source(data)[0]

    def generate_with_source(self, data: StockSummaryInput) -> tuple[dict[str, Any], str]:
        """(summary, "llm" | "template") 를 돌려준다."""
        symbol = str(data.symbol or "").strip().upper()
        facts = chart_facts(data.candles)
        news = normalized_news(data.news)
        events = normalized_events(data.events)
        base = {"symbol": symbol, "generatedAt": isoformat_z(self.now())}
        if self.model_client is not None:
            try:
                output = self.model_client.generate_json(
                    name="stock_summary_ko_v1",
                    instructions=INSTRUCTIONS,
                    payload=model_payload(data, symbol, facts, news, events),
                    schema=OUTPUT_SCHEMA,
                )
                return {**base, **validate_model_output(output, news=news, events=events, facts=facts)}, "llm"
            except (SummaryModelError, ValueError, TypeError, KeyError) as exc:
                logger.warning("stock summary model fallback symbol=%s reason=%s", symbol, exc)
        return {**base, **template_summary(data, facts, news, events)}, "template"


# ---------- 차트 사실 ----------

def chart_closes(candles: list[dict[str, Any]]) -> list[float]:
    return [value for value in (finite(row.get("close")) for row in candles or []) if value is not None and value > 0]


def chart_facts(candles: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [row for row in candles or [] if (finite(row.get("close")) or 0) > 0][-SMA_WINDOW:]
    closes = [float(row["close"]) for row in rows]
    if len(closes) < 2:
        return {"available": False}
    last, previous = closes[-1], closes[-2]
    sma = sum(closes) / len(closes)
    volumes = [finite(row.get("volume")) for row in rows]
    last_volume = volumes[-1]
    prior = [value for value in volumes[:-1] if value is not None and value > 0]
    volume_ratio = (last_volume / (sum(prior) / len(prior))) if last_volume and prior else None
    return {
        "available": True,
        "asOf": rows[-1].get("timestamp"),
        "bars": len(closes),
        "close": round(last, 4),
        "previousClose": round(previous, 4),
        "changePercent": round((last / previous - 1) * 100, 2),
        "sma20": round(sma, 4),
        "aboveSma20": last >= sma,
        "volumeRatio20d": round(volume_ratio, 2) if volume_ratio is not None else None,
    }


def template_chart_note(facts: dict[str, Any]) -> str | None:
    if not facts.get("available"):
        return None
    position = "20일선 위에서" if facts["aboveSma20"] else "20일선 아래에서"
    ratio = facts.get("volumeRatio20d")
    if ratio is None:
        volume = ""
    elif ratio >= 1.2:
        volume = " 거래량 늘며"
    elif ratio <= 0.8:
        volume = " 거래량 줄며"
    else:
        volume = " 평소 거래량으로"
    change = facts["changePercent"]
    direction = "상승" if change >= 0.05 else "하락" if change <= -0.05 else "보합"
    return f"{position}{volume} {direction}"


# ---------- 입력 정리 ----------

def normalized_news(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items or []:
        news_id = clean_text(item.get("newsId") or item.get("articleId"), 120)
        title = clean_text(item.get("title"), 160)
        if not news_id or not title or news_id in seen:
            continue
        seen.add(news_id)
        concerns = item.get("concerns")
        out.append({
            "newsId": news_id,
            "title": title,
            "summary": clean_text(item.get("summary"), 400),
            "publishedAt": item.get("publishedAt"),
            "impactDirection": clean_text(item.get("impactDirection"), 20).lower() or None,
            "concerns": [clean_text(c, 160) for c in concerns if clean_text(c, 160)] if isinstance(concerns, list) else [],
        })
    return out


def normalized_events(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for item in items or []:
        title = clean_text(item.get("title"), 40)
        at = clean_text(item.get("at"), 10)
        if title and at:
            out.append({"title": title, "at": at, "note": clean_text(item.get("note"), NOTE_MAX_CHARS)})
    out.sort(key=lambda event: event["at"])
    return out[:MAX_EVENTS]


def model_payload(
    data: StockSummaryInput,
    symbol: str,
    facts: dict[str, Any],
    news: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "promptVersion": PROMPT_VERSION,
        "symbol": symbol,
        "nameKo": data.name_ko,
        "sector": {"labelKo": data.sector_label_ko, "changePercent": data.sector_change_percent},
        "chart": facts,
        "news": news,
        "events": events,
        "limits": {"news": MAX_NEWS, "risks": MAX_RISKS},
    }


# ---------- LLM 출력 검증 ----------

def validate_model_output(
    output: Any,
    *,
    news: list[dict[str, Any]],
    events: list[dict[str, Any]],
    facts: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(output, dict):
        raise ValueError("model output must be an object")
    allowed_ids = {item["newsId"] for item in news}
    head = require_sentence(output.get("head"), field="head", max_length=HEAD_MAX_CHARS)
    news_rows = _validated_refs(output.get("news"), allowed_ids, field="news", limit=MAX_NEWS)
    risk_rows = _validated_refs(output.get("risks"), allowed_ids, field="risks", limit=MAX_RISKS)
    by_at = {event["at"]: event for event in events}
    notes: dict[str, str] = {}
    raw_events = output.get("events")
    if not isinstance(raw_events, list):
        raise ValueError("events must be a list")
    for item in raw_events:
        if not isinstance(item, dict) or item.get("at") not in by_at:
            raise ValueError("events must come from the input events")
        note = clean_text(item.get("note"), NOTE_MAX_CHARS + 1)
        if len(note) > NOTE_MAX_CHARS or contains_forbidden(note):
            raise ValueError("event note is invalid")
        notes[item["at"]] = note
    validated_events = [{**event, "note": notes.get(event["at"]) or event["note"]} for event in events]
    chart_note: str | None = None
    if facts.get("available"):
        chart_note = clean_text(output.get("chartNote"), CHART_NOTE_MAX_CHARS + 1)
        if not chart_note or len(chart_note) > CHART_NOTE_MAX_CHARS or contains_forbidden(chart_note):
            chart_note = template_chart_note(facts)
    return {
        "head": head,
        "news": news_rows,
        "risks": risk_rows,
        "events": validated_events,
        "chartNote": chart_note,
    }


def _validated_refs(value: Any, allowed_ids: set[str], *, field: str, limit: int) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    rows = []
    for index, item in enumerate(value[:limit]):
        if not isinstance(item, dict):
            raise ValueError(f"{field}[{index}] must be an object")
        news_id = str(item.get("newsId") or "")
        if news_id not in allowed_ids:
            raise ValueError(f"{field}[{index}].newsId is not an input article")
        text = require_sentence(item.get("text"), field=f"{field}[{index}].text", max_length=ITEM_MAX_CHARS)
        rows.append({"text": text, "newsId": news_id})
    return rows


# ---------- 템플릿(LLM 꺼짐·실패) ----------

def template_summary(
    data: StockSummaryInput,
    facts: dict[str, Any],
    news: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    risks_src = [item for item in news if item["impactDirection"] == "negative" or item["concerns"]][:MAX_RISKS]
    risk_ids = {item["newsId"] for item in risks_src}
    news_src = [item for item in news if item["newsId"] not in risk_ids][:MAX_NEWS]
    return {
        "head": template_head(data, facts, news),
        "news": [{"text": _news_sentence(item["summary"] or item["title"]), "newsId": item["newsId"]} for item in news_src],
        "risks": [
            {"text": _news_sentence(item["concerns"][0] if item["concerns"] else (item["summary"] or item["title"])), "newsId": item["newsId"]}
            for item in risks_src
        ],
        "events": events,
        "chartNote": template_chart_note(facts),
    }


def template_head(data: StockSummaryInput, facts: dict[str, Any], news: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    if facts.get("available"):
        change = facts["changePercent"]
        if change >= 0.05:
            move = f"전 거래일보다 {abs(change):.1f}% 올랐"
        elif change <= -0.05:
            move = f"전 거래일보다 {abs(change):.1f}% 내렸"
        else:
            move = "전 거래일과 비슷하게 마감했"
        ratio = facts.get("volumeRatio20d")
        if ratio is not None:
            parts.append(f"{move}고 거래량은 20일 평균의 {ratio:.1f}배였어요.")
        else:
            parts.append(f"{move}어요.")
        sector_change = finite(data.sector_change_percent)
        if data.sector_label_ko and sector_change is not None and abs(change) >= 0.05:
            same = (change > 0) == (sector_change > 0)
            parts.append(
                f"{data.sector_label_ko} 업종({signed_percent(sector_change)})과 "
                f"{'같은 방향이에요' if same else '반대로 움직였어요'}."
            )
    if news:
        parts.append(f"관련 기사 {len(news)}건 중 가장 최근 것은 “{news[0]['title']}” 기사예요.")
    else:
        parts.append("이 종목만 다룬 새 기사는 없어요.")
    return clean_text(" ".join(parts), HEAD_MAX_CHARS * 2)


def _news_sentence(text: str) -> str:
    """기사 요약을 그대로 쓰되 해요체가 아니면 '~ 소식이에요.'로 감싼다."""
    sentence = clean_text(text, ITEM_MAX_CHARS - 10).rstrip(".")
    if sentence.endswith("요"):
        return f"{sentence}."
    return f"“{sentence}” 소식이에요."
