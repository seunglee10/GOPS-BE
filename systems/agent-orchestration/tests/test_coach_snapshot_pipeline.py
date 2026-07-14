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
    _market_day_bounds,
)
from gops_agents.runtime.coach_snapshot_archive import (  # noqa: E402
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
                    "category": "chart",
                    "label": "RSI",
                    "status": "unchecked",
                    "checked_at": "2026-07-10T13:59:00Z",
                    "source_as_of": "2026-07-09T20:00:00Z",
                    "source": "clickhouse",
                    "evidence": {"summary": "RSI 72", "marker": {"type": "rsi", "value": 72}},
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
                {"id": "alert-1", "symbol": "NVDA", "type": "price_cross", "status": "active", "created_at": "2026-07-10T13:00:00Z"},
                {"id": "alert-future", "symbol": "NVDA", "type": "price_cross", "status": "active", "created_at": "2026-07-10T16:00:00Z"},
            ],
        }


class FakeMarketProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, datetime]] = []

    def trade_case(self, fill: dict, *, requested_at: datetime) -> dict:
        self.calls.append((str(fill["fillId"]), requested_at))
        entry = datetime.fromisoformat(str(fill["filledAt"]).replace("Z", "+00:00"))
        return {
            "caseId": "provider-controlled-id",
            "featureAsOf": entry.isoformat(),
            "rsiBand": "overbought",
            "series": [
                _point(entry - timedelta(days=1), -1, 99, rsi=65),
                _point(entry, 0, 101, rsi=99),
                _point(entry + timedelta(days=1), 1, 102, rsi=100),
            ],
        }


class FakeCandleProvider:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows

    def candles(self, symbol, interval, limit, *, from_time=None, to_time=None):
        return list(self.rows)


class PreconditionFailed(Exception):
    def __init__(self) -> None:
        self.response = {"Error": {"Code": "PreconditionFailed"}}


class MemoryS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], dict] = {}
        self.put_calls: list[dict] = []

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)
        key = (kwargs["Bucket"], kwargs["Key"])
        if key in self.objects:
            raise PreconditionFailed()
        self.objects[key] = {"Body": kwargs["Body"], "Metadata": dict(kwargs.get("Metadata") or {})}

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
        builder = CoachInputSnapshotBuilder(data_provider=data, market_provider=market, now_provider=lambda: FIXED_NOW)

        snapshot = builder.build(
            user_id="trusted-user",
            analysis_id="analysis-1",
            coach_request={"enabled": True, "selectedFillId": "attacker-fill", "tradingDate": "2026-07-11"},
            submitted_at="2026-07-10T16:00:00Z",
        )

        self.assertEqual(len(data.calls), 1)
        self.assertEqual(data.calls[0], {"user_id": "trusted-user", "requested_at": FIXED_NOW, "trading_date": FIXED_NOW.date()})
        self.assertEqual([item[0] for item in market.calls], ["fill-today", "fill-history"])
        self.assertEqual(snapshot["request"]["selectedFillId"], "fill-today")
        self.assertEqual([item["fillId"] for item in snapshot["fills"]], ["fill-today"])
        self.assertEqual([item["caseId"] for item in snapshot["chartContext"]["historicalCases"]], ["fill-history"])
        self.assertEqual(snapshot["chartContext"]["currentCase"]["caseId"], "fill-today")
        self.assertEqual(snapshot["chartContext"]["currentCase"]["featureAsOf"], "2026-07-09T14:00:00Z")
        self.assertEqual(snapshot["chartContext"]["currentCase"]["rsiBand"], "neutral")
        self.assertEqual([point["relativeDay"] for point in snapshot["chartContext"]["currentCase"]["series"]], [-1, 0])
        self.assertEqual(len(snapshot["request"]["decisionChecksByFillId"]["fill-today"]), 1)
        self.assertEqual([item["id"] for item in snapshot["request"]["alerts"]], ["alert-1"])
        self.assertEqual(len(snapshot["portfolioBefore"]["history"]), 2)
        self.assertEqual(snapshot["user"]["subjectHash"], hashlib.sha256(b"trusted-user").hexdigest()[:24])
        self.assertNotIn("trusted-user", json.dumps(snapshot, ensure_ascii=False))

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

    def test_archive_is_canonical_encrypted_immutable_and_write_only(self) -> None:
        client = MemoryS3Client()
        archive = CoachSnapshotArchive(client=client, bucket="coach-bucket", prefix="coach")
        snapshot = {"request": {"requestedAt": "2026-07-10T15:00:00Z"}, "fills": [{"symbol": "NVDA"}]}
        with patch.dict(os.environ, {"AI_COACH_SNAPSHOT_ARCHIVE_ENABLED": "true", "AI_COACH_SNAPSHOT_ARCHIVE_REQUIRED": "false"}, clear=False):
            first = archive.put_once(snapshot, "analysis-1")
            second = archive.put_once({"fills": [{"symbol": "NVDA"}], "request": {"requestedAt": "2026-07-10T15:00:00Z"}}, "analysis-1")
            different_retry = archive.put_once({**snapshot, "fills": [{"symbol": "AMD"}]}, "analysis-1")

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

    def test_worker_marks_write_only_retry_digest_as_unverified(self) -> None:
        class ExistingArchive(RecordingArchive):
            def put_once(self, snapshot: dict, analysis_id: str):
                self.calls.append((snapshot, analysis_id))
                return {
                    "status": "already_exists_unverified",
                    "key": "coach/v1/date=2026-07-10/analysis-retry.json",
                    "sha256": None,
                }

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

        self.assertEqual(trace["archiveStatus"], "already_exists_unverified")
        self.assertIsNone(trace["sha256"])
        self.assertEqual(
            payload["coachInputSnapshot"]["missingData"][-1]["code"],
            "existing_object_digest_unverified",
        )

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
