from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[3]
SIMULATOR_ROOT = REPO_ROOT / "systems" / "simulator"
if str(SIMULATOR_ROOT) not in sys.path:
    sys.path.insert(0, str(SIMULATOR_ROOT))

from gops_simul.dataset import (
    DATASET_END,
    DATASET_ID,
    DATASET_START,
    FEED_SEGMENTS,
    REPLAY_SYMBOLS,
    in_half_open_window,
)
from gops_simul.tick_replay import InMemoryReplayEventSource, ReplayController, ReplayEvent
from gops_simul.tools.import_alpaca import fetch_kind


class ManualClock:
    def __init__(self) -> None:
        self.value = 1_000.0

    def __call__(self) -> float:
        return self.value


def quote(sequence: int, seconds: float, symbol: str, bid: float, ask: float) -> ReplayEvent:
    timestamp = DATASET_START + timedelta(seconds=seconds)
    return ReplayEvent(
        sequence=sequence,
        timestamp=timestamp,
        feed="sip",
        payload={"T": "q", "S": symbol, "bp": bid, "ap": ask, "bs": 10, "as": 10, "t": timestamp.isoformat()},
    )


def trade(sequence: int, seconds: float, symbol: str, price: float) -> ReplayEvent:
    timestamp = DATASET_START + timedelta(seconds=seconds)
    return ReplayEvent(
        sequence=sequence,
        timestamp=timestamp,
        feed="sip",
        payload={"T": "t", "S": symbol, "p": price, "s": 1, "t": timestamp.isoformat()},
    )


class DatasetContractTests(unittest.TestCase):
    def test_dataset_is_the_fixed_kst_day_and_twenty_companies(self):
        self.assertEqual(DATASET_ID, "sp500-top20-20260715-kst-v1")
        self.assertEqual(DATASET_START, datetime(2026, 7, 14, 15, 0, tzinfo=UTC))
        self.assertEqual(DATASET_END, datetime(2026, 7, 15, 15, 0, tzinfo=UTC))
        self.assertEqual(len(REPLAY_SYMBOLS), 21)
        self.assertEqual(REPLAY_SYMBOLS[:4], ("NVDA", "MSFT", "AAPL", "AMZN"))
        self.assertEqual(REPLAY_SYMBOLS[-2:], ("HD", "JNJ"))
        self.assertEqual([segment.feed for segment in FEED_SEGMENTS], ["sip", "boats", "sip"])

    def test_half_open_filter_rejects_the_exact_end_boundary(self):
        self.assertTrue(in_half_open_window(DATASET_START, DATASET_START, DATASET_END))
        self.assertTrue(in_half_open_window(DATASET_END - timedelta(microseconds=1), DATASET_START, DATASET_END))
        self.assertFalse(in_half_open_window(DATASET_END, DATASET_START, DATASET_END))

    def test_importer_follows_every_page_and_filters_end_boundary(self):
        pages = {
            None: {
                "trades": {"NVDA": [{"t": "2026-07-14T15:00:00Z", "p": 100}]},
                "next_page_token": "p2",
            },
            "p2": {
                "trades": {"NVDA": [{"t": "2026-07-14T15:00:01Z", "p": 101}]},
                "next_page_token": "p3",
            },
            "p3": {
                "trades": {"NVDA": [{"t": "2026-07-15T00:00:00Z", "p": 102}]},
                "next_page_token": None,
            },
        }

        def fake_fetch(url, _headers):
            token = None
            if "page_token=p2" in url:
                token = "p2"
            elif "page_token=p3" in url:
                token = "p3"
            return pages[token]

        with patch("gops_simul.tools.import_alpaca.fetch_json", side_effect=fake_fetch):
            rows = fetch_kind(
                kind="trades",
                base_url="https://example.test",
                symbol="NVDA",
                feed="sip",
                start="2026-07-14T15:00:00Z",
                end="2026-07-15T00:00:00Z",
                limit=1,
                headers={},
            )

        self.assertEqual([row["p"] for row in rows], [100, 101])


