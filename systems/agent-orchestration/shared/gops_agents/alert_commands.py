from __future__ import annotations

import json
import os
from typing import Any

from gops_agents.chart_command import ChartCommandError
from gops_agents.chart_command.agent import request_openai_response


ALERT_RESOLUTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "status": {"type": "string", "enum": ["ready", "clarify", "rejected"]},
        "symbol": {"type": ["string", "null"]},
        "kind": {
            "type": ["string", "null"],
            "enum": ["price_cross", "price_change", "volume_absolute", "volume_relative", "rsi_threshold", None],
        },
        "operator": {"type": ["string", "null"], "enum": ["above", "below", "either", None]},
        "threshold": {"type": ["number", "null"]},
        "interval": {"type": ["string", "null"], "enum": ["1m", "5m", "10m", "1h", "4h", "1D", None]},
        "windowMin": {"type": ["integer", "null"]},
        "lookback": {"type": ["integer", "null"]},
        "period": {"type": ["integer", "null"]},
        "repeatLimit": {"type": ["integer", "null"]},
        "clarification": {"type": ["string", "null"]},
    },
    "required": [
        "status", "symbol", "kind", "operator", "threshold", "interval",
        "windowMin", "lookback", "period", "repeatLimit", "clarification",
    ],
}


def resolve_alert_expression(request: dict[str, Any]) -> dict[str, Any]:
    text = str(request.get("text") or "").strip()
    context_symbol = _symbol_or_none(request.get("contextSymbol"))
    context_interval = _interval_or_none(request.get("contextInterval"))
    if not text:
        return _clarify("알림 조건을 알려주세요.")

    prompt_context = {
        "text": text,
        "contextSymbol": context_symbol,
        "contextInterval": context_interval,
    }
    payload = {
        "model": os.getenv("OPENAI_ALERT_RESOLVER_MODEL") or os.getenv("OPENAI_MODEL", "gpt-5.2"),
        "instructions": (
            "Extract exactly one stock alert condition from the Korean or English request. "
            "Supported kinds are price_cross, price_change, volume_absolute, volume_relative, and rsi_threshold. "
            "Never invent a symbol, threshold, timeframe, or duration. If a required value is absent, return clarify "
            "with one concise Korean question. Compound AND/OR conditions must be rejected. Default RSI interval is 1D "
            "and period is 14. Default volume-relative lookback is 20, but volume conditions require an explicit interval. "
            "Default repeatLimit is 1; null means unlimited repeated notifications. Return only the schema JSON."
        ),
        "input": [{"role": "user", "content": json.dumps(prompt_context, ensure_ascii=False)}],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "alert_resolution",
                "schema": ALERT_RESOLUTION_SCHEMA,
                "strict": True,
            }
        },
    }
    try:
        raw = request_openai_response(payload, read_config=lambda key: os.getenv(key))
        parsed = json.loads(raw)
    except (ChartCommandError, json.JSONDecodeError, TypeError, ValueError):
        return _clarify("알림 조건을 해석하지 못했습니다. 기업명, 조건값, 기준 시간을 한 문장으로 알려주세요.")
    return _validated_resolution(parsed, context_symbol=context_symbol)


def _validated_resolution(value: Any, *, context_symbol: str | None) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    status = str(source.get("status") or "").strip()
    if status == "rejected":
        return {"status": "rejected", "clarification": str(source.get("clarification") or "한 알림에는 한 조건만 설정할 수 있습니다.")}
    if status != "ready":
        return _clarify(str(source.get("clarification") or "알림 조건을 조금 더 구체적으로 알려주세요."))

    symbol = _symbol_or_none(source.get("symbol")) or context_symbol
    kind = str(source.get("kind") or "")
    operator = str(source.get("operator") or "")
    threshold = source.get("threshold")
    if symbol is None:
        return _clarify("어느 기업의 알림인지 기업명이나 티커를 알려주세요.")
    if kind not in {"price_cross", "price_change", "volume_absolute", "volume_relative", "rsi_threshold"}:
        return _clarify("목표가, 변동률, 거래량 또는 RSI 중 어떤 조건인지 알려주세요.")
    allowed_operators = {"above", "below", "either"} if kind == "price_change" else {"above", "below"}
    if operator not in allowed_operators or not isinstance(threshold, (int, float)) or isinstance(threshold, bool) or threshold <= 0:
        return _clarify("알림 기준값과 위·아래 방향을 알려주세요.")

    condition: dict[str, Any] = {"kind": kind, "operator": operator, "threshold": threshold}
    if kind in {"volume_absolute", "volume_relative", "rsi_threshold"}:
        interval = _interval_or_none(source.get("interval"))
        if interval is None and kind != "rsi_threshold":
            return _clarify("거래량 조건은 몇 분봉 기준인지 알려주세요. 예: 5분봉")
        condition["interval"] = interval or "1D"
    if kind == "price_change":
        window_min = source.get("windowMin")
        if not isinstance(window_min, int) or isinstance(window_min, bool) or window_min <= 0:
            return _clarify("가격 변동률을 몇 분 기준으로 볼지 알려주세요. 예: 10분")
        condition["windowMin"] = window_min
    if kind == "volume_relative":
        condition["lookback"] = int(source.get("lookback") or 20)
    if kind == "rsi_threshold":
        condition["period"] = int(source.get("period") or 14)

    repeat_limit = source.get("repeatLimit")
    if repeat_limit not in {None, 1, 3, 5, 10}:
        repeat_limit = 1
    return {"status": "ready", "symbol": symbol, "condition": condition, "repeatLimit": repeat_limit}


def _clarify(message: str) -> dict[str, Any]:
    return {"status": "clarify", "clarification": message}


def _symbol_or_none(value: Any) -> str | None:
    symbol = str(value or "").strip().upper()
    return symbol if symbol and symbol.replace(".", "").isalnum() and symbol[0].isalpha() else None


def _interval_or_none(value: Any) -> str | None:
    interval = str(value or "").strip()
    normalized = "1D" if interval.lower() == "1d" else interval.lower()
    return normalized if normalized in {"1m", "5m", "10m", "1h", "4h", "1D"} else None
