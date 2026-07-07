from __future__ import annotations

import os
from datetime import date, timedelta
from typing import Any, Iterable

import httpx


ALPACA_CORPORATE_ACTIONS_ENABLED_ENV = "ALPACA_CORPORATE_ACTIONS_ENABLED"
ALPACA_CORPORATE_ACTIONS_LOOKAHEAD_DAYS_ENV = "ALPACA_CORPORATE_ACTIONS_LOOKAHEAD_DAYS"
ALPACA_CORPORATE_ACTIONS_TIMEOUT_SECONDS_ENV = "ALPACA_CORPORATE_ACTIONS_TIMEOUT_SECONDS"
ALPACA_DATA_BASE_URL_ENV = "ALPACA_DATA_BASE_URL"


def enrich_holdings_with_alpaca_dividends(payload: dict[str, Any]) -> dict[str, Any]:
    if not _env_bool(ALPACA_CORPORATE_ACTIONS_ENABLED_ENV, default=True):
        return payload
    positions = payload.get("positions")
    if not isinstance(positions, list) or not positions:
        return payload

    symbols = sorted(
        {
            str(position.get("symbol", "")).strip().upper()
            for position in positions
            if isinstance(position, dict) and str(position.get("symbol", "")).strip()
        }
    )
    if not symbols:
        return payload

    try:
        key_id, secret_key = _load_alpaca_credentials()
    except Exception:
        return payload
    if not key_id or not secret_key:
        return payload

    today = date.today()
    actions = _fetch_corporate_actions(symbols, key_id, secret_key, today)
    if not actions:
        return payload
    dividend_by_symbol = _cash_dividend_by_symbol(actions, today)
    if not dividend_by_symbol:
        return payload

    enriched_positions: list[Any] = []
    changed = False
    for raw_position in positions:
        if not isinstance(raw_position, dict):
            enriched_positions.append(raw_position)
            continue
        symbol = str(raw_position.get("symbol", "")).strip().upper()
        dividend = dividend_by_symbol.get(symbol)
        if not dividend:
            enriched_positions.append(raw_position)
            continue

        per_share = dividend.get("amount")
        quantity = _number(raw_position.get("quantity"))
        if per_share is None or quantity is None:
            enriched_positions.append(raw_position)
            continue

        expected_dividend = per_share * quantity
        position = dict(raw_position)
        position["dividendPerShare"] = per_share
        position["annualDividend"] = expected_dividend
        position["dividendSource"] = "alpaca_corporate_actions"
        if dividend.get("date"):
            position["nextDividendDate"] = dividend["date"]
        market_value = _first_number(position.get("marketValueForeign"), position.get("marketValueKrw"))
        if market_value and market_value > 0:
            position["dividendYield"] = (expected_dividend / market_value) * 100
        enriched_positions.append(position)
        changed = True

    if not changed:
        return payload
    enriched = dict(payload)
    enriched["positions"] = enriched_positions
    limitations = list(enriched.get("limitations") or [])
    if "alpaca corporate actions dividends enriched" not in limitations:
        limitations.append("alpaca corporate actions dividends enriched")
    enriched["limitations"] = limitations
    return enriched


def _load_alpaca_credentials() -> tuple[str | None, str | None]:
    from alfaka.common.secrets import load_alpaca_credentials

    return load_alpaca_credentials()


def _fetch_corporate_actions(symbols: list[str], key_id: str, secret_key: str, today: date) -> list[dict[str, Any]]:
    base_url = os.getenv(ALPACA_DATA_BASE_URL_ENV, "https://data.alpaca.markets").rstrip("/")
    lookahead_days = _env_int(ALPACA_CORPORATE_ACTIONS_LOOKAHEAD_DAYS_ENV, default=370)
    timeout_seconds = _env_float(ALPACA_CORPORATE_ACTIONS_TIMEOUT_SECONDS_ENV, default=4.0)
    params = {
        "symbols": ",".join(symbols),
        "types": "cash_dividend,stock_dividend",
        "start": today.isoformat(),
        "end": (today + timedelta(days=max(1, lookahead_days))).isoformat(),
    }
    headers = {
        "APCA-API-KEY-ID": key_id,
        "APCA-API-SECRET-KEY": secret_key,
    }
    try:
        with httpx.Client(timeout=timeout_seconds) as client:
            response = client.get(f"{base_url}/v1/corporate-actions", params=params, headers=headers)
            response.raise_for_status()
            payload = response.json()
    except Exception:
        return []
    return list(_iter_action_candidates(payload))


def _cash_dividend_by_symbol(actions: Iterable[dict[str, Any]], today: date) -> dict[str, dict[str, Any]]:
    by_symbol: dict[str, dict[str, Any]] = {}
    for action in actions:
        symbol = _action_symbol(action)
        if not symbol:
            continue
        action_type = str(action.get("type") or action.get("action_type") or action.get("ca_type") or "").lower()
        amount = _first_number(
            action.get("cash"),
            action.get("amount"),
            action.get("cash_amount"),
            action.get("rate"),
            action.get("dividend"),
            action.get("dividend_amount"),
            action.get("per_share_amount"),
        )
        if amount is None or amount < 0:
            continue
        if action_type and "cash" not in action_type and "dividend" not in action_type:
            continue
        action_date = _action_date(action)
        candidate = {"amount": amount, "date": action_date.isoformat() if action_date else None}
        existing = by_symbol.get(symbol)
        if existing is None or _is_better_dividend(candidate, existing, today):
            by_symbol[symbol] = candidate
    return by_symbol


def _iter_action_candidates(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, list):
        for item in value:
            yield from _iter_action_candidates(item)
        return
    if not isinstance(value, dict):
        return
    if _looks_like_action(value):
        yield value
    for child in value.values():
        if isinstance(child, (dict, list)):
            yield from _iter_action_candidates(child)


def _looks_like_action(value: dict[str, Any]) -> bool:
    if _action_symbol(value) is None:
        return False
    return _first_number(
        value.get("cash"),
        value.get("amount"),
        value.get("cash_amount"),
        value.get("rate"),
        value.get("dividend"),
        value.get("dividend_amount"),
        value.get("per_share_amount"),
    ) is not None


def _action_symbol(action: dict[str, Any]) -> str | None:
    for key in ("symbol", "new_symbol", "old_symbol"):
        value = action.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
    return None


def _action_date(action: dict[str, Any]) -> date | None:
    for key in ("payable_date", "pay_date", "ex_date", "ex_dividend_date", "record_date", "date"):
        raw = action.get(key)
        if isinstance(raw, str) and len(raw) >= 10:
            try:
                return date.fromisoformat(raw[:10])
            except ValueError:
                continue
    return None


def _is_better_dividend(candidate: dict[str, Any], existing: dict[str, Any], today: date) -> bool:
    candidate_date = _parse_iso_date(candidate.get("date"))
    existing_date = _parse_iso_date(existing.get("date"))
    if candidate_date is None:
        return existing_date is None
    if existing_date is None:
        return True
    candidate_future = candidate_date >= today
    existing_future = existing_date >= today
    if candidate_future != existing_future:
        return candidate_future
    if candidate_future:
        return candidate_date < existing_date
    return candidate_date > existing_date


def _parse_iso_date(value: Any) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _first_number(*values: Any) -> float | None:
    for value in values:
        number = _number(value)
        if number is not None:
            return number
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value == value:
        return float(value)
    if isinstance(value, str):
        normalized = value.replace(",", "").strip()
        if not normalized:
            return None
        try:
            return float(normalized)
        except ValueError:
            return None
    return None


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, *, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, *, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default
