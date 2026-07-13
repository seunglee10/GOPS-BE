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
        for interval in ("1m", "5m", "10m", "1h", "4h", "1D", "1W", "1M"):
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

    def test_exact_mode_returns_requested_equal_width_bucket_count(self):
        payload = compute_volume_profile_payload(
            {
                "source": "unit",
                "feed": "sip",
                "candles": [
                    {"timestamp": "2026-06-25T13:30:00.000Z", "low": 100, "high": 102, "close": 101, "volume": 100},
                ],
            },
            symbol="AAPL",
            interval="1m",
            from_time="2026-06-25T13:30:00.000Z",
            to_time="2026-06-25T14:00:00.000Z",
            target_bins=10,
            price_min=100,
            price_max=102,
            binning_mode="exact",
        )

        self.assertEqual(payload["calculationVersion"], "volume-profile-exact-v2")
        self.assertEqual(payload["targetBins"], 10)
        self.assertEqual(payload["bucketCount"], 10)
        self.assertEqual(len(payload["bins"]), 10)
        self.assertEqual(payload["priceBinSize"], 0.2)
        self.assertEqual(payload["bins"][0]["priceMin"], 100.0)
        self.assertEqual(payload["bins"][-1]["priceMax"], 102.0)
        self.assertTrue(all(bucket["priceBinSize"] == 0.2 for bucket in payload["bins"]))
        self.assertAlmostEqual(sum(bucket["volume"] for bucket in payload["bins"]), 100)

    def test_exact_mode_retains_zero_volume_price_slots(self):
        payload = compute_volume_profile_payload(
            {"candles": [{"low": 105, "high": 105, "close": 105, "volume": 40}]},
            symbol="AAPL",
            from_time="from",
            to_time="to",
            target_bins=10,
            price_min=100,
            price_max=110,
            binning_mode="exact",
        )

        self.assertEqual(payload["bucketCount"], 10)
        self.assertEqual(sum(bucket["volume"] == 0 for bucket in payload["bins"]), 9)
        self.assertEqual(payload["bins"][5]["volume"], 40)
        self.assertEqual(payload["poc"]["index"], 5)
        self.assertEqual(payload["totalVolume"], 40)

    def test_exact_mode_preserves_decimal_boundaries_and_total_volume(self):
        payload = compute_volume_profile_payload(
            {"candles": [{"low": 99.97, "high": 100.13, "close": 100.03, "volume": 13}]},
            symbol="AAPL",
            from_time="from",
            to_time="to",
            target_bins=10,
            price_min=99.97,
            price_max=100.13,
            binning_mode="exact",
        )

        self.assertEqual(len(payload["bins"]), 10)
        self.assertEqual(payload["bins"][0]["priceMin"], 99.97)
        self.assertEqual(payload["bins"][-1]["priceMax"], 100.13)
        self.assertAlmostEqual(sum(bucket["volume"] for bucket in payload["bins"]), 13)
        self.assertAlmostEqual(payload["totalVolume"], 13)

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

        exact_payload = compute_volume_profile_payload(
            {"candles": []},
            symbol="AAPL",
            from_time="from",
            to_time="to",
            target_bins=10,
            price_min=99,
            price_max=101,
            binning_mode="exact",
        )
        self.assertEqual(exact_payload["calculationVersion"], "volume-profile-exact-v2")
        self.assertEqual(exact_payload["bucketCount"], 0)
        self.assertEqual(exact_payload["bins"], [])


if __name__ == "__main__":
    unittest.main()
