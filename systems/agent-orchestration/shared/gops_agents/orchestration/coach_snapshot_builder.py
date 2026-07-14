from __future__ import annotations

import hashlib
import os
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Protocol
from zoneinfo import ZoneInfo


MARKET_TIMEZONE = ZoneInfo("America/New_York")


class CoachSnapshotDataProvider(Protocol):
    def load(self, user_id: str, *, requested_at: datetime, trading_date: date) -> dict[str, Any]: ...


class CoachMarketDataProvider(Protocol):
    def trade_case(self, fill: dict[str, Any], *, requested_at: datetime) -> dict[str, Any]: ...


class CoachInputSnapshotBuilder:
    """Build exactly one point-in-time input from trusted server-side sources."""

    def __init__(
        self,
        *,
        data_provider: CoachSnapshotDataProvider | None = None,
        market_provider: CoachMarketDataProvider | None = None,
        now_provider=None,
    ) -> None:
        self.data_provider = data_provider or PostgresCoachSnapshotProvider()
        self.market_provider = market_provider or ClickHouseCoachMarketProvider()
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))

    def build(
        self,
        *,
        user_id: str,
        analysis_id: str,
        coach_request: dict[str, Any] | None,
        submitted_at: str | None = None,
    ) -> dict[str, Any]:
        server_now = _as_datetime(self.now_provider()) or datetime.now(timezone.utc)
        submitted = _as_datetime(submitted_at)
        now = min(submitted, server_now) if submitted is not None else server_now
        now = now.astimezone(timezone.utc)
        request = dict(coach_request or {})
        current_market_date = now.astimezone(MARKET_TIMEZONE).date()
        trading_date = _as_date(request.get("tradingDate")) or current_market_date
        if trading_date > current_market_date:
            trading_date = current_market_date
        missing: list[dict[str, Any]] = []
        try:
            source = self.data_provider.load(user_id, requested_at=now, trading_date=trading_date)
            if not isinstance(source, dict):
                source = {}
                missing.append(_missing("account", "snapshot_provider_invalid", "계좌 snapshot 응답 형식이 올바르지 않습니다."))
        except Exception as exc:
            source = {}
            missing.append(_missing("account", "snapshot_provider_unavailable", f"계좌 snapshot 조회 실패: {exc.__class__.__name__}"))

        trading_start, trading_day_end = _market_day_bounds(trading_date)
        today_fills = [_normalize_fill(row) for row in _dict_rows(source.get("fills"))]
        today_fills = [
            row
            for row in today_fills
            if row.get("fillId")
            and row.get("symbol")
            and (fill_at := _as_datetime(row.get("filledAt"))) is not None
            and trading_start <= fill_at <= now
            and fill_at < trading_day_end
        ]
        history_fills = [_normalize_fill(row) for row in _dict_rows(source.get("historicalFills"))]
        history_fills = [
            row
            for row in history_fills
            if row.get("fillId")
            and row.get("symbol")
            and (fill_at := _as_datetime(row.get("filledAt"))) is not None
            and fill_at < trading_start
            and fill_at <= now
        ]
        if not today_fills:
            missing.append(_missing("fills", "today_fills_missing", "당일 사용자 소유 체결이 없습니다."))
        elif any(fill.get("averageFillPrice") is None or fill.get("quantity") is None for fill in today_fills):
            missing.append(_missing("fills", "execution_details_missing", "일부 체결의 실제 체결가 또는 체결 수량이 없습니다."))

        case_by_fill: dict[str, dict[str, Any]] = {}
        market_as_of: str | None = None
        for fill in [*today_fills, *history_fills[:24]]:
            try:
                raw_case = self.market_provider.trade_case(fill, requested_at=now)
                case = _sanitize_trade_case(raw_case, fill, requested_at=now)
            except Exception as exc:
                missing.append(_missing("market", "trade_case_unavailable", f"{fill.get('symbol')} 차트 조회 실패: {exc.__class__.__name__}"))
                continue
            if case.get("series"):
                case_by_fill[str(fill["fillId"])] = case
                last_time = str(case["series"][-1].get("time") or "")
                market_as_of = max(filter(None, [market_as_of, last_time]), default=None)

        history_cases = []
        for fill in history_fills:
            case = case_by_fill.get(str(fill["fillId"]))
            if case:
                history_cases.append({**fill, **case})

        portfolio_history = [
            dict(row)
            for row in _dict_rows(source.get("portfolioHistory"))
            if (row_at := _row_timestamp(row, "sourceAsOf", "source_as_of", "asOf")) is not None
            and row_at <= now
        ]
        checks = [
            dict(row)
            for row in _dict_rows(source.get("decisionChecks"))
            if ((row_at := _row_timestamp(row, "checkedAt", "checked_at", "sourceAsOf", "source_as_of", "created_at")) is None or row_at <= now)
        ]
        alerts = [
            dict(row)
            for row in _dict_rows(source.get("alerts"))
            if ((row_at := _row_timestamp(row, "createdAt", "created_at")) is None or row_at <= now)
        ]
        before_by_fill: dict[str, dict[str, Any]] = {}
        after_by_fill: dict[str, dict[str, Any]] = {}
        checks_by_fill: dict[str, list[dict[str, Any]]] = {}
        for fill in today_fills:
            fill_id = str(fill["fillId"])
            fill_at = _as_datetime(fill.get("filledAt")) or now
            before_by_fill[fill_id], after_by_fill[fill_id] = _portfolio_pair(portfolio_history, fill_at)
            checks_by_fill[fill_id] = [_normalize_check(row) for row in checks if str(row.get("fill_id") or row.get("fillId")) == fill_id]

        requested_selected_id = str(request.get("selectedFillId") or "")
        valid_fill_ids = {str(fill["fillId"]) for fill in today_fills}
        selected_id = requested_selected_id if requested_selected_id in valid_fill_ids else str(today_fills[0]["fillId"] if today_fills else "")
        selected_case = case_by_fill.get(selected_id, {"caseId": selected_id or "current", "series": [], "missedChecks": []})
        if not selected_case.get("series") and today_fills:
            missing.append(_missing("chart", "current_chart_missing", "현재 거래의 검증된 일봉 시계열이 없습니다."))
        if not portfolio_history:
            missing.append(_missing("portfolio", "portfolio_history_missing", "거래 전후 포트폴리오 이력이 없습니다."))
        if not checks:
            missing.append(_missing("decision_checks", "confirmation_log_missing", "거래 당시 확인 기록이 없습니다."))
        if today_fills and any(fill.get("currentPrice") is None for fill in today_fills):
            missing.append(_missing("market", "current_quote_missing", "현재가 point-in-time quote가 연결되지 않았습니다."))
        missing.extend([
            _missing("news", "provider_not_connected", "뉴스 point-in-time 원천이 Snapshot Builder에 연결되지 않았습니다."),
            _missing("fundamentals", "provider_not_connected", "재무 point-in-time 원천이 Snapshot Builder에 연결되지 않았습니다."),
            _missing("earnings", "provider_not_connected", "실적 일정 point-in-time 원천이 Snapshot Builder에 연결되지 않았습니다."),
            _missing("ontology", "provider_not_connected", "온톨로지 point-in-time 원천이 Snapshot Builder에 연결되지 않았습니다."),
        ])

        order_as_of = _latest_value(today_fills + history_fills, "filledAt")
        portfolio_as_of = _latest_value(portfolio_history, "sourceAsOf", "source_as_of", "updated_at")
        checks_as_of = _latest_value(checks, "checkedAt", "checked_at", "sourceAsOf", "source_as_of")
        source_as_of = {
            "fills": order_as_of,
            "portfolio": portfolio_as_of,
            "market": market_as_of,
            "decisionChecks": checks_as_of,
            "news": None,
            "fundamentals": None,
            "earnings": None,
            "ontology": None,
        }
        for item in _dict_rows(source.get("missingData")):
            missing.append(dict(item))

        selected_before = before_by_fill.get(selected_id, {})
        selected_after = after_by_fill.get(selected_id, {})
        return {
            "schemaVersion": "coach-input.v1",
            "request": {
                "analysisId": analysis_id,
                "requestedAt": now.isoformat().replace("+00:00", "Z"),
                "tradingDate": trading_date.isoformat(),
                "selectedFillId": selected_id or None,
                "decisionChecksByFillId": checks_by_fill,
                "alerts": alerts,
            },
            "user": {"subjectHash": hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:24]},
            "fills": today_fills,
            "positionsBefore": _positions(selected_before),
            "positionsAfter": _positions(selected_after),
            "portfolioBefore": {"selected": selected_before, "byFillId": before_by_fill, "history": portfolio_history[:50]},
            "portfolioAfter": {"selected": selected_after, "byFillId": after_by_fill},
            "marketContext": {},
            "chartContext": {
                "currentCase": selected_case,
                "currentCasesByFillId": {fill_id: case for fill_id, case in case_by_fill.items() if fill_id in before_by_fill},
                "historicalCases": history_cases,
            },
            "indicatorContext": {},
            "newsContext": {},
            "fundamentalsContext": {},
            "earningsContext": {},
            "ontologyContext": {},
            "sourceAsOf": source_as_of,
            "missingData": _dedupe_missing(missing),
        }


