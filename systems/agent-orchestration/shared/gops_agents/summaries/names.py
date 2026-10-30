"""종목 한글 이름. 원본은 market_data.reference 의 시드 JSON(API 서버 instrument_names 와 같은 값)."""

from __future__ import annotations

from typing import Any


def display_name(symbol: Any) -> str:
    key = str(symbol or "").strip().upper()
    try:
        from market_data.reference import load_instrument_names_ko

        entry = load_instrument_names_ko().get(key)
    except Exception:
        entry = None
    return (entry or {}).get("nameKo") or key
