from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from .coach_point_in_time import (
    CoachPointInTimeContextProvider,
    StoreCoachPointInTimeContextProvider,
)


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
        context_provider: CoachPointInTimeContextProvider | None = None,
        now_provider=None,
    ) -> None:
        self.data_provider = data_provider or S3CoachSnapshotDataProvider()
        self.market_provider = market_provider or ClickHouseCoachMarketProvider()
        self.context_provider = context_provider or StoreCoachPointInTimeContextProvider()
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
        all_fills = [*today_fills, *history_fills]
        current_fill_ids = {str(fill["fillId"]) for fill in today_fills}
        for fill in all_fills:
            fill_id = str(fill["fillId"])
            fill_at = _as_datetime(fill.get("filledAt")) or now
            normalized_checks = [
                _normalize_check(row)
                for row in checks
                if str(row.get("fill_id") or row.get("fillId")) == fill_id
            ]
            checks_by_fill[fill_id] = normalized_checks
            checked_times = [
                checked_at
                for item in normalized_checks
                if (checked_at := _as_datetime(item.get("checkedAt"))) is not None
                and checked_at <= fill_at
            ]
            decision_at = _as_datetime(fill.get("decisionAt"))
            decision_candidates = [value for value in [decision_at, *checked_times] if value is not None and value <= fill_at]
            fill["decisionAt"] = _iso(min(decision_candidates) if decision_candidates else fill_at)
            before_by_fill[fill_id], after_by_fill[fill_id] = _portfolio_pair(
                portfolio_history,
                fill_at,
                fill_id=fill_id,
                execution_mode=str(fill.get("executionMode") or "kis"),
            )

        context = _empty_context_result()
        try:
            loaded_context = self.context_provider.load(
                fills=all_fills,
                requested_at=now,
                portfolio_before_by_fill_id=before_by_fill,
                portfolio_after_by_fill_id=after_by_fill,
                portfolio_current=_latest_portfolio_snapshot(portfolio_history),
                current_fill_ids=current_fill_ids,
            )
            if isinstance(loaded_context, dict):
                context.update(loaded_context)
            else:
                missing.append(_missing("context", "context_provider_invalid", "point-in-time context 응답 형식이 올바르지 않습니다."))
        except Exception as exc:
            missing.append(_missing("context", "context_provider_unavailable", f"point-in-time context 조회 실패: {exc.__class__.__name__}"))

        fill_enrichment = context.get("fillEnrichmentById")
        fill_enrichment = fill_enrichment if isinstance(fill_enrichment, dict) else {}
        today_fills = [_enrich_fill(fill, fill_enrichment.get(str(fill["fillId"]))) for fill in today_fills]
        history_fills = [_enrich_fill(fill, fill_enrichment.get(str(fill["fillId"]))) for fill in history_fills]

        case_by_fill: dict[str, dict[str, Any]] = {}
        chart_as_of: str | None = None
        configured_case_limit = max(6, min(30, int(os.getenv("AI_COACH_MARKET_CASE_LIMIT", "12"))))
        case_candidates = [*today_fills, *history_fills[:max(0, configured_case_limit - len(today_fills))]]
        market_provider_failed = False
        for fill in case_candidates:
            try:
                raw_case = self.market_provider.trade_case(fill, requested_at=now)
                case = _sanitize_trade_case(raw_case, fill, requested_at=now)
            except Exception as exc:
                missing.append(_missing("market", "trade_case_unavailable", f"{fill.get('symbol')} 차트 조회 실패: {exc.__class__.__name__}"))
                market_provider_failed = True
                break
            if case.get("series"):
                case_by_fill[str(fill["fillId"])] = case
                last_time = str(case["series"][-1].get("time") or "")
                chart_as_of = max(filter(None, [chart_as_of, last_time]), default=None)
        if market_provider_failed and len(case_candidates) > 1:
            missing.append(_missing("market", "trade_case_batch_stopped", "시장 데이터 장애 후 반복 조회를 중단했습니다."))

        checks_by_fill = _enrich_decision_checks(
            checks_by_fill,
            fills=[*today_fills, *history_fills],
            cases_by_fill=case_by_fill,
            news_context=context.get("newsContext"),
            fundamentals_context=context.get("fundamentalsContext"),
            earnings_context=context.get("earningsContext"),
            market_context=context.get("marketContext"),
        )

        history_cases = []
        for fill in history_fills:
            fill_id = str(fill["fillId"])
            case = case_by_fill.get(fill_id)
            if case:
                history_cases.append({**fill, **case, "decisionChecks": checks_by_fill.get(fill_id, [])})

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
        for item in _dict_rows(context.get("missingData")):
            missing.append(dict(item))

        order_as_of = _latest_value(today_fills + history_fills, "fillObservedAt", "filledAt")
        portfolio_as_of = _latest_value(portfolio_history, "sourceAsOf", "source_as_of", "updated_at")
        checks_as_of = _latest_value(checks, "checkedAt", "checked_at", "sourceAsOf", "source_as_of")
        context_as_of = context.get("sourceAsOf") if isinstance(context.get("sourceAsOf"), dict) else {}
        source_as_of = {
            "fills": order_as_of,
            "portfolio": portfolio_as_of,
            "market": _latest_iso(chart_as_of, context_as_of.get("market")),
            "decisionChecks": checks_as_of,
            "news": context_as_of.get("news"),
            "fundamentals": context_as_of.get("fundamentals"),
            "earnings": context_as_of.get("earnings"),
            "ontology": context_as_of.get("ontology"),
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
            "marketContext": _dict_value(context.get("marketContext")),
            "chartContext": {
                "currentCase": selected_case,
                "currentCasesByFillId": {fill_id: case for fill_id, case in case_by_fill.items() if fill_id in valid_fill_ids},
                "historicalCases": history_cases,
            },
            "indicatorContext": {
                "featuresByFillId": {
                    fill_id: {key: case.get(key) for key in ("featureAsOf", "rsiBand", "macdState", "volumeState", "trend", "momentum")}
                    for fill_id, case in case_by_fill.items()
                }
            },
            "newsContext": _dict_value(context.get("newsContext")),
            "fundamentalsContext": _dict_value(context.get("fundamentalsContext")),
            "earningsContext": _dict_value(context.get("earningsContext")),
            "ontologyContext": _dict_value(context.get("ontologyContext")),
            "sourceAsOf": source_as_of,
            "missingData": _dedupe_missing(missing),
        }


