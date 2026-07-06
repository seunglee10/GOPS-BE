import unittest

from alfaka.serving.footprint import compute_footprint_payload


class FootprintCalculationTest(unittest.TestCase):
    def test_computes_estimated_bid_ask_delta_by_minute(self):
        payload = compute_footprint_payload(
            {
                "source": "unit",
                "feed": "sip",
                "quotes": [
                    {
                        "timestamp": "2026-06-25T13:30:00.000Z",
                        "bidPrice": 100.0,
                        "askPrice": 100.1,
                    },
                    {
                        "timestamp": "2026-06-25T13:31:00.000Z",
                        "bidPrice": 100.2,
                        "askPrice": 100.3,
                    },
                ],
                "trades": [
                    {"timestamp": "2026-06-25T13:30:01.000Z", "price": 100.1, "size": 10},
                    {"timestamp": "2026-06-25T13:30:02.000Z", "price": 100.0, "size": 4},
                    {"timestamp": "2026-06-25T13:30:03.000Z", "price": 100.05, "size": 2},
                    {"timestamp": "2026-06-25T13:31:02.000Z", "price": 100.31, "size": 5},
                ],
            },
            symbol="AAPL",
            from_time="2026-06-25T13:30:00.000Z",
            to_time="2026-06-25T13:32:00.000Z",
        )

        self.assertEqual(payload["interval"], "footprint")
        self.assertEqual(payload["sourceInterval"], "1m")
        self.assertEqual(payload["sideClassification"], "estimated")
        self.assertEqual(payload["classificationVersion"], "footprint-estimated-v1")
        self.assertEqual(payload["tradeCount"], 4)
        self.assertEqual(len(payload["buckets"]), 2)
        first = payload["buckets"][0]
        self.assertEqual(first["timestamp"], "2026-06-25T13:30:00.000Z")
        self.assertEqual(first["askVolume"], 10)
        self.assertEqual(first["bidVolume"], 4)
        self.assertEqual(first["unknownVolume"], 2)
        self.assertEqual(first["delta"], 6)
        self.assertEqual(first["priceLevels"][0]["price"], 100.1)
        self.assertEqual(payload["buckets"][1]["askVolume"], 5)

    def test_trade_without_quote_is_unknown(self):
        payload = compute_footprint_payload(
            {
                "trades": [
                    {"timestamp": "2026-06-25T13:30:01.000Z", "price": 100.1, "size": 10},
                ],
                "quotes": [],
            },
            symbol="AAPL",
            from_time="2026-06-25T13:30:00.000Z",
            to_time="2026-06-25T13:31:00.000Z",
        )

        self.assertEqual(payload["buckets"][0]["unknownVolume"], 10)
        self.assertEqual(payload["buckets"][0]["delta"], 0)


if __name__ == "__main__":
    unittest.main()