class PostgresCoachSnapshotProvider:
    def __init__(self, conninfo: str | None = None) -> None:
        self.conninfo = conninfo or _database_conninfo()

    def load(self, user_id: str, *, requested_at: datetime, trading_date: date) -> dict[str, Any]:
        if not self.conninfo:
            return {"missingData": [_missing("account", "database_not_configured", "주문 데이터베이스 연결이 구성되지 않았습니다.")]}
        import psycopg
        from psycopg.rows import dict_row

        start, day_end = _market_day_bounds(trading_date)
        end = min(requested_at, day_end)
        with psycopg.connect(self.conninfo, row_factory=dict_row) as conn:
            fills = conn.execute(_FILLS_SQL, (user_id, start, end, 20)).fetchall()
            historical = conn.execute(_HISTORICAL_FILLS_SQL, (user_id, start, 50)).fetchall()
            portfolio = conn.execute(_PORTFOLIO_HISTORY_SQL, (user_id, requested_at, 200)).fetchall()
            checks = conn.execute(_DECISION_CHECKS_SQL, (user_id, requested_at, 500)).fetchall()
            alerts = conn.execute(_ALERTS_SQL, (user_id,)).fetchall()
        return {
            "fills": [_json_ready(row) for row in fills],
            "historicalFills": [_json_ready(row) for row in historical],
            "portfolioHistory": [_portfolio_row(row) for row in portfolio],
            "decisionChecks": [_json_ready(row) for row in checks],
            "alerts": [_json_ready(row) for row in alerts],
        }