class S3CoachSnapshotDataProvider:
    """Read a completed, user-owned post-market input archive from S3.

    The coach is a retrospective reader.  It does not query or mutate the
    order service, paper account, Redis, or a broker at panel-open time.  The
    upstream post-market export owns this input contract and writes one JSON
    object per user and trading date before the coach job runs.
    """

    def __init__(self, *, client=None, bucket: str | None = None, prefix: str | None = None) -> None:
        self.client = client
        self.bucket = bucket or os.getenv("AI_COACH_INPUT_S3_BUCKET") or os.getenv("AI_COACH_SNAPSHOT_S3_BUCKET")
        self.prefix = (prefix or os.getenv("AI_COACH_INPUT_S3_PREFIX", "ai-coach/input/v1")).strip("/")

    def load(self, user_id: str, *, requested_at: datetime, trading_date: date) -> dict[str, Any]:
        if not self.bucket:
            return {"missingData": [_missing("account", "input_archive_not_configured", "AI 코치용 거래 archive S3가 구성되지 않았습니다.")]}
        key = self._key(user_id, trading_date)
        try:
            response = (self.client or self._default_client()).get_object(Bucket=self.bucket, Key=key)
            body = response.get("Body") if isinstance(response, dict) else None
            raw = body.read() if hasattr(body, "read") else body
            payload = json.loads(bytes(raw).decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw or ""))
        except Exception as exc:
            code = _s3_error_code(exc)
            if code in {"NoSuchKey", "404", "NotFound"}:
                return {"missingData": [_missing("account", "input_archive_missing", "해당 거래일의 AI 코치 거래 archive가 아직 생성되지 않았습니다.")]}
            return {"missingData": [_missing("account", "input_archive_unavailable", f"AI 코치 거래 archive 조회 실패: {exc.__class__.__name__}")]}
        if not isinstance(payload, dict):
            return {"missingData": [_missing("account", "input_archive_invalid", "AI 코치 거래 archive 형식이 올바르지 않습니다.")]}
        archive_as_of = _as_datetime(payload.get("sourceAsOf") or payload.get("generatedAt"))
        if archive_as_of is None:
            return {"missingData": [_missing("account", "input_archive_as_of_missing", "AI 코치 거래 archive의 기준시각이 없습니다.")]}
        if archive_as_of > requested_at:
            return {"missingData": [_missing("account", "input_archive_future", "요청 기준시각 이후에 생성된 거래 archive는 사용할 수 없습니다.")]}
        return payload

    def _key(self, user_id: str, trading_date: date) -> str:
        subject_hash = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:24]
        return f"{self.prefix}/user={subject_hash}/date={trading_date.isoformat()}.json"

    def _default_client(self):
        import boto3

        self.client = boto3.client("s3", region_name=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION"))
        return self.client


class ClickHouseCoachMarketProvider:
    def __init__(self, provider=None) -> None:
        self.provider = provider

    def trade_case(self, fill: dict[str, Any], *, requested_at: datetime) -> dict[str, Any]:
        entry_at = _as_datetime(fill.get("filledAt"))
        if entry_at is None:
            return {"caseId": str(fill.get("fillId") or "unknown"), "series": [], "missedChecks": []}
        decision_at = _as_datetime(fill.get("decisionAt")) or entry_at
        decision_at = min(decision_at, entry_at, requested_at)
        provider = self.provider or self._default_provider()
        symbol = str(fill.get("symbol") or "")
        vintage_capable = hasattr(provider, "latest_canonical_daily_source") and hasattr(provider, "query_json_each_row")
        from_time = entry_at - timedelta(days=120)
        to_time = min(requested_at, entry_at + timedelta(days=45))
        rows = self._vintage_daily_rows(
            provider,
            symbol=symbol,
            from_time=from_time,
            to_time=to_time,
            available_at=requested_at,
        )
        feature_rows = self._vintage_daily_rows(
            provider,
            symbol=symbol,
            from_time=from_time,
            to_time=min(to_time, decision_at),
            available_at=decision_at,
        ) if vintage_capable else []
        points = _indicator_series(rows, entry_at)
        feature_points = _indicator_series(feature_rows, entry_at)
        features = _entry_features(feature_points, decision_at)
        decision_point = _feature_point(feature_points, decision_at)
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
            "featureVintageAsOf": _iso(decision_at) if vintage_capable else None,
            "decisionPoint": decision_point if vintage_capable else None,
            **features,
        }

    @staticmethod
    def _vintage_daily_rows(
        provider: Any,
        *,
        symbol: str,
        from_time: datetime,
        to_time: datetime,
        available_at: datetime,
    ) -> list[dict[str, Any]]:
        """Read one canonical revision per trading day as it existed at cutoff."""

        if not hasattr(provider, "latest_canonical_daily_source") or not hasattr(provider, "query_json_each_row"):
            # A provider without revision-aware primitives may be used by tests
            # for display only, but it is never eligible for similarity inputs.
            if hasattr(provider, "candles"):
                return list(provider.candles(
                    symbol,
                    "1D",
                    140,
                    from_time=_iso(from_time),
                    to_time=_iso(to_time),
                ))
            return []
        source_query = provider.latest_canonical_daily_source("""
            symbol = {symbol:String}
            AND interval IN ('1D', '1d')
            AND is_closed = 1
            AND event_time >= parseDateTime64BestEffort({fromTime:String})
            AND event_time <= parseDateTime64BestEffort({toTime:String})
            AND inserted_at <= parseDateTime64BestEffort({availableAt:String})
        """)
        query = f"""
        SELECT
          formatDateTime(event_time, '%Y-%m-%dT%H:%i:%S.000Z', 'UTC') AS timestamp,
          open, high, low, close, volume,
          inserted_at AS insertedAt,
          source_event_id AS sourceEventId
        FROM (
          {source_query}
        )
        ORDER BY event_time ASC
        LIMIT 140
        FORMAT JSONEachRow
        """
        rows = provider.query_json_each_row(query, {
            "symbol": symbol,
            "fromTime": _iso(from_time),
            "toTime": _iso(to_time),
            "availableAt": _iso(available_at),
        })
        return [dict(row) for row in rows or [] if isinstance(row, dict)]

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
    decision_at = min(_as_datetime(fill.get("decisionAt")) or entry_at or requested_at, entry_at or requested_at, requested_at)
    vintage_at = _as_datetime(source.get("featureVintageAsOf"))
    feature_at = _as_datetime(source.get("featureAsOf"))
    vintage_is_valid = (
        vintage_at is not None
        and vintage_at <= decision_at
        and (feature_at is None or feature_at <= decision_at)
    )
    raw_decision_point = source.get("decisionPoint") if isinstance(source.get("decisionPoint"), dict) else {}
    decision_point_at = _as_datetime(raw_decision_point.get("time"))
    decision_point = dict(raw_decision_point) if (
        vintage_is_valid
        and decision_point_at is not None
        and decision_point_at <= decision_at
        and _candle_trading_date(decision_point_at) < decision_at.astimezone(MARKET_TIMEZONE).date()
    ) else {}
    features = {
        "featureAsOf": _iso(source.get("featureAsOf")),
        "rsiBand": source.get("rsiBand"),
        "macdState": source.get("macdState"),
        "volumeState": source.get("volumeState"),
        "trend": source.get("trend"),
        "momentum": source.get("momentum"),
    } if vintage_is_valid else {
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
        "featureVintageAsOf": _iso(vintage_at) if vintage_is_valid else None,
        "decisionPoint": decision_point or None,
        **features,
    }


