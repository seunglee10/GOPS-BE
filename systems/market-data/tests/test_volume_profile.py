import unittest

from alfaka.serving.volume_profile import compute_volume_profile_payload


class VolumeProfileCalculationTest(unittest.TestCase):
    def test_estimates_display_buckets_from_candle_range_overlap(self):
        payload = compute_volume_profile_payload(
            {
                "source": "unit",
                "feed": "sip",
                "candles": [
                    {"timestamp": "2026-06-25T13:30:00.000Z", "low": 100, "high": 102, "close": 101, "volume": 100},
                    {"timestamp": "2026-06-25T13:31:00.000Z", "low": 101, "high": 101, "close": 101, "volume": 40},
                ],
            },
            symbol="AAPL",
            interval="1m",
            from_time="2026-06-25T13:30:00.000Z",
            to_time="2026-06-25T14:00:00.000Z",
            target_bins=4,
            price_min=100,
            price_max=102,
        )

        self.assertEqual(payload["symbol"], "AAPL")
        self.assertEqual(payload["interval"], "1m")
        self.assertEqual(payload["calculationVersion"], "volume-profile-v1")
        self.assertEqual(payload["sideClassification"], "estimated")
        self.assertEqual(payload["estimationMethod"], "candle-range-volume-overlap")
        self.assertEqual(payload["targetBins"], 4)
        self.assertEqual(payload["bucketCount"], 4)
        self.assertEqual(payload["totalVolume"], 140)
        self.assertEqual([bucket["volume"] for bucket in payload["bins"]], [25, 25, 65, 25])
        self.assertEqual(payload["poc"]["volume"], 65)
        self.assertEqual(payload["poc"]["priceMin"], 101.0)
        self.assertGreaterEqual(payload["valueArea"]["volumePercent"], 0.7)
        self.assertTrue(any(bucket["isPoc"] for bucket in payload["bins"]))
        self.assertTrue(any(bucket["inValueArea"] for bucket in payload["bins"]))

    def test_estimates_volume_profile_for_all_chart_intervals(self):
        for interval in ("1m", "5m", "10m", "1D", "1W", "1M"):
            with self.subTest(interval=interval):
                payload = compute_volume_profile_payload(
                    {"candles": [{"low": 10, "high": 11, "close": 10.5, "volume": 7}]},
                    symbol="AAPL",
                    interval=interval,
                    from_time="from",
                    to_time="to",
                    target_bins=10,
                )

                self.assertEqual(payload["interval"], interval)
                self.assertEqual(payload["sourceInterval"], interval)
                self.assertEqual(payload["sideClassification"], "estimated")
                self.assertEqual(payload["totalVolume"], 7)

    def test_empty_payload_keeps_requested_range(self):
        payload = compute_volume_profile_payload(
            {"candles": []},
            symbol="AAPL",
            from_time="from",
            to_time="to",
            target_bins=10,
            price_min=99,
            price_max=101,
        )

        self.assertEqual(payload["dataStatus"], "empty")
        self.assertEqual(payload["priceRange"]["min"], 99)
        self.assertEqual(payload["priceRange"]["max"], 101)
        self.assertEqual(payload["bins"], [])


if __name__ == "__main__":
    unittest.main()