class ClickHouseCoachMarketProvider:
    def __init__(self, provider=None) -> None:
        self.provider = provider

    def trade_case(self, fill: dict[str, Any], *, requested_at: datetime) -> dict[str, Any]:
        entry_at = _as_datetime(fill.get("filledAt"))
        if entry_at is None:
            return {"caseId": str(fill.get("fillId") or "unknown"), "series": [], "missedChecks": []}
        provider = self.provider or self._default_provider()
        from_time = (entry_at - timedelta(days=100)).isoformat()
        to_time = min(requested_at, entry_at + timedelta(days=35)).isoformat()
        rows = provider.candles(str(fill.get("symbol") or ""), "1D", 120, from_time=from_time, to_time=to_time)
        points = _indicator_series(rows, entry_at)
        features = _entry_features(points, entry_at)
        return {
            "caseId": str(fill.get("fillId") or "unknown"),
            "tradeDate": fill.get("filledAt"),
            "symbol": fill.get("symbol"),
            "side": fill.get("side"),
            "entryPrice": fill.get("averageFillPrice"),
            "evaluationPrice": points[-1].get("close") if points else None,
            "evaluationAt": points[-1].get("time") if points else None,
            "seriesInterval": "1D",
            "series": points,
            "missedChecks": [],
            **features,
        }

    def _default_provider(self):
        from alfaka.serving.clickhouse_provider import ClickHouseMarketDataProvider

        self.provider = ClickHouseMarketDataProvider()
        return self.provider


