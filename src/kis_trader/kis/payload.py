"""Domestic/overseas KIS payload conversion kept inside the adapter boundary."""

from __future__ import annotations

from kis_trader.domain.commands import OrderCommand


def build_kis_order_payload(command: OrderCommand) -> dict[str, str]:
    if command.env != "demo":
        raise ValueError("only KIS demo order payloads are implemented")
    if command.market == "overseas":
        return {
            "market": "overseas",
            "OVRS_EXCG_CD": command.exchange,
            "PDNO": command.symbol,
            "ORD_QTY": str(command.qty),
            "OVRS_ORD_UNPR": str(command.price),
            "ORD_DVSN": command.order_division,
            "side": command.side,
        }
    if command.market == "domestic":
        return {
            "market": "domestic",
            "PDNO": command.symbol,
            "ORD_QTY": str(command.qty),
            "ORD_UNPR": str(command.price),
            "ORD_DVSN": command.order_division,
            "EXCG_ID_DVSN_CD": command.exchange,
            "SLL_TYPE": str(command.payload.get("sell_type", "")),
            "CNDT_PRIC": str(command.payload.get("condition_price", "")),
            "side": command.side,
        }
    raise ValueError(f"unsupported market: {command.market}")
