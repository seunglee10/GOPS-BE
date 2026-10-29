"""종목 한글 이름 조회.

market_data.reference 의 JSON 이 원본이다. Postgres instrument_names_ko 테이블은
같은 내용을 운영용으로 복제한 것이라 API 는 DB 없이도 nameKo 를 돌려준다.
"""

from __future__ import annotations

from typing import Any

from market_data.reference import load_instrument_names_ko


def _entry(symbol: Any) -> dict[str, Any] | None:
    key = str(symbol or "").strip().upper()
    if not key:
        return None
    return load_instrument_names_ko().get(key)


def name_ko(symbol: Any) -> str | None:
    entry = _entry(symbol)
    return entry["nameKo"] if entry else None


def exchange_ko(symbol: Any) -> str | None:
    entry = _entry(symbol)
    return entry.get("exchangeKo") if entry else None


def korean_name_fields(symbol: Any) -> dict[str, str | None]:
    """응답 dict 에 펼쳐 넣을 {"nameKo", "exchangeKo"}. 시드에 없으면 둘 다 None."""
    entry = _entry(symbol)
    if not entry:
        return {"nameKo": None, "exchangeKo": None}
    return {"nameKo": entry["nameKo"], "exchangeKo": entry.get("exchangeKo")}


def with_korean_names(item: dict[str, Any], symbol_key: str = "symbol") -> dict[str, Any]:
    """item 에 nameKo/exchangeKo 를 덧붙인 새 dict 를 돌려준다. 기존 키는 건드리지 않는다."""
    return {**item, **korean_name_fields(item.get(symbol_key))}