def _normalize_fill(row: dict[str, Any]) -> dict[str, Any]:
    execution = row.get("execution_payload") if isinstance(row.get("execution_payload"), dict) else {}
    price = _first_number(execution, "average_fill_price", "avg_fill_price", "avg_price", "fill_price", "filled_price", "px", "price")
    if price is None:
        price = _first_number(row, "averageFillPrice", "average_fill_price", "fill_price", "filled_price")
    qty = _first_number(execution, "filled_qty", "qty", "quantity")
    if qty is None:
        qty = _first_number(row, "quantity", "filled_qty", "qty")
    execution_mode = str(row.get("executionMode") or row.get("execution_mode") or "kis").lower()
    filled_at = row.get("filledAt") or row.get("filled_at") or row.get("updated_at") or row.get("occurred_at")
    decision_at = row.get("decisionAt") or row.get("decision_at") or row.get("occurred_at") or row.get("created_at") or filled_at
    return {
        "fillId": str(row.get("fillId") or row.get("fill_id") or row.get("execution_id") or row.get("order_id") or ""),
        "orderId": row.get("order_id"),
        "symbol": str(row.get("symbol") or "").upper(),
        "companyName": row.get("companyName") or row.get("company_name") or str(row.get("symbol") or "").upper(),
        "side": str(row.get("side") or "").lower(),
        "filledAt": _iso(filled_at),
        "fillObservedAt": _iso(row.get("fillObservedAt") or row.get("fill_observed_at") or filled_at),
        "decisionAt": _iso(decision_at),
        "averageFillPrice": price,
        "quantity": qty,
        # Only an explicit point-in-time quote may populate currentPrice. The
        # latest daily candle can predate an intraday fill and is not a quote.
        "currentPrice": _first_number(row, "currentPrice", "current_price"),
        "sector": row.get("sector"),
        "executionMode": execution_mode if execution_mode in {"kis", "paper"} else "kis",
        "generation": row.get("generation"),
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
    entry = _feature_point(points, entry_at)
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


def _feature_point(points: list[dict[str, Any]], decision_at: datetime) -> dict[str, Any]:
    decision_trading_date = decision_at.astimezone(MARKET_TIMEZONE).date()
    eligible = [
        point
        for point in points
        if (point_at := _as_datetime(point.get("time"))) is not None
        and _candle_trading_date(point_at) < decision_trading_date
    ]
    eligible.sort(key=lambda point: str(point.get("time") or ""))
    return dict(eligible[-1]) if eligible else {}


def _candle_trading_date(value: datetime) -> date:
    utc_value = value.astimezone(timezone.utc)
    if utc_value.time().replace(tzinfo=None) == time.min:
        return utc_value.date()
    return value.astimezone(MARKET_TIMEZONE).date()


def _portfolio_pair(
    rows: list[dict[str, Any]],
    fill_at: datetime,
    *,
    fill_id: str | None = None,
    execution_mode: str = "kis",
) -> tuple[dict[str, Any], dict[str, Any]]:
    direct = [row for row in rows if fill_id and str(row.get("fillId") or row.get("fill_id") or "") == fill_id]
    if direct:
        before = next((dict(row) for row in direct if str(row.get("phase") or "") == "before"), {})
        after = next((dict(row) for row in direct if str(row.get("phase") or "") == "after"), {})
        if before or after:
            return before, after

    # An adjacent account snapshot is not proof that it belongs to this fill:
    # another execution can occur between the two timestamps. Until a writer
    # records fillId+phase, report the impact as not calculated for every mode.
    return {}, {}


def _positions(portfolio: dict[str, Any]) -> list[dict[str, Any]]:
    value = portfolio.get("positions")
    if isinstance(value, dict):
        value = list(value.values())
    return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _latest_portfolio_snapshot(rows: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = [row for row in rows if _row_timestamp(row, "sourceAsOf", "source_as_of", "asOf") is not None]
    if not eligible:
        return {}
    return max(eligible, key=lambda row: _row_timestamp(row, "sourceAsOf", "source_as_of", "asOf") or datetime.min.replace(tzinfo=timezone.utc))


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
        "checkKey": row.get("check_key") or row.get("checkKey") or evidence.get("checkKey"),
        "category": row.get("category"), "label": row.get("label"), "status": row.get("status"),
        "checkedAt": _iso(row.get("checked_at") or row.get("checkedAt")),
        "evidence": evidence.get("summary") or evidence.get("value"),
        "source": row.get("source"), "sourceAsOf": _iso(row.get("source_as_of") or row.get("sourceAsOf")),
        "marker": evidence.get("marker") if isinstance(evidence.get("marker"), dict) else None,
    }


def _empty_context_result() -> dict[str, Any]:
    return {
        "fillEnrichmentById": {},
        "marketContext": {},
        "newsContext": {},
        "fundamentalsContext": {},
        "earningsContext": {},
        "ontologyContext": {},
        "sourceAsOf": {},
        "missingData": [],
    }


def _dict_value(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _enrich_fill(fill: dict[str, Any], value: Any) -> dict[str, Any]:
    enrichment = value if isinstance(value, dict) else {}
    result = dict(fill)
    for key in ("companyName", "sector", "industry", "currentPrice"):
        if enrichment.get(key) is not None:
            result[key] = enrichment[key]
    for key in ("metadataSource", "metadataSourceAsOf", "currentPriceSource", "currentPriceAsOf"):
        if enrichment.get(key) is not None:
            result[key] = enrichment[key]
    return result


def _enrich_decision_checks(
    checks_by_fill: dict[str, list[dict[str, Any]]],
    *,
    fills: list[dict[str, Any]],
    cases_by_fill: dict[str, dict[str, Any]],
    news_context: Any,
    fundamentals_context: Any,
    earnings_context: Any,
    market_context: Any,
) -> dict[str, list[dict[str, Any]]]:
    """Attach immutable evidence to order-time confirmations.

    The checked/unchecked status always comes from the user's order-time event.
    Stored market evidence can explain that status, but it never changes it.
    """

    fills_by_id = {str(fill.get("fillId") or ""): fill for fill in fills}
    news_by_fill = _dict_value(_dict_value(news_context).get("byFillId"))
    fundamentals_by_fill = _dict_value(_dict_value(fundamentals_context).get("byFillId"))
    earnings_by_symbol = _dict_value(earnings_context)
    market = _dict_value(market_context)
    metadata_by_symbol = _dict_value(market.get("metadataBySymbol"))
    result: dict[str, list[dict[str, Any]]] = {}

    for fill_id, rows in checks_by_fill.items():
        fill = fills_by_id.get(fill_id, {})
        symbol = str(fill.get("symbol") or "")
        fill_at = _as_datetime(fill.get("filledAt"))
        decision_at = _as_datetime(fill.get("decisionAt")) or fill_at
        case = cases_by_fill.get(fill_id, {})
        decision_point = _decision_point(case)
        enriched_rows: list[dict[str, Any]] = []
        for raw in rows:
            item = dict(raw)
            key = str(item.get("checkKey") or "")
            evidence: str | None = None
            evidence_source: str | None = None
            evidence_as_of: str | None = None
            marker: dict[str, Any] | None = None

            if key == "chart.rsi" and _number(decision_point.get("rsi")) is not None:
                value = round(float(decision_point["rsi"]), 2)
                is_overbought = value >= 70
                evidence = f"RSI {value:g}"
                evidence_source = "clickhouse.chart_candles.1D"
                evidence_as_of = decision_point.get("time")
                marker = _check_marker(
                    fill_id, "rsi", "RSI 과열 미확인" if is_overbought else "RSI 확인 누락", decision_point, value, 70,
                    "RSI 과열 상태를 확인하지 않았습니다." if is_overbought else "진입 전 RSI 상태 확인 기록이 없습니다.", evidence_source,
                )
            elif key == "chart.macd" and _number(decision_point.get("macd")) is not None:
                macd = round(float(decision_point["macd"]), 4)
                signal = _number(decision_point.get("signal"))
                histogram = _number(decision_point.get("histogram"))
                value = round(histogram, 4) if histogram is not None else macd
                is_weak = (histogram is not None and histogram < 0) or (signal is not None and macd < signal)
                evidence = f"MACD {macd:g}" + (f" · signal {signal:.4g}" if signal is not None else "")
                evidence_source = "clickhouse.chart_candles.1D"
                evidence_as_of = decision_point.get("time")
                marker = _check_marker(
                    fill_id, "macd", "MACD 약화 미확인" if is_weak else "MACD 확인 누락", decision_point, value, 0,
                    "MACD 약화 상태를 확인하지 않았습니다." if is_weak else "진입 전 MACD 방향 확인 기록이 없습니다.", evidence_source,
                )
            elif key == "chart.volume" and _number(decision_point.get("relativeVolume")) is not None:
                value = round(float(decision_point["relativeVolume"]), 2)
                is_weak = value < 1.2
                evidence = f"상대 거래량 {value:g}배"
                evidence_source = "clickhouse.chart_candles.1D"
                evidence_as_of = decision_point.get("time")
                marker = _check_marker(
                    fill_id, "volume", "상대 거래량 부족 미확인" if is_weak else "거래량 확인 누락", decision_point, value, 1.2,
                    "상대 거래량이 기준보다 낮은 상태를 확인하지 않았습니다." if is_weak else "진입 전 상대 거래량 확인 기록이 없습니다.", evidence_source,
                )
            elif key == "news.company":
                items = _context_items(news_by_fill.get(fill_id))
                if items:
                    news = items[0]
                    evidence = str(news.get("headline") or news.get("summary") or "기업 뉴스 확인")
                    evidence_source = str(news.get("source") or "clickhouse.news_articles")
                    evidence_as_of = news.get("sourceAsOf") or news.get("availableAt")
            elif key == "fundamentals.earnings":
                earnings = earnings_by_symbol.get(symbol)
                earnings = earnings if isinstance(earnings, dict) else {}
                earnings_source_at = _as_datetime(earnings.get("sourceAsOf"))
                if earnings and decision_at is not None and earnings_source_at is not None and earnings_source_at <= decision_at:
                    days = earnings.get("earningsDaysRemaining")
                    evidence = f"다음 실적 {earnings.get('earningsAt') or '일정 확인'}" + (f" · D-{days}" if isinstance(days, int) and days >= 0 else "")
                    evidence_source = str(earnings.get("source") or "yahoo-finance.earnings_dates")
                    evidence_as_of = earnings.get("sourceAsOf")
                else:
                    items = _context_items(fundamentals_by_fill.get(fill_id))
                    if items:
                        fact = items[0]
                        evidence = f"{fact.get('metric') or '재무 지표'} {fact.get('value') if fact.get('value') is not None else '확인'}"
                        evidence_source = str(fact.get("source") or "clickhouse.sec_financial_facts")
                        evidence_as_of = fact.get("sourceAsOf")
            elif key == "market.context":
                metadata = metadata_by_symbol.get(symbol)
                metadata = metadata if isinstance(metadata, dict) else {}
                metadata_at = _as_datetime(metadata.get("sourceAsOf"))
                if metadata_at is not None and (decision_at is None or metadata_at <= decision_at):
                    sector = metadata.get("sector")
                    industry = metadata.get("industry")
                    if sector or industry:
                        evidence = " · ".join(str(value) for value in (sector, industry) if value)
                        evidence_source = str(metadata.get("source") or "stored_symbol_metadata")
                        evidence_as_of = metadata.get("sourceAsOf")

            if evidence is not None:
                item["evidence"] = evidence
                item["source"] = evidence_source
                item["sourceAsOf"] = _iso(evidence_as_of)
            elif not item.get("evidence"):
                item["evidence"] = "확인 여부만 기록됨 · 근거 데이터 부족"
            if marker is not None:
                item["marker"] = marker
            enriched_rows.append(item)
        result[fill_id] = enriched_rows
    return result


def _decision_point(case: dict[str, Any]) -> dict[str, Any]:
    point = case.get("decisionPoint")
    return dict(point) if isinstance(point, dict) else {}


def _check_marker(
    fill_id: str,
    marker_type: str,
    label: str,
    point: dict[str, Any],
    value: float,
    threshold: float,
    reason: str,
    source: str,
) -> dict[str, Any]:
    return {
        "id": f"{fill_id}-{marker_type}",
        "type": marker_type,
        "label": label,
        "relativeDay": int(point.get("relativeDay", -1)),
        "value": value,
        "threshold": threshold,
        "reason": reason,
        "source": source,
        "sourceAsOf": point.get("time"),
    }


def _context_items(value: Any) -> list[dict[str, Any]]:
    source = value if isinstance(value, dict) else {}
    return [dict(item) for item in source.get("items", []) if isinstance(item, dict)]


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


def _s3_error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict) and error.get("Code") is not None:
            return str(error["Code"])
    return str(getattr(exc, "code", ""))


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


def _latest_iso(*values: Any) -> str | None:
    parsed = [_as_datetime(value) for value in values]
    eligible = [value for value in parsed if value is not None]
    return _iso(max(eligible)) if eligible else None


def _missing(source: str, code: str, message: str) -> dict[str, str]:
    return {"source": source, "code": code, "message": message}


def _dedupe_missing(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []; seen: set[tuple[str, str]] = set()
    for item in items:
        key = (str(item.get("source") or "unknown"), str(item.get("code") or "unknown"))
        if key not in seen:
            seen.add(key); result.append(item)
    return result
