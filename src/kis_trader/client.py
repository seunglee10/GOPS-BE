from __future__ import annotations

from datetime import date
from typing import Any

import requests

from .auth import KisAuthClient
from .config import KisConfig
from .market import ccnl_side_code, default_currency, normalize_exchange, resolve_order_tr_id
from .models import DomesticOrderRequest, OverseasOrderRequest


class KisApiError(RuntimeError):
    def __init__(self, message: str, *, response: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.response = response or {}


class KisOverseasClient:
    def __init__(self, config: KisConfig) -> None:
        self.config = config
        self.auth = KisAuthClient(config)

    def order(self, order_request: OverseasOrderRequest) -> dict[str, Any]:
        tr_id = resolve_order_tr_id(order_request.side, order_request.exchange, self.config.env)
        body = order_request.to_api_body(
            account_no=self.config.account_no,
            product_code=self.config.product_code,
            contact_phone=self.config.contact_phone,
            mgco_aptm_odno=self.config.mgco_aptm_odno,
            order_server_code=self.config.order_server_code,
        )
        return self._post("/uapi/overseas-stock/v1/trading/order", tr_id=tr_id, body=body)

    def domestic_order(self, order_request: DomesticOrderRequest) -> dict[str, Any]:
        tr_id = self._resolve_domestic_order_tr_id(order_request.side)
        body = order_request.to_api_body(
            account_no=self.config.account_no,
            product_code=self.config.product_code,
        )
        return self._post("/uapi/domestic-stock/v1/trading/order-cash", tr_id=tr_id, body=body)

    def balance(self, *, exchange: str | None = None, currency: str | None = None) -> dict[str, Any]:
        selected_exchange = normalize_exchange(exchange or self.config.default_exchange)
        selected_currency = (currency or default_currency(selected_exchange)).strip().upper()
        params = {
            "CANO": self.config.account_no,
            "ACNT_PRDT_CD": self.config.product_code,
            "OVRS_EXCG_CD": selected_exchange,
            "TR_CRCY_CD": selected_currency,
            "CTX_AREA_FK200": "",
            "CTX_AREA_NK200": "",
        }
        tr_id = "VTTS3012R" if self.config.env == "demo" else "TTTS3012R"
        return self._get("/uapi/overseas-stock/v1/trading/inquire-balance", tr_id=tr_id, params=params)

    def domestic_balance(
        self,
        *,
        afhr_flpr_yn: str = "N",
        inqr_dvsn: str = "02",
        unpr_dvsn: str = "01",
        fund_sttl_icld_yn: str = "N",
        fncg_amt_auto_rdpt_yn: str = "N",
        prcs_dvsn: str = "00",
        ctx_area_fk100: str = "",
        ctx_area_nk100: str = "",
        tr_cont: str = "",
    ) -> dict[str, Any]:
        params = {
            "CANO": self.config.account_no,
            "ACNT_PRDT_CD": self.config.product_code,
            "AFHR_FLPR_YN": afhr_flpr_yn,
            "OFL_YN": "",
            "INQR_DVSN": inqr_dvsn,
            "UNPR_DVSN": unpr_dvsn,
            "FUND_STTL_ICLD_YN": fund_sttl_icld_yn,
            "FNCG_AMT_AUTO_RDPT_YN": fncg_amt_auto_rdpt_yn,
            "PRCS_DVSN": prcs_dvsn,
            "CTX_AREA_FK100": ctx_area_fk100,
            "CTX_AREA_NK100": ctx_area_nk100,
        }
        tr_id = "VTTC8434R" if self.config.env == "demo" else "TTTC8434R"
        return self._get(
            "/uapi/domestic-stock/v1/trading/inquire-balance",
            tr_id=tr_id,
            params=params,
            tr_cont=tr_cont,
        )

    def order_history(
        self,
        *,
        start_date: date,
        end_date: date,
        symbol: str | None = None,
        side: str | None = None,
        exchange: str | None = None,
        fill_status: str = "00",
        sort: str = "DS",
    ) -> dict[str, Any]:
        if self.config.env == "demo":
            pdno = ""
            selected_exchange = ""
            selected_side = "00"
            fill_status = "00"
            sort = "DS"
        else:
            pdno = (symbol or "%").strip().upper()
            selected_exchange = normalize_exchange(exchange or self.config.default_exchange)
            selected_side = ccnl_side_code(side)

        params = {
            "CANO": self.config.account_no,
            "ACNT_PRDT_CD": self.config.product_code,
            "PDNO": pdno,
            "ORD_STRT_DT": start_date.strftime("%Y%m%d"),
            "ORD_END_DT": end_date.strftime("%Y%m%d"),
            "SLL_BUY_DVSN": selected_side,
            "CCLD_NCCS_DVSN": fill_status,
            "OVRS_EXCG_CD": selected_exchange,
            "SORT_SQN": sort,
            "ORD_DT": "",
            "ORD_GNO_BRNO": "",
            "ODNO": "",
            "CTX_AREA_NK200": "",
            "CTX_AREA_FK200": "",
        }
        tr_id = "VTTS3035R" if self.config.env == "demo" else "TTTS3035R"
        return self._get("/uapi/overseas-stock/v1/trading/inquire-ccnl", tr_id=tr_id, params=params)

    def preview_order(self, order_request: OverseasOrderRequest) -> dict[str, Any]:
        return {
            "env": self.config.env,
            "tr_id": resolve_order_tr_id(order_request.side, order_request.exchange, self.config.env),
            "path": "/uapi/overseas-stock/v1/trading/order",
            "body": order_request.to_api_body(
                account_no=self.config.account_no,
                product_code=self.config.product_code,
                contact_phone=self.config.contact_phone,
                mgco_aptm_odno=self.config.mgco_aptm_odno,
                order_server_code=self.config.order_server_code,
            ),
        }

    def preview_domestic_order(self, order_request: DomesticOrderRequest) -> dict[str, Any]:
        return {
            "env": self.config.env,
            "tr_id": self._resolve_domestic_order_tr_id(order_request.side),
            "path": "/uapi/domestic-stock/v1/trading/order-cash",
            "body": order_request.to_api_body(
                account_no=self.config.account_no,
                product_code=self.config.product_code,
            ),
        }

    def _resolve_domestic_order_tr_id(self, side: str) -> str:
        if self.config.env == "demo":
            return "VTTC0012U" if side == "buy" else "VTTC0011U"
        if self.config.env == "real":
            return "TTTC0012U" if side == "buy" else "TTTC0011U"
        raise ValueError("env must be either 'demo' or 'real'.")

    def _get(self, path: str, *, tr_id: str, params: dict[str, str], tr_cont: str = "") -> dict[str, Any]:
        try:
            response = requests.get(
                f"{self.config.base_url}{path}",
                headers=self.auth.trading_headers(tr_id=tr_id, tr_cont=tr_cont),
                params=params,
                timeout=self.config.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise KisApiError(f"KIS GET request failed: {exc}") from exc
        return self._parse_response(response)

    def _post(self, path: str, *, tr_id: str, body: dict[str, str], tr_cont: str = "") -> dict[str, Any]:
        try:
            response = requests.post(
                f"{self.config.base_url}{path}",
                headers=self.auth.trading_headers(tr_id=tr_id, tr_cont=tr_cont),
                json=body,
                timeout=self.config.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise KisApiError(f"KIS POST request failed: {exc}") from exc
        return self._parse_response(response)

    @staticmethod
    def _parse_response(response: requests.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise KisApiError(f"Response is not JSON: HTTP {response.status_code} {response.text}") from exc

        if response.status_code != 200:
            raise KisApiError(f"KIS request failed: HTTP {response.status_code} {payload}", response=payload)

        if isinstance(payload, dict) and payload.get("rt_cd") not in (None, "0"):
            msg_cd = payload.get("msg_cd", "")
            msg = payload.get("msg1", payload)
            raise KisApiError(f"KIS API rejected request: {msg_cd} {msg}", response=payload)

        if not isinstance(payload, dict):
            raise KisApiError(f"Unexpected response JSON type: {payload!r}")
        return payload
