from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "systems/api-server/pods/api-server/gops-backend"))
sys.path.insert(0, str(REPO_ROOT / "systems/market-data/shared"))

from fastapi import HTTPException  # noqa: E402

from app.market_data.backfill.service import BackfillService  # noqa: E402


class FakeBackfillStore:
    def __init__(self) -> None:
        self.created: list[dict[str, object]] = []

    def latest_status(self, symbol: str, interval: str):
        return None

    def create_request(self, symbol, interval, start=None, end=None, force=False):
        record = {
            "requestId": f"backfill:{symbol}:{interval}:range",
            "symbol": symbol,
            "interval": interval,
            "range": {"start": start, "end": end},
            "status": "queued",
            "requestedAt": "2026-06-30T00:00:00.000Z",
            "updatedAt": "2026-06-30T00:00:00.000Z",
        }
        self.created.append(record)
        return record, False


class RangeBackfillServiceTests(unittest.TestCase):
    def test_request_backfill_requires_explicit_range(self) -> None:
        service = BackfillService(store=FakeBackfillStore())

        with self.assertRaises(HTTPException) as context:
            service.request_backfill("AAPL", "1m")

        self.assertEqual(context.exception.status_code, 400)

    def test_request_backfill_queues_source_interval_range(self) -> None:
        store = FakeBackfillStore()
        service = BackfillService(store=store)

        payload = service.request_backfill(
            "AAPL",
            "5m",
            start="2026-06-29T13:30:00Z",
            end="2026-06-29T14:30:00Z",
        )

        self.assertEqual(payload["status"], "queued")
        self.assertEqual(payload["interval"], "5m")
        self.assertEqual(payload["sourceInterval"], "1m")
        self.assertEqual(store.created[0]["interval"], "1m")

    def test_weekly_backfill_queues_daily_source_interval_range(self) -> None:
        store = FakeBackfillStore()
        service = BackfillService(store=store)

        payload = service.request_backfill(
            "AAPL",
            "1W",
            start="2026-01-01T00:00:00Z",
            end="2026-06-30T00:00:00Z",
        )

        self.assertEqual(payload["interval"], "1W")
        self.assertEqual(payload["sourceInterval"], "1D")
        self.assertEqual(store.created[0]["interval"], "1D")

    def test_monthly_backfill_queues_daily_source_interval_range(self) -> None:
        store = FakeBackfillStore()
        service = BackfillService(store=store)

        payload = service.request_backfill(
            "AAPL",
            "1M",
            start="2025-01-01T00:00:00Z",
            end="2026-06-30T00:00:00Z",
        )

        self.assertEqual(payload["interval"], "1M")
        self.assertEqual(payload["sourceInterval"], "1D")
        self.assertEqual(store.created[0]["interval"], "1D")

    def test_snapshot_metadata_has_no_legacy_targets(self) -> None:
        service = BackfillService(store=FakeBackfillStore())
        metadata = service.snapshot_metadata("AAPL", "1m", {
            "requestedLimit": 1,
            "returnedCount": 1,
            "storedCandleCount": 1,
            "availableFrom": "2026-06-29T13:59:00.000Z",
            "availableTo": "2026-06-29T13:59:00.000Z",
            "requestedRange": {"before": "2026-06-29T14:00:00.000Z"},
            "candles": [{
                "timestamp": "2026-06-29T13:59:00.000Z",
                "open": 1,
                "high": 2,
                "low": 1,
                "close": 2,
                "volume": 100,
            }],
        })

        self.assertEqual(metadata["dataStatus"], "ready")
        self.assertEqual(metadata["repairStatus"], "gapfill_required")
        self.assertTrue(metadata["coverage"]["renderable"])
        self.assertEqual(metadata["coverage"]["renderabilityReasonCode"], "insufficient_source_bars")
        for field in (f"target{'Stored'}Count", f"target{'Range'}From"):
            self.assertNotIn(field, metadata["coverage"])

    def test_snapshot_metadata_treats_complete_explicit_daily_range_as_repaired(self) -> None:
        service = BackfillService(store=FakeBackfillStore())
        candles = [
            {"timestamp": "2024-06-03T00:00:00.000Z"},
            {"timestamp": "2024-06-04T00:00:00.000Z"},
            {"timestamp": "2024-06-05T00:00:00.000Z"},
            {"timestamp": "2024-06-06T00:00:00.000Z"},
            {"timestamp": "2024-06-07T00:00:00.000Z"},
        ]

        metadata = service.snapshot_metadata("ABT", "1D", {
            "requestedLimit": 10,
            "returnedCount": len(candles),
            "storedCandleCount": len(candles),
            "availableFrom": "2024-06-03T00:00:00.000Z",
            "availableTo": "2024-06-07T00:00:00.000Z",
            "requestedRange": {
                "from": "2024-06-03T00:00:00.000Z",
                "to": "2024-06-08T00:00:00.000Z",
            },
            "candles": candles,
        })

        self.assertEqual(metadata["dataStatus"], "ready")
        self.assertEqual(metadata["repairStatus"], "none")
        self.assertEqual(metadata["coverage"]["expectedRequestedRangeBars"], 5)
        self.assertIsNone(metadata["coverage"]["renderabilityReasonCode"])

    def test_snapshot_metadata_counts_complete_explicit_weekly_range_from_daily_source(self) -> None:
        service = BackfillService(store=FakeBackfillStore())
        candles = [
            {"timestamp": "2024-01-01T00:00:00.000Z"},
            {"timestamp": "2024-01-08T00:00:00.000Z"},
            {"timestamp": "2024-01-15T00:00:00.000Z"},
        ]

        metadata = service.snapshot_metadata("ABT", "1W", {
            "requestedLimit": 10,
            "returnedCount": len(candles),
            "storedCandleCount": 15,
            "availableFrom": "2024-01-02T00:00:00.000Z",
            "availableTo": "2024-01-19T00:00:00.000Z",
            "requestedRange": {
                "from": "2024-01-01T00:00:00.000Z",
                "to": "2024-01-20T00:00:00.000Z",
            },
            "candles": candles,
        })

        self.assertEqual(metadata["dataStatus"], "ready")
        self.assertEqual(metadata["repairStatus"], "none")
        self.assertEqual(metadata["coverage"]["sourceInterval"], "1D")
        self.assertEqual(metadata["coverage"]["expectedRequestedRangeBars"], 3)

    def test_snapshot_metadata_respects_small_requested_limit(self) -> None:
        service = BackfillService(store=FakeBackfillStore())
        candles = [
            {"timestamp": "2026-05-11T00:00:00.000Z"},
            {"timestamp": "2026-05-18T00:00:00.000Z"},
            {"timestamp": "2026-05-25T00:00:00.000Z"},
            {"timestamp": "2026-06-01T00:00:00.000Z"},
            {"timestamp": "2026-06-08T00:00:00.000Z"},
            {"timestamp": "2026-06-15T00:00:00.000Z"},
            {"timestamp": "2026-06-22T00:00:00.000Z"},
            {"timestamp": "2026-06-29T00:00:00.000Z"},
        ]

        metadata = service.snapshot_metadata("AAPL", "1W", {
            "requestedLimit": len(candles),
            "returnedCount": len(candles),
            "storedCandleCount": 2637,
            "availableFrom": "2016-01-04T00:00:00.000Z",
            "availableTo": "2026-06-30T00:00:00.000Z",
            "candles": candles,
        })

        self.assertEqual(metadata["dataStatus"], "ready")
        self.assertEqual(metadata["repairStatus"], "none")
        self.assertEqual(metadata["coverage"]["minimumReturnedCount"], len(candles))
        self.assertIsNone(metadata["coverage"]["renderabilityReasonCode"])

    def test_snapshot_metadata_stops_gapfill_at_no_data_boundary(self) -> None:
        service = BackfillService(store=FakeBackfillStore())

        metadata = service.snapshot_metadata("AAPL", "1D", {
            "requestedLimit": 5,
            "returnedCount": 0,
            "storedCandleCount": 2637,
            "availableFrom": "2016-01-04T00:00:00.000Z",
            "availableTo": "2026-06-30T00:00:00.000Z",
            "noDataBefore": "2016-01-04T00:00:00.000Z",
            "requestedRange": {
                "before": "2016-01-04T00:00:00.000Z",
            },
            "candles": [],
        })

        self.assertEqual(metadata["dataStatus"], "empty")
        self.assertEqual(metadata["repairStatus"], "none")
        self.assertFalse(metadata["canBackfill"])
        self.assertEqual(metadata["coverage"]["reasonCode"], "no_data_boundary_reached")
        self.assertEqual(metadata["coverage"]["noDataBefore"], "2016-01-04T00:00:00.000Z")


if __name__ == "__main__":
    unittest.main()
