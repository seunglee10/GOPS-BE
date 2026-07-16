from __future__ import annotations

import hashlib
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "systems" / "agent-orchestration" / "shared"))
sys.path.insert(0, str(ROOT / "systems" / "market-data" / "shared"))

from gops_agents.contracts import AnalysisReport  # noqa: E402
from gops_agents.orchestrator import AgentOrchestrator  # noqa: E402
from gops_agents.orchestration.coach_snapshot_builder import (  # noqa: E402
    ClickHouseCoachMarketProvider,
    CoachInputSnapshotBuilder,
    S3CoachSnapshotDataProvider,
    _market_day_bounds,
    _enrich_decision_checks,
    _normalize_fill,
    _portfolio_pair,
    _sanitize_trade_case,
)
from gops_agents.runtime.coach_snapshot_archive import (  # noqa: E402
    CoachReportArchive,
    CoachSnapshotArchive,
    CoachSnapshotArchiveError,
    canonical_snapshot_bytes,
    safe_analysis_id,
)
from gops_agents.runtime.envelope import build_request_envelope  # noqa: E402
from gops_agents.runtime.report_store import (  # noqa: E402
    InMemoryReportStore,
    RedisReportStore,
    build_report_store_from_env,
)
from gops_agents.runtime.workers import (  # noqa: E402
    AgentAnalysisWorker,
    analysis_payload_for_envelope,
    should_queue_deep_analysis,
)


FIXED_NOW = datetime(2026, 7, 10, 15, 0, tzinfo=timezone.utc)


class FakeSnapshotDataProvider:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def load(self, user_id: str, *, requested_at: datetime, trading_date):
        self.calls.append({"user_id": user_id, "requested_at": requested_at, "trading_date": trading_date})
        return {
            "fills": [
                {
                    "fillId": "fill-today",
                    "order_id": "order-today",
                    "symbol": "NVDA",
                    "side": "buy",
                    "filledAt": "2026-07-10T14:00:00Z",
                    "execution_payload": {"price": 100, "filled_qty": 2},
                    "sector": "Technology",
                },
                {
                    "fillId": "fill-future",
                    "symbol": "AMD",
                    "side": "buy",
                    "filledAt": "2026-07-10T16:00:00Z",
                    "execution_payload": {"price": 80, "filled_qty": 1},
                },
            ],
            "historicalFills": [
                {
                    "fillId": "fill-history",
                    "symbol": "NVDA",
                    "side": "buy",
                    "filledAt": "2026-06-01T14:00:00Z",
                    "execution_payload": {"price": 90, "filled_qty": 1},
                    "sector": "Technology",
                },
                {
                    "fillId": "not-history",
                    "symbol": "NVDA",
                    "side": "buy",
                    "filledAt": "2026-07-10T13:00:00Z",
                    "execution_payload": {"price": 95, "filled_qty": 1},
                },
            ],
            "portfolioHistory": [
                _portfolio("2026-07-10T13:00:00Z", 100, 300),
                _portfolio("2026-07-10T14:30:00Z", 200, 200),
                _portfolio("2026-07-10T16:00:00Z", 900, 0),
            ],
            "decisionChecks": [
                {
                    "fill_id": "fill-today",
                    "check_key": "chart.rsi",
                    "category": "chart",
                    "label": "RSI",
                    "status": "unchecked",
                    "checked_at": "2026-07-10T13:59:00Z",
                    "source_as_of": "2026-07-09T20:00:00Z",
                    "source": "clickhouse",
                    "evidence": {"summary": "RSI 72", "marker": {"type": "rsi", "value": 72}},
                },
                {
                    "fill_id": "fill-history",
                    "check_key": "news.company",
                    "category": "news",
                    "label": "기업 뉴스",
                    "status": "checked",
                    "checked_at": "2026-06-01T13:58:00Z",
                    "evidence": {},
                },
                {
                    "fill_id": "fill-today",
                    "category": "news",
                    "label": "future",
                    "status": "unchecked",
                    "checked_at": "2026-07-10T16:00:00Z",
                    "evidence": {},
                },
            ],
            "alerts": [
                {"id": "alert-1", "symbol": "NVDA", "type": "price_cross", "status": "active", "proposal_source": "daily_trade", "created_at": "2026-07-10T13:00:00Z"},
                {"id": "alert-future", "symbol": "NVDA", "type": "price_cross", "status": "active", "created_at": "2026-07-10T16:00:00Z"},
            ],
        }


class FakeMarketProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, datetime]] = []

    def trade_case(self, fill: dict, *, requested_at: datetime) -> dict:
        self.calls.append((str(fill["fillId"]), requested_at))
        entry = datetime.fromisoformat(str(fill["filledAt"]).replace("Z", "+00:00"))
        decision_point = _point(entry - timedelta(days=1), -1, 99, rsi=65)
        return {
            "caseId": "provider-controlled-id",
            "featureAsOf": (entry - timedelta(days=1)).isoformat(),
            "featureVintageAsOf": (entry - timedelta(minutes=1)).isoformat(),
            "rsiBand": "neutral",
            "decisionPoint": decision_point,
            "series": [
                decision_point,
                _point(entry, 0, 101, rsi=99),
                _point(entry + timedelta(days=1), 1, 102, rsi=100),
            ],
        }


class FakeContextProvider:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def load(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        fills = kwargs["fills"]
        enrichment = {
            str(fill["fillId"]): {
                "companyName": "NVIDIA" if fill["symbol"] == "NVDA" else fill["symbol"],
                "sector": "Technology",
                "currentPrice": 105 if str(fill["fillId"]) == "fill-today" else None,
            }
            for fill in fills
        }
        return {
            "fillEnrichmentById": enrichment,
            "marketContext": {"metadataBySymbol": {"NVDA": {"sector": "Technology", "source": "test", "sourceAsOf": "2026-07-01T00:00:00Z"}}},
            "newsContext": {"byFillId": {}},
            "fundamentalsContext": {"byFillId": {}},
            "earningsContext": {},
            "ontologyContext": {"temporalScope": "current-only", "historicalSimilarityEligible": False},
            "sourceAsOf": {"market": "2026-07-10T14:59:00Z", "news": None, "fundamentals": None, "earnings": None, "ontology": None},
            "missingData": [],
        }


class FakeCandleProvider:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.calls: list[dict] = []

    def candles(self, symbol, interval, limit, *, from_time=None, to_time=None):
        return list(self.rows)

    def latest_canonical_daily_source(self, where_sql):
        return f"SELECT * FROM fake_canonical_daily WHERE {where_sql}"

    def query_json_each_row(self, query, params):
        self.calls.append(dict(params))
        available_at = datetime.fromisoformat(str(params["availableAt"]).replace("Z", "+00:00"))
        from_time = datetime.fromisoformat(str(params["fromTime"]).replace("Z", "+00:00"))
        to_time = datetime.fromisoformat(str(params["toTime"]).replace("Z", "+00:00"))
        by_time: dict[str, dict] = {}
        for raw in self.rows:
            row = dict(raw)
            timestamp = datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00"))
            inserted_at = datetime.fromisoformat(
                str(row.get("insertedAt") or "1970-01-01T00:00:00Z").replace("Z", "+00:00")
            )
            if timestamp < from_time or timestamp > to_time or inserted_at > available_at:
                continue
            key = timestamp.isoformat()
            previous = by_time.get(key)
            previous_inserted = datetime.fromisoformat(
                str((previous or {}).get("insertedAt") or "1970-01-01T00:00:00Z").replace("Z", "+00:00")
            )
            if previous is None or inserted_at >= previous_inserted:
                by_time[key] = row
        return [by_time[key] for key in sorted(by_time)]


class PreconditionFailed(Exception):
    def __init__(self) -> None:
        self.response = {"Error": {"Code": "PreconditionFailed"}}


class NoSuchKey(Exception):
    def __init__(self) -> None:
        self.response = {"Error": {"Code": "NoSuchKey"}}


class MemoryS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], dict] = {}
        self.put_calls: list[dict] = []

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)
        key = (kwargs["Bucket"], kwargs["Key"])
        if key in self.objects and kwargs.get("IfNoneMatch") == "*":
            raise PreconditionFailed()
        self.objects[key] = {"Body": kwargs["Body"], "Metadata": dict(kwargs.get("Metadata") or {})}

    def get_object(self, **kwargs):
        key = (kwargs["Bucket"], kwargs["Key"])
        if key not in self.objects:
            raise NoSuchKey()
        return dict(self.objects[key])