class ReplayControllerTests(unittest.TestCase):
    def setUp(self):
        self.clock = ManualClock()
        self.source = InMemoryReplayEventSource(
            [
                quote(1, 1, "NVDA", 99.0, 100.0),
                trade(2, 1, "NVDA", 99.5),
                quote(3, 3, "NVDA", 101.0, 102.0),
                trade(4, 3, "NVDA", 101.5),
            ]
        )
        self.controller = ReplayController(self.source, clock=self.clock, default_speed=1)

    def test_entering_simulation_is_ready_and_resume_advances_source_time(self):
        ready = self.controller.set_mode("simulation")
        self.assertEqual(ready["state"], "ready")
        self.assertEqual(ready["virtualTime"], "2026-07-15T00:00:00+09:00")
        self.assertEqual(ready["requestedSpeed"], 1)

        self.controller.resume()
        self.clock.value += 1.1
        running = self.controller.status()

        self.assertEqual(running["processedEventCount"], 2)
        self.assertEqual(self.controller.latest_quote("NVDA"), {"bid": 99.0, "ask": 100.0})
        self.assertEqual(self.controller.emitted_events()[0].payload["t"], "2026-07-14T15:00:01+00:00")

    def test_speed_can_change_mid_run_without_dropping_events(self):
        self.controller.set_mode("simulation")
        self.controller.resume()
        self.controller.set_speed(5)
        self.clock.value += 1

        completed = self.controller.status()

        self.assertEqual(completed["requestedSpeed"], 5)
        self.assertEqual(completed["processedEventCount"], 4)
        self.assertEqual([event.sequence for event in self.controller.emitted_events()], [1, 2, 3, 4])
        with self.assertRaises(ValueError):
            self.controller.set_speed(2)

    def test_market_and_limit_orders_fill_at_the_replayed_quote(self):
        self.controller.set_mode("simulation")
        self.controller.resume()
        self.clock.value += 1.1
        self.controller.status()

        market = self.controller.submit_order(
            user_id="user-1",
            symbol="NVDA",
            side="buy",
            quantity=2,
            order_type="market",
            idempotency_key="market-1",
        )
        resting = self.controller.submit_order(
            user_id="user-1",
            symbol="NVDA",
            side="sell",
            quantity=1,
            order_type="limit",
            limit_price=101,
            idempotency_key="limit-1",
        )

        self.assertEqual(market["order"]["status"], "filled")
        self.assertEqual(market["order"]["filled_price"], 100.0)
        self.assertEqual(resting["order"]["status"], "accepted")

        self.clock.value += 2
        self.controller.status()
        account = self.controller.account("user-1")
        filled_limit = next(order for order in account["orders"] if order["order_id"] == resting["order"]["order_id"])
        self.assertEqual(filled_limit["status"], "filled")
        self.assertEqual(filled_limit["filled_price"], 101.0)
        self.assertEqual(account["account"]["cashForeign"], 99_901.0)

    def test_market_order_is_blocked_while_paused_and_restart_resets_account(self):
        self.controller.set_mode("simulation")
        with self.assertRaisesRegex(ValueError, "SIM_NOT_RUNNING"):
            self.controller.submit_order(
                user_id="user-1",
                symbol="NVDA",
                side="buy",
                quantity=1,
                order_type="market",
                idempotency_key="blocked",
            )

        self.controller.resume()
        self.clock.value += 1.1
        self.controller.status()
        self.controller.submit_order(
            user_id="user-1",
            symbol="NVDA",
            side="buy",
            quantity=1,
            order_type="market",
            idempotency_key="filled",
        )
        old_run = self.controller.status()["runId"]

        restarted = self.controller.restart()

        self.assertNotEqual(restarted["runId"], old_run)
        self.assertEqual(restarted["state"], "ready")
        self.assertEqual(self.controller.account("user-1")["account"]["cashForeign"], 100_000.0)
        self.assertEqual(self.controller.account("user-1")["orders"], [])


if __name__ == "__main__":
    unittest.main()
