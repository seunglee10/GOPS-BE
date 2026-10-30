"""오늘 시장 요약(D15) 생성기.

출력 형태(docs/MOCK_API_CONTRACT.md D15):
{marketDate, generatedAt, paragraphs:[{text, refs:[{kind:"symbol"|"news"|"macro", value}]}]}
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .llm import JsonModelClient, SummaryModelError
from .names import display_name
from .text_rules import clean_text, finite, isoformat_z, require_sentence, signed_percent, utc_now

logger = logging.getLogger(__name__)

PROMPT_VERSION = "market-brief.ko.v1"
MAX_PARAGRAPHS = 4
PARAGRAPH_MAX_CHARS = 320
MAX_REFS = 8
REF_KINDS = ("symbol", "news", "macro")
HEADLINE_INDICES = ("^GSPC", "^IXIC", "^DJI", "^SOX")
MACRO_LEVELS = ("^TNX", "^VIX")
MOVER_COUNT = 3
NEWS_COUNT = 3

INSTRUCTIONS = (
    "당신은 한국 개인 투자자에게 미국 장 마감 뒤 시장을 정리해 주는 편집자입니다. "
    "입력 JSON 의 지수 등락, 업종 등락, 많이 움직인 종목, 오늘 기사만 근거로 2~4개 문단을 씁니다. "
    "입력에 없는 숫자나 사실은 만들지 않습니다. 모든 문장은 해요체로 끝냅니다(예: '반도체가 지수 상승을 이끌었어요.'). "
    "앞으로의 가격, 사라·팔라는 말은 쓰지 않습니다. "
    "각 문단의 refs 에는 그 문단이 다룬 대상만 넣습니다: 종목은 kind=symbol(value=티커), "
    "기사는 kind=news(value=newsId), 지수·금리·VIX 는 kind=macro(value=지수 심볼). 입력에 있는 값만 씁니다."
)

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "paragraphs": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "text": {"type": "string"},
                    "refs": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "kind": {"type": "string", "enum": list(REF_KINDS)},
                                "value": {"type": "string"},
                            },
                            "required": ["kind", "value"],
                        },
                    },
                },
                "required": ["text", "refs"],
            },
        }
    },
    "required": ["paragraphs"],
}


@dataclass
class MarketBriefInput:
    """market_date: 미국 동부 기준 거래일 YYYY-MM-DD.

    indices: 지수 스냅샷 items [{symbol, nameKo, price, changePercent}]
    heatmap_items: 히트맵 projection items [{symbol, sectorLabelKo|sector, changePercent, marketCap, sessionDollarVolume}]
    news: 오늘 기사 [{newsId, title, summary, symbols}]
    """

    market_date: str
    indices: list[dict[str, Any]] = field(default_factory=list)
    heatmap_items: list[dict[str, Any]] = field(default_factory=list)
    news: list[dict[str, Any]] = field(default_factory=list)

    def has_data(self) -> bool:
        return any(finite(item.get("changePercent")) is not None for item in self.indices) or any(
            finite(item.get("changePercent")) is not None for item in self.heatmap_items
        )


class MarketBriefGenerator:
    def __init__(self, model_client: JsonModelClient | None = None, *, now: Callable[[], datetime] | None = None) -> None:
        self.model_client = model_client
        self.now = now or utc_now

    def generate(self, data: MarketBriefInput) -> dict[str, Any]:
        return self.generate_with_source(data)[0]

    def generate_with_source(self, data: MarketBriefInput) -> tuple[dict[str, Any], str]:
        facts = brief_facts(data)
        base = {"marketDate": data.market_date, "generatedAt": isoformat_z(self.now())}
        if self.model_client is not None:
            try:
                output = self.model_client.generate_json(
                    name="market_brief_ko_v1",
                    instructions=INSTRUCTIONS,
                    payload={"promptVersion": PROMPT_VERSION, "marketDate": data.market_date, **facts},
                    schema=OUTPUT_SCHEMA,
                )
                return {**base, "paragraphs": validate_brief_output(output, facts)}, "llm"
            except (SummaryModelError, ValueError, TypeError, KeyError) as exc:
                logger.warning("market brief model fallback date=%s reason=%s", data.market_date, exc)
        return {**base, "paragraphs": template_paragraphs(facts)}, "template"


# ---------- 사실 정리 ----------

def brief_facts(data: MarketBriefInput) -> dict[str, Any]:
    indices = {}
    for item in data.indices:
        symbol = str(item.get("symbol") or "")
        if symbol in HEADLINE_INDICES + MACRO_LEVELS:
            indices[symbol] = {
                "symbol": symbol,
                "nameKo": item.get("nameKo") or symbol,
                "price": finite(item.get("price")),
                "changePercent": finite(item.get("changePercent")),
            }
    rows = [
        {
            "symbol": str(item.get("symbol") or "").upper(),
            "sector": item.get("sectorLabelKo") or item.get("sector") or "기타",
            "changePercent": finite(item.get("changePercent")),
            "marketCap": finite(item.get("marketCap")) or 0.0,
            "dollarVolume": finite(item.get("sessionDollarVolume")) or 0.0,
        }
        for item in data.heatmap_items
        if item.get("symbol")
    ]
    priced = [row for row in rows if row["changePercent"] is not None]
    sectors: dict[str, list[dict[str, Any]]] = {}
    for row in priced:
        sectors.setdefault(row["sector"], []).append(row)
    sector_moves = []
    for name, members in sectors.items():
        total_cap = sum(member["marketCap"] for member in members)
        if total_cap > 0:
            change = sum(member["changePercent"] * member["marketCap"] for member in members) / total_cap
        else:
            change = sum(member["changePercent"] for member in members) / len(members)
        sector_moves.append({"sectorKo": name, "changePercent": round(change, 2), "count": len(members)})
    sector_moves.sort(key=lambda row: row["changePercent"], reverse=True)
    by_change = sorted(priced, key=lambda row: row["changePercent"], reverse=True)
    gainers = [_mover(row) for row in by_change[:MOVER_COUNT] if row["changePercent"] > 0]
    losers = [_mover(row) for row in reversed(by_change[-MOVER_COUNT:]) if row["changePercent"] < 0]
    most_traded = [_mover(row) for row in sorted(priced, key=lambda row: row["dollarVolume"], reverse=True)[:MOVER_COUNT] if row["dollarVolume"] > 0]
    news = []
    for item in data.news:
        news_id = clean_text(item.get("newsId") or item.get("articleId"), 120)
        title = clean_text(item.get("title"), 140)
        if news_id and title and all(existing["newsId"] != news_id for existing in news):
            symbols = [str(s).upper() for s in item.get("symbols") or [] if str(s).strip()]
            news.append({"newsId": news_id, "title": title, "summary": clean_text(item.get("summary"), 300), "symbols": symbols[:4]})
    return {
        "indices": [indices[s] for s in HEADLINE_INDICES + MACRO_LEVELS if s in indices],
        "sectors": sector_moves,
        "gainers": gainers,
        "losers": losers,
        "mostTraded": most_traded,
        "news": news[:NEWS_COUNT * 2],
    }


def _mover(row: dict[str, Any]) -> dict[str, Any]:
    return {"symbol": row["symbol"], "nameKo": display_name(row["symbol"]), "changePercent": round(row["changePercent"], 2)}


# ---------- 검증 ----------

def validate_brief_output(output: Any, facts: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(output, dict) or not isinstance(output.get("paragraphs"), list):
        raise ValueError("paragraphs must be a list")
    raw = output["paragraphs"]
    if not 1 <= len(raw) <= MAX_PARAGRAPHS:
        raise ValueError("paragraph count is out of range")
    allowed = {
        "macro": {item["symbol"] for item in facts["indices"]},
        "news": {item["newsId"] for item in facts["news"]},
        "symbol": {
            item["symbol"]
            for key in ("gainers", "losers", "mostTraded")
            for item in facts[key]
        } | {symbol for item in facts["news"] for symbol in item["symbols"]},
    }
    paragraphs = []
    for index, paragraph in enumerate(raw):
        if not isinstance(paragraph, dict):
            raise ValueError(f"paragraphs[{index}] must be an object")
        text = require_sentence(paragraph.get("text"), field=f"paragraphs[{index}].text", max_length=PARAGRAPH_MAX_CHARS)
        refs = []
        for ref in paragraph.get("refs") or []:
            kind = ref.get("kind") if isinstance(ref, dict) else None
            value = str(ref.get("value") or "") if isinstance(ref, dict) else ""
            if kind not in allowed or value not in allowed[kind]:
                raise ValueError(f"paragraphs[{index}] has an unknown ref {kind}:{value}")
            if {"kind": kind, "value": value} not in refs:
                refs.append({"kind": kind, "value": value})
        paragraphs.append({"text": text, "refs": refs[:MAX_REFS]})
    return paragraphs


# ---------- 템플릿 ----------

def template_paragraphs(facts: dict[str, Any]) -> list[dict[str, Any]]:
    paragraphs = []
    index_rows = [row for row in facts["indices"] if row["symbol"] in HEADLINE_INDICES and row["changePercent"] is not None]
    if index_rows:
        moves = ", ".join(f"{row['nameKo']} 지수는 {signed_percent(row['changePercent'])}" for row in index_rows)
        text = f"{moves} 움직였어요."
        refs = [{"kind": "macro", "value": row["symbol"]} for row in index_rows]
        levels = []
        for row in facts["indices"]:
            if row["symbol"] == "^TNX" and row["price"] is not None:
                levels.append(f"미국 10년물 금리는 {row['price']:.2f}% 수준")
                refs.append({"kind": "macro", "value": row["symbol"]})
            if row["symbol"] == "^VIX" and row["price"] is not None:
                levels.append(f"VIX는 {row['price']:.1f} 수준")
                refs.append({"kind": "macro", "value": row["symbol"]})
        if levels:
            text += f" {', '.join(levels)}이에요."
        paragraphs.append({"text": text, "refs": refs})

    sector_parts = []
    refs: list[dict[str, str]] = []
    sectors = facts["sectors"]
    if len(sectors) >= 2:
        top, bottom = sectors[0], sectors[-1]
        sector_parts.append(
            f"업종별로 가장 강한 곳은 {top['sectorKo']} {signed_percent(top['changePercent'])}, "
            f"가장 약한 곳은 {bottom['sectorKo']} {signed_percent(bottom['changePercent'])}였어요."
        )
    if facts["gainers"]:
        sector_parts.append(f"가장 많이 오른 종목은 {_mover_list(facts['gainers'])}{_ending(facts['gainers'])}")
        refs += [{"kind": "symbol", "value": row["symbol"]} for row in facts["gainers"]]
    if facts["losers"]:
        sector_parts.append(f"가장 많이 내린 종목은 {_mover_list(facts['losers'])}{_ending(facts['losers'])}")
        refs += [{"kind": "symbol", "value": row["symbol"]} for row in facts["losers"]]
    if sector_parts:
        paragraphs.append({"text": " ".join(sector_parts), "refs": _dedupe_refs(refs)})

    news = facts["news"][:NEWS_COUNT]
    if news:
        titles = ", ".join(f"“{item['title']}”" for item in news)
        refs = [{"kind": "news", "value": item["newsId"]} for item in news]
        refs += [{"kind": "symbol", "value": symbol} for item in news for symbol in item["symbols"][:2]]
        paragraphs.append({"text": f"오늘 많이 다룬 소식은 {titles} 기사예요.", "refs": _dedupe_refs(refs)[:MAX_REFS]})
    return paragraphs


def _mover_list(rows: list[dict[str, Any]]) -> str:
    return ", ".join(f"{row['nameKo']} {signed_percent(row['changePercent'])}" for row in rows)


def _ending(rows: list[dict[str, Any]]) -> str:
    # 숫자 끝 '%'(퍼센트)는 모음으로 끝나 '예요', 여러 개면 '순이에요'.
    return " 순이에요." if len(rows) > 1 else "예요."


def _dedupe_refs(refs: list[dict[str, str]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for ref in refs:
        if ref not in out:
            out.append(ref)
    return out