def _sanitize_trade_case(raw_case: Any, fill: dict[str, Any], *, requested_at: datetime) -> dict[str, Any]:
    """Keep market-provider output bounded to the immutable request cutoff."""
    source = dict(raw_case) if isinstance(raw_case, dict) else {}
    points: list[dict[str, Any]] = []
    for raw_point in _dict_rows(source.get("series")):
        point_at = _as_datetime(raw_point.get("time") or raw_point.get("timestamp"))
        try:
            relative_day = int(raw_point.get("relativeDay"))
        except (TypeError, ValueError):
            continue
        if point_at is None or point_at > requested_at or relative_day < -60 or relative_day > 20:
            continue
        point = dict(raw_point)
        point["relativeDay"] = relative_day
        point["time"] = _iso(point_at)
        points.append(point)
    points.sort(key=lambda item: (int(item["relativeDay"]), str(item.get("time") or "")))

    entry_at = _as_datetime(fill.get("filledAt"))
    features = _entry_features(points, entry_at) if entry_at is not None else {
        "featureAsOf": None,
        "rsiBand": None,
        "macdState": None,
        "volumeState": None,
        "trend": None,
        "momentum": None,
    }
    latest = points[-1] if points else {}
    return {
        **source,
        "caseId": str(fill.get("fillId") or "unknown"),
        "tradeDate": fill.get("filledAt"),
        "symbol": fill.get("symbol"),
        "side": fill.get("side"),
        "entryPrice": fill.get("averageFillPrice"),
        "evaluationPrice": latest.get("close"),
        "evaluationAt": latest.get("time"),
        "seriesInterval": "1D",
        "series": points,
        **features,
    }


_FILLS_SQL = """
SELECT o.*, e.execution_id AS fill_id,
       e.created_at AS filled_at,
       e.payload AS execution_payload
FROM orders o
JOIN executions e ON e.order_id = o.order_id
WHERE o.user_sub = %s
  AND o.status IN ('FILLED', 'PARTIALLY_FILLED')
  AND e.created_at >= %s
  AND e.created_at < %s
ORDER BY filled_at DESC
LIMIT %s
"""

_HISTORICAL_FILLS_SQL = """
SELECT o.*, e.execution_id AS fill_id,
       e.created_at AS filled_at,
       e.payload AS execution_payload
FROM orders o
JOIN executions e ON e.order_id = o.order_id
WHERE o.user_sub = %s
  AND o.status IN ('FILLED', 'PARTIALLY_FILLED')
  AND e.created_at < %s
ORDER BY filled_at DESC
LIMIT %s
"""

_PORTFOLIO_HISTORY_SQL = """
SELECT payload, source_as_of FROM user_portfolio_snapshot_history
WHERE user_sub = %s AND source_as_of <= %s
ORDER BY source_as_of DESC LIMIT %s
"""

_DECISION_CHECKS_SQL = """
SELECT * FROM trade_decision_check_events
WHERE user_sub = %s AND created_at <= %s
ORDER BY created_at DESC LIMIT %s
"""

_ALERTS_SQL = """
SELECT id, symbol, type, direction, target_price, change_pct, window_min, status, proposal_source, created_at
FROM alerts WHERE user_sub = %s AND status IN ('active', 'disabled')
ORDER BY created_at DESC LIMIT 100
"""


def _normalize_fill(row: dict[str, Any]) -> dict[str, Any]:
    execution = row.get("execution_payload") if isinstance(row.get("execution_payload"), dict) else {}
    price = _first_number(execution, "price", "px", "fill_price", "filled_price", "avg_fill_price", "avg_price")
    qty = _first_number(execution, "filled_qty", "qty", "quantity")
    return {
        "fillId": str(row.get("fillId") or row.get("fill_id") or row.get("execution_id") or row.get("order_id") or ""),
        "orderId": row.get("order_id"),
        "symbol": str(row.get("symbol") or "").upper(),
        "companyName": row.get("companyName") or row.get("company_name") or str(row.get("symbol") or "").upper(),
        "side": str(row.get("side") or "").lower(),
        "filledAt": _iso(row.get("filledAt") or row.get("filled_at") or row.get("updated_at") or row.get("occurred_at")),
        "averageFillPrice": price,
        "quantity": qty,
        # Only an explicit point-in-time quote may populate currentPrice. The
        # latest daily candle can predate an intraday fill and is not a quote.
        "currentPrice": _first_number(row, "currentPrice", "current_price"),
        "sector": row.get("sector"),
    }


