"""종목 기준정보 중 코드와 함께 배포되는 정적 참조 데이터.

instrument_names_ko.json 은 S&P 500 종목의 한글 이름·거래소 한글 표기다.
API 는 이 파일을 직접 읽어 nameKo/exchangeKo 를 내려 주고, 같은 내용을
Postgres instrument_names_ko 테이블에도 적재한다(운영 조회·ERD 용).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

INSTRUMENT_NAMES_KO_FILENAME = "instrument_names_ko.json"


def instrument_names_ko_path() -> Path:
    return Path(__file__).resolve().parent / INSTRUMENT_NAMES_KO_FILENAME


@lru_cache(maxsize=1)
def load_instrument_names_ko() -> dict[str, dict[str, Any]]:
    """심볼(대문자) -> {"nameKo", "exchange", "exchangeKo"} 매핑을 한 번만 읽어 돌려준다."""
    with instrument_names_ko_path().open(encoding="utf-8") as handle:
        payload = json.load(handle)
    items = payload.get("items") if isinstance(payload, dict) else payload
    names: dict[str, dict[str, Any]] = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").strip().upper()
        name_ko = str(item.get("nameKo") or "").strip()
        if not symbol or not name_ko:
            continue
        exchange_ko = str(item.get("exchangeKo") or "").strip() or None
        names[symbol] = {
            "nameKo": name_ko,
            "exchange": str(item.get("exchange") or "").strip() or None,
            "exchangeKo": exchange_ko,
        }
    return names