class RecordingBuilder:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def build(self, **kwargs):
        self.calls.append(kwargs)
        return {"schemaVersion": "coach-input.v1", "request": {"requestedAt": "2026-07-10T15:00:00Z"}, "fills": [], "missingData": []}


class RecordingArchive:
    def __init__(self, *, required: bool = False, error: bool = False) -> None:
        self.required = required
        self.error = error
        self.calls: list[tuple[dict, str]] = []

    def put_once(self, snapshot: dict, analysis_id: str):
        self.calls.append((snapshot, analysis_id))
        if self.error:
            raise CoachSnapshotArchiveError("archive unavailable")
        return None

    def get_existing(self, analysis_id: str, requested_at):
        return None


class BrokenRedis:
    def ping(self):
        raise ConnectionError("redis unavailable")


class WriteFailRedis:
    def ping(self):
        return True

    def get(self, key):
        return None

    def setex(self, key, ttl, value):
        raise ConnectionError("redis write unavailable")


class CoachSnapshotPipelineTests(unittest.TestCase):
    def test_builder_uses_trusted_user_and_caps_every_source_at_request_time(self) -> None:
        data = FakeSnapshotDataProvider()
        market = FakeMarketProvider()
        context = FakeContextProvider()
        builder = CoachInputSnapshotBuilder(data_provider=data, market_provider=market, context_provider=context, now_provider=lambda: FIXED_NOW)

        snapshot = builder.build(
            user_id="trusted-user",
            analysis_id="analysis-1",
            coach_request={"enabled": True, "selectedFillId": "attacker-fill", "tradingDate": "2026-07-11"},
            submitted_at="2026-07-10T16:00:00Z",
        )

        self.assertEqual(len(data.calls), 1)
        self.assertEqual(len(context.calls), 1)
        self.assertEqual(context.calls[0]["current_fill_ids"], {"fill-today"})
        self.assertEqual(data.calls[0], {"user_id": "trusted-user", "requested_at": FIXED_NOW, "trading_date": FIXED_NOW.date()})
        self.assertEqual([item[0] for item in market.calls], ["fill-today", "fill-history"])
        self.assertEqual(snapshot["request"]["selectedFillId"], "fill-today")
        self.assertEqual([item["fillId"] for item in snapshot["fills"]], ["fill-today"])
        self.assertEqual(snapshot["fills"][0]["companyName"], "NVIDIA")
        self.assertEqual(snapshot["fills"][0]["currentPrice"], 105)
        self.assertEqual([item["caseId"] for item in snapshot["chartContext"]["historicalCases"]], ["fill-history"])
        self.assertEqual(snapshot["chartContext"]["historicalCases"][0]["decisionChecks"][0]["checkKey"], "news.company")
        self.assertEqual(snapshot["chartContext"]["currentCase"]["caseId"], "fill-today")
        self.assertEqual(snapshot["chartContext"]["currentCase"]["featureAsOf"], "2026-07-09T14:00:00Z")
        self.assertEqual(snapshot["chartContext"]["currentCase"]["rsiBand"], "neutral")
        self.assertEqual([point["relativeDay"] for point in snapshot["chartContext"]["currentCase"]["series"]], [-1, 0])
        self.assertEqual(len(snapshot["request"]["decisionChecksByFillId"]["fill-today"]), 1)
        decision_check = snapshot["request"]["decisionChecksByFillId"]["fill-today"][0]
        self.assertEqual(decision_check["checkKey"], "chart.rsi")
        self.assertEqual(decision_check["marker"]["relativeDay"], -1)
        self.assertEqual(decision_check["marker"]["value"], 65.0)
        self.assertEqual([item["id"] for item in snapshot["request"]["alerts"]], ["alert-1"])
        self.assertEqual(snapshot["request"]["alerts"][0]["proposal_source"], "daily_trade")
        self.assertEqual(len(snapshot["portfolioBefore"]["history"]), 2)
        self.assertEqual(snapshot["user"]["subjectHash"], hashlib.sha256(b"trusted-user").hexdigest()[:24])
        self.assertNotIn("trusted-user", json.dumps(snapshot, ensure_ascii=False))

    def test_paper_fill_normalization_and_fill_scoped_cost_basis_portfolio_pair(self) -> None:
        fill = _normalize_fill({
            "fill_id": "paper:order-1",
            "order_id": "order-1",
            "symbol": "nvda",
            "side": "buy",
            "filled_at": "2026-07-10T14:00:00Z",
            "fill_price": "190.80",
            "filled_qty": "12",
            "execution_mode": "paper",
            "generation": 3,
        })
        self.assertEqual(fill["fillId"], "paper:order-1")
        self.assertEqual(fill["averageFillPrice"], 190.8)
        self.assertEqual(fill["quantity"], 12.0)
        self.assertEqual(fill["executionMode"], "paper")
        self.assertEqual(fill["generation"], 3)
        kis_fill = _normalize_fill({
            "fill_id": "kis:order-1",
            "symbol": "NVDA",
            "filled_at": "2026-07-10T14:00:00Z",
            "execution_payload": {
                "price": "200.00",
                "px": "198.00",
                "average_fill_price": "190.80",
                "filled_qty": "12",
            },
        })
        self.assertEqual(kis_fill["averageFillPrice"], 190.8)
        fill_at = datetime(2026, 7, 10, 14, tzinfo=timezone.utc)
        rows = [
            {"sourceAsOf": "2026-07-10T13:59:00Z", "positions": [{"symbol": "KIS"}]},
            {"fillId": "paper:order-1", "phase": "before", "sourceAsOf": "2026-07-10T14:00:00Z", "valuationBasis": "cost_basis", "positions": []},
            {"fillId": "paper:order-1", "phase": "after", "sourceAsOf": "2026-07-10T14:00:00Z", "valuationBasis": "cost_basis", "positions": [{"symbol": "NVDA", "costBasisValue": "2289.60"}]},
        ]
        before, after = _portfolio_pair(rows, fill_at, fill_id="paper:order-1", execution_mode="paper")
        self.assertEqual(before["phase"], "before")
        self.assertEqual(after["phase"], "after")
        self.assertEqual(after["valuationBasis"], "cost_basis")
        self.assertEqual(_portfolio_pair(rows[:1], fill_at, fill_id="paper:missing", execution_mode="paper"), ({}, {}))
        self.assertEqual(_portfolio_pair(rows[:1], fill_at, fill_id="kis:missing", execution_mode="kis"), ({}, {}))

    def test_s3_input_archive_is_user_scoped_and_missing_without_a_generated_file(self) -> None:
        client = MemoryS3Client()
        provider = S3CoachSnapshotDataProvider(client=client, bucket="coach-bucket", prefix="coach/input")
        trading_date = datetime(2026, 7, 10, tzinfo=timezone.utc).date()
        requested_at = datetime(2026, 7, 10, 15, tzinfo=timezone.utc)
        key = provider._key("trusted-user", trading_date)
        client.objects[("coach-bucket", key)] = {
            "Body": json.dumps({"sourceAsOf": "2026-07-10T14:59:00Z", "fills": [{"fillId": "fill-1"}]}).encode("utf-8"),
            "Metadata": {},
        }
        loaded = provider.load("trusted-user", requested_at=requested_at, trading_date=trading_date)
        self.assertEqual(loaded["fills"][0]["fillId"], "fill-1")
        self.assertNotIn("missingData", loaded)
        missing = provider.load("other-user", requested_at=requested_at, trading_date=trading_date)
        self.assertEqual(missing["missingData"][0]["code"], "input_archive_missing")

    def test_s3_report_archive_returns_only_the_authenticated_users_latest_report(self) -> None:
        client = MemoryS3Client()
        archive = CoachReportArchive(client=client, bucket="coach-bucket", prefix="coach/reports")
        report = {"contractVersion": "coach-report.v1", "analysisId": "analysis-1", "page1": None}
        with patch.dict(os.environ, {"AI_COACH_SNAPSHOT_ARCHIVE_ENABLED": "true"}, clear=False):
            stored = archive.put_daily(report, user_id="trusted-user", trading_date="2026-07-10")
            self.assertIsNotNone(stored)
            self.assertEqual(archive.get_latest(user_id="trusted-user"), report)
            self.assertIsNone(archive.get_latest(user_id="other-user"))

            # A retried analysis cannot replace the immutable daily report.
            archive.put_daily({**report, "analysisId": "retry"}, user_id="trusted-user", trading_date="2026-07-10")
            self.assertEqual(archive.get_latest(user_id="trusted-user"), report)

    def test_default_trading_date_uses_new_york_day_across_utc_midnight(self) -> None:
        boundary_now = datetime(2026, 7, 11, 2, 0, tzinfo=timezone.utc)

        class BoundaryDataProvider:
            def __init__(self) -> None:
                self.trading_date = None

            def load(self, user_id, *, requested_at, trading_date):
                self.trading_date = trading_date
                return {
                    "fills": [{
                        "fillId": "after-hours-fill",
                        "symbol": "NVDA",
                        "side": "buy",
                        "filledAt": "2026-07-11T01:00:00Z",
                        "execution_payload": {"price": 100, "filled_qty": 1},
                    }],
                    "historicalFills": [],
                    "portfolioHistory": [],
                    "decisionChecks": [],
                    "alerts": [],
                }

        data = BoundaryDataProvider()
        snapshot = CoachInputSnapshotBuilder(
            data_provider=data,
            market_provider=FakeMarketProvider(),
            context_provider=FakeContextProvider(),
            now_provider=lambda: boundary_now,
        ).build(user_id="trusted-user", analysis_id="analysis-boundary", coach_request={"enabled": True})

        self.assertEqual(str(data.trading_date), "2026-07-10")
        self.assertEqual([item["fillId"] for item in snapshot["fills"]], ["after-hours-fill"])

        summer_start, summer_end = _market_day_bounds(datetime(2026, 7, 10).date())
        winter_start, winter_end = _market_day_bounds(datetime(2026, 1, 10).date())
        self.assertEqual((summer_start.hour, summer_end.hour), (4, 4))
        self.assertEqual((winter_start.hour, winter_end.hour), (5, 5))

    def test_clickhouse_daily_similarity_features_use_last_closed_pre_entry_bar(self) -> None:
        start = datetime(2026, 6, 10, tzinfo=timezone.utc)
        rows = []
        for index in range(35):
            close = 100 + index if index < 30 else (50 if index == 30 else 51 + index)
            rows.append({
                "timestamp": (start + timedelta(days=index)).isoformat(),
                "open": close - 1,
                "high": close + 1,
                "low": close - 2,
                "close": close,
                "volume": 1000 + index,
            })
        entry_at = start + timedelta(days=30, hours=14)
        result = ClickHouseCoachMarketProvider(FakeCandleProvider(rows)).trade_case(
            {"fillId": "fill-1", "symbol": "NVDA", "side": "buy", "filledAt": entry_at.isoformat(), "averageFillPrice": 50},
            requested_at=entry_at + timedelta(days=4),
        )

        feature_at = datetime.fromisoformat(str(result["featureAsOf"]).replace("Z", "+00:00"))
        self.assertLess(feature_at.date(), entry_at.date())
        self.assertEqual(feature_at.date(), (entry_at - timedelta(days=1)).date())
        self.assertEqual(result["rsiBand"], "overbought")
        day_zero = next(item for item in result["series"] if item["relativeDay"] == 0)
        self.assertLess(day_zero["rsi"], 70)

    def test_clickhouse_similarity_features_use_entry_vintage_not_later_correction(self) -> None:
        start = datetime(2026, 5, 25, tzinfo=timezone.utc)
        entry_at = datetime(2026, 7, 10, 14, tzinfo=timezone.utc)
        rows = []
        for index in range(46):
            timestamp = start + timedelta(days=index)
            rows.append({
                "timestamp": timestamp.isoformat(),
                "open": 100 + index,
                "high": 102 + index,
                "low": 99 + index,
                "close": 101 + index,
                "volume": 1000 + index,
                "insertedAt": (timestamp + timedelta(hours=23)).isoformat(),
            })
        prior_day = entry_at.replace(hour=0) - timedelta(days=1)
        rows.append({
            "timestamp": prior_day.isoformat(),
            "open": 20,
            "high": 21,
            "low": 19,
            "close": 20,
            "volume": 1,
            "insertedAt": (entry_at + timedelta(days=1)).isoformat(),
            "sourceEventId": "late-correction",
        })
        provider = FakeCandleProvider(rows)

        result = ClickHouseCoachMarketProvider(provider).trade_case(
            {
                "fillId": "kis:order-1",
                "symbol": "NVDA",
                "side": "buy",
                "decisionAt": (entry_at - timedelta(minutes=2)).isoformat(),
                "filledAt": entry_at.isoformat(),
                "averageFillPrice": 146,
            },
            requested_at=entry_at + timedelta(days=3),
        )

        self.assertEqual(len(provider.calls), 2)
        self.assertEqual(provider.calls[0]["availableAt"], (entry_at + timedelta(days=3)).isoformat().replace("+00:00", "Z"))
        self.assertEqual(provider.calls[1]["availableAt"], (entry_at - timedelta(minutes=2)).isoformat().replace("+00:00", "Z"))
        self.assertEqual(result["featureVintageAsOf"], "2026-07-10T13:58:00Z")
        corrected = next(point for point in result["series"] if point["time"].startswith("2026-07-09"))
        self.assertEqual(corrected["close"], 20.0)

    def test_delayed_fill_marker_uses_order_time_vintage_not_pre_fill_display_bar(self) -> None:
        fill = {
            "fillId": "kis:limit-order",
            "symbol": "NVDA",
            "decisionAt": "2026-07-05T14:00:00Z",
            "filledAt": "2026-07-10T14:00:00Z",
        }
        case = _sanitize_trade_case({
            "featureVintageAsOf": "2026-07-05T14:00:00Z",
            "featureAsOf": "2026-07-04T00:00:00Z",
            "rsiBand": "neutral",
            "decisionPoint": {
                "relativeDay": -6,
                "time": "2026-07-04T00:00:00Z",
                "close": 100,
                "rsi": 45,
            },
            "series": [
                {"relativeDay": -1, "time": "2026-07-09T00:00:00Z", "close": 110, "rsi": 75},
                {"relativeDay": 0, "time": "2026-07-10T00:00:00Z", "close": 111, "rsi": 78},
            ],
        }, fill, requested_at=datetime(2026, 7, 11, tzinfo=timezone.utc))
        checks = _enrich_decision_checks(
            {"kis:limit-order": [{
                "checkKey": "chart.rsi",
                "category": "chart",
                "label": "RSI",
                "status": "unchecked",
            }]},
            fills=[fill],
            cases_by_fill={"kis:limit-order": case},
            news_context={},
            fundamentals_context={},
            earnings_context={},
            market_context={},
        )

        marker = checks["kis:limit-order"][0]["marker"]
        self.assertEqual(marker["value"], 45.0)
        self.assertEqual(marker["label"], "RSI 확인 누락")
        self.assertEqual(marker["sourceAsOf"], "2026-07-04T00:00:00Z")

    def test_archive_is_canonical_encrypted_and_immutable(self) -> None:
        client = MemoryS3Client()
        archive = CoachSnapshotArchive(client=client, bucket="coach-bucket", prefix="coach")
        snapshot = {
            "schemaVersion": "coach-input.v1",
            "request": {"analysisId": "analysis-1", "requestedAt": "2026-07-10T15:00:00Z"},
            "fills": [{"symbol": "NVDA"}],
        }
        with patch.dict(os.environ, {"AI_COACH_SNAPSHOT_ARCHIVE_ENABLED": "true", "AI_COACH_SNAPSHOT_ARCHIVE_REQUIRED": "false"}, clear=False):
            first = archive.put_once(snapshot, "analysis-1")
            second = archive.put_once({"fills": [{"symbol": "NVDA"}], "request": {"requestedAt": "2026-07-10T15:00:00Z"}}, "analysis-1")
            different_retry = archive.put_once({**snapshot, "fills": [{"symbol": "AMD"}]}, "analysis-1")
            reused = archive.get_existing("analysis-1", "2026-07-10T15:00:00Z")

        assert first is not None and second is not None and different_retry is not None
        self.assertEqual(first["status"], "stored")
        self.assertEqual(second["status"], "already_exists_unverified")
        self.assertEqual(different_retry["status"], "already_exists_unverified")
        self.assertIsNone(second["sha256"])
        self.assertEqual(client.put_calls[0]["IfNoneMatch"], "*")
        self.assertEqual(client.put_calls[0]["ServerSideEncryption"], "AES256")
        self.assertEqual(client.put_calls[0]["Body"], canonical_snapshot_bytes(snapshot))
        stored = next(iter(client.objects.values()))
        self.assertEqual(stored["Body"], canonical_snapshot_bytes(snapshot))
        assert reused is not None
        self.assertEqual(reused[0], snapshot)
        self.assertEqual(reused[1]["status"], "already_exists_reused")
        self.assertEqual(reused[1]["sha256"], first["sha256"])

    def test_required_archive_cannot_be_disabled_and_ids_do_not_sanitize_to_collisions(self) -> None:
        archive = CoachSnapshotArchive(client=MemoryS3Client(), bucket="coach-bucket")
        with patch.dict(os.environ, {"AI_COACH_SNAPSHOT_ARCHIVE_ENABLED": "false", "AI_COACH_SNAPSHOT_ARCHIVE_REQUIRED": "true"}, clear=False):
            with self.assertRaisesRegex(CoachSnapshotArchiveError, "required but disabled"):
                archive.put_once({"request": {}}, "analysis-1")
        self.assertNotEqual(safe_analysis_id("analysis/1"), safe_analysis_id("analysis1"))

    def test_worker_strips_client_snapshot_and_builds_once_from_envelope_owner(self) -> None:
        builder = RecordingBuilder()
        archive = RecordingArchive()
        worker = AgentAnalysisWorker(
            store=InMemoryReportStore(),
            orchestrator=object(),
            coach_snapshot_builder=builder,
            coach_snapshot_archive=archive,
        )
        envelope = build_request_envelope(
            {
                "symbol": "NVDA",
                "coachRequest": {"enabled": True},
                "coachInputSnapshot": {"user": {"subjectHash": "attacker"}, "fills": [{"fillId": "fake"}]},
            },
            user_id="trusted-user",
            request_id="analysis-worker",
        )
        payload = analysis_payload_for_envelope(envelope)

        trace = worker._prepare_coach_snapshot(envelope, payload)

        self.assertEqual(len(builder.calls), 1)
        self.assertEqual(builder.calls[0]["user_id"], "trusted-user")
        self.assertEqual(len(archive.calls), 1)
        self.assertEqual(payload["coachInputSnapshot"]["fills"], [])
        self.assertNotIn("attacker", json.dumps(payload))
        self.assertEqual(trace["archiveStatus"], "disabled")

    def test_worker_end_to_end_attaches_archived_snapshot_to_v2_report_once(self) -> None:
        class StoredArchive(RecordingArchive):
            def put_once(self, snapshot: dict, analysis_id: str):
                self.calls.append((snapshot, analysis_id))
                return {"status": "stored", "key": "coach/v1/date=2026-07-10/analysis-e2e.json", "sha256": "a" * 64}

        store = InMemoryReportStore()
        builder = RecordingBuilder()
        archive = StoredArchive()
        worker = AgentAnalysisWorker(
            store=store,
            orchestrator=AgentOrchestrator(store=store),
            coach_snapshot_builder=builder,
            coach_snapshot_archive=archive,
        )
        envelope = build_request_envelope(
            {"symbol": "NVDA", "intent": "coach", "coachRequest": {"enabled": True}},
            user_id="trusted-user",
            request_id="analysis-e2e",
        )

        with patch("gops_agents.runtime.workers.publish_agent_outputs"):
            report = worker.process_envelope(envelope)

        self.assertEqual(len(builder.calls), 1)
        self.assertEqual(len(archive.calls), 1)
        self.assertIsNotNone(report.coachReport)
        self.assertEqual(report.coachReport["contractVersion"], "coach-report.v2")
        self.assertEqual(report.coachReport["snapshotRef"], "coach/v1/date=2026-07-10/analysis-e2e.json")
        self.assertEqual(report.coachReport["snapshotDigest"], "a" * 64)
        self.assertEqual(report.agentTrace["coachSnapshot"]["archiveStatus"], "stored")

    def test_worker_archive_failure_is_explicit_optional_or_fatal_required(self) -> None:
        envelope = build_request_envelope(
            {"symbol": "NVDA", "coachRequest": {"enabled": True}},
            user_id="trusted-user",
            request_id="analysis-worker",
        )
        optional_worker = AgentAnalysisWorker(
            store=InMemoryReportStore(), orchestrator=object(), coach_snapshot_builder=RecordingBuilder(),
            coach_snapshot_archive=RecordingArchive(required=False, error=True),
        )
        optional_payload = analysis_payload_for_envelope(envelope)
        trace = optional_worker._prepare_coach_snapshot(envelope, optional_payload)
        self.assertEqual(trace["archiveStatus"], "failed_optional")
        self.assertEqual(optional_payload["coachInputSnapshot"]["missingData"][0]["code"], "archive_failed")

        required_worker = AgentAnalysisWorker(
            store=InMemoryReportStore(), orchestrator=object(), coach_snapshot_builder=RecordingBuilder(),
            coach_snapshot_archive=RecordingArchive(required=True, error=True),
        )
        with self.assertRaises(CoachSnapshotArchiveError):
            required_worker._prepare_coach_snapshot(envelope, analysis_payload_for_envelope(envelope))

    def test_worker_reuses_existing_immutable_snapshot_after_conditional_put_conflict(self) -> None:
        class ExistingArchive(RecordingArchive):
            def put_once(self, snapshot: dict, analysis_id: str):
                self.calls.append((snapshot, analysis_id))
                return {
                    "status": "already_exists_unverified",
                    "key": "coach/v1/date=2026-07-10/analysis-retry.json",
                    "sha256": None,
                }

            def get_existing(self, analysis_id: str, requested_at):
                return ({
                    "schemaVersion": "coach-input.v1",
                    "request": {"analysisId": analysis_id, "requestedAt": requested_at},
                    "fills": [{"fillId": "first-immutable-fill"}],
                    "missingData": [],
                }, {
                    "status": "already_exists_reused",
                    "key": "coach/v1/date=2026-07-10/analysis-retry.json",
                    "sha256": "b" * 64,
                })

        envelope = build_request_envelope(
            {"symbol": "NVDA", "coachRequest": {"enabled": True}},
            user_id="trusted-user",
            request_id="analysis-retry",
        )
        payload = analysis_payload_for_envelope(envelope)
        worker = AgentAnalysisWorker(
            store=InMemoryReportStore(),
            orchestrator=object(),
            coach_snapshot_builder=RecordingBuilder(),
            coach_snapshot_archive=ExistingArchive(),
        )

        trace = worker._prepare_coach_snapshot(envelope, payload)

        self.assertEqual(trace["archiveStatus"], "already_exists_reused")
        self.assertTrue(trace["built"])
        self.assertEqual(trace["sha256"], "b" * 64)
        self.assertEqual(payload["coachInputSnapshot"]["fills"][0]["fillId"], "first-immutable-fill")
        self.assertEqual(len(worker.coach_snapshot_builder.calls), 1)

    def test_coach_request_never_rebuilds_snapshot_in_deep_worker(self) -> None:
        envelope = build_request_envelope(
            {"symbol": "NVDA", "coachRequest": {"enabled": True}},
            user_id="trusted-user",
            request_id="analysis-worker",
        )
        report = AnalysisReport(
            analysisId="analysis-worker", symbol="NVDA", intent="coach", status="completed",
            createdAt="2026-07-10T15:00:00Z", summary="done", rationale="done",
        )
        with patch.dict(os.environ, {"AGENT_DEEP_ANALYSIS_ENABLED": "true"}, clear=False):
            self.assertFalse(should_queue_deep_analysis(envelope, report))

    def test_explicit_redis_report_store_pings_and_rethrows_write_failures(self) -> None:
        with self.assertRaises(ConnectionError):
            RedisReportStore(BrokenRedis(), strict=True, verify_connection=True)

        store = RedisReportStore(WriteFailRedis(), strict=True)
        report = AnalysisReport(
            analysisId="analysis-1", symbol="NVDA", intent="coach", status="completed",
            createdAt="2026-07-10T15:00:00Z", summary="done", rationale="done",
        )
        with self.assertRaises(ConnectionError):
            store.save(report)

        with patch("gops_agents.runtime.report_store.RedisReportStore") as constructor:
            constructor.return_value = InMemoryReportStore()
            with patch.dict(os.environ, {"AGENT_REPORT_STORE_BACKEND": "redis"}, clear=False):
                build_report_store_from_env()
            constructor.assert_called_once_with(strict=True, verify_connection=True)


def _portfolio(as_of: str, nvda_value: float, cash: float) -> dict:
    return {
        "sourceAsOf": as_of,
        "account": {"cashForeign": cash, "totalValueForeign": 1000},
        "positions": [{"symbol": "NVDA", "sector": "Technology", "marketValueForeign": nvda_value}],
    }


def _point(observed_at: datetime, relative_day: int, close: float, *, rsi: float) -> dict:
    return {
        "time": observed_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "relativeDay": relative_day,
        "open": close - 1,
        "high": close + 2,
        "low": close - 2,
        "close": close,
        "volume": 1000,
        "relativeVolume": 1.0,
        "rsi": rsi,
        "macd": 1.0,
        "signal": 0.5,
    }


if __name__ == "__main__":
    unittest.main()