def _indicator_series(rows: list[dict[str, Any]], entry_at: datetime) -> list[dict[str, Any]]:
    clean = [dict(row) for row in rows if isinstance(row, dict) and _number(row.get("close")) is not None]
    if not clean:
        return []
    times = [_as_datetime(row.get("timestamp") or row.get("time")) for row in clean]
    entry_trading_date = entry_at.astimezone(MARKET_TIMEZONE).date()
    entry_index = min(
        range(len(clean)),
        key=lambda index: abs(((times[index] or entry_at).astimezone(timezone.utc).date() - entry_trading_date).days),
    )
    closes = [float(row["close"]) for row in clean]
    volumes = [_number(row.get("volume")) for row in clean]
    rsi = _rsi(closes, 14)
    macd, signal = _macd(closes)
    result: list[dict[str, Any]] = []
    for index, row in enumerate(clean):
        relative_day = index - entry_index
        if relative_day < -60 or relative_day > 20:
            continue
        prior_volumes = [value for value in volumes[max(0, index - 20):index] if value is not None]
        avg_volume = sum(prior_volumes) / len(prior_volumes) if prior_volumes else None
        volume = volumes[index]
        result.append({
            "relativeDay": relative_day,
            "time": _iso(row.get("timestamp") or row.get("time")),
            "open": _number(row.get("open")), "high": _number(row.get("high")),
            "low": _number(row.get("low")), "close": _number(row.get("close")),
            "volume": volume,
            "relativeVolume": round(volume / avg_volume, 4) if volume is not None and avg_volume else None,
            "rsi": rsi[index], "macd": macd[index], "signal": signal[index],
            "histogram": round(macd[index] - signal[index], 8) if macd[index] is not None and signal[index] is not None else None,
        })
    return result


def _entry_features(points: list[dict[str, Any]], entry_at: datetime) -> dict[str, Any]:
    # A daily candle stamped at midnight contains the full regular-session bar.
    # For an intraday fill it is therefore post-entry information even when its
    # timestamp is earlier than the fill. Similarity features use only the last
    # fully closed trading date strictly before the fill date.
    entry_trading_date = entry_at.astimezone(MARKET_TIMEZONE).date()
    eligible = [
        point
        for point in points
        if (point_at := _as_datetime(point.get("time"))) is not None
        and point_at.astimezone(timezone.utc).date() < entry_trading_date
    ]
    entry = eligible[-1] if eligible else {}
    feature_relative_day = int(entry.get("relativeDay", -999)) if entry else -999
    rsi = _number(entry.get("rsi")); macd = _number(entry.get("macd")); signal = _number(entry.get("signal")); rv = _number(entry.get("relativeVolume"))
    prior = [
        point
        for point in points
        if feature_relative_day - 20 <= int(point.get("relativeDay", -999)) < feature_relative_day
        and _number(point.get("close")) is not None
    ]
    avg_close = sum(float(point["close"]) for point in prior) / len(prior) if prior else None
    close = _number(entry.get("close"))
    return {
        "featureAsOf": entry.get("time"),
        "rsiBand": "overbought" if rsi is not None and rsi >= 70 else "oversold" if rsi is not None and rsi <= 30 else "neutral" if rsi is not None else None,
        "macdState": "bullish" if macd is not None and signal is not None and macd >= signal else "bearish" if macd is not None and signal is not None else None,
        "volumeState": "confirmed" if rv is not None and rv >= 1.2 else "weak" if rv is not None else None,
        "trend": "up" if close is not None and avg_close is not None and close >= avg_close else "down" if close is not None and avg_close is not None else None,
        "momentum": "positive" if macd is not None and macd >= 0 else "negative" if macd is not None else None,
    }


def _portfolio_pair(rows: list[dict[str, Any]], fill_at: datetime) -> tuple[dict[str, Any], dict[str, Any]]:
    ordered = sorted(rows, key=lambda row: _as_datetime(row.get("sourceAsOf") or row.get("source_as_of")) or datetime.min.replace(tzinfo=timezone.utc))
    before = [row for row in ordered if (_as_datetime(row.get("sourceAsOf") or row.get("source_as_of")) or fill_at) <= fill_at]
    after = [row for row in ordered if (_as_datetime(row.get("sourceAsOf") or row.get("source_as_of")) or fill_at) >= fill_at]
    return (dict(before[-1]) if before else {}, dict(after[0]) if after else {})


