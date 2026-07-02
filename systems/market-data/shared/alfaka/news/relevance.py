from __future__ import annotations

import re
from typing import Any


SUBJECT_LEVELS = {"primary", "secondary", "mention", "irrelevant"}

COMPANY_DIRECT_TERMS = {
    "AAPL": (
        "apple",
        "aapl",
        "tim cook",
        "iphone",
        "ipad",
        "mac",
        "app store",
        "vision pro",
        "ios",
        "애플",
        "팀 쿡",
    ),
    "NVDA": ("nvidia", "nvda", "jensen huang", "geforce", "cuda", "엔비디아", "젠슨 황"),
    "AMD": ("advanced micro devices", "amd", "radeon", "epyc", "ryzen"),
    "MU": ("micron", "mu", "마이크론"),
    "AVGO": ("broadcom", "avgo", "브로드컴"),
    "TSM": ("taiwan semiconductor", "tsmc", "tsm"),
    "ASML": ("asml",),
    "AMAT": ("applied materials", "amat"),
    "DDOG": ("datadog", "ddog"),
    "MSFT": ("microsoft", "msft", "azure", "windows"),
    "GOOGL": ("alphabet", "google", "googl", "goog"),
    "GOOG": ("alphabet", "google", "googl", "goog"),
    "META": ("meta platforms", "meta", "facebook", "instagram"),
    "AMZN": ("amazon", "amzn", "aws"),
    "TSLA": ("tesla", "tsla", "elon musk"),
}

MARKET_BROAD_TERMS = (
    "whale activity",
    "options activity",
    "today's session",
    "market positives",
    "jobs data",
    "bitcoin",
    "s&p 500",
    "spdr",
    "qqq",
    "spy",
    "sector",
    "stocks whale",
    "information technology stocks",
    "대규모 거래",
    "고래",
    "시장 호재",
    "비트코인",
)

LISTICLE_TERMS = (
    "final trades",
    "halftime report",
    "10 ",
    "top ",
    "watchlist",
    "stocks to watch",
    "최종 매매",
    "거론",
)

TARGET_CONTEXT_TERMS = (
    "analyst",
    "bullish",
    "bearish",
    "buy",
    "sell",
    "upgrade",
    "downgrade",
    "raises",
    "cuts",
    "target",
    "earnings",
    "revenue",
    "guidance",
    "sales",
    "profit",
    "shortage",
    "supply",
    "demand",
    "launch",
    "contract",
    "deal",
    "stock",
    "shares",
    "주가",
    "실적",
    "매출",
    "가이던스",
    "목표가",
    "상향",
    "하향",
    "공급",
    "수요",
    "계약",
    "출시",
    "강세",
    "약세",
)


def classify_subject_relevance(
    *,
    target_symbol: str,
    headline: Any,
    summary: Any = None,
    content: Any = None,
    symbols: Any = None,
) -> dict[str, Any]:
    symbol = normalize_symbol(target_symbol)
    article_symbols = normalize_symbols(symbols)
    title = clean_text(headline)
    body = clean_text(summary)
    full_text = " ".join(part for part in [title, body, clean_text(content)] if part)
    full_lower = full_text.lower()
    title_lower = title.lower()
    direct_signals = direct_subject_signals(symbol, full_text)
    title_signals = direct_subject_signals(symbol, title)
    body_has_target_context = has_target_context(symbol, body)
    symbol_count = len(article_symbols)
    in_symbols = symbol in article_symbols if symbol else False
    broad = any(term in full_lower for term in MARKET_BROAD_TERMS)
    listicle = any(term in full_lower for term in LISTICLE_TERMS)

    if not symbol:
        level = "irrelevant"
        reason = "missing target symbol"
    elif listicle and (title_signals or direct_signals) and not broad:
        if body_has_target_context:
            level = "secondary"
            reason = "target company has a concrete sentence in list-style article"
        else:
            level = "mention"
            reason = "target company appears in list-style article without direct context"
    elif title_signals and not broad:
        level = "primary"
        reason = "target company appears in headline"
    elif direct_signals and symbol_count <= 4 and not broad:
        level = "secondary" if listicle else "primary"
        reason = "target company appears directly in article text"
    elif direct_signals and symbol_count > 4:
        level = "mention"
        reason = "target company appears in broad multi-symbol article"
    elif in_symbols:
        level = "mention"
        reason = "target symbol appears only in provider metadata"
    else:
        level = "irrelevant"
        reason = "target company not found in article text or metadata"

    score = relevance_score_for_level(level)
    if level in {"primary", "secondary"} and symbol_count >= 5:
        score = min(score, 0.7)
    if broad and level != "irrelevant":
        score = min(score, 0.45)
        if level in {"primary", "secondary"}:
            level = "mention"
            reason = "broad market article with only weak target-company focus"

    return {
        "targetSymbol": symbol,
        "subjectRelevance": level,
        "relevanceScoreV2": round(score, 2),
        "relevanceReason": reason,
        "directSignals": direct_signals,
    }


def direct_subject_signals(symbol: str, text: str) -> list[str]:
    symbol = normalize_symbol(symbol)
    text_value = clean_text(text)
    text_lower = text_value.lower()
    signals = []
    terms = COMPANY_DIRECT_TERMS.get(symbol, ())
    for term in terms:
        if term and term.lower() in text_lower and term not in signals:
            signals.append(term)
    if symbol and re.search(rf"\b{re.escape(symbol)}\b", text_value.upper()) and symbol not in signals:
        signals.append(symbol)
    return signals


def has_target_context(symbol: str, text: str) -> bool:
    text_value = clean_text(text)
    if not text_value:
        return False
    signals = direct_subject_signals(symbol, text_value)
    if not signals:
        return False
    for sentence in split_sentences(text_value):
        sentence_lower = sentence.lower()
        if not any(signal.lower() in sentence_lower for signal in signals):
            continue
        if any(term in sentence_lower for term in TARGET_CONTEXT_TERMS):
            return True
    return False


def split_sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"[.!?。！？]\s+|\n+", text) if part.strip()]


def relevance_score_for_level(level: str) -> float:
    return {
        "primary": 0.95,
        "secondary": 0.75,
        "mention": 0.35,
        "irrelevant": 0.0,
    }.get(level, 0.0)


def normalize_subject_level(value: Any) -> str:
    level = str(value or "").strip().lower()
    return level if level in SUBJECT_LEVELS else "mention"


def is_direct_subject(value: Any) -> bool:
    return normalize_subject_level(value) in {"primary", "secondary"}


def normalize_symbols(values: Any) -> list[str]:
    if isinstance(values, str):
        values = [values]
    result = []
    for value in values or []:
        symbol = normalize_symbol(value)
        if symbol and symbol not in result:
            result.append(symbol)
    return result


def normalize_symbol(value: Any) -> str:
    return str(value or "").strip().upper()


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())
