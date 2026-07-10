import unittest

from alfaka.orderflow import (
    ORDER_FLOW_CLASSIFICATION_VERSION,
    classify_trade_side,
    merge_trades_with_quotes,
    normalize_quotes,
    normalize_trades,
)


class OrderFlowClassificationTest(unittest.TestCase):
    def test_classifies_estimated_bid_ask_unknown_with_asof_quotes(self):
        quotes = normalize_quotes([
            {"timestamp": "2026-06-25T13:30:00.000Z", "bidPrice": 100.0, "askPrice": 100.1},
            {"timestamp": "2026-06-25T13:31:00.000Z", "bidPrice": 100.2, "askPrice": 100.3},
        ])
        trades = normalize_trades([
            {"timestamp": "2026-06-25T13:30:01.000Z", "price": 100.1, "size": 10},
            {"timestamp": "2026-06-25T13:30:02.000Z", "price": 100.0, "size": 4},
            {"timestamp": "2026-06-25T13:30:03.000Z", "price": 100.05, "size": 2},
            {"timestamp": "2026-06-25T13:31:02.000Z", "price": 100.31, "size": 5},
        ])

        merged = list(merge_trades_with_quotes(trades, quotes))
        sides = [classify_trade_side(trade, quote) for trade, quote in merged]

        self.assertEqual(ORDER_FLOW_CLASSIFICATION_VERSION, "orderflow-estimated-v2")
        self.assertEqual(sides, ["ask", "bid", "unknown", "ask"])
        self.assertEqual(sum(trade["size"] for (trade, _), side in zip(merged, sides) if side == "ask"), 15)
        self.assertEqual(sum(trade["size"] for (trade, _), side in zip(merged, sides) if side == "bid"), 4)
        self.assertEqual(sum(trade["size"] for (trade, _), side in zip(merged, sides) if side == "unknown"), 2)

    def test_trade_without_quote_is_unknown(self):
        side = classify_trade_side({"price": 100.1}, None)

        self.assertEqual(side, "unknown")

    def test_trade_with_missing_or_invalid_price_is_unknown(self):
        quote = {"bidPrice": 100.0, "askPrice": 100.1}

        self.assertEqual(classify_trade_side({}, quote), "unknown")
        self.assertEqual(classify_trade_side({"price": "100.1"}, quote), "unknown")

    def test_initial_quote_carries_across_window_boundary(self):
        initial_quote = normalize_quotes([
            {"timestamp": "2026-06-25T13:59:59.000Z", "bidPrice": 101.0, "askPrice": 101.2},
        ])[0]
        trades = normalize_trades([
            {"timestamp": "2026-06-25T14:00:01.000Z", "price": 101.19, "size": 3},
        ])

        merged = list(merge_trades_with_quotes(trades, [], initial_quote=initial_quote))

        self.assertIs(merged[0][1], initial_quote)
        self.assertEqual(classify_trade_side(*merged[0]), "ask")

    def test_quote_age_guard_marks_stale_quote_unknown(self):
        quote = normalize_quotes([
            {"timestamp": "2026-06-25T13:30:00.000Z", "bidPrice": 100.0, "askPrice": 100.1},
        ])[0]
        trade = normalize_trades([
            {"timestamp": "2026-06-25T13:30:10.000Z", "price": 100.2, "size": 5},
        ])[0]

        stale_side = classify_trade_side(trade, quote, max_quote_age_ms=2000, future_tolerance_ms=250)
        fresh_side = classify_trade_side(trade, quote, max_quote_age_ms=11000, future_tolerance_ms=250)

        self.assertEqual(stale_side, "unknown")
        self.assertEqual(fresh_side, "ask")

    def test_quote_age_guard_accepts_alpaca_nanosecond_timestamps(self):
        quote = normalize_quotes([
            {"timestamp": "2026-06-25T13:30:00.928442175Z", "bidPrice": 100.0, "askPrice": 100.1},
        ])[0]
        trade = normalize_trades([
            {"timestamp": "2026-06-25T13:30:01.000Z", "price": 100.2, "size": 5},
        ])[0]

        self.assertEqual(classify_trade_side(trade, quote, max_quote_age_ms=2000, future_tolerance_ms=250), "ask")

    def test_quote_age_guard_allows_small_future_tolerance(self):
        quote = normalize_quotes([
            {"timestamp": "2026-06-25T13:30:02.000Z", "bidPrice": 100.0, "askPrice": 100.1},
        ])[0]
        trade = normalize_trades([
            {"timestamp": "2026-06-25T13:30:00.000Z", "price": 100.2, "size": 5},
        ])[0]

        self.assertEqual(classify_trade_side(trade, quote, max_quote_age_ms=2000, future_tolerance_ms=2500), "ask")
        self.assertEqual(classify_trade_side(trade, quote, max_quote_age_ms=2000, future_tolerance_ms=250), "unknown")


if __name__ == "__main__":
    unittest.main()
