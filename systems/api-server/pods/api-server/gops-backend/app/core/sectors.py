from __future__ import annotations

from typing import Any


UNCLASSIFIED_SECTOR = "Unclassified"

CANONICAL_SECTORS: tuple[str, ...] = (
    "Communication Services",
    "Consumer Discretionary",
    "Consumer Staples",
    "Energy",
    "Financials",
    "Health Care",
    "Industrials",
    "Information Technology",
    "Materials",
    "Real Estate",
    "Utilities",
)

SECTOR_LABELS_KO: dict[str, str] = {
    "Communication Services": "커뮤니케이션 서비스",
    "Consumer Discretionary": "경기소비재",
    "Consumer Staples": "필수소비재",
    "Energy": "에너지",
    "Financials": "금융",
    "Health Care": "헬스케어",
    "Industrials": "산업재",
    "Information Technology": "정보기술",
    "Materials": "소재",
    "Real Estate": "부동산",
    "Utilities": "유틸리티",
    UNCLASSIFIED_SECTOR: "미분류",
}

_ALIASES: dict[str, str] = {
    "basic materials": "Materials",
    "communication services": "Communication Services",
    "communications": "Communication Services",
    "consumer cyclical": "Consumer Discretionary",
    "consumer discretionary": "Consumer Discretionary",
    "consumer defensive": "Consumer Staples",
    "consumer staples": "Consumer Staples",
    "energy": "Energy",
    "energy and utilities": "Energy",
    "financial services": "Financials",
    "financial technology": "Financials",
    "financials": "Financials",
    "health care": "Health Care",
    "healthcare": "Health Care",
    "industrials": "Industrials",
    "information technology": "Information Technology",
    "technology": "Information Technology",
    "materials": "Materials",
    "real estate": "Real Estate",
    "utilities": "Utilities",
    "unclassified": UNCLASSIFIED_SECTOR,
}


def normalize_sector(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return UNCLASSIFIED_SECTOR
    return _ALIASES.get(_sector_key(text), text if text in CANONICAL_SECTORS else UNCLASSIFIED_SECTOR)


def sector_label_ko(value: Any) -> str:
    sector = normalize_sector(value)
    return SECTOR_LABELS_KO.get(sector, sector)


def normalize_sector_list(values: Any, *, max_items: int | None = None) -> list[str]:
    if not isinstance(values, list):
        return []
    normalized: list[str] = []
    for value in values:
        sector = normalize_sector(value)
        if sector == UNCLASSIFIED_SECTOR or sector in normalized:
            continue
        normalized.append(sector)
        if max_items is not None and len(normalized) >= max_items:
            break
    return normalized


def sector_payload_fields(value: Any) -> dict[str, str]:
    sector = normalize_sector(value)
    return {"sector": sector, "sectorLabelKo": sector_label_ko(sector)}


def _sector_key(value: str) -> str:
    return " ".join(value.replace("&", "and").split()).lower()
