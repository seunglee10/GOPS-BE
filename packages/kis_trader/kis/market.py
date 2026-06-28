"""KIS market-specific constants."""

from __future__ import annotations


def resolve_overseas_order_tr_id(side: str, exchange: str, env: str = "demo") -> str:
    if env != "demo":
        raise ValueError("real overseas order TR IDs are not implemented")
    normalized_side = side.lower()
    normalized_exchange = exchange.upper()
    if normalized_exchange in {"NASD", "NYSE", "AMEX"}:
        return "VTTT1002U" if normalized_side == "buy" else "VTTT1001U"
    return "VTTT1002U" if normalized_side == "buy" else "VTTT1001U"


def resolve_domestic_order_tr_id(side: str, env: str = "demo") -> str:
    if env != "demo":
        raise ValueError("real domestic order TR IDs are not implemented")
    return "VTTC0012U" if side.lower() == "buy" else "VTTC0011U"


def ccnl_side_code(side: str | None) -> str:
    if side is None:
        return "00"
    return {"buy": "02", "sell": "01"}.get(side.lower(), "00")
