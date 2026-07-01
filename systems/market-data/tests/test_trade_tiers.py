import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "systems" / "market-data" / "shared"))

from alfaka.alpaca.trade_tiers import resolve_trade_subscription_plan  # noqa: E402


class TradeTierTest(unittest.TestCase):
    def test_active_watchlist_and_hot_symbols_are_ordered_by_priority(self):
        plan = resolve_trade_subscription_plan(
            active_symbols=["MSFT", "AAPL"],
            watchlist_symbols=["AAPL", "JPM"],
            hot_symbols=["TSLA", "MSFT"],
        )

        self.assertEqual(plan["symbols"], ["MSFT", "AAPL", "JPM", "TSLA"])
        self.assertEqual(plan["tiersBySymbol"]["MSFT"], ["active", "hot"])
        self.assertEqual(plan["tiersBySymbol"]["AAPL"], ["active", "watchlist"])
        self.assertEqual(plan["tiersBySymbol"]["JPM"], ["watchlist"])
        self.assertEqual(plan["counts"], {
            "active": 2,
            "watchlist": 2,
            "hot": 2,
            "resolved": 4,
        })

    def test_max_symbols_caps_after_priority_ordering(self):
        plan = resolve_trade_subscription_plan(
            active_symbols=["AAPL"],
            watchlist_symbols=["MSFT", "JPM"],
            hot_symbols=["TSLA"],
            max_symbols=2,
        )

        self.assertEqual(plan["symbols"], ["AAPL", "MSFT"])
        self.assertNotIn("JPM", plan["tiersBySymbol"])
        self.assertEqual(plan["counts"]["resolved"], 2)

    def test_watchlist_and_hot_caps_are_applied_before_priority_merge(self):
        plan = resolve_trade_subscription_plan(
            active_symbols=["AAPL", "MSFT"],
            watchlist_symbols=["JPM", "TSLA", "AMZN"],
            hot_symbols=["META", "GOOGL", "WMT"],
            max_watchlist_symbols=2,
            max_hot_symbols=1,
        )

        self.assertEqual(plan["symbols"], ["AAPL", "MSFT", "JPM", "TSLA", "META"])
        self.assertEqual(plan["counts"], {
            "active": 2,
            "watchlist": 2,
            "hot": 1,
            "resolved": 5,
        })

    def test_zero_tier_caps_disable_watchlist_and_hot_without_removing_active_symbols(self):
        plan = resolve_trade_subscription_plan(
            active_symbols=["AAPL", "MSFT", "JPM"],
            watchlist_symbols=["TSLA", "AMZN"],
            hot_symbols=["META", "GOOGL"],
            max_watchlist_symbols=0,
            max_hot_symbols=0,
        )

        self.assertEqual(plan["symbols"], ["AAPL", "MSFT", "JPM"])
        self.assertEqual(plan["counts"], {
            "active": 3,
            "watchlist": 0,
            "hot": 0,
            "resolved": 3,
        })

    def test_invalid_symbols_are_ignored_before_capping(self):
        plan = resolve_trade_subscription_plan(
            active_symbols=["aapl", "bad-symbol"],
            watchlist_symbols=["MSFT", ""],
            hot_symbols=["JPM", "MSFT"],
        )

        self.assertEqual(plan["symbols"], ["AAPL", "MSFT", "JPM"])


if __name__ == "__main__":
    unittest.main()
