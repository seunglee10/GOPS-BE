from __future__ import annotations

from typing import Literal

TradeEnv = Literal["demo", "real"]
OrderSide = Literal["buy", "sell"]

BUY_TR_IDS = {
    "NASD": "TTTT1002U",
    "NYSE": "TTTT1002U",
    "AMEX": "TTTT1002U",
    "SEHK": "TTTS1002U",
    "SHAA": "TTTS0202U",
    "SZAA": "TTTS0305U",
    "TKSE": "TTTS0308U",
    "HASE": "TTTS0311U",
    "VNSE": "TTTS0311U",
}

SELL_TR_IDS = {
    "NASD": "TTTT1006U",
    "NYSE": "TTTT1006U",
    "AMEX": "TTTT1006U",
    "SEHK": "TTTS1001U",
    "SHAA": "TTTS1005U",
    "SZAA": "TTTS0304U",
    "TKSE": "TTTS0307U",
    "HASE": "TTTS0310U",
    "VNSE": "TTTS0310U",
}

CURRENCY_BY_EXCHANGE = {
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


def normalize_exchange(exchange: str) -> str:
    normalized = exchange.strip().upper()
    if normalized not in BUY_TR_IDS:
        allowed = ", ".join(sorted(BUY_TR_IDS))
        raise ValueError(f"Unsupported overseas exchange code: {exchange!r}. Allowed: {allowed}")
    return normalized


def normalize_side(side: str) -> OrderSide:
    normalized = side.strip().lower()
    if normalized not in {"buy", "sell"}:
        raise ValueError("side must be either 'buy' or 'sell'.")
    return normalized  # type: ignore[return-value]


def resolve_order_tr_id(side: str, exchange: str, env: str) -> str:
    normalized_side = normalize_side(side)
    normalized_exchange = normalize_exchange(exchange)

    tr_id = (
        BUY_TR_IDS[normalized_exchange]
        if normalized_side == "buy"
        else SELL_TR_IDS[normalized_exchange]
    )
    if env == "demo":
        return "V" + tr_id[1:]
    if env == "real":
        return tr_id
    raise ValueError("env must be either 'demo' or 'real'.")


def sell_type(side: str) -> str:
    return "00" if normalize_side(side) == "sell" else ""


def ccnl_side_code(side: str | None) -> str:
    if side is None:
        return "00"
    normalized = normalize_side(side)
    return "02" if normalized == "buy" else "01"


def default_currency(exchange: str) -> str:
    return CURRENCY_BY_EXCHANGE[normalize_exchange(exchange)]
