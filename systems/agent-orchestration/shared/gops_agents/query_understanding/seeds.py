from __future__ import annotations

from typing import Any


# Fallback seeds only. Operational company/theme coverage should come from
# market_data.symbols, GraphDB, or the alias catalog artifact.
COMPANY_SYMBOL_ALIASES: tuple[tuple[str, str], ...] = (
    ("apple", "AAPL"),
    ("apple inc", "AAPL"),
    ("애플", "AAPL"),
    ("nvidia", "NVDA"),
    ("nvidia corp", "NVDA"),
    ("엔비디아", "NVDA"),
    ("datadog", "DDOG"),
    ("data dog", "DDOG"),
    ("데이터독", "DDOG"),
    ("oracle", "ORCL"),
    ("오라클", "ORCL"),
    ("tesla", "TSLA"),
    ("테슬라", "TSLA"),
    ("microsoft", "MSFT"),
    ("마이크로소프트", "MSFT"),
    ("마소", "MSFT"),
    ("amazon", "AMZN"),
    ("아마존", "AMZN"),
    ("google", "GOOGL"),
    ("alphabet", "GOOGL"),
    ("구글", "GOOGL"),
    ("알파벳", "GOOGL"),
    ("meta", "META"),
    ("meta platforms", "META"),
    ("메타", "META"),
    ("amd", "AMD"),
    ("advanced micro devices", "AMD"),
    ("브로드컴", "AVGO"),
    ("broadcom", "AVGO"),
    ("intel", "INTC"),
    ("인텔", "INTC"),
    ("netflix", "NFLX"),
    ("넷플릭스", "NFLX"),
    ("palantir", "PLTR"),
    ("팔란티어", "PLTR"),
)


NEWS_TOPIC_BASKETS: tuple[dict[str, Any], ...] = (
    {
        "label": "반도체",
        "aliases": (
            "반도체",
            "반도체주",
            "반도체 섹터",
            "반도체 관련",
            "semiconductor",
            "semiconductors",
            "chip",
            "chips",
            "ai chip",
            "gpu",
            "memory chip",
            "hbm",
            "메모리",
        ),
        "symbols": ("NVDA", "AMD", "AVGO", "TSM", "ASML", "MU", "INTC", "QCOM", "TXN", "ADI", "MRVL", "AMAT", "LRCX", "KLAC"),
    },
    {
        "label": "AI",
        "aliases": ("ai 테마", "ai 관련", "인공지능", "생성형 ai", "artificial intelligence"),
        "symbols": ("NVDA", "MSFT", "GOOGL", "META", "AMZN", "AMD", "AVGO", "PLTR", "ARM"),
    },
    {
        "label": "클라우드",
        "aliases": ("클라우드", "cloud", "saas", "소프트웨어"),
        "symbols": ("MSFT", "AMZN", "GOOGL", "ORCL", "CRM", "DDOG", "SNOW", "NET"),
    },
)


EXTRA_KNOWN_SYMBOLS: tuple[str, ...] = ("XLV",)
