"""Domestic/overseas KIS payload conversion kept inside the adapter boundary."""

from __future__ import annotations

from kis_trader.domain.commands import OrderCommand


def build_kis_order_payload(command: OrderCommand) -> dict[str, str]:
    if command.env != "demo":
        raise ValueError("only KIS demo order payloads are implemented")
    if command.market != "overseas":
        raise ValueError("only KIS overseas demo order payloads are implemented")
    return {
        "market": "overseas",
        "OVRS_EXCG_CD": command.exchange,
        "PDNO": command.symbol,
        "ORD_QTY": str(command.qty),
        "OVRS_ORD_UNPR": str(command.price),
        "ORD_DVSN": command.order_division,
        "SLL_TYPE": "00" if command.side == "sell" else "",
        "side": command.side,
    }
