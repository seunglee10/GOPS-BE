"""KIS demo HTTP client for actual mock-investment order POSTs."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

import requests

from kis_trader.domain.commands import OrderCommand

from .auth import KisAuthClient, KisAuthError
from .config import KisConfig, load_kis_config
from .fake import KisConnectionReset, KisExplicitReject, KisHttpError, KisTimeout, KisTokenExpired
from .holdings import normalize_account_holdings
from .market import (
    ccnl_side_code,
    default_currency,
    resolve_domestic_balance_tr_id,
    resolve_overseas_balance_tr_id,
    resolve_overseas_order_tr_id,
)


class DemoKisHttpClient:
    def __init__(self, config: KisConfig | None = None) -> None:
        self.config = config or load_kis_config()
        self.auth = KisAuthClient(self.config)

    @classmethod
    def from_env(cls) -> "DemoKisHttpClient":
        return cls(load_kis_config())

    def refresh_token(self) -> None:
        self.auth.invalidate_access_token()
        self.auth.get_access_token()

    def submit_order(self, command: OrderCommand) -> dict[str, Any]:
        if command.env != "demo" or self.config.env != "demo":
            raise KisExplicitReject("only KIS demo orders are implemented")
        if command.market != "overseas":
            raise KisExplicitReject("only KIS overseas demo orders are implemented")
        return self._post_overseas_order(command)

    def fetch_order_history(
        self,
        *,
        start_date: date,
        end_date: date,
        symbol: str | None = None,
        side: str | None = None,
        exchange: str | None = None,
    ) -> list[dict[str, Any]]:
        params = {
            "CANO": self.config.account_no,
            "ACNT_PRDT_CD": self.config.product_code,
            "PDNO": "",
            "ORD_STRT_DT": start_date.strftime("%Y%m%d"),
            "ORD_END_DT": end_date.strftime("%Y%m%d"),
            "SLL_BUY_DVSN": ccnl_side_code(side),
            "CCLD_NCCS_DVSN": "00",
            "OVRS_EXCG_CD": "",
            "SORT_SQN": "DS",
            "ORD_DT": "",
            "ORD_GNO_BRNO": "",
            "ODNO": "",
            "CTX_AREA_NK200": "",
            "CTX_AREA_FK200": "",
        }
        payload = self._get("/uapi/overseas-stock/v1/trading/inquire-ccnl", tr_id="VTTS3035R", params=params)
        rows = payload.get("output", [])
        return rows if isinstance(rows, list) else []

    def fetch_orderable_cash(
        self,
        *,
        symbol: str,
        exchange: str = "NASD",
        price: str = "0",
    ) -> dict[str, Any]:
        if self.config.env != "demo":
            raise KisExplicitReject("only KIS demo balance lookups are implemented")
        normalized_exchange = exchange.strip().upper() or "NASD"
        normalized_symbol = symbol.strip().upper()
        if not normalized_symbol:
            raise KisExplicitReject("symbol is required for KIS orderable cash lookup")
        params = {
            "CANO": self.config.account_no,
            "ACNT_PRDT_CD": self.config.product_code,
            "OVRS_EXCG_CD": normalized_exchange,
            "OVRS_ORD_UNPR": str(price or "0"),
            "ITEM_CD": normalized_symbol,
        }
        payload = self._get("/uapi/overseas-stock/v1/trading/inquire-psamount", tr_id="VTTS3007R", params=params)
        output = payload.get("output")
        output_map = output if isinstance(output, dict) else {}
        orderable_cash = _first_decimal_text(
            output_map,
            (
                "ord_psbl_frcr_amt",
                "frcr_ord_psbl_amt1",
                "ovrs_ord_psbl_amt",
                "ord_psbl_amt",
            ),
        )
        orderable_qty = _first_decimal_text(
            output_map,
            (
                "ord_psbl_qty",
                "max_ord_psbl_qty",
                "ovrs_max_ord_psbl_qty",
            ),
        )
        return {
            "env": self.config.env,
            "market": "overseas",
            "exchange": normalized_exchange,
            "currency": default_currency(normalized_exchange),
            "symbol": normalized_symbol,
            "orderable_cash": orderable_cash,
            "orderable_qty": orderable_qty,
            "rt_cd": payload.get("rt_cd"),
            "msg_cd": payload.get("msg_cd"),
            "msg1": payload.get("msg1"),
        }

    def fetch_holdings(self, *, market: str = "overseas", currency: str = "USD", exchange: str = "") -> dict[str, Any]:
        if self.config.env != "demo":
            raise KisExplicitReject("only KIS demo holdings are implemented")
        normalized_market = market.strip().lower()
        if normalized_market == "overseas":
            return self.fetch_overseas_holdings(currency=currency, exchange=exchange)
        if normalized_market == "domestic":
            return self.fetch_domestic_holdings()
        raise KisExplicitReject(f"unsupported holdings market: {market}")

    def fetch_overseas_holdings(self, *, currency: str = "USD", exchange: str = "") -> dict[str, Any]:
        tr_currency = currency.strip().upper() or "USD"
        params = {
            "CANO": self.config.account_no,
            "ACNT_PRDT_CD": self.config.product_code,
            "OVRS_EXCG_CD": exchange.strip().upper(),
            "TR_CRCY_CD": tr_currency,
            "CTX_AREA_FK200": "",
            "CTX_AREA_NK200": "",
        }
        payload = self._get(
            "/uapi/overseas-stock/v1/trading/inquire-balance",
            tr_id=resolve_overseas_balance_tr_id(self.config.env),
            params=params,
        )
        return normalize_account_holdings(payload, market="overseas", currency=tr_currency)

    def fetch_domestic_holdings(self) -> dict[str, Any]:
        params = {
            "CANO": self.config.account_no,
            "ACNT_PRDT_CD": self.config.product_code,
            "AFHR_FLPR_YN": "N",
            "OFL_YN": "",
            "INQR_DVSN": "02",
            "UNPR_DVSN": "01",
            "FUND_STTL_ICLD_YN": "N",
            "FNCG_AMT_AUTO_RDPT_YN": "N",
            "PRCS_DVSN": "00",
            "CTX_AREA_FK100": "",
            "CTX_AREA_NK100": "",
        }
        payload = self._get(
            "/uapi/domestic-stock/v1/trading/inquire-balance",
            tr_id=resolve_domestic_balance_tr_id(self.config.env),
            params=params,
        )
        return normalize_account_holdings(payload, market="domestic", currency="KRW")

    def _post_overseas_order(self, command: OrderCommand) -> dict[str, Any]:
        tr_id = resolve_overseas_order_tr_id(command.side, command.exchange, self.config.env)
        body = {
            "CANO": self.config.account_no,
            "ACNT_PRDT_CD": self.config.product_code,
            "OVRS_EXCG_CD": command.exchange,
            "PDNO": command.symbol,
            "ORD_QTY": str(command.qty),
            "OVRS_ORD_UNPR": str(command.price),
            "CTAC_TLNO": self.config.contact_phone,
            "MGCO_APTM_ODNO": self.config.mgco_aptm_odno,
            "SLL_TYPE": "00" if command.side == "sell" else "",
            "ORD_SVR_DVSN_CD": self.config.order_server_code,
            "ORD_DVSN": command.order_division,
        }
        return self._post("/uapi/overseas-stock/v1/trading/order", tr_id=tr_id, body=body)

    def _post(self, path: str, *, tr_id: str, body: dict[str, str], tr_cont: str = "") -> dict[str, Any]:
        try:
            response = requests.post(
                f"{self.config.base_url}{path}",
                headers=self.auth.trading_headers(tr_id=tr_id, tr_cont=tr_cont),
                json=body,
                timeout=self.config.timeout_seconds,
            )
        except requests.Timeout as exc:
            raise KisTimeout(str(exc)) from exc
        except requests.ConnectionError as exc:
            raise KisConnectionReset(str(exc)) from exc
        except KisAuthError as exc:
            raise KisTokenExpired(str(exc)) from exc
        except requests.RequestException as exc:
            raise KisHttpError(0, safe_to_retry=False, message=str(exc)) from exc
        return self._parse_response(response)

    def _get(self, path: str, *, tr_id: str, params: dict[str, str], tr_cont: str = "") -> dict[str, Any]:
        try:
            response = requests.get(
                f"{self.config.base_url}{path}",
                headers=self.auth.trading_headers(tr_id=tr_id, tr_cont=tr_cont),
                params=params,
                timeout=self.config.timeout_seconds,
            )
        except requests.Timeout as exc:
            raise KisTimeout(str(exc)) from exc
        except requests.ConnectionError as exc:
            raise KisConnectionReset(str(exc)) from exc
        except KisAuthError as exc:
            raise KisTokenExpired(str(exc)) from exc
        except requests.RequestException as exc:
            raise KisHttpError(0, safe_to_retry=False, message=str(exc)) from exc
        return self._parse_response(response)

    def _parse_response(self, response: requests.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise KisHttpError(response.status_code, safe_to_retry=False) from exc

        if response.status_code in {401, 403}:
            raise KisTokenExpired(f"KIS auth failed: HTTP {response.status_code}")
        if response.status_code == 429:
            raise KisHttpError(response.status_code, safe_to_retry=True)
        if response.status_code != 200:
            raise KisHttpError(response.status_code, safe_to_retry=False)
        if not isinstance(payload, dict):
            raise KisHttpError(response.status_code, safe_to_retry=False)
        if payload.get("rt_cd") not in (None, "0"):
            msg_cd = payload.get("msg_cd", "")
            msg = payload.get("msg1", payload)
            raise KisExplicitReject(f"KIS API rejected request: {msg_cd} {msg}")

        broker_order_id = _extract_broker_order_id(payload)
        if broker_order_id:
            payload["broker_order_id"] = broker_order_id
        return payload


def _extract_broker_order_id(payload: dict[str, Any]) -> str | None:
    output = payload.get("output")
    if not isinstance(output, dict):
        return None
    for key in ("ODNO", "odno", "order_no", "ORD_NO"):
        value = output.get(key)
        if value:
            return str(value)
    return None


def _first_decimal_text(source: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = source.get(key)
        if value in (None, ""):
            continue
        try:
            parsed = Decimal(str(value).replace(",", "").strip())
        except (InvalidOperation, ValueError):
            continue
        return format(parsed, "f")
    return None
