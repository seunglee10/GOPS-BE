"""Finnhub 경제지표 이름 → 한글 이름·중요도 하한.

Finnhub 은 "CPI YoY", "Non Farm Payrolls", "Fed Interest Rate Decision" 처럼 영문 이름만 준다.
자주 나오는 미국·한국 지표만 표로 옮기고, 표에 없으면 title_ko 를 비워 API 가 영문 이름을 쓰게 한다.

한글 이름은 목업 캘린더(CAL_EV)의 표기를 따른다: "소비자물가(CPI)", "비농업 고용", "FOMC 금리 결정".
'발표' 같은 꼬리말은 붙이지 않는다. 전월·전년 대비, 속보치·확정치 같은 구분은 뒤에 덧붙인다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

IMPORTANCE_ORDER = {"normal": 0, "watch": 1, "major": 2}


@dataclass(frozen=True)
class TitleRule:
    pattern: re.Pattern[str]
    title_ko: str
    floor: str = "normal"
    region: str | None = None


def _rule(pattern: str, title_ko: str, floor: str = "normal", region: str | None = None) -> TitleRule:
    return TitleRule(re.compile(pattern, re.I), title_ko, floor, region)


# 위에서부터 처음 맞는 규칙을 쓴다. 더 구체적인 이름(근원 PCE, FOMC 의사록)을 앞에 둔다.
TITLE_RULES: tuple[TitleRule, ...] = (
    # 금리
    _rule(r"\b(fed|fomc)\b.*\b(interest )?rate decision\b|\bfed funds rate\b", "FOMC 금리 결정", "major", "us"),
    _rule(r"\bfomc\b.*\bminutes\b", "FOMC 의사록 공개", "watch", "us"),
    _rule(r"\bfomc\b.*\b(economic projections|press conference)\b", "FOMC 기자회견", "watch", "us"),
    _rule(r"\b(bok|bank of korea)\b.*\brate\b|\binterest rate decision\b", "한국은행 기준금리 결정", "major", "kr"),
    _rule(r"\binterest rate decision\b", "FOMC 금리 결정", "major", "us"),
    # 물가
    _rule(r"\bcore pce\b", "근원 개인소비지출(PCE) 물가", "major", "us"),
    _rule(r"\bpce\b", "개인소비지출(PCE) 물가", "watch", "us"),
    _rule(r"\bcore (cpi|inflation rate)\b", "근원 소비자물가(CPI)", "major"),
    _rule(r"\b(cpi|inflation rate)\b", "소비자물가(CPI)", "major"),
    _rule(r"\bcore ppi\b", "근원 생산자물가(PPI)", "watch"),
    _rule(r"\b(ppi|producer price)", "생산자물가(PPI)", "watch"),
    _rule(r"\bmichigan\b.*\binflation expectations\b", "미시간대 인플레이션 기대치", "normal", "us"),
    _rule(r"\bmichigan\b.*\bsentiment\b", "미시간대 소비자심리지수", "normal", "us"),
    # 고용
    _rule(r"\bnon ?-?farm payrolls\b", "비농업 고용", "major", "us"),
    _rule(r"\badp\b.*\bemployment\b", "ADP 민간 고용", "watch", "us"),
    _rule(r"\bunemployment rate\b", "실업률", "major"),
    _rule(r"\binitial jobless claims\b", "주간 신규실업수당 청구건수", "normal", "us"),
    _rule(r"\bcontinuing jobless claims\b", "연속 실업수당 청구건수", "normal", "us"),
    _rule(r"\bjolts\b", "JOLTS 구인건수", "watch", "us"),
    _rule(r"\baverage hourly earnings\b", "평균 시간당 임금", "watch", "us"),
    # 성장·경기
    _rule(r"\bgdp\b", "경제성장률(GDP)", "major"),
    _rule(r"\bretail sales\b", "소매판매", "watch"),
    _rule(r"\bism manufacturing\b", "ISM 제조업 지수", "watch", "us"),
    _rule(r"\bism (services|non-manufacturing)\b", "ISM 서비스업 지수", "watch", "us"),
    _rule(r"\bindustrial production\b", "산업생산", "normal"),
    _rule(r"\bdurable goods\b", "내구재 주문", "normal", "us"),
    _rule(r"\bcb consumer confidence\b|\bconference board\b", "CB 소비자신뢰지수", "normal", "us"),
    _rule(r"\bconsumer confidence\b", "소비자심리지수", "normal"),
    _rule(r"\bbusiness confidence\b", "기업경기실사지수(BSI)", "normal", "kr"),
    # 주택
    _rule(r"\bnew home sales\b", "신규주택 매매건수", "normal", "us"),
    _rule(r"\bexisting home sales\b", "기존주택 매매건수", "normal", "us"),
    _rule(r"\bhousing starts\b", "주택 착공건수", "normal", "us"),
    _rule(r"\bbuilding permits\b", "건축 허가건수", "normal", "us"),
    # 무역
    _rule(r"\bexports\b", "수출", "watch", "kr"),
    _rule(r"\bimports\b", "수입", "normal", "kr"),
    _rule(r"\btrade balance\b", "무역수지", "watch", "kr"),
    _rule(r"\bexports\b", "수출", "normal"),
    _rule(r"\bimports\b", "수입", "normal"),
    _rule(r"\btrade balance\b", "무역수지", "normal"),
)

# 이름 끝 구분어. 순서대로 붙인다.
_QUALIFIERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bmom\b", re.I), "전월 대비"),
    (re.compile(r"\byoy\b", re.I), "전년 대비"),
    (re.compile(r"\bqoq\b", re.I), "전분기 대비"),
    (re.compile(r"\b(adv|advance)\b", re.I), "속보치"),
    (re.compile(r"\b(prel|prelim|preliminary|flash)\b", re.I), "예비치"),
    (re.compile(r"\b(2nd|second) est", re.I), "잠정치"),
    (re.compile(r"\b(final|3rd est|third est)\b", re.I), "확정치"),
)


def korean_title(title_en: str, region: str) -> tuple[str | None, str]:
    """(title_ko 또는 None, 중요도 하한). 표에 없으면 (None, "normal")."""
    name = " ".join(str(title_en or "").split())
    if not name:
        return None, "normal"
    for rule in TITLE_RULES:
        if rule.region is not None and rule.region != region:
            continue
        if rule.pattern.search(name):
            qualifiers = [label for pattern, label in _QUALIFIERS if pattern.search(name)]
            title = rule.title_ko
            if qualifiers:
                title = f"{title} {' '.join(qualifiers)}"
            return title, rule.floor
    return None, "normal"


def importance_from_impact(impact: object, floor: str = "normal") -> str:
    """Finnhub impact high→major, medium→watch, 그 밖→normal. 매핑표 하한보다 낮아지지 않는다."""
    value = str(impact or "").strip().lower()
    level = {"high": "major", "medium": "watch"}.get(value, "normal")
    return level if IMPORTANCE_ORDER[level] >= IMPORTANCE_ORDER.get(floor, 0) else floor
