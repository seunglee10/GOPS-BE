# 역할: 뉴스 제목·요약을 키워드 규칙으로 market|corp|earn|rate|etf|global 중 하나로 분류합니다.
# 사용: 뉴스 수집(적재) 단계와, category 가 비어 있는 과거 행을 읽을 때 같은 규칙으로 씁니다.
#
# 규칙은 결정적(같은 입력 → 같은 결과)이어야 합니다. LLM 분류를 붙이더라도 이 함수가 기준값입니다.
# 우선순위: earn > rate > etf > global > corp > market. 한 기사에 여러 신호가 섞이면 앞쪽이 이깁니다.
from __future__ import annotations

import re
from typing import Iterable

NEWS_CATEGORIES = ("market", "corp", "earn", "rate", "etf", "global")
DEFAULT_NEWS_CATEGORY = "market"

# 영어는 단어 경계로, 한국어는 부분 문자열로 찾습니다.
_ENGLISH_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("earn", (
        r"earnings?", r"quarterly results?", r"(?:first|second|third|fourth)[- ]quarter", r"q[1-4] (?:results?|revenue|profit|sales)",
        r"eps", r"guidance", r"revenue", r"profits?", r"net income", r"beats? estimates?", r"miss(?:es)? estimates?",
        r"(?:raises|cuts|lowers) (?:its )?(?:outlook|forecast)", r"fiscal (?:year|quarter)",
    )),
    ("rate", (
        r"federal reserve", r"fomc", r"powell", r"interest rates?", r"rate (?:cuts?|hikes?|decision|path)",
        r"cpi", r"pce", r"inflation", r"treasur(?:y|ies)", r"(?:bond|treasury) yields?", r"10-year yield", r"central bank",
    )),
    ("etf", (
        r"etfs?", r"exchange[- ]traded funds?", r"fund (?:flows?|inflows?|outflows?)", r"inflows?", r"outflows?",
    )),
    ("global", (
        r"china", r"chinese", r"beijing", r"europe", r"european", r"eurozone", r"ecb", r"japan", r"japanese", r"boj",
        r"bank of japan", r"tariffs?", r"trade war", r"germany", r"britain", r"u\.k\.", r"hong kong", r"taiwan",
    )),
    ("corp", (
        r"acquires?", r"acquisition", r"merger", r"buyout", r"takeover", r"to buy", r"lawsuit", r"sues?", r"sued",
        r"settlement", r"antitrust", r"recalls?", r"launch(?:es|ed)?", r"unveils?", r"new product", r"partnership",
        r"ceo", r"layoffs?", r"buybacks?", r"dividend", r"stake", r"fda", r"upgrades?", r"downgrades?", r"price target",
        r"contract", r"deal",
    )),
    ("market", (
        r"s&p 500", r"nasdaq", r"dow jones", r"the dow", r"wall street", r"stock market", r"stocks", r"indexes", r"indices",
        r"index", r"sectors?", r"russell 2000", r"sell-?off", r"rally", r"vix",
    )),
)

_KOREAN_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("earn", ("실적", "어닝", "매출", "영업이익", "순이익", "가이던스", "분기 이익", "주당순이익")),
    ("rate", ("금리", "연준", "기준금리", "국채", "물가", "인플레이션", "파월", "소비자물가", "채권 금리")),
    ("etf", ("ETF", "상장지수펀드", "자금 유입", "자금 유출", "자금유입", "자금유출")),
    ("global", ("중국", "유럽", "일본", "관세", "무역전쟁", "무역 전쟁", "홍콩", "대만", "독일", "영국", "유로존")),
    ("corp", ("인수", "합병", "소송", "출시", "제휴", "계약", "리콜", "자사주", "배당", "감원", "목표가", "신제품", "승인")),
    ("market", ("증시", "지수", "나스닥", "다우", "뉴욕증시", "업종", "섹터", "S&P")),
)

_ENGLISH_PATTERNS = tuple(
    (category, re.compile(r"(?<![a-z0-9])(?:" + "|".join(terms) + r")(?![a-z0-9])", re.IGNORECASE))
    for category, terms in _ENGLISH_RULES
)

# 대소문자를 가려야 하는 약칭. "Fed" 는 연준이지만 "fed into" 의 fed 는 아니다.
_CASE_SENSITIVE_PATTERNS = (
    ("rate", re.compile(r"(?<![A-Za-z])Fed(?![A-Za-z])")),
)


def categorize_news(
    headline: str | None,
    summary: str | None = None,
    symbols: Iterable[str] | None = None,
) -> str:
    """제목·요약에서 뉴스 분류를 고릅니다.

    제목에서 먼저 찾고, 제목에 신호가 없을 때만 요약을 봅니다. 요약은 길어서 잡음이 많기 때문입니다.
    아무 신호도 없으면 관련 종목이 하나뿐인 기사는 개별 기업 뉴스(corp), 그 밖에는 market 으로 둡니다.
    """
    for text in (headline, summary):
        category = _match_category(str(text or ""))
        if category:
            return category
    related = [str(symbol or "").strip() for symbol in symbols or [] if str(symbol or "").strip()]
    if len(set(related)) == 1:
        return "corp"
    return DEFAULT_NEWS_CATEGORY


def normalize_news_category(value: object) -> str | None:
    """저장된 값이 허용된 분류면 소문자로, 아니면 None 을 돌려줍니다."""
    text = str(value or "").strip().lower()
    return text if text in NEWS_CATEGORIES else None


def _match_category(text: str) -> str | None:
    if not text.strip():
        return None
    hits = set()
    for category, pattern in _ENGLISH_PATTERNS:
        if pattern.search(text):
            hits.add(category)
    for category, pattern in _CASE_SENSITIVE_PATTERNS:
        if pattern.search(text):
            hits.add(category)
    for category, terms in _KOREAN_RULES:
        if any(term in text for term in terms):
            hits.add(category)
    for category in ("earn", "rate", "etf", "global", "corp", "market"):
        if category in hits:
            return category
    return None
