import unittest

from alfaka.serving.volume_profile import compute_volume_profile_payload


class VolumeProfileCalculationTest(unittest.TestCase):
    def test_computes_display_buckets_poc_and_value_area(self):
        payload = compute_volume_profile_payload(
            {
                "source": "unit",
                "feed": "sip",
                "bins": [
                    {"priceBin": 100.0, "priceBinSize": 0.25, "volume": 10, "tradeCount": 1, "vwap": 100.1},
                    {"priceBin": 100.5, "priceBinSize": 0.25, "volume": 50, "tradeCount": 5, "vwap": 100.55},
                    {"priceBin": 101.0, "priceBinSize": 0.25, "volume": 30, "tradeCount": 3, "vwap": 101.05},
                    {"priceBin": 102.0, "priceBinSize": 0.25, "volume": 5, "tradeCount": 1, "vwap": 102.1},
                ],
            },
            symbol="AAPL",
            from_time="2026-06-25T13:30:00.000Z",
            to_time="2026-06-25T14:00:00.000Z",
            target_bins=4,
            price_min=100,
            price_max=102.5,
        )

        self.assertEqual(payload["symbol"], "AAPL")
        self.assertEqual(payload["calculationVersion"], "volume-profile-v1")
        self.assertEqual(payload["targetBins"], 4)
        self.assertGreaterEqual(payload["bucketCount"], 3)
        self.assertEqual(payload["totalVolume"], 95)
        self.assertEqual(payload["poc"]["volume"], 60)
        self.assertEqual(payload["poc"]["priceMin"], 100.0)
        self.assertGreaterEqual(payload["valueArea"]["volumePercent"], 0.7)
        self.assertTrue(any(bucket["isPoc"] for bucket in payload["bins"]))
        self.assertTrue(any(bucket["inValueArea"] for bucket in payload["bins"]))

    def test_empty_payload_keeps_requested_range(self):
        payload = compute_volume_profile_payload(
            {"bins": []},
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
