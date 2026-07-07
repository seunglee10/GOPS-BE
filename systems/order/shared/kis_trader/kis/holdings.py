"""Normalize KIS account balance responses into a stable GOPS contract."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


def normalize_account_holdings(
    payload: dict[str, Any],
    *,
    market: str,
    account_alias: str = "모의투자",
    currency: str = "USD",
) -> dict[str, Any]:
    normalized_market = market.strip().lower()
    rows = _extract_rows(payload)
    summary = _extract_summary(payload)
    positions = [_normalize_position(row, normalized_market, currency) for row in rows if isinstance(row, dict)]
    positions = [position for position in positions if position["symbol"] and (position["quantity"] or 0) > 0]
    positions.sort(key=lambda position: position.get("marketValueKrw") or position.get("marketValueForeign") or 0, reverse=True)

    account = _normalize_account(summary, market=normalized_market, account_alias=account_alias, currency=currency)
    if account["totalValueKrw"] is None:
        account["totalValueKrw"] = _sum_present(position.get("marketValueKrw") for position in positions)
    if account["totalValueForeign"] is None:
        account["totalValueForeign"] = _sum_present(position.get("marketValueForeign") for position in positions)
    if account["unrealizedPnlKrw"] is None:
        account["unrealizedPnlKrw"] = _sum_present(position.get("unrealizedPnlKrw") for position in positions)
    if account["unrealizedPnlForeign"] is None:
        account["unrealizedPnlForeign"] = _sum_present(position.get("unrealizedPnlForeign") for position in positions)

    return {
        "status": "ok" if positions else "empty",
        "source": "kis-demo",
        "asOf": datetime.now(timezone.utc).isoformat(),
        "account": account,
        "positions": positions,
        "limitations": _limitations(account, positions, normalized_market),
    }


def _extract_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("output1", "output", "positions"):
        rows = payload.get(key)
        if isinstance(rows, list):
            return rows
    return []


def _extract_summary(payload: dict[str, Any]) -> dict[str, Any]:
    for key in ("output2", "summary", "account"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value[0]
    return {}


def _normalize_account(summary: dict[str, Any], *, market: str, account_alias: str, currency: str) -> dict[str, Any]:
    return {
        "alias": account_alias,
        "market": market,
        "currency": "KRW" if market == "domestic" else currency.upper(),
        "cashKrw": _number(_first(summary, ("dnca_tot_amt", "cash_krw", "tot_ccld_amt"))),
        "cashForeign": _number(_first(summary, ("frcr_use_psbl_amt", "ord_psbl_cash", "cash_foreign", "frcr_buy_amt_smtl1"))),
        "stockValueKrw": _number(_first(summary, ("scts_evlu_amt", "ovrs_stck_evlu_amt", "stock_value_krw"))),
        "stockValueForeign": _number(_first(summary, ("frcr_evlu_tota", "ovrs_stck_evlu_amt", "stock_value_foreign"))),
        "totalValueKrw": _number(_first(summary, ("tot_evlu_amt", "tot_asst_amt", "total_value_krw"))),
        "totalValueForeign": _number(_first(summary, ("frcr_evlu_tota", "tot_evlu_amt", "total_value_foreign"))),
        "unrealizedPnlKrw": _number(_first(summary, ("evlu_pfls_smtl_amt", "tot_evlu_pfls_amt", "unrealized_pnl_krw"))),
        "unrealizedPnlForeign": _number(_first(summary, ("ovrs_tot_pfls", "tot_evlu_pfls_amt", "unrealized_pnl_foreign"))),
        "unrealizedPnlRate": _number(_first(summary, ("asst_icdc_erng_rt", "tot_pftrt", "rlzt_erng_rt", "unrealized_pnl_rate"))),
    }


def _normalize_position(row: dict[str, Any], market: str, default_currency: str) -> dict[str, Any]:
    symbol = _text(_first(row, ("ovrs_pdno", "pdno", "PDNO", "symbol")))
    quantity = _number(_first(row, ("ovrs_cblc_qty", "hldg_qty", "ord_psbl_qty", "quantity")))
    return {
        "symbol": symbol,
        "name": _text(_first(row, ("ovrs_item_name", "prdt_name", "prdt_name120", "name"))) or symbol,
        "market": market,
        "exchange": _text(_first(row, ("ovrs_excg_cd", "OVRS_EXCG_CD", "excg_cd", "exchange"))),
        "currency": _text(_first(row, ("tr_crcy_cd", "TR_CRCY_CD", "currency"))) or ("KRW" if market == "domestic" else default_currency.upper()),
        "sector": _text(_first(row, ("sector", "sector_name", "gics_sector"))) or None,
        "industry": _text(_first(row, ("industry", "industry_name", "gics_industry"))) or None,
        "quantity": quantity,
        "availableQuantity": _number(_first(row, ("ord_psbl_qty", "sellable_qty", "available_quantity"))),
        "averagePrice": _number(_first(row, ("pchs_avg_pric", "pchs_avg_pric2", "average_price"))),
        "currentPrice": _number(_first(row, ("now_pric2", "prpr", "current_price"))),
        "purchaseAmountKrw": _number(_first(row, ("pchs_amt", "purchase_amount_krw"))),
        "purchaseAmountForeign": _number(_first(row, ("frcr_pchs_amt1", "purchase_amount_foreign"))),
        "marketValueKrw": _number(_first(row, ("evlu_amt", "scts_evlu_amt", "market_value_krw"))),
        "marketValueForeign": _number(_first(row, ("frcr_evlu_amt2", "ovrs_stck_evlu_amt", "market_value_foreign"))),
        "unrealizedPnlKrw": _number(_first(row, ("evlu_pfls_amt", "unrealized_pnl_krw"))),
        "unrealizedPnlForeign": _number(_first(row, ("frcr_evlu_pfls_amt", "unrealized_pnl_foreign"))),
        "unrealizedPnlRate": _number(_first(row, ("evlu_pfls_rt", "evlu_erng_rt", "unrealized_pnl_rate"))),
        "dayPnlForeign": _number(_first(row, ("day_pnl_foreign", "dayPnlForeign"))),
        "dayPnlRate": _number(_first(row, ("day_pnl_rate", "dayPnlRate"))),
        "dividendYield": _number(_first(row, ("dividend_yield", "dividendYield", "dvdn_yld"))),
        "dividendPerShare": _number(_first(row, ("dividend_per_share", "dividendPerShare", "dps"))),
        "annualDividend": _number(_first(row, ("annual_dividend", "annualDividend"))),
    }


def _first(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return None


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        normalized = str(value).replace(",", "").strip()
        if not normalized:
            return None
        return float(Decimal(normalized))
    except (InvalidOperation, ValueError):
        return None


def _sum_present(values: Any) -> float | None:
    present = [value for value in values if isinstance(value, (int, float))]
    if not present:
        return None
    return float(sum(present))


def _limitations(account: dict[str, Any], positions: list[dict[str, Any]], market: str) -> list[str]:
    limitations = []
    if market == "overseas" and account["cashKrw"] is None:
        limitations.append("KIS overseas balance response may not include KRW cash fields.")
    if positions and all(position.get("marketValueKrw") is None for position in positions):
        limitations.append("KIS response did not include KRW valuation for positions.")
    return limitations
