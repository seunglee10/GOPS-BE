"""KIS market-specific constants."""

from __future__ import annotations

OVERSEAS_ORDER_TR_IDS: dict[str, dict[str, str]] = {
    "buy": {
        "NASD": "VTTT1002U",
        "NYSE": "VTTT1002U",
        "AMEX": "VTTT1002U",
        "SEHK": "VTTS1002U",
        "SHAA": "VTTS0202U",
        "SZAA": "VTTS0305U",
        "TKSE": "VTTS0308U",
        "HASE": "VTTS0311U",
        "VNSE": "VTTS0311U",
    },
    "sell": {
        "NASD": "VTTT1006U",
        "NYSE": "VTTT1006U",
        "AMEX": "VTTT1006U",
        "SEHK": "VTTS1001U",
        "SHAA": "VTTS1005U",
        "SZAA": "VTTS0304U",
        "TKSE": "VTTS0307U",
        "HASE": "VTTS0310U",
        "VNSE": "VTTS0310U",
    },
}

SUPPORTED_OVERSEAS_EXCHANGES: frozenset[str] = frozenset(OVERSEAS_ORDER_TR_IDS["buy"])
OVERSEAS_CURRENCY_BY_EXCHANGE: dict[str, str] = {
    "NASD": "USD",
    "NYSE": "USD",
    "AMEX": "USD",
    "SEHK": "HKD",
    "SHAA": "CNY",
    "SZAA": "CNY",
    "TKSE": "JPY",
    "HASE": "VND",
    "VNSE": "VND",
}


def resolve_overseas_order_tr_id(side: str, exchange: str, env: str = "demo") -> str:
    if env != "demo":
        raise ValueError("real overseas order TR IDs are not implemented")
    normalized_side = side.lower()
    normalized_exchange = exchange.upper()
    try:
        return OVERSEAS_ORDER_TR_IDS[normalized_side][normalized_exchange]
    except KeyError as exc:
        raise ValueError(f"unsupported KIS overseas order side/exchange: {side}/{exchange}") from exc


def resolve_overseas_balance_tr_id(env: str = "demo") -> str:
    if env != "demo":
        raise ValueError("real overseas balance TR IDs are not implemented")
    return "VTTS3012R"


def resolve_domestic_balance_tr_id(env: str = "demo") -> str:
    if env != "demo":
        raise ValueError("real domestic balance TR IDs are not implemented")
    return "VTTC8434R"


def ccnl_side_code(side: str | None) -> str:
    if side is None:
        return "00"
    return {"buy": "02", "sell": "01"}.get(side.lower(), "00")


def default_currency(exchange: str) -> str:
    return OVERSEAS_CURRENCY_BY_EXCHANGE.get(exchange.upper(), "USD")