def _portfolio_row(row: dict[str, Any]) -> dict[str, Any]:
    payload = dict(row.get("payload") or {}) if isinstance(row.get("payload"), dict) else {}
    return {**payload, "sourceAsOf": _iso(row.get("source_as_of") or payload.get("asOf") or payload.get("sourceAsOf"))}


def _positions(portfolio: dict[str, Any]) -> list[dict[str, Any]]:
    value = portfolio.get("positions")
    if isinstance(value, dict):
        value = list(value.values())
    return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _dict_rows(value: Any) -> list[dict[str, Any]]:
    return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, (list, tuple)) else []


def _row_timestamp(row: dict[str, Any], *keys: str) -> datetime | None:
    for key in keys:
        parsed = _as_datetime(row.get(key))
        if parsed is not None:
            return parsed.astimezone(timezone.utc)
    return None


def _normalize_check(row: dict[str, Any]) -> dict[str, Any]:
    evidence = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
    return {
        "category": row.get("category"), "label": row.get("label"), "status": row.get("status"),
        "checkedAt": _iso(row.get("checked_at") or row.get("checkedAt")),
        "evidence": evidence.get("summary") or evidence.get("value"),
        "source": row.get("source"), "sourceAsOf": _iso(row.get("source_as_of") or row.get("sourceAsOf")),
        "marker": evidence.get("marker") if isinstance(evidence.get("marker"), dict) else None,
    }


def _rsi(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    gains: list[float] = []; losses: list[float] = []
    for index in range(1, len(values)):
        delta = values[index] - values[index - 1]
        gains.append(max(delta, 0.0)); losses.append(max(-delta, 0.0))
        if index < period:
            continue
        window_gains = gains[index - period:index]; window_losses = losses[index - period:index]
        avg_gain = sum(window_gains) / period; avg_loss = sum(window_losses) / period
        result[index] = round(100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss), 4)
    return result


def _ema(values: list[float], period: int) -> list[float]:
    alpha = 2 / (period + 1); result: list[float] = []
    for value in values:
        result.append(value if not result else alpha * value + (1 - alpha) * result[-1])
    return result


def _macd(values: list[float]) -> tuple[list[float | None], list[float | None]]:
    fast = _ema(values, 12); slow = _ema(values, 26)
    line = [round(left - right, 8) for left, right in zip(fast, slow)]
    signal = _ema(line, 9)
    return ([value if index >= 25 else None for index, value in enumerate(line)], [value if index >= 25 else None for index, value in enumerate(signal)])


def _database_conninfo() -> str | None:
    if os.getenv("DATABASE_URL"):
        return os.getenv("DATABASE_URL")
    required = [os.getenv("DATABASE_HOST"), os.getenv("DATABASE_NAME"), os.getenv("DATABASE_USER"), os.getenv("DATABASE_PASSWORD")]
    if not all(required):
        return None
    from psycopg.conninfo import make_conninfo
    return make_conninfo(host=required[0], port=os.getenv("DATABASE_PORT", "5432"), dbname=required[1], user=required[2], password=required[3])


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _as_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _market_day_bounds(trading_date: date) -> tuple[datetime, datetime]:
    start = datetime.combine(trading_date, time.min, tzinfo=MARKET_TIMEZONE)
    end = datetime.combine(trading_date + timedelta(days=1), time.min, tzinfo=MARKET_TIMEZONE)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _iso(value: Any) -> str | None:
    parsed = _as_datetime(value)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if parsed else (str(value) if value else None)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _first_number(source: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _number(source.get(key))
        if value is not None:
            return value
    return None


def _latest_value(rows: list[dict[str, Any]], *keys: str) -> str | None:
    values = [_iso(row.get(key)) for row in rows for key in keys if row.get(key)]
    return max(values) if values else None


def _missing(source: str, code: str, message: str) -> dict[str, str]:
    return {"source": source, "code": code, "message": message}


def _dedupe_missing(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []; seen: set[tuple[str, str]] = set()
    for item in items:
        key = (str(item.get("source") or "unknown"), str(item.get("code") or "unknown"))
        if key not in seen:
            seen.add(key); result.append(item)
    return result


def _json_ready(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_ready(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(child) for child in value]
    return value
