from __future__ import annotations

import requests

from .client import KisApiError
from .config import KisConfig
from .models import DomesticOrderRequest, OverseasOrderRequest


class _FakeAuth:
    def invalidate_access_token(self) -> None:
        pass


class FakeKisClient:
    """KIS-compatible client for Kafka/DB smoke tests without external order side effects."""

    def __init__(self, config: KisConfig, *, mode: str = "success") -> None:
        if mode not in {"success", "reject", "timeout"}:
            raise ValueError("fake KIS mode must be success, reject, or timeout.")
        self.config = config
        self.mode = mode
        self.auth = _FakeAuth()

    def order(self, order_request: OverseasOrderRequest) -> dict[str, object]:
        self._maybe_raise(order_request.symbol)
        return {
            "rt_cd": "0",
            "msg_cd": "FAKE0000",
            "msg1": "fake overseas order accepted",
            "output": {
                "ODNO": self._order_id(order_request.symbol),
                "PDNO": order_request.symbol,
            },
        }

    def domestic_order(self, order_request: DomesticOrderRequest) -> dict[str, object]:
        self._maybe_raise(order_request.symbol)
        return {
            "rt_cd": "0",
            "msg_cd": "FAKE0000",
            "msg1": "fake domestic order accepted",
            "output": {
                "ODNO": self._order_id(order_request.symbol),
                "PDNO": order_request.symbol,
            },
        }

    def preview_order(self, order_request: OverseasOrderRequest) -> dict[str, object]:
        return {
            "env": self.config.env,
            "tr_id": "FAKE_OVERSEAS_ORDER",
            "path": "/fake/overseas-order",
            "body": {
                "CANO": self.config.account_no or "fake-account",
                "ACNT_PRDT_CD": self.config.product_code,
                "OVRS_EXCG_CD": order_request.exchange,
                "PDNO": order_request.symbol,
                "ORD_QTY": str(order_request.quantity),
                "OVRS_ORD_UNPR": str(order_request.price),
                "ORD_DVSN": order_request.order_division,
            },
        }

    def preview_domestic_order(self, order_request: DomesticOrderRequest) -> dict[str, object]:
        return {
            "env": self.config.env,
            "tr_id": "FAKE_DOMESTIC_ORDER",
            "path": "/fake/domestic-order",
            "body": {
                "CANO": self.config.account_no or "fake-account",
                "ACNT_PRDT_CD": self.config.product_code,
                "PDNO": order_request.symbol,
                "ORD_QTY": str(order_request.quantity),
                "ORD_UNPR": str(order_request.price),
                "ORD_DVSN": order_request.order_division,
            },
        }

    def order_history(self, **kwargs: object) -> dict[str, object]:
        return {
            "rt_cd": "0",
            "msg_cd": "FAKE0000",
            "output1": [
                {
                    "ODNO": "FAKE-AAPL",
                    "PDNO": "AAPL",
                    "TOT_CCLD_QTY": "1",
                    "CNCL_YN": "N",
                }
            ],
        }

    def domestic_order_history(self, **kwargs: object) -> dict[str, object]:
        return {
            "rt_cd": "0",
            "msg_cd": "FAKE0000",
            "output1": [
                {
                    "ODNO": "FAKE-005930",
                    "PDNO": "005930",
                    "TOT_CCLD_QTY": "1",
                    "CNCL_YN": "N",
                }
            ],
        }

    def _maybe_raise(self, symbol: str) -> None:
        if self.mode == "reject":
            raise KisApiError(
                "fake KIS API rejected request",
                response={"rt_cd": "1", "msg_cd": "FAKE_REJECT", "msg1": "fake rejection"},
                status_code=200,
            )
        if self.mode == "timeout":
            try:
                raise requests.Timeout("fake timeout")
            except requests.Timeout as exc:
                raise KisApiError("fake KIS POST timeout") from exc

    @staticmethod
    def _order_id(symbol: str) -> str:
        return f"FAKE-{symbol}"
