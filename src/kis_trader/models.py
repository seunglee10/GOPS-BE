from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .market import normalize_exchange, normalize_side


@dataclass(frozen=True)
class OverseasOrderRequest:
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    exchange: str = "NASD"
    order_division: str = "00"

    @classmethod
    def from_strings(
        cls,
        *,
        symbol: str,
        side: str,
        qty: str,
        price: str,
        exchange: str,
        order_division: str = "00",
    ) -> "OverseasOrderRequest":
        try:
            quantity = Decimal(qty)
            order_price = Decimal(price)
        except InvalidOperation as exc:
            raise ValueError("qty and price must be numeric strings.") from exc

        if quantity <= 0:
            raise ValueError("qty must be greater than zero.")
        if order_price < 0:
            raise ValueError("price must be zero or greater.")

        normalized_symbol = symbol.strip().upper()
        if not normalized_symbol:
            raise ValueError("symbol is required.")

        return cls(
            symbol=normalized_symbol,
            side=normalize_side(side),
            quantity=quantity,
            price=order_price,
            exchange=normalize_exchange(exchange),
            order_division=order_division.strip() or "00",
        )

    def to_api_body(
        self,
        *,
        account_no: str,
        product_code: str,
        contact_phone: str,
        mgco_aptm_odno: str,
        order_server_code: str,
    ) -> dict[str, str]:
        return {
            "CANO": account_no,
            "ACNT_PRDT_CD": product_code,
            "OVRS_EXCG_CD": self.exchange,
            "PDNO": self.symbol,
            "ORD_QTY": str(self.quantity),
            "OVRS_ORD_UNPR": str(self.price),
            "CTAC_TLNO": contact_phone,
            "MGCO_APTM_ODNO": mgco_aptm_odno,
            "SLL_TYPE": "00" if self.side == "sell" else "",
            "ORD_SVR_DVSN_CD": order_server_code,
            "ORD_DVSN": self.order_division,
        }


@dataclass(frozen=True)
class DomesticOrderRequest:
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    exchange: str = "KRX"
    order_division: str = "00"
    sell_type: str = ""
    condition_price: str = ""

    @classmethod
    def from_strings(
        cls,
        *,
        symbol: str,
        side: str,
        qty: str,
        price: str,
        exchange: str = "KRX",
        order_division: str = "00",
        sell_type: str = "",
        condition_price: str = "",
    ) -> "DomesticOrderRequest":
        try:
            quantity = Decimal(qty)
            order_price = Decimal(price)
        except InvalidOperation as exc:
            raise ValueError("qty and price must be numeric strings.") from exc

        if quantity <= 0:
            raise ValueError("qty must be greater than zero.")
        if order_price < 0:
            raise ValueError("price must be zero or greater.")

        normalized_symbol = symbol.strip().upper()
        if not normalized_symbol:
            raise ValueError("symbol is required.")
        if not normalized_symbol.isdigit() or len(normalized_symbol) not in {6, 7}:
            raise ValueError("domestic symbol must be a 6-digit stock code or 7-digit ETN code.")

        normalized_exchange = exchange.strip().upper()
        if not normalized_exchange:
            raise ValueError("exchange is required.")

        return cls(
            symbol=normalized_symbol,
            side=normalize_side(side),
            quantity=quantity,
            price=order_price,
            exchange=normalized_exchange,
            order_division=order_division.strip() or "00",
            sell_type=sell_type.strip(),
            condition_price=condition_price.strip(),
        )

    def to_api_body(self, *, account_no: str, product_code: str) -> dict[str, str]:
        return {
            "CANO": account_no,
            "ACNT_PRDT_CD": product_code,
            "PDNO": self.symbol,
            "ORD_DVSN": self.order_division,
            "ORD_QTY": str(self.quantity),
            "ORD_UNPR": str(self.price),
            "EXCG_ID_DVSN_CD": self.exchange,
            "SLL_TYPE": self.sell_type,
            "CNDT_PRIC": self.condition_price,
        }
